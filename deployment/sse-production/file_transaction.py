"""Durable file backup and compare-before-restore for SSE infrastructure."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import tempfile


@dataclass(frozen=True)
class FileImage:
    content: bytes
    mode: int
    uid: int
    gid: int

    def metadata(self) -> dict[str, int | str]:
        return {
            "sha256": sha256(self.content).hexdigest(),
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
        }


def read_image(path: Path) -> FileImage | None:
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(handle, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("managed target must be a regular file")
        content = stream.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("managed file exceeds backup limit")
        return FileImage(content, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)


def sync_directory(path: Path) -> None:
    handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def replace_image(path: Path, image: FileImage | None) -> None:
    if image is None:
        path.unlink(missing_ok=True)
    else:
        handle, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(image.content)
                stream.flush()
                os.fchown(stream.fileno(), image.uid, image.gid)
                os.fchmod(stream.fileno(), image.mode)
                os.fsync(stream.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    sync_directory(path.parent)


def save_backup(
    directory: Path,
    targets: dict[str, Path],
    before: dict[str, FileImage | None],
    planned: dict[str, FileImage | None],
) -> None:
    """Create a pre-change record below a root-owned backup parent."""
    if before.keys() != targets.keys() or planned.keys() != targets.keys():
        raise ValueError("backup and release must contain the same targets")
    if any(not name.isascii() or not name.replace("-", "").isalnum() for name in targets):
        raise ValueError("invalid managed file identifier")
    owner = os.geteuid()
    group = os.getegid()
    directory.mkdir(mode=0o700)
    manifest = {}
    for name in targets:
        previous = before[name]
        replacement = planned[name]
        if previous is not None:
            replace_image(directory / f"{name}.before", FileImage(previous.content, 0o600, owner, group))
        manifest[name] = {
            "before": previous.metadata() if previous is not None else None,
            "planned": replacement.metadata() if replacement is not None else None,
        }
    content = json.dumps(manifest, sort_keys=True).encode() + b"\n"
    replace_image(directory / "manifest.json", FileImage(content, 0o600, owner, group))
    sync_directory(directory.parent)


def restore_backup(directory: Path, targets: dict[str, Path]) -> None:
    """Restore only reviewed file images and reject concurrent external changes."""
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if manifest.keys() != targets.keys():
        raise ValueError("backup targets do not match the infrastructure release")
    restore = []
    for name, path in reversed(tuple(targets.items())):
        saved = manifest[name]
        current = read_image(path)
        current_metadata = current.metadata() if current is not None else None
        previous = saved["before"]
        if current_metadata == previous:
            continue
        if current_metadata != saved["planned"]:
            raise ValueError("managed file changed outside the SSE release")
        if previous is None:
            image = None
        else:
            data = (directory / f"{name}.before").read_bytes()
            image = FileImage(data, previous["mode"], previous["uid"], previous["gid"])
            if image.metadata() != previous:
                raise ValueError("backup bytes do not match the recorded fingerprint")
        restore.append((path, current, image))
    for path, expected, image in restore:
        if read_image(path) != expected:
            raise ValueError("managed file changed during SSE rollback")
        replace_image(path, image)


def apply_file_plan(
    directory: Path,
    targets: dict[str, Path],
    expected: dict[str, FileImage | None],
    planned: dict[str, FileImage | None],
) -> None:
    if {name: read_image(path) for name, path in targets.items()} != expected:
        raise ValueError("SSE infrastructure changed since verification")
    save_backup(directory, targets, expected, planned)
    try:
        for name, path in targets.items():
            if read_image(path) != expected[name]:
                raise ValueError("managed file changed during SSE rollout")
            replace_image(path, planned[name])
    except BaseException:
        restore_backup(directory, targets)
        raise
