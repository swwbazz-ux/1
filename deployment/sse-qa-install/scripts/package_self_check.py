from __future__ import annotations

import csv
import hashlib
import os
import re
import sys
from pathlib import Path


FORBIDDEN_NAMES = {
    ".env", "google-services.json", "service-account.json",
}
FORBIDDEN_SUFFIXES = {".jks", ".keystore", ".p12", ".pfx", ".key", ".sqlite3", ".pyc"}
SECRET_PATTERN = re.compile(
    rb"(?i)(authorization:\s*bearer|sessionid=|private[_ -]?key-----|"
    rb"postgres_password\s*=\s*[^@\r\n]|redis_password\s*=\s*[^@\r\n])"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def secret_scan_payload(path: Path) -> bytes:
    scan_data = path.read_bytes() if path.stat().st_size <= 2 * 1024 * 1024 else b""
    if path.suffix.lower() == ".patch":
        # Removed lines are evidence, not package payload. Scan every added
        # line (and only added lines) so an old redacted/fake fixture cannot
        # make a review patch look like a shipped credential.
        scan_data = b"\n".join(
            line[1:] for line in scan_data.splitlines()
            if line.startswith(b"+") and not line.startswith(b"+++")
        )
    return scan_data


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    required = root / "REQUIRED_FILES.txt"
    manifest = root / "PACKAGE_MANIFEST.csv"
    if not required.is_file() or not manifest.is_file():
        raise SystemExit("missing required package indexes")

    failures: list[str] = []
    for raw in required.read_text(encoding="utf-8").splitlines():
        rel = raw.strip()
        if not rel or rel.startswith("#"):
            continue
        target = root / rel
        if not target.is_file() or target.stat().st_size <= 0:
            failures.append(f"required file missing/empty: {rel}")

    listed: dict[str, tuple[int, str]] = {}
    with manifest.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            listed[row["path"]] = (int(row["size"]), row["sha256"].upper())

    actual: set[str] = set()
    excluded = {"PACKAGE_MANIFEST.csv", "evidence/package-self-check.log"}
    for path in root.rglob("*"):
        if path.is_symlink():
            failures.append(f"symlink forbidden: {path.relative_to(root).as_posix()}")
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts:
            failures.append(f"cache artifact forbidden: {rel}")
        if rel in excluded:
            continue
        actual.add(rel)
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            failures.append(f"forbidden artifact: {rel}")
        size_hash = listed.get(rel)
        if size_hash is None:
            failures.append(f"unlisted file: {rel}")
        elif size_hash != (path.stat().st_size, sha256(path)):
            failures.append(f"manifest mismatch: {rel}")
        scan_data = secret_scan_payload(path)
        if rel != "scripts/package_self_check.py" and SECRET_PATTERN.search(scan_data):
            failures.append(f"possible secret material: {rel}")

    for rel in sorted(set(listed) - actual):
        failures.append(f"manifest entry missing: {rel}")

    if failures:
        print("PACKAGE_SELF_CHECK_FAIL")
        for item in failures:
            print(item)
        return 1
    print(f"PACKAGE_SELF_CHECK_OK files={len(actual)} root={root.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
