"""Build a byte manifest from immutable Git blobs, not checkout files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def text(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args],
        text=True,
        encoding="utf-8",
        errors="strict",
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--remote-ref")
    args = parser.parse_args()
    commit = text(args.repo, "rev-parse", args.commit)
    base = text(args.repo, "rev-parse", args.base)
    paths = sorted(filter(None, text(
        args.repo, "diff", "--name-only", f"{base}..{commit}",
    ).splitlines()))
    files = []
    for path in paths:
        oid = text(args.repo, "rev-parse", f"{commit}:{path}")
        content = subprocess.check_output(
            ["git", "-C", str(args.repo), "cat-file", "blob", oid],
        )
        files.append({
            "path": path,
            "blob": oid,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "crlf_count": content.count(b"\r\n"),
            "lf_count": content.count(b"\n"),
        })
    remote_oid = None
    if args.remote_ref:
        lines = subprocess.check_output(
            ["git", "-C", str(args.repo), "ls-remote", "origin", args.remote_ref],
            text=True,
            encoding="utf-8",
        ).splitlines()
        remote_oid = lines[0].split()[0] if lines else None
    print(json.dumps({
        "commit": commit,
        "base": base,
        "parents": text(args.repo, "show", "-s", "--format=%P", commit).split(),
        "remote_ref": args.remote_ref,
        "remote_oid": remote_oid,
        "remote_matches_commit": remote_oid == commit if args.remote_ref else None,
        "files": files,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
