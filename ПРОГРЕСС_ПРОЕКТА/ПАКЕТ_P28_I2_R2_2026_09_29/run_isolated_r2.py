"""Strict isolated replay for the disconnected P28-I2-R2 candidate.

The source probes are loaded from immutable Git blobs.  The runner therefore
checks the bytes which were actually published and does not depend on checkout
LF/CRLF conversion.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile


EXPECTED_HEAD = "bdecc7eb9c528e40a4891201e1bae4682dc309e1"
EXPECTED_BASE = "5721c045d665f5811fc8d343d7374575f386af66"
EXPECTED_CORE_BLOB = "64b2519fb8bb0874012577fd2d779d7b9a619b74"
SOURCE_DOCS_COMMIT = "834539b201b68eb3ac165fefd26c6118eb53e899"
PROBE_SPECS = {
    "probe_adapter.py": {
        "path": (
            "ПРОГРЕСС_ПРОЕКТА/"
            "ДОКАЗАТЕЛЬСТВА_ПРИЕМКИ_P28_I2_2026_09_28/probe_adapter.py"
        ),
        "blob": "9b02c502c0667194d1b412c2985ee21f18326a0b",
        "sha256": "05573f69d16913516c0273ee461890e6f65aa9e8cfa97f89d37b2e7e0a1f9b5f",
    },
    "probe_load_origin.py": {
        "path": (
            "ПРОГРЕСС_ПРОЕКТА/"
            "ДОКАЗАТЕЛЬСТВА_ПРИЕМКИ_P28_I2_2026_09_28/probe_load_origin.py"
        ),
        "blob": "f1ff9609397ebb066b29c016f9535c8bf2ef2ae8",
        "sha256": "6302543320ada0674c2d7702572c4fd9cd929b1d31453908c0996440fe999a4a",
    },
    "probe_local_conflict.py": {
        "path": (
            "ПРОГРЕСС_ПРОЕКТА/"
            "ДОКАЗАТЕЛЬСТВА_ПРИЕМКИ_P28_I2_R1_2026_09_29/"
            "probe_local_conflict.py"
        ),
        "blob": "57001ac995e22b1a7907312331e8f74ac6ed24e1",
        "sha256": "caabf74cf3633bda47aa171d4b41497f29facd277523a73cc5190cef0d22fa81",
    },
}
CORE_TEST_FILES = (
    "route_core.py",
    "test_original_i1.py",
    "test_route_core_r1.py",
)
CORE_PACKAGE_PREFIX = "ПРОГРЕСС_ПРОЕКТА/ПАКЕТ_P28_I1_R1_2026_09_28"
ORIGINAL_CORE_TEST_PATH = (
    "ПРОГРЕСС_ПРОЕКТА/ПАКЕТ_P28_I1_2026_09_28/test_route_core.py"
)
EXPECTED_CANDIDATE_PATHS = (
    "СИСТЕМА_MVP/backend/trips/route_projection_adapter.py",
    "СИСТЕМА_MVP/backend/trips/route_projection_core.py",
    "СИСТЕМА_MVP/backend/trips/test_route_projection_adapter.py",
)
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


def run_bytes(*args: str, cwd: Path | None = None) -> bytes:
    return subprocess.check_output(list(args), cwd=cwd)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


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
    raise SystemExit("P28_I2_R2_REFUSED: " + message)


def git_blob(docs_root: Path, path: str) -> tuple[str, bytes]:
    oid = run_text(
        "git", "-C", str(docs_root), "rev-parse",
        f"{SOURCE_DOCS_COMMIT}:{path}",
    )
    content = run_bytes("git", "-C", str(docs_root), "cat-file", "blob", oid)
    return oid, content


def materialize_published_sources(
    docs_root: Path,
    probe_root: Path,
    core_root: Path,
    original_core_root: Path,
) -> None:
    for name, spec in PROBE_SPECS.items():
        oid, content = git_blob(docs_root, spec["path"])
        actual_sha256 = sha256_bytes(content)
        if oid != spec["blob"]:
            fail(f"published probe blob changed: {name}={oid}")
        if actual_sha256 != spec["sha256"]:
            fail(f"published probe bytes changed: {name}={actual_sha256}")
        (probe_root / name).write_bytes(content)
        print(
            "PUBLISHED_PROBE=" + name
            + " BLOB=" + oid
            + " SHA256=" + actual_sha256
            + f" CRLF={content.count(b'\r\n')} LF={content.count(b'\n')}",
            flush=True,
        )

    for name in CORE_TEST_FILES:
        path = f"{CORE_PACKAGE_PREFIX}/{name}"
        oid, content = git_blob(docs_root, path)
        (core_root / name).write_bytes(content)
        print(
            "PUBLISHED_CORE_TEST=" + name
            + " BLOB=" + oid
            + " SHA256=" + sha256_bytes(content),
            flush=True,
        )

    oid, content = git_blob(docs_root, ORIGINAL_CORE_TEST_PATH)
    (original_core_root / "test_route_core.py").write_bytes(content)
    print(
        "PUBLISHED_ORIGINAL_CORE_TEST=test_route_core.py"
        + " BLOB=" + oid
        + " SHA256=" + sha256_bytes(content),
        flush=True,
    )


def preflight(candidate: Path, docs_root: Path) -> Path:
    candidate = candidate.resolve()
    if not (candidate / ".git").exists():
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
    changed_paths = tuple(sorted(filter(None, run_text(
        "git", "-C", str(candidate), "diff", "--name-only",
        f"{EXPECTED_BASE}..HEAD",
    ).splitlines())))
    if core_blob != EXPECTED_CORE_BLOB:
        fail(f"R1 core blob changed: {core_blob}")
    if changed_paths != tuple(sorted(EXPECTED_CANDIDATE_PATHS)):
        fail(f"unexpected candidate paths: {changed_paths!r}")
    for env_path in (candidate / ".env", backend / ".env"):
        if env_path.exists():
            fail(f"environment file is forbidden in isolated checkout: {env_path}")
    if run_text("git", "-C", str(docs_root), "cat-file", "-t", SOURCE_DOCS_COMMIT) != "commit":
        fail(f"missing source docs commit: {SOURCE_DOCS_COMMIT}")

    print(f"CANDIDATE_HEAD={head}")
    print(f"RELEASE_BASE={merge_base}")
    print(f"SOURCE_DOCS_COMMIT={SOURCE_DOCS_COMMIT}")
    print("SOURCE_CLEAN=True")
    print(f"R1_CORE_BLOB={core_blob}")
    print("CANDIDATE_PATHS=" + ",".join(changed_paths))
    print("PREFLIGHT_OK=True", flush=True)
    return backend


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--docs-root", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    package_root = Path(__file__).resolve().parent
    docs_root = (args.docs_root or package_root.parents[1]).resolve()
    backend = preflight(args.candidate_root, docs_root)
    if args.preflight_only:
        print("P28_I2_R2_PREFLIGHT_ONLY_OK")
        return 0

    inherited_db_names = tuple(name for name in DB_ENV_NAMES if name in os.environ)
    for name in DB_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ["DJANGO_DB_ENGINE"] = "sqlite"
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for name in THREAD_ENV_NAMES:
        os.environ[name] = "1"

    persistent_db = backend / "db.sqlite3"
    persistent_before = file_state(persistent_db)
    with tempfile.TemporaryDirectory(prefix="p28-i2-r2-sources-") as source_dir:
        source_root = Path(source_dir)
        probe_root = source_root / "probes"
        core_root = source_root / "ПАКЕТ_P28_I1_R1_2026_09_28"
        original_core_root = source_root / "ПАКЕТ_P28_I1_2026_09_28"
        probe_root.mkdir()
        core_root.mkdir()
        original_core_root.mkdir()
        materialize_published_sources(
            docs_root, probe_root, core_root, original_core_root,
        )

        sys.path.insert(0, str(backend))
        sys.path.insert(0, str(probe_root))
        os.chdir(backend)
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
        with tempfile.TemporaryDirectory(prefix="p28-i2-r2-media-") as media_root:
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
                    "P28-I2-R2 adapter suite (R1 19 plus two scope guards)",
                    ("trips.test_route_projection_adapter",),
                ),
                (
                    "Three unchanged original Astra acceptance probes",
                    (
                        "probe_adapter.AdapterAcceptanceProbes",
                        "probe_load_origin.LateLoadOriginProbe",
                    ),
                ),
                (
                    "Unchanged combined C1/C2 Astra probe",
                    ("probe_local_conflict.LocalOriginalCollisionProbe",),
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
                    "-s", str(core_root), "-p", "test_*.py", "-q",
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
    print("P28_I2_R2_REPLAY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
