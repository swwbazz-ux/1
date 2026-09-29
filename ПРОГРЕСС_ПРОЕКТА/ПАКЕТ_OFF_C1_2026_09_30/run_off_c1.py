#!/usr/bin/env python3
"""Reproducible OFF-C1 candidate checks without touching production."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


EXPECTED_BASE = "33ee7bb09d99c651d95e5187b1c2593f51ae9607"
EXPECTED_HEAD = "c05a59259c95e8e0e19ad316f7a1dc6eed3e45c3"


def run(name: str, argv: list[str], *, cwd: Path, logs: Path, env=None) -> dict:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    stdout_path = logs / f"{name}.stdout.log"
    stderr_path = logs / f"{name}.stderr.log"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    return {
        "name": name,
        "argv": argv,
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "stdout": stdout_path.name,
        "stderr": stderr_path.name,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--python", required=True, dest="python_exe", type=Path)
    parser.add_argument("--expected-sha", default=EXPECTED_HEAD)
    parser.add_argument("--rendered-shell", type=Path)
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--android", action="store_true")
    parser.add_argument("--full-node", action="store_true")
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "raw")
    args = parser.parse_args()

    product = args.product_root.resolve()
    backend = product / "СИСТЕМА_MVP" / "backend"
    mobile = product / "mobile" / "capacitor-shell"
    logs = args.output.resolve()
    logs.mkdir(parents=True, exist_ok=True)

    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=product, text=True, encoding="utf-8"
    ).strip()
    if head != args.expected_sha:
        raise SystemExit(f"Expected {args.expected_sha}, got {head}")
    if subprocess.run(["git", "merge-base", "--is-ancestor", EXPECTED_BASE, head], cwd=product).returncode:
        raise SystemExit(f"Base {EXPECTED_BASE} is not an ancestor of {head}")

    changed_files = subprocess.check_output(
        ["git", "diff", "--name-only", f"{EXPECTED_BASE}..{head}"],
        cwd=product,
        text=True,
        encoding="utf-8",
    ).splitlines()
    patch_bytes = subprocess.check_output(
        ["git", "diff", "--binary", f"{EXPECTED_BASE}..{head}"], cwd=product
    )
    (logs / "candidate.patch").write_bytes(patch_bytes)
    source_metadata = {
        "base_sha": EXPECTED_BASE,
        "head_sha": head,
        "branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=product, text=True, encoding="utf-8"
        ).strip(),
        "changed_files": changed_files,
        "changed_file_sha256": {
            name: sha256(product / name)
            for name in changed_files
            if (product / name).is_file()
        },
    }
    (logs / "source-metadata.json").write_text(
        json.dumps(source_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    environment = {
        "ANDROID_HOME": os.environ.get("ANDROID_HOME", ""),
        "ANDROID_SDK_ROOT": os.environ.get("ANDROID_SDK_ROOT", ""),
        "adb": shutil.which("adb"),
        "psql": shutil.which("psql"),
        "docker": shutil.which("docker"),
    }
    (logs / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    results: list[dict] = []
    diagnostics: list[dict] = []
    node_tests = [
        "static/js/tests/excavator-local-shift-v1.test.js",
        "static/js/tests/excavator-manual-pickup.test.js",
        "static/js/tests/trip-lost-response-runtime.test.js",
        "static/js/tests/excavator-hourly-report-contract.test.js",
        "static/js/tests/excavator-service-worker-update-runtime.test.js",
    ]
    node_env = os.environ.copy()
    if args.rendered_shell:
        rendered = args.rendered_shell.resolve()
        if not rendered.is_file():
            raise SystemExit(f"Rendered shell not found: {rendered}")
        node_env["EXCAVATOR_RENDERED_SHELL_PATH"] = str(rendered)
    results.append(run("node_off_c1", ["node", "--test", *node_tests], cwd=backend, logs=logs, env=node_env))

    django_tests = [
        "core.test_offline_sync",
        "core.test_free_bucket_sync",
        "core.test_offline_autonomous_shift",
        "trips.test_excavator_hourly_report",
        "trips.tests.ExcavatorWorkServerIntegrationTests",
    ]
    results.append(run(
        "django_off_c1",
        [str(args.python_exe), "manage.py", "test", *django_tests, "--verbosity", "1"],
        cwd=backend,
        logs=logs,
    ))
    results.append(run("django_check", [str(args.python_exe), "manage.py", "check"], cwd=backend, logs=logs))
    results.append(run(
        "django_migrations",
        [str(args.python_exe), "manage.py", "makemigrations", "--check", "--dry-run"],
        cwd=backend,
        logs=logs,
    ))
    npm_exe = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm_exe:
        raise SystemExit("npm executable was not found")
    results.append(run("mobile_node", [npm_exe, "test"], cwd=mobile, logs=logs))

    if args.full_node:
        diagnostics.append(run(
            "node_full_candidate",
            ["node", "--test", "static/js/tests/*.test.js"],
            cwd=backend,
            logs=logs,
        ))
    if args.baseline_root:
        baseline_backend = args.baseline_root.resolve() / "СИСТЕМА_MVP" / "backend"
        baseline_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=args.baseline_root.resolve(), text=True, encoding="utf-8"
        ).strip()
        if baseline_head != EXPECTED_BASE:
            raise SystemExit(f"Baseline root must be {EXPECTED_BASE}, got {baseline_head}")
        diagnostics.append(run(
            "node_baseline_driver_contract",
            ["node", "--test", "static/js/tests/driver-drum-readability-contract.test.js"],
            cwd=baseline_backend,
            logs=logs,
        ))

    if args.postgres:
        vendor = subprocess.check_output(
            [str(args.python_exe), "manage.py", "shell", "-c", "from django.db import connection; print(connection.vendor)"],
            cwd=backend,
            text=True,
            encoding="utf-8",
        )
        if vendor.strip().splitlines()[-1] != "postgresql":
            raise SystemExit(f"--postgres requested, actual vendor is {vendor!r}")
        results.append(run(
            "django_postgresql_concurrency",
            [
                str(args.python_exe), "manage.py", "test",
                "core.test_offline_autonomous_shift.AutonomousExcavatorShiftPostgreSQLTests",
                "--verbosity", "2",
            ],
            cwd=backend,
            logs=logs,
        ))
    else:
        (logs / "postgresql.NOT_RUN.txt").write_text(
            "NOT_RUN: use --postgres only with an isolated DJANGO_DB_ENGINE=postgres test database.\n",
            encoding="utf-8",
        )

    if args.android:
        results.append(run(
            "android_gradle",
            ["cmd", "/c", "gradlew.bat", "testExcavatorDebugUnitTest"],
            cwd=mobile / "android",
            logs=logs,
        ))
    else:
        (logs / "android.NOT_RUN.txt").write_text(
            "NOT_RUN: pass --android only when ANDROID_HOME/ANDROID_SDK_ROOT points to an installed SDK.\n",
            encoding="utf-8",
        )

    manifest = product / ".github" / "deploy" / "production-files.txt"
    manifest_lines = manifest.read_text(encoding="utf-8").splitlines()
    required_runtime = [
        "СИСТЕМА_MVP/backend/static/js/excavator-local-shift-v1.js",
        "СИСТЕМА_MVP/backend/static/js/excavator-hourly-report-v1.js",
        "СИСТЕМА_MVP/backend/trips/excavator_hourly_report.py",
    ]
    scope = {
        "duplicates": len(manifest_lines) - len(set(manifest_lines)),
        "migration_entries": [line for line in manifest_lines if "/migrations/" in line],
        "required_runtime_counts": {path: manifest_lines.count(path) for path in required_runtime},
    }
    scope_ok = (
        scope["duplicates"] == 0
        and not scope["migration_entries"]
        and all(count == 1 for count in scope["required_runtime_counts"].values())
    )
    (logs / "scope.json").write_text(json.dumps(scope, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [item for item in results if item["returncode"] != 0]
    summary = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "base_sha": EXPECTED_BASE,
        "head_sha": head,
        "product_root": str(product),
        "python": str(args.python_exe),
        "rendered_shell": str(args.rendered_shell.resolve()) if args.rendered_shell else None,
        "rendered_shell_sha256": sha256(args.rendered_shell.resolve()) if args.rendered_shell else None,
        "database": "postgresql" if args.postgres else "sqlite (PostgreSQL NOT_RUN)",
        "android": "requested" if args.android else "NOT_RUN",
        "scope_ok": scope_ok,
        "commands": results,
        "diagnostics_not_candidate_gates": diagnostics,
        "ok": not failed and scope_ok,
    }
    result_path = logs / "runner-result.json"
    result_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    hashes = {}
    for path in sorted(logs.iterdir(), key=lambda value: value.name):
        if path.is_file() and path.name != "sha256.json":
            hashes[path.name] = sha256(path)
    (logs / "sha256.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
