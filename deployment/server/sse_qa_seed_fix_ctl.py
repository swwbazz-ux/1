"""Fixed in-place repair for the accepted SSE-QA C2 seed defect.

The protected receiver invokes this controller with one fixed bundle.  The
public CLI deliberately accepts no paths other than the receiver-owned bundle
root and no database values.  Production paths and production services are
never consulted.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
from typing import Any


VERSION = "C2+seed-fix"
DISPLAY_VERSION = "C2 + seed-fix"
OVERLAY_SCHEMA = "SSE_QA_SEED_FIX_V1"
DB_RESULT_SCHEMA = "SSE_QA_SEED_FIX_DB_V1"

BASE_CONTROLLER_SHA256 = (
    "3e3ee8af9b2877bb93a7487f89a832834331a647d87f721180fe4b2ae8c2ea44"
)
OLD_SEED_SHA256 = (
    "b47facab26ef49977fee7c859307fcbf72b78e3b898bf1f2d11556ecefb320da"
)
OLD_TEST_SHA256 = (
    "0e374c80c6ce9aa5d4315036d07f6d5217ba504d462bcc2478d9020a67cbfa96"
)
NEW_SEED_SHA256 = (
    "0bf8580308073684e1e1ae4cce024620e36179f75c920feb18e5d6e16e98326c"
)
NEW_TEST_SHA256 = (
    "26288fad768c1ea00383d9cfed686d1f4ab6aa9ef1eaebaff99404a955531236"
)
DB_HELPER_SHA256 = (
    "2e25eabe333c99178caab70141711ba580abf0d6569c4693bc0afaa4c16f86da"
)

APP_ROOT = Path("/srv/sse-qa")
RELEASE_ROOT = APP_ROOT / "releases/r3"
BACKEND_ROOT = RELEASE_ROOT / "backend"
CURRENT_ROOT = APP_ROOT / "current"
STATE_ROOT = Path("/var/lib/sse-qa")
OWNERSHIP_PATH = STATE_ROOT / "OWNERSHIP.json"
INSTALLATION_MARKER = STATE_ROOT / "INSTALLATION_MARKER"
APP_ENV = Path("/etc/sse-qa/app.env")
NGINX_SITE = Path("/etc/nginx/sites-enabled/sse-qa.conf")
SEED_TARGET = BACKEND_ROOT / "users/management/commands/seed_sse_qa.py"
TEST_TARGET = BACKEND_ROOT / "users/test_sse_qa_seed.py"
SEED_LOGICAL = SEED_TARGET.as_posix()
TEST_LOGICAL = TEST_TARGET.as_posix()
SCOPED_UNIT = "sse-qa-seed-fix.service"
FAULT_MARKER = Path("/run/sse-qa-disposable-test")

EXPECTED_BUNDLE_FILES = {
    "scripts/sse_qa_ctl.py",
    "scripts/sse_qa_seed_fix_ctl.py",
    "scripts/sse_qa_seed_fix_db.py",
    "payload/seed_sse_qa.py",
    "payload/test_sse_qa_seed.py",
}


class SeedFixError(RuntimeError):
    pass


class SeedFixCancelled(SeedFixError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def rooted(root: Path, absolute: Path) -> Path:
    logical = PurePosixPath(str(absolute).replace("\\", "/"))
    if not logical.is_absolute():
        raise SeedFixError("managed path is not absolute")
    if root == Path("/"):
        return Path(logical.as_posix())
    return root.joinpath(*logical.parts[1:])


def load_base_controller(bundle_root: Path):
    source = bundle_root / "scripts/sse_qa_ctl.py"
    if source.is_symlink() or not source.is_file():
        raise SeedFixError("accepted base controller is missing")
    if sha256_file(source) != BASE_CONTROLLER_SHA256:
        raise SeedFixError("accepted base controller hash mismatch")
    spec = importlib.util.spec_from_file_location("sse_qa_seed_fix_base_ctl", source)
    if spec is None or spec.loader is None:
        raise SeedFixError("accepted base controller cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_bundle(bundle_root: Path) -> dict[str, Path]:
    try:
        resolved = bundle_root.resolve(strict=True)
    except OSError as exc:
        raise SeedFixError("seed-fix bundle is unavailable") from exc
    if not resolved.is_dir() or bundle_root.is_symlink():
        raise SeedFixError("seed-fix bundle root is invalid")
    own_path = Path(__file__).resolve()
    if own_path != resolved / "scripts/sse_qa_seed_fix_ctl.py":
        raise SeedFixError("seed-fix controller is outside its fixed bundle")
    actual: set[str] = set()
    for item in resolved.rglob("*"):
        if item.is_symlink():
            raise SeedFixError("seed-fix bundle links are forbidden")
        if item.is_file():
            actual.add(item.relative_to(resolved).as_posix())
    if actual != EXPECTED_BUNDLE_FILES:
        raise SeedFixError("seed-fix bundle member set mismatch")
    paths = {
        "base": resolved / "scripts/sse_qa_ctl.py",
        "controller": resolved / "scripts/sse_qa_seed_fix_ctl.py",
        "db_helper": resolved / "scripts/sse_qa_seed_fix_db.py",
        "seed": resolved / "payload/seed_sse_qa.py",
        "test": resolved / "payload/test_sse_qa_seed.py",
    }
    expected = {
        "base": BASE_CONTROLLER_SHA256,
        "db_helper": DB_HELPER_SHA256,
        "seed": NEW_SEED_SHA256,
        "test": NEW_TEST_SHA256,
    }
    for name, digest in expected.items():
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SeedFixError(f"seed-fix {name} pin is invalid")
        if sha256_file(paths[name]) != digest:
            raise SeedFixError(f"seed-fix {name} hash mismatch")
    return paths


def read_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes, os.stat_result]:
    if path.is_symlink() or not path.is_file():
        raise SeedFixError(f"{label} is missing or unsafe")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SeedFixError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise SeedFixError(f"{label} is not an object")
    return value, raw, path.stat(follow_symlinks=False)


def expected_overlay() -> dict[str, str]:
    return {
        "schema": OVERLAY_SCHEMA,
        "version": DISPLAY_VERSION,
        "controller_sha256": sha256_file(Path(__file__).resolve()),
        "db_helper_sha256": DB_HELPER_SHA256,
        "seed_sha256": NEW_SEED_SHA256,
        "test_sha256": NEW_TEST_SHA256,
    }


def source_state(root: Path, ownership: dict[str, Any]) -> str:
    files = ownership.get("files")
    if not isinstance(files, dict):
        raise SeedFixError("QA ownership file map is invalid")
    seed = rooted(root, SEED_TARGET)
    test = rooted(root, TEST_TARGET)
    if seed.is_symlink() or test.is_symlink() or not seed.is_file() or not test.is_file():
        raise SeedFixError("installed seed sources are missing or unsafe")
    actual_pair = (sha256_file(seed), sha256_file(test))
    journal_pair = (files.get(SEED_LOGICAL), files.get(TEST_LOGICAL))
    overlay = ownership.get("seed_fix")
    if actual_pair == (OLD_SEED_SHA256, OLD_TEST_SHA256):
        if journal_pair != actual_pair or overlay is not None:
            raise SeedFixError("legacy seed ownership state is inconsistent")
        return "old"
    if actual_pair == (NEW_SEED_SHA256, NEW_TEST_SHA256):
        if journal_pair != actual_pair or overlay != expected_overlay():
            raise SeedFixError("fixed seed ownership state is inconsistent")
        return "fixed"
    raise SeedFixError("installed seed sources have an unsupported or mixed state")


def validate_disabled_installation(root: Path, base: Any) -> tuple[dict[str, Any], bytes, os.stat_result, str]:
    marker = rooted(root, INSTALLATION_MARKER)
    if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8").strip() != base.MARKER:
        raise SeedFixError("complete QA installation marker is unavailable")
    ownership_path = rooted(root, OWNERSHIP_PATH)
    ownership, ownership_raw, ownership_stat = read_json_object(
        ownership_path, "QA ownership journal",
    )
    if (
        ownership.get("schema") != base.OWNERSHIP_SCHEMA
        or ownership.get("complete") is not True
        or ownership.get("phase") != "complete_disabled"
    ):
        raise SeedFixError("QA must be complete and disabled before seed repair")
    app_env = rooted(root, APP_ENV)
    if app_env.is_symlink() or not app_env.is_file():
        raise SeedFixError("QA environment is missing or unsafe")
    enabled = re.findall(
        r"(?m)^SSE_PILOT_ENABLED=(true|false)$",
        app_env.read_text(encoding="utf-8"),
    )
    if enabled != ["false"]:
        raise SeedFixError("QA kill switch must be disabled")
    site = rooted(root, NGINX_SITE)
    if site.exists() or site.is_symlink():
        raise SeedFixError("QA nginx site must be disabled")
    current = rooted(root, CURRENT_ROOT)
    release = rooted(root, RELEASE_ROOT)
    if not current.is_symlink() or current.resolve(strict=True) != release.resolve(strict=True):
        raise SeedFixError("QA current release target mismatch")
    return ownership, ownership_raw, ownership_stat, source_state(root, ownership)


def ensure_real_runtime_disabled(base: Any) -> None:
    if os.geteuid() != 0:
        raise SeedFixError("seed repair requires root receiver")
    base._assert_operation_scope(SCOPED_UNIT)
    if base._mount_status() != "match":
        raise SeedFixError("QA mount backing file mismatch")
    active = [
        unit for unit in base.SERVICE_UNITS
        if base._systemctl_property(unit, "ActiveState") != "inactive"
    ]
    if active:
        raise SeedFixError("QA services must be inactive before seed repair")


def preserve_write(base: Any, path: Path, data: bytes, previous: os.stat_result) -> None:
    base.secure_atomic_write(
        path,
        data,
        stat.S_IMODE(previous.st_mode),
        replace_existing=True,
        owner_uid=previous.st_uid,
        owner_gid=previous.st_gid,
    )


def render_ownership(ownership: dict[str, Any]) -> bytes:
    return (json.dumps(ownership, sort_keys=True) + "\n").encode("utf-8")


def fault_injection(point: str) -> None:
    requested = os.getenv("SSE_QA_SEED_FIX_FAULT_AT", "")
    if not requested:
        return
    if (
        os.getenv("SSE_QA_SEED_FIX_FAULT_INJECTION") != "1"
        or not FAULT_MARKER.is_file()
    ):
        raise SeedFixError("seed-fix fault injection is forbidden")
    allowed = {"after_seed_file", "after_test_file", "after_journal", "after_database"}
    if requested not in allowed:
        raise SeedFixError("unsupported seed-fix fault point")
    if requested == point:
        raise SeedFixError(f"injected seed-fix fault: {point}")


def snapshot_sources_and_journal(
    root: Path,
) -> tuple[dict[str, tuple[bytes, os.stat_result]], bytes]:
    targets = {"seed": rooted(root, SEED_TARGET), "test": rooted(root, TEST_TARGET)}
    backups: dict[str, tuple[bytes, os.stat_result]] = {}
    for name, target in targets.items():
        if target.is_symlink() or not target.is_file():
            raise SeedFixError(f"installed {name} source is unsafe")
        backups[name] = (target.read_bytes(), target.stat(follow_symlinks=False))
    ownership_path = rooted(root, OWNERSHIP_PATH)
    if ownership_path.is_symlink() or not ownership_path.is_file():
        raise SeedFixError("QA ownership journal is unsafe")
    return backups, ownership_path.read_bytes()


def update_sources_and_journal(
    root: Path,
    base: Any,
    ownership: dict[str, Any],
    ownership_stat: os.stat_result,
    paths: dict[str, Path],
    backups: dict[str, tuple[bytes, os.stat_result]],
    ownership_before: bytes,
) -> None:
    targets = {"seed": rooted(root, SEED_TARGET), "test": rooted(root, TEST_TARGET)}
    try:
        preserve_write(base, targets["seed"], paths["seed"].read_bytes(), backups["seed"][1])
        fault_injection("after_seed_file")
        preserve_write(base, targets["test"], paths["test"].read_bytes(), backups["test"][1])
        fault_injection("after_test_file")
        files = ownership["files"]
        assert isinstance(files, dict)
        files[SEED_LOGICAL] = NEW_SEED_SHA256
        files[TEST_LOGICAL] = NEW_TEST_SHA256
        ownership["seed_fix"] = expected_overlay()
        preserve_write(
            base,
            rooted(root, OWNERSHIP_PATH),
            render_ownership(ownership),
            ownership_stat,
        )
        fault_injection("after_journal")
    except BaseException as primary:
        try:
            restore_sources_and_journal(
                root, base, backups, ownership_before, ownership_stat,
            )
        except Exception as rollback:
            raise SeedFixError("seed-fix staging rollback incomplete") from primary
        raise


def restore_sources_and_journal(
    root: Path,
    base: Any,
    backups: dict[str, tuple[bytes, os.stat_result]],
    ownership_raw: bytes,
    ownership_stat: os.stat_result,
) -> None:
    errors: list[str] = []
    for name, logical in (("seed", SEED_TARGET), ("test", TEST_TARGET)):
        try:
            data, previous = backups[name]
            preserve_write(base, rooted(root, logical), data, previous)
        except Exception as exc:  # pragma: no cover - aggregated safety path
            errors.append(f"{name}:{type(exc).__name__}")
    try:
        preserve_write(
            base,
            rooted(root, OWNERSHIP_PATH),
            ownership_raw,
            ownership_stat,
        )
    except Exception as exc:  # pragma: no cover - aggregated safety path
        errors.append(f"ownership:{type(exc).__name__}")
    if errors:
        raise SeedFixError("seed-fix source rollback incomplete: " + ",".join(errors))


def parse_db_result(completed: subprocess.CompletedProcess[str], allowed: set[str]) -> dict[str, Any]:
    if completed.returncode != 0:
        raise SeedFixError("seed-fix database helper failed")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SeedFixError("seed-fix database helper returned an invalid response")
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise SeedFixError("seed-fix database helper response is not JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != DB_RESULT_SCHEMA:
        raise SeedFixError("seed-fix database helper schema mismatch")
    action = value.get("action")
    if action not in allowed:
        raise SeedFixError("seed-fix database helper action mismatch")
    expected_keys = {
        "schema", "action", "legacy_id", "canonical_id", "placement_id",
        "protected_digest",
    }
    if set(value) != expected_keys:
        raise SeedFixError("seed-fix database helper response shape mismatch")
    for key in ("legacy_id", "placement_id"):
        if not isinstance(value.get(key), int) or value[key] <= 0:
            raise SeedFixError("seed-fix database helper identifier is invalid")
    canonical_id = value.get("canonical_id")
    if action == "old":
        if canonical_id is not None:
            raise SeedFixError("legacy database state unexpectedly has canonical rock")
    elif not isinstance(canonical_id, int) or canonical_id <= 0:
        raise SeedFixError("seed-fix canonical identifier is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("protected_digest", ""))):
        raise SeedFixError("seed-fix protected digest is invalid")
    return value


def run_db_helper(base: Any, helper: Path, action: str) -> dict[str, Any]:
    env = base.application_environment(include_pins=True)
    completed = base.run(
        [str(APP_ROOT / "venv/bin/python"), str(helper), action],
        cwd=APP_ROOT / "current/backend",
        env=env,
        timeout=180,
        check=False,
        step=f"seed-fix-db-{action}",
    )
    allowed = {
        "inspect": {"old", "fixed"},
        "apply": {"applied", "already_applied"},
        "rollback": {"rolled_back"},
    }[action]
    return parse_db_result(completed, allowed)


def assert_protected_state_unchanged(
    before: dict[str, Any], after: dict[str, Any], *, compare_canonical: bool = False,
) -> None:
    keys = ["legacy_id", "placement_id", "protected_digest"]
    if compare_canonical:
        keys.append("canonical_id")
    if any(before.get(key) != after.get(key) for key in keys):
        raise SeedFixError("seed-fix database protected state changed")


def start_postgres(base: Any) -> None:
    base.run(["systemctl", "start", "postgresql@16-sseqa.service"], step="seed-fix-postgres-start")
    base._assert_unit_in_qa_slice("postgresql@16-sseqa.service")


def stop_postgres(base: Any) -> None:
    stopped = base.run(
        ["systemctl", "stop", "postgresql@16-sseqa.service"],
        check=False,
        timeout=90,
        step="seed-fix-postgres-stop",
    )
    if stopped.returncode != 0 or base._systemctl_property(
        "postgresql@16-sseqa.service", "ActiveState",
    ) != "inactive":
        raise SeedFixError("QA PostgreSQL stop was not confirmed")


def stop_verify_dependencies(base: Any) -> None:
    units = ("redis-sse-qa.service", "postgresql@16-sseqa.service")
    stopped = base.run(
        ["systemctl", "stop", *units],
        check=False,
        timeout=90,
        step="seed-fix-verifier-dependencies-stop",
    )
    active = [
        unit for unit in units
        if base._systemctl_property(unit, "ActiveState") != "inactive"
    ]
    if stopped.returncode != 0 or active:
        raise SeedFixError("QA verifier dependency stop was not confirmed")


def repair(bundle_root: Path) -> None:
    paths = validate_bundle(bundle_root)
    base = load_base_controller(bundle_root)
    ensure_real_runtime_disabled(base)
    ownership, ownership_raw, ownership_stat, initial_source = validate_disabled_installation(
        Path("/"), base,
    )
    backups: dict[str, tuple[bytes, os.stat_result]] | None = None
    db_transition_attempted = False
    before_db: dict[str, Any] | None = None
    postgres_started = False
    verify_dependencies_may_be_active = False
    result_action = "already_applied"
    source_result = "already_fixed"
    database_result = "already_fixed"
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    cancelling = False

    def cancel(_signum, _frame):
        nonlocal cancelling
        if cancelling:
            return
        cancelling = True
        raise SeedFixCancelled("seed repair cancelled")

    signal.signal(signal.SIGTERM, cancel)
    try:
        # The installed verifier temporarily starts PostgreSQL and Redis when
        # QA is disabled.  Install the cancellation trap and cleanup obligation
        # before entering it so SIGTERM cannot bypass its Python finally block.
        verify_dependencies_may_be_active = True
        base.verify_installation(Path("/"))
        ensure_real_runtime_disabled(base)
        verify_dependencies_may_be_active = False

        if initial_source == "old":
            # Capture caller-owned rollback state before the first write.  A
            # signal delivered at any function return/store boundary therefore
            # cannot hide the rollback obligation.
            backups, ownership_raw = snapshot_sources_and_journal(Path("/"))
            update_sources_and_journal(
                Path("/"), base, ownership, ownership_stat, paths,
                backups, ownership_raw,
            )
            source_result = "updated"
        # From this point PostgreSQL may have been started even if systemctl,
        # the slice check, or SIGTERM prevents start_postgres() from returning.
        # Mark the cleanup obligation before issuing the mutating command.
        postgres_started = True
        start_postgres(base)
        before_db = run_db_helper(base, paths["db_helper"], "inspect")
        if initial_source == "old" and before_db["action"] != "old":
            raise SeedFixError("legacy source and database state disagree")
        if initial_source == "fixed" and before_db["action"] != "fixed":
            raise SeedFixError("fixed source and database state disagree")
        db_transition_attempted = before_db["action"] == "old"
        applied = run_db_helper(base, paths["db_helper"], "apply")
        if applied["action"] == "applied":
            if initial_source != "old":
                raise SeedFixError("database changed under an already-fixed source state")
            result_action = "applied"
            database_result = "updated"
        elif initial_source == "old":
            raise SeedFixError("legacy database transition unexpectedly reported no-op")
        assert_protected_state_unchanged(
            before_db,
            applied,
            compare_canonical=before_db["action"] == "fixed",
        )
        after_db = run_db_helper(base, paths["db_helper"], "inspect")
        if after_db["action"] != "fixed":
            raise SeedFixError("seed-fix database did not reach the fixed state")
        assert_protected_state_unchanged(applied, after_db, compare_canonical=True)
        fault_injection("after_database")
        stop_postgres(base)
        postgres_started = False
        # Verify the installed fixed command, every owned hash and disabled state.
        verify_dependencies_may_be_active = True
        base.verify_installation(Path("/"))
        ensure_real_runtime_disabled(base)
        verify_dependencies_may_be_active = False
    except BaseException as primary:
        rollback_errors: list[str] = []
        if verify_dependencies_may_be_active:
            try:
                stop_verify_dependencies(base)
                verify_dependencies_may_be_active = False
            except Exception as exc:
                rollback_errors.append("verifier-dependencies:" + type(exc).__name__)
        if db_transition_attempted and before_db is not None:
            try:
                # systemctl start is idempotent and removes ambiguity after a
                # failed or interrupted stop attempt.
                postgres_started = True
                start_postgres(base)
                current_db = run_db_helper(base, paths["db_helper"], "inspect")
                if current_db["action"] == "fixed":
                    rolled_back = run_db_helper(base, paths["db_helper"], "rollback")
                    assert_protected_state_unchanged(before_db, rolled_back)
                    restored_db = run_db_helper(base, paths["db_helper"], "inspect")
                    if restored_db["action"] != "old":
                        raise SeedFixError("seed-fix database rollback state mismatch")
                    assert_protected_state_unchanged(before_db, restored_db)
                elif current_db["action"] != "old":
                    raise SeedFixError("seed-fix database rollback prestate is invalid")
            except Exception as exc:
                rollback_errors.append("database:" + type(exc).__name__)
        if postgres_started:
            try:
                stop_postgres(base)
                postgres_started = False
            except Exception as exc:
                rollback_errors.append("postgres:" + type(exc).__name__)
        if backups is not None:
            try:
                restore_sources_and_journal(
                    Path("/"), base, backups, ownership_raw, ownership_stat,
                )
            except Exception as exc:
                rollback_errors.append("source:" + type(exc).__name__)
        if rollback_errors:
            raise SeedFixError(
                "seed repair failed and rollback is incomplete: " + ",".join(rollback_errors)
            ) from primary
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)

    print(
        "SSE_QA_SEED_FIX_OK "
        f"version={VERSION} action={result_action} source={source_result} "
        f"database={database_result} history=preserved access=preserved qa=disabled"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("repair",))
    parser.add_argument("--bundle-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repair(args.bundle_root)
    except (SeedFixError, subprocess.SubprocessError, OSError, ValueError) as exc:
        print(f"SSE_QA_SEED_FIX_FAIL reason={type(exc).__name__}", file=sys.stderr)
        return 1
    except Exception:
        # Imported pinned-controller exceptions can contain commands, paths or
        # credential diagnostics.  The receiver gets only this fixed category.
        print("SSE_QA_SEED_FIX_FAIL reason=internal_error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
