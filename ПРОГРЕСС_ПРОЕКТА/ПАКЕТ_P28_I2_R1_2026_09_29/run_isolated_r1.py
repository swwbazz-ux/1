"""Strict isolated replay for the disconnected P28-I2-R1 candidate.

The runner refuses a different or dirty candidate, removes inherited database
environment, forces Django's test connection to in-memory SQLite, and reuses
the three unchanged acceptance probes published with the I2 review.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile


EXPECTED_HEAD = "735f676ec2755bdd43f5c7c567d7ab253744037c"
EXPECTED_BASE = "5074ca6f047b4954a42676dc452cbc5749dde6ae"
EXPECTED_CORE_BLOB = "64b2519fb8bb0874012577fd2d779d7b9a619b74"
EXPECTED_PROBES = {
    "probe_adapter.py": "b3d2fc99bab29f0adf2c2a453e575380ffb4e2b91714b366bbd75d1bd7bb8389",
    "probe_load_origin.py": "28639a0049b10170d922fd9ef80cf511d0ada7a106e6828b2716978e0fc8eec6",
}
DB_ENV_NAMES = (
    "DATABASE_URL",
    "DJANGO_DB_ENGINE",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_CONN_MAX_AGE",
)
THREAD_ENV_NAMES = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def run_text(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(
        list(args), cwd=cwd, text=True, encoding="utf-8", errors="replace",
    ).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_state(path: Path) -> tuple[bool, int | None, str | None]:
    if not path.exists():
        return False, None, None
    return True, path.stat().st_size, sha256(path)


def fail(message: str) -> None:
    raise SystemExit("P28_I2_R1_REFUSED: " + message)


def preflight(candidate: Path, progress_root: Path) -> tuple[Path, Path, Path]:
    candidate = candidate.resolve()
    if not (candidate / ".git").exists():
        # A linked worktree has a .git file, while a normal checkout has a dir.
        if not (candidate / ".git").is_file():
            fail(f"not a Git worktree: {candidate}")
    head = run_text("git", "-C", str(candidate), "rev-parse", "HEAD")
    merge_base = run_text(
        "git", "-C", str(candidate), "merge-base", "HEAD", EXPECTED_BASE,
    )
    status = run_text(
        "git", "-C", str(candidate), "status", "--porcelain=v1",
        "--untracked-files=all",
    )
    if head != EXPECTED_HEAD:
        fail(f"unexpected candidate HEAD {head}; expected {EXPECTED_HEAD}")
    if merge_base != EXPECTED_BASE:
        fail(f"unexpected release base {merge_base}; expected {EXPECTED_BASE}")
    if status:
        fail("dirty candidate (staged, unstaged, or untracked):\n" + status)

    backend = candidate / "СИСТЕМА_MVP" / "backend"
    core_rel = "СИСТЕМА_MVP/backend/trips/route_projection_core.py"
    core_blob = run_text(
        "git", "-C", str(candidate), "rev-parse", f"HEAD:{core_rel}",
    )
    if core_blob != EXPECTED_CORE_BLOB:
        fail(f"R1 core blob changed: {core_blob}")
    for env_path in (candidate / ".env", backend / ".env"):
        if env_path.exists():
            fail(f"environment file is forbidden in isolated checkout: {env_path}")

    original_probes = (
        progress_root / "ДОКАЗАТЕЛЬСТВА_ПРИЕМКИ_P28_I2_2026_09_28"
    )
    for name, expected in EXPECTED_PROBES.items():
        actual = sha256(original_probes / name)
        if actual != expected:
            fail(f"original acceptance probe changed: {name}={actual}")

    r1_root = progress_root / "ПАКЕТ_P28_I1_R1_2026_09_28"
    print(f"CANDIDATE_HEAD={head}")
    print(f"RELEASE_BASE={merge_base}")
    print("SOURCE_CLEAN=True")
    print(f"R1_CORE_BLOB={core_blob}")
    print("ORIGINAL_PROBES_UNCHANGED=True")
    print("PREFLIGHT_OK=True", flush=True)
    return backend, original_probes, r1_root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    package_root = Path(__file__).resolve().parent
    progress_root = package_root.parent
    backend, original_probes, r1_root = preflight(
        args.candidate_root, progress_root,
    )
    if args.preflight_only:
        print("P28_I2_R1_PREFLIGHT_ONLY_OK")
        return 0

    inherited_db_names = tuple(name for name in DB_ENV_NAMES if name in os.environ)
    for name in DB_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ["DJANGO_DB_ENGINE"] = "sqlite"
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for name in THREAD_ENV_NAMES:
        os.environ[name] = "1"

    sys.path.insert(0, str(backend))
    sys.path.insert(0, str(original_probes))
    os.chdir(backend)
    persistent_db = backend / "db.sqlite3"
    persistent_before = file_state(persistent_db)

    from django.conf import settings

    settings.DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
            "TEST": {"NAME": ":memory:"},
        },
    }
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    }
    with tempfile.TemporaryDirectory(prefix="p28-i2-r1-media-") as media_root:
        settings.MEDIA_ROOT = media_root

        import django

        django.setup()
        from django.core.management import call_command
        from django.db import connection
        from django.test.utils import get_runner

        print(f"PYTHON={sys.executable}")
        print(f"DJANGO={django.get_version()}")
        print(f"DB_VENDOR={connection.vendor}")
        print(f"DB_NAME={settings.DATABASES['default']['NAME']}")
        print(f"DB_TEST_NAME={settings.DATABASES['default']['TEST']['NAME']}")
        print("EXTERNAL_DB_ALLOWED=False")
        print("INHERITED_DB_ENV_REMOVED=" + ",".join(inherited_db_names))
        if connection.vendor != "sqlite":
            fail(f"unexpected database vendor: {connection.vendor}")
        if settings.DATABASES["default"]["NAME"] != ":memory:":
            fail("persistent database name survived isolation")

        runner_type = get_runner(settings)
        groups = (
            (
                "P28-I2-R1 adapter suite (original 13 plus R1 regressions)",
                ("trips.test_route_projection_adapter",),
            ),
            (
                "Three unchanged Astra acceptance probes",
                (
                    "probe_adapter.AdapterAcceptanceProbes",
                    "probe_load_origin.LateLoadOriginProbe",
                ),
            ),
            (
                "Four existing route handler tests",
                (
                    "core.test_offline_sync.OfflineEventSyncTests."
                    "test_dump_point_a_to_b_to_a_keeps_distinct_ordered_events",
                    "core.test_offline_sync.OfflineEventSyncTests."
                    "test_dump_point_current_choice_is_accepted_without_business_change",
                    "core.test_offline_sync.OfflineEventSyncTests."
                    "test_dump_point_change_and_dependent_unload_complete_same_exact_trip",
                    "core.test_offline_sync.OfflineEventSyncTests."
                    "test_late_equal_timestamp_dump_point_change_cannot_roll_back_newer_state",
                ),
            ),
        )
        for label, test_labels in groups:
            print(f"=== {label} ===", flush=True)
            failures = runner_type(verbosity=2, interactive=False).run_tests(test_labels)
            print(f"GROUP_FAILURES={failures}", flush=True)
            if failures:
                return 1

        print("=== Django system check ===", flush=True)
        call_command("check", verbosity=1)
        print("=== Migration drift check ===", flush=True)
        call_command("makemigrations", check=True, dry_run=True, verbosity=1)

        print("=== Transferred R1 core tests ===", flush=True)
        core_result = subprocess.run(
            [
                sys.executable, "-m", "unittest", "discover",
                "-s", str(r1_root), "-p", "test_*.py", "-q",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        print(f"CORE_R1_EXIT={core_result.returncode}", flush=True)
        if core_result.returncode:
            return core_result.returncode

    persistent_after = file_state(persistent_db)
    print(f"PERSISTENT_DB_BEFORE={persistent_before}")
    print(f"PERSISTENT_DB_AFTER={persistent_after}")
    print(f"PERSISTENT_DB_TOUCHED={persistent_before != persistent_after}")
    if persistent_before != persistent_after:
        fail("persistent SQLite file changed")
    print("P28_I2_R1_REPLAY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
