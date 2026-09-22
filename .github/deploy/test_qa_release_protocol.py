from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / ".github" / "deploy" / "build_qa_release.py"
RECEIVER = ROOT / "deployment" / "server" / "accounting_github_qa_deploy_receiver.py"
WORKFLOW = ROOT / ".github" / "workflows" / "qa-backend-deploy.yml"


def load_receiver():
    spec = importlib.util.spec_from_file_location("qa_receiver", RECEIVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load QA receiver")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QAReleaseProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.receiver = load_receiver()
        cls.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        cls.temporary = tempfile.TemporaryDirectory(prefix="qa-release-tests-")
        cls.temp = Path(cls.temporary.name)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def build(self, mode: str, name: str, *extra: str) -> tuple[Path, str]:
        target = self.temp / name
        result = subprocess.run(
            [
                sys.executable,
                str(BUILDER),
                "--root", str(ROOT),
                "--output", str(target),
                "--commit", self.commit,
                "--mode", mode,
                *extra,
            ],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return target, result.stdout

    def test_verify_package_is_deterministic_and_complete(self):
        first, first_output = self.build("qa_verify", "verify-1.tar.gz")
        second, second_output = self.build("qa_verify", "verify-2.tar.gz")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertIn("QA_SNAPSHOT_SHA256=", first_output)
        self.assertEqual(
            [line for line in first_output.splitlines() if "SNAPSHOT" in line],
            [line for line in second_output.splitlines() if "SNAPSHOT" in line],
        )
        manifest, payload = self.receiver.load_release(first)
        self.assertEqual(manifest["schema"], 2)
        self.assertEqual(manifest["mode"], "qa_verify")
        self.assertGreater(len(payload), 100)
        self.assertTrue(self.receiver.REQUIRED_FILES.issubset(payload))
        self.assertNotIn(".env", payload)
        self.assertFalse(any("/media/" in f"/{path}/" for path in payload))

    def test_audit_has_no_payload(self):
        package, output = self.build("qa_audit", "audit.tar.gz")
        manifest, payload = self.receiver.load_release(package)
        self.assertEqual(manifest["mode"], "qa_audit")
        self.assertEqual(payload, {})
        self.assertIn("QA_FILES=0", output)
        with tarfile.open(package, "r:gz") as archive:
            self.assertEqual(archive.getnames(), ["qa-release-manifest.json"])

    def test_rollback_id_is_strict(self):
        valid = "qa-github-20260923-010203-0123456789ab-before"
        package, _ = self.build(
            "qa_rollback", "rollback.tar.gz", "--rollback-id", valid
        )
        manifest, payload = self.receiver.load_release(package)
        self.assertEqual(manifest["metadata"]["rollback_id"], valid)
        self.assertEqual(payload, {})
        failed = subprocess.run(
            [
                sys.executable, str(BUILDER),
                "--root", str(ROOT),
                "--output", str(self.temp / "invalid-rollback.tar.gz"),
                "--commit", self.commit,
                "--mode", "qa_rollback",
                "--rollback-id", "../production",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertNotEqual(failed.returncode, 0)

    def test_deploy_requires_server_verification_fields(self):
        failed = subprocess.run(
            [
                sys.executable, str(BUILDER),
                "--root", str(ROOT),
                "--output", str(self.temp / "unverified-deploy.tar.gz"),
                "--commit", self.commit,
                "--mode", "qa_deploy",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertNotEqual(failed.returncode, 0)
        package, _ = self.build(
            "qa_deploy",
            "verified-deploy.tar.gz",
            "--verification-id", "a" * 64,
            "--migration-plan-sha256", "b" * 64,
        )
        manifest, _ = self.receiver.load_release(package)
        self.assertEqual(manifest["metadata"]["verification_id"], "a" * 64)
        self.assertEqual(
            manifest["metadata"]["migration_plan_sha256"], "b" * 64
        )
        self.assertIs(manifest["metadata"]["allow_migrations"], True)

    def test_receiver_rejects_unsafe_targets(self):
        for value in (
            "../manage.py",
            "/srv/accounting-mvp/manage.py",
            r"config\settings.py",
            ".env",
            ".venv/bin/python",
            "media/apk/test.apk",
            "keys/server.pem",
        ):
            with self.subTest(value=value):
                with self.assertRaises(self.receiver.ReleaseError):
                    self.receiver.validate_target(value)

    def test_receiver_is_qa_only(self):
        self.receiver.assert_qa_boundaries()
        self.assertEqual(
            self.receiver.APP, Path("/srv/accounting-mvp-excavator-qa")
        )
        self.assertEqual(
            self.receiver.EXPECTED_DATABASE, "accounting_mvp_excavator_qa"
        )
        self.assertNotIn("deploy", self.receiver.MODES)
        self.assertNotIn("rollback", self.receiver.MODES)
        self.assertNotEqual(self.receiver.SERVICE, "accounting-mvp")
        self.assertNotEqual(
            self.receiver.SOCKET, Path("/run/accounting-mvp/accounting-mvp.sock")
        )

    def test_backup_tracks_stale_code_but_ignores_runtime_directories(self):
        original_app = self.receiver.APP
        original_backups = self.receiver.BACKUPS
        original_load_policy = self.receiver.load_policy
        original_validate_backup_root = self.receiver.validate_backup_root
        with tempfile.TemporaryDirectory(prefix="qa-backup-test-") as temporary:
            root = Path(temporary)
            app = root / "app"
            (app / "core").mkdir(parents=True)
            (app / "assignments").mkdir()
            (app / "media").mkdir()
            (app / "core" / "kept.py").write_text("old", encoding="utf-8")
            (app / "core" / "stale.py").write_text("stale", encoding="utf-8")
            (app / "assignments" / "removed_app.py").write_text(
                "legacy", encoding="utf-8"
            )
            (app / "media" / "operator.jpg").write_bytes(b"runtime")
            self.receiver.APP = app
            self.receiver.BACKUPS = app / "backups" / "github-qa"
            self.receiver.BACKUPS.mkdir(parents=True)
            self.receiver.load_policy = lambda: {"instance_id": "test-instance-1234"}
            self.receiver.validate_backup_root = lambda: None
            try:
                manifest = {
                    "commit": "1" * 40,
                    "files": [{"path": "core/kept.py"}],
                }
                payload = {"core/kept.py": (b"new", 0o644)}
                backup = self.receiver.new_backup(manifest, payload)
                stale = json.loads(
                    (backup / "stale.json").read_text(encoding="utf-8")
                )
                existing = json.loads(
                    (backup / "existing.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    stale, ["assignments/removed_app.py", "core/stale.py"]
                )
                self.assertEqual(
                    {item["path"] for item in existing},
                    {
                        "assignments/removed_app.py",
                        "core/kept.py",
                        "core/stale.py",
                    },
                )
                self.assertFalse((backup / "files" / "media").exists())
            finally:
                self.receiver.APP = original_app
                self.receiver.BACKUPS = original_backups
                self.receiver.load_policy = original_load_policy
                self.receiver.validate_backup_root = original_validate_backup_root

    def test_subprocess_timeout_is_fail_closed(self):
        with mock.patch.object(
            self.receiver.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["systemctl", "stop"], 1),
        ):
            with self.assertRaisesRegex(
                self.receiver.ReleaseError, "command timed out after 1s: systemctl"
            ):
                self.receiver.run(
                    ["systemctl", "stop", self.receiver.SERVICE],
                    cwd=self.temp,
                    timeout_seconds=1,
                )

    def test_failed_change_restores_exact_backup_before_restart(self):
        backup = self.temp / "recovery-point"
        current = {"commit": "a" * 40}
        calls: list[str] = []

        with (
            mock.patch.object(
                self.receiver,
                "stop_services_best_effort",
                side_effect=lambda: calls.append("stop"),
            ),
            mock.patch.object(
                self.receiver,
                "restore_files",
                side_effect=lambda value: calls.append(f"files:{value.name}"),
            ),
            mock.patch.object(
                self.receiver,
                "restore_database",
                side_effect=lambda value: calls.append(f"db:{value.name}"),
            ),
            mock.patch.object(
                self.receiver,
                "verify_live_backup",
                side_effect=lambda value: calls.append(f"verify:{value.name}"),
            ),
            mock.patch.object(
                self.receiver,
                "django_command",
                side_effect=lambda *args, **kwargs: calls.append("static"),
            ),
            mock.patch.object(
                self.receiver,
                "set_current_release",
                side_effect=lambda value: calls.append(f"current:{value['commit'][:1]}"),
            ),
            mock.patch.object(
                self.receiver,
                "start_services",
                side_effect=lambda **kwargs: calls.append(
                    f"start:{kwargs['simulator']}"
                ),
            ),
            mock.patch.object(
                self.receiver,
                "wait_for_service",
                side_effect=lambda: calls.append("ready"),
            ),
        ):
            self.receiver.recover_failed_change(
                backup,
                simulator_was_active=True,
                current_manifest=current,
            )

        self.assertEqual(
            calls,
            [
                "stop",
                "files:recovery-point",
                "db:recovery-point",
                "verify:recovery-point",
                "static",
                "current:a",
                "start:True",
                "ready",
            ],
        )

    def test_failed_recovery_never_restarts_services(self):
        backup = self.temp / "broken-recovery-point"
        with (
            mock.patch.object(self.receiver, "stop_services_best_effort"),
            mock.patch.object(self.receiver, "restore_files"),
            mock.patch.object(
                self.receiver,
                "restore_database",
                side_effect=self.receiver.ReleaseError("restore timed out"),
            ),
            mock.patch.object(self.receiver, "start_services") as start,
        ):
            with self.assertRaisesRegex(
                self.receiver.ReleaseError, "restore timed out"
            ):
                self.receiver.recover_failed_change(
                    backup, simulator_was_active=True
                )
            start.assert_not_called()

    def test_redis_boundary_rejects_production_endpoint(self):
        policy = {
            "redis_scheme": "redis",
            "redis_host": "qa-redis.internal",
            "redis_port": 6380,
            "redis_username": "accounting-qa",
            "redis_database": 2,
            "cache_prefix": "accounting-mvp-excavator-qa",
        }
        self.receiver.validate_redis_boundary(
            "redis://accounting-qa@qa-redis.internal:6380/2",
            "2",
            "accounting-mvp-excavator-qa",
            policy,
        )
        with self.assertRaisesRegex(
            self.receiver.ReleaseError, "Redis endpoint/database/prefix"
        ):
            self.receiver.validate_redis_boundary(
                "redis://accounting-qa@production-redis.internal:6379/2",
                "2",
                "accounting-mvp-excavator-qa",
                policy,
            )
        with self.assertRaisesRegex(
            self.receiver.ReleaseError, "Redis endpoint/database/prefix"
        ):
            self.receiver.validate_redis_boundary(
                "redis://accounting-qa@qa-redis.internal:6380/2?db=0",
                "2",
                "accounting-mvp-excavator-qa",
                policy,
            )

    def test_receiver_deadline_finishes_before_outer_job_timeout(self):
        self.assertLess(
            self.receiver.MUTATING_PHASE_TIMEOUT_SECONDS
            + self.receiver.RECOVERY_PHASE_TIMEOUT_SECONDS,
            self.receiver.OUTER_RELEASE_TIMEOUT_SECONDS,
        )
        self.receiver.set_command_deadline(1)
        try:
            self.assertLessEqual(self.receiver.bounded_timeout(60), 1)
        finally:
            self.receiver.set_command_deadline(None)

    def test_backup_root_rejects_wrong_owner_or_mode(self):
        fake_backup = mock.Mock()
        fake_backup.is_symlink.return_value = False
        fake_backup.is_dir.return_value = True
        fake_backup.stat.return_value = mock.Mock(st_uid=1000, st_mode=0o40700)
        with mock.patch.object(self.receiver, "BACKUPS", fake_backup):
            with self.assertRaisesRegex(
                self.receiver.ReleaseError, "root-owned mode 0700"
            ):
                self.receiver.validate_backup_root()

        fake_backup.stat.return_value = mock.Mock(st_uid=0, st_mode=0o40750)
        with mock.patch.object(self.receiver, "BACKUPS", fake_backup):
            with self.assertRaisesRegex(
                self.receiver.ReleaseError, "root-owned mode 0700"
            ):
                self.receiver.validate_backup_root()

    def test_restore_reconciles_newer_files_to_older_backup(self):
        original_app = self.receiver.APP
        original_current = self.receiver.CURRENT
        original_write_atomic = self.receiver.write_atomic
        with tempfile.TemporaryDirectory(prefix="qa-rollback-test-") as temporary:
            root = Path(temporary)
            app = root / "app"
            backup = root / "backup"
            (app / "core").mkdir(parents=True)
            (backup / "files" / "core").mkdir(parents=True)
            (app / "core" / "kept.py").write_bytes(b"release-b")
            (app / "core" / "newer.py").write_bytes(b"only-b")
            (app / "sitecustomize.py").write_bytes(b"untracked-root-code")
            (backup / "files" / "core" / "kept.py").write_bytes(b"release-a")
            (backup / "existing.json").write_text(
                json.dumps([{
                    "path": "core/kept.py",
                    "mode": 0o644,
                    "size": len(b"release-a"),
                    "sha256": hashlib.sha256(b"release-a").hexdigest(),
                }]),
                encoding="utf-8",
            )
            (backup / "created.json").write_text("[]", encoding="utf-8")
            (backup / "qa-release-manifest.json").write_text(
                json.dumps({
                    "schema": 2,
                    "channel": "excavator_qa",
                    "commit": "a" * 40,
                    "files": [{"path": "core/kept.py", "mode": 0o644}],
                }),
                encoding="utf-8",
            )
            current = root / "current.json"
            current.write_text(
                json.dumps({
                    "schema": 2,
                    "channel": "excavator_qa",
                    "commit": "b" * 40,
                    "files": [
                        {"path": "core/kept.py", "mode": 0o644},
                        {"path": "core/newer.py", "mode": 0o644},
                    ],
                }),
                encoding="utf-8",
            )

            def local_write(target, data, mode):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o750 if mode & 0o111 else 0o640)

            self.receiver.APP = app
            self.receiver.CURRENT = current
            self.receiver.write_atomic = local_write
            try:
                self.receiver.restore_files(backup)
                self.assertEqual((app / "core" / "kept.py").read_bytes(), b"release-a")
                self.assertFalse((app / "core" / "newer.py").exists())
                self.assertFalse((app / "sitecustomize.py").exists())
            finally:
                self.receiver.APP = original_app
                self.receiver.CURRENT = original_current
                self.receiver.write_atomic = original_write_atomic

    def test_live_snapshot_drift_is_detected(self):
        original_app = self.receiver.APP
        with tempfile.TemporaryDirectory(prefix="qa-live-drift-") as temporary:
            app = Path(temporary) / "app"
            (app / "core").mkdir(parents=True)
            source = app / "core" / "stable.py"
            source.write_bytes(b"stable")
            source.chmod(0o640)
            manifest = {
                "files": [{
                    "path": "core/stable.py",
                    "mode": 0o644,
                    "size": 6,
                    "sha256": hashlib.sha256(b"stable").hexdigest(),
                }]
            }
            self.receiver.APP = app
            try:
                self.receiver.verify_live_manifest(manifest)
                source.write_bytes(b"changed")
                with self.assertRaisesRegex(
                    self.receiver.ReleaseError, "live code drift"
                ):
                    self.receiver.verify_live_manifest(manifest)
            finally:
                self.receiver.APP = original_app

    def test_workflow_uses_only_qa_release_secrets(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("environment: qa", text)
        self.assertIn("QA_DEPLOY_KEY", text)
        self.assertIn("QA_DEPLOY_HOST", text)
        self.assertIn("QA_DEPLOY_MIGRATIONS", text)
        self.assertIn("verified_snapshot_sha256", text)
        self.assertIn("verification_id", text)
        self.assertIn("migration_plan_sha256", text)
        self.assertIn("timeout-minutes: 30", text)
        self.assertIn("timeout-minutes: 60", text)
        self.assertNotIn("PROD_", text)
        self.assertNotIn("production-deploy", text)
        self.assertNotIn("accounting_github_deploy_receiver.py", text)
        self.assertLess(
            text.index("Upload immutable QA package"),
            text.index("Run targeted QA backend tests after immutable packaging"),
        )

    def test_candidate_python_is_dropped_to_dedicated_qa_identity(self):
        source = RECEIVER.read_text(encoding="utf-8")
        self.assertIn('APP_OS_USER = "accounting-qa"', source)
        self.assertIn('APP_OS_GROUP = "accounting-qa"', source)
        self.assertIn("os.setgroups([])", source)
        self.assertIn("os.setuid(qa_uid)", source)
        self.assertIn("PR_SET_NO_NEW_PRIVS", source)
        self.assertIn("as_qa_user=True", source)
        self.assertIn('"static_root": cwd / "staticfiles"', source)

    def test_manifest_and_snapshot_tampering_are_rejected(self):
        package, _ = self.build("qa_verify", "tamper-source.tar.gz")
        with tarfile.open(package, "r:gz") as archive:
            members = {
                member.name: archive.extractfile(member).read()
                for member in archive.getmembers()
            }
        manifest = json.loads(members["qa-release-manifest.json"])
        manifest["metadata"]["snapshot_sha256"] = "0" * 64
        members["qa-release-manifest.json"] = (
            json.dumps(manifest, sort_keys=True) + "\n"
        ).encode()
        tampered = self.temp / "tampered.tar.gz"
        import gzip
        import io
        with tampered.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w") as out:
                    for name, data in members.items():
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        info.mtime = 0
                        out.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(
            self.receiver.ReleaseError, "snapshot checksum mismatch"
        ):
            self.receiver.load_release(tampered)

    def test_schema_one_package_is_rejected_by_new_receiver(self):
        package, _ = self.build("qa_audit", "schema-source.tar.gz")
        with tarfile.open(package, "r:gz") as archive:
            manifest = json.loads(
                archive.extractfile("qa-release-manifest.json").read()
            )
        manifest["schema"] = 1
        import gzip
        import io
        legacy = self.temp / "schema-one.tar.gz"
        data = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        with legacy.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w") as out:
                    info = tarfile.TarInfo("qa-release-manifest.json")
                    info.size = len(data)
                    info.mtime = 0
                    out.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(
            self.receiver.ReleaseError, "unsupported QA release protocol"
        ):
            self.receiver.load_release(legacy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
