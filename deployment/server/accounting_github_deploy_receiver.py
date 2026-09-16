#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
from urllib.parse import urlparse


APP = Path("/srv/accounting-mvp")
BACKUPS = APP / "backups" / "code"
LOCK = Path("/run/lock/accounting-github-deploy.lock")
MAX_PACKAGE_BYTES = 150 * 1024 * 1024
CODE_MODES = {"verify", "deploy"}
MIGRATION_MODES = {"verify_migrations", "deploy_migrations"}
APK_MODES = {"verify_apk", "publish_apk"}
DATA_MODES = {"verify_data", "apply_data"}
ALL_MODES = CODE_MODES | MIGRATION_MODES | APK_MODES | DATA_MODES | {"rollback"}
VERIFY_MODES = {"verify", "verify_migrations", "verify_apk", "verify_data"}
ALLOWED_TOP_LEVEL = {
    "assignments", "config", "core", "deploy", "downtimes", "portal",
    "references", "reports", "rotations", "settlement", "shifts", "static",
    "templates", "tools", "trips", "users",
}
ALLOWED_ROOT_FILES = {"manage.py", "requirements.txt"}


class ReleaseError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(command: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        command, cwd=APP, check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=process_env,
    )


def validate_target(value: str, mode: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ReleaseError(f"unsafe target path: {value}")
    if mode in APK_MODES:
        if path.parts[:2] != ("media", "apk") or len(path.parts) != 3:
            raise ReleaseError(f"APK release target is not allowed: {value}")
        if path.suffix not in {".apk", ".json"}:
            raise ReleaseError(f"APK release file type is not allowed: {value}")
    elif mode in DATA_MODES:
        if path.parts[:2] != ("deploy", "data_updates"):
            raise ReleaseError(f"data update target is not allowed: {value}")
        if path.suffix not in {".py", ".json", ".csv", ".xlsx"}:
            raise ReleaseError(f"data update file type is not allowed: {value}")
    else:
        if "migrations" in path.parts and mode not in MIGRATION_MODES:
            raise ReleaseError(f"database migration is not allowed in {mode}: {value}")
        if len(path.parts) == 1:
            if value not in ALLOWED_ROOT_FILES:
                raise ReleaseError(f"root file is not allowlisted: {value}")
        elif path.parts[0] not in ALLOWED_TOP_LEVEL:
            raise ReleaseError(f"top-level directory is not allowlisted: {value}")
    resolved = (APP / Path(*path.parts)).resolve()
    if APP.resolve() not in resolved.parents:
        raise ReleaseError(f"target escapes application root: {value}")
    return path


def read_package() -> Path:
    handle = tempfile.NamedTemporaryFile(prefix="accounting-release-", suffix=".tar.gz", delete=False)
    total = 0
    try:
        while True:
            chunk = sys.stdin.buffer.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PACKAGE_BYTES:
                raise ReleaseError("release package exceeds 150 MiB")
            handle.write(chunk)
    finally:
        handle.close()
    if total == 0:
        Path(handle.name).unlink(missing_ok=True)
        raise ReleaseError("empty release package")
    return Path(handle.name)


def load_release(package: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    with tarfile.open(package, "r:gz") as archive:
        members = archive.getmembers()
        if any(not member.isfile() for member in members):
            raise ReleaseError("release archive may contain regular files only")
        names = [member.name for member in members]
        if names.count("release-manifest.json") != 1:
            raise ReleaseError("release manifest is missing or duplicated")
        manifest_file = archive.extractfile("release-manifest.json")
        if manifest_file is None:
            raise ReleaseError("release manifest cannot be read")
        manifest = json.loads(manifest_file.read().decode("utf-8"))
        if manifest.get("schema") != 2:
            raise ReleaseError("unsupported release schema")
        mode = manifest.get("mode")
        if mode not in ALL_MODES:
            raise ReleaseError("unsupported release mode")
        commit = manifest.get("commit")
        if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise ReleaseError("invalid release commit")
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            raise ReleaseError("invalid release metadata")
        entries = manifest.get("files")
        if not isinstance(entries, list) or len(entries) > 750:
            raise ReleaseError("invalid release file list")
        if mode != "rollback" and not entries:
            raise ReleaseError("release file list is empty")
        if mode == "rollback" and entries:
            raise ReleaseError("rollback package cannot contain files")
        payload: dict[str, bytes] = {}
        expected_names = {"release-manifest.json"}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ReleaseError("invalid release entry")
            path = validate_target(str(entry.get("path", "")), mode)
            target = path.as_posix()
            archive_name = f"payload/{target}"
            expected_names.add(archive_name)
            if target in payload:
                raise ReleaseError(f"duplicate release target: {target}")
            extracted = archive.extractfile(archive_name)
            if extracted is None:
                raise ReleaseError(f"payload is missing: {target}")
            data = extracted.read()
            if len(data) != entry.get("size") or digest(data) != entry.get("sha256"):
                raise ReleaseError(f"payload checksum mismatch: {target}")
            payload[target] = data
        if set(names) != expected_names:
            raise ReleaseError("release archive contains undeclared files")
        validate_mode_contract(manifest, payload)
        return manifest, payload


def validate_mode_contract(manifest: dict[str, Any], payload: dict[str, bytes]) -> None:
    mode = manifest["mode"]
    metadata = manifest["metadata"]
    if mode in APK_MODES:
        profile = metadata.get("apk_profile")
        if profile not in {"driver", "excavator"}:
            raise ReleaseError("invalid APK profile")
        expected_manifest = f"media/apk/{profile}-update.json"
        apk_files = [path for path in payload if path.endswith(".apk")]
        if expected_manifest not in payload or len(apk_files) != 1 or len(payload) != 2:
            raise ReleaseError("APK release must contain one APK and its role manifest")
        validate_apk_payload(profile, apk_files[0], payload)
    elif mode in DATA_MODES:
        operation = metadata.get("operation")
        if not isinstance(operation, str) or operation not in payload or not operation.endswith(".py"):
            raise ReleaseError("data operation script is missing from the package")
    elif mode == "rollback":
        rollback_id = metadata.get("rollback_id")
        if not isinstance(rollback_id, str) or not rollback_id.startswith("github-"):
            raise ReleaseError("invalid rollback id")


def validate_apk_payload(profile: str, apk_target: str, payload: dict[str, bytes]) -> dict[str, Any]:
    manifest_target = f"media/apk/{profile}-update.json"
    try:
        update = json.loads(payload[manifest_target].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("APK update manifest is invalid") from exc
    if update.get("schemaVersion") != 1 or update.get("profile") != profile:
        raise ReleaseError("APK update manifest contract mismatch")
    version_code = update.get("versionCode")
    version_name = update.get("versionName")
    if not isinstance(version_code, int) or version_code <= 0 or not isinstance(version_name, str):
        raise ReleaseError("APK version is invalid")
    parsed = urlparse(str(update.get("apkUrl", "")))
    expected_name = f"{profile}-{version_name}.apk"
    if parsed.scheme != "https" or parsed.netloc != "driverform.ru" or Path(parsed.path).name != expected_name:
        raise ReleaseError("APK URL does not match the production contract")
    if apk_target != f"media/apk/{expected_name}":
        raise ReleaseError("APK target does not match versionName")
    apk_data = payload[apk_target]
    if digest(apk_data) != update.get("sha256") or not apk_data.startswith(b"PK"):
        raise ReleaseError("APK content does not match update manifest")
    return update


def write_atomic(target: Path, data: bytes, uid: int, gid: int, mode: int = 0o664) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chown(target.parent, uid, gid)
    os.chmod(target.parent, 0o755)
    temporary = target.with_name(f".{target.name}.github-deploy-{os.getpid()}")
    temporary.write_bytes(data)
    os.chown(temporary, uid, gid)
    os.chmod(temporary, mode)
    os.replace(temporary, target)


def wait_for_service() -> None:
    for _ in range(45):
        active = run(["systemctl", "is-active", "accounting-mvp"], check=False)
        if active.returncode == 0:
            response = run([
                "curl", "--silent", "--output", "/dev/null", "--write-out", "%{http_code}",
                "--unix-socket", "/run/accounting-mvp/accounting-mvp.sock", "http://localhost/",
            ], check=False)
            code = response.stdout.strip()
            if code.startswith(("2", "3")) or code == "403":
                return
        time.sleep(1)
    raise ReleaseError("application readiness check failed")


def new_backup(manifest: dict[str, Any], payload: dict[str, bytes]) -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup = BACKUPS / f"github-{stamp}-{manifest['commit'][:12]}-{manifest['mode']}-before"
    backup.mkdir(mode=0o750)
    existing: list[str] = []
    created: list[str] = []
    for relative in payload:
        source = APP / Path(*PurePosixPath(relative).parts)
        if source.exists():
            destination = backup / "files" / Path(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            existing.append(relative)
        else:
            created.append(relative)
    (backup / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (backup / "existing.json").write_text(json.dumps(existing) + "\n", encoding="utf-8")
    (backup / "created.json").write_text(json.dumps(created) + "\n", encoding="utf-8")
    return backup


def database_settings() -> dict[str, str]:
    script = (
        "import json, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
        "import django; django.setup(); from django.conf import settings; "
        "d=settings.DATABASES['default']; "
        "print(json.dumps({k:str(d.get(k) or '') for k in ('NAME','USER','PASSWORD','HOST','PORT')}))"
    )
    result = run([str(APP / ".venv/bin/python"), "-c", script])
    try:
        config = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ReleaseError("cannot load production database settings") from exc
    if not config.get("NAME") or not config.get("USER"):
        raise ReleaseError("production database settings are incomplete")
    config["HOST"] = config.get("HOST") or "localhost"
    config["PORT"] = config.get("PORT") or "5432"
    return config


def pg_args(config: dict[str, str]) -> list[str]:
    return ["--host", config["HOST"], "--port", config["PORT"], "--username", config["USER"]]


def backup_database(backup: Path) -> Path:
    config = database_settings()
    target = backup / "database.dump"
    result = run(["pg_dump", *pg_args(config), "--format=custom", "--file", str(target), config["NAME"]], env={"PGPASSWORD": config.get("PASSWORD", "")})
    print(result.stdout, end="")
    if not target.is_file() or target.stat().st_size == 0:
        raise ReleaseError("PostgreSQL backup was not created")
    run(["pg_restore", "--list", str(target)])
    (backup / "database.sha256").write_text(f"{digest(target.read_bytes())}  database.dump\n")
    return target


def restore_database(backup: Path) -> None:
    source = backup / "database.dump"
    if not source.is_file():
        raise ReleaseError("rollback point has no database dump")
    config = database_settings()
    result = run([
        "pg_restore", *pg_args(config), "--dbname", config["NAME"], "--clean", "--if-exists",
        "--no-owner", "--no-privileges", "--exit-on-error", str(source),
    ], env={"PGPASSWORD": config.get("PASSWORD", "")})
    print(result.stdout, end="")


def file_owner() -> tuple[int, int]:
    return pwd.getpwnam("deploy").pw_uid, grp.getgrnam("www-data").gr_gid


def install_payload(payload: dict[str, bytes]) -> None:
    uid, gid = file_owner()
    for relative, data in payload.items():
        mode = 0o775 if relative.startswith("deploy/data_updates/") and relative.endswith(".py") else 0o664
        write_atomic(APP / Path(*PurePosixPath(relative).parts), data, uid, gid, mode)


def restore_files(backup: Path) -> None:
    uid, gid = file_owner()
    existing = json.loads((backup / "existing.json").read_text(encoding="utf-8"))
    created = json.loads((backup / "created.json").read_text(encoding="utf-8"))
    for relative in existing:
        saved = backup / "files" / Path(*PurePosixPath(relative).parts)
        write_atomic(APP / Path(*PurePosixPath(relative).parts), saved.read_bytes(), uid, gid)
    for relative in created:
        (APP / Path(*PurePosixPath(relative).parts)).unlink(missing_ok=True)


def finish_application_release(*, run_migrations: bool) -> None:
    for command in (
        [str(APP / ".venv/bin/python"), "manage.py", "check"],
        [str(APP / ".venv/bin/python"), "manage.py", "makemigrations", "--check", "--dry-run"],
        [str(APP / ".venv/bin/python"), "manage.py", "migrate", "--plan"],
    ):
        result = run(command)
        print(result.stdout, end="")
    if run_migrations:
        result = run([str(APP / ".venv/bin/python"), "manage.py", "migrate", "--noinput"])
        print(result.stdout, end="")
    result = run([str(APP / ".venv/bin/python"), "manage.py", "collectstatic", "--noinput"])
    print(result.stdout, end="")
    result = run(["nginx", "-t"])
    print(result.stdout, end="")
    run(["systemctl", "restart", "accounting-mvp"])
    wait_for_service()


def deploy_code(manifest: dict[str, Any], payload: dict[str, bytes], *, migrations: bool) -> Path:
    backup = new_backup(manifest, payload)
    if migrations:
        backup_database(backup)
        run(["systemctl", "stop", "accounting-mvp"])
    try:
        install_payload(payload)
        finish_application_release(run_migrations=migrations)
    except Exception:
        run(["systemctl", "stop", "accounting-mvp"], check=False)
        restore_files(backup)
        if migrations:
            restore_database(backup)
        run([str(APP / ".venv/bin/python"), "manage.py", "collectstatic", "--noinput"], check=False)
        run(["systemctl", "restart", "accounting-mvp"], check=False)
        raise
    return backup


def publish_apk(manifest: dict[str, Any], payload: dict[str, bytes]) -> Path:
    profile = manifest["metadata"]["apk_profile"]
    apk_target = next(path for path in payload if path.endswith(".apk"))
    update = validate_apk_payload(profile, apk_target, payload)
    current_path = APP / "media" / "apk" / f"{profile}-update.json"
    if current_path.is_file():
        current = json.loads(current_path.read_text(encoding="utf-8"))
        if int(update["versionCode"]) <= int(current.get("versionCode", 0)):
            raise ReleaseError("APK versionCode must be greater than the published version")
    apk_path = APP / Path(*PurePosixPath(apk_target).parts)
    if apk_path.exists() and digest(apk_path.read_bytes()) != update["sha256"]:
        raise ReleaseError("versioned APK path already exists with different content")
    backup = new_backup(manifest, payload)
    uid, gid = file_owner()
    write_atomic(apk_path, payload[apk_target], uid, gid)
    write_atomic(current_path, payload[f"media/apk/{profile}-update.json"], uid, gid)
    return backup


def apply_data(manifest: dict[str, Any], payload: dict[str, bytes]) -> Path:
    backup = new_backup(manifest, payload)
    install_payload(payload)
    operation = APP / Path(*PurePosixPath(manifest["metadata"]["operation"]).parts)
    dry_run = run([str(APP / ".venv/bin/python"), str(operation), "--dry-run"])
    print(dry_run.stdout, end="")
    run(["systemctl", "stop", "accounting-mvp"])
    backup_database(backup)
    try:
        applied = run([str(APP / ".venv/bin/python"), str(operation), "--apply"])
        print(applied.stdout, end="")
        check = run([str(APP / ".venv/bin/python"), "manage.py", "check"])
        print(check.stdout, end="")
        run(["systemctl", "start", "accounting-mvp"])
        wait_for_service()
    except Exception:
        restore_database(backup)
        restore_files(backup)
        run(["systemctl", "restart", "accounting-mvp"], check=False)
        raise
    return backup


def rollback(manifest: dict[str, Any]) -> Path:
    rollback_id = manifest["metadata"]["rollback_id"]
    backup = (BACKUPS / rollback_id).resolve()
    if backup.parent != BACKUPS.resolve() or not backup.is_dir():
        raise ReleaseError("rollback point does not exist")
    run(["systemctl", "stop", "accounting-mvp"])
    if (backup / "database.dump").is_file():
        restore_database(backup)
    restore_files(backup)
    run([str(APP / ".venv/bin/python"), "manage.py", "collectstatic", "--noinput"])
    run(["systemctl", "restart", "accounting-mvp"])
    wait_for_service()
    return backup


def main() -> int:
    package = read_package()
    try:
        manifest, payload = load_release(package)
        with LOCK.open("w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            mode = manifest["mode"]
            package_sha = digest(package.read_bytes())
            if mode in VERIFY_MODES:
                print(f"VERIFY_OK mode={mode} commit={manifest['commit']} files={len(payload)} package_sha256={package_sha}")
                return 0
            if mode == "deploy":
                backup = deploy_code(manifest, payload, migrations=False)
            elif mode == "deploy_migrations":
                backup = deploy_code(manifest, payload, migrations=True)
            elif mode == "publish_apk":
                backup = publish_apk(manifest, payload)
            elif mode == "apply_data":
                backup = apply_data(manifest, payload)
            elif mode == "rollback":
                backup = rollback(manifest)
            else:
                raise ReleaseError(f"unsupported executable mode: {mode}")
            print(f"RELEASE_OK mode={mode} commit={manifest['commit']} files={len(payload)} backup={backup}")
            return 0
    except Exception as exc:
        print(f"RELEASE_FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        package.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
