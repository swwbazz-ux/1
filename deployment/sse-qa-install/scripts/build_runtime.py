from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


BASELINE = "f99c48cacdc718c303d6b30197701107d33e688c"
R3_SHA256 = "676FFC21E40AB62EF63D2C9223F97AEB005B2F256DF003718A28688376E84C1C"
BACKEND = PurePosixPath("СИСТЕМА_MVP/backend")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def safe_member(name: str) -> PurePosixPath:
    item = PurePosixPath(name.replace("\\", "/"))
    if item.is_absolute() or ".." in item.parts:
        raise ValueError(f"unsafe archive member: {name}")
    return item


def extract_git(repo: Path, target: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", BASELINE],
        check=True, capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        for member in source.getmembers():
            safe_member(member.name)
        source.extractall(target, filter="data")


def overlay_r3(review_zip: Path, target: Path) -> None:
    if digest(review_zip) != R3_SHA256:
        raise ValueError("R3 archive SHA-256 mismatch")
    with zipfile.ZipFile(review_zip) as source:
        for member in source.infolist():
            name = safe_member(member.filename)
            prefix = PurePosixPath("source-overlay")
            if not name.parts or name.parts[0] != prefix.name or member.is_dir():
                continue
            relative = PurePosixPath(*name.parts[1:])
            destination = target.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read(member))


def overlay_instrumentation(package: Path, target: Path) -> None:
    source_root = package / "app-overlay"
    backend = target.joinpath(*BACKEND.parts)
    for source in source_root.rglob("*"):
        relative = source.relative_to(source_root)
        if "__pycache__" in relative.parts or source.suffix in {".pyc", ".pyo"}:
            continue
        if source.is_file():
            destination = backend / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def build(repo: Path, package: Path, review_zip: Path, output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="sse-qa-runtime-") as raw:
        root = Path(raw)
        extract_git(repo, root)
        overlay_r3(review_zip, root)
        overlay_instrumentation(package, root)
        backend = root.joinpath(*BACKEND.parts)
        if any(path.is_symlink() for path in backend.rglob("*")):
            raise ValueError("runtime backend may not contain symlinks")

        def normalized(member: tarfile.TarInfo) -> tarfile.TarInfo:
            member.uid = 0
            member.gid = 0
            member.uname = "root"
            member.gname = "root"
            member.mtime = 0
            if member.isfile():
                member.mode = 0o644
            elif member.isdir():
                member.mode = 0o755
            return member

        with output.open("wb") as raw_output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    archive.add(backend, arcname="backend", recursive=True, filter=normalized)
    print(f"SSE_QA_RUNTIME_OK sha256={digest(output)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--r3-zip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.repo_root.resolve(), args.package_root.resolve(), args.r3_zip.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
