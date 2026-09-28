"""Read-only, fail-closed SSE-QA host compatibility preflight.

This controller deliberately has no installer imports, secret inputs, service
operations, cleanup logic, or state-changing modes.  The only filesystem writes
are private temporary copies used by ``systemd-analyze verify``.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


MARKER = "SSE_QA_INSTALLATION_V2"
OWNERSHIP_SCHEMA = "SSE_QA_OWNERSHIP_V2"
REAL_ROOT = Path("/")
STATE_ROOT = Path("/var/lib/sse-qa")
OWNERSHIP_PATH = STATE_ROOT / "OWNERSHIP.json"
PORTS = (18080, 18082, 55432, 6381)
SYSTEMD_ASSETS = (
    "postgresql@16-sseqa.service.d/qa-limits.conf",
    "redis-sse-qa.service",
    "srv-sse-x2dqa.mount",
    "sse-qa-asgi.service",
    "sse-qa-reconcile.service",
    "sse-qa-wsgi.service",
    "sse-qa.slice",
    "sse-qa.target",
)
MANAGED_CONFLICT_PATHS = (
    Path("/etc/systemd/system/postgresql@16-sseqa.service.d/qa-limits.conf"),
    Path("/etc/systemd/system/redis-sse-qa.service"),
    Path("/etc/systemd/system/srv-sse\\x2dqa.mount"),
    Path("/etc/systemd/system/sse-qa-asgi.service"),
    Path("/etc/systemd/system/sse-qa-reconcile.service"),
    Path("/etc/systemd/system/sse-qa-wsgi.service"),
    Path("/etc/systemd/system/sse-qa.slice"),
    Path("/etc/systemd/system/sse-qa.target"),
    Path("/srv/sse-qa"),
    STATE_ROOT,
    Path("/etc/sse-qa"),
    Path("/etc/postgresql/16/sseqa"),
    Path("/etc/logrotate.d/sse-qa"),
    Path("/etc/nginx/sites-enabled/sse-qa.conf"),
)
REQUIRED_COMMANDS = (
    "systemctl", "systemd-analyze", "systemd-run", "losetup", "mkfs.ext4",
    "findmnt", "fallocate", "pg_createcluster", "pg_dropcluster", "psql",
    "pg_lsclusters", "redis-server", "redis-cli", "nginx", "python3.12",
    "useradd", "userdel", "runuser", "tar", "id", "getent", "mountpoint",
)


class PreflightError(RuntimeError):
    pass


def run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=30, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"inventory query unavailable: {command[0]}") from exc


def rooted(root: Path, absolute: str | Path | PurePosixPath) -> Path:
    text = absolute.as_posix() if isinstance(absolute, Path) else str(absolute)
    parts = tuple(part for part in text.split("/") if part)
    if not text.startswith("/") or ".." in parts:
        raise PreflightError("internal path must be absolute")
    return root.joinpath(*parts)


def require_absent(path: Path, description: str) -> None:
    if path.exists() or path.is_symlink():
        raise PreflightError(f"{description} already exists")


def check_identity_conflicts() -> list[str]:
    user = run(["id", "-u", "sseqa"])
    if user.returncode not in {0, 1}:
        raise PreflightError("OS user state query failed")
    if user.returncode == 0:
        raise PreflightError("OS user sseqa already exists")
    group = run(["getent", "group", "sseqa"])
    if group.returncode not in {0, 2}:
        raise PreflightError("OS group state query failed")
    if group.returncode == 0:
        raise PreflightError("OS group sseqa already exists")
    clusters = run(["pg_lsclusters", "--no-header"])
    if clusters.returncode != 0:
        raise PreflightError("PostgreSQL cluster state query failed")
    if re.search(r"(?m)^16\s+sseqa\s+", clusters.stdout):
        raise PreflightError("PostgreSQL cluster 16/sseqa already exists")
    return ["os_user_absent", "os_group_absent", "postgres_cluster_absent"]


def check_ports() -> None:
    for port in PORTS:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            number = exc.errno if isinstance(exc.errno, int) else -1
            if number == errno.EADDRINUSE:
                raise PreflightError(f"loopback port conflict: {port}") from exc
            raise PreflightError(f"loopback port inventory failed: {port} errno={number}") from exc
        finally:
            probe.close()


def verify_unit_syntax(bundle: Path) -> None:
    source_root = bundle / "config" / "systemd"
    actual = sorted(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*") if path.is_file()
    ) if source_root.is_dir() else []
    if actual != list(SYSTEMD_ASSETS):
        raise PreflightError("preflight unit asset set mismatch")
    with tempfile.TemporaryDirectory(prefix="sse-qa-preflight-units-") as raw:
        temporary = Path(raw)
        os.chmod(temporary, 0o700)
        for relative_text in SYSTEMD_ASSETS:
            source = source_root / relative_text
            relative = Path(relative_text)
            if relative_text == "srv-sse-x2dqa.mount":
                relative = Path("srv-sse\\x2dqa.mount")
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            text = source.read_text(encoding="utf-8")
            if destination.suffix == ".service":
                text = re.sub(
                    r"(?m)^(Exec(?:Start|StartPre|StartPost|Stop|StopPost|Reload)=)([-+!:@]*)(/\S+)(.*)$",
                    r"\1/bin/true\4", text,
                )
                text = re.sub(r"(?m)^WorkingDirectory=.*$", "WorkingDirectory=/", text)
                text = re.sub(r"(?m)^EnvironmentFile=.*$", "EnvironmentFile=-/dev/null", text)
                text = re.sub(r"(?m)^ReadWritePaths=.*$", "ReadWritePaths=/tmp", text)
            destination.write_text(text, encoding="utf-8", newline="\n")
        units = [
            str(path) for path in temporary.iterdir()
            if path.is_file() and path.suffix in {".service", ".slice", ".target", ".mount"}
        ]
        environment = os.environ.copy()
        environment["SYSTEMD_UNIT_PATH"] = f"{temporary}:/etc/systemd/system:/lib/systemd/system"
        completed = run(["systemd-analyze", "verify", *units], env=environment)
        if completed.returncode != 0:
            raise PreflightError("systemd unit verification failed")


def preflight(root: Path, bundle: Path, *, real_host: bool) -> list[str]:
    marker = rooted(root, STATE_ROOT / "INSTALLATION_MARKER")
    journal = rooted(root, OWNERSHIP_PATH)
    require_absent(marker, "QA installation marker")
    require_absent(journal, "QA ownership journal")
    conflicts = [
        str(path) for path in MANAGED_CONFLICT_PATHS
        if rooted(root, path).exists() or rooted(root, path).is_symlink()
    ]
    if conflicts:
        raise PreflightError("conflicting QA paths: " + ",".join(conflicts))

    checks = ["marker_absent", "ownership_journal_absent", "managed_paths_absent"]
    if real_host:
        if platform.machine().lower() not in {"x86_64", "amd64"}:
            raise PreflightError("SSE QA wheelhouse supports x86_64 only")
        missing = [name for name in REQUIRED_COMMANDS if shutil.which(name) is None]
        if missing:
            raise PreflightError("missing commands: " + ",".join(missing))
        try:
            controllers = Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()
            memory = Path("/proc/meminfo").read_text()
            stat = shutil.disk_usage("/var/lib")
        except OSError as exc:
            raise PreflightError("host capacity inventory unavailable") from exc
        if not {"cpu", "memory", "io", "pids"}.issubset(controllers):
            raise PreflightError("cgroup v2 controllers unavailable")
        if not Path("/dev/loop-control").exists():
            raise PreflightError("loop devices unavailable")
        if stat.free < 8 * 1024**3:
            raise PreflightError("less than 8 GiB free under /var/lib")
        match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", memory, re.M)
        if not match or int(match.group(1)) < 3 * 1024**2:
            raise PreflightError("less than 3 GiB available RAM")
        checks += ["x86_64", "commands", "cgroup_v2", "loop_ext4", "disk_free", "memory_available"]
        checks += check_identity_conflicts()
        verify_unit_syntax(bundle)
        checks.append("systemd_unit_syntax")
    else:
        checks.append("local_test_root")
    check_ports()
    checks += ["ports_18080_18082_55432_6381", "production_paths_disjoint"]
    return checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--test-root", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = REAL_ROOT
    real_host = True
    if args.test_root is not None:
        if os.getenv("SSE_QA_LOCAL_TEST") != "1":
            raise PreflightError("test root is disabled")
        root = args.test_root.resolve()
        real_host = False
    checks = preflight(root, args.bundle_root.resolve(), real_host=real_host)
    print("SSE_QA_PREFLIGHT_OK " + ",".join(checks))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreflightError as exc:
        print(f"SSE_QA_PREFLIGHT_FAIL {exc}", file=sys.stderr)
        raise SystemExit(2)
