from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tarfile


BACKEND_PREFIX = PurePosixPath("СИСТЕМА_MVP/backend")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--files", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--mode", choices=("verify", "deploy"), required=True)
    return parser.parse_args()


def load_paths(root: Path, list_path: Path) -> list[tuple[PurePosixPath, Path]]:
    result: list[tuple[PurePosixPath, Path]] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(list_path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        repository_path = PurePosixPath(value)
        if repository_path.is_absolute() or ".." in repository_path.parts:
            raise SystemExit(f"unsafe path at line {line_number}: {value}")
        try:
            target = repository_path.relative_to(BACKEND_PREFIX)
        except ValueError as exc:
            raise SystemExit(f"path is outside {BACKEND_PREFIX}: {value}") from exc
        target_text = target.as_posix()
        if not target_text or target_text in seen:
            raise SystemExit(f"duplicate or empty target: {value}")
        source = root.joinpath(*repository_path.parts)
        if not source.is_file():
            raise SystemExit(f"release file is missing: {value}")
        if "migrations" in target.parts:
            raise SystemExit(f"database migrations require a separately approved release: {value}")
        seen.add(target_text)
        result.append((target, source))
    if not result:
        raise SystemExit("release file list is empty")
    return sorted(result, key=lambda item: item[0].as_posix())


def add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    paths = load_paths(root, args.files.resolve())
    manifest_files = []
    payload: list[tuple[str, bytes]] = []
    for target, source in paths:
        data = source.read_bytes()
        target_text = target.as_posix()
        manifest_files.append(
            {"path": target_text, "sha256": sha256(data), "size": len(data)}
        )
        payload.append((f"payload/{target_text}", data))

    manifest = {
        "schema": 1,
        "mode": args.mode,
        "commit": args.commit,
        "files": manifest_files,
    }
    manifest_data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        add_bytes(archive, "release-manifest.json", manifest_data)
        for name, data in payload:
            add_bytes(archive, name, data)
    print(f"PACKAGE={args.output}")
    print(f"FILES={len(payload)}")
    print(f"SHA256={sha256(args.output.read_bytes())}")


if __name__ == "__main__":
    main()
