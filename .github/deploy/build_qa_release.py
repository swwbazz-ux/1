from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile


BACKEND_TREE = "СИСТЕМА_MVP/backend"
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
    ".env",
    ".venv",
    "__pycache__",
    "media",
    "private_media",
    "staticfiles",
    "backups",
}
DENIED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx"}
MAX_UNPACKED_BYTES = 300 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an isolated Excavator QA release package.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    parser.add_argument("--rollback-id")
    parser.add_argument("--verification-id")
    parser.add_argument("--migration-plan-sha256")
    return parser.parse_args()


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def validate_target(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise SystemExit(f"unsafe QA snapshot path: {name}")
    if any(part in DENIED_PARTS for part in path.parts):
        raise SystemExit(f"runtime or secret path is forbidden in QA snapshot: {name}")
    if path.suffix.lower() in DENIED_SUFFIXES:
        raise SystemExit(f"secret or runtime file type is forbidden in QA snapshot: {name}")
    return path


def load_snapshot(root: Path, commit: str) -> list[tuple[str, bytes, int]]:
    archive_bytes = git(root, "archive", "--format=tar", f"{commit}:{BACKEND_TREE}")
    files: list[tuple[str, bytes, int]] = []
    seen: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise SystemExit(f"QA snapshot contains a non-regular file: {member.name}")
            target = validate_target(member.name).as_posix()
            if target in seen:
                raise SystemExit(f"duplicate QA snapshot path: {target}")
            source = archive.extractfile(member)
            if source is None:
                raise SystemExit(f"cannot read QA snapshot path: {target}")
            data = source.read()
            if len(data) > MAX_FILE_BYTES:
                raise SystemExit(f"QA snapshot file exceeds 64 MiB: {target}")
            mode = 0o755 if member.mode & 0o111 else 0o644
            files.append((target, data, mode))
            seen.add(target)
    missing = REQUIRED_FILES - seen
    if missing:
        raise SystemExit(f"QA snapshot is incomplete: {sorted(missing)}")
    if len(files) > 2000:
        raise SystemExit("QA snapshot contains too many files")
    if sum(len(data) for _, data, _ in files) > MAX_UNPACKED_BYTES:
        raise SystemExit("QA snapshot exceeds the unpacked size limit")
    return sorted(files)


def add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def snapshot_sha256(files: list[tuple[str, bytes, int]]) -> str:
    canonical = bytearray()
    for target, data, mode in files:
        canonical.extend(target.encode("utf-8"))
        canonical.extend(b"\0")
        canonical.extend(str(mode).encode("ascii"))
        canonical.extend(b"\0")
        canonical.extend(str(len(data)).encode("ascii"))
        canonical.extend(b"\0")
        canonical.extend(sha256(data).encode("ascii"))
        canonical.extend(b"\n")
    return sha256(bytes(canonical))


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    commit = args.commit.strip().lower()
    if not COMMIT_RE.fullmatch(commit):
        raise SystemExit("QA release commit must be a full lowercase SHA")
    resolved = git(root, "rev-parse", f"{commit}^{{commit}}").decode("ascii").strip()
    if resolved != commit:
        raise SystemExit("QA release commit does not resolve to the requested SHA")

    files = load_snapshot(root, commit) if args.mode in PAYLOAD_MODES else []
    snapshot_hash = snapshot_sha256(files) if files else ""
    metadata: dict[str, object] = {
        "target": "excavator_qa",
        "snapshot": "full_tracked_backend" if args.mode in PAYLOAD_MODES else "none",
        "snapshot_sha256": snapshot_hash,
    }
    if args.mode == "qa_rollback":
        rollback_id = str(args.rollback_id or "")
        if not ROLLBACK_RE.fullmatch(rollback_id):
            raise SystemExit("QA rollback requires an exact qa-github-...-before id")
        metadata["rollback_id"] = rollback_id
    elif args.rollback_id:
        raise SystemExit("--rollback-id is valid only for qa_rollback")
    if args.mode == "qa_deploy":
        verification_id = str(args.verification_id or "")
        migration_plan_sha256 = str(args.migration_plan_sha256 or "")
        if not HASH_RE.fullmatch(verification_id):
            raise SystemExit("qa_deploy requires the QA_VERIFICATION_ID from qa_verify")
        if not HASH_RE.fullmatch(migration_plan_sha256):
            raise SystemExit("qa_deploy requires the QA_MIGRATION_PLAN_SHA256 from qa_verify")
        metadata.update({
            "verification_id": verification_id,
            "migration_plan_sha256": migration_plan_sha256,
            "allow_migrations": True,
        })
    elif args.verification_id or args.migration_plan_sha256:
        raise SystemExit("verification fields are valid only for qa_deploy")

    manifest_files = [
        {"path": target, "sha256": sha256(data), "size": len(data), "mode": mode}
        for target, data, mode in files
    ]
    manifest = {
        "schema": 2,
        "channel": "excavator_qa",
        "mode": args.mode,
        "commit": commit,
        "files": manifest_files,
        "metadata": metadata,
    }
    manifest_data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                add_bytes(archive, "qa-release-manifest.json", manifest_data)
                for target, data, mode in files:
                    add_bytes(archive, f"payload/{target}", data, mode)

    package_hash = sha256(args.output.read_bytes())
    print(f"QA_PACKAGE={args.output}")
    print(f"QA_MODE={args.mode}")
    print(f"QA_FILES={len(files)}")
    if snapshot_hash:
        print(f"QA_SNAPSHOT_SHA256={snapshot_hash}")
    print(f"QA_SHA256={package_hash}")


if __name__ == "__main__":
    main()
