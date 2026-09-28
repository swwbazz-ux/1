from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tarfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "github-actions/source-overlay"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


ctl = load("sse_qa_ctl", ROOT / "scripts/sse_qa_ctl.py")
package_checker = load("sse_qa_package_checker", ROOT / "scripts/package_self_check.py")
receiver = load(
    "sse_qa_receiver",
    CONTROL / "deployment/server/accounting_github_deploy_receiver.py",
)


def valid_secrets() -> dict[str, object]:
    return {
        "schema": 1,
        "allow_cidr": "198.51.100.10/32",
        "basic_auth_line": "reviewer:$2y$12$" + "a" * 53,
        "django_secret_key": "D" * 64,
        "postgres_app_password": "A" * 32,
        "postgres_maint_password": "M" * 32,
        "redis_password": "R" * 32,
        "driver_pin": "135791",
        "excavator_pin": "246802",
    }


def sealed_credentials() -> dict[str, bytes]:
    return {
        name: f"SYSTEMD-CREDENTIAL-TEST:{name}".encode("ascii")
        for name in ctl.ENCRYPTED_CREDENTIAL_KEYS
    }


def qa_metadata() -> dict[str, object]:
    return dict(receiver.SSE_QA_METADATA)


class PackageContractTests(unittest.TestCase):
    def test_resource_limits_are_hard_and_bounded(self):
        slice_text = (ROOT / "config/systemd/sse-qa.slice").read_text(encoding="utf-8")
        self.assertIn("CPUQuota=100%", slice_text)
        self.assertIn("MemoryMax=2G", slice_text)
        self.assertIn("MemorySwapMax=0", slice_text)
        self.assertIn("TasksMax=256", slice_text)
        postgres = (ROOT / "config/postgresql/postgresql.conf").read_text(encoding="utf-8")
        self.assertIn("port = 55432", postgres)
        self.assertIn("max_connections = 16", postgres)
        redis = (ROOT / "config/redis/redis.conf.template").read_text(encoding="utf-8")
        self.assertIn("port 6381", redis)
        self.assertIn("maxmemory 64mb", redis)
        self.assertIn("maxclients 32", redis)

    def test_installer_postgres_and_redis_share_exact_parent_budget(self):
        properties = {
            ("sse-qa.slice", "CPUQuotaPerSecUSec"): "1s",
            ("sse-qa.slice", "CPUQuotaPeriodUSec"): "100ms",
            ("sse-qa.slice", "MemoryHigh"): str(1792 * 1024**2),
            ("sse-qa.slice", "MemoryMax"): str(2 * 1024**3),
            ("sse-qa.slice", "MemorySwapMax"): "0",
            ("sse-qa.slice", "TasksMax"): "256",
            ("sse-qa.slice", "ControlGroup"): "/sse.slice/sse-qa.slice",
        }
        for unit in (
            "sse-qa-install.service",
            "postgresql@16-sseqa.service",
            "redis-sse-qa.service",
        ):
            properties[(unit, "Slice")] = "sse-qa.slice"
            properties[(unit, "ControlGroup")] = f"/sse.slice/sse-qa.slice/{unit}"

        with mock.patch.object(
            ctl, "_systemctl_property", side_effect=lambda unit, name: properties[(unit, name)]
        ), mock.patch.object(
            ctl, "_current_unified_cgroup", return_value="/sse.slice/sse-qa.slice/sse-qa-install.service"
        ):
            ctl.assert_install_scope()
            ctl._assert_unit_in_qa_slice("postgresql@16-sseqa.service")
            ctl._assert_unit_in_qa_slice("redis-sse-qa.service")

    def test_operation_scope_rejects_process_outside_real_systemd_hierarchy(self):
        parent = "/sse.slice/sse-qa.slice"
        unit = "sse-qa-install.service"
        properties = {
            ("sse-qa.slice", "CPUQuotaPerSecUSec"): "1s",
            ("sse-qa.slice", "CPUQuotaPeriodUSec"): "100ms",
            ("sse-qa.slice", "MemoryHigh"): str(1792 * 1024**2),
            ("sse-qa.slice", "MemoryMax"): str(2 * 1024**3),
            ("sse-qa.slice", "MemorySwapMax"): "0",
            ("sse-qa.slice", "TasksMax"): "256",
            ("sse-qa.slice", "ControlGroup"): parent,
            (unit, "Slice"): "sse-qa.slice",
            (unit, "ControlGroup"): f"{parent}/{unit}",
        }
        with mock.patch.object(
            ctl, "_systemctl_property", side_effect=lambda target, name: properties[(target, name)]
        ), mock.patch.object(
            ctl, "_current_unified_cgroup", return_value="/system.slice/foreign.service"
        ):
            with self.assertRaisesRegex(ctl.QaError, "operation process is outside"):
                ctl._assert_operation_scope(unit)

    def test_unit_cgroup_rejects_similar_but_non_child_path(self):
        parent = "/sse.slice/sse-qa.slice"
        unit = "redis-sse-qa.service"
        properties = {
            ("sse-qa.slice", "ControlGroup"): parent,
            (unit, "Slice"): "sse-qa.slice",
            (unit, "ControlGroup"): f"{parent}-foreign/{unit}",
        }
        with mock.patch.object(
            ctl, "_systemctl_property", side_effect=lambda target, name: properties[(target, name)]
        ):
            with self.assertRaisesRegex(ctl.QaError, "cgroup membership mismatch"):
                ctl._assert_unit_in_qa_slice(unit)

    def test_exact_parent_budget_rejects_wrong_cpu_quota(self):
        values = {
            "CPUQuotaPerSecUSec": "2s",
            "CPUQuotaPeriodUSec": "100ms",
            "MemoryHigh": str(1792 * 1024**2),
            "MemoryMax": str(2 * 1024**3),
            "MemorySwapMax": "0",
            "TasksMax": "256",
            "ControlGroup": "/sse.slice/sse-qa.slice",
        }
        with mock.patch.object(
            ctl, "_systemctl_property", side_effect=lambda unit, name: values[name]
        ):
            with self.assertRaisesRegex(ctl.QaError, "exactly one CPU"):
                ctl._assert_qa_slice_limits()

    def test_bootstrap_slice_lifecycle_preserves_only_successful_install(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "bundle"
            source = bundle / "config/systemd/sse-qa.slice"
            source.parent.mkdir(parents=True)
            source.write_bytes((ROOT / "config/systemd/sse-qa.slice").read_bytes())
            runtime = root / "run/systemd/system/sse-qa.slice"
            persistent = root / "etc/systemd/system/sse-qa.slice"
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(receiver, "SSE_QA_RUNTIME_SLICE_PATH", runtime), mock.patch.object(
                receiver, "SSE_QA_PERSISTENT_SLICE_PATH", persistent
            ), mock.patch.object(receiver, "run", side_effect=fake_run), mock.patch.object(
                receiver.os, "chown", create=True
            ), mock.patch.object(receiver.os, "chmod"):
                staged_digest = receiver._stage_sse_qa_runtime_slice(bundle)
                self.assertTrue(runtime.is_file())
                receiver._cleanup_sse_qa_runtime_slice(staged_digest, keep_installed=False)
                self.assertFalse(runtime.exists())
                self.assertIn(["systemctl", "stop", "sse-qa.slice"], calls)

                calls.clear()
                staged_digest = receiver._stage_sse_qa_runtime_slice(bundle)
                persistent.parent.mkdir(parents=True)
                persistent.write_bytes(source.read_bytes())
                receiver._cleanup_sse_qa_runtime_slice(staged_digest, keep_installed=True)
                self.assertFalse(runtime.exists())
                self.assertNotIn(["systemctl", "stop", "sse-qa.slice"], calls)

    def test_nginx_is_closed_and_first_stage_is_two_sse_clients(self):
        nginx = (ROOT / "config/nginx/sse-qa.conf.template").read_text(encoding="utf-8")
        for required in (
            "auth_basic", "allow @@ALLOW_CIDR@@", "deny all",
            "limit_conn sse_qa_total 2", "proxy_buffering off",
            "server_name sse-qa.driverform.ru",
        ):
            self.assertIn(required, nginx)

    def test_secret_contract_is_exact(self):
        parsed = ctl.validate_secrets(json.dumps(valid_secrets()).encode())
        self.assertEqual(parsed["schema"], 1)
        for mutation in (
            {**valid_secrets(), "command": "id"},
            {**valid_secrets(), "driver_pin": "123"},
            {**valid_secrets(), "allow_cidr": "not-a-cidr"},
            {**valid_secrets(), "redis_password": "short"},
        ):
            with self.assertRaises((ctl.QaError, ValueError)):
                ctl.validate_secrets(json.dumps(mutation).encode())

    def test_local_render_has_no_placeholders_and_no_production_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            test_root = Path(raw)
            ctl.install_local_layout(
                ROOT, test_root, valid_secrets(), sealed_credentials=sealed_credentials(),
            )
            rendered = test_root / "etc/sse-qa/app.env"
            self.assertTrue(rendered.is_file())
            self.assertNotIn("@@", rendered.read_text(encoding="utf-8"))
            self.assertFalse((test_root / "etc/accounting-mvp.env").exists())
            self.assertFalse((test_root / "srv/accounting-mvp").exists())
            marker = test_root / "var/lib/sse-qa/INSTALLATION_MARKER"
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), ctl.MARKER)

    def test_receiver_accepts_only_fixed_sse_qa_targets(self):
        for mode in receiver.SSE_QA_MODES:
            accepted = receiver.validate_target(receiver.SSE_QA_PACKAGE_PAYLOAD, mode)
            self.assertEqual(accepted.as_posix(), receiver.SSE_QA_PACKAGE_PAYLOAD)
            with self.assertRaises(receiver.ReleaseError):
                receiver.validate_target("deploy/sse-qa/command.sh", mode)
        self.assertEqual(
            receiver.validate_target(receiver.SSE_QA_SECRETS_PAYLOAD, "install_sse_qa").as_posix(),
            receiver.SSE_QA_SECRETS_PAYLOAD,
        )
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_target(receiver.SSE_QA_SECRETS_PAYLOAD, "enable_sse_qa")

    def test_receiver_contract_rejects_extra_payload_or_metadata(self):
        package = b"PK-placeholder"
        manifest = {"mode": "verify_sse_qa", "metadata": qa_metadata()}
        receiver.validate_mode_contract(manifest, {receiver.SSE_QA_PACKAGE_PAYLOAD: package})
        with self.assertRaises(receiver.ReleaseError):
            bad_metadata = qa_metadata()
            bad_metadata["command"] = "id"
            receiver.validate_mode_contract(
                {"mode": "verify_sse_qa", "metadata": bad_metadata},
                {receiver.SSE_QA_PACKAGE_PAYLOAD: package},
            )

    def test_release_builder_emits_only_fixed_qa_payloads(self):
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            package = temp / "qa.zip"
            package.write_bytes(b"PK-placeholder")
            output = temp / "release.tar.gz"
            command = [
                sys.executable, str(CONTROL / ".github/deploy/build_release.py"),
                "--root", str(CONTROL),
                "--files", str(ROOT / "REQUIRED_FILES.txt"),
                "--output", str(output), "--commit", "a" * 40,
                "--mode", "verify_sse_qa", "--sse-qa-package", str(package),
                "--sse-qa-candidate-commit", receiver.SSE_QA_CANDIDATE_COMMIT,
                "--sse-qa-controller-sha256", receiver.SSE_QA_CONTROLLER_SHA256,
                "--sse-qa-runtime-sha256", receiver.SSE_QA_RUNTIME_SHA256,
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            manifest, payload = receiver.load_release(output)
            self.assertEqual(manifest["metadata"], qa_metadata())
            self.assertEqual(set(payload), {receiver.SSE_QA_PACKAGE_PAYLOAD})

            secrets = temp / "secrets.json"
            secrets.write_text(json.dumps(valid_secrets()), encoding="utf-8")
            output2 = temp / "install.tar.gz"
            command[command.index("verify_sse_qa")] = "install_sse_qa"
            command[command.index(str(output))] = str(output2)
            command.extend(["--sse-qa-secrets", str(secrets)])
            subprocess.run(command, check=True, capture_output=True, text=True)
            _, payload2 = receiver.load_release(output2)
            self.assertEqual(
                set(payload2),
                {receiver.SSE_QA_PACKAGE_PAYLOAD, receiver.SSE_QA_SECRETS_PAYLOAD},
            )

            output3 = temp / "smoke.tar.gz"
            command[command.index("install_sse_qa")] = "smoke_sse_qa"
            command[command.index(str(output2))] = str(output3)
            secrets_flag = command.index("--sse-qa-secrets")
            del command[secrets_flag:secrets_flag + 2]
            subprocess.run(command, check=True, capture_output=True, text=True)
            manifest3, payload3 = receiver.load_release(output3)
            self.assertEqual(manifest3["mode"], "smoke_sse_qa")
            self.assertEqual(set(payload3), {receiver.SSE_QA_PACKAGE_PAYLOAD})

    def test_event_loop_sampler_writes_same_loop_samples(self):
        sampler = load("sse_qa_lag_test", ROOT / "app-overlay/config/sse_event_loop_lag.py")
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "lag.jsonl"

            async def scenario():
                task = asyncio.create_task(sampler._sample_forever(path, 0.01))
                await asyncio.sleep(0.075)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(scenario())
            samples = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertGreaterEqual(len(samples), 2)
            self.assertTrue(all(item["schema"] == 1 and item["lag_ms"] >= 0 for item in samples))
            summary = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts/summarize_event_loop_lag.py"), str(path),
                    "--start-unix-ns", str(min(item["unix_ns"] for item in samples)),
                    "--end-unix-ns", str(max(item["unix_ns"] for item in samples) + 1),
                ],
                check=True, capture_output=True, text=True,
            )
            parsed = json.loads(summary.stdout)
            self.assertEqual(parsed["samples"], len(samples))
            self.assertEqual(parsed["workers"], 1)

    def test_real_mutations_are_not_available_under_test_root(self):
        old = os.environ.get("SSE_QA_LOCAL_TEST")
        os.environ["SSE_QA_LOCAL_TEST"] = "1"
        try:
            with tempfile.TemporaryDirectory() as raw:
                with self.assertRaises(ctl.QaError):
                    ctl.main(["enable", "--test-root", raw])
        finally:
            if old is None:
                os.environ.pop("SSE_QA_LOCAL_TEST", None)
            else:
                os.environ["SSE_QA_LOCAL_TEST"] = old

    def test_runtime_archive_is_backend_only_and_rejects_traversal(self):
        ctl.validate_runtime_archive(ROOT / "generated/runtime.tar.gz")
        with tarfile.open(ROOT / "generated/runtime.tar.gz", "r:gz") as archive:
            names = [member.name for member in archive.getmembers()]
        self.assertFalse(
            any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names)
        )
        with tempfile.TemporaryDirectory() as raw:
            bad = Path(raw) / "bad.tar.gz"
            payload = Path(raw) / "payload"
            payload.write_text("bad", encoding="utf-8")
            with tarfile.open(bad, "w:gz") as archive:
                archive.add(payload, arcname="../production.env")
            with self.assertRaises(ctl.QaError):
                ctl.validate_runtime_archive(bad)

    def test_preflight_rejects_every_managed_path_before_writing(self):
        with mock.patch.object(ctl, "port_is_free", return_value=True):
            for managed in ctl.MANAGED_CONFLICT_PATHS:
                with self.subTest(path=str(managed)), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    target = ctl.rooted(root, managed)
                    if managed in {ctl.APP_ROOT, ctl.STATE_ROOT, ctl.ETC_ROOT, Path("/etc/postgresql/16/sseqa")}:
                        target.mkdir(parents=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("foreign", encoding="utf-8")
                    with self.assertRaisesRegex(ctl.QaError, "conflicting QA paths"):
                        ctl.preflight(root)
                    self.assertTrue(target.exists())

    def test_rooted_preserves_literal_posix_backslash(self):
        logical = "/etc/systemd/system/srv-sse\\x2dqa.mount"
        target = ctl.rooted(PurePosixPath("/sandbox"), logical)
        self.assertEqual(
            target,
            PurePosixPath("/sandbox/etc/systemd/system/srv-sse\\x2dqa.mount"),
        )
        self.assertEqual(target.name, "srv-sse\\x2dqa.mount")

    @unittest.skipUnless(os.name == "posix", "literal backslash filenames require POSIX")
    def test_mount_unit_conflict_copy_verify_remove_uses_exact_linux_name(self):
        logical = "/etc/systemd/system/srv-sse\\x2dqa.mount"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            exact_target = root / "etc" / "systemd" / "system" / "srv-sse\\x2dqa.mount"
            exact_target.parent.mkdir(parents=True)
            exact_target.write_text("foreign", encoding="utf-8")
            with mock.patch.object(ctl, "port_is_free", return_value=True):
                with self.assertRaisesRegex(ctl.QaError, "conflicting QA paths"):
                    ctl.preflight(root)
            exact_target.unlink()

            ctl.rooted(root, ctl.STATE_ROOT).mkdir(parents=True)
            state = ctl.new_ownership()
            ctl.save_ownership(state, root)
            ctl.install_local_layout(
                ROOT, root, valid_secrets(), ownership=state,
                sealed_credentials=sealed_credentials(),
            )
            source = ROOT / "config/systemd/srv-sse-x2dqa.mount"
            ctl._copy_owned(source, logical, state, root)
            state["complete"] = True
            state["phase"] = "complete_disabled"
            ctl.save_ownership(state, root)

            with mock.patch.object(ctl, "port_is_free", return_value=True):
                self.assertIn("owned_file_hashes", ctl.verify_installation(root))
            self.assertEqual(exact_target.read_bytes(), source.read_bytes())

            # Runtime payload cleanup is outside this exact-name contract; leave
            # the controller only the empty owned app root it already handles.
            app_root = ctl.rooted(root, ctl.APP_ROOT)
            for child in sorted(app_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if child.is_dir():
                    child.rmdir()
            old_root = ctl.REAL_ROOT
            ctl.REAL_ROOT = root
            try:
                completed = subprocess.CompletedProcess([], 0, "inactive\n", "")
                with mock.patch.object(ctl, "run", return_value=completed):
                    ctl.cleanup_owned_installation(state, remove_complete=True)
            finally:
                ctl.REAL_ROOT = old_root
            self.assertFalse(exact_target.exists())

    def test_cleanup_foreign_mount_fails_before_destructive_calls(self):
        old_root = ctl.REAL_ROOT
        try:
            with tempfile.TemporaryDirectory() as raw:
                ctl.REAL_ROOT = Path(raw)
                ctl.rooted(ctl.REAL_ROOT, ctl.STATE_ROOT).mkdir(parents=True)
                state = ctl.new_ownership()
                state["postgres_cluster_created"] = True
                state["mount_started"] = True
                ctl.save_ownership(state)
                with (
                    mock.patch.object(ctl, "_postgres_cluster_status", return_value="match"),
                    mock.patch.object(ctl, "_mount_status", return_value="mismatch"),
                    mock.patch.object(ctl, "run") as run,
                ):
                    with self.assertRaisesRegex(ctl.QaError, "QA mount source mismatch"):
                        ctl.cleanup_owned_installation(state, remove_complete=True)
                run.assert_not_called()
        finally:
            ctl.REAL_ROOT = old_root

    def test_cleanup_changed_owned_file_fails_before_destructive_calls(self):
        old_root = ctl.REAL_ROOT
        try:
            with tempfile.TemporaryDirectory() as raw:
                ctl.REAL_ROOT = Path(raw)
                ctl.rooted(ctl.REAL_ROOT, ctl.STATE_ROOT).mkdir(parents=True)
                state = ctl.new_ownership()
                ctl.save_ownership(state)
                logical = "/etc/sse-qa/example"
                target = ctl.rooted(ctl.REAL_ROOT, logical)
                target.parent.mkdir(parents=True)
                target.write_text("owned", encoding="utf-8")
                ctl.journal_file(state, logical)
                target.write_text("foreign-change", encoding="utf-8")
                with mock.patch.object(ctl, "run") as run:
                    with self.assertRaisesRegex(ctl.QaError, "owned file changed"):
                        ctl.cleanup_owned_installation(state, remove_complete=True)
                run.assert_not_called()
                self.assertEqual(target.read_text(encoding="utf-8"), "foreign-change")
        finally:
            ctl.REAL_ROOT = old_root

    def test_cleanup_rechecks_guard_after_stopping_services(self):
        old_root = ctl.REAL_ROOT
        try:
            with tempfile.TemporaryDirectory() as raw:
                ctl.REAL_ROOT = Path(raw)
                ctl.rooted(ctl.REAL_ROOT, ctl.STATE_ROOT).mkdir(parents=True)
                state = ctl.new_ownership()
                state["postgres_cluster_created"] = True
                state["mount_started"] = True
                ctl.save_ownership(state)
                completed = subprocess.CompletedProcess([], 0, "", "")
                with (
                    mock.patch.object(ctl, "_postgres_cluster_status", side_effect=("match", "match")),
                    mock.patch.object(ctl, "_mount_status", side_effect=("match", "mismatch")),
                    mock.patch.object(ctl, "run", return_value=completed) as run,
                ):
                    with self.assertRaisesRegex(ctl.QaError, "QA mount source mismatch"):
                        ctl.cleanup_owned_installation(state, remove_complete=True)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(commands, [["systemctl", "stop", *ctl.SERVICE_UNITS]])
                self.assertFalse(any(command[0] == "pg_dropcluster" for command in commands))
        finally:
            ctl.REAL_ROOT = old_root

    def test_local_installed_verification_checks_hashes_and_phase(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(ctl, "port_is_free", return_value=True):
            root = Path(raw)
            ctl.rooted(root, ctl.STATE_ROOT).mkdir(parents=True)
            state = ctl.new_ownership()
            ctl.save_ownership(state, root)
            ctl.install_local_layout(
                ROOT, root, valid_secrets(), ownership=state,
                sealed_credentials=sealed_credentials(),
            )
            state["complete"] = True
            state["phase"] = "complete_disabled"
            ctl.save_ownership(state, root)
            checks = ctl.verify_installation(root)
            self.assertIn("owned_file_hashes", checks)
            redis = ctl.rooted(root, Path("/etc/sse-qa/redis.conf"))
            redis.write_text(redis.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ctl.QaError, "owned file changed"):
                ctl.verify_installation(root)

    def test_redis_acl_is_memfd_only_and_canonical_equipment_types(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ctl.install_local_layout(
                ROOT, root, valid_secrets(), sealed_credentials=sealed_credentials(),
            )
            self.assertFalse(ctl.rooted(root, Path("/etc/sse-qa/redis.acl")).exists())
            redis_unit = (ROOT / "config/systemd/redis-sse-qa.service").read_text(encoding="utf-8")
            self.assertIn("LoadCredentialEncrypted=redis_password:", redis_unit)
            self.assertIn("sse-qa-redis-launcher", redis_unit)
        seed = (ROOT / "app-overlay/users/management/commands/seed_sse_qa.py").read_text(encoding="utf-8")
        self.assertIn('get_or_create(name="Самосвал")', seed)
        self.assertIn('get_or_create(name="Экскаватор")', seed)
        self.assertNotIn('get_or_create(name="Экскаватор SSE QA")', seed)
        self.assertIn("--business-smoke", seed)
        self.assertIn("SSE_QA_BUSINESS_SMOKE_OK", seed)

    def test_preinstall_systemd_verify_substitutes_only_runtime_dependencies(self):
        observed = {}

        def fake_run(command, **kwargs):
            observed["command"] = command
            texts = [Path(path).read_text(encoding="utf-8") for path in command[2:]]
            observed["texts"] = texts
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(ctl, "run", side_effect=fake_run):
            ctl.verify_linux_units(ROOT, runtime_ready=False)
        combined = "\n".join(observed["texts"])
        self.assertIn("ExecStart=/bin/true", combined)
        self.assertNotRegex(combined, r"(?m)^ExecStart=/srv/sse-qa/venv/")
        self.assertIn("CPUQuota=100%", combined)
        self.assertIn("MemoryMax=2G", combined)

    def test_owned_file_guard_refuses_changed_object(self):
        old_root = ctl.REAL_ROOT
        try:
            with tempfile.TemporaryDirectory() as raw:
                ctl.REAL_ROOT = Path(raw)
                state = ctl.new_ownership()
                ctl.rooted(ctl.REAL_ROOT, ctl.STATE_ROOT).mkdir(parents=True)
                ctl.save_ownership(state)
                target = ctl.rooted(ctl.REAL_ROOT, Path("/etc/sse-qa/example"))
                target.parent.mkdir(parents=True)
                target.write_text("owned", encoding="utf-8")
                ctl.journal_file(state, Path("/etc/sse-qa/example"), ctl.REAL_ROOT)
                self.assertTrue(ctl._owned_file_may_remove(state, Path("/etc/sse-qa/example")))
                target.write_text("foreign-change", encoding="utf-8")
                self.assertFalse(ctl._owned_file_may_remove(state, Path("/etc/sse-qa/example")))
        finally:
            ctl.REAL_ROOT = old_root

    def test_fault_injection_is_fail_closed(self):
        with mock.patch.dict(os.environ, {"SSE_QA_FAULT_AT": "after_image_before_marker", "SSE_QA_FAULT_INJECTION": "1"}, clear=False):
            with mock.patch.object(Path, "is_file", return_value=False):
                with self.assertRaisesRegex(ctl.QaError, "forbidden"):
                    ctl.fault_injection("after_image_before_marker")
            with mock.patch.object(Path, "is_file", return_value=True):
                with self.assertRaisesRegex(ctl.QaError, "injected failure"):
                    ctl.fault_injection("after_image_before_marker")

    def test_control_package_needs_no_runtime_or_package_index(self):
        controller = b"print('controller')\n"
        payload_buffer = BytesIO()
        with zipfile.ZipFile(payload_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("scripts/sse_qa_ctl.py", controller)

        class FakeProcess:
            returncode = 0

            def __init__(self, command, **kwargs):
                self.command = command

            def communicate(self, timeout=None):
                return "SSE_QA_DISABLE_OK data_preserved=true complete=false\n", None

        with mock.patch.object(
            receiver, "SSE_QA_CONTROLLER_SHA256", hashlib.sha256(controller).hexdigest()
        ), mock.patch.object(receiver.subprocess, "Popen", FakeProcess):
            result = receiver.run_sse_qa(
                "disable_sse_qa",
                {receiver.SSE_QA_PACKAGE_PAYLOAD: payload_buffer.getvalue()},
            )
        self.assertTrue(result.startswith("SSE_QA_DISABLE_OK"))
        workflow = (CONTROL / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
        control_branch = workflow.split('if [[ "$MODE" == "install_sse_qa" || "$MODE" == "verify_sse_qa" ]]', 1)[1]
        self.assertIn('cp "$qa_source/scripts/sse_qa_ctl.py"', control_branch)
        self.assertNotIn("pip download", control_branch.split("fi\n            args+=", 1)[0].split("else", 1)[1])

    def test_installed_smoke_process_is_child_of_shared_slice(self):
        controller = b"print('controller')\n"
        payload_buffer = BytesIO()
        with zipfile.ZipFile(payload_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("scripts/sse_qa_ctl.py", controller)
        commands: list[list[str]] = []

        class FakeProcess:
            returncode = 0
            pid = 123

            def __init__(self, command, **kwargs):
                commands.append(command)

            def communicate(self, timeout=None):
                return "SSE_QA_SMOKE_OK clients=2 synthetic_trip=1 delivery=1\n", None

        with mock.patch.object(
            receiver, "_receiver_unified_cgroup", return_value="/system.slice/receiver.service"
        ), mock.patch.object(
            receiver, "_sse_qa_slice_cgroup", return_value=receiver.SSE_QA_SLICE_CGROUP
        ), mock.patch.object(
            receiver, "SSE_QA_CONTROLLER_SHA256", hashlib.sha256(controller).hexdigest()
        ), mock.patch.object(receiver.subprocess, "Popen", FakeProcess):
            result = receiver.run_sse_qa(
                "smoke_sse_qa",
                {receiver.SSE_QA_PACKAGE_PAYLOAD: payload_buffer.getvalue()},
            )

        self.assertTrue(result.startswith("SSE_QA_SMOKE_OK"))
        self.assertIn("--unit=sse-qa-smoke.service", commands[0])
        self.assertIn("--slice=sse-qa.slice", commands[0])
        self.assertNotIn("receiver.service", " ".join(commands[0]))

    def test_receiver_rejects_actual_qa_slice_and_similar_prefix_is_not_a_child(self):
        parent = "/sse.slice/sse-qa.slice"
        self.assertTrue(receiver._cgroup_is_at_or_below(parent, parent))
        self.assertTrue(receiver._cgroup_is_at_or_below(f"{parent}/receiver.service", parent))
        self.assertFalse(receiver._cgroup_is_at_or_below("/system.slice/receiver.service", parent))
        self.assertFalse(receiver._cgroup_is_at_or_below("/sse.slice/sse-qa.slice-other/x", parent))

        controller = b"print('controller')\n"
        payload_buffer = BytesIO()
        with zipfile.ZipFile(payload_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("scripts/sse_qa_ctl.py", controller)
        with mock.patch.object(
            receiver, "_receiver_unified_cgroup", return_value=f"{parent}/receiver.service"
        ), mock.patch.object(
            receiver, "_sse_qa_slice_cgroup", return_value=parent
        ), mock.patch.object(
            receiver, "SSE_QA_CONTROLLER_SHA256", hashlib.sha256(controller).hexdigest()
        ), mock.patch.object(receiver.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(receiver.ReleaseError, "receiver must remain outside"):
                receiver.run_sse_qa(
                    "smoke_sse_qa",
                    {receiver.SSE_QA_PACKAGE_PAYLOAD: payload_buffer.getvalue()},
                )
        popen.assert_not_called()

    def test_receiver_reads_exact_slice_control_group_and_disposable_checks_child(self):
        parent = "/sse.slice/sse-qa.slice"
        started = subprocess.CompletedProcess([], 0, "", "")
        completed = subprocess.CompletedProcess([], 0, parent + "\n", "")
        with mock.patch.object(receiver, "run", side_effect=(started, completed)) as run:
            self.assertEqual(receiver._sse_qa_slice_cgroup(), parent)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["systemctl", "start", "sse-qa.slice"],
                ["systemctl", "show", "sse-qa.slice", "--property", "ControlGroup", "--value"],
            ],
        )
        wrong = subprocess.CompletedProcess([], 0, "/sse-qa.slice\n", "")
        with mock.patch.object(receiver, "run", side_effect=(started, wrong)):
            with self.assertRaisesRegex(receiver.ReleaseError, "cgroup hierarchy mismatch"):
                receiver._sse_qa_slice_cgroup()
        start_failed = subprocess.CompletedProcess([], 1, "", "start failed")
        with mock.patch.object(receiver, "run", return_value=start_failed) as run:
            with self.assertRaisesRegex(receiver.ReleaseError, "could not be activated"):
                receiver._sse_qa_slice_cgroup()
        run.assert_called_once_with(["systemctl", "start", "sse-qa.slice"], check=False)

        disposable = (ROOT / "scripts/linux_disposable_cycle.sh").read_text(encoding="utf-8")
        self.assertIn("systemctl start sse-qa.slice\n  assert_slice_limits", disposable)
        self.assertIn('test "$(systemctl show sse-qa.slice -p ControlGroup --value)" = "$QA_CGROUP"', disposable)
        self.assertIn('test "$(systemctl show "$unit" -p ControlGroup --value)" = "$QA_CGROUP/$unit"', disposable)
        self.assertNotIn('ControlGroup --value)" = /sse-qa.slice', disposable)

    def test_disposable_cycle_has_executable_normal_fault_cancel_evidence_gates(self):
        disposable = (ROOT / "scripts/linux_disposable_cycle.sh").read_text(encoding="utf-8")
        diagnostics = (ROOT / "scripts/linux_install_diagnostics.sh").read_text(encoding="utf-8")
        executable_cycle = disposable + "\n" + diagnostics
        for required in (
            "wait_install_checkpoint dependencies_started",
            "ActiveState --value",
            "MainPID --value",
            "ownership_phase",
            'test "$proc_cgroup" = "$QA_CGROUP/$INSTALL_UNIT"',
            "after_image_before_marker after_postgres_redis_start",
            "SSE_QA_CANCEL_HOLD_READY point=dependencies_started",
            "SSE_QA_NETWORK_LOGIN_OK logins=2 screens=2 https=1 nginx=1 basic_auth=1",
            "SSE_QA_BUSINESS_SMOKE_OK logins=2 screens=2",
            "trip_id=[0-9]+ version=[0-9]+ catchup=1 sse=1",
            "--start-unix-ns", "--end-unix-ns", "EVENT_LOOP_WINDOW_OK",
            "linux_redis_metrics.py",
            "emergency-exit",
        ):
            self.assertIn(required, executable_cycle)
        self.assertNotIn("sleep 2", disposable)
        self.assertNotIn("ps -eo pid=,ppid=,user=,cgroup=,args=", disposable)

    def test_zero_residue_gate_covers_all_fixed_resource_classes_without_argv(self):
        scanner = (ROOT / "scripts/linux_zero_residue_scan.sh").read_text(encoding="utf-8")
        for required in (
            "units-loaded", "unit-files", "processes", "users", "groups", "clusters",
            "mounts", "loops", "listeners", "cgroups", "managed_paths",
            "ZERO_RESIDUE_OK", "55432|6381|18080|18082", "inspection-$probe",
        ):
            self.assertIn(required, scanner)
        self.assertIn("pid=,ppid=,user=,cgroup=,comm=", scanner)
        self.assertNotIn("args=", scanner)
        self.assertNotIn("/proc/*/environ", scanner)

    def test_network_login_gate_has_fixed_synthetic_roles_and_no_secret_output(self):
        source = (ROOT / "scripts/linux_network_login_smoke.py").read_text(encoding="utf-8")
        for required in (
            '"basic_auth_password", "driver_pin", "excavator_pin"',
            '"+79000000001"', '"/driver/"',
            '"+79000000002"', '"/excavator/work/"',
            '"csrfmiddlewaretoken"', '"Authorization"',
            "SSE_QA_NETWORK_LOGIN_OK logins=2 screens=2 https=1 nginx=1 basic_auth=1",
        ):
            self.assertIn(required, source)
        self.assertNotIn("print(password", source)
        self.assertNotIn("print(pin", source)
        redis_metrics = (ROOT / "scripts/linux_redis_metrics.py").read_text(encoding="utf-8")
        self.assertIn('command(sock, "AUTH", parsed.username, parsed.password)', redis_metrics)
        self.assertIn('command(sock, "CLIENT", "LIST")', redis_metrics)
        self.assertIn('"connected_clients"', redis_metrics)
        self.assertNotIn("print(parsed.password", redis_metrics)

    def test_enable_process_is_child_of_shared_slice(self):
        controller = b"print('controller')\n"
        payload_buffer = BytesIO()
        with zipfile.ZipFile(payload_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("scripts/sse_qa_ctl.py", controller)
        commands: list[list[str]] = []

        class FakeProcess:
            returncode = 0
            pid = 124

            def __init__(self, command, **kwargs):
                commands.append(command)

            def communicate(self, timeout=None):
                return "SSE_QA_ENABLE_OK max_sse_clients=2\n", None

        with mock.patch.object(
            receiver, "_receiver_unified_cgroup", return_value="/system.slice/receiver.service"
        ), mock.patch.object(
            receiver, "_sse_qa_slice_cgroup", return_value=receiver.SSE_QA_SLICE_CGROUP
        ), mock.patch.object(
            receiver, "SSE_QA_CONTROLLER_SHA256", hashlib.sha256(controller).hexdigest()
        ), mock.patch.object(receiver.subprocess, "Popen", FakeProcess):
            result = receiver.run_sse_qa(
                "enable_sse_qa",
                {receiver.SSE_QA_PACKAGE_PAYLOAD: payload_buffer.getvalue()},
            )

        self.assertTrue(result.startswith("SSE_QA_ENABLE_OK"))
        self.assertIn("--unit=sse-qa-enable.service", commands[0])
        self.assertIn("--slice=sse-qa.slice", commands[0])
        self.assertNotIn("receiver.service", " ".join(commands[0]))

    def test_install_enable_and_smoke_use_shared_parent_slice_but_preflight_does_not(self):
        receiver_source = (CONTROL / "deployment/server/accounting_github_deploy_receiver.py").read_text(encoding="utf-8")
        for required in (
            'scoped_unit = "sse-qa-install.service"',
            'scoped_unit = "sse-qa-enable.service"',
            'scoped_unit = "sse-qa-smoke.service"',
            'f"--slice={SSE_QA_SLICE_UNIT}"', '"--property=CPUQuota=100%"',
            '"--property=MemoryMax=2G"', '"--property=MemorySwapMax=0"',
            '"--property=TasksMax=256"', 'SSE_QA_RUNTIME_SLICE_PATH',
            'os.killpg(process.pid, signal.SIGTERM)',
        ):
            self.assertIn(required, receiver_source)
        self.assertNotIn('scoped_unit = "sse-qa-verify.service"', receiver_source)
        self.assertIn('"verify_sse_qa": "preflight"', receiver_source)
        for service in ("sse-qa-wsgi.service", "sse-qa-asgi.service", "sse-qa-reconcile.service"):
            text = (ROOT / "config/systemd" / service).read_text(encoding="utf-8")
            self.assertIn("StandardOutput=append:/srv/sse-qa/log/", text)
            self.assertIn("StandardError=append:/srv/sse-qa/log/", text)

    def test_patch_secret_scan_ignores_removed_lines_but_checks_added_lines(self):
        with tempfile.TemporaryDirectory() as raw:
            patch = Path(raw) / "review.patch"
            patch.write_bytes(
                b"--- a/test\n+++ b/test\n-PRIVATE " b"KEY----- old-fixture\n+safe replacement\n"
            )
            self.assertIsNone(package_checker.SECRET_PATTERN.search(package_checker.secret_scan_payload(patch)))
            patch.write_bytes(
                b"--- a/test\n+++ b/test\n-safe\n+PRIVATE " b"KEY----- leaked\n"
            )
            self.assertIsNotNone(package_checker.SECRET_PATTERN.search(package_checker.secret_scan_payload(patch)))

    def test_secret_scan_distinguishes_source_literals_from_secret_values(self):
        safe_literals = (
            b'key = "POSTGRES_PASSWORD="\n'
            b'other = "REDIS_PASSWORD="\n'
            b'fixture = "sessionid="\n'
            b'header = "Authorization: Bearer"\n'
        )
        self.assertIsNone(package_checker.SECRET_PATTERN.search(safe_literals))
        for leaked_value in (
            b"POSTGRES_" b"PASSWORD=synthetic-value",
            b"REDIS_" b"PASSWORD=!synthetic-value",
            b"session" b"id=abc123",
            b"Authorization: " b"Bearer token.value",
        ):
            with self.subTest(leaked_value=leaked_value):
                self.assertIsNotNone(package_checker.SECRET_PATTERN.search(leaked_value))


if __name__ == "__main__":
    unittest.main()
