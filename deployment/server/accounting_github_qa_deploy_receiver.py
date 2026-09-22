#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import ctypes
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

try:
    import fcntl
    import grp
    import pwd
except ImportError:  # pragma: no cover - protocol tests run on Windows
    fcntl = None
    grp = None
    pwd = None


APP = Path("/srv/accounting-mvp-excavator-qa")
PRODUCTION_APP = Path("/srv/accounting-mvp")
BACKUPS = Path("/var/backups/accounting-mvp-excavator-qa/releases")
STATE = Path("/var/lib/accounting-github-qa-deploy")
VERIFICATIONS = STATE / "verified"
CURRENT = STATE / "current.json"
INITIALIZED = STATE / "initialized.json"
LOCK = Path("/run/lock/accounting-github-qa-deploy.lock")
SERVICE = "accounting-mvp-excavator-qa"
SIMULATOR_SERVICE = "accounting-mvp-excavator-qa-simulator"
APP_OS_USER = "accounting-qa"
APP_OS_GROUP = "accounting-qa"
SOCKET = Path("/run/accounting-mvp-excavator-qa/app.sock")
FCM_CONFIG_PATH = Path("/etc/accounting-mvp-excavator-qa/firebase-service-account.json")
POLICY_PATH = Path("/etc/accounting-mvp-excavator-qa/release-policy.json")
EXPECTED_DATABASE = "accounting_mvp_excavator_qa"
EXPECTED_HOSTS = {
    "qa-admin.driverform.ru": "admin",
    "qa-driver.driverform.ru": "driver",
    "qa-excavator.driverform.ru": "excavator_operator",
}
EXPECTED_ORIGINS = {f"https://{host}" for host in EXPECTED_HOSTS}
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 300 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120
DJANGO_COMMAND_TIMEOUT_SECONDS = 300
DATABASE_COMMAND_TIMEOUT_SECONDS = 600
SYSTEMD_COMMAND_TIMEOUT_SECONDS = 60
CURL_COMMAND_TIMEOUT_SECONDS = 20
MUTATING_PHASE_TIMEOUT_SECONDS = 30 * 60
RECOVERY_PHASE_TIMEOUT_SECONDS = 15 * 60
OUTER_RELEASE_TIMEOUT_SECONDS = 60 * 60
_COMMAND_DEADLINE_MONOTONIC: float | None = None
MODES = {"qa_audit", "qa_verify", "qa_deploy", "qa_rollback"}
PAYLOAD_MODES = {"qa_verify", "qa_deploy"}
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
ROLLBACK_RE = re.compile(r"qa-github-[0-9]{8}-[0-9]{6}-[0-9a-f]{12}-before\Z")
REQUIRED_FILES = {
    "manage.py",
    "requirements.txt",
    "config/settings.py",
    "core/management/commands/check_excavator_qa_runtime.py",
}
DENIED_PARTS = {
    ".env", ".venv", "__pycache__", "media", "private_media", "staticfiles", "backups",
}
DENIED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx"}
MANAGED_DIRECTORIES = {
    "assignments", "config", "core", "deploy", "downtimes", "portal",
    "references", "reports", "rotations", "settlement", "shifts", "static",
    "templates", "tools", "trips", "users",
}
MANAGED_ROOT_FILES = {
    ".coveragerc", ".env.example", "AGENTS.md", "manage.py", "requirements.txt",
    "requirements-coverage.txt", "requirements-quality.txt", "requirements-security.txt",
}


class ReleaseError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def set_command_deadline(seconds: int | None) -> None:
    global _COMMAND_DEADLINE_MONOTONIC
    _COMMAND_DEADLINE_MONOTONIC = (
        None if seconds is None else time.monotonic() + seconds
    )


def bounded_timeout(requested_seconds: int) -> float:
    if _COMMAND_DEADLINE_MONOTONIC is None:
        return float(requested_seconds)
    remaining = _COMMAND_DEADLINE_MONOTONIC - time.monotonic()
    if remaining <= 0:
        raise ReleaseError("QA operation deadline expired before command start")
    return max(0.1, min(float(requested_seconds), remaining))


def assert_qa_boundaries() -> None:
    if (
        MUTATING_PHASE_TIMEOUT_SECONDS + RECOVERY_PHASE_TIMEOUT_SECONDS
        >= OUTER_RELEASE_TIMEOUT_SECONDS
    ):
        raise ReleaseError("QA receiver deadline must remain below the outer release timeout")
    if APP != Path("/srv/accounting-mvp-excavator-qa") or APP == PRODUCTION_APP:
        raise ReleaseError("QA receiver application boundary is invalid")
    if SERVICE == "accounting-mvp" or SOCKET == Path("/run/accounting-mvp/accounting-mvp.sock"):
        raise ReleaseError("QA receiver service boundary is invalid")
    if EXPECTED_DATABASE == "accounting_mvp":
        raise ReleaseError("QA receiver database boundary is invalid")
    if FCM_CONFIG_PATH == Path("/etc/accounting-mvp/firebase-service-account.json"):
        raise ReleaseError("QA receiver FCM boundary is invalid")
    if APP.is_symlink() or (
        APP.exists() and APP.resolve() == PRODUCTION_APP.resolve(strict=False)
    ):
        raise ReleaseError("QA application root cannot resolve to production")
    for guarded in (
        APP / ".env", APP / ".venv", BACKUPS, FCM_CONFIG_PATH, POLICY_PATH, SOCKET
    ):
        if guarded.is_symlink():
            raise ReleaseError(f"QA boundary cannot be a symlink: {guarded}")
    if (
        STATE.is_symlink()
        or VERIFICATIONS.is_symlink()
        or CURRENT.is_symlink()
        or INITIALIZED.is_symlink()
    ):
        raise ReleaseError("QA receiver state paths cannot be symlinks")
    production_candidates = [PRODUCTION_APP]
    if PRODUCTION_APP.is_dir():
        production_candidates.extend(
            path for path in PRODUCTION_APP.iterdir() if path.exists()
        )
    qa_candidates = [APP]
    if BACKUPS.exists():
        qa_candidates.append(BACKUPS)
    if APP.is_dir():
        qa_candidates.extend(
            APP / name for name in (MANAGED_DIRECTORIES | MANAGED_ROOT_FILES)
            if (APP / name).exists()
        )
    for qa_path in qa_candidates:
        for production_path in production_candidates:
            try:
                if os.path.samefile(qa_path, production_path):
                    raise ReleaseError(
                        f"QA path is bind-mounted to production: {qa_path}"
                    )
            except FileNotFoundError:
                continue


def load_policy() -> dict[str, Any]:
    if POLICY_PATH.is_symlink() or not POLICY_PATH.is_file():
        raise ReleaseError("root-owned QA release policy is missing")
    stat = POLICY_PATH.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022:
        raise ReleaseError("QA release policy must be root-owned and not group/world writable")
    try:
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("QA release policy is invalid") from exc
    required = {
        "schema": 1,
        "target": "accounting-mvp-excavator-qa",
        "database_name": EXPECTED_DATABASE,
    }
    if any(policy.get(key) != value for key, value in required.items()):
        raise ReleaseError("QA release policy target/database mismatch")
    if not isinstance(policy.get("database_user"), str) or not policy["database_user"]:
        raise ReleaseError("QA release policy database user is missing")
    if not isinstance(policy.get("redis_database"), int) or policy["redis_database"] <= 0:
        raise ReleaseError("QA release policy Redis database is invalid")
    if policy.get("redis_scheme") not in {"redis", "rediss"}:
        raise ReleaseError("QA release policy Redis scheme is invalid")
    if not isinstance(policy.get("redis_host"), str) or not policy["redis_host"]:
        raise ReleaseError("QA release policy Redis host is missing")
    if (
        not isinstance(policy.get("redis_port"), int)
        or not 1 <= policy["redis_port"] <= 65535
    ):
        raise ReleaseError("QA release policy Redis port is invalid")
    if not isinstance(policy.get("redis_username"), str):
        raise ReleaseError("QA release policy Redis username is invalid")
    if (
        not isinstance(policy.get("cache_prefix"), str)
        or "qa" not in policy["cache_prefix"].lower()
    ):
        raise ReleaseError("QA release policy cache prefix is invalid")
    if not isinstance(policy.get("firebase_project_id"), str) or not policy["firebase_project_id"]:
        raise ReleaseError("QA release policy Firebase project is missing")
    if not isinstance(policy.get("instance_id"), str) or len(policy["instance_id"]) < 16:
        raise ReleaseError("QA release policy instance id is missing")
    return policy


def run(
    command: list[str],
    *,
    cwd: Path = APP,
    check: bool = True,
    env: dict[str, str] | None = None,
    as_qa_user: bool = False,
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    effective_timeout = bounded_timeout(timeout_seconds)
    process_env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(APP),
    }
    if env:
        process_env.update(env)
    preexec_fn = None
    if as_qa_user:
        if pwd is None or grp is None:
            raise ReleaseError("QA subprocess requires POSIX identity support")
        qa_uid = pwd.getpwnam(APP_OS_USER).pw_uid
        qa_gid = grp.getgrnam(APP_OS_GROUP).gr_gid

        def drop_qa_privileges() -> None:
            os.setgroups([])
            os.setgid(qa_gid)
            os.setuid(qa_uid)
            os.umask(0o077)
            libc = ctypes.CDLL(None)
            if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
                os._exit(126)

        preexec_fn = drop_qa_privileges
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=process_env,
            preexec_fn=preexec_fn,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        executable = Path(command[0]).name if command else "subprocess"
        raise ReleaseError(
            f"QA command timed out after {effective_timeout:g}s: {executable}"
        ) from exc


def read_package() -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix="accounting-qa-release-", suffix=".tar.gz", delete=False
    )
    total = 0
    try:
        while True:
            chunk = sys.stdin.buffer.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PACKAGE_BYTES:
                raise ReleaseError("QA release package exceeds 200 MiB")
            handle.write(chunk)
    finally:
        handle.close()
    if total == 0:
        Path(handle.name).unlink(missing_ok=True)
        raise ReleaseError("empty QA release package")
    return Path(handle.name)


def validate_target(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in value:
        raise ReleaseError(f"unsafe QA target path: {value}")
    if any(part in DENIED_PARTS for part in path.parts):
        raise ReleaseError(f"runtime or secret QA target is forbidden: {value}")
    if path.suffix.lower() in DENIED_SUFFIXES:
        raise ReleaseError(f"secret or runtime QA file type is forbidden: {value}")
    resolved = (APP / Path(*path.parts)).resolve(strict=False)
    if APP.resolve(strict=False) not in resolved.parents:
        raise ReleaseError(f"QA target escapes application root: {value}")
    return path


def snapshot_sha256(payload: dict[str, tuple[bytes, int]]) -> str:
    canonical = bytearray()
    for target in sorted(payload):
        data, mode = payload[target]
        canonical.extend(target.encode("utf-8"))
        canonical.extend(b"\0")
        canonical.extend(str(mode).encode("ascii"))
        canonical.extend(b"\0")
        canonical.extend(str(len(data)).encode("ascii"))
        canonical.extend(b"\0")
        canonical.extend(digest(data).encode("ascii"))
        canonical.extend(b"\n")
    return digest(bytes(canonical))


def load_release(package: Path) -> tuple[dict[str, Any], dict[str, tuple[bytes, int]]]:
    try:
        archive = tarfile.open(package, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError("invalid QA release archive") from exc
    with archive:
        members = archive.getmembers()
        if any(not member.isfile() for member in members):
            raise ReleaseError("QA release archive may contain regular files only")
        if any(member.size < 0 or member.size > MAX_FILE_BYTES for member in members):
            raise ReleaseError("QA release archive contains an oversized file")
        if sum(member.size for member in members) > MAX_UNPACKED_BYTES:
            raise ReleaseError("QA release archive exceeds the unpacked size limit")
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ReleaseError("QA release archive contains duplicate members")
        if names.count("qa-release-manifest.json") != 1:
            raise ReleaseError("QA release manifest is missing or duplicated")
        source = archive.extractfile("qa-release-manifest.json")
        if source is None:
            raise ReleaseError("QA release manifest cannot be read")
        try:
            manifest = json.loads(source.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("QA release manifest is invalid") from exc
        if manifest.get("schema") != 2 or manifest.get("channel") != "excavator_qa":
            raise ReleaseError("unsupported QA release protocol")
        mode = manifest.get("mode")
        if mode not in MODES:
            raise ReleaseError("unsupported QA release mode")
        commit = manifest.get("commit")
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            raise ReleaseError("invalid QA release commit")
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("target") != "excavator_qa":
            raise ReleaseError("invalid QA release target")
        entries = manifest.get("files")
        if not isinstance(entries, list) or len(entries) > MAX_FILES:
            raise ReleaseError("invalid QA release file list")
        if mode in PAYLOAD_MODES:
            if metadata.get("snapshot") != "full_tracked_backend" or not entries:
                raise ReleaseError("QA code release requires a full tracked backend snapshot")
        elif entries or metadata.get("snapshot") != "none":
            raise ReleaseError(f"{mode} cannot contain QA code files")
        if mode == "qa_rollback":
            rollback_id = metadata.get("rollback_id")
            if not isinstance(rollback_id, str) or not ROLLBACK_RE.fullmatch(rollback_id):
                raise ReleaseError("invalid QA rollback id")
        elif "rollback_id" in metadata:
            raise ReleaseError("unexpected QA rollback id")
        if mode == "qa_deploy":
            if not isinstance(metadata.get("verification_id"), str) or not HASH_RE.fullmatch(
                metadata["verification_id"]
            ):
                raise ReleaseError("qa_deploy requires a valid verification id")
            if not isinstance(metadata.get("migration_plan_sha256"), str) or not HASH_RE.fullmatch(
                metadata["migration_plan_sha256"]
            ):
                raise ReleaseError("qa_deploy requires a verified migration plan")
            if metadata.get("allow_migrations") is not True:
                raise ReleaseError("qa_deploy requires explicit migration approval")
        elif any(
            key in metadata
            for key in ("verification_id", "migration_plan_sha256", "allow_migrations")
        ):
            raise ReleaseError("verification approval fields are valid only for qa_deploy")

        payload: dict[str, tuple[bytes, int]] = {}
        expected_names = {"qa-release-manifest.json"}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ReleaseError("invalid QA release entry")
            target = validate_target(str(entry.get("path", ""))).as_posix()
            if target in payload:
                raise ReleaseError(f"duplicate QA release target: {target}")
            archive_name = f"payload/{target}"
            expected_names.add(archive_name)
            extracted = archive.extractfile(archive_name)
            if extracted is None:
                raise ReleaseError(f"QA payload is missing: {target}")
            data = extracted.read()
            mode_bits = entry.get("mode")
            if mode_bits not in {0o644, 0o755}:
                raise ReleaseError(f"invalid QA file mode: {target}")
            if len(data) != entry.get("size") or digest(data) != entry.get("sha256"):
                raise ReleaseError(f"QA payload checksum mismatch: {target}")
            payload[target] = (data, mode_bits)
        if set(names) != expected_names:
            raise ReleaseError("QA release archive contains undeclared files")
        if mode in PAYLOAD_MODES and not REQUIRED_FILES.issubset(payload):
            raise ReleaseError("QA backend snapshot is incomplete")
        if mode in PAYLOAD_MODES:
            expected_snapshot = metadata.get("snapshot_sha256")
            if not isinstance(expected_snapshot, str) or expected_snapshot != snapshot_sha256(payload):
                raise ReleaseError("QA backend snapshot checksum mismatch")
        elif metadata.get("snapshot_sha256") != "":
            raise ReleaseError("payload-free QA mode cannot have a snapshot checksum")
        return manifest, payload


RUNTIME_SETTINGS_SCRIPT = r'''
import json
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.conf import settings
d = settings.DATABASES["default"]
c = settings.CACHES.get("default", {})
print(json.dumps({
    "database": {k: str(d.get(k) or "") for k in ("NAME", "USER", "PASSWORD", "HOST", "PORT")},
    "hosts": list(settings.ALLOWED_HOSTS),
    "origins": list(settings.CSRF_TRUSTED_ORIGINS),
    "aliases": dict(settings.ROLE_APP_HOST_ALIASES),
    "debug": bool(settings.DEBUG),
    "secret_is_default": settings.SECRET_KEY == "django-insecure-local-dev-key",
    "session_secure": bool(settings.SESSION_COOKIE_SECURE),
    "csrf_secure": bool(settings.CSRF_COOKIE_SECURE),
    "session_domain": settings.SESSION_COOKIE_DOMAIN,
    "csrf_domain": settings.CSRF_COOKIE_DOMAIN,
    "ssl_redirect": bool(settings.SECURE_SSL_REDIRECT),
    "proxy_header": list(settings.SECURE_PROXY_SSL_HEADER or []),
    "qa_enabled": bool(settings.EXCAVATOR_QA_ENABLED),
    "qa_database": str(settings.EXCAVATOR_QA_DATABASE_NAME or ""),
    "redis_location": str(c.get("LOCATION") or ""),
    "redis_backend": str(c.get("BACKEND") or ""),
    "redis_prefix": str(c.get("KEY_PREFIX") or ""),
    "redis_guard_db": str(settings.EXCAVATOR_QA_REDIS_DB or ""),
    "fcm_path": str(settings.FCM_SERVICE_ACCOUNT_FILE or ""),
    "fcm_project": str(settings.FCM_PROJECT_ID or ""),
    "fcm_guard_project": str(settings.EXCAVATOR_QA_FIREBASE_PROJECT_ID or ""),
    "static_root": str(settings.STATIC_ROOT),
    "media_root": str(settings.MEDIA_ROOT),
    "portal_private_media_root": str(settings.PORTAL_PRIVATE_MEDIA_ROOT),
    "rotations_private_media_root": str(settings.ROTATIONS_PRIVATE_MEDIA_ROOT),
}, ensure_ascii=True))
'''

MIGRATION_STATE_SCRIPT = r'''
import json
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
executor = MigrationExecutor(connection)
plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
applied = sorted([list(item) for item in executor.loader.applied_migrations])
print(json.dumps({
    "plan": [
        [migration.app_label, migration.name, bool(backwards)]
        for migration, backwards in plan
    ],
    "applied": applied,
}, sort_keys=True, separators=(",", ":")))
'''

DATABASE_SETTINGS_SCRIPT = r'''
import json
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.conf import settings
d = settings.DATABASES["default"]
print(json.dumps({
    k: str(d.get(k) or "") for k in ("NAME", "USER", "PASSWORD", "HOST", "PORT")
}, ensure_ascii=True))
'''


def runtime_settings(*, cwd: Path = APP) -> dict[str, Any]:
    python = APP / ".venv/bin/python"
    if not python.is_file():
        raise ReleaseError("QA virtual environment is missing")
    result = run(
        [str(python), "-c", RUNTIME_SETTINGS_SCRIPT],
        cwd=cwd,
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HOME": str(cwd),
        },
        as_qa_user=True,
        timeout_seconds=DJANGO_COMMAND_TIMEOUT_SECONDS,
    )
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ReleaseError("cannot load QA runtime settings") from exc


def validate_redis_boundary(
    location: str,
    guard_db: str,
    prefix: str,
    policy: dict[str, Any],
) -> None:
    redis_url = urlparse(location)
    redis_db = redis_url.path.lstrip("/")
    try:
        redis_port = redis_url.port
    except ValueError as exc:
        raise ReleaseError("QA Redis endpoint has an invalid port") from exc
    if (
        redis_url.scheme not in {"redis", "rediss"}
        or redis_url.params
        or redis_url.query
        or redis_url.fragment
        or not guard_db.isdigit()
        or int(guard_db) <= 0
        or redis_db != guard_db
        or int(guard_db) != policy["redis_database"]
        or prefix != policy["cache_prefix"]
        or redis_url.scheme != policy["redis_scheme"]
        or redis_url.hostname != policy["redis_host"]
        or redis_port != policy["redis_port"]
        or (redis_url.username or "") != policy["redis_username"]
    ):
        raise ReleaseError("QA Redis endpoint/database/prefix boundary is invalid")


def validate_runtime_boundaries(*, cwd: Path = APP) -> dict[str, Any]:
    assert_qa_boundaries()
    validate_backup_root()
    policy = load_policy()
    qa_gid = grp.getgrnam(APP_OS_GROUP).gr_gid if grp is not None else -1
    for protected_file in (APP / ".env", FCM_CONFIG_PATH):
        if not protected_file.is_file() or protected_file.is_symlink():
            raise ReleaseError(f"protected QA file is missing or unsafe: {protected_file}")
        stat = protected_file.stat()
        if (
            stat.st_uid != 0
            or stat.st_gid != qa_gid
            or stat.st_mode & 0o777 != 0o640
        ):
            raise ReleaseError(
                f"protected QA file must be root:accounting-qa mode 0640: {protected_file}"
            )
    values = runtime_settings(cwd=cwd)
    database = values.get("database", {})
    if database.get("NAME") != EXPECTED_DATABASE or values.get("qa_database") != EXPECTED_DATABASE:
        raise ReleaseError("QA database boundary does not match the dedicated database")
    if database.get("USER") != policy["database_user"] or not values.get("qa_enabled"):
        raise ReleaseError("QA database user or environment guard is missing")
    if set(values.get("hosts", [])) != set(EXPECTED_HOSTS):
        raise ReleaseError("QA allowed hosts do not match the isolated three-host contract")
    if set(values.get("origins", [])) != EXPECTED_ORIGINS:
        raise ReleaseError("QA CSRF origins do not match the isolated three-host contract")
    if values.get("aliases") != EXPECTED_HOSTS:
        raise ReleaseError("QA role host aliases do not match the isolated contract")
    if values.get("debug") or values.get("secret_is_default"):
        raise ReleaseError("public QA runtime has unsafe Django debug/secret settings")
    if not values.get("session_secure") or not values.get("csrf_secure") or not values.get("ssl_redirect"):
        raise ReleaseError("public QA runtime requires HTTPS-only cookies and redirect")
    if values.get("session_domain") is not None or values.get("csrf_domain") is not None:
        raise ReleaseError("QA session and CSRF cookies must remain host-only")
    if values.get("proxy_header") != ["HTTP_X_FORWARDED_PROTO", "https"]:
        raise ReleaseError("QA trusted HTTPS proxy header is not configured")
    if values.get("redis_backend") != "django.core.cache.backends.redis.RedisCache":
        raise ReleaseError("QA runtime requires the shared Django Redis backend")
    guard_db = str(values.get("redis_guard_db") or "")
    prefix = str(values.get("redis_prefix") or "")
    validate_redis_boundary(
        str(values.get("redis_location") or ""), guard_db, prefix, policy
    )
    if values.get("fcm_path") != str(FCM_CONFIG_PATH):
        raise ReleaseError("QA FCM credential path is outside the dedicated boundary")
    project = str(values.get("fcm_project") or "")
    if (
        not project
        or project != values.get("fcm_guard_project")
        or project != policy["firebase_project_id"]
    ):
        raise ReleaseError("QA Firebase project guard is missing or mismatched")
    try:
        credentials = json.loads(FCM_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("QA FCM service account is missing or invalid") from exc
    if credentials.get("type") != "service_account" or credentials.get("project_id") != project:
        raise ReleaseError("QA FCM service account does not match the guarded project")
    expected_paths = {
        "static_root": cwd / "staticfiles",
        "media_root": cwd / "media",
        "portal_private_media_root": cwd / "private_media",
        "rotations_private_media_root": cwd / "private_media" / "rotations",
    }
    for key, expected_path in expected_paths.items():
        if Path(str(values.get(key) or "")).resolve(strict=False) != expected_path.resolve(
            strict=False
        ):
            raise ReleaseError(f"QA runtime path escapes the selected application root: {key}")
    return values


def django_command(
    cwd: Path,
    *arguments: str,
    check: bool = True,
    read_only: bool = False,
    timeout_seconds: int = DJANGO_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command_env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HOME": str(cwd),
    }
    if read_only:
        command_env["PGOPTIONS"] = (
            "-c default_transaction_read_only=on "
            "-c statement_timeout=30000 -c lock_timeout=3000"
        )
    return run(
        [str(APP / ".venv/bin/python"), "manage.py", *arguments],
        cwd=cwd,
        check=check,
        env=command_env,
        as_qa_user=True,
        timeout_seconds=timeout_seconds,
    )


def migration_state(cwd: Path) -> dict[str, Any]:
    result = run(
        [str(APP / ".venv/bin/python"), "-c", MIGRATION_STATE_SCRIPT],
        cwd=cwd,
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HOME": str(cwd),
            "PGOPTIONS": (
                "-c default_transaction_read_only=on "
                "-c statement_timeout=30000 -c lock_timeout=3000"
            ),
        },
        as_qa_user=True,
        timeout_seconds=DJANGO_COMMAND_TIMEOUT_SECONDS,
    )
    try:
        raw = json.loads(result.stdout.strip().splitlines()[-1])
        plan = raw["plan"]
        applied = raw["applied"]
        if not isinstance(plan, list) or not isinstance(applied, list):
            raise ValueError
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseError("cannot calculate the QA migration state") from exc
    plan_data = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    applied_data = json.dumps(applied, sort_keys=True, separators=(",", ":")).encode()
    return {
        "migration_plan_sha256": digest(plan_data),
        "database_state_sha256": digest(applied_data),
        "pending_migrations": len(plan),
    }


def write_stage(payload: dict[str, tuple[bytes, int]], root: Path) -> None:
    for relative, (data, mode) in payload.items():
        target = root / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
    env_source = APP / ".env"
    if not env_source.is_file():
        raise ReleaseError("QA environment file is missing")
    shutil.copy2(env_source, root / ".env")
    (root / ".env").chmod(0o600)
    if pwd is None or grp is None:
        raise ReleaseError("QA stage requires POSIX identity support")
    uid = pwd.getpwnam(APP_OS_USER).pw_uid
    gid = grp.getgrnam(APP_OS_GROUP).gr_gid
    for directory, _, file_names in os.walk(root):
        directory_path = Path(directory)
        os.chown(directory_path, uid, gid)
        directory_path.chmod(0o700)
        for file_name in file_names:
            file_path = directory_path / file_name
            os.chown(file_path, uid, gid)
    os.chown(root, uid, gid)


def verify_staged_release(payload: dict[str, tuple[bytes, int]]) -> dict[str, Any]:
    current_requirements = APP / "requirements.txt"
    if (
        not current_requirements.is_file()
        or current_requirements.read_bytes() != payload["requirements.txt"][0]
    ):
        raise ReleaseError(
            "QA dependency changes require a separately provisioned virtual environment"
        )
    with tempfile.TemporaryDirectory(prefix="accounting-qa-stage-") as temporary:
        stage = Path(temporary)
        write_stage(payload, stage)
        validate_runtime_boundaries(cwd=stage)
        for arguments in (
            ("check",),
            ("makemigrations", "--check", "--dry-run"),
            ("migrate", "--plan"),
        ):
            result = django_command(stage, *arguments, read_only=True)
            print(result.stdout, end="")
        return migration_state(stage)


def write_state(target: Path, value: dict[str, Any]) -> None:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if STATE.is_symlink():
        raise ReleaseError("QA receiver state root cannot be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.parent.is_symlink() or target.is_symlink():
        raise ReleaseError("QA receiver state target cannot be a symlink")
    for directory in {STATE, target.parent}:
        os.chown(directory, 0, 0)
        directory.chmod(0o700)
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    os.replace(temporary, target)


def create_verification(
    manifest: dict[str, Any],
    payload: dict[str, tuple[bytes, int]],
    state: dict[str, Any],
) -> dict[str, Any]:
    policy = load_policy()
    receipt = {
        "schema": 1,
        "commit": manifest["commit"],
        "snapshot_sha256": manifest["metadata"]["snapshot_sha256"],
        "migration_plan_sha256": state["migration_plan_sha256"],
        "database_state_sha256": state["database_state_sha256"],
        "pending_migrations": state["pending_migrations"],
        "requirements_sha256": digest(payload["requirements.txt"][0]),
        "instance_id": policy["instance_id"],
        "created_at": int(time.time()),
    }
    receipt_id = digest(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    )
    receipt["verification_id"] = receipt_id
    write_state(VERIFICATIONS / f"{receipt_id}.json", receipt)
    return receipt


def validate_verification(
    manifest: dict[str, Any],
    payload: dict[str, tuple[bytes, int]],
    state: dict[str, Any],
) -> dict[str, Any]:
    policy = load_policy()
    verification_id = manifest["metadata"]["verification_id"]
    source = VERIFICATIONS / f"{verification_id}.json"
    if (
        VERIFICATIONS.is_symlink()
        or not VERIFICATIONS.is_dir()
        or (
            os.name == "posix"
            and (
                VERIFICATIONS.stat().st_uid != 0
                or VERIFICATIONS.stat().st_mode & 0o777 != 0o700
            )
        )
        or source.is_symlink()
        or not source.is_file()
        or (
            os.name == "posix"
            and (
                source.stat().st_uid != 0
                or source.stat().st_mode & 0o777 != 0o600
            )
        )
    ):
        raise ReleaseError("QA deploy has no server-side verification receipt")
    try:
        receipt = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("QA verification receipt is invalid") from exc
    expected = {
        "verification_id": verification_id,
        "commit": manifest["commit"],
        "snapshot_sha256": manifest["metadata"]["snapshot_sha256"],
        "migration_plan_sha256": state["migration_plan_sha256"],
        "database_state_sha256": state["database_state_sha256"],
        "pending_migrations": state["pending_migrations"],
        "requirements_sha256": digest(payload["requirements.txt"][0]),
        "instance_id": policy["instance_id"],
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ReleaseError("QA verification receipt no longer matches code or database state")
    created_at = receipt.get("created_at")
    if not isinstance(created_at, int) or time.time() - created_at > 2 * 60 * 60:
        raise ReleaseError("QA verification receipt has expired")
    if manifest["metadata"]["migration_plan_sha256"] != state["migration_plan_sha256"]:
        raise ReleaseError("approved QA migration plan does not match the current plan")
    return receipt


def file_owner() -> tuple[int, int]:
    if pwd is None or grp is None:
        raise ReleaseError("QA receiver requires POSIX ownership support")
    return 0, grp.getgrnam(APP_OS_GROUP).gr_gid


def write_atomic(target: Path, data: bytes, mode: int) -> None:
    uid, gid = file_owner()
    target.parent.mkdir(parents=True, exist_ok=True)
    parent = target.parent
    while parent == APP or APP in parent.parents:
        if parent.is_symlink():
            raise ReleaseError(f"QA code parent cannot be a symlink: {parent}")
        os.chown(parent, uid, gid)
        parent.chmod(0o751 if parent == APP else 0o750)
        if parent == APP:
            break
        parent = parent.parent
    temporary = target.with_name(f".{target.name}.qa-deploy-{os.getpid()}")
    temporary.write_bytes(data)
    os.chown(temporary, uid, gid)
    temporary.chmod(0o750 if mode & 0o111 else 0o640)
    os.replace(temporary, target)


def collect_managed_files(extra_paths: set[str]) -> dict[str, Path]:
    managed_directories = MANAGED_DIRECTORIES | {
        PurePosixPath(relative).parts[0]
        for relative in extra_paths
        if len(PurePosixPath(relative).parts) > 1
    }
    managed_root_files = MANAGED_ROOT_FILES | {
        relative for relative in extra_paths if len(PurePosixPath(relative).parts) == 1
    }
    if APP.is_dir():
        for child in APP.iterdir():
            if child.is_symlink():
                raise ReleaseError(f"QA application root cannot contain symlinks: {child.name}")
            if child.is_dir() and child.name not in DENIED_PARTS:
                managed_directories.add(child.name)
            elif (
                child.is_file()
                and child.name not in DENIED_PARTS
                and child.suffix.lower() not in DENIED_SUFFIXES
            ):
                managed_root_files.add(child.name)
    current: dict[str, Path] = {}
    for directory_name in managed_directories:
        directory = APP / directory_name
        if directory.is_symlink():
            raise ReleaseError(f"QA managed directory cannot be a symlink: {directory_name}")
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ReleaseError(f"QA managed path is not a directory: {directory_name}")
        for source in directory.rglob("*"):
            if source.is_symlink():
                raise ReleaseError(
                    f"QA managed tree cannot contain symlinks: {source.relative_to(APP)}"
                )
            if not source.is_file():
                continue
            relative_path = PurePosixPath(source.relative_to(APP).as_posix())
            if any(part in DENIED_PARTS for part in relative_path.parts):
                continue
            current[relative_path.as_posix()] = source
    for relative in managed_root_files:
        source = APP / relative
        if source.is_symlink():
            raise ReleaseError(f"QA managed root file cannot be a symlink: {relative}")
        if source.is_file():
            current[relative] = source
        elif source.exists():
            raise ReleaseError(f"QA managed root path is not a file: {relative}")
    return current


def validate_backup_root() -> None:
    if BACKUPS.is_symlink() or not BACKUPS.is_dir():
        raise ReleaseError("root-owned QA backup directory is missing or unsafe")
    backup_stat = BACKUPS.stat()
    if backup_stat.st_uid != 0 or stat.S_IMODE(backup_stat.st_mode) != 0o700:
        raise ReleaseError("QA backup directory must be root-owned mode 0700")
    floor = Path("/var")
    current = BACKUPS.parent
    while True:
        if current.is_symlink() or not current.is_dir():
            raise ReleaseError(f"QA backup parent is missing or unsafe: {current}")
        current_stat = current.stat()
        if current_stat.st_uid != 0 or stat.S_IMODE(current_stat.st_mode) & 0o022:
            raise ReleaseError(
                f"QA backup parent must be root-owned and not writable: {current}"
            )
        if current == floor:
            break
        if floor not in current.parents:
            raise ReleaseError("QA backup directory escapes the protected /var tree")
        current = current.parent


def new_backup(
    manifest: dict[str, Any], payload: dict[str, tuple[bytes, int]]
) -> Path:
    validate_backup_root()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup = BACKUPS / f"qa-github-{stamp}-{manifest['commit'][:12]}-before"
    backup.mkdir(mode=0o700)
    payload_paths = set(payload)
    current = collect_managed_files(payload_paths)
    existing: list[dict[str, Any]] = []
    for relative, source in sorted(current.items()):
        destination = backup / "files" / Path(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        data = source.read_bytes()
        existing.append({
            "path": relative,
            "mode": source.stat().st_mode & 0o777,
            "size": len(data),
            "sha256": digest(data),
        })
    created = sorted(payload_paths - set(current))
    stale = sorted(set(current) - payload_paths)
    (backup / "qa-release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (backup / "instance-id.txt").write_text(
        str(load_policy()["instance_id"]) + "\n", encoding="utf-8"
    )
    if CURRENT.is_file():
        shutil.copy2(CURRENT, backup / "previous-current.json")
    (backup / "existing.json").write_text(
        json.dumps(existing) + "\n", encoding="utf-8"
    )
    (backup / "created.json").write_text(
        json.dumps(created) + "\n", encoding="utf-8"
    )
    (backup / "stale.json").write_text(
        json.dumps(stale) + "\n", encoding="utf-8"
    )
    return backup


def database_settings() -> dict[str, str]:
    policy = load_policy()
    result = run(
        [str(APP / ".venv/bin/python"), "-c", DATABASE_SETTINGS_SCRIPT],
        cwd=APP,
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HOME": str(APP),
        },
        as_qa_user=True,
    )
    try:
        database = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ReleaseError("cannot load QA database settings") from exc
    if (
        database.get("NAME") != EXPECTED_DATABASE
        or database.get("USER") != policy["database_user"]
    ):
        raise ReleaseError("QA database settings do not match the root-owned policy")
    database["HOST"] = database.get("HOST") or "localhost"
    database["PORT"] = database.get("PORT") or "5432"
    role_check = run(
        [
            "psql",
            "--host", database["HOST"],
            "--port", database["PORT"],
            "--username", database["USER"],
            "--dbname", EXPECTED_DATABASE,
            "--tuples-only", "--no-align",
            "--command",
            (
                "SELECT current_database(), current_user, rolsuper, "
                "rolcreatedb, rolcreaterole, rolreplication "
                "FROM pg_roles WHERE rolname = current_user"
            ),
        ],
        env={"PGPASSWORD": database.get("PASSWORD", "")},
        timeout_seconds=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    fields = role_check.stdout.strip().split("|")
    if fields != [
        EXPECTED_DATABASE,
        policy["database_user"],
        "f", "f", "f", "f",
    ]:
        raise ReleaseError("QA database role is missing or over-privileged")
    return database


def pg_args(config: dict[str, str]) -> list[str]:
    return [
        "--host", config["HOST"],
        "--port", config["PORT"],
        "--username", config["USER"],
    ]


def backup_database(backup: Path) -> None:
    config = database_settings()
    target = backup / "database.dump"
    run(
        [
            "pg_dump", *pg_args(config), "--format=custom",
            "--file", str(target), EXPECTED_DATABASE,
        ],
        env={"PGPASSWORD": config.get("PASSWORD", "")},
        timeout_seconds=DATABASE_COMMAND_TIMEOUT_SECONDS,
    )
    if not target.is_file() or target.stat().st_size == 0:
        raise ReleaseError("QA PostgreSQL backup was not created")
    target.chmod(0o600)
    run(
        ["pg_restore", "--list", str(target)],
        timeout_seconds=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    (backup / "database.sha256").write_text(
        f"{digest(target.read_bytes())}  database.dump\n", encoding="utf-8"
    )


def restore_database(backup: Path) -> None:
    source = backup / "database.dump"
    hash_file = backup / "database.sha256"
    if not source.is_file() or not hash_file.is_file():
        raise ReleaseError("QA rollback point has no validated database dump")
    expected_hash = hash_file.read_text(encoding="utf-8").split()[0]
    if digest(source.read_bytes()) != expected_hash:
        raise ReleaseError("QA database backup checksum mismatch")
    config = database_settings()
    run(
        [
            "pg_restore", *pg_args(config), "--dbname", EXPECTED_DATABASE,
            "--clean", "--if-exists", "--no-owner", "--no-privileges",
            "--single-transaction", "--exit-on-error", str(source),
        ],
        env={"PGPASSWORD": config.get("PASSWORD", "")},
        timeout_seconds=DATABASE_COMMAND_TIMEOUT_SECONDS,
    )


def install_payload(payload: dict[str, tuple[bytes, int]]) -> None:
    for relative, (data, mode) in payload.items():
        target = APP / Path(*PurePosixPath(relative).parts)
        if target.is_symlink():
            raise ReleaseError(f"QA target cannot be a symlink: {relative}")
        write_atomic(target, data, mode)


def remove_stale_files(backup: Path) -> None:
    stale = json.loads((backup / "stale.json").read_text(encoding="utf-8"))
    for relative_value in stale:
        relative = validate_target(str(relative_value)).as_posix()
        target = APP / Path(*PurePosixPath(relative).parts)
        if target.is_symlink():
            raise ReleaseError(f"QA stale target cannot be a symlink: {relative}")
        if target.is_file():
            target.unlink()
    manifest = json.loads(
        (backup / "qa-release-manifest.json").read_text(encoding="utf-8")
    )
    managed_directories = MANAGED_DIRECTORIES | {
        PurePosixPath(str(entry["path"])).parts[0]
        for entry in manifest["files"]
        if len(PurePosixPath(str(entry["path"])).parts) > 1
    }
    for directory_name in managed_directories:
        directory = APP / directory_name
        if directory.is_dir() and not directory.is_symlink():
            for cache_dir in directory.rglob("__pycache__"):
                if cache_dir.is_dir() and not cache_dir.is_symlink():
                    shutil.rmtree(cache_dir)


def restore_files(backup: Path) -> None:
    existing = json.loads((backup / "existing.json").read_text(encoding="utf-8"))
    expected_paths = {validate_target(str(entry["path"])).as_posix() for entry in existing}
    extra_paths = set(expected_paths)
    backup_manifest = json.loads(
        (backup / "qa-release-manifest.json").read_text(encoding="utf-8")
    )
    extra_paths.update(
        validate_target(str(entry["path"])).as_posix()
        for entry in backup_manifest["files"]
    )
    if CURRENT.is_file():
        current_manifest = load_current_release()
        extra_paths.update(
            validate_target(str(entry["path"])).as_posix()
            for entry in current_manifest["files"]
        )
    current_files = collect_managed_files(extra_paths)
    for relative in sorted(set(current_files) - expected_paths):
        target = current_files[relative]
        if target.is_file() and not target.is_symlink():
            target.unlink()
    for entry in existing:
        relative = validate_target(str(entry["path"])).as_posix()
        saved = backup / "files" / Path(*PurePosixPath(relative).parts)
        saved_data = saved.read_bytes()
        if (
            len(saved_data) != entry.get("size")
            or digest(saved_data) != entry.get("sha256")
        ):
            raise ReleaseError(f"QA backup file checksum mismatch: {relative}")
        write_atomic(
            APP / Path(*PurePosixPath(relative).parts),
            saved_data,
            int(entry["mode"]),
        )


def set_current_release(manifest: dict[str, Any]) -> None:
    write_state(CURRENT, manifest)
    write_state(INITIALIZED, {"schema": 1, "initialized": True})


def load_current_release() -> dict[str, Any]:
    if (
        CURRENT.is_symlink()
        or not CURRENT.is_file()
        or (
            os.name == "posix"
            and (
                CURRENT.stat().st_uid != 0
                or CURRENT.stat().st_mode & 0o777 != 0o600
            )
        )
    ):
        raise ReleaseError("QA current release metadata is missing")
    try:
        manifest = json.loads(CURRENT.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("QA current release metadata is invalid") from exc
    if (
        manifest.get("schema") != 2
        or manifest.get("channel") != "excavator_qa"
        or not isinstance(manifest.get("commit"), str)
        or not COMMIT_RE.fullmatch(manifest["commit"])
        or not isinstance(manifest.get("files"), list)
    ):
        raise ReleaseError("QA current release metadata contract is invalid")
    return manifest


def current_release_for_audit() -> dict[str, Any] | None:
    if CURRENT.is_symlink() or INITIALIZED.is_symlink():
        raise ReleaseError("QA release state cannot be a symlink")
    if CURRENT.exists():
        if not CURRENT.is_file():
            raise ReleaseError("QA current release metadata path is invalid")
        return load_current_release()
    if INITIALIZED.exists():
        raise ReleaseError("QA current release metadata disappeared after initialization")
    return None


def verify_live_manifest(manifest: dict[str, Any]) -> None:
    entries = {
        validate_target(str(entry["path"])).as_posix(): entry
        for entry in manifest["files"]
    }
    current = collect_managed_files(set(entries))
    if set(current) != set(entries):
        raise ReleaseError("QA live code file set differs from current release metadata")
    for relative, entry in entries.items():
        source = current[relative]
        data = source.read_bytes()
        expected_mode = 0o750 if int(entry["mode"]) & 0o111 else 0o640
        if (
            len(data) != entry["size"]
            or digest(data) != entry["sha256"]
            or (
                os.name == "posix"
                and source.stat().st_mode & 0o777 != expected_mode
            )
        ):
            raise ReleaseError(f"QA live code drift detected: {relative}")


def verify_live_backup(backup: Path) -> None:
    existing = json.loads((backup / "existing.json").read_text(encoding="utf-8"))
    expected_entries = {
        validate_target(str(entry["path"])).as_posix(): entry for entry in existing
    }
    expected_paths = set(expected_entries)
    current = collect_managed_files(expected_paths)
    if set(current) != expected_paths:
        raise ReleaseError("restored QA file set differs from the rollback point")
    for relative, entry in expected_entries.items():
        data = current[relative].read_bytes()
        if len(data) != entry.get("size") or digest(data) != entry.get("sha256"):
            raise ReleaseError(f"restored QA file checksum mismatch: {relative}")


def restore_previous_current(backup: Path) -> None:
    source = backup / "previous-current.json"
    if source.is_file():
        try:
            manifest = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("QA previous release metadata is invalid") from exc
        set_current_release(manifest)
    else:
        CURRENT.unlink(missing_ok=True)


def service_is_active(name: str) -> bool:
    return run(
        ["systemctl", "is-active", name],
        check=False,
        timeout_seconds=SYSTEMD_COMMAND_TIMEOUT_SECONDS,
    ).returncode == 0


def validate_service_identity() -> None:
    for service in (SERVICE, SIMULATOR_SERVICE):
        user = run(
            ["systemctl", "show", "--property", "User", "--value", service],
            timeout_seconds=SYSTEMD_COMMAND_TIMEOUT_SECONDS,
        ).stdout.strip()
        group = run(
            ["systemctl", "show", "--property", "Group", "--value", service],
            timeout_seconds=SYSTEMD_COMMAND_TIMEOUT_SECONDS,
        ).stdout.strip()
        if user != APP_OS_USER or group != APP_OS_GROUP:
            raise ReleaseError(f"QA systemd identity is unsafe for {service}")


def stop_services() -> bool:
    simulator_was_active = service_is_active(SIMULATOR_SERVICE)
    if simulator_was_active:
        run(
            ["systemctl", "stop", SIMULATOR_SERVICE],
            timeout_seconds=SYSTEMD_COMMAND_TIMEOUT_SECONDS,
        )
    run(
        ["systemctl", "stop", SERVICE],
        timeout_seconds=SYSTEMD_COMMAND_TIMEOUT_SECONDS,
    )
    return simulator_was_active


def start_services(*, simulator: bool) -> None:
    run(
        ["systemctl", "start", SERVICE],
        timeout_seconds=SYSTEMD_COMMAND_TIMEOUT_SECONDS,
    )
    if simulator:
        run(
            ["systemctl", "start", SIMULATOR_SERVICE],
            timeout_seconds=SYSTEMD_COMMAND_TIMEOUT_SECONDS,
        )


def stop_services_best_effort() -> None:
    for service in (SERVICE, SIMULATOR_SERVICE):
        try:
            run(
                ["systemctl", "stop", service],
                check=False,
                timeout_seconds=SYSTEMD_COMMAND_TIMEOUT_SECONDS,
            )
        except ReleaseError:
            pass


def recover_failed_change(
    backup: Path | None,
    *,
    simulator_was_active: bool,
    current_manifest: dict[str, Any] | None = None,
) -> None:
    stop_services_best_effort()
    if backup is not None:
        restore_files(backup)
        restore_database(backup)
        verify_live_backup(backup)
        django_command(
            APP,
            "collectstatic",
            "--clear",
            "--noinput",
            check=False,
        )
        if current_manifest is not None:
            set_current_release(current_manifest)
    start_services(simulator=simulator_was_active)
    wait_for_service()


ROLE_PROBES = {
    "qa-admin.driverform.ru": ("/system-admin-sw.js", "admin"),
    "qa-driver.driverform.ru": ("/driver-sw.js", "driver"),
    "qa-excavator.driverform.ru": ("/excavator-sw.js", "excavator_operator"),
}


def role_probe(host: str, *, public: bool) -> bool:
    path, expected_role = ROLE_PROBES[host]
    command = [
        "curl", "--silent", "--show-error", "--head", "--max-time", "15",
    ]
    if public:
        command.append(f"https://{host}{path}")
    else:
        command.extend([
            "--unix-socket", str(SOCKET), "--header", f"Host: {host}",
            f"http://localhost{path}",
        ])
    response = run(
        command, check=False, timeout_seconds=CURL_COMMAND_TIMEOUT_SECONDS
    )
    if response.returncode != 0:
        return False
    lines = [line.strip() for line in response.stdout.splitlines() if line.strip()]
    status_ok = bool(lines and re.match(r"HTTP/\S+ [23][0-9]{2}\b", lines[0]))
    role_ok = any(
        line.lower() == f"x-app-role-code: {expected_role}" for line in lines[1:]
    )
    return status_ok and role_ok


def wait_for_service() -> None:
    for _ in range(45):
        if service_is_active(SERVICE) and SOCKET.exists():
            if all(role_probe(host, public=False) for host in ROLE_PROBES):
                return
        time.sleep(1)
    raise ReleaseError("QA application readiness check failed")


def verify_public_hosts() -> None:
    for host in sorted(ROLE_PROBES):
        if not role_probe(host, public=True):
            raise ReleaseError(f"QA public HTTPS readiness failed for {host}")


def audit_runtime(manifest: dict[str, Any] | None = None) -> str | None:
    validate_runtime_boundaries()
    validate_service_identity()
    selected_manifest = manifest
    if selected_manifest is None:
        selected_manifest = current_release_for_audit()
    if selected_manifest is not None:
        verify_live_manifest(selected_manifest)
    else:
        print("QA_AUDIT_UNTRACKED_RUNTIME=1")
    if not service_is_active(SERVICE):
        raise ReleaseError("QA application service is not active")
    wait_for_service()
    verify_public_hosts()
    result = django_command(APP, "check_excavator_qa_runtime")
    print(result.stdout, end="")
    return selected_manifest["commit"] if selected_manifest is not None else None


def audit_rollback_runtime() -> None:
    validate_service_identity()
    database_settings()
    if not service_is_active(SERVICE):
        raise ReleaseError("restored QA application service is not active")
    wait_for_service()
    verify_public_hosts()
    result = django_command(APP, "check")
    print(result.stdout, end="")


def finish_deploy() -> None:
    for arguments in (
        ("check",),
        ("makemigrations", "--check", "--dry-run"),
        ("migrate", "--plan"),
    ):
        result = django_command(APP, *arguments)
        print(result.stdout, end="")
    result = django_command(APP, "migrate", "--noinput")
    print(result.stdout, end="")
    result = django_command(APP, "prepare_excavator_qa")
    print(result.stdout, end="")
    result = django_command(APP, "collectstatic", "--clear", "--noinput")
    print(result.stdout, end="")


def deploy(
    manifest: dict[str, Any], payload: dict[str, tuple[bytes, int]]
) -> Path:
    set_command_deadline(MUTATING_PHASE_TIMEOUT_SECONDS)
    try:
        validate_service_identity()
        verification_state = verify_staged_release(payload)
        validate_verification(manifest, payload, verification_state)
        simulator_was_active = service_is_active(SIMULATOR_SERVICE)
    except Exception:
        set_command_deadline(None)
        raise
    backup: Path | None = None
    modified = False
    try:
        stop_services()
        stopped_state = verify_staged_release(payload)
        validate_verification(manifest, payload, stopped_state)
        backup = new_backup(manifest, payload)
        backup_database(backup)
        modified = True
        remove_stale_files(backup)
        install_payload(payload)
        finish_deploy()
        start_services(simulator=simulator_was_active)
        wait_for_service()
        audit_runtime(manifest)
        set_current_release(manifest)
    except Exception:
        set_command_deadline(RECOVERY_PHASE_TIMEOUT_SECONDS)
        recover_failed_change(
            backup if modified else None,
            simulator_was_active=simulator_was_active,
        )
        raise
    finally:
        set_command_deadline(None)
    if backup is None:
        raise ReleaseError("QA deploy did not create a rollback point")
    return backup


def rollback(manifest: dict[str, Any]) -> Path:
    set_command_deadline(MUTATING_PHASE_TIMEOUT_SECONDS)
    try:
        validate_backup_root()
        rollback_id = manifest["metadata"]["rollback_id"]
        backup = (BACKUPS / rollback_id).resolve(strict=False)
        if backup.parent != BACKUPS.resolve(strict=False) or not backup.is_dir():
            raise ReleaseError("QA rollback point does not exist")
        if backup.is_symlink():
            raise ReleaseError("QA rollback point cannot be a symlink")
        instance_file = backup / "instance-id.txt"
        if (
            not instance_file.is_file()
            or instance_file.read_text(encoding="utf-8").strip()
            != load_policy()["instance_id"]
        ):
            raise ReleaseError("QA rollback point belongs to another instance")
        current_manifest = load_current_release()
        current_payload = {
            str(entry["path"]): (b"", int(entry.get("mode", 0o644)))
            for entry in current_manifest["files"]
        }
        simulator_was_active = service_is_active(SIMULATOR_SERVICE)
    except Exception:
        set_command_deadline(None)
        raise
    rollback_guard: Path | None = None
    modified = False
    try:
        stop_services()
        rollback_guard = new_backup(current_manifest, current_payload)
        backup_database(rollback_guard)
        modified = True
        restore_files(backup)
        restore_database(backup)
        verify_live_backup(backup)
        result = django_command(APP, "collectstatic", "--clear", "--noinput")
        print(result.stdout, end="")
        start_services(simulator=simulator_was_active)
        wait_for_service()
        audit_rollback_runtime()
        restore_previous_current(backup)
        if CURRENT.is_file():
            verify_live_manifest(load_current_release())
    except Exception:
        set_command_deadline(RECOVERY_PHASE_TIMEOUT_SECONDS)
        recover_failed_change(
            rollback_guard if modified else None,
            simulator_was_active=simulator_was_active,
            current_manifest=current_manifest if modified else None,
        )
        raise
    finally:
        set_command_deadline(None)
    if rollback_guard is None:
        raise ReleaseError("QA rollback guard was not created")
    print(f"QA_ROLLBACK_GUARD={rollback_guard.name}")
    return backup


def main() -> int:
    if fcntl is None or grp is None or pwd is None:
        raise SystemExit("the QA receiver requires POSIX file locking")
    os.umask(0o077)
    assert_qa_boundaries()
    package = read_package()
    try:
        manifest, payload = load_release(package)
        mode = manifest["mode"]
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with LOCK.open("w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            package_hash = digest(package.read_bytes())
            if mode == "qa_audit":
                deployed_commit = audit_runtime() or "untracked"
                print(
                    f"QA_AUDIT_OK deployed_commit={deployed_commit} "
                    f"request_commit={manifest['commit']} "
                    f"package_sha256={package_hash}"
                )
                return 0
            if mode == "qa_verify":
                verification_state = verify_staged_release(payload)
                receipt = create_verification(
                    manifest, payload, verification_state
                )
                print(
                    f"QA_VERIFY_OK commit={manifest['commit']} files={len(payload)} "
                    f"package_sha256={package_hash}"
                )
                print(f"QA_VERIFICATION_ID={receipt['verification_id']}")
                print(
                    "QA_MIGRATION_PLAN_SHA256="
                    f"{receipt['migration_plan_sha256']}"
                )
                print(
                    f"QA_PENDING_MIGRATIONS={receipt['pending_migrations']}"
                )
                return 0
            if mode == "qa_deploy":
                backup = deploy(manifest, payload)
            elif mode == "qa_rollback":
                backup = rollback(manifest)
            else:  # load_release already validates modes
                raise ReleaseError(f"unsupported executable QA mode: {mode}")
            print(
                f"QA_RELEASE_OK mode={mode} commit={manifest['commit']} "
                f"files={len(payload)} backup={backup.name} "
                f"package_sha256={package_hash}"
            )
            return 0
    except Exception as exc:
        print(f"QA_RELEASE_FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        package.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
