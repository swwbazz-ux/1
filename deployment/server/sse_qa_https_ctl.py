"""Fixed-purpose HTTPS controller for the isolated SSE QA installation.

This controller deliberately exposes only fixed operations:

* ``inspect`` performs read-only readiness checks;
* ``prepare`` installs the fixed renewal controller, issues/validates the
  certificate and updates the single owned nginx ``allow`` directive;
* ``renew-pre``, ``renew-post`` and ``renew-deploy`` are fixed Certbot lineage
  hooks which expose only the HTTP-01 challenge and never the QA application.

There are no caller supplied paths, hostnames or commands.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
import re
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath


QA_HOSTNAME = "sse-qa.driverform.ru"
EXPECTED_PUBLIC_IPV4 = "77.91.93.47"
MARKER = "SSE_QA_INSTALLATION_V2"
OWNERSHIP_SCHEMA = "SSE_QA_OWNERSHIP_V2"
REAL_ROOT = Path("/")
STATE_ROOT = Path("/var/lib/sse-qa")
OWNERSHIP_PATH = STATE_ROOT / "OWNERSHIP.json"
INSTALLATION_MARKER = STATE_ROOT / "INSTALLATION_MARKER"
APP_ENV = Path("/etc/sse-qa/app.env")
QA_NGINX_CONFIG = Path("/etc/sse-qa/nginx.conf")
QA_NGINX_SITE = Path("/etc/nginx/sites-enabled/sse-qa.conf")
ACME_CONFIG = Path("/etc/nginx/sites-available/sse-qa-acme.conf")
ACME_SITE = Path("/etc/nginx/sites-enabled/sse-qa-acme.conf")
ACME_WEBROOT = Path("/var/lib/letsencrypt/sse-qa")
ACME_WEBROOT_MARKER = ACME_WEBROOT / ".sse-qa-owned"
CERTBOT = Path("/usr/bin/certbot")
CERT_LIVE = Path("/etc/letsencrypt/live") / QA_HOSTNAME
CERT_ARCHIVE = Path("/etc/letsencrypt/archive") / QA_HOSTNAME
CERT_RENEWAL = Path("/etc/letsencrypt/renewal") / f"{QA_HOSTNAME}.conf"
CERTBOT_ACCOUNTS = Path("/etc/letsencrypt/accounts")
HOOK_CONTROLLER = Path("/usr/local/libexec/sse-qa-https-hook")
ACME_OWNERSHIP = Path("/run/sse-qa-acme-ownership.json")
QA_SERVICES = (
    "sse-qa-asgi.service",
    "sse-qa-wsgi.service",
    "sse-qa-reconcile.service",
    "redis-sse-qa.service",
    "postgresql@16-sseqa.service",
)
QA_SLICE_CGROUP = "/sse.slice/sse-qa.slice"
PREPARE_UNIT = "sse-qa-https.service"
COMMAND_TIMEOUT = 120
CERTBOT_TIMEOUT = 300
ROLLBACK_COMMAND_TIMEOUT = 20
ROLLBACK_CERTBOT_TIMEOUT = 30
ROLLBACK_BUDGET_SECONDS = 75

ACME_CONFIG_BYTES = f"""server {{
    listen 80;
    server_name {QA_HOSTNAME};

    location ^~ /.well-known/acme-challenge/ {{
        root {ACME_WEBROOT};
        default_type text/plain;
        try_files $uri =404;
    }}

    location / {{ return 404; }}
}}
""".encode("ascii")

WINDOWS_TEST_SYMLINK_BYTES = (ACME_CONFIG.as_posix() + "\n").encode("ascii")
HOOK_PRE_COMMAND = f"/usr/bin/python3 {HOOK_CONTROLLER} renew-pre"
HOOK_POST_COMMAND = f"/usr/bin/python3 {HOOK_CONTROLLER} renew-post"
HOOK_DEPLOY_COMMAND = f"/usr/bin/python3 {HOOK_CONTROLLER} renew-deploy"
_STEP = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_ACME_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

# Exact accepted installer template from immutable C2 commit
# 9d336723f3dc2fc574937a57602a27b54c54fd77, Git blob
# 70baadc6873ec8180dad38329076be9fe0cb5e8c.  The HTTPS controller is
# delivered without the installer bundle, so it carries the complete pinned
# contract and substitutes only the already validated allow CIDR.
C2_NGINX_TEMPLATE = """limit_conn_zone $server_name zone=sse_qa_total:32k;
limit_conn_zone $binary_remote_addr zone=sse_qa_per_ip:64k;

upstream sse_qa_wsgi {
    server 127.0.0.1:18080;
}

upstream sse_qa_asgi {
    server 127.0.0.1:18082;
}

server {
    listen 443 ssl http2;
    server_name sse-qa.driverform.ru;
    access_log /srv/sse-qa/log/nginx-access.log combined buffer=16k flush=5s;
    error_log /srv/sse-qa/log/nginx-error.log warn;

    ssl_certificate /etc/letsencrypt/live/sse-qa.driverform.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sse-qa.driverform.ru/privkey.pem;

    auth_basic "SSE QA";
    auth_basic_user_file /run/sse-qa-nginx/htpasswd;
    satisfy all;
    allow @@ALLOW_CIDR@@;
    deny all;

    add_header X-Robots-Tag "noindex, nofollow, noarchive" always;
    limit_conn sse_qa_per_ip 8;
    client_max_body_size 2m;

    location = /realtime/stream/ {
        limit_conn sse_qa_total 2;
        proxy_pass http://sse_qa_asgi;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        add_header X-Accel-Buffering no always;
        proxy_read_timeout 35s;
        proxy_send_timeout 35s;
    }

    location /static/ {
        alias /srv/sse-qa/current/backend/staticfiles/;
        access_log off;
        expires 5m;
    }

    location /media/ {
        return 404;
    }

    location / {
        proxy_pass http://sse_qa_wsgi;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 3s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }
}
"""
C2_NGINX_TEMPLATE_SHA256 = (
    "a9d33543398df5188734e152d056e04e3e27770e09bcf57ce73eba42f9e710c9"
)

_C2_NGINX_CONTRACT_FRAGMENTS = (
    ("nginx_c2_hostname_v1", "    server_name sse-qa.driverform.ru;\n"),
    (
        "nginx_c2_tls_paths_v1",
        "    ssl_certificate /etc/letsencrypt/live/sse-qa.driverform.ru/fullchain.pem;\n"
        "    ssl_certificate_key /etc/letsencrypt/live/sse-qa.driverform.ru/privkey.pem;\n",
    ),
    (
        "nginx_c2_access_control_v1",
        "    auth_basic \"SSE QA\";\n"
        "    auth_basic_user_file /run/sse-qa-nginx/htpasswd;\n"
        "    satisfy all;\n"
        "    allow @@ALLOW_CIDR@@;\n"
        "    deny all;\n",
    ),
    (
        "nginx_c2_upstream_wsgi_v1",
        "upstream sse_qa_wsgi {\n"
        "    server 127.0.0.1:18080;\n"
        "}\n",
    ),
    (
        "nginx_c2_upstream_asgi_v1",
        "upstream sse_qa_asgi {\n"
        "    server 127.0.0.1:18082;\n"
        "}\n",
    ),
    (
        "nginx_c2_route_realtime_v1",
        "    location = /realtime/stream/ {\n"
        "        limit_conn sse_qa_total 2;\n"
        "        proxy_pass http://sse_qa_asgi;\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Connection \"\";\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Forwarded-Proto https;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_buffering off;\n"
        "        proxy_cache off;\n"
        "        gzip off;\n"
        "        add_header X-Accel-Buffering no always;\n"
        "        proxy_read_timeout 35s;\n"
        "        proxy_send_timeout 35s;\n"
        "    }\n",
    ),
    (
        "nginx_c2_route_application_v1",
        "    location / {\n"
        "        proxy_pass http://sse_qa_wsgi;\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Forwarded-Proto https;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_connect_timeout 3s;\n"
        "        proxy_read_timeout 30s;\n"
        "        proxy_send_timeout 30s;\n"
        "    }\n",
    ),
)


class QaHttpsError(RuntimeError):
    pass


class QaHttpsCancelled(QaHttpsError):
    pass


class FixedCommandError(QaHttpsError):
    def __init__(
        self, step: str, *, exit_code: int | None = None,
        timed_out: bool = False, diagnostic: str = "none",
    ) -> None:
        self.step = step
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.diagnostic = diagnostic
        status = "timeout" if timed_out else f"exit={exit_code}"
        super().__init__(f"step={step} {status} diagnostic={diagnostic}")


class ChallengeOwnership:
    def __init__(
        self, *, webroot: bool = False, config: bool = False, site: bool = False,
    ) -> None:
        self.webroot = webroot
        self.config = config
        self.site = site


def _safe_diagnostic(step: str, output: str) -> str:
    if step == "nginx-render":
        return "nginx_output_withheld"
    # External tools can print credentials in formats that cannot be safely
    # redacted with a finite list of regular expressions.  Preserve the fixed
    # command step and status in FixedCommandError, but never echo arbitrary
    # command output into receiver diagnostics.
    return "command_output_withheld" if output.strip() else "none"


@contextmanager
def _defer_sigterm():
    """Defer SIGTERM across a short ownership publication transaction."""
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if pthread_sigmask is None:
        yield
        return
    previous = pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        yield
    finally:
        pthread_sigmask(signal.SIG_SETMASK, previous)


def rooted(root: Path, absolute: Path) -> Path:
    text = absolute.as_posix()
    parsed = PurePosixPath(text)
    if not text.startswith("/") or ".." in parsed.parts:
        raise QaHttpsError("internal path must be absolute")
    return root.joinpath(*parsed.parts[1:])


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    command: list[str], *, step: str, check: bool = True,
    timeout: int = COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    if not _STEP.fullmatch(step):
        raise QaHttpsError("invalid fixed command step")
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=5)
        raise FixedCommandError(
            step, timed_out=True, diagnostic=_safe_diagnostic(step, output or "")
        ) from exc
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    completed = subprocess.CompletedProcess(
        command, process.returncode, stdout=output, stderr=None,
    )
    if check and completed.returncode != 0:
        raise FixedCommandError(
            step,
            exit_code=completed.returncode,
            diagnostic=_safe_diagnostic(step, output or ""),
        )
    return completed


def systemctl_property(unit: str, name: str) -> str:
    return run(
        ["systemctl", "show", unit, "--property", name, "--value"],
        step="systemctl-read",
    ).stdout.strip()


def current_unified_cgroup() -> str:
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and controllers == "":
            return path
    raise QaHttpsError("unified process cgroup is unavailable")


def assert_prepare_scope() -> None:
    if systemctl_property(PREPARE_UNIT, "Slice") != "sse-qa.slice":
        raise QaHttpsError("HTTPS preparation is outside QA slice")
    parent = systemctl_property("sse-qa.slice", "ControlGroup")
    expected = f"{QA_SLICE_CGROUP}/{PREPARE_UNIT}"
    if parent != QA_SLICE_CGROUP:
        raise QaHttpsError("QA slice cgroup hierarchy mismatch")
    if systemctl_property(PREPARE_UNIT, "ControlGroup") != expected:
        raise QaHttpsError("HTTPS preparation unit cgroup mismatch")
    if current_unified_cgroup() != expected:
        raise QaHttpsError("HTTPS preparation process cgroup mismatch")


def atomic_write(path: Path, data: bytes, mode: int, *, replace: bool) -> None:
    if path.is_symlink() or (path.exists() and not replace):
        raise QaHttpsError(f"managed path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except (FileExistsError, FileNotFoundError) as exc:
                raise QaHttpsError(f"managed path changed before publish: {path}") from exc
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def save_ownership(root: Path, state: dict[str, object]) -> None:
    path = rooted(root, OWNERSHIP_PATH)
    atomic_write(
        path,
        (json.dumps(state, sort_keys=True) + "\n").encode("utf-8"),
        0o600,
        replace=True,
    )


def _render_c2_nginx(allow_cidr: str) -> str:
    if digest_bytes(C2_NGINX_TEMPLATE.encode("utf-8")) != C2_NGINX_TEMPLATE_SHA256:
        raise QaHttpsError("pinned C2 nginx template digest mismatch")
    rendered = C2_NGINX_TEMPLATE.replace("@@ALLOW_CIDR@@", allow_cidr)
    if "@@" in rendered:
        raise QaHttpsError("pinned C2 nginx template marker mismatch")
    return rendered


def _nginx_contract_id(actual: str, expected: str, allow_cidr: str) -> str:
    """Classify an exact-template mismatch without exposing file content.

    These fragment checks are diagnostics only.  Acceptance is decided solely
    by full equality with the pinned rendered C2 template.
    """
    for contract_id, template_fragment in _C2_NGINX_CONTRACT_FRAGMENTS:
        fragment = template_fragment.replace("@@ALLOW_CIDR@@", allow_cidr)
        if actual.count(fragment) != expected.count(fragment):
            return contract_id
    return "nginx_c2_exact_template_v1"


def _validate_installed_nginx(nginx_text: str) -> str:
    allow_values = re.findall(
        r"(?m)^[ \t]*allow[ \t]+([^;\r\n]+);[ \t]*$", nginx_text,
    )
    if len(allow_values) != 1:
        raise QaHttpsError(
            "installed QA nginx template mismatch "
            "contract_id=nginx_c2_allow_cidr_v1"
        )
    try:
        allow_cidr = validate_allow_cidr(allow_values[0])
    except QaHttpsError as exc:
        raise QaHttpsError(
            "installed QA nginx template mismatch "
            "contract_id=nginx_c2_allow_cidr_v1"
        ) from exc
    expected = _render_c2_nginx(allow_cidr)
    if nginx_text != expected:
        contract_id = _nginx_contract_id(nginx_text, expected, allow_cidr)
        raise QaHttpsError(
            f"installed QA nginx template mismatch contract_id={contract_id}"
        )
    return allow_cidr


def load_disabled_installation(root: Path) -> tuple[dict[str, object], str]:
    marker = rooted(root, INSTALLATION_MARKER)
    if not marker.is_file() or marker.is_symlink():
        raise QaHttpsError("complete QA installation marker is missing")
    if marker.read_text(encoding="utf-8").strip() != MARKER:
        raise QaHttpsError("unknown QA installation marker")
    ownership_path = rooted(root, OWNERSHIP_PATH)
    if not ownership_path.is_file() or ownership_path.is_symlink():
        raise QaHttpsError("QA ownership journal is missing")
    try:
        state = json.loads(ownership_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QaHttpsError("QA ownership journal is invalid") from exc
    if (
        not isinstance(state, dict)
        or state.get("schema") != OWNERSHIP_SCHEMA
        or state.get("complete") is not True
        or state.get("phase") != "complete_disabled"
        or not isinstance(state.get("files"), dict)
    ):
        raise QaHttpsError("QA must be complete and disabled")
    files = state["files"]
    assert isinstance(files, dict)
    for logical in (APP_ENV.as_posix(), QA_NGINX_CONFIG.as_posix()):
        expected = files.get(logical)
        target = rooted(root, Path(logical))
        if (
            not isinstance(expected, str)
            or not target.is_file()
            or target.is_symlink()
            or digest_path(target) != expected
        ):
            raise QaHttpsError(f"owned QA file changed or missing: {logical}")
    app_env = rooted(root, APP_ENV).read_text(encoding="utf-8")
    enabled = re.findall(r"(?m)^SSE_PILOT_ENABLED=(true|false)$", app_env)
    if enabled != ["false"]:
        raise QaHttpsError("QA kill switch must be false")
    site = rooted(root, QA_NGINX_SITE)
    if site.exists() or site.is_symlink():
        raise QaHttpsError("QA nginx site must remain disabled")
    nginx_text = rooted(root, QA_NGINX_CONFIG).read_text(encoding="utf-8")
    allow_cidr = _validate_installed_nginx(nginx_text)
    if root == REAL_ROOT:
        active = [
            unit for unit in QA_SERVICES
            if systemctl_property(unit, "ActiveState") != "inactive"
        ]
        if active:
            raise QaHttpsError("QA services must remain inactive")
    return state, allow_cidr


def validate_allow_cidr(value: str) -> str:
    if value != value.strip() or len(value) > 18:
        raise QaHttpsError("allow_cidr must be one canonical IPv4 /32")
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise QaHttpsError("allow_cidr must be one canonical IPv4 /32") from exc
    if network.version != 4 or network.prefixlen != 32 or str(network) != value:
        raise QaHttpsError("allow_cidr must be one canonical IPv4 /32")
    return value


def dns_matches() -> bool:
    values = {
        item[4][0]
        for item in socket.getaddrinfo(QA_HOSTNAME, 443, socket.AF_INET, socket.SOCK_STREAM)
    }
    return values == {EXPECTED_PUBLIC_IPV4}


def certbot_ready(root: Path) -> None:
    binary = rooted(root, CERTBOT)
    if not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK):
        raise QaHttpsError("fixed Certbot binary is unavailable")
    if root == REAL_ROOT:
        if systemctl_property("certbot.timer", "UnitFileState") != "enabled":
            raise QaHttpsError("certbot.timer is not enabled")
        if systemctl_property("certbot.timer", "ActiveState") != "active":
            raise QaHttpsError("certbot.timer is not active")
    accounts = rooted(root, CERTBOT_ACCOUNTS)
    if not accounts.is_dir() or accounts.is_symlink():
        raise QaHttpsError("existing Certbot account store is unavailable")
    registrations = [
        item for item in accounts.rglob("regr.json")
        if item.is_file() and not item.is_symlink()
    ]
    if not registrations:
        raise QaHttpsError("existing Certbot account is unavailable")


def _nginx_tokens(rendered: str) -> list[str]:
    """Tokenize enough nginx syntax to parse directives fail-closed."""
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    comment = False

    def flush() -> None:
        if current:
            tokens.append("".join(current))
            current.clear()

    for char in rendered:
        if comment:
            if char == "\n":
                comment = False
            continue
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#":
            flush()
            comment = True
            continue
        if char.isspace():
            flush()
            continue
        if char in ";{}":
            flush()
            tokens.append(char)
            continue
        current.append(char)
    flush()
    if quote is not None or escaped:
        raise QaHttpsError("nginx configuration tokenization is ambiguous")
    return tokens


def nginx_conflict() -> bool:
    rendered = run(["nginx", "-T"], step="nginx-render").stdout
    tokens = _nginx_tokens(rendered)
    index = 0
    while index < len(tokens):
        if tokens[index].casefold() != "server_name":
            index += 1
            continue
        index += 1
        names: list[str] = []
        while index < len(tokens) and tokens[index] != ";":
            if tokens[index] in {"{", "}"}:
                raise QaHttpsError("nginx server_name directive is ambiguous")
            names.append(tokens[index])
            index += 1
        if index >= len(tokens):
            raise QaHttpsError("nginx server_name directive is unterminated")
        if QA_HOSTNAME in names:
            return True
        index += 1
    return False


def _resolved_cert_file(root: Path, logical: Path) -> Path:
    path = rooted(root, logical)
    try:
        resolved = path.resolve(strict=True)
        archive = rooted(root, CERT_ARCHIVE).resolve(strict=True)
    except OSError as exc:
        raise QaHttpsError("QA certificate lineage is incomplete") from exc
    if not resolved.is_file() or archive not in resolved.parents:
        raise QaHttpsError("QA certificate target escapes its fixed lineage")
    return path


def _parse_renewal_config(text: str) -> dict[str, dict[str, str]]:
    """Parse the small ConfigObj subset used by Certbot renewal files."""
    sections: dict[str, dict[str, str]] = {"lineage": {}}
    current = "lineage"
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        nested = re.fullmatch(r"\[\[([A-Za-z0-9_-]+)\]\]", line)
        ordinary = re.fullmatch(r"\[([A-Za-z0-9_-]+)\]", line)
        if nested:
            if current != "renewalparams" or nested.group(1) != "webroot_map":
                raise QaHttpsError("QA renewal configuration section is unsupported")
            current = "webroot_map"
            sections.setdefault(current, {})
            continue
        if ordinary:
            current = ordinary.group(1)
            if current != "renewalparams" or current in sections:
                raise QaHttpsError("QA renewal configuration section is unsupported or repeated")
            sections[current] = {}
            continue
        matched = re.fullmatch(r"([^=\s][^=]*?)\s*=\s*(.*?)\s*", line)
        if not matched:
            raise QaHttpsError(
                f"QA renewal configuration line {number} is unsupported"
            )
        key, value = matched.group(1).strip(), matched.group(2)
        if not key or key in sections[current]:
            raise QaHttpsError("QA renewal configuration key is repeated")
        sections[current][key] = value
    return sections


def _parse_configobj_path_list(value: str) -> list[str]:
    """Parse the scalar/list syntax ConfigObj uses for webroot_path."""
    if not value or any(character in value for character in "'\""):
        raise QaHttpsError("QA renewal webroot path syntax is ambiguous")
    if "," not in value:
        return [value]
    parts = [part.strip() for part in value.split(",")]
    if parts[-1] == "":
        parts.pop()
    if not parts or any(not part for part in parts):
        raise QaHttpsError("QA renewal webroot path list is empty or ambiguous")
    return parts


def _validate_renewal_config(text: str) -> None:
    sections = _parse_renewal_config(text)
    lineage = sections.get("lineage", {})
    renewal = sections.get("renewalparams", {})
    webroot_map = sections.get("webroot_map", {})
    expected_lineage = {
        "archive_dir": CERT_ARCHIVE.as_posix(),
        "cert": (CERT_LIVE / "cert.pem").as_posix(),
        "privkey": (CERT_LIVE / "privkey.pem").as_posix(),
        "chain": (CERT_LIVE / "chain.pem").as_posix(),
        "fullchain": (CERT_LIVE / "fullchain.pem").as_posix(),
    }
    if any(lineage.get(key) != value for key, value in expected_lineage.items()):
        raise QaHttpsError("QA renewal configuration does not use the fixed lineage")
    expected_renewal = {
        "authenticator": "webroot",
        "pre_hook": HOOK_PRE_COMMAND,
        "post_hook": HOOK_POST_COMMAND,
    }
    if any(renewal.get(key) != value for key, value in expected_renewal.items()):
        raise QaHttpsError("QA renewal configuration does not use fixed webroot hooks")
    if "webroot_path" in renewal:
        webroot_paths = _parse_configobj_path_list(renewal["webroot_path"])
        if webroot_paths != [ACME_WEBROOT.as_posix()]:
            raise QaHttpsError("QA renewal webroot path conflicts with the fixed QA webroot")
    hook_keys = {
        key: renewal[key] for key in ("renew_hook", "deploy_hook") if key in renewal
    }
    if len(hook_keys) != 1 or next(iter(hook_keys.values()), None) != HOOK_DEPLOY_COMMAND:
        raise QaHttpsError("QA renewal deploy hook is missing, conflicting or unsupported")
    if webroot_map != {QA_HOSTNAME: ACME_WEBROOT.as_posix()}:
        raise QaHttpsError("QA renewal webroot map is not the fixed QA webroot")


def certificate_state(root: Path) -> str:
    live_root = rooted(root, CERT_LIVE)
    archive_root = rooted(root, CERT_ARCHIVE)
    fullchain = rooted(root, CERT_LIVE / "fullchain.pem")
    privkey = rooted(root, CERT_LIVE / "privkey.pem")
    renewal = rooted(root, CERT_RENEWAL)
    present = [path.exists() or path.is_symlink() for path in (
        live_root, archive_root, fullchain, privkey, renewal,
    )]
    if not any(present):
        return "missing"
    if not all(present):
        raise QaHttpsError("QA certificate lineage is partial")
    fullchain = _resolved_cert_file(root, CERT_LIVE / "fullchain.pem")
    _resolved_cert_file(root, CERT_LIVE / "privkey.pem")
    if not renewal.is_file() or renewal.is_symlink():
        raise QaHttpsError("QA renewal configuration is unsafe")
    if root == REAL_ROOT and stat.S_IMODE(renewal.stat().st_mode) & 0o022:
        raise QaHttpsError("QA renewal configuration is writable by non-root")
    if root == REAL_ROOT:
        host = run(
            ["openssl", "x509", "-in", str(fullchain), "-noout", "-checkhost", QA_HOSTNAME],
            step="openssl-host",
            check=False,
        )
        lifetime = run(
            ["openssl", "x509", "-in", str(fullchain), "-noout", "-checkend", "2592000"],
            step="openssl-lifetime",
            check=False,
        )
        if host.returncode != 0 or lifetime.returncode != 0:
            raise QaHttpsError("QA certificate name or remaining lifetime is invalid")
    _validate_renewal_config(renewal.read_text(encoding="utf-8"))
    return "valid"


def hook_controller_bytes() -> bytes:
    return Path(__file__).read_bytes()


def renewal_hook_state(root: Path) -> str:
    hook = rooted(root, HOOK_CONTROLLER)
    if not hook.exists() and not hook.is_symlink():
        return "missing"
    if hook.is_symlink() or not hook.is_file():
        raise QaHttpsError("QA renewal hook path is unsafe")
    wrong_mode = root == REAL_ROOT and stat.S_IMODE(hook.stat().st_mode) != 0o755
    if hook.read_bytes() != hook_controller_bytes() or wrong_mode:
        raise QaHttpsError("QA renewal hook differs from the fixed controller")
    return "valid"


def inspect(root: Path) -> str:
    _state, allow_cidr = load_disabled_installation(root)
    if not dns_matches():
        raise QaHttpsError("public QA DNS does not match the fixed server IPv4")
    certbot_ready(root)
    if nginx_conflict():
        raise QaHttpsError("active nginx server_name conflict detected")
    certificate = certificate_state(root)
    hook = renewal_hook_state(root)
    if (certificate == "valid") != (hook == "valid"):
        raise QaHttpsError("QA certificate and renewal hook state mismatch")
    if certificate == "valid":
        if _challenge_present(root):
            raise QaHttpsError("transient ACME challenge is still active")
        _validate_persistent_webroot(root)
    elif _challenge_present(root) or (
        rooted(root, ACME_WEBROOT).exists() or rooted(root, ACME_WEBROOT).is_symlink()
    ):
        raise QaHttpsError("fixed ACME path exists without an owned certificate")
    return (
        "SSE_QA_HTTPS_INSPECT_OK "
        f"dns_ipv4=match certificate={certificate} renewal_hook={hook} "
        f"nginx_conflict=none qa=disabled allow_cidr={allow_cidr}"
    )


def _reload_nginx(*, rollback: bool = False) -> None:
    timeout = ROLLBACK_COMMAND_TIMEOUT if rollback else COMMAND_TIMEOUT
    run(["nginx", "-t"], step="nginx-test", timeout=timeout)
    run(["systemctl", "reload", "nginx"], step="nginx-reload", timeout=timeout)


def _load_https_ownership(
    root: Path, *, require_hook: bool = True,
) -> tuple[dict[str, object], dict[str, object]]:
    path = rooted(root, OWNERSHIP_PATH)
    if not path.is_file() or path.is_symlink():
        raise QaHttpsError("QA ownership journal is missing")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QaHttpsError("QA ownership journal is invalid") from exc
    https = state.get("https_preparation") if isinstance(state, dict) else None
    if (
        not isinstance(https, dict)
        or https.get("webroot") != ACME_WEBROOT.as_posix()
        or re.fullmatch(
            r"[0-9a-f]{64}", str(https.get("webroot_marker_sha256", ""))
        ) is None
        or (
            require_hook
            and https.get("hook_sha256") != digest_bytes(hook_controller_bytes())
        )
    ):
        raise QaHttpsError("HTTPS ownership journal mismatch")
    return state, https


def _validate_webroot_entries(webroot: Path) -> None:
    for item in webroot.rglob("*"):
        relative = item.relative_to(webroot).as_posix()
        allowed_directory = relative in {".well-known", ".well-known/acme-challenge"}
        allowed_token = (
            item.is_file()
            and item.parent == webroot / ".well-known" / "acme-challenge"
            and _ACME_TOKEN.fullmatch(item.name) is not None
        )
        allowed_marker = item.is_file() and item == webroot / ACME_WEBROOT_MARKER.name
        if item.is_symlink() or not (allowed_directory or allowed_token or allowed_marker):
            raise QaHttpsError("refusing unsafe entry in owned ACME webroot")


def _validate_webroot_object(root: Path, marker_sha256: str) -> Path:
    webroot = rooted(root, ACME_WEBROOT)
    if webroot.is_symlink() or not webroot.is_dir():
        raise QaHttpsError("owned ACME webroot is missing or changed")
    marker = rooted(root, ACME_WEBROOT_MARKER)
    if (
        marker.is_symlink()
        or not marker.is_file()
        or digest_path(marker) != marker_sha256
    ):
        raise QaHttpsError("owned ACME webroot marker is missing or changed")
    _validate_webroot_entries(webroot)
    return webroot


def _validate_persistent_webroot(root: Path) -> Path:
    _state, https = _load_https_ownership(root)
    marker_sha256 = https["webroot_marker_sha256"]
    assert isinstance(marker_sha256, str)
    return _validate_webroot_object(root, marker_sha256)


def _clean_persistent_webroot(root: Path) -> None:
    webroot = _validate_persistent_webroot(root)
    challenge_root = webroot / ".well-known"
    if challenge_root.exists() or challenge_root.is_symlink():
        shutil.rmtree(challenge_root)


def _install_webroot(
    root: Path, state: dict[str, object], created: dict[str, object],
) -> None:
    webroot = rooted(root, ACME_WEBROOT)
    marker_payload = (secrets.token_hex(32) + "\n").encode("ascii")
    marker_sha256 = digest_bytes(marker_payload)
    with _defer_sigterm():
        if webroot.exists() or webroot.is_symlink():
            raise QaHttpsError(f"fixed ACME path already exists: {webroot}")
        webroot.mkdir(parents=True, mode=0o755)
        created["webroot"] = True
        created["webroot_marker_sha256"] = marker_sha256
        atomic_write(
            rooted(root, ACME_WEBROOT_MARKER), marker_payload, 0o600, replace=False,
        )
        https = state.setdefault("https_preparation", {})
        if not isinstance(https, dict):
            raise QaHttpsError("invalid HTTPS ownership state")
        https.update({
            "webroot": ACME_WEBROOT.as_posix(),
            "webroot_marker_sha256": marker_sha256,
        })
        save_ownership(root, state)


def _remove_new_webroot(
    root: Path, state: dict[str, object], marker_sha256: str,
) -> None:
    https = state.get("https_preparation")
    if (
        not isinstance(https, dict)
        or https.get("webroot") != ACME_WEBROOT.as_posix()
        or https.get("webroot_marker_sha256") != marker_sha256
    ):
        raise QaHttpsError("intermediate HTTPS ownership state mismatch")
    webroot = _validate_webroot_object(root, marker_sha256)
    shutil.rmtree(webroot)
    https.pop("webroot", None)
    https.pop("webroot_marker_sha256", None)
    if not https:
        state.pop("https_preparation", None)
    save_ownership(root, state)


def _create_challenge(root: Path, owned: ChallengeOwnership) -> None:
    config = rooted(root, ACME_CONFIG)
    site = rooted(root, ACME_SITE)
    webroot = rooted(root, ACME_WEBROOT)
    for path in (config, site):
        if path.exists() or path.is_symlink():
            raise QaHttpsError(f"fixed ACME path already exists: {path}")
    _validate_persistent_webroot(root)
    atomic_write(config, ACME_CONFIG_BYTES, 0o644, replace=False)
    owned.config = True
    site.parent.mkdir(parents=True, exist_ok=True)
    try:
        site.symlink_to(ACME_CONFIG.as_posix())
    except OSError:
        if root == REAL_ROOT or os.name != "nt":
            raise
        atomic_write(site, WINDOWS_TEST_SYMLINK_BYTES, 0o600, replace=False)
    owned.site = True
    _reload_nginx()


def _remove_challenge(
    root: Path, owned: ChallengeOwnership, *, reload_nginx: bool,
    rollback: bool = False,
) -> None:
    config = rooted(root, ACME_CONFIG)
    site = rooted(root, ACME_SITE)
    webroot = rooted(root, ACME_WEBROOT)
    # Validate the complete owned set before deleting any part of it.  A
    # changed or foreign object is preserved and makes cleanup fail closed.
    if owned.site:
        if site.is_symlink():
            if os.readlink(site) != ACME_CONFIG.as_posix():
                raise QaHttpsError("refusing changed ACME nginx symlink")
        elif root != REAL_ROOT and os.name == "nt" and site.is_file():
            if site.read_bytes() != WINDOWS_TEST_SYMLINK_BYTES:
                raise QaHttpsError("refusing changed ACME nginx site")
        elif site.exists() or site.is_symlink():
            raise QaHttpsError("owned ACME nginx site is missing or changed")
    if owned.config:
        if config.exists() or config.is_symlink():
            if config.is_symlink() or not config.is_file() or config.read_bytes() != ACME_CONFIG_BYTES:
                raise QaHttpsError("refusing changed ACME nginx config")
    if owned.webroot and (webroot.exists() or webroot.is_symlink()):
        if webroot.is_symlink() or not webroot.is_dir():
            raise QaHttpsError("refusing changed ACME webroot")
        _validate_webroot_entries(webroot)
    if owned.site and (site.exists() or site.is_symlink()):
        site.unlink()
    if owned.config and (config.exists() or config.is_symlink()):
        config.unlink()
    if owned.webroot and (webroot.exists() or webroot.is_symlink()):
        shutil.rmtree(webroot)
    if reload_nginx:
        _reload_nginx(rollback=rollback)


def _challenge_residues(root: Path, owned: ChallengeOwnership) -> list[str]:
    result: list[str] = []
    for name, logical, created in (
        ("acme_config", ACME_CONFIG, owned.config),
        ("acme_site", ACME_SITE, owned.site),
        ("acme_webroot", ACME_WEBROOT, owned.webroot),
    ):
        path = rooted(root, logical)
        if created and (path.exists() or path.is_symlink()):
            result.append(name)
    return result


def _save_challenge_marker(root: Path) -> None:
    path = rooted(root, ACME_OWNERSHIP)
    payload = {
        "schema": 1,
        "hostname": QA_HOSTNAME,
        "config_sha256": digest_bytes(ACME_CONFIG_BYTES),
        "site_target": ACME_CONFIG.as_posix(),
        "webroot": ACME_WEBROOT.as_posix(),
        "remove_webroot": False,
    }
    atomic_write(
        path, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
        0o600, replace=False,
    )


def _load_challenge_marker(root: Path) -> ChallengeOwnership:
    path = rooted(root, ACME_OWNERSHIP)
    if not path.is_file() or path.is_symlink():
        raise QaHttpsError("renewal ACME ownership marker is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QaHttpsError("renewal ACME ownership marker is invalid") from exc
    expected = {
        "schema": 1,
        "hostname": QA_HOSTNAME,
        "config_sha256": digest_bytes(ACME_CONFIG_BYTES),
        "site_target": ACME_CONFIG.as_posix(),
        "webroot": ACME_WEBROOT.as_posix(),
        "remove_webroot": False,
    }
    if payload != expected:
        raise QaHttpsError("renewal ACME ownership marker mismatch")
    return ChallengeOwnership(webroot=False, config=True, site=True)


def _failure_text(exc: BaseException) -> str:
    if isinstance(exc, FixedCommandError):
        return str(exc)
    if isinstance(exc, QaHttpsCancelled):
        return "step=cancel signal=SIGTERM"
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return f"step=controller type={type(exc).__name__} diagnostic={(text[:240] or 'none')}"


def _install_cancel_handler(cancel_state: dict[str, bool]):
    def cancel(_signum, _frame):
        if cancel_state["cancel_requested"] or cancel_state["rollback_started"]:
            return
        cancel_state["cancel_requested"] = True
        raise QaHttpsCancelled("HTTPS operation cancelled")

    return cancel


def _begin_rollback(cancel_state: dict[str, bool] | None) -> None:
    if cancel_state is None:
        return
    cancel_state["rollback_started"] = True
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def renew_pre(root: Path, cancel_state: dict[str, bool] | None = None) -> str:
    marker = rooted(root, ACME_OWNERSHIP)
    if marker.exists() or marker.is_symlink():
        # A previously interrupted Certbot run may have missed its post-hook.
        # Recover only a byte-for-byte fixed, explicitly owned challenge set.
        renew_post(root, cancel_state)
    _clean_persistent_webroot(root)
    owned = ChallengeOwnership()
    try:
        _create_challenge(root, owned)
        _save_challenge_marker(root)
    except BaseException as exc:
        _begin_rollback(cancel_state)
        rollback_errors: list[str] = []
        if any((owned.webroot, owned.config, owned.site)):
            try:
                _remove_challenge(root, owned, reload_nginx=True, rollback=True)
            except Exception as cleanup_exc:
                rollback_errors.append(_failure_text(cleanup_exc))
        residues = _challenge_residues(root, owned)
        if rollback_errors or residues:
            raise QaHttpsError(
                f"primary=({_failure_text(exc)}), rollback=incomplete "
                f"errors={','.join(rollback_errors) or 'none'} "
                f"residues={','.join(residues) or 'none'}"
            ) from exc
        raise
    return "SSE_QA_HTTPS_RENEW_PRE_OK challenge=ready application=closed"


def renew_post(root: Path, cancel_state: dict[str, bool] | None = None) -> str:
    owned = _load_challenge_marker(root)
    marker = rooted(root, ACME_OWNERSHIP)
    try:
        _remove_challenge(root, owned, reload_nginx=True, rollback=True)
        _clean_persistent_webroot(root)
        marker.unlink()
    except BaseException as exc:
        _begin_rollback(cancel_state)
        residues = _challenge_residues(root, owned)
        if marker.exists() or marker.is_symlink():
            residues.append("ownership_marker")
        raise QaHttpsError(
            f"primary=({_failure_text(exc)}), rollback=incomplete "
            f"residues={','.join(residues) or 'nginx_runtime_unknown'}"
        ) from exc
    return "SSE_QA_HTTPS_RENEW_POST_OK challenge=removed"


def renew_deploy(root: Path) -> str:
    if os.getenv("RENEWED_LINEAGE") != CERT_LIVE.as_posix():
        raise QaHttpsError("renewed lineage does not match fixed QA certificate")
    if certificate_state(root) != "valid":
        raise QaHttpsError("renewed QA certificate did not validate")
    _validate_persistent_webroot(root)
    _reload_nginx()
    return "SSE_QA_HTTPS_RENEW_DEPLOY_OK certificate=valid nginx=reloaded"


def _issue_certificate(root: Path) -> None:
    command = [
        str(rooted(root, CERTBOT)),
        "certonly",
        "--webroot",
        "--webroot-path",
        str(rooted(root, ACME_WEBROOT)),
        "--domain",
        QA_HOSTNAME,
        "--cert-name",
        QA_HOSTNAME,
        "--non-interactive",
        "--agree-tos",
        "--keep-until-expiring",
        "--pre-hook",
        HOOK_PRE_COMMAND,
        "--post-hook",
        HOOK_POST_COMMAND,
        "--deploy-hook",
        HOOK_DEPLOY_COMMAND,
    ]
    run(command, step="certbot-issue", timeout=CERTBOT_TIMEOUT)


def _delete_new_lineage(root: Path) -> None:
    run(
        [str(rooted(root, CERTBOT)), "delete", "--cert-name", QA_HOSTNAME, "--non-interactive"],
        step="certbot-delete",
        timeout=ROLLBACK_CERTBOT_TIMEOUT,
    )


def _lineage_present(root: Path) -> bool:
    paths = (CERT_LIVE, CERT_ARCHIVE, CERT_RENEWAL)
    return any(
        rooted(root, path).exists() or rooted(root, path).is_symlink()
        for path in paths
    )


def _challenge_present(root: Path) -> bool:
    paths = (ACME_CONFIG, ACME_SITE, ACME_OWNERSHIP)
    return any(
        rooted(root, path).exists() or rooted(root, path).is_symlink()
        for path in paths
    )


def _install_hook(
    root: Path, state: dict[str, object], created: dict[str, object],
) -> bool:
    if renewal_hook_state(root) == "valid":
        return False
    hook = rooted(root, HOOK_CONTROLLER)
    hook.parent.mkdir(parents=True, exist_ok=True)
    payload = hook_controller_bytes()
    with _defer_sigterm():
        atomic_write(hook, payload, 0o755, replace=False)
        created["hook"] = True
        https = state.setdefault("https_preparation", {})
        if not isinstance(https, dict):
            raise QaHttpsError("invalid HTTPS ownership state")
        https["hook_sha256"] = digest_bytes(payload)
        save_ownership(root, state)
    return True


def _remove_new_hook(root: Path, state: dict[str, object]) -> None:
    hook = rooted(root, HOOK_CONTROLLER)
    if hook.is_file() and not hook.is_symlink() and hook.read_bytes() == hook_controller_bytes():
        hook.unlink()
    else:
        raise QaHttpsError("new QA renewal hook cannot be rolled back safely")
    https = state.get("https_preparation")
    if isinstance(https, dict):
        https.pop("hook_sha256", None)
        if not https:
            state.pop("https_preparation", None)
    save_ownership(root, state)


def _render_allow_cidr(before: bytes, allow_cidr: str) -> bytes:
    text = before.decode("utf-8")
    after, count = re.subn(
        r"(?m)^(\s*allow\s+)[0-9.]+/32(;\s*)$",
        rf"\g<1>{allow_cidr}\g<2>",
        text,
    )
    if count != 1:
        raise QaHttpsError("installed QA nginx allow directive mismatch")
    return after.encode("utf-8")


def _update_allow_cidr(
    root: Path, state: dict[str, object], before: bytes, payload: bytes,
) -> None:
    files = state["files"]
    assert isinstance(files, dict)
    logical = QA_NGINX_CONFIG.as_posix()
    if files.get(logical) != digest_bytes(before):
        raise QaHttpsError("owned QA nginx config changed before update")
    files[logical] = digest_bytes(payload)
    save_ownership(root, state)
    atomic_write(rooted(root, QA_NGINX_CONFIG), payload, 0o640, replace=True)


def _restore_allow_cidr(
    root: Path, state: dict[str, object], current: bytes, original: bytes,
) -> None:
    path = rooted(root, QA_NGINX_CONFIG)
    if not path.is_file() or path.is_symlink():
        raise QaHttpsError("updated QA nginx config cannot be rolled back safely")
    actual = path.read_bytes()
    if actual not in {current, original}:
        raise QaHttpsError("updated QA nginx config cannot be rolled back safely")
    files = state["files"]
    assert isinstance(files, dict)
    files[QA_NGINX_CONFIG.as_posix()] = digest_bytes(original)
    save_ownership(root, state)
    if actual != original:
        atomic_write(path, original, 0o640, replace=True)


def prepare(
    root: Path, allow_cidr: str, cancel_state: dict[str, bool] | None = None,
) -> str:
    allow_cidr = validate_allow_cidr(allow_cidr)
    if root == REAL_ROOT:
        assert_prepare_scope()
    state, _old_allow = load_disabled_installation(root)
    if not dns_matches():
        raise QaHttpsError("public QA DNS does not match the fixed server IPv4")
    certbot_ready(root)
    if nginx_conflict():
        raise QaHttpsError("active nginx server_name conflict detected")
    certificate_before = certificate_state(root)
    hook_before = renewal_hook_state(root)
    nginx_before = rooted(root, QA_NGINX_CONFIG).read_bytes()
    if certificate_before == "missing" and hook_before != "missing":
        raise QaHttpsError("QA certificate and renewal hook state mismatch")
    if certificate_before == "valid" and hook_before != "valid":
        raise QaHttpsError("QA certificate and renewal hook state mismatch")
    if _challenge_present(root) or (
        rooted(root, ACME_WEBROOT).exists() or rooted(root, ACME_WEBROOT).is_symlink()
    ):
        raise QaHttpsError("fixed ACME path or ownership marker already exists")
    if "https_preparation" in state:
        raise QaHttpsError("HTTPS ownership journal already exists")

    original_state = json.loads(json.dumps(state))
    certificate_attempted = False
    certificate_created = False
    created: dict[str, object] = {
        "hook": False,
        "webroot": False,
        "webroot_marker_sha256": None,
    }
    nginx_after: bytes | None = None
    rollback_deadline = 0.0

    def check_rollback_budget() -> None:
        if rollback_deadline and time.monotonic() >= rollback_deadline:
            raise QaHttpsError("step=rollback-budget timeout diagnostic=budget_exhausted")

    try:
        if certificate_before == "missing":
            _install_webroot(root, state, created)
            _install_hook(root, state, created)
            certificate_attempted = True
            _issue_certificate(root)
            if _challenge_present(root):
                raise QaHttpsError("Certbot left fixed ACME resources after issuance")
            if certificate_state(root) != "valid":
                raise QaHttpsError("QA certificate was not validated after issuance")
            certificate_created = True
        if certificate_state(root) != "valid":
            raise QaHttpsError("QA certificate was not validated after preparation")
        if renewal_hook_state(root) != "valid":
            raise QaHttpsError("QA renewal hook was not validated")
        nginx_after = _render_allow_cidr(nginx_before, allow_cidr)
        _update_allow_cidr(root, state, nginx_before, nginx_after)
        https = state.setdefault("https_preparation", {})
        if not isinstance(https, dict):
            raise QaHttpsError("invalid HTTPS ownership state")
        https.update({
            "schema": 1,
            "hostname": QA_HOSTNAME,
            "allow_cidr": allow_cidr,
            "certificate_created": certificate_created,
            "renewal": "pre_post_deploy_hooks",
        })
        save_ownership(root, state)
        _state, installed_allow = load_disabled_installation(root)
        if installed_allow != allow_cidr:
            raise QaHttpsError("QA allow_cidr was not committed")
    except BaseException as exc:
        primary = _failure_text(exc)
        _begin_rollback(cancel_state)
        rollback_deadline = time.monotonic() + ROLLBACK_BUDGET_SECONDS
        rollback_errors: list[str] = []
        residues: list[str] = []
        marker = rooted(root, ACME_OWNERSHIP)
        if marker.exists() or marker.is_symlink():
            try:
                check_rollback_budget()
                renew_post(root, cancel_state)
            except Exception as rollback_exc:
                rollback_errors.append(_failure_text(rollback_exc))
        elif any(
            rooted(root, path).exists() or rooted(root, path).is_symlink()
            for path in (ACME_CONFIG, ACME_SITE)
        ):
            residues.append("unowned_acme_resources")
        if nginx_after is not None:
            try:
                check_rollback_budget()
                _restore_allow_cidr(root, state, nginx_after, nginx_before)
            except Exception as rollback_exc:
                rollback_errors.append(_failure_text(rollback_exc))
        infrastructure_cleanup_allowed = True
        if certificate_created:
            try:
                check_rollback_budget()
                _delete_new_lineage(root)
            except Exception as rollback_exc:
                rollback_errors.append(_failure_text(rollback_exc))
                infrastructure_cleanup_allowed = False
            if _lineage_present(root):
                residues.append("certificate_lineage")
                infrastructure_cleanup_allowed = False
        elif certificate_attempted and _lineage_present(root):
            residues.append("certificate_lineage_unproven")
            infrastructure_cleanup_allowed = False

        webroot_removed = not created["webroot"]
        if created["webroot"] and infrastructure_cleanup_allowed:
            try:
                check_rollback_budget()
                marker_sha256 = created["webroot_marker_sha256"]
                if not isinstance(marker_sha256, str):
                    raise QaHttpsError("owned ACME webroot marker was not published")
                _remove_new_webroot(root, state, marker_sha256)
                webroot_removed = True
            except Exception as rollback_exc:
                rollback_errors.append(_failure_text(rollback_exc))
        elif created["webroot"]:
            rollback_errors.append(
                "step=webroot-remove blocked diagnostic=certificate_residue"
            )
        if created["hook"] and webroot_removed and infrastructure_cleanup_allowed:
            try:
                check_rollback_budget()
                _remove_new_hook(root, state)
            except Exception as rollback_exc:
                rollback_errors.append(_failure_text(rollback_exc))
        elif created["hook"]:
            rollback_errors.append(
                "step=hook-remove blocked diagnostic=owned_webroot_residue"
            )
        if _challenge_present(root):
            residues.append("acme_resources")
        if created["hook"] and (
            rooted(root, HOOK_CONTROLLER).exists()
            or rooted(root, HOOK_CONTROLLER).is_symlink()
        ):
            residues.append("renewal_hook")
        if created["webroot"] and (
            rooted(root, ACME_WEBROOT).exists()
            or rooted(root, ACME_WEBROOT).is_symlink()
        ):
            residues.append("acme_webroot")
        if not rollback_errors and not residues:
            try:
                save_ownership(root, original_state)
            except Exception as rollback_exc:
                rollback_errors.append(_failure_text(rollback_exc))
        if rollback_errors or residues:
            raise QaHttpsError(
                f"primary=({primary}), rollback=incomplete "
                f"errors={','.join(rollback_errors) or 'none'} "
                f"residues={','.join(dict.fromkeys(residues)) or 'none'}"
            ) from exc
        raise
    return (
        "SSE_QA_HTTPS_PREPARE_OK "
        f"hostname={QA_HOSTNAME} certificate=valid renewal=pre_post_deploy_hooks "
        f"allow_cidr={allow_cidr} qa_enabled=false"
    )


def read_allow_cidr_stdin() -> str:
    raw = sys.stdin.buffer.read(65)
    if not raw or len(raw) > 64:
        raise QaHttpsError("allow_cidr stdin is empty or too large")
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise QaHttpsError("allow_cidr stdin must be ASCII") from exc
    return validate_allow_cidr(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=("inspect", "prepare", "renew-pre", "renew-post", "renew-deploy"),
    )
    parser.add_argument("--allow-cidr-stdin", action="store_true")
    parser.add_argument("--test-root", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = REAL_ROOT
    if args.test_root is not None:
        if os.getenv("SSE_QA_LOCAL_TEST") != "1":
            raise QaHttpsError("test root is disabled")
        root = args.test_root.resolve()
    if args.operation == "inspect":
        if args.allow_cidr_stdin:
            raise QaHttpsError("inspect accepts no input")
        print(inspect(root))
        return 0
    if args.operation == "renew-deploy":
        if args.allow_cidr_stdin:
            raise QaHttpsError("renew-deploy accepts no input")
        print(renew_deploy(root))
        return 0
    cancel_state = {"cancel_requested": False, "rollback_started": False}
    signal.signal(signal.SIGTERM, _install_cancel_handler(cancel_state))
    if args.operation == "renew-pre":
        if args.allow_cidr_stdin:
            raise QaHttpsError("renew-pre accepts no input")
        print(renew_pre(root, cancel_state))
        return 0
    if args.operation == "renew-post":
        if args.allow_cidr_stdin:
            raise QaHttpsError("renew-post accepts no input")
        print(renew_post(root, cancel_state))
        return 0
    if not args.allow_cidr_stdin:
        raise QaHttpsError("prepare requires allow_cidr on stdin")
    print(prepare(root, read_allow_cidr_stdin(), cancel_state))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QaHttpsError as exc:
        print(f"SSE_QA_HTTPS_FAIL {exc}", file=sys.stderr)
        raise SystemExit(2)
