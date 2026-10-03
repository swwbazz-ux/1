from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deployment" / "server" / "sse_qa_seed_fix_db.py"
SPEC = importlib.util.spec_from_file_location("sse_qa_seed_fix_db", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
dbfix = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dbfix
SPEC.loader.exec_module(dbfix)


class FakeStore:
    def __init__(self, state: str = "old") -> None:
        self.legacy = dbfix.RockRecord(
            11,
            dbfix.LEGACY_ROCK_NAME,
            dbfix.LEGACY_ROCK_DENSITY,
            dbfix.LEGACY_ROCK_LOOSENING_FACTOR,
            True,
        )
        self.canonical = None
        self.placement = dbfix.PlacementRecord(31, 41, self.legacy.pk)
        if state == "fixed":
            self.canonical = dbfix.RockRecord(
                21,
                dbfix.CANONICAL_ROCK_NAME,
                dbfix.CANONICAL_ROCK_DENSITY,
                dbfix.CANONICAL_ROCK_LOOSENING_FACTOR,
                True,
            )
            self.placement = dbfix.PlacementRecord(
                self.placement.pk, self.placement.excavator_pk, self.canonical.pk
            )
        self.protected = {
            "trip": [1, "history-preserved"],
            "access": [2, "access-preserved"],
            "version": [3, 26],
        }
        self.extra_canonical_reference = False
        self.mutate_protected_on_update = False
        self.validated = 0

    @contextmanager
    def atomic(self):
        snapshot = copy.deepcopy(self.__dict__)
        try:
            yield
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(snapshot)
            raise

    def validate_target(self):
        self.validated += 1

    def load_locked_state(self):
        return dbfix.LockedState(self.legacy, self.canonical, self.placement)

    def protected_digest(self, placement_id):
        assert placement_id == self.placement.pk
        return hashlib.sha256(
            json.dumps(self.protected, sort_keys=True).encode("ascii")
        ).hexdigest()

    def create_canonical(self):
        if self.canonical is not None:
            raise AssertionError("duplicate canonical creation")
        self.canonical = dbfix.RockRecord(
            21,
            dbfix.CANONICAL_ROCK_NAME,
            dbfix.CANONICAL_ROCK_DENSITY,
            dbfix.CANONICAL_ROCK_LOOSENING_FACTOR,
            True,
        )
        return self.canonical

    def set_placement_rock(self, placement_id, expected_rock_id, new_rock_id):
        if (
            placement_id != self.placement.pk
            or self.placement.rock_pk != expected_rock_id
        ):
            raise dbfix.SeedFixError("placement_concurrent_change")
        self.placement = dbfix.PlacementRecord(
            self.placement.pk, self.placement.excavator_pk, new_rock_id
        )
        if self.mutate_protected_on_update:
            self.protected["version"][1] += 1

    def assert_canonical_only_target_reference(self, canonical_id, placement_id):
        if self.canonical is None or self.canonical.pk != canonical_id:
            raise dbfix.SeedFixError("canonical_rock_missing")
        if self.placement.pk != placement_id or self.placement.rock_pk != canonical_id:
            raise dbfix.SeedFixError("canonical_rock_placement_mismatch")
        if self.extra_canonical_reference:
            raise dbfix.SeedFixError("canonical_rock_in_use")

    def delete_canonical(self, canonical_id):
        if self.canonical is None or self.canonical.pk != canonical_id:
            raise dbfix.SeedFixError("canonical_delete_failed")
        self.canonical = None


class FakeCursor:
    def __init__(self, row=(dbfix.MARKER,), fail=False):
        self.row = row
        self.fail = fail
        self.query = None
        self.parameters = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def execute(self, query, parameters):
        if self.fail:
            raise RuntimeError("table absent")
        self.query = query
        self.parameters = parameters

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(
        self,
        *,
        vendor="postgresql",
        name="sseqa",
        host="127.0.0.1",
        port="55432",
        marker=(dbfix.MARKER,),
        cursor_fail=False,
    ):
        self.vendor = vendor
        self.settings_dict = {"NAME": name, "HOST": host, "PORT": port}
        self.fake_cursor = FakeCursor(marker, cursor_fail)

    def cursor(self):
        return self.fake_cursor


class SeedFixCoreTests(unittest.TestCase):
    def test_inspect_reports_exact_old_state_without_mutation(self):
        store = FakeStore("old")
        before = copy.deepcopy(store.__dict__)
        result = dbfix.run_store_action(store, "inspect")
        self.assertEqual(result.action, "old")
        self.assertIsNone(result.canonical_id)
        self.assertEqual(store.__dict__, {**before, "validated": 1})

    def test_apply_is_narrow_and_preserves_protected_digest(self):
        store = FakeStore("old")
        legacy_before = store.legacy
        protected_before = copy.deepcopy(store.protected)
        result = dbfix.run_store_action(store, "apply")
        self.assertEqual(result.action, "applied")
        self.assertEqual(dbfix.classify_state(store.load_locked_state()), "fixed")
        self.assertEqual(store.legacy, legacy_before)
        self.assertEqual(store.protected, protected_before)
        self.assertEqual(result.protected_digest, store.protected_digest(31))

    def test_apply_is_idempotent_on_exact_fixed_state(self):
        store = FakeStore("fixed")
        before = copy.deepcopy(store.__dict__)
        result = dbfix.run_store_action(store, "apply")
        self.assertEqual(result.action, "already_applied")
        self.assertEqual(store.__dict__, {**before, "validated": 1})

    def test_apply_rejects_third_rock_placement(self):
        store = FakeStore("old")
        store.placement = dbfix.PlacementRecord(31, 41, 999)
        with self.assertRaisesRegex(dbfix.SeedFixError, "mixed_seed_state"):
            dbfix.run_store_action(store, "apply")
        self.assertIsNone(store.canonical)

    def test_apply_rejects_mixed_existing_canonical(self):
        store = FakeStore("fixed")
        store.placement = dbfix.PlacementRecord(31, 41, store.legacy.pk)
        with self.assertRaisesRegex(dbfix.SeedFixError, "mixed_seed_state"):
            dbfix.run_store_action(store, "apply")

    def test_apply_rejects_wrong_legacy_measurements(self):
        store = FakeStore("old")
        store.legacy = dbfix.RockRecord(
            11,
            dbfix.LEGACY_ROCK_NAME,
            dbfix.LEGACY_ROCK_DENSITY,
            dbfix.Decimal("1.1000"),
            True,
        )
        with self.assertRaisesRegex(dbfix.SeedFixError, "legacy_rock_mismatch"):
            dbfix.run_store_action(store, "apply")

    def test_apply_rejects_wrong_or_inactive_canonical(self):
        for density, active in ((dbfix.Decimal("2.7000"), True), (dbfix.CANONICAL_ROCK_DENSITY, False)):
            with self.subTest(density=density, active=active):
                store = FakeStore("fixed")
                store.canonical = dbfix.RockRecord(
                    21,
                    dbfix.CANONICAL_ROCK_NAME,
                    density,
                    dbfix.CANONICAL_ROCK_LOOSENING_FACTOR,
                    active,
                )
                with self.assertRaisesRegex(dbfix.SeedFixError, "canonical_rock_mismatch"):
                    dbfix.run_store_action(store, "apply")

    def test_digest_change_aborts_and_atomic_restores_old_state(self):
        store = FakeStore("old")
        before = copy.deepcopy(store.__dict__)
        store.mutate_protected_on_update = True
        before["mutate_protected_on_update"] = True
        with self.assertRaisesRegex(dbfix.SeedFixError, "protected_state_changed"):
            dbfix.run_store_action(store, "apply")
        self.assertEqual(store.__dict__, before)

    def test_rollback_restores_exact_old_state(self):
        store = FakeStore("fixed")
        protected_before = copy.deepcopy(store.protected)
        result = dbfix.run_store_action(store, "rollback")
        self.assertEqual(result.action, "rolled_back")
        self.assertEqual(result.canonical_id, 21)
        self.assertEqual(dbfix.classify_state(store.load_locked_state()), "old")
        self.assertIsNone(store.canonical)
        self.assertEqual(store.protected, protected_before)

    def test_rollback_refuses_canonical_used_by_history(self):
        store = FakeStore("fixed")
        store.extra_canonical_reference = True
        before = copy.deepcopy(store.__dict__)
        with self.assertRaisesRegex(dbfix.SeedFixError, "canonical_rock_in_use"):
            dbfix.run_store_action(store, "rollback")
        self.assertEqual(store.__dict__, before)

    def test_rollback_refuses_old_state(self):
        store = FakeStore("old")
        with self.assertRaisesRegex(dbfix.SeedFixError, "rollback_requires_fixed_state"):
            dbfix.run_store_action(store, "rollback")


class TargetValidationTests(unittest.TestCase):
    def test_accepts_only_fixed_postgresql_identity_and_marker(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                connection = FakeConnection(host=host)
                dbfix.validate_database_identity(connection)
                self.assertEqual(connection.fake_cursor.parameters, [dbfix.MARKER])

    def test_rejects_wrong_database_endpoint_or_marker(self):
        cases = (
            (FakeConnection(vendor="sqlite"), "invalid_database_vendor"),
            (FakeConnection(name="production"), "invalid_database_name"),
            (FakeConnection(host="10.0.0.2"), "invalid_database_host"),
            (FakeConnection(port="5432"), "invalid_database_port"),
            (FakeConnection(marker=None), "missing_database_marker"),
            (FakeConnection(cursor_fail=True), "missing_database_marker"),
        )
        for connection, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(
                dbfix.SeedFixError, error
            ):
                dbfix.validate_database_identity(connection)


class CliContractTests(unittest.TestCase):
    def test_success_is_one_deterministic_json_line(self):
        result = dbfix.Result("applied", 11, 21, 31, "a" * 64)
        output = io.StringIO()
        with mock.patch.object(dbfix, "run_django_action", return_value=result):
            with redirect_stdout(output):
                exit_code = dbfix.main(["apply"])
        self.assertEqual(exit_code, 0)
        expected = json.dumps(
            result.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ) + "\n"
        self.assertEqual(output.getvalue(), expected)
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_cli_accepts_no_ids_paths_or_extra_arguments(self):
        for arguments in ([], ["apply", "21"], ["apply", "C:/tmp/x"], ["other"]):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = dbfix.main(arguments)
                self.assertEqual(exit_code, 64)
                self.assertEqual(json.loads(output.getvalue())["error"], "invalid_action")

    def test_expected_error_is_sanitized_one_line(self):
        output = io.StringIO()
        with mock.patch.object(
            dbfix,
            "run_django_action",
            side_effect=dbfix.SeedFixError("mixed_seed_state"),
        ):
            with redirect_stdout(output):
                exit_code = dbfix.main(["inspect"])
        self.assertEqual(exit_code, 65)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "schema": dbfix.SCHEMA,
                "action": "error",
                "error": "mixed_seed_state",
            },
        )
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_real_adapter_contains_required_transaction_and_row_locks(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("self._transaction.atomic()", source)
        self.assertGreaterEqual(source.count("select_for_update()"), 7)
        self.assertIn(".update(work_rock_type_id=new_rock_id)", source)
        self.assertIn("bulk_create([rock])", source)
        self.assertIn("_raw_delete(using=self._connection.alias)", source)
        self.assertNotIn("RockType._base_manager.create(", source)

    def test_protected_model_discovery_covers_all_managed_concrete_app_models(self):
        def model(label, *, managed=True, proxy=False):
            app_label, _ = label.split(".", 1)
            return SimpleNamespace(
                _meta=SimpleNamespace(
                    app_label=app_label,
                    label_lower=label,
                    managed=managed,
                    proxy=proxy,
                )
            )

        included = [
            model("assignments.equipmentassignment"),
            model("core.offlinefieldeventconflict"),
            model("shifts.shiftclientaction"),
            model("trips.dispatcheractionlog"),
            model("trips.freebucketacceptance"),
            model("users.employeeaccess"),
        ]
        excluded = [
            model("assignments.excavatorplacement"),
            model("references.rocktype"),
            model("admin.logentry"),
            model("users.proxyrow", proxy=True),
            model("users.unmanagedrow", managed=False),
        ]
        store = object.__new__(dbfix.DjangoStore)
        store._apps = SimpleNamespace(
            get_models=lambda **unused: list(reversed(included + excluded))
        )
        labels = [item._meta.label_lower for item in store._protected_models()]
        self.assertEqual(labels, sorted(item._meta.label_lower for item in included))

    def test_real_runtime_import_path_is_fixed_to_installed_r3_backend(self):
        backend = (ROOT / "СИСТЕМА_MVP" / "backend").resolve()
        previous_path = list(sys.path)
        try:
            with mock.patch.object(dbfix.Path, "cwd", return_value=backend), mock.patch.object(
                dbfix, "REAL_BACKEND", backend
            ), mock.patch.object(dbfix, "CURRENT_BACKEND", backend):
                dbfix._prepare_backend_import_path()
            self.assertEqual(sys.path[0], str(backend))
        finally:
            sys.path[:] = previous_path

    def test_real_runtime_import_path_rejects_another_backend(self):
        backend = (ROOT / "СИСТЕМА_MVP" / "backend").resolve()
        with mock.patch.object(dbfix.Path, "cwd", return_value=backend), mock.patch.object(
            dbfix, "REAL_BACKEND", backend.parent
        ), mock.patch.object(dbfix, "CURRENT_BACKEND", backend):
            with self.assertRaisesRegex(dbfix.SeedFixError, "invalid_backend_runtime"):
                dbfix._prepare_backend_import_path()


if __name__ == "__main__":
    unittest.main(verbosity=2)
