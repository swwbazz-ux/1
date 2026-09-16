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


APP = Path("/srv/accounting-mvp")
BACKUPS = APP / "backups" / "code"
LOCK = Path("/run/lock/accounting-github-deploy.lock")
MAX_PACKAGE_BYTES = 100 * 1024 * 1024
ALLOWED_TOP_LEVEL = {
    "assignments",
    "config",
    "core",
    "deploy",
    "downtimes",
    "portal",
    "references",
    "reports",
    "rotations",
    "settlement",
    "shifts",
    "static",
    "templates",
    "tools",
    "trips",
    "users",
}
ALLOWED_ROOT_FILES = {"manage.py", "requirements.txt"}


class ReleaseError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=APP,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def validate_target(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ReleaseError(f"unsafe target path: {value}")
    if "migrations" in path.parts:
        raise ReleaseError(f"database migration is not allowed by this channel: {value}")
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
                raise ReleaseError("release package exceeds 100 MiB")
            handle.write(chunk)
    finally:
        handle.close()
    if total == 0:
        Path(handle.name).unlink(missing_ok=True)
        raise ReleaseError("empty release package")
    return Path(handle.name)


def load_release(package: Path) -> tuple[dict, dict[str, bytes]]:
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
        if manifest.get("schema") != 1:
            raise ReleaseError("unsupported release schema")
        if manifest.get("mode") not in {"verify", "deploy"}:
            raise ReleaseError("unsupported release mode")
        commit = manifest.get("commit")
        if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise ReleaseError("invalid release commit")
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries or len(entries) > 500:
            raise ReleaseError("invalid release file list")
        payload: dict[str, bytes] = {}
        expected_names = {"release-manifest.json"}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ReleaseError("invalid release entry")
            path = validate_target(str(entry.get("path", "")))
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
        return manifest, payload


def write_atomic(target: Path, data: bytes, uid: int, gid: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chown(target.parent, uid, gid)
    os.chmod(target.parent, 0o755)
    temporary = target.with_name(f".{target.name}.github-deploy-{os.getpid()}")
    temporary.write_bytes(data)
    os.chown(temporary, uid, gid)
    os.chmod(temporary, 0o664)
    os.replace(temporary, target)


def wait_for_service() -> None:
    for _ in range(30):
        active = run(["systemctl", "is-active", "accounting-mvp"], check=False)
        if active.returncode == 0:
            response = run(
                [
                    "curl",
                    "--silent",
                    "--output",
                    "/dev/null",
                    "--write-out",
                    "%{http_code}",
                    "--unix-socket",
                    "/run/accounting-mvp/accounting-mvp.sock",
                    "http://localhost/",
                ],
                check=False,
            )
            code = response.stdout.strip()
            if code.startswith(("2", "3")) or code == "403":
                return
        time.sleep(1)
    raise ReleaseError("application readiness check failed")


def deploy(manifest: dict, payload: dict[str, bytes]) -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup = BACKUPS / f"github-{stamp}-{manifest['commit'][:12]}-before"
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
    (backup / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (backup / "existing.json").write_text(json.dumps(existing) + "\n", encoding="utf-8")
    (backup / "created.json").write_text(json.dumps(created) + "\n", encoding="utf-8")

    deploy_uid = pwd.getpwnam("deploy").pw_uid
    www_gid = grp.getgrnam("www-data").gr_gid
    try:
        for relative, data in payload.items():
            write_atomic(APP / Path(*PurePosixPath(relative).parts), data, deploy_uid, www_gid)
        check = run([str(APP / ".venv/bin/python"), "manage.py", "check"])
        print(check.stdout, end="")
        migrations = run(
            [str(APP / ".venv/bin/python"), "manage.py", "makemigrations", "--check", "--dry-run"]
        )
        print(migrations.stdout, end="")
        collectstatic = run([str(APP / ".venv/bin/python"), "manage.py", "collectstatic", "--noinput"])
        print(collectstatic.stdout, end="")
        nginx = run(["nginx", "-t"])
        print(nginx.stdout, end="")
        run(["systemctl", "restart", "accounting-mvp"])
        wait_for_service()
    except Exception:
        for relative in existing:
            saved = backup / "files" / Path(*PurePosixPath(relative).parts)
            target = APP / Path(*PurePosixPath(relative).parts)
            write_atomic(target, saved.read_bytes(), deploy_uid, www_gid)
        for relative in created:
            (APP / Path(*PurePosixPath(relative).parts)).unlink(missing_ok=True)
        run([str(APP / ".venv/bin/python"), "manage.py", "collectstatic", "--noinput"], check=False)
        run(["systemctl", "restart", "accounting-mvp"], check=False)
        raise
    return backup


def main() -> int:
    package = read_package()
    try:
        manifest, payload = load_release(package)
        with LOCK.open("w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            if manifest["mode"] == "verify":
                print(
                    f"VERIFY_OK commit={manifest['commit']} files={len(payload)} "
                    f"package_sha256={digest(package.read_bytes())}"
                )
                return 0
            backup = deploy(manifest, payload)
            print(
                f"DEPLOY_OK commit={manifest['commit']} files={len(payload)} backup={backup}"
            )
            return 0
    except Exception as exc:
        print(f"RELEASE_FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        package.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
