#!/usr/bin/env python
"""Read-only traffic measurement for the ready-core role PWA applications.

The script targets only a local Django server on port 8000 and uses the
isolated weekly QA accounts created by ``full_week_load_qa.py``. It never
prints or stores access codes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from core.pwa_performance_qa import (
    PWA_PERFORMANCE_QA_SCHEMA,
    PWA_PERFORMANCE_QA_SCHEMA_VERSION,
    validate_pwa_performance_qa_run_id,
    verify_pwa_performance_qa_database,
)
from shifts.models import EmployeeShift
from users.models import EmployeeAccess


ALLOWED_PORT = 8000
ARTIFACT_ROOT_NAME = 'copper-pwa-performance-qa-20260823'
CSRF_RE = re.compile(
    r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoleTarget:
    ordinal: int
    role: str
    host: str
    start_url: str
    manifest_url: str
    service_worker_url: str
    realtime: bool
    simultaneous_sessions: int
    poll_interval_seconds: int | None

    @property
    def phone(self) -> str:
        return f"+7999200{self.ordinal:04d}"

    @property
    def pin(self) -> str:
        return f"{630000 + self.ordinal:06d}"


READY_ROLES = (
    RoleTarget(
        0,
        "admin",
        "admin.localhost",
        "/system-admin/",
        "/system-admin.webmanifest",
        "/system-admin-sw.js",
        True,
        1,
        15,
    ),
    RoleTarget(
        1,
        "oup",
        "oup.localhost",
        "/oup/employees/",
        "/oup.webmanifest",
        "/oup-sw.js",
        False,
        1,
        None,
    ),
    RoleTarget(
        2,
        "deputy_mining_manager",
        "deputy.localhost",
        "/deputy-mining-manager/",
        "/deputy-mining-manager.webmanifest",
        "/deputy-mining-manager-sw.js",
        False,
        1,
        None,
    ),
    RoleTarget(
        4,
        "dispatcher",
        "dispatcher.localhost",
        "/dispatcher/control/",
        "/dispatcher.webmanifest",
        "/dispatcher-sw.js",
        True,
        1,
        5,
    ),
    RoleTarget(
        5,
        "mining_master",
        "mining-master.localhost",
        "/mining-master/assignments/",
        "/mining-master-manifest.webmanifest",
        "/mining-master-sw.js",
        True,
        1,
        5,
    ),
    RoleTarget(
        12,
        "excavator_operator",
        "excavator.localhost",
        "/excavator/work/",
        "/excavator.webmanifest",
        "/excavator-sw.js",
        True,
        8,
        5,
    ),
    RoleTarget(
        44,
        "driver",
        "driver.localhost",
        "/driver/",
        "/driver.webmanifest",
        "/driver-sw.js",
        True,
        53,
        5,
    ),
    RoleTarget(
        3,
        "manager",
        "management.localhost",
        "/reports/management/?date=2026-07-26",
        "/management.webmanifest",
        "/management-sw.js",
        True,
        1,
        15,
    ),
)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        candidate = ""
        if tag in {"script", "img", "source"}:
            candidate = values.get("src") or ""
        elif tag == "link":
            candidate = values.get("href") or ""
        if candidate:
            self.urls.append(candidate)


@dataclass
class MeasuredResponse:
    category: str
    role: str
    path: str
    status: int
    elapsed_ms: float
    body_bytes: int
    gzip_bytes: int
    response_header_bytes: int
    content_type: str
    content_encoding: str
    cache_control: str
    etag: str
    last_modified: str
    sha256: str
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=ALLOWED_PORT)
    parser.add_argument("--timeout-seconds", type=float, default=12.0)
    parser.add_argument("--realtime-polls", type=int, default=12)
    parser.add_argument(
        '--role',
        required=True,
        choices=tuple(role.role for role in READY_ROLES),
    )
    parser.add_argument('--run-id', required=True)
    return parser.parse_args()


def _has_reparse_boundary(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.lstat(), 'st_file_attributes', 0))
    except FileNotFoundError:
        return False
    return bool(attributes & 0x400)


def artifact_directory_for(run_id: str, role: str) -> Path:
    normalized_run_id = validate_pwa_performance_qa_run_id(run_id)
    if role not in {item.role for item in READY_ROLES}:
        raise RuntimeError(f'Unknown ready role: {role!r}.')
    temp_root = Path(tempfile.gettempdir()).resolve()
    audit_root = temp_root / ARTIFACT_ROOT_NAME
    artifact_dir = audit_root / normalized_run_id / role
    current = temp_root
    for part in artifact_dir.relative_to(temp_root).parts:
        current = current / part
        if current.exists() and _has_reparse_boundary(current):
            raise RuntimeError(
                f'Artifact path contains a reparse boundary: {current}'
            )
    if artifact_dir.exists() and any(artifact_dir.iterdir()):
        raise RuntimeError(f'Artifact directory is not empty: {artifact_dir}')
    artifact_dir.mkdir(parents=True, exist_ok=True)
    validate_artifact_output_path(artifact_dir / 'probe.json')
    return artifact_dir


def validate_artifact_output_path(path: Path) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    audit_root = temp_root / ARTIFACT_ROOT_NAME
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(audit_root)
    except ValueError as error:
        raise RuntimeError('Artifact output is outside the allowlisted root.') from error
    if not relative.parts or absolute.name in {'', '.', '..'}:
        raise RuntimeError('Artifact output path is invalid.')
    current = temp_root
    for part in (Path(ARTIFACT_ROOT_NAME) / relative.parent).parts:
        current = current / part
        if not current.exists() or not current.is_dir():
            raise RuntimeError(
                f'Artifact parent is absent or not a directory: {current}'
            )
        if _has_reparse_boundary(current):
            raise RuntimeError(
                f'Artifact path contains a reparse boundary: {current}'
            )


def ensure_safe_args(args: argparse.Namespace) -> Path:
    validate_safe_args(args)
    return artifact_directory_for(args.run_id, args.role)


def validate_safe_args(args: argparse.Namespace) -> None:
    if args.port != ALLOWED_PORT:
        raise RuntimeError(
            f"Only local port {ALLOWED_PORT} is allowed, got {args.port}."
        )
    if not 2 <= args.timeout_seconds <= 30:
        raise RuntimeError("Timeout must be between 2 and 30 seconds.")
    if not 3 <= args.realtime_polls <= 60:
        raise RuntimeError("Realtime poll count must be between 3 and 60.")
    validate_pwa_performance_qa_run_id(args.run_id)
    if args.role not in {role.role for role in READY_ROLES}:
        raise RuntimeError('Unknown ready role.')


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def host_header(role: RoleTarget, port: int) -> str:
    return f"{role.host}:{port}"


def response_header_size(status: int, headers: Any) -> int:
    total = len(f"HTTP/1.1 {status}\r\n".encode("ascii"))
    for key, value in headers.items():
        total += len(f"{key}: {value}\r\n".encode("utf-8"))
    return total + 2


def gzip_size(body: bytes) -> int:
    if not body:
        return 0
    return len(gzip.compress(body, compresslevel=6, mtime=0))


def measure_request(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    *,
    role: RoleTarget,
    category: str,
    path: str,
    timeout: float,
) -> tuple[MeasuredResponse, bytes, Any]:
    started = time.perf_counter()
    status = 0
    body = b""
    headers: Any = {}
    error = ""
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            headers = response.headers
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        headers = exc.headers
        body = exc.read()
        if status not in {301, 302, 303, 304, 307, 308}:
            error = f"HTTPError:{status}"
    except Exception as exc:  # noqa: BLE001 - audit must record all failures.
        error = f"{type(exc).__name__}:{exc}"
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    result = MeasuredResponse(
        category=category,
        role=role.role,
        path=path,
        status=status,
        elapsed_ms=elapsed_ms,
        body_bytes=len(body),
        gzip_bytes=gzip_size(body),
        response_header_bytes=response_header_size(status, headers),
        content_type=str(headers.get("Content-Type", "")),
        content_encoding=str(headers.get("Content-Encoding", "")),
        cache_control=str(headers.get("Cache-Control", "")),
        etag=str(headers.get("ETag", "")),
        last_modified=str(headers.get("Last-Modified", "")),
        sha256=hashlib.sha256(body).hexdigest() if body else "",
        error=error,
    )
    return result, body, headers


def new_session() -> tuple[
    urllib.request.OpenerDirector,
    http.cookiejar.CookieJar,
]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(cookie_jar),
        NoRedirectHandler(),
    )
    return opener, cookie_jar


def verify_server_preflight(
    *,
    port: int,
    timeout: float,
    run_id: str,
    expected_fingerprint: str,
) -> dict[str, Any]:
    opener, _ = new_session()
    request = urllib.request.Request(
        f'{base_url(port)}/qa/pwa-traffic/preflight/',
        headers={
            'Host': f'dispatcher.localhost:{port}',
            'Accept': 'application/json',
            'User-Agent': 'Copper-PWA-Traffic-Audit/2.0',
            'X-Copper-QA-Run-ID': run_id,
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            headers = response.headers
            body = response.read()
    except Exception as error:
        raise RuntimeError(
            'Actual Django server did not pass the QA DB preflight.'
        ) from error
    try:
        payload = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError('Server preflight returned invalid JSON.') from error
    expected_keys = {
        'status',
        'schema',
        'schema_version',
        'fingerprint',
    }
    if (
        status != 200
        or set(payload) != expected_keys
        or payload.get('status') != 'ok'
        or payload.get('schema') != PWA_PERFORMANCE_QA_SCHEMA
        or payload.get('schema_version')
        != PWA_PERFORMANCE_QA_SCHEMA_VERSION
        or payload.get('fingerprint') != expected_fingerprint
        or 'no-store' not in str(headers.get('Cache-Control', '')).lower()
    ):
        raise RuntimeError(
            'Configured process and actual Django server DB fingerprints differ.'
        )
    return payload


def canonical_json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + '\n'
    ).encode('utf-8')


def write_canonical_new_json(path: Path, payload: Any) -> str:
    encoded = canonical_json_bytes(payload)
    validate_artifact_output_path(path)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        validate_artifact_output_path(path)
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise RuntimeError(
                f'Artifact overwrite is forbidden: {path}'
            ) from error
        except OSError as error:
            raise RuntimeError(
                'Atomic no-overwrite artifact publication failed.'
            ) from error
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(encoded).hexdigest().upper()


def ensure_artifacts_contain_no_credentials(
    artifact_dir: Path,
    role: RoleTarget,
    credentials: tuple[str, str],
) -> None:
    phone, pin = credentials
    forbidden = (
        role.phone.encode('utf-8'),
        role.pin.encode('ascii'),
        phone.encode('utf-8'),
        pin.encode('ascii'),
        b'POSTGRES_PASSWORD',
        b'sessionid=',
        b'csrftoken=',
    )
    for path in artifact_dir.rglob('*'):
        if not path.is_file():
            continue
        content = path.read_bytes()
        if any(token and token in content for token in forbidden):
            raise RuntimeError(
                f'Credential-like value found in artifact: {path.name}'
            )


def login(
    role: RoleTarget,
    *,
    port: int,
    timeout: float,
    credentials: tuple[str, str] | None = None,
) -> tuple[
    urllib.request.OpenerDirector,
    http.cookiejar.CookieJar,
    list[MeasuredResponse],
]:
    opener, cookie_jar = new_session()
    root = f"{base_url(port)}/"
    common = {
        "Host": host_header(role, port),
        "User-Agent": "Copper-PWA-Traffic-Audit/1.0",
    }
    get_result, body, _ = measure_request(
        opener,
        urllib.request.Request(root, headers=common),
        role=role,
        category="login_get",
        path="/",
        timeout=timeout,
    )
    token = CSRF_RE.search(body.decode("utf-8", errors="replace"))
    if get_result.status != 200 or not token:
        raise RuntimeError(
            f"{role.role}: login form unavailable ({get_result.status})."
        )
    phone, pin = credentials or (role.phone, role.pin)
    form = urllib.parse.urlencode(
        {
            "csrfmiddlewaretoken": token.group(1),
            "phone": phone,
            "access_code": pin,
            "device_kind": (
                "shared"
                if role.role in {"dispatcher", "mining_master"}
                else "personal"
            ),
        }
    ).encode("ascii")
    post_result, _, _ = measure_request(
        opener,
        urllib.request.Request(
            root,
            data=form,
            headers={
                **common,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"http://{host_header(role, port)}/",
            },
        ),
        role=role,
        category="login_post",
        path="/",
        timeout=timeout,
    )
    if post_result.status not in {301, 302, 303, 307, 308}:
        raise RuntimeError(
            f"{role.role}: login rejected ({post_result.status})."
        )
    return opener, cookie_jar, [get_result, post_result]


def _load_dispatcher_scenario_binding(
    run_id: str,
    expected_database_fingerprint: str,
) -> dict[str, Any]:
    scenario_path = (
        Path(tempfile.gettempdir()).resolve()
        / ARTIFACT_ROOT_NAME
        / validate_pwa_performance_qa_run_id(run_id)
        / 'scenario'
        / 'scenario_manifest.json'
    )
    validate_artifact_output_path(scenario_path)
    if (
        not scenario_path.is_file()
        or _has_reparse_boundary(scenario_path)
        or scenario_path.stat().st_size > 256 * 1024
    ):
        raise RuntimeError('Dispatcher scenario manifest is unavailable or unsafe.')
    try:
        manifest = json.loads(scenario_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError('Dispatcher scenario manifest is invalid.') from error
    required = {
        'schema': 'copper-dispatcher-performance-qa-scenario',
        'schema_version': 1,
        'synthetic': True,
        'official': False,
        'run_id': run_id,
        'database_fingerprint': expected_database_fingerprint,
    }
    if any(manifest.get(key) != value for key, value in required.items()):
        raise RuntimeError('Dispatcher scenario manifest does not match this run.')
    for key in (
        'marker',
        'shift_type',
        'dispatcher_shift_id',
        'dispatcher_employee_id',
        'dispatcher_access_id',
    ):
        if key not in manifest:
            raise RuntimeError('Dispatcher scenario binding is incomplete.')
    return manifest


def selected_role_credentials(
    role: RoleTarget,
    *,
    run_id: str,
    expected_database_fingerprint: str,
) -> tuple[str, str]:
    if role.role != 'dispatcher':
        return role.phone, role.pin
    manifest = _load_dispatcher_scenario_binding(
        run_id,
        expected_database_fingerprint,
    )
    try:
        open_shift = (
            EmployeeShift.objects.filter(
                pk=manifest['dispatcher_shift_id'],
                employee_id=manifest['dispatcher_employee_id'],
                workplace_code='dispatcher',
                closed_at__isnull=True,
                employee__full_name__startswith=manifest['marker'],
                shift_type=manifest['shift_type'],
            )
            .select_related('employee')
            .get()
        )
    except (EmployeeShift.DoesNotExist, EmployeeShift.MultipleObjectsReturned) as error:
        raise RuntimeError(
            'Dispatcher scenario shift binding is stale or ambiguous.'
        ) from error
    open_shift_count = EmployeeShift.objects.filter(
        workplace_code='dispatcher',
        closed_at__isnull=True,
        employee__full_name__startswith=manifest['marker'],
    ).count()
    if open_shift_count != 1:
        raise RuntimeError(
            'Expected exactly one active synthetic Dispatcher shift.'
        )
    try:
        access = EmployeeAccess.objects.get(
            pk=manifest['dispatcher_access_id'],
            employee=open_shift.employee,
            role__code='dispatcher',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
    except (EmployeeAccess.DoesNotExist, EmployeeAccess.MultipleObjectsReturned) as error:
        raise RuntimeError(
            'Dispatcher scenario access binding is stale or ambiguous.'
        ) from error
    return access.employee.phone, access.access_code


def require_response_status(
    response: MeasuredResponse,
    expected: set[int],
) -> None:
    if response.error or response.status not in expected:
        raise RuntimeError(
            f'{response.role}:{response.category}:{response.path} '
            f'returned {response.status} ({response.error or "unexpected"}).'
        )


def normalize_asset_urls(html: bytes, role: RoleTarget) -> list[str]:
    parser = AssetParser()
    parser.feed(html.decode("utf-8", errors="replace"))
    raw_urls = [
        *parser.urls,
        role.manifest_url,
        role.service_worker_url,
    ]
    result: list[str] = []
    for raw in raw_urls:
        if not raw or raw.startswith(("data:", "blob:", "javascript:")):
            continue
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme or parsed.netloc:
            continue
        path = urllib.parse.urlunparse(("", "", parsed.path, "", parsed.query, ""))
        if not path.startswith("/"):
            path = "/" + path.lstrip("/")
        if path not in result:
            result.append(path)
    return result


def build_get(
    role: RoleTarget,
    *,
    port: int,
    path: str,
    accept: str,
    conditional_headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    return urllib.request.Request(
        f"{base_url(port)}{path}",
        headers={
            "Host": host_header(role, port),
            "Accept": accept,
            "User-Agent": "Copper-PWA-Traffic-Audit/1.0",
            **(conditional_headers or {}),
        },
    )


def audit_role(
    role: RoleTarget,
    *,
    port: int,
    timeout: float,
    realtime_polls: int,
    credentials: tuple[str, str] | None = None,
) -> dict[str, Any]:
    opener, cookie_jar, rows = login(
        role,
        port=port,
        timeout=timeout,
        credentials=credentials,
    )
    cold_page, cold_html, _ = measure_request(
        opener,
        build_get(
            role,
            port=port,
            path=role.start_url,
            accept="text/html,application/xhtml+xml",
        ),
        role=role,
        category="cold_page",
        path=role.start_url,
        timeout=timeout,
    )
    rows.append(cold_page)
    require_response_status(cold_page, {200})
    if (
        role.role == 'dispatcher'
        and b'data-dispatcher-own-shift-open="true"' not in cold_html
    ):
        raise RuntimeError(
            'Dispatcher HTTP session is not bound to its selected active shift.'
        )

    cold_assets: dict[str, tuple[MeasuredResponse, Any]] = {}
    for path in normalize_asset_urls(cold_html, role):
        result, _, headers = measure_request(
            opener,
            build_get(role, port=port, path=path, accept="*/*"),
            role=role,
            category="cold_asset",
            path=path,
            timeout=timeout,
        )
        rows.append(result)
        require_response_status(result, {200})
        cold_assets[path] = (result, headers)

    # Cross a second boundary to catch any regression back to unstable
    # second-resolution URLs while preserving the stable release contract.
    time.sleep(1.05)
    warm_page, warm_html, _ = measure_request(
        opener,
        build_get(
            role,
            port=port,
            path=role.start_url,
            accept="text/html,application/xhtml+xml",
        ),
        role=role,
        category="warm_page",
        path=role.start_url,
        timeout=timeout,
    )
    rows.append(warm_page)
    require_response_status(warm_page, {200})
    warm_urls = normalize_asset_urls(warm_html, role)
    for path in warm_urls:
        result, _, _ = measure_request(
            opener,
            build_get(role, port=port, path=path, accept="*/*"),
            role=role,
            category="warm_asset_unconditional",
            path=path,
            timeout=timeout,
        )
        rows.append(result)
        require_response_status(result, {200})

    for path, (_, headers) in cold_assets.items():
        conditional: dict[str, str] = {}
        etag = str(headers.get("ETag", ""))
        last_modified = str(headers.get("Last-Modified", ""))
        if etag:
            conditional["If-None-Match"] = etag
        if last_modified:
            conditional["If-Modified-Since"] = last_modified
        if not conditional:
            continue
        result, _, _ = measure_request(
            opener,
            build_get(
                role,
                port=port,
                path=path,
                accept="*/*",
                conditional_headers=conditional,
            ),
            role=role,
            category="warm_asset_conditional",
            path=path,
            timeout=timeout,
        )
        rows.append(result)
        require_response_status(result, {200, 304})

    realtime_rows: list[MeasuredResponse] = []
    if role.realtime:
        version = 0
        for _ in range(realtime_polls):
            path = (
                f"/realtime/state/?after={version}&include_events=0"
                if version
                else "/realtime/state/?include_events=0"
            )
            result, body, _ = measure_request(
                opener,
                build_get(
                    role,
                    port=port,
                    path=path,
                    accept="application/json",
                ),
                role=role,
                category="realtime_idle",
                path=path,
                timeout=timeout,
            )
            rows.append(result)
            realtime_rows.append(result)
            require_response_status(result, {200})
            payload = json.loads(body.decode("utf-8"))
            version = max(version, int(payload.get("version") or 0))

    cold_urls = set(cold_assets)
    warm_url_set = set(warm_urls)
    changed_urls = sorted(
        path
        for path in cold_urls.symmetric_difference(warm_url_set)
        if path.startswith("/static/")
    )
    return {
        "role": role.role,
        "host": role.host,
        "simultaneous_sessions": role.simultaneous_sessions,
        "realtime": role.realtime,
        "poll_interval_seconds": role.poll_interval_seconds,
        "cookie_count": len(list(cookie_jar)),
        "cold_asset_count": len(cold_urls),
        "warm_asset_count": len(warm_url_set),
        "changed_static_urls_between_navigations": changed_urls,
        "rows": [asdict(row) for row in rows],
        "realtime_body_mean": (
            round(
                sum(row.body_bytes for row in realtime_rows)
                / len(realtime_rows),
                2,
            )
            if realtime_rows
            else 0
        ),
        "realtime_gzip_mean": (
            round(
                sum(row.gzip_bytes for row in realtime_rows)
                / len(realtime_rows),
                2,
            )
            if realtime_rows
            else 0
        ),
    }


def sum_category(role_result: dict[str, Any], category: str, field: str) -> int:
    return int(
        sum(
            int(row[field])
            for row in role_result["rows"]
            if row["category"] == category and row["status"] in {200, 304}
        )
    )


def build_summary(role_results: list[dict[str, Any]]) -> dict[str, Any]:
    role_summary = []
    shift_seconds = 12 * 60 * 60
    body_projection = 0.0
    gzip_projection = 0.0
    realtime_requests = 0
    for result in role_results:
        sessions = int(result["simultaneous_sessions"])
        projected_requests = 0
        poll_interval_seconds = result.get("poll_interval_seconds")
        if result["realtime"]:
            if not poll_interval_seconds or int(poll_interval_seconds) <= 0:
                raise ValueError(
                    f"{result['role']}: realtime poll interval is not defined."
                )
            projected_requests = round(
                sessions * shift_seconds / int(poll_interval_seconds)
            )
            body_projection += (
                float(result["realtime_body_mean"])
                * projected_requests
            )
            gzip_projection += (
                float(result["realtime_gzip_mean"])
                * projected_requests
            )
            realtime_requests += projected_requests
        role_summary.append(
            {
                "role": result["role"],
                "simultaneous_sessions": sessions,
                "realtime": result["realtime"],
                "poll_interval_seconds": poll_interval_seconds,
                "projected_realtime_requests_per_12h_shift": projected_requests,
                "cold_page_body_bytes": sum_category(
                    result, "cold_page", "body_bytes"
                ),
                "cold_assets_body_bytes": sum_category(
                    result, "cold_asset", "body_bytes"
                ),
                "warm_page_body_bytes": sum_category(
                    result, "warm_page", "body_bytes"
                ),
                "warm_assets_unconditional_body_bytes": sum_category(
                    result,
                    "warm_asset_unconditional",
                    "body_bytes",
                ),
                "warm_assets_conditional_body_bytes": sum_category(
                    result,
                    "warm_asset_conditional",
                    "body_bytes",
                ),
                "realtime_body_mean": result["realtime_body_mean"],
                "realtime_gzip_mean": result["realtime_gzip_mean"],
                "changed_static_urls_between_navigations": result[
                    "changed_static_urls_between_navigations"
                ],
            }
        )
    return {
        "roles": role_summary,
        "ready_role_count": len(role_results),
        "simultaneous_sessions": sum(
            int(item["simultaneous_sessions"]) for item in role_results
        ),
        "simultaneous_realtime_sessions": sum(
            int(item["simultaneous_sessions"])
            for item in role_results
            if item["realtime"]
        ),
        "realtime_requests_per_12h_shift": realtime_requests,
        "realtime_body_bytes_per_12h_shift": round(body_projection),
        "realtime_gzip_estimate_bytes_per_12h_shift": round(gzip_projection),
        "realtime_projection_assumes_visible_active_windows": True,
    }


def main() -> int:
    args = parse_args()
    validate_safe_args(args)
    normalized_run_id = validate_pwa_performance_qa_run_id(args.run_id)
    selected_role = next(
        role for role in READY_ROLES if role.role == args.role
    )
    direct_preflight = verify_pwa_performance_qa_database(normalized_run_id)
    server_preflight = verify_server_preflight(
        port=args.port,
        timeout=args.timeout_seconds,
        run_id=normalized_run_id,
        expected_fingerprint=direct_preflight['fingerprint'],
    )
    artifact_dir = artifact_directory_for(args.run_id, args.role)
    credentials = selected_role_credentials(
        selected_role,
        run_id=args.run_id,
        expected_database_fingerprint=direct_preflight['fingerprint'],
    )
    started_at = datetime.now(UTC)
    role_results = []
    errors = []
    try:
        role_results.append(
            audit_role(
                selected_role,
                port=args.port,
                timeout=args.timeout_seconds,
                realtime_polls=args.realtime_polls,
                credentials=credentials,
            )
        )
    except Exception as exc:  # noqa: BLE001 - keep complete audit evidence.
        errors.append(
            {
                'role': selected_role.role,
                'error': f'{type(exc).__name__}:{exc}',
            }
        )

    summary = build_summary(role_results)
    summary.update(
        {
            'status': (
                'PASS'
                if not errors and len(role_results) == 1
                else 'FAIL'
            ),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "target": "http://*.localhost:8000",
            "production_mutated": False,
            'run_id': normalized_run_id,
            'selected_role': selected_role.role,
            'selected_role_count': 1,
            'catalog_role_count': len(READY_ROLES),
            "errors": errors,
        }
    )
    artifact_hashes = {}
    artifact_hashes['preflight.json'] = write_canonical_new_json(
        artifact_dir / 'preflight.json',
        server_preflight,
    )
    artifact_hashes['traffic_detail.json'] = write_canonical_new_json(
        artifact_dir / 'traffic_detail.json',
        role_results,
    )
    artifact_hashes['summary.json'] = write_canonical_new_json(
        artifact_dir / 'summary.json',
        summary,
    )
    source_paths = {
        'tools/full_pwa_traffic_audit.py': Path(__file__).resolve(),
        'core/pwa_performance_qa.py': (
            Path(__file__).resolve().parents[1]
            / 'core'
            / 'pwa_performance_qa.py'
        ),
    }
    manifest = {
        'schema': 'copper-pwa-performance-artifact-manifest',
        'schema_version': 1,
        'run_id': normalized_run_id,
        'role': selected_role.role,
        'artifacts': artifact_hashes,
        'sources': {
            label: hashlib.sha256(path.read_bytes()).hexdigest().upper()
            for label, path in source_paths.items()
        },
    }
    write_canonical_new_json(artifact_dir / 'manifest.json', manifest)
    ensure_artifacts_contain_no_credentials(
        artifact_dir,
        selected_role,
        credentials,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
