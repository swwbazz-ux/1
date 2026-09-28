"""Verify a manifest against bytes stored in one published Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def git_bytes(root: Path, commit: str, path: str) -> tuple[str, bytes]:
    oid = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", f"{commit}:{path}"],
        text=True,
        encoding="utf-8",
    ).strip()
    content = subprocess.check_output(
        ["git", "-C", str(root), "cat-file", "blob", oid],
    )
    return oid, content


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs-root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--manifest-commit")
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    manifest_commit = args.manifest_commit or args.commit
    manifest_oid, manifest_bytes = git_bytes(
        args.docs_root, manifest_commit, args.manifest,
    )
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest["commit"] != args.commit:
        raise SystemExit(
            f"manifest targets {manifest['commit']}, expected {args.commit}"
        )
    results = []
    ok = True
    for expected in manifest["files"]:
        oid, content = git_bytes(args.docs_root, args.commit, expected["path"])
        actual = {
            "path": expected["path"],
            "blob": oid,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "crlf_count": content.count(b"\r\n"),
            "lf_count": content.count(b"\n"),
        }
        actual["matches"] = all(
            actual[key] == expected[key] for key in ("blob", "sha256", "size")
        )
        ok = ok and actual["matches"]
        results.append(actual)
    print(json.dumps({
        "commit": args.commit,
        "manifest_commit": manifest_commit,
        "manifest_path": args.manifest,
        "manifest_blob": manifest_oid,
        "checked": len(results),
        "ok": ok,
        "results": results,
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
