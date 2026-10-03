from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "deployment/server/sse_qa_seed_fix_ctl.py"
SPEC = importlib.util.spec_from_file_location("sse_qa_seed_fix_ctl", SOURCE)
assert SPEC and SPEC.loader
ctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ctl)


class FakeBase:
    MARKER = "SSE_QA_INSTALLATION_V2"
    OWNERSHIP_SCHEMA = "SSE_QA_OWNERSHIP_V2"

    @staticmethod
    def secure_atomic_write(
        path: Path,
        data: bytes,
        mode: int,
        *,
        replace_existing: bool,
        owner_uid=None,
        owner_gid=None,
    ) -> None:
        if not replace_existing and path.exists():
            raise RuntimeError("existing")
        temporary = path.with_name("." + path.name + ".unit-test")
        temporary.write_bytes(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)


class SeedFixControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.seed = ctl.rooted(self.root, ctl.SEED_TARGET)
        self.test = ctl.rooted(self.root, ctl.TEST_TARGET)
        self.ownership_path = ctl.rooted(self.root, ctl.OWNERSHIP_PATH)
        self.seed.parent.mkdir(parents=True, exist_ok=True)
        self.test.parent.mkdir(parents=True, exist_ok=True)
        self.ownership_path.parent.mkdir(parents=True, exist_ok=True)
        self.old_seed = b"old seed bytes"
        self.old_test = b"old test bytes"
        self.new_seed = b"new seed bytes"
        self.new_test = b"new test bytes"
        self.patches = (
            mock.patch.object(ctl, "OLD_SEED_SHA256", self._hash(self.old_seed)),
            mock.patch.object(ctl, "OLD_TEST_SHA256", self._hash(self.old_test)),
            mock.patch.object(ctl, "NEW_SEED_SHA256", self._hash(self.new_seed)),
            mock.patch.object(ctl, "NEW_TEST_SHA256", self._hash(self.new_test)),
            mock.patch.object(ctl, "DB_HELPER_SHA256", "d" * 64),
        )
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.seed.write_bytes(self.old_seed)
        self.test.write_bytes(self.old_test)
        self.ownership = {
            "schema": FakeBase.OWNERSHIP_SCHEMA,
            "complete": True,
            "phase": "complete_disabled",
            "files": {
                ctl.SEED_LOGICAL: self._hash(self.old_seed),
                ctl.TEST_LOGICAL: self._hash(self.old_test),
            },
            "runtime_files": {},
        }
        self._write_ownership()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _hash(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def _write_ownership(self) -> None:
        self.ownership_path.write_text(
            json.dumps(self.ownership, sort_keys=True) + "\n", encoding="utf-8",
        )

    def test_source_state_accepts_only_exact_legacy_or_fixed_pairs(self) -> None:
        self.assertEqual(ctl.source_state(self.root, self.ownership), "old")
        self.seed.write_bytes(self.new_seed)
        with self.assertRaisesRegex(ctl.SeedFixError, "unsupported or mixed"):
            ctl.source_state(self.root, self.ownership)
        self.test.write_bytes(self.new_test)
        self.ownership["files"][ctl.SEED_LOGICAL] = self._hash(self.new_seed)
        self.ownership["files"][ctl.TEST_LOGICAL] = self._hash(self.new_test)
        self.ownership["seed_fix"] = ctl.expected_overlay()
        self.assertEqual(ctl.source_state(self.root, self.ownership), "fixed")
        self.ownership["seed_fix"]["version"] = "wrong"
        with self.assertRaisesRegex(ctl.SeedFixError, "inconsistent"):
            ctl.source_state(self.root, self.ownership)

    def test_source_update_and_restore_preserve_bytes_mode_and_journal(self) -> None:
        os.chmod(self.seed, 0o640)
        os.chmod(self.test, 0o600)
        ownership_raw = self.ownership_path.read_bytes()
        ownership_stat = self.ownership_path.stat(follow_symlinks=False)
        payload = self.root / "payload"
        payload.mkdir()
        new_seed = payload / "seed_sse_qa.py"
        new_test = payload / "test_sse_qa_seed.py"
        new_seed.write_bytes(self.new_seed)
        new_test.write_bytes(self.new_test)
        backups, saved_raw = ctl.snapshot_sources_and_journal(self.root)
        ctl.update_sources_and_journal(
            self.root,
            FakeBase,
            self.ownership,
            ownership_stat,
            {"seed": new_seed, "test": new_test},
            backups,
            saved_raw,
        )
        self.assertEqual(saved_raw, ownership_raw)
        self.assertEqual(self.seed.read_bytes(), self.new_seed)
        self.assertEqual(self.test.read_bytes(), self.new_test)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.seed.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(self.test.stat().st_mode), 0o600)
        updated = json.loads(self.ownership_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["seed_fix"], ctl.expected_overlay())
        self.assertEqual(updated["files"][ctl.SEED_LOGICAL], self._hash(self.new_seed))
        ctl.restore_sources_and_journal(
            self.root,
            FakeBase,
            backups,
            saved_raw,
            ownership_stat,
        )
        self.assertEqual(self.seed.read_bytes(), self.old_seed)
        self.assertEqual(self.test.read_bytes(), self.old_test)
        self.assertEqual(self.ownership_path.read_bytes(), ownership_raw)

    def test_parse_db_result_rejects_arbitrary_output_and_accepts_fixed_shape(self) -> None:
        value = {
            "schema": ctl.DB_RESULT_SCHEMA,
            "action": "applied",
            "legacy_id": 1,
            "canonical_id": 2,
            "placement_id": 3,
            "protected_digest": "a" * 64,
        }
        completed = subprocess.CompletedProcess(
            ["helper", "apply"], 0, json.dumps(value) + "\n", "",
        )
        self.assertEqual(ctl.parse_db_result(completed, {"applied"}), value)
        value["message"] = "untrusted"
        completed = subprocess.CompletedProcess(
            ["helper", "apply"], 0, json.dumps(value) + "\n", "",
        )
        with self.assertRaisesRegex(ctl.SeedFixError, "shape"):
            ctl.parse_db_result(completed, {"applied"})
        completed = subprocess.CompletedProcess(
            ["helper", "apply"], 1, "secret diagnostics", "",
        )
        with self.assertRaisesRegex(ctl.SeedFixError, "failed"):
            ctl.parse_db_result(completed, {"applied"})
        old = {
            "schema": ctl.DB_RESULT_SCHEMA,
            "action": "old",
            "legacy_id": 1,
            "canonical_id": None,
            "placement_id": 3,
            "protected_digest": "a" * 64,
        }
        completed = subprocess.CompletedProcess(
            ["helper", "inspect"], 0, json.dumps(old) + "\n", "",
        )
        self.assertEqual(ctl.parse_db_result(completed, {"old"}), old)

    def test_each_source_staging_fault_restores_both_files_and_exact_journal(self) -> None:
        payload = self.root / "payload"
        payload.mkdir()
        new_seed = payload / "seed_sse_qa.py"
        new_test = payload / "test_sse_qa_seed.py"
        new_seed.write_bytes(self.new_seed)
        new_test.write_bytes(self.new_test)
        paths = {"seed": new_seed, "test": new_test}
        raw = self.ownership_path.read_bytes()
        ownership_stat = self.ownership_path.stat(follow_symlinks=False)
        for requested in ("after_seed_file", "after_test_file", "after_journal"):
            with self.subTest(point=requested):
                self.seed.write_bytes(self.old_seed)
                self.test.write_bytes(self.old_test)
                self.ownership = json.loads(raw.decode("utf-8"))
                self.ownership_path.write_bytes(raw)

                def fail_at(point: str) -> None:
                    if point == requested:
                        raise ctl.SeedFixError("synthetic fault")

                with mock.patch.object(ctl, "fault_injection", side_effect=fail_at):
                    with self.assertRaisesRegex(ctl.SeedFixError, "synthetic fault"):
                        backups, saved_raw = ctl.snapshot_sources_and_journal(self.root)
                        ctl.update_sources_and_journal(
                            self.root,
                            FakeBase,
                            self.ownership,
                            ownership_stat,
                            paths,
                            backups,
                            saved_raw,
                        )
                self.assertEqual(self.seed.read_bytes(), self.old_seed)
                self.assertEqual(self.test.read_bytes(), self.old_test)
                self.assertEqual(self.ownership_path.read_bytes(), raw)

    def test_repair_applies_once_and_rolls_back_database_then_sources_on_failure(self) -> None:
        base = types.SimpleNamespace(
            verify_installation=mock.Mock(side_effect=[[], RuntimeError("post verify")]),
        )
        ownership_stat = self.ownership_path.stat(follow_symlinks=False)
        paths = {"db_helper": self.root / "db.py"}
        paths["db_helper"].write_text("pass\n", encoding="utf-8")
        backups = {
            "seed": (self.old_seed, self.seed.stat(follow_symlinks=False)),
            "test": (self.old_test, self.test.stat(follow_symlinks=False)),
        }
        calls: list[str] = []
        db_state = {"fixed": False, "canonical_id": 2}

        def db(_base, _helper, action):
            calls.append(action)
            if action == "inspect":
                selected = "fixed" if db_state["fixed"] else "old"
            elif action == "apply":
                db_state["fixed"] = True
                selected = "applied"
            else:
                db_state["fixed"] = False
                selected = "rolled_back"
            return {
                "schema": ctl.DB_RESULT_SCHEMA,
                "action": selected,
                "legacy_id": 1,
                "canonical_id": None if selected == "old" else db_state["canonical_id"],
                "placement_id": 3,
                "protected_digest": "a" * 64,
            }

        with (
            mock.patch.object(ctl, "validate_bundle", return_value=paths),
            mock.patch.object(ctl, "load_base_controller", return_value=base),
            mock.patch.object(ctl, "ensure_real_runtime_disabled"),
            mock.patch.object(
                ctl,
                "validate_disabled_installation",
                return_value=(
                    self.ownership,
                    self.ownership_path.read_bytes(),
                    ownership_stat,
                    "old",
                ),
            ),
            mock.patch.object(
                ctl,
                "snapshot_sources_and_journal",
                return_value=(backups, self.ownership_path.read_bytes()),
            ),
            mock.patch.object(ctl, "update_sources_and_journal"),
            mock.patch.object(ctl, "run_db_helper", side_effect=db),
            mock.patch.object(ctl, "start_postgres"),
            mock.patch.object(ctl, "stop_postgres"),
            mock.patch.object(ctl, "stop_verify_dependencies"),
            mock.patch.object(ctl, "restore_sources_and_journal") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "post verify"):
                ctl.repair(self.root)
        self.assertEqual(
            calls,
            ["inspect", "apply", "inspect", "inspect", "rollback", "inspect"],
        )
        restore.assert_called_once()

    def test_repair_stops_postgres_when_start_returns_with_an_error(self) -> None:
        """A post-start slice/SIGTERM error must still trigger an exact stop."""

        base = types.SimpleNamespace(verify_installation=mock.Mock(return_value=[]))
        ownership_stat = self.ownership_path.stat(follow_symlinks=False)
        paths = {"db_helper": self.root / "db.py"}
        paths["db_helper"].write_text("pass\n", encoding="utf-8")
        backups = {
            "seed": (self.old_seed, self.seed.stat(follow_symlinks=False)),
            "test": (self.old_test, self.test.stat(follow_symlinks=False)),
        }

        with (
            mock.patch.object(ctl, "validate_bundle", return_value=paths),
            mock.patch.object(ctl, "load_base_controller", return_value=base),
            mock.patch.object(ctl, "ensure_real_runtime_disabled"),
            mock.patch.object(
                ctl,
                "validate_disabled_installation",
                return_value=(
                    self.ownership,
                    self.ownership_path.read_bytes(),
                    ownership_stat,
                    "old",
                ),
            ),
            mock.patch.object(
                ctl,
                "snapshot_sources_and_journal",
                return_value=(backups, self.ownership_path.read_bytes()),
            ),
            mock.patch.object(ctl, "update_sources_and_journal"),
            mock.patch.object(
                ctl,
                "start_postgres",
                side_effect=ctl.SeedFixCancelled("post-start slice check interrupted"),
            ),
            mock.patch.object(ctl, "stop_postgres") as stop,
            mock.patch.object(ctl, "restore_sources_and_journal") as restore,
        ):
            with self.assertRaisesRegex(ctl.SeedFixCancelled, "post-start"):
                ctl.repair(self.root)

        stop.assert_called_once_with(base)
        restore.assert_called_once()

    def test_cancel_during_initial_verifier_stops_both_dependencies(self) -> None:
        base = types.SimpleNamespace(
            verify_installation=mock.Mock(
                side_effect=ctl.SeedFixCancelled("cancelled inside verifier")
            ),
        )
        ownership_stat = self.ownership_path.stat(follow_symlinks=False)
        paths = {"db_helper": self.root / "db.py"}
        paths["db_helper"].write_text("pass\n", encoding="utf-8")

        with (
            mock.patch.object(ctl, "validate_bundle", return_value=paths),
            mock.patch.object(ctl, "load_base_controller", return_value=base),
            mock.patch.object(ctl, "ensure_real_runtime_disabled"),
            mock.patch.object(
                ctl,
                "validate_disabled_installation",
                return_value=(
                    self.ownership,
                    self.ownership_path.read_bytes(),
                    ownership_stat,
                    "old",
                ),
            ),
            mock.patch.object(ctl, "stop_verify_dependencies") as stop_dependencies,
            mock.patch.object(ctl, "start_postgres") as start_postgres,
        ):
            with self.assertRaisesRegex(ctl.SeedFixCancelled, "inside verifier"):
                ctl.repair(self.root)

        stop_dependencies.assert_called_once_with(base)
        start_postgres.assert_not_called()

    def test_foreign_controller_error_is_redacted_without_traceback(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            ctl,
            "repair",
            side_effect=RuntimeError("sensitive path command credential value"),
        ), redirect_stderr(stderr):
            result = ctl.main(["repair", "--bundle-root", str(self.root)])

        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue(),
            "SSE_QA_SEED_FIX_FAIL reason=internal_error\n",
        )

    def test_verifier_postcheck_failure_stops_both_dependencies(self) -> None:
        base = types.SimpleNamespace(
            verify_installation=mock.Mock(return_value=[]),
        )
        ownership_stat = self.ownership_path.stat(follow_symlinks=False)
        paths = {"db_helper": self.root / "db.py"}
        paths["db_helper"].write_text("pass\n", encoding="utf-8")

        with (
            mock.patch.object(ctl, "validate_bundle", return_value=paths),
            mock.patch.object(ctl, "load_base_controller", return_value=base),
            mock.patch.object(
                ctl,
                "ensure_real_runtime_disabled",
                side_effect=[None, ctl.SeedFixError("QA service still active")],
            ),
            mock.patch.object(
                ctl,
                "validate_disabled_installation",
                return_value=(
                    self.ownership,
                    self.ownership_path.read_bytes(),
                    ownership_stat,
                    "old",
                ),
            ),
            mock.patch.object(ctl, "stop_verify_dependencies") as stop_dependencies,
            mock.patch.object(ctl, "start_postgres") as start_postgres,
        ):
            with self.assertRaisesRegex(ctl.SeedFixError, "still active"):
                ctl.repair(self.root)

        stop_dependencies.assert_called_once_with(base)
        start_postgres.assert_not_called()


if __name__ == "__main__":
    unittest.main()
