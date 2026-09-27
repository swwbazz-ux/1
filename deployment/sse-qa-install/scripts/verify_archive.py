from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def main() -> int:
    archive_path = Path(sys.argv[1]).resolve()
    with zipfile.ZipFile(archive_path) as archive:
        names: set[str] = set()
        folded: set[str] = set()
        for member in archive.infolist():
            path = PurePosixPath(member.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"unsafe ZIP entry: {member.filename}")
            if member.filename in names or member.filename.casefold() in folded:
                raise ValueError(f"duplicate/case collision: {member.filename}")
            names.add(member.filename)
            folded.add(member.filename.casefold())
            kind = (member.external_attr >> 16) & 0o170000
            if kind not in {0, 0o040000, 0o100000}:
                raise ValueError(f"link/device entry forbidden: {member.filename}")
        with tempfile.TemporaryDirectory(prefix="sse-qa-archive-check-") as raw:
            target = Path(raw)
            archive.extractall(target)
            roots = [item for item in target.iterdir() if item.is_dir()]
            if len(roots) != 1:
                raise ValueError("archive must have exactly one top-level directory")
            root = roots[0]
            subprocess.run(
                [sys.executable, str(root / "scripts/package_self_check.py"), str(root)],
                check=True,
            )
            for evidence in (root / "evidence").rglob("*"):
                if evidence.is_dir() and not any(evidence.iterdir()):
                    raise ValueError(f"empty evidence directory: {evidence.relative_to(root)}")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest().upper()
    print(f"ARCHIVE_VERIFY_OK sha256={digest} entries={len(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

