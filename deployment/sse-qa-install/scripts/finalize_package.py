from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path


EXCLUDED = {"PACKAGE_MANIFEST.csv", "evidence/package-self-check.log"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() not in EXCLUDED
    )
    with (root / "PACKAGE_MANIFEST.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=("path", "size", "sha256"))
        writer.writeheader()
        for path in files:
            writer.writerow({
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": digest(path),
            })
    print(f"PACKAGE_MANIFEST_OK files={len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

