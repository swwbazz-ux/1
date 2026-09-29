#!/usr/bin/env python3
"""Reproduce the local S106-C2 evidence without touching production state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


EXPECTED_SHA = "f238aa51fbb80909d5e7f9e84e49b67f1ee5184e"


def run_bytes(name: str, command: list[str], cwd: Path, raw_dir: Path, env: dict[str, str]):
    started = time.monotonic()
    process = subprocess.run(command, cwd=cwd, env=env, capture_output=True, check=False)
    stdout_path = raw_dir / f"{name}.stdout.log"
    stderr_path = raw_dir / f"{name}.stderr.log"
    stdout_path.write_bytes(process.stdout)
    stderr_path.write_bytes(process.stderr)
    return {
        "name": name,
        "command": command,
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout_path.name,
        "stderr": stderr_path.name,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="Clean S106-C2 candidate worktree")
    parser.add_argument("--python", type=Path, required=True, help="Python executable with project dependencies")
    arguments = parser.parse_args()

    source = arguments.source.resolve()
    backend = source / "СИСТЕМА_MVP" / "backend"
    mobile = source / "mobile" / "capacitor-shell"
    raw_dir = Path(__file__).resolve().parent / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    actual_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True, encoding="utf-8"
    ).strip()
    if actual_sha != EXPECTED_SHA:
        raise SystemExit(f"wrong source SHA: expected {EXPECTED_SHA}, got {actual_sha}")
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=source, text=True, encoding="utf-8"
    )
    if status.strip():
        raise SystemExit("candidate worktree is not clean")

    python = str(arguments.python.resolve())
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise SystemExit("node/npm are required")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    checks: list[dict[str, object]] = []
    checks.append(run_bytes("django_check", [python, "manage.py", "check"], backend, raw_dir, env))
    checks.append(run_bytes(
        "django_migrations",
        [python, "manage.py", "makemigrations", "--check", "--dry-run"],
        backend,
        raw_dir,
        env,
    ))
    checks.append(run_bytes(
        "django_offline_free_bucket",
        [
            python,
            "manage.py",
            "test",
            "core.test_offline_sync.OfflineEventSyncTests",
            "core.test_free_bucket_sync.FreeBucketServerIntegrationTests",
            "--verbosity",
            "1",
            "--keepdb",
        ],
        backend,
        raw_dir,
        env,
    ))
    checks.append(run_bytes(
        "django_shell_versions",
        [python, "manage.py", "test", "users.tests.AccessLoginTests", "--verbosity", "1", "--keepdb"],
        backend,
        raw_dir,
        env,
    ))
    checks.append(run_bytes(
        "tools_contracts",
        [python, "-m", "unittest", "discover", "-s", "tools", "-p", "test*.py"],
        backend,
        raw_dir,
        env,
    ))
    node_tests = sorted((backend / "static" / "js" / "tests").glob("*.test.js"))
    checks.append(run_bytes(
        "backend_node",
        [node, "--test", *[str(path) for path in node_tests]],
        backend,
        raw_dir,
        env,
    ))
    checks.append(run_bytes("mobile_npm", [npm, "test"], mobile, raw_dir, env))

    if env.get("DJANGO_DB_ENGINE") == "postgres":
        checks.append(run_bytes(
            "postgres_vendor",
            [
                python,
                "manage.py",
                "shell",
                "-c",
                "from django.db import connection; print(connection.vendor)",
            ],
            backend,
            raw_dir,
            env,
        ))
        checks.append(run_bytes(
            "postgres_concurrency",
            [
                python,
                "manage.py",
                "test",
                "core.test_offline_sync.OfflineEventPostgreSQLConcurrencyTests",
                "core.test_free_bucket_sync.FreeBucketPostgreSQLConcurrencyTests",
                "--verbosity",
                "2",
            ],
            backend,
            raw_dir,
            env,
        ))
    else:
        not_run = raw_dir / "postgres_local.NOT_RUN.txt"
        not_run.write_bytes(
            b"NOT_RUN: DJANGO_DB_ENGINE=postgres and an isolated PostgreSQL test database were unavailable locally.\n"
        )

    metadata = {
        "expected_sha": EXPECTED_SHA,
        "actual_sha": actual_sha,
        "source": str(source),
        "python": python,
        "node": node,
        "npm": npm,
        "local_database_mode": env.get("DJANGO_DB_ENGINE", "sqlite"),
        "checks": checks,
    }
    result_path = raw_dir / "runner-result.json"
    result_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    hashes = {
        path.name: sha256(path)
        for path in sorted(raw_dir.iterdir())
        if path.is_file() and path.name != "sha256.json"
    }
    (raw_dir / "sha256.json").write_text(
        json.dumps(hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    failures = [item for item in checks if item["exit_code"] != 0]
    if failures:
        print(json.dumps(failures, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
