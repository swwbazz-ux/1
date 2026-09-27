"""Fail-closed controller invoked only by fixed protected receiver modes.

The public CLI accepts no paths in real mode. ``--test-root`` is available only
when SSE_QA_LOCAL_TEST=1 and is used by the package contract tests.
"""

from __future__ import annotations

import argparse
import base64
import errno
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import os
import platform
import re
import signal
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath


MARKER = "SSE_QA_INSTALLATION_V2"
OWNERSHIP_SCHEMA = "SSE_QA_OWNERSHIP_V2"
REAL_ROOT = Path("/")
APP_ROOT = Path("/srv/sse-qa")
STATE_ROOT = Path("/var/lib/sse-qa")
ETC_ROOT = Path("/etc/sse-qa")
OWNERSHIP_PATH = STATE_ROOT / "OWNERSHIP.json"
PORTS = (18080, 18082, 55432, 6381)
PORT_RELEASE_TIMEOUT_SECONDS = 90.0
PORT_RELEASE_POLL_SECONDS = 0.25
SECRET_KEYS = {
    "schema", "allow_cidr", "basic_auth_line", "django_secret_key",
    "postgres_app_password", "postgres_maint_password", "redis_password",
    "driver_pin", "excavator_pin",
}
UNITS = (
    "sse-qa.slice", "srv-sse\\x2dqa.mount", "sse-qa.target",
    "redis-sse-qa.service", "sse-qa-wsgi.service", "sse-qa-asgi.service",
    "sse-qa-reconcile.service",
)
SERVICE_UNITS = (
    "sse-qa-asgi.service", "sse-qa-wsgi.service", "sse-qa-reconcile.service",
    "redis-sse-qa.service", "postgresql@16-sseqa.service",
)
QA_SLICE_CGROUP = "/sse.slice/sse-qa.slice"
SYSTEMD_SOURCE_TARGETS = (
    ("sse-qa.slice", "/etc/systemd/system/sse-qa.slice"),
    ("srv-sse-x2dqa.mount", "/etc/systemd/system/srv-sse\\x2dqa.mount"),
    ("sse-qa.target", "/etc/systemd/system/sse-qa.target"),
    ("redis-sse-qa.service", "/etc/systemd/system/redis-sse-qa.service"),
    ("sse-qa-wsgi.service", "/etc/systemd/system/sse-qa-wsgi.service"),
    ("sse-qa-asgi.service", "/etc/systemd/system/sse-qa-asgi.service"),
    ("sse-qa-reconcile.service", "/etc/systemd/system/sse-qa-reconcile.service"),
    (
        "postgresql@16-sseqa.service.d/qa-limits.conf",
        "/etc/systemd/system/postgresql@16-sseqa.service.d/qa-limits.conf",
    ),
)
MANAGED_CONFLICT_PATHS = tuple(target for _, target in SYSTEMD_SOURCE_TARGETS) + (
    APP_ROOT,
    STATE_ROOT,
    ETC_ROOT,
    Path("/etc/postgresql/16/sseqa"),
    Path("/etc/logrotate.d/sse-qa"),
    Path("/etc/nginx/sites-enabled/sse-qa.conf"),
)
INSTALL_TIMEOUTS = {
    "default": 120,
    "pip": 300,
    "migrate": 300,
    "collectstatic": 180,
    "systemd": 60,
}
_DIAGNOSTIC_SECRET_VALUES: set[str] = set()
_DIAGNOSTIC_STEP = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:postgres_app_password|postgres_maint_password|redis_password|"
    r"django_secret_key|password|secret|token|authorization|cookie|pin|"
    r"basic_auth(?:_line|_password)?)\b\s*[:=]\s*[^\s,;]+"
)
_PACKAGING_BOOTSTRAP_WHEEL = re.compile(
    r"^packaging-[0-9][A-Za-z0-9_.!+]*-py3-none-any\.whl$",
    re.IGNORECASE,
)
_WHEEL_COMPATIBILITY_CHECK = r"""
import json
import sys


def emit(payload, status=0):
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    raise SystemExit(status)


try:
    bootstrap_wheel = sys.argv[1]
    sys.path.insert(0, bootstrap_wheel)
    from packaging.tags import Tag, sys_tags
    from packaging.utils import InvalidWheelFilename, parse_wheel_filename
except Exception:
    emit({"ok": False, "error": "validator_dependency_unavailable"}, 70)

try:
    request = json.load(sys.stdin)
    wheel_names = request["wheel_names"]
    requested_tags = request.get("target_tags")
    if not isinstance(wheel_names, list) or not all(
        isinstance(name, str) for name in wheel_names
    ):
        raise ValueError("invalid wheel_names")
    if requested_tags is None:
        supported_tags = set(sys_tags())
    else:
        if not isinstance(requested_tags, list):
            raise ValueError("invalid target_tags")
        supported_tags = {
            Tag(*parts)
            for parts in requested_tags
            if isinstance(parts, list)
            and len(parts) == 3
            and all(isinstance(part, str) and part for part in parts)
        }
        if len(supported_tags) != len(requested_tags):
            raise ValueError("invalid target tag tuple")
except Exception:
    emit({"ok": False, "error": "validator_request_invalid"}, 64)

rejected = []
for wheel_name in wheel_names:
    try:
        _, _, _, wheel_tags = parse_wheel_filename(wheel_name)
    except InvalidWheelFilename:
        rejected.append(
            {"filename": wheel_name, "reason": "invalid_wheel_filename"}
        )
        continue
    if not wheel_tags.intersection(supported_tags):
        rejected.append(
            {
                "filename": wheel_name,
                "reason": "no_supported_python_abi_platform_tag",
            }
        )

emit({"ok": not rejected, "rejected": rejected})
"""


class QaError(RuntimeError):
    pass


class QaCancelled(QaError):
    pass


def _set_diagnostic_secret_values(values) -> None:
    global _DIAGNOSTIC_SECRET_VALUES
    _DIAGNOSTIC_SECRET_VALUES = {
        str(value) for value in values if isinstance(value, (str, int)) and len(str(value)) >= 4
    }


def _diagnostic_excerpt(value: str | bytes | None) -> str:
    if value is None:
        return "<empty>"
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    for secret in sorted(_DIAGNOSTIC_SECRET_VALUES, key=len, reverse=True):
        value = value.replace(secret, "<redacted>")
    value = _SENSITIVE_ASSIGNMENT.sub("sensitive=<redacted>", value)
    value = "".join(character if character in "\n\t" or character.isprintable() else "?" for character in value)
    value = value.replace("\r", "").strip()
    if len(value) > 1024:
        value = value[:1024] + "<truncated>"
    return value or "<empty>"


def _emit_command_diagnostic(*, step: str, kind: str, exit_value: int | str,
                             stdout: str | bytes | None, stderr: str | bytes | None) -> None:
    if not _DIAGNOSTIC_STEP.fullmatch(step):
        step = "invalid-step"
    payload = {
        "event": "SSE_QA_COMMAND_FAILED",
        "step": step,
        "kind": kind,
        "exit": exit_value,
        "stdout": _diagnostic_excerpt(stdout),
        "stderr": _diagnostic_excerpt(stderr),
    }
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True), file=sys.stderr)


def run(command: list[str], *, input_text: str | None = None, check: bool = True,
        env: dict[str, str] | None = None, cwd: str | Path | None = None,
        timeout: int | None = None, step: str = "subprocess") -> subprocess.CompletedProcess:
    effective_timeout = timeout or INSTALL_TIMEOUTS["default"]
    try:
        return subprocess.run(command, input=input_text, text=True, check=check,
                              capture_output=True, env=env, cwd=cwd,
                              timeout=effective_timeout)
    except subprocess.TimeoutExpired as exc:
        _emit_command_diagnostic(
            step=step, kind="timeout", exit_value=effective_timeout,
            stdout=exc.stdout, stderr=exc.stderr,
        )
        raise
    except subprocess.CalledProcessError as exc:
        _emit_command_diagnostic(
            step=step, kind="exit", exit_value=exc.returncode,
            stdout=exc.stdout, stderr=exc.stderr,
        )
        raise


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def new_ownership() -> dict[str, object]:
    return {
        "schema": OWNERSHIP_SCHEMA,
        "complete": False,
        "phase": "state_created",
        "files": {},
        "directories": [str(STATE_ROOT)],
        "user_created": False,
        "mount_started": False,
        "postgres_cluster_created": False,
        "redis_started": False,
        "cleanup_errors": [],
    }


def load_ownership(root: Path | None = None) -> dict[str, object]:
    root = REAL_ROOT if root is None else root
    path = rooted(root, OWNERSHIP_PATH)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QaError("owned installation journal missing or invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != OWNERSHIP_SCHEMA:
        raise QaError("unknown QA ownership journal")
    if not isinstance(value.get("files"), dict) or not isinstance(value.get("directories"), list):
        raise QaError("invalid QA ownership journal shape")
    return value


def save_ownership(state: dict[str, object], root: Path | None = None) -> None:
    root = REAL_ROOT if root is None else root
    atomic_json(rooted(root, OWNERSHIP_PATH), state)


def logical_path_text(absolute: str | Path | PurePosixPath) -> str:
    if isinstance(absolute, str) or isinstance(absolute, PurePosixPath):
        return str(absolute)
    if os.name == "nt" and isinstance(absolute, Path):
        return absolute.as_posix()
    return str(absolute)


def journal_file(state: dict[str, object], path: str | Path, root: Path | None = None) -> None:
    root = REAL_ROOT if root is None else root
    target = rooted(root, path)
    files = state.setdefault("files", {})
    assert isinstance(files, dict)
    files[logical_path_text(path)] = digest(target)
    save_ownership(state, root)


def journal_expected_digest(
    state: dict[str, object], path: str | Path, expected: str, root: Path | None = None,
) -> None:
    root = REAL_ROOT if root is None else root
    files = state.setdefault("files", {})
    assert isinstance(files, dict)
    files[logical_path_text(path)] = expected
    save_ownership(state, root)


def journal_directory(state: dict[str, object], path: Path, root: Path | None = None) -> None:
    root = REAL_ROOT if root is None else root
    directories = state.setdefault("directories", [])
    assert isinstance(directories, list)
    value = logical_path_text(path)
    if value not in directories:
        directories.append(value)
        save_ownership(state, root)


def rooted(root: Path, absolute: str | Path | PurePosixPath) -> Path:
    # Logical install paths are POSIX paths even when package contracts run on
    # Windows. A raw string must retain a literal backslash: systemd's escaped
    # mount unit is one filename (``srv-sse\\x2dqa.mount``), not a subdirectory.
    text = logical_path_text(absolute)
    parts = PurePathCompat(text)
    if not text.startswith("/") or ".." in parts:
        raise QaError("internal path must be absolute")
    return root.joinpath(*parts)


def PurePathCompat(text: str) -> tuple[str, ...]:
    return tuple(part for part in text.split("/") if part)


def validate_secrets(raw: bytes) -> dict[str, str | int]:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QaError("invalid secrets JSON") from exc
    if not isinstance(data, dict) or set(data) != SECRET_KEYS or data.get("schema") != 1:
        raise QaError("unexpected secrets schema")
    for key in SECRET_KEYS - {"schema"}:
        if not isinstance(data.get(key), str) or not data[key] or "REPLACE_" in data[key]:
            raise QaError(f"missing secret value: {key}")
    ipaddress.ip_network(data["allow_cidr"], strict=False)
    if not re.fullmatch(r"[A-Za-z0-9._-]+:\$2[aby]\$\d{2}\$.{53}", data["basic_auth_line"]):
        raise QaError("basic_auth_line must be one bcrypt htpasswd entry")
    for key in ("django_secret_key", "postgres_app_password", "postgres_maint_password", "redis_password"):
        if len(data[key]) < (64 if key == "django_secret_key" else 32):
            raise QaError(f"secret too short: {key}")
        if any(char in data[key] for char in "\r\n\x00@"):
            raise QaError(f"unsafe secret character: {key}")
        if key != "django_secret_key" and not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", data[key]):
            raise QaError(f"credential must be URL/ACL safe: {key}")
    for key in ("driver_pin", "excavator_pin"):
        if not re.fullmatch(r"\d{6}", data[key]):
            raise QaError(f"invalid PIN: {key}")
    if data["driver_pin"] == data["excavator_pin"]:
        raise QaError("QA PIN values must differ")
    return data


def render(template: str, replacements: dict[str, str]) -> str:
    result = template
    for key, value in replacements.items():
        result = result.replace(f"@@{key}@@", value)
    if "@@" in result:
        raise QaError("unresolved template marker")
    return result


def strict_loopback_bind_probe(port: int) -> tuple[bool, int | None, str | None]:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
        return True, None, None
    except OSError as exc:
        number = exc.errno if isinstance(exc.errno, int) else -1
        name = "EADDRINUSE" if number == errno.EADDRINUSE else errno.errorcode.get(number, "UNKNOWN")
        return False, number, name
    finally:
        probe.close()


def port_is_free(port: int) -> bool:
    return strict_loopback_bind_probe(port)[0]


def _fixed_port_tcp_states(ports: tuple[int, ...]) -> list[str]:
    """Return bounded socket-state evidence without process or command data."""
    try:
        completed = subprocess.run(
            ["ss", "-H", "-tan"], check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["snapshot_status=unavailable"]
    if completed.returncode != 0:
        return [f"snapshot_status=failed exit={completed.returncode}"]
    wanted = {str(port) for port in ports}
    lines: list[str] = []
    for raw in completed.stdout.splitlines():
        fields = raw.split()
        if not fields:
            continue
        if any(re.search(rf":{re.escape(port)}$", field) for port in wanted for field in fields):
            state = re.sub(r"[^A-Za-z0-9_-]", "_", fields[0])[:32]
            matched = sorted(port for port in wanted if any(field.endswith(f":{port}") for field in fields))
            lines.append(f"state={state} ports={','.join(matched)}")
            if len(lines) >= 64:
                break
    return lines or ["snapshot_status=no_matching_rows"]


def wait_for_qa_ports(
    evidence_path: Path,
    *,
    ports: tuple[int, ...] = PORTS,
    timeout_seconds: float = PORT_RELEASE_TIMEOUT_SECONDS,
    poll_seconds: float = PORT_RELEASE_POLL_SECONDS,
    probe=strict_loopback_bind_probe,
    monotonic=time.monotonic,
    sleeper=time.sleep,
    snapshot=_fixed_port_tcp_states,
    reporter=lambda line: None,
) -> None:
    deadline = monotonic() + timeout_seconds
    first_busy_written = False
    records: list[str] = [
        f"ports={','.join(str(port) for port in ports)}",
        f"deadline_seconds={timeout_seconds:g}",
    ]
    while True:
        busy: list[tuple[int, int, str]] = []
        for port in ports:
            ok, number, name = probe(port)
            if ok:
                continue
            number = -1 if number is None else number
            name = name or "UNKNOWN"
            if number != errno.EADDRINUSE:
                records.append(f"result=probe_error port={port} errno={number} name={name}")
                reporter(records[-1])
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_path.write_text("\n".join(records) + "\n", encoding="utf-8")
                raise QaError(f"loopback port probe failed: port={port} errno={number} name={name}")
            busy.append((port, number, name))
        if not busy:
            records.append("result=ready")
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text("\n".join(records) + "\n", encoding="utf-8")
            return
        if not first_busy_written:
            records.append("first_busy=" + ",".join(f"{p}:{n}:{name}" for p, n, name in busy))
            records.extend("first_" + line for line in snapshot(ports))
            for line in records[2:]:
                reporter(line)
            first_busy_written = True
        now = monotonic()
        if now >= deadline:
            records.append("timeout_busy=" + ",".join(f"{p}:{n}:{name}" for p, n, name in busy))
            timeout_snapshot = ["timeout_" + line for line in snapshot(ports)]
            records.extend(timeout_snapshot)
            records.append("result=timeout")
            for line in [records[-len(timeout_snapshot) - 2], *timeout_snapshot, records[-1]]:
                reporter(line)
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text("\n".join(records) + "\n", encoding="utf-8")
            port, number, name = busy[0]
            raise QaError(f"loopback port release timeout: port={port} errno={number} name={name}")
        sleeper(min(poll_seconds, max(0.0, deadline - now)))


def preflight(root: Path, installed_ok: bool = False) -> list[str]:
    checks: list[str] = []
    if root == REAL_ROOT:
        if platform.machine().lower() not in {"x86_64", "amd64"}:
            raise QaError("SSE QA wheelhouse supports x86_64 only")
        commands = (
            "systemctl", "systemd-analyze", "systemd-run", "losetup", "mkfs.ext4", "findmnt",
            "fallocate", "pg_createcluster", "pg_dropcluster", "psql",
            "pg_lsclusters", "redis-server", "redis-cli", "nginx", "python3.12",
            "useradd", "userdel", "runuser", "tar",
            "id", "mountpoint",
        )
        missing = [name for name in commands if shutil.which(name) is None]
        if missing:
            raise QaError("missing commands: " + ",".join(missing))
        controllers = Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()
        required = {"cpu", "memory", "io", "pids"}
        if not required.issubset(controllers):
            raise QaError("cgroup v2 controllers unavailable")
        if not Path("/dev/loop-control").exists():
            raise QaError("loop devices unavailable")
        stat = shutil.disk_usage("/var/lib")
        if stat.free < 8 * 1024**3:
            raise QaError("less than 8 GiB free under /var/lib")
        memory = Path("/proc/meminfo").read_text()
        match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", memory, re.M)
        if not match or int(match.group(1)) < 3 * 1024**2:
            raise QaError("less than 3 GiB available RAM")
        checks.extend(["x86_64", "commands", "cgroup_v2", "loop_ext4", "disk_free", "memory_available"])
        if run(["id", "-u", "sseqa"], check=False).returncode == 0 and not installed_ok:
            raise QaError("OS user sseqa already exists")
        clusters = run(["pg_lsclusters", "--no-header"], check=False).stdout
        if re.search(r"(?m)^16\s+sseqa\s+", clusters) and not installed_ok:
            raise QaError("PostgreSQL cluster 16/sseqa already exists")
        checks.extend(["os_user", "postgres_cluster_name"])
    else:
        checks.append("local_test_root")

    marker = rooted(root, STATE_ROOT / "INSTALLATION_MARKER")
    ownership = rooted(root, OWNERSHIP_PATH)
    owned = ownership.is_file()
    if not owned:
        existing = [
            str(item) for item in MANAGED_CONFLICT_PATHS
            if rooted(root, item).exists() or rooted(root, item).is_symlink()
        ]
        if existing:
            raise QaError("conflicting QA paths: " + ",".join(existing))
    elif not installed_ok:
        raise QaError("partial or complete QA ownership journal already exists")
    for port in PORTS:
        ok, number, name = strict_loopback_bind_probe(port)
        if not ok and not (installed_ok and marker.is_file()):
            if number == errno.EADDRINUSE:
                raise QaError(f"loopback port conflict: {port} errno={number} name={name}")
            raise QaError(f"loopback port probe failed: port={port} errno={number} name={name}")
    checks.append("ports_18080_18082_55432_6381")

    forbidden = (
        Path("/srv/accounting-mvp"), Path("/etc/accounting-mvp.env"),
        Path("/etc/redis/redis.conf"), Path("/etc/postgresql/16/main"),
    )
    if any(str(APP_ROOT).startswith(str(item)) for item in forbidden):
        raise QaError("QA path overlaps production")
    checks.append("production_paths_disjoint")

    if marker.exists() and marker.read_text(encoding="utf-8").strip() != MARKER:
        raise QaError("unknown QA installation marker")
    if owned:
        load_ownership(root)
    checks.append("ownership_journal")
    return checks


def _env_value(text: str, key: str) -> str:
    matches = re.findall(rf"(?m)^{re.escape(key)}=([^\r\n]*)$", text)
    if len(matches) != 1:
        raise QaError(f"invalid environment entry: {key}")
    return matches[0]


def _systemctl_property(unit: str, name: str) -> str:
    completed = run(["systemctl", "show", unit, "--property", name, "--value"])
    return completed.stdout.strip()


def _systemd_duration_usec(value: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(us|ms|s)", value.strip())
    if not match:
        raise QaError(f"invalid systemd duration: {value!r}")
    factors = {"us": 1, "ms": 1000, "s": 1_000_000}
    try:
        result = Decimal(match.group(1)) * factors[match.group(2)]
    except InvalidOperation as exc:
        raise QaError(f"invalid systemd duration: {value!r}") from exc
    if result != result.to_integral_value():
        raise QaError(f"non-integral systemd duration: {value!r}")
    return int(result)


def _qa_slice_cgroup() -> str:
    actual = _systemctl_property("sse-qa.slice", "ControlGroup")
    if actual != QA_SLICE_CGROUP:
        raise QaError("QA slice cgroup hierarchy mismatch")
    return actual


def _assert_qa_slice_limits() -> str:
    if _systemd_duration_usec(
        _systemctl_property("sse-qa.slice", "CPUQuotaPerSecUSec")
    ) != 1_000_000:
        raise QaError("QA slice CPU quota must equal exactly one CPU")
    if _systemd_duration_usec(
        _systemctl_property("sse-qa.slice", "CPUQuotaPeriodUSec")
    ) != 100_000:
        raise QaError("QA slice CPU quota period mismatch")
    expected = {
        "MemoryHigh": str(1792 * 1024**2),
        "MemoryMax": str(2 * 1024**3),
        "MemorySwapMax": "0",
        "TasksMax": "256",
    }
    for name, value in expected.items():
        if _systemctl_property("sse-qa.slice", name) != value:
            raise QaError(f"QA slice resource property mismatch: {name}")
    return _qa_slice_cgroup()


def _assert_unit_in_qa_slice(unit: str, parent_cgroup: str | None = None) -> None:
    if _systemctl_property(unit, "Slice") != "sse-qa.slice":
        raise QaError(f"unit outside QA slice: {unit}")
    parent_cgroup = _qa_slice_cgroup() if parent_cgroup is None else parent_cgroup
    expected = f"{parent_cgroup}/{unit}"
    if _systemctl_property(unit, "ControlGroup") != expected:
        raise QaError(f"unit cgroup membership mismatch: {unit}")


def _current_unified_cgroup() -> str:
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and controllers == "":
            return path
    raise QaError("unified process cgroup is unavailable")


def _assert_operation_scope(unit: str) -> None:
    parent_cgroup = _assert_qa_slice_limits()
    _assert_unit_in_qa_slice(unit, parent_cgroup)
    expected = f"{parent_cgroup}/{unit}"
    if _current_unified_cgroup() != expected:
        raise QaError(f"operation process is outside {unit}")


def _verify_real_runtime(expected_enabled: bool) -> list[str]:
    checks: list[str] = []
    mount_source = run(["findmnt", "-n", "-o", "SOURCE", "--target", str(APP_ROOT)]).stdout.strip()
    if not mount_source.startswith("/dev/loop"):
        raise QaError("QA mount source is not a loop device")
    backing = run(["losetup", "-n", "-O", "BACK-FILE", mount_source]).stdout.strip()
    if os.path.realpath(backing) != str(STATE_ROOT / "sse-qa.img"):
        raise QaError("QA loop mount backing file mismatch")
    checks.append("mount_source")

    parent_cgroup = _assert_qa_slice_limits()
    for unit in SERVICE_UNITS:
        if _systemctl_property(unit, "Slice") != "sse-qa.slice":
            raise QaError(f"unit outside QA slice: {unit}")
    checks.append("cgroup_limits_applied")

    active = {unit: _systemctl_property(unit, "ActiveState") for unit in SERVICE_UNITS}
    if expected_enabled:
        if any(value != "active" for value in active.values()):
            raise QaError("enabled QA has inactive services")
    elif any(value == "active" for value in active.values()):
        raise QaError("disabled QA has active services")
    checks.append("service_phase")
    temporarily_started = not expected_enabled
    try:
        if temporarily_started:
            # Keep the start inside the cleanup boundary.  systemctl may start
            # PostgreSQL and then fail on Redis; both fixed QA units must still
            # be stopped and verified below.
            run(
                ["systemctl", "start", "postgresql@16-sseqa.service", "redis-sse-qa.service"],
                timeout=INSTALL_TIMEOUTS["systemd"],
            )
        membership_units = (
            SERVICE_UNITS if expected_enabled
            else ("postgresql@16-sseqa.service", "redis-sse-qa.service")
        )
        for unit in membership_units:
            _assert_unit_in_qa_slice(unit, parent_cgroup)
        checks.append("actual_cgroup_membership")
        sql = (
            "SELECT rolname || ':' || rolconnlimit FROM pg_roles "
            "WHERE rolname IN ('sseqa_app','sseqa_maint') ORDER BY rolname;"
        )
        roles = run(["runuser", "-u", "postgres", "--", "psql", "-At", "-p", "55432", "-d", "postgres", "-c", sql]).stdout.splitlines()
        if roles != ["sseqa_app:8", "sseqa_maint:2"]:
            raise QaError("PostgreSQL role connection limits mismatch")
        max_connections = run(["runuser", "-u", "postgres", "--", "psql", "-At", "-p", "55432", "-d", "postgres", "-c", "SHOW max_connections;"]).stdout.strip()
        if max_connections != "16":
            raise QaError("PostgreSQL max_connections mismatch")
        checks.append("postgres_live_limits")

        if run(["runuser", "-u", "sseqa", "--", "test", "-r", "/etc/sse-qa/redis.acl"], check=False).returncode != 0:
            raise QaError("Redis ACL is not readable by sseqa")
        secret = json.loads(Path("/etc/sse-qa/secrets.json").read_text(encoding="utf-8"))["redis_password"]
        ping = run(
            ["redis-cli", "-h", "127.0.0.1", "-p", "6381", "--user", "sseqa", "PING"],
            env={**os.environ, "REDISCLI_AUTH": str(secret)},
        )
        if ping.stdout.strip() != "PONG":
            raise QaError("Redis ACL authentication failed")
        checks.append("redis_acl_live")

        env = os.environ.copy()
        for line in Path("/etc/sse-qa/app.env").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value
        verify = run(
            ["/srv/sse-qa/venv/bin/python", "manage.py", "seed_sse_qa", "--verify-only"],
            cwd="/srv/sse-qa/current/backend", env=env,
        )
        if "SSE_QA_FIXTURE_OK" not in verify.stdout:
            raise QaError("synthetic QA fixture verification failed")
        checks.append("synthetic_fixture")
    finally:
        if temporarily_started:
            stop = run(
                ["systemctl", "stop", "redis-sse-qa.service", "postgresql@16-sseqa.service"],
                check=False,
                timeout=INSTALL_TIMEOUTS["systemd"],
            )
            if stop.returncode != 0:
                raise QaError("temporary QA dependency stop failed")
            still_running = [
                unit for unit in ("redis-sse-qa.service", "postgresql@16-sseqa.service")
                if _systemctl_property(unit, "ActiveState") != "inactive"
            ]
            if still_running:
                raise QaError("temporary QA dependencies still active: " + ",".join(still_running))
    return checks


def verify_installation(root: Path) -> list[str]:
    checks = preflight(root, installed_ok=True)
    marker = rooted(root, STATE_ROOT / "INSTALLATION_MARKER")
    if not marker.is_file():
        checks.append("not_installed_preflight_only")
        return checks
    required = (
        Path("/etc/sse-qa/app.env"), Path("/etc/sse-qa/redis.conf"),
        Path("/etc/sse-qa/redis.acl"), Path("/etc/sse-qa/postgresql.conf"),
        Path("/etc/sse-qa/pg_hba.conf"),
    )
    missing = [str(path) for path in required if not rooted(root, path).is_file()]
    if missing:
        raise QaError("installed QA files missing: " + ",".join(missing))
    app_env = rooted(root, Path("/etc/sse-qa/app.env")).read_text(encoding="utf-8")
    enabled_text = _env_value(app_env, "SSE_PILOT_ENABLED")
    if enabled_text not in {"true", "false"}:
        raise QaError("invalid SSE kill switch")
    postgres = rooted(root, Path("/etc/sse-qa/postgresql.conf")).read_text(encoding="utf-8")
    if "port = 55432" not in postgres or "max_connections = 16" not in postgres:
        raise QaError("PostgreSQL isolation config mismatch")
    redis = rooted(root, Path("/etc/sse-qa/redis.conf")).read_text(encoding="utf-8")
    if "bind 127.0.0.1" not in redis or "port 6381" not in redis:
        raise QaError("Redis isolation config mismatch")
    ownership = load_ownership(root)
    if not ownership.get("complete"):
        raise QaError("QA installation is partial")
    files = ownership["files"]
    assert isinstance(files, dict)
    for raw_path, expected_hash in files.items():
        target = rooted(root, raw_path)
        if not target.is_file() or digest(target) != expected_hash:
            raise QaError(f"owned file changed or missing: {raw_path}")
    checks.extend(["installed_files", "kill_switch", "postgres_limits", "redis_loopback", "owned_file_hashes"])
    if root == REAL_ROOT:
        checks.extend(_verify_real_runtime(enabled_text == "true"))
    return checks


def verify_linux_units(bundle: Path, *, runtime_ready: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="sse-qa-units-") as raw:
        temporary = Path(raw)
        source_root = bundle / "config/systemd"
        for source in source_root.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            if relative.as_posix() == "srv-sse-x2dqa.mount":
                relative = Path("srv-sse\\x2dqa.mount")
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            text = source.read_text(encoding="utf-8")
            if not runtime_ready and destination.suffix == ".service":
                # systemd-analyze verifies executable existence.  Before install,
                # preserve and parse every directive but substitute only the first
                # executable token; the exact executable paths are checked below.
                text = re.sub(
                    r"(?m)^(Exec(?:Start|StartPre|StartPost|Stop|StopPost|Reload)=)([-+!:@]*)(/\S+)(.*)$",
                    r"\1/bin/true\4",
                    text,
                )
                text = re.sub(r"(?m)^WorkingDirectory=.*$", "WorkingDirectory=/", text)
                text = re.sub(r"(?m)^EnvironmentFile=.*$", "EnvironmentFile=-/dev/null", text)
                text = re.sub(r"(?m)^ReadWritePaths=.*$", "ReadWritePaths=/tmp", text)
            destination.write_text(text, encoding="utf-8")
        units = [
            str(path) for path in temporary.iterdir()
            if path.is_file() and path.suffix in {".service", ".slice", ".target", ".mount"}
        ]
        environment = os.environ.copy()
        environment["SYSTEMD_UNIT_PATH"] = str(temporary) + ":/etc/systemd/system:/lib/systemd/system"
        completed = run(["systemd-analyze", "verify", *units], check=False, env=environment)
        if completed.returncode != 0:
            details = (completed.stdout or "") + (completed.stderr or "")
            raise QaError("systemd unit verification failed: " + details[-1200:])
    if runtime_ready:
        for executable in (
            Path("/srv/sse-qa/venv/bin/python"), Path("/srv/sse-qa/venv/bin/pip"),
            Path("/srv/sse-qa/venv/bin/gunicorn"), Path("/srv/sse-qa/venv/bin/uvicorn"),
        ):
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise QaError(f"runtime executable missing: {executable}")


def write_private(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8", newline="\n")
    os.chmod(path, mode)


def validate_runtime_archive(path: Path) -> None:
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if not members or len(members) > 10000:
            raise QaError("invalid runtime archive member count")
        for member in members:
            normalized = member.name.replace("\\", "/")
            parts = tuple(part for part in normalized.split("/") if part)
            if not parts or parts[0] != "backend" or normalized.startswith("/") or ".." in parts:
                raise QaError("runtime archive escapes backend root")
            if not (member.isfile() or member.isdir()):
                raise QaError("runtime archive links/devices are forbidden")
            total += member.size
            if total > 250 * 1024 * 1024:
                raise QaError("runtime archive expands beyond 250 MiB")


def install_local_layout(
    bundle: Path,
    root: Path,
    secrets: dict[str, str | int],
    *,
    ownership: dict[str, object] | None = None,
    complete_marker: bool = True,
) -> None:
    """Deterministic filesystem phase; also exercised against a temporary root."""
    state_root = rooted(root, STATE_ROOT)
    etc = rooted(root, ETC_ROOT)
    app = rooted(root, APP_ROOT)
    for directory in (state_root, etc, app / "log", app / "metrics", app / "redis", app / "media"):
        directory.mkdir(parents=True, exist_ok=True)
    if complete_marker:
        write_private(state_root / "INSTALLATION_MARKER", MARKER + "\n", 0o600)

    def owned_write(path: Path, data: str, mode: int) -> None:
        if ownership is not None:
            journal_expected_digest(
                ownership, path, hashlib.sha256(data.encode("utf-8")).hexdigest(), root,
            )
        write_private(rooted(root, path), data, mode)

    def owned_copy(source: Path, path: Path) -> None:
        if ownership is not None:
            journal_expected_digest(ownership, path, digest(source), root)
        target = rooted(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    app_template = (bundle / "config/app.env.template").read_text(encoding="utf-8")
    app_env = render(app_template, {
        "DJANGO_SECRET_KEY": str(secrets["django_secret_key"]),
        "POSTGRES_APP_PASSWORD": str(secrets["postgres_app_password"]),
        "REDIS_URL_PASSWORD": str(secrets["redis_password"]),
        "DRIVER_PIN": str(secrets["driver_pin"]),
        "EXCAVATOR_PIN": str(secrets["excavator_pin"]),
    })
    owned_write(ETC_ROOT / "app.env", app_env, 0o640)
    redis = render((bundle / "config/redis/redis.conf.template").read_text(encoding="utf-8"), {})
    owned_write(ETC_ROOT / "redis.conf", redis, 0o640)
    redis_acl = render(
        (bundle / "config/redis/redis.acl.template").read_text(encoding="utf-8"),
        {"REDIS_PASSWORD": str(secrets["redis_password"])},
    )
    owned_write(ETC_ROOT / "redis.acl", redis_acl, 0o640)
    owned_write(ETC_ROOT / "htpasswd", str(secrets["basic_auth_line"]) + "\n", 0o640)
    nginx = render(
        (bundle / "config/nginx/sse-qa.conf.template").read_text(encoding="utf-8"),
        {"ALLOW_CIDR": str(secrets["allow_cidr"])},
    )
    owned_write(ETC_ROOT / "nginx.conf", nginx, 0o640)
    owned_copy(bundle / "config/postgresql/postgresql.conf", ETC_ROOT / "postgresql.conf")
    owned_copy(bundle / "config/postgresql/pg_hba.conf", ETC_ROOT / "pg_hba.conf")
    owned_write(ETC_ROOT / "secrets.json", json.dumps(secrets, sort_keys=True) + "\n", 0o600)


def _validate_wheel_compatibility(
    wheels: list[Path],
    *,
    target_tags: set[tuple[str, str, str]] | None = None,
    target_python: str | Path | None = None,
) -> None:
    bootstrap = [
        wheel for wheel in wheels
        if _PACKAGING_BOOTSTRAP_WHEEL.fullmatch(wheel.name)
    ]
    if len(bootstrap) != 1:
        raise QaError(
            "wheel compatibility validator dependency unavailable before "
            "application venv: expected exactly one "
            "packaging-*-py3-none-any.whl"
        )

    python = str(target_python) if target_python is not None else "/usr/bin/python3.12"
    if not Path(python).is_file():
        raise QaError(
            "wheel compatibility target interpreter unavailable: "
            "/usr/bin/python3.12 is required"
        )
    request = {
        "wheel_names": [wheel.name for wheel in wheels],
        "target_tags": (
            None
            if target_tags is None
            else [list(tag) for tag in sorted(target_tags)]
        ),
    }
    try:
        completed = subprocess.run(
            [
                python, "-I", "-S", "-c", _WHEEL_COMPATIBILITY_CHECK,
                str(bootstrap[0]),
            ],
            input=json.dumps(request, sort_keys=True),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        kind = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "unavailable"
        raise QaError(
            f"wheel compatibility validator {kind} before application venv"
        ) from None

    try:
        result = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        result = None
    if not isinstance(result, dict):
        raise QaError(
            "wheel compatibility validator returned invalid structured result"
        )
    if completed.returncode != 0 or result.get("error"):
        if result.get("error") == "validator_dependency_unavailable":
            raise QaError(
                "wheel compatibility validator dependency unavailable before "
                "application venv"
            )
        raise QaError(
            "wheel compatibility validator failed closed before application venv"
        )
    rejected = result.get("rejected")
    if not isinstance(rejected, list) or result.get("ok") not in {True, False}:
        raise QaError("wheel compatibility validator returned inconsistent result")
    if rejected:
        if result.get("ok") is not False:
            raise QaError("wheel compatibility validator returned inconsistent result")
        first = rejected[0]
        if not isinstance(first, dict):
            raise QaError("wheel compatibility validator returned inconsistent result")
        filename = first.get("filename")
        reason = first.get("reason")
        known_names = {wheel.name for wheel in wheels}
        if filename not in known_names or reason not in {
            "invalid_wheel_filename",
            "no_supported_python_abi_platform_tag",
        }:
            raise QaError("wheel compatibility validator returned inconsistent result")
        explanation = {
            "invalid_wheel_filename": "invalid wheel filename",
            "no_supported_python_abi_platform_tag": (
                "no supported Python/ABI/platform tag"
            ),
        }[reason]
        raise QaError(f"incompatible wheel {filename}: {explanation}")
    if result.get("ok") is not True:
        raise QaError("wheel compatibility validator returned inconsistent result")


def validate_wheelhouse(
    bundle: Path,
    *,
    target_tags: set[tuple[str, str, str]] | None = None,
    target_python: str | Path | None = None,
) -> Path:
    wheelhouse = bundle / "generated/wheelhouse"
    manifest = bundle / "generated/wheelhouse.sha256"
    if not wheelhouse.is_dir() or not manifest.is_file():
        raise QaError("generated wheelhouse or hash manifest missing")
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise QaError("wheelhouse is empty")
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})  ([A-Za-z0-9_.+-]+\.whl)", line)
        if not match or match.group(2) in expected:
            raise QaError("invalid wheelhouse hash manifest")
        expected[match.group(2)] = match.group(1).lower()
    actual = {path.name: digest(path) for path in wheels}
    if actual != expected:
        raise QaError("wheelhouse hash mismatch")
    _validate_wheel_compatibility(
        wheels,
        target_tags=target_tags,
        target_python=target_python,
    )
    return wheelhouse


def fault_injection(point: str) -> None:
    requested = os.getenv("SSE_QA_FAULT_AT", "")
    if not requested:
        return
    marker = Path("/run/sse-qa-disposable-test")
    if os.getenv("SSE_QA_FAULT_INJECTION") != "1" or not marker.is_file():
        raise QaError("fault injection is forbidden outside disposable test host")
    if requested == point:
        raise QaError(f"injected failure at {point}")


def cancellation_hold(point: str) -> None:
    """Hold a disposable install at a journalled ownership phase for SIGTERM.

    This is deliberately unavailable on an ordinary host.  The integration
    workflow uses it to remove the race between observing an install phase and
    cancelling its transient systemd unit.
    """
    requested = os.getenv("SSE_QA_CANCEL_HOLD_AT", "")
    if not requested:
        return
    marker = Path("/run/sse-qa-disposable-test")
    if os.getenv("SSE_QA_FAULT_INJECTION") != "1" or not marker.is_file():
        raise QaError("cancellation hold is forbidden outside disposable test host")
    if requested != "dependencies_started":
        raise QaError("unsupported cancellation hold point")
    if requested != point:
        return
    print(f"SSE_QA_CANCEL_HOLD_READY point={point}", flush=True)
    release = Path(f"/run/sse-qa-cycle/release-{point}")
    while not release.is_file():
        time.sleep(1)
    release.unlink()
    print(f"SSE_QA_CANCEL_HOLD_RELEASED point={point}", flush=True)


def assert_install_scope() -> None:
    _assert_operation_scope("sse-qa-install.service")


def _copy_owned(
    source: Path,
    target: str | Path,
    state: dict[str, object],
    root: Path | None = None,
) -> None:
    root = REAL_ROOT if root is None else root
    journal_expected_digest(state, target, digest(source), root)
    actual_target = rooted(root, target)
    actual_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, actual_target)
    if digest(actual_target) != digest(source):
        raise QaError(f"managed file copy mismatch: {target}")


def _postgres_cluster_status() -> str:
    completed = run(["pg_lsclusters", "--no-header", "16", "sseqa"], check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        return "missing"
    return "match" if "/srv/sse-qa/postgres/data" in completed.stdout.split() else "mismatch"


def _mount_status() -> str:
    if run(["mountpoint", "-q", str(APP_ROOT)], check=False).returncode != 0:
        return "missing"
    completed = run(["findmnt", "-n", "-o", "SOURCE", "--target", str(APP_ROOT)], check=False)
    if completed.returncode != 0:
        return "mismatch"
    source = completed.stdout.strip()
    if not source.startswith("/dev/loop"):
        return "mismatch"
    backing = run(["losetup", "-n", "-O", "BACK-FILE", source], check=False).stdout.strip()
    return "match" if os.path.realpath(backing) == str(STATE_ROOT / "sse-qa.img") else "mismatch"


def _owned_file_may_remove(state: dict[str, object], path: str | Path) -> bool:
    files = state.get("files", {})
    assert isinstance(files, dict)
    expected = files.get(logical_path_text(path))
    if expected is None:
        return False
    target = rooted(REAL_ROOT, path)
    return not target.is_symlink() and target.is_file() and digest(target) == expected


def _cleanup_guard_errors(state: dict[str, object]) -> list[str]:
    """Read-only ownership checks which must pass before cleanup mutates state."""
    errors: list[str] = []
    site = rooted(REAL_ROOT, "/etc/nginx/sites-enabled/sse-qa.conf")
    expected_site = rooted(REAL_ROOT, "/etc/sse-qa/nginx.conf")
    if site.is_symlink():
        if os.path.realpath(site) != os.path.realpath(expected_site):
            errors.append("foreign nginx QA site")
    elif site.exists():
        errors.append("foreign nginx QA site")

    cluster_status = "missing"
    if state.get("postgres_cluster_created"):
        cluster_status = _postgres_cluster_status()
        if cluster_status == "mismatch":
            errors.append("PostgreSQL cluster path/ownership mismatch")

    mount_status = "missing"
    if state.get("mount_started"):
        mount_status = _mount_status()
        if mount_status == "mismatch":
            errors.append("QA mount source mismatch")

    if cluster_status == "match" and mount_status != "match":
        errors.append("PostgreSQL cluster is not on the owned QA mount")

    files = state.get("files", {})
    assert isinstance(files, dict)
    for raw_path in files:
        target = rooted(REAL_ROOT, raw_path)
        if not target.exists() and not target.is_symlink():
            continue
        if not _owned_file_may_remove(state, raw_path):
            errors.append(f"owned file changed: {raw_path}")
    return errors


def _raise_cleanup_guard(state: dict[str, object], errors: list[str]) -> None:
    state["cleanup_errors"] = errors
    state["phase"] = "cleanup_failed"
    save_ownership(state)
    raise QaError("cleanup stopped safely: " + "; ".join(errors))


def cleanup_owned_installation(state: dict[str, object], *, remove_complete: bool) -> None:
    errors = _cleanup_guard_errors(state)
    if errors:
        _raise_cleanup_guard(state, errors)

    # A second complete guard after services stop closes the gap between the
    # initial decision and destructive PostgreSQL/filesystem operations.
    run(["systemctl", "stop", *SERVICE_UNITS], check=False, timeout=INSTALL_TIMEOUTS["systemd"])
    errors = _cleanup_guard_errors(state)
    if errors:
        _raise_cleanup_guard(state, errors)
    if remove_complete:
        slice_stop = run(
            ["systemctl", "stop", "sse-qa.slice"],
            check=False,
            timeout=INSTALL_TIMEOUTS["systemd"],
        )
        if slice_stop.returncode != 0:
            _raise_cleanup_guard(state, ["QA slice stop failed"])
        if _systemctl_property("sse-qa.slice", "ActiveState") != "inactive":
            _raise_cleanup_guard(state, ["QA slice still active"])

    site = rooted(REAL_ROOT, "/etc/nginx/sites-enabled/sse-qa.conf")
    expected_site = rooted(REAL_ROOT, "/etc/sse-qa/nginx.conf")
    if site.is_symlink() and os.path.realpath(site) == os.path.realpath(expected_site):
        site.unlink()
        run(["nginx", "-t"], check=False)
        run(["systemctl", "reload", "nginx"], check=False, timeout=INSTALL_TIMEOUTS["systemd"])
    elif site.exists() or site.is_symlink():
        errors.append("foreign nginx QA site")

    if state.get("postgres_cluster_created"):
        cluster_status = _postgres_cluster_status()
        if cluster_status == "match":
            if not state.get("mount_started") or _mount_status() != "match":
                _raise_cleanup_guard(state, ["PostgreSQL cluster is not on the owned QA mount"])
            completed = run(["pg_dropcluster", "--stop", "16", "sseqa"], check=False, timeout=INSTALL_TIMEOUTS["systemd"])
            if completed.returncode != 0:
                errors.append("PostgreSQL cluster removal failed")
        elif cluster_status == "mismatch":
            errors.append("PostgreSQL cluster path/ownership mismatch")

    if state.get("mount_started"):
        mount_status = _mount_status()
        if mount_status == "match":
            completed = run(["systemctl", "stop", "srv-sse\\x2dqa.mount"], check=False, timeout=INSTALL_TIMEOUTS["systemd"])
            if completed.returncode != 0:
                errors.append("QA mount stop failed")
        elif mount_status == "mismatch":
            errors.append("QA mount source mismatch")

    files = state.get("files", {})
    assert isinstance(files, dict)
    for raw_path in sorted(files, key=lambda item: (item.count("/"), item), reverse=True):
        target = rooted(REAL_ROOT, raw_path)
        if not target.exists() and not target.is_symlink():
            continue
        if not _owned_file_may_remove(state, raw_path):
            errors.append(f"owned file changed: {raw_path}")
            continue
        target.unlink()

    # No broad recursive delete is allowed until all remaining entries are known.
    for directory, allowed in (
        (Path("/etc/sse-qa"), set()),
        (Path("/etc/systemd/system/postgresql@16-sseqa.service.d"), set()),
        (APP_ROOT, set()),
        (STATE_ROOT, {"OWNERSHIP.json", "INSTALLATION_MARKER", "sse-qa.img"}),
    ):
        target = rooted(REAL_ROOT, directory)
        if not target.exists() or not target.is_dir():
            continue
        unexpected = {child.name for child in target.iterdir()} - allowed
        if unexpected:
            errors.append(f"unexpected entries under {directory}: {','.join(sorted(unexpected))}")

    if errors:
        state["cleanup_errors"] = errors
        state["phase"] = "cleanup_failed"
        save_ownership(state)
        raise QaError("cleanup stopped safely: " + "; ".join(errors))

    etc_root = rooted(REAL_ROOT, "/etc/sse-qa")
    etc_root.rmdir() if etc_root.is_dir() else None
    dropin = rooted(REAL_ROOT, "/etc/systemd/system/postgresql@16-sseqa.service.d")
    if dropin.is_dir():
        dropin.rmdir()
    app_root = rooted(REAL_ROOT, APP_ROOT)
    if app_root.is_dir():
        app_root.rmdir()
    for leaf in (STATE_ROOT / "INSTALLATION_MARKER", STATE_ROOT / "sse-qa.img"):
        rooted(REAL_ROOT, leaf).unlink(missing_ok=True)
    rooted(REAL_ROOT, OWNERSHIP_PATH).unlink(missing_ok=True)
    state_root = rooted(REAL_ROOT, STATE_ROOT)
    if state_root.is_dir():
        state_root.rmdir()
    if state.get("user_created"):
        run(["userdel", "sseqa"], check=False)
    run(["systemctl", "daemon-reload"], check=False, timeout=INSTALL_TIMEOUTS["systemd"])


def real_install(bundle: Path, secrets: dict[str, str | int]) -> None:
    if os.geteuid() != 0:
        raise QaError("install requires root receiver")
    preflight(REAL_ROOT)
    runtime = bundle / "generated/runtime.tar.gz"
    if not runtime.is_file():
        raise QaError("generated runtime missing")
    validate_runtime_archive(runtime)
    wheelhouse = validate_wheelhouse(bundle)
    verify_linux_units(bundle, runtime_ready=False)
    python_version = run(
        ["/usr/bin/python3.12", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        step="python-version",
    ).stdout.strip()
    if python_version != "3.12":
        raise QaError("target interpreter must be Python 3.12")
    assert_install_scope()

    Path(STATE_ROOT).mkdir(mode=0o700, parents=True, exist_ok=False)
    state = new_ownership()
    save_ownership(state)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def cancel_install(_signum, _frame):
        raise QaCancelled("install cancelled")

    signal.signal(signal.SIGTERM, cancel_install)
    try:
        run(["useradd", "--system", "--home", str(APP_ROOT), "--shell", "/usr/sbin/nologin", "sseqa"], step="user-create")
        state["user_created"] = True
        state["phase"] = "user_created"
        save_ownership(state)
        run(["fallocate", "-l", "6G", str(STATE_ROOT / "sse-qa.img")], step="image-allocate")
        run(["mkfs.ext4", "-F", "-L", "SSE_QA_V2", str(STATE_ROOT / "sse-qa.img")], step="image-format")
        state["phase"] = "image_created"
        save_ownership(state)
        fault_injection("after_image_before_marker")

        for relative, target_text in SYSTEMD_SOURCE_TARGETS:
            _copy_owned(bundle / "config/systemd" / relative, target_text, state)
        run(["systemctl", "daemon-reload"], timeout=INSTALL_TIMEOUTS["systemd"], step="systemd-reload")
        state["mount_started"] = True
        save_ownership(state)
        run(["systemctl", "start", "srv-sse\\x2dqa.mount"], timeout=INSTALL_TIMEOUTS["systemd"], step="mount-start")
        if _mount_status() != "match":
            raise QaError("mounted QA filesystem does not match owned image")
        state["phase"] = "mounted"
        save_ownership(state)

        install_local_layout(bundle, REAL_ROOT, secrets, ownership=state, complete_marker=False)
        for path, group in (
            ("/etc/sse-qa/app.env", "sseqa"), ("/etc/sse-qa/redis.conf", "sseqa"),
            ("/etc/sse-qa/redis.acl", "sseqa"), ("/etc/sse-qa/htpasswd", "www-data"),
        ):
            shutil.chown(path, user="root", group=group)
        for directory in APP_ROOT.iterdir():
            shutil.chown(directory, user="sseqa", group="sseqa")
        if run(["runuser", "-u", "sseqa", "--", "test", "-r", "/etc/sse-qa/redis.acl"], check=False).returncode != 0:
            raise QaError("Redis ACL is not readable by service account")
        _copy_owned(bundle / "config/logrotate/sse-qa", Path("/etc/logrotate.d/sse-qa"), state)

        release = APP_ROOT / "releases/r3"
        release.mkdir(parents=True, exist_ok=False)
        run(["tar", "-xzf", str(runtime), "-C", str(release), "--no-same-owner", "--no-same-permissions"], step="runtime-extract")
        (APP_ROOT / "current").symlink_to(release, target_is_directory=True)
        for writable in (release / "backend/media", release / "backend/private_media"):
            writable.mkdir(parents=True, exist_ok=True)
            shutil.chown(writable, user="sseqa", group="sseqa")
        run(["/usr/bin/python3.12", "-m", "venv", str(APP_ROOT / "venv")], step="venv-create")
        run(
            [str(APP_ROOT / "venv/bin/pip"), "install", "--no-index", "--find-links", str(wheelhouse),
             "-r", str(APP_ROOT / "current/backend/requirements.txt"), "uvicorn==0.37.0"],
            timeout=INSTALL_TIMEOUTS["pip"],
            step="pip-install",
        )
        verify_linux_units(bundle, runtime_ready=True)

        state["postgres_cluster_created"] = True
        save_ownership(state)
        run(["pg_createcluster", "16", "sseqa", "--port", "55432", "--datadir", str(APP_ROOT / "postgres/data"), "--start-conf", "manual"], timeout=INSTALL_TIMEOUTS["systemd"], step="postgres-create")
        state["phase"] = "postgres_created"
        save_ownership(state)
        shutil.copy2("/etc/sse-qa/postgresql.conf", "/etc/postgresql/16/sseqa/postgresql.conf")
        shutil.copy2("/etc/sse-qa/pg_hba.conf", "/etc/postgresql/16/sseqa/pg_hba.conf")
        (APP_ROOT / "log/postgresql").mkdir(parents=True, exist_ok=True)
        shutil.chown(APP_ROOT / "log/postgresql", user="postgres", group="postgres")
        run(["systemctl", "daemon-reload"], timeout=INSTALL_TIMEOUTS["systemd"], step="postgres-reload")
        run(["systemctl", "start", "postgresql@16-sseqa.service"], timeout=INSTALL_TIMEOUTS["systemd"], step="postgres-start")
        sql = (
            "CREATE ROLE sseqa_app LOGIN CONNECTION LIMIT 8 PASSWORD '" + str(secrets["postgres_app_password"]).replace("'", "''") + "';\n"
            "CREATE ROLE sseqa_maint LOGIN CONNECTION LIMIT 2 PASSWORD '" + str(secrets["postgres_maint_password"]).replace("'", "''") + "';\n"
            "CREATE DATABASE sseqa OWNER sseqa_app;\n"
            "REVOKE ALL ON DATABASE sseqa FROM PUBLIC;\n"
            "GRANT CONNECT ON DATABASE sseqa TO sseqa_app, sseqa_maint;\n"
        )
        run(["runuser", "-u", "postgres", "--", "psql", "-p", "55432", "-d", "postgres", "-v", "ON_ERROR_STOP=1"], input_text=sql, step="postgres-bootstrap")
        run(["systemctl", "start", "redis-sse-qa.service"], timeout=INSTALL_TIMEOUTS["systemd"], step="redis-start")
        for unit in ("postgresql@16-sseqa.service", "redis-sse-qa.service"):
            _assert_unit_in_qa_slice(unit)
        state["redis_started"] = True
        state["phase"] = "dependencies_started"
        save_ownership(state)
        fault_injection("after_postgres_redis_start")
        cancellation_hold("dependencies_started")
        env = os.environ.copy()
        for line in Path("/etc/sse-qa/app.env").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value
        backend = APP_ROOT / "current/backend"
        run([str(APP_ROOT / "venv/bin/python"), "manage.py", "migrate", "--noinput"], cwd=backend, env=env, timeout=INSTALL_TIMEOUTS["migrate"], step="django-migrate")
        run([str(APP_ROOT / "venv/bin/python"), "manage.py", "seed_sse_qa"], cwd=backend, env=env, step="django-seed")
        run([str(APP_ROOT / "venv/bin/python"), "manage.py", "seed_sse_qa", "--verify-only"], cwd=backend, env=env, step="django-seed-verify")
        run([str(APP_ROOT / "venv/bin/python"), "manage.py", "collectstatic", "--noinput"], cwd=backend, env=env, timeout=INSTALL_TIMEOUTS["collectstatic"], step="django-collectstatic")
        run(["systemctl", "stop", "redis-sse-qa.service", "postgresql@16-sseqa.service"], timeout=INSTALL_TIMEOUTS["systemd"], step="dependencies-stop")
        write_private(STATE_ROOT / "INSTALLATION_MARKER", MARKER + "\n", 0o600)
        state["complete"] = True
        state["phase"] = "complete_disabled"
        save_ownership(state)
    except BaseException as exc:
        try:
            cleanup_owned_installation(state, remove_complete=False)
        except Exception as cleanup_exc:
            raise QaError(f"install failed ({type(exc).__name__}); cleanup incomplete: {cleanup_exc}") from exc
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    print("SSE_QA_INSTALL_OK enabled=false clients=0")


def set_sse_enabled(enabled: bool) -> None:
    path = Path("/etc/sse-qa/app.env")
    state = load_ownership()
    if not _owned_file_may_remove(state, path):
        raise QaError("refusing changed or unowned app.env")
    before = path.read_text(encoding="utf-8")
    after, count = re.subn(r"(?m)^SSE_PILOT_ENABLED=(?:true|false)$", f"SSE_PILOT_ENABLED={'true' if enabled else 'false'}", before)
    if count != 1:
        raise QaError("invalid SSE kill-switch entry")
    journal_expected_digest(
        state, Path("/etc/sse-qa/app.env"), hashlib.sha256(after.encode("utf-8")).hexdigest(),
    )
    write_private(path, after, 0o640)
    shutil.chown(path, user="root", group="sseqa")


def _real_enable_scoped() -> None:
    preflight(REAL_ROOT, installed_ok=True)
    verify_installation(REAL_ROOT)
    for path in (
        Path("/etc/letsencrypt/live/sse-qa.driverform.ru/fullchain.pem"),
        Path("/etc/letsencrypt/live/sse-qa.driverform.ru/privkey.pem"),
    ):
        if not path.is_file():
            raise QaError(f"TLS gate missing: {path}")
    site = Path("/etc/nginx/sites-enabled/sse-qa.conf")
    if site.exists() or site.is_symlink():
        raise QaError("nginx QA site path already exists")
    try:
        set_sse_enabled(True)
        run(["systemctl", "start", "postgresql@16-sseqa.service", "redis-sse-qa.service",
             "sse-qa-reconcile.service", "sse-qa-wsgi.service", "sse-qa-asgi.service"])
        site.symlink_to("/etc/sse-qa/nginx.conf")
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])
        state = load_ownership()
        state["phase"] = "complete_enabled"
        save_ownership(state)
        verify_installation(REAL_ROOT)
    except Exception as exc:
        try:
            real_disable(emit_summary=False)
        except Exception as rollback_exc:
            raise QaError("QA enable failed and rollback was not confirmed") from rollback_exc
        raise
    print("SSE_QA_ENABLE_OK max_sse_clients=2")


def real_enable() -> None:
    _assert_operation_scope("sse-qa-enable.service")
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def cancel_enable(_signum, _frame):
        raise QaCancelled("enable cancelled")

    signal.signal(signal.SIGTERM, cancel_enable)
    try:
        _real_enable_scoped()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


def real_disable(*, emit_summary: bool = True) -> None:
    state = load_ownership()
    marker = Path("/var/lib/sse-qa/INSTALLATION_MARKER")
    complete = marker.is_file() and marker.read_text().strip() == MARKER
    site = Path("/etc/nginx/sites-enabled/sse-qa.conf")
    reload_error: Exception | None = None
    switch_error: Exception | None = None
    stop_error: Exception | None = None
    try:
        if site.is_symlink() and os.path.realpath(site) == "/etc/sse-qa/nginx.conf":
            site.unlink()
            run(["nginx", "-t"])
            run(["systemctl", "reload", "nginx"])
        elif site.exists() or site.is_symlink():
            raise QaError("refusing unknown nginx QA path")
    except Exception as exc:
        reload_error = exc
    finally:
        try:
            if Path("/etc/sse-qa/app.env").is_file():
                set_sse_enabled(False)
        except Exception as exc:
            switch_error = exc
        finally:
            try:
                stop = run(
                    ["systemctl", "stop", "sse-qa-asgi.service", "sse-qa-wsgi.service",
                     "sse-qa-reconcile.service", "redis-sse-qa.service", "postgresql@16-sseqa.service"],
                    check=False,
                    timeout=INSTALL_TIMEOUTS["systemd"],
                )
                if stop.returncode != 0:
                    raise QaError("QA service stop command failed")
                still_running = [
                    unit for unit in SERVICE_UNITS
                    if _systemctl_property(unit, "ActiveState") != "inactive"
                ]
                if still_running:
                    raise QaError("QA services still active: " + ",".join(still_running))
            except Exception as exc:
                stop_error = exc
    failure = reload_error or switch_error or stop_error
    if failure is not None:
        state = load_ownership()
        state["phase"] = "disable_failed"
        errors = list(state.get("cleanup_errors", []))
        errors.append(type(failure).__name__ + ": " + str(failure))
        state["cleanup_errors"] = errors[-20:]
        save_ownership(state)
        raise QaError("QA disable was not confirmed") from failure
    state = load_ownership()
    state["phase"] = "complete_disabled" if complete else "partial_disabled"
    save_ownership(state)
    if emit_summary:
        print(f"SSE_QA_DISABLE_OK data_preserved=true complete={'true' if complete else 'false'}")


def real_remove() -> None:
    state = load_ownership()
    try:
        real_disable()
    except QaError as exc:
        # A partial install may not have app.env/nginx yet.  Continue only when
        # the authenticated ownership journal is still valid.
        if state.get("complete"):
            raise
        state["phase"] = "partial_remove_after_disable_error"
        state["disable_error"] = str(exc)
        save_ownership(state)
    state = load_ownership()
    cleanup_owned_installation(state, remove_complete=True)
    print("SSE_QA_REMOVE_OK production_touched=false")


def real_smoke() -> None:
    _assert_operation_scope("sse-qa-smoke.service")
    state = load_ownership()
    if not state.get("complete") or state.get("phase") != "complete_enabled":
        raise QaError("business smoke requires enabled complete QA")
    env = os.environ.copy()
    for line in Path("/etc/sse-qa/app.env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    result = run(
        ["/srv/sse-qa/venv/bin/python", "manage.py", "seed_sse_qa", "--business-smoke"],
        cwd="/srv/sse-qa/current/backend", env=env, timeout=180,
    )
    marker_pattern = re.compile(
        r"^SSE_QA_BUSINESS_SMOKE_OK logins=2 screens=2 "
        r"trip_id=[1-9][0-9]* version=[1-9][0-9]* catchup=1 sse=1$"
    )
    markers = [line for line in result.stdout.splitlines() if marker_pattern.fullmatch(line)]
    if len(markers) != 1:
        raise QaError("business smoke returned no unique fixed success marker")
    # Emit only the schema-checked marker.  Never relay arbitrary command
    # output into workflow evidence.
    print(markers[0])
    print("SSE_QA_SMOKE_OK clients=2 synthetic_trip=1 delivery=1")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("preflight", "wait-ports", "verify", "install", "enable", "smoke", "disable", "remove", "render-test"))
    parser.add_argument("--bundle-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--secrets-file", type=Path)
    parser.add_argument("--test-root", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    bundle = args.bundle_root.resolve()
    root = REAL_ROOT
    if args.test_root is not None:
        if os.getenv("SSE_QA_LOCAL_TEST") != "1":
            raise QaError("test root is disabled")
        root = args.test_root.resolve()
    if args.operation == "preflight":
        if root == REAL_ROOT:
            verify_linux_units(bundle, runtime_ready=False)
        checks = preflight(root, installed_ok=rooted(root, OWNERSHIP_PATH).is_file())
        print("SSE_QA_PREFLIGHT_OK " + ",".join(checks))
        return 0
    if args.operation == "wait-ports":
        if root != REAL_ROOT:
            raise QaError("wait-ports requires real mode")
        wait_for_qa_ports(
            Path("/run/sse-qa-port-readiness.txt"),
            reporter=lambda line: print("SSE_QA_PORT_DIAGNOSTIC " + line),
        )
        print("SSE_QA_PORTS_READY_OK ports=18080,18082,55432,6381 deadline=90")
        return 0
    if args.operation == "verify":
        if root == REAL_ROOT:
            installed = rooted(root, STATE_ROOT / "INSTALLATION_MARKER").is_file()
            verify_linux_units(bundle, runtime_ready=installed)
            if installed:
                _assert_operation_scope("sse-qa-verify.service")
        checks = verify_installation(root)
        print("SSE_QA_VERIFY_OK " + ",".join(checks))
        return 0
    if args.operation in {"install", "render-test"}:
        if args.secrets_file is None:
            raise QaError("secrets file required")
        secrets = validate_secrets(args.secrets_file.read_bytes())
        if root != REAL_ROOT or args.operation == "render-test":
            preflight(root)
            install_local_layout(bundle, root, secrets)
            print("SSE_QA_RENDER_TEST_OK")
            return 0
        _set_diagnostic_secret_values(secrets.values())
        try:
            real_install(bundle, secrets)
        finally:
            _set_diagnostic_secret_values(())
        return 0
    if root != REAL_ROOT:
        raise QaError("state-changing test-root operation is forbidden")
    if args.operation == "enable":
        real_enable()
    elif args.operation == "smoke":
        real_smoke()
    elif args.operation == "disable":
        real_disable()
    elif args.operation == "remove":
        real_remove()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QaError as exc:
        print(f"SSE_QA_FAIL {exc}", file=sys.stderr)
        raise SystemExit(2)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("SSE_QA_FAIL child command failed; see fixed command diagnostic", file=sys.stderr)
        raise SystemExit(2)
