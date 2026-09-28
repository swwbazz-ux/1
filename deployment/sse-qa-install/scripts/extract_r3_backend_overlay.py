from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path, PurePosixPath


SOURCE_SHA256 = "D0F67C33F394D2F40E8E34400C37096B6A228E79D06685A0DA7C4F7C9862324A"
PREFIX = PurePosixPath("source-overlay/СИСТЕМА_MVP/backend")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> int:
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    if digest(source) != SOURCE_SHA256:
        raise SystemExit("unexpected R3 source archive SHA-256")
    count = 0
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as target:
        for member in sorted(archive.infolist(), key=lambda item: item.filename):
            name = PurePosixPath(member.filename.replace("\\", "/"))
            if member.is_dir() or name.is_absolute() or ".." in name.parts:
                continue
            try:
                name.relative_to(PREFIX)
            except ValueError:
                continue
            info = zipfile.ZipInfo(name.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            target.writestr(info, archive.read(member))
            count += 1
    if count < 40:
        raise SystemExit("derived R3 backend overlay is unexpectedly incomplete")
    print(f"R3_BACKEND_OVERLAY_OK files={count} sha256={digest(output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

