from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_module("build_release", Path(__file__).with_name("build_release.py"))
release_audit = load_module(
    "audit_release_scope",
    Path(__file__).with_name("audit_release_scope.py"),
)
receiver = load_module(
    "accounting_github_deploy_receiver",
    ROOT / "deployment" / "server" / "accounting_github_deploy_receiver.py",
)
sse_qa_ctl = load_module(
    "sse_qa_ctl",
    ROOT / "deployment" / "server" / "sse_qa_ctl.py",
)


class CredentialPathProbe:
    def __init__(
        self,
        *,
        file: bool = True,
        symlink: bool = False,
        uid: int = 0,
        mode: int = 0o100600,
        name: str = "probe",
    ) -> None:
        self.file = file
        self.symlink = symlink
        self.uid = uid
        self.mode = mode
        self.name = name

    def __fspath__(self) -> str:
        return self.name

    def is_file(self) -> bool:
        return self.file

    def is_symlink(self) -> bool:
        return self.symlink

    def stat(self, *, follow_symlinks: bool = True):
        if follow_symlinks:
            raise AssertionError("host key metadata must not follow symlinks")
        return type("Metadata", (), {"st_uid": self.uid, "st_mode": self.mode})()


class SseQaHostKeyPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executable = CredentialPathProbe(name="systemd-creds")
        self.host_key = CredentialPathProbe(name="credential.secret")

    def verify(self) -> None:
        with mock.patch.object(sse_qa_ctl.os, "access", return_value=True):
            sse_qa_ctl.verify_systemd_credential_prerequisites(
                executable=self.executable,
                host_key=self.host_key,
            )

    def test_preflight_rejects_missing_systemd_creds_executable(self):
        self.executable.file = False
        with self.assertRaisesRegex(
            sse_qa_ctl.QaError, "systemd-creds executable is unavailable"
        ):
            self.verify()

    def test_preflight_rejects_missing_systemd_host_key(self):
        self.host_key.file = False
        with self.assertRaisesRegex(
            sse_qa_ctl.QaError, "systemd host credential key is unavailable"
        ):
            self.verify()

    def test_preflight_rejects_symlinked_systemd_host_key(self):
        self.host_key.symlink = True
        with self.assertRaisesRegex(
            sse_qa_ctl.QaError, "systemd host credential key is unavailable"
        ):
            self.verify()

    def test_preflight_rejects_unsafe_systemd_host_key_owner_or_mode(self):
        for uid, mode in ((1, 0o100600), (0, 0o100640), (0, 0o100604)):
            with self.subTest(uid=uid, mode=oct(mode)):
                self.host_key.uid = uid
                self.host_key.mode = mode
                with self.assertRaisesRegex(
                    sse_qa_ctl.QaError,
                    "systemd host credential key ownership or mode is unsafe",
                ):
                    self.verify()

    def test_valid_systemd_host_key_check_has_no_mutating_side_effects(self):
        with mock.patch.object(sse_qa_ctl.os, "access", return_value=True), \
             mock.patch.object(sse_qa_ctl, "run") as run, \
             mock.patch.object(sse_qa_ctl.subprocess, "run") as subprocess_run, \
             mock.patch.object(sse_qa_ctl.subprocess, "Popen") as popen, \
             mock.patch.object(sse_qa_ctl.os, "chmod") as chmod, \
             mock.patch.object(sse_qa_ctl.os, "chown", create=True) as chown:
            sse_qa_ctl.verify_systemd_credential_prerequisites(
                executable=self.executable,
                host_key=self.host_key,
            )
        run.assert_not_called()
        subprocess_run.assert_not_called()
        popen.assert_not_called()
        chmod.assert_not_called()
        chown.assert_not_called()

    def test_initial_real_preflight_requires_host_key_after_existing_gates(self):
        with mock.patch.object(
            sse_qa_ctl, "preflight", return_value=["existing_conflict_checks"]
        ) as preflight, mock.patch.object(
            sse_qa_ctl, "verify_systemd_credential_prerequisites"
        ) as credential_gate:
            self.assertEqual(
                sse_qa_ctl.initial_preflight(sse_qa_ctl.REAL_ROOT),
                ["existing_conflict_checks", "systemd_host_credential_key"],
            )
        preflight.assert_called_once_with(sse_qa_ctl.REAL_ROOT, installed_ok=False)
        credential_gate.assert_called_once_with()

        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            sse_qa_ctl, "preflight", return_value=["local_test_root"]
        ), mock.patch.object(
            sse_qa_ctl, "verify_systemd_credential_prerequisites"
        ) as credential_gate:
            self.assertEqual(
                sse_qa_ctl.initial_preflight(Path(raw)),
                ["local_test_root"],
            )
        credential_gate.assert_not_called()

    def test_existing_preflight_failure_stops_before_host_key_gate(self):
        with mock.patch.object(
            sse_qa_ctl,
            "preflight",
            side_effect=sse_qa_ctl.QaError("loopback port conflict"),
        ), mock.patch.object(
            sse_qa_ctl, "verify_systemd_credential_prerequisites"
        ) as credential_gate:
            with self.assertRaisesRegex(sse_qa_ctl.QaError, "loopback port conflict"):
                sse_qa_ctl.initial_preflight(sse_qa_ctl.REAL_ROOT)
        credential_gate.assert_not_called()

    def test_preflight_cli_routes_only_to_read_only_gates(self):
        with mock.patch.object(
            sse_qa_ctl, "verify_linux_units"
        ) as verify_units, mock.patch.object(
            sse_qa_ctl,
            "initial_preflight",
            return_value=["existing_conflicts", "systemd_host_credential_key"],
        ) as initial_preflight, mock.patch.object(
            sse_qa_ctl, "run"
        ) as run, mock.patch.object(
            sse_qa_ctl, "real_install"
        ) as install, mock.patch.object(
            sse_qa_ctl, "real_enable"
        ) as enable, mock.patch.object(
            sse_qa_ctl, "real_disable"
        ) as disable, mock.patch.object(
            sse_qa_ctl, "real_remove"
        ) as remove, mock.patch("builtins.print") as output:
            self.assertEqual(
                sse_qa_ctl.main(
                    ["preflight", "--bundle-root", str(ROOT / "deployment")]
                ),
                0,
            )
        verify_units.assert_called_once_with((ROOT / "deployment").resolve(), runtime_ready=False)
        initial_preflight.assert_called_once_with(sse_qa_ctl.REAL_ROOT)
        run.assert_not_called()
        install.assert_not_called()
        enable.assert_not_called()
        disable.assert_not_called()
        remove.assert_not_called()
        output.assert_called_once_with(
            "SSE_QA_PREFLIGHT_OK existing_conflicts,systemd_host_credential_key"
        )


class ReleaseProtocolTests(unittest.TestCase):
    def test_production_manifest_paths_are_unique_and_exist(self):
        production_files = release_audit.read_manifest(ROOT)

        self.assertEqual(len(production_files), len(set(production_files)))
        self.assertEqual(
            [path for path in production_files if not (ROOT / path).is_file()],
            [],
        )

    def test_dispatcher_shell_and_runtime_assets_are_consistent(self):
        result = release_audit.validate_dispatcher_shell(
            ROOT,
            release_audit.read_manifest(ROOT),
        )

        self.assertRegex(
            result["shell_version"],
            re.compile(r"^dispatcher-desktop-shell-v\d+$"),
        )
        self.assertGreaterEqual(result["template_runtime_assets"], 10)
        self.assertGreaterEqual(result["service_worker_static_assets"], 20)

    def test_release_scope_classifier_keeps_only_server_payload(self):
        non_production = (
            ".github/deploy/test_release_protocol.py",
            "ПРОГРЕСС_ПРОЕКТА/210_ПАСПОРТ.md",
            "СИСТЕМА_MVP/backend/static/js/tests/dispatcher.test.js",
            "СИСТЕМА_MVP/backend/trips/test_dispatcher_guards.py",
            "СИСТЕМА_MVP/backend/trips/tests.py",
            "СИСТЕМА_MVP/backend/tools/audit_dispatcher_css.cjs",
        )
        production = (
            "СИСТЕМА_MVP/backend/trips/views.py",
            "СИСТЕМА_MVP/backend/trips/dispatcher_guards.py",
            "СИСТЕМА_MVP/backend/static/js/dispatcher-control-v1.js",
            "СИСТЕМА_MVP/backend/templates/trips/dispatcher_control.html",
        )

        for path in non_production:
            with self.subTest(path=path):
                self.assertTrue(release_audit.is_non_production_path(path))
        for path in production:
            with self.subTest(path=path):
                self.assertFalse(release_audit.is_non_production_path(path))

    def test_dispatcher_downtime_projection_is_packaged_once(self):
        expected = (
            "СИСТЕМА_MVP/backend/trips/dispatcher_downtime_projection.py"
        )
        production_files = (
            ROOT / ".github" / "deploy" / "production-files.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(production_files.count(expected), 1)

    def test_dispatcher_canvas_assets_are_packaged_once(self):
        production_files = (
            ROOT / ".github" / "deploy" / "production-files.txt"
        ).read_text(encoding="utf-8").splitlines()
        for expected in (
            "СИСТЕМА_MVP/backend/static/css/dispatcher-canvas-v1.css",
            "СИСТЕМА_MVP/backend/static/js/dispatcher-canvas-v1.js",
        ):
            with self.subTest(asset=expected):
                self.assertEqual(production_files.count(expected), 1)

    def test_dispatcher_equipment_detail_include_is_packaged_once(self):
        expected = (
            "СИСТЕМА_MVP/backend/templates/trips/includes/"
            "dispatcher_equipment_detail.html"
        )
        production_files = (
            ROOT / ".github" / "deploy" / "production-files.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(production_files.count(expected), 1)

    def test_dispatcher_service_lists_include_is_packaged_once(self):
        expected = (
            "СИСТЕМА_MVP/backend/templates/trips/includes/"
            "dispatcher_service_lists.html"
        )
        production_files = (
            ROOT / ".github" / "deploy" / "production-files.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(production_files.count(expected), 1)

    def test_dispatcher_board_include_is_packaged_once(self):
        expected = (
            "СИСТЕМА_MVP/backend/templates/trips/includes/dispatcher_board.html"
        )
        production_files = (
            ROOT / ".github" / "deploy" / "production-files.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(production_files.count(expected), 1)

    def test_dispatcher_push_include_is_packaged_once(self):
        expected = (
            "СИСТЕМА_MVP/backend/templates/trips/includes/"
            "dispatcher_push_invite.html"
        )
        production_files = (
            ROOT / ".github" / "deploy" / "production-files.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(production_files.count(expected), 1)

    def test_dispatcher_url_routing_is_packaged_once(self):
        expected = "СИСТЕМА_MVP/backend/trips/urls.py"
        production_files = (
            ROOT / ".github" / "deploy" / "production-files.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(production_files.count(expected), 1)

    def diagnostic_metadata(self):
        return {
            "operation": "trip_accounting_incident_v1",
            "equipment": "EXC-TEST-9",
            "from_utc": "2026-01-01T00:00:00Z",
            "to_utc": "2026-01-01T02:00:00Z",
            "max_rows": 500,
        }

    def diagnostic_report(self):
        metadata = self.diagnostic_metadata()
        sections = {
            name: {"total": 0, "returned": 0, "truncated": False, "rows": []}
            for name in receiver.DIAGNOSTIC_ALLOWED_SECTION_FIELDS
        }
        sections["trips"] = {
            "total": 1,
            "returned": 1,
            "truncated": False,
            "rows": [{"id": 1201, "status": "completed"}],
        }
        return {
            "schema": 1,
            "operation": metadata["operation"],
            "request": {
                "equipment": metadata["equipment"],
                "from_utc": metadata["from_utc"],
                "to_utc": metadata["to_utc"],
                "max_rows": metadata["max_rows"],
            },
            "database": {
                "vendor": "postgresql",
                "transaction_read_only": True,
                "transaction_isolation": "repeatable read",
            },
            "resolution": "resolved",
            "equipment": {
                "id": 9,
                "garage_number": "EXC-TEST-9",
                "equipment_type": "Экскаватор",
                "is_active": True,
            },
            "sections": sections,
            "summary": {"row_count": 1, "truncated": False},
        }

    def infra_metadata(self):
        return {"operation": "infra_capacity_v1"}

    def infra_report(self):
        service = {
            "load_state": "loaded",
            "active_state": "active",
            "sub_state": "running",
            "main_pid": 123,
            "restarts": 0,
            "cpu_usage_ns": 123456789,
            "memory_current_bytes": 268435456,
            "memory_peak_bytes": 300000000,
            "tasks_current": 8,
            "tasks_max": 256,
            "limit_nofile": 4096,
        }
        redis_ok = {
            "status": "ok",
            "port": 6379,
            "version": "7.2.4",
            "uptime_seconds": 86400,
            "connected_clients": 12,
            "blocked_clients": 0,
            "maxclients": 10000,
            "used_memory_bytes": 8388608,
            "used_memory_peak_bytes": 12582912,
            "maxmemory_bytes": 67108864,
            "maxmemory_policy": "noeviction",
            "instantaneous_ops_per_sec": 25,
            "rejected_connections": 0,
            "evicted_keys": 0,
            "pubsub_channels": 4,
            "acl_details_returned": False,
            "channel_names_returned": False,
        }
        redis_auth = {
            "status": "auth_required",
            "port": 6381,
            "version": None,
            "uptime_seconds": None,
            "connected_clients": None,
            "blocked_clients": None,
            "maxclients": None,
            "used_memory_bytes": None,
            "used_memory_peak_bytes": None,
            "maxmemory_bytes": None,
            "maxmemory_policy": None,
            "instantaneous_ops_per_sec": None,
            "rejected_connections": None,
            "evicted_keys": None,
            "pubsub_channels": None,
            "acl_details_returned": False,
            "channel_names_returned": False,
        }
        return {
            "schema": 1,
            "operation": "infra_capacity_v1",
            "request": {},
            "scope": {
                "sample_started_utc": "2026-09-27T00:00:00Z",
                "sample_finished_utc": "2026-09-27T00:00:05Z",
                "sample_seconds": 5.1,
                "historical_window_available": False,
                "application_event_loop_probe_available": False,
            },
            "host": {
                "logical_cpu_count": 8,
                "cpu_usage_percent_samples": [10.0, 20.0, 30.0, 40.0, 50.0],
                "cpu_usage_percent_average": 30.0,
                "cpu_usage_percent_maximum": 50.0,
                "load_average": {"one_minute": 1.25, "five_minutes": 1.0, "fifteen_minutes": 0.75},
                "uptime_seconds": 123456.0,
                "memory_bytes": {
                    "total": 17179869184,
                    "available": 8589934592,
                    "swap_total": 2147483648,
                    "swap_free": 2147483648,
                },
                "pressure": {
                    "cpu": {"avg10": 0.1, "avg60": 0.2, "avg300": 0.3, "total_us": 1000},
                    "memory": None,
                    "io": None,
                },
                "file_handles": {"allocated": 1024, "maximum": 1048576},
                "disk_bytes": {
                    "root": {"total": 1000, "used": 400, "free": 600},
                    "application": {"total": 1000, "used": 400, "free": 600},
                },
            },
            "services": {
                "accounting_mvp": dict(service),
                "nginx": dict(service),
                "postgresql": dict(service),
                "redis_server": dict(service),
            },
            "postgresql": {
                "status": "ok",
                "server_version_num": 160004,
                "transaction_read_only": True,
                "transaction_isolation": "read_committed",
                "max_connections": 100,
                "reserved_connections_supported": True,
                "reserved_connections": 5,
                "superuser_reserved_connections": 3,
                "role_connection_limit": -1,
                "server_process_rows": 22,
                "observed_client_backend_connections": 20,
                "rows_with_unknown_backend_type": 2,
                "client_backend_count_complete": False,
                "database_process_rows": 14,
                "observed_database_client_connections": 12,
                "database_rows_with_unknown_backend_type": 1,
                "database_rows_with_known_nonclient_backend_type": 1,
                "database_client_backend_count_complete": False,
                "observed_database_client_connections_with_visible_details": 10,
                "observed_database_client_connections_with_hidden_details": 2,
                "observed_database_client_connections_with_disabled_tracking": 0,
                "activity_details_visibility": "partial",
                "visible_active_database_client_connections": 3,
                "visible_idle_in_transaction_database_client_connections": 0,
                "visible_lock_waiting_database_client_connections": 0,
                "locks": 25,
                "ungranted_locks": 0,
                "database_size_bytes": 1073741824,
                "visible_oldest_transaction_seconds": 0.125,
            },
            "redis": {"6379": redis_ok, "6381": redis_auth},
            "summary": {"row_count": 0, "truncated": False},
            "limitations": list(receiver.INFRA_CAPACITY_LIMITATIONS),
        }

    def test_plain_deploy_rejects_migration_but_migration_mode_accepts_it(self):
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_target("trips/migrations/0013_example.py", "deploy")
        target = receiver.validate_target(
            "trips/migrations/0013_example.py", "deploy_migrations"
        )
        self.assertEqual(target.as_posix(), "trips/migrations/0013_example.py")

    def test_data_mode_is_limited_to_data_update_directory(self):
        accepted = receiver.validate_target(
            "deploy/data_updates/employees_20260916.py", "apply_data"
        )
        self.assertEqual(
            accepted.as_posix(), "deploy/data_updates/employees_20260916.py"
        )
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_target("users/views.py", "apply_data")

    def test_receiver_mode_accepts_only_the_receiver_payload(self):
        accepted = receiver.validate_target(
            "deploy/receiver/accounting_github_deploy_receiver.py",
            "update_receiver",
        )
        self.assertEqual(accepted.as_posix(), receiver.RECEIVER_PAYLOAD)
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_target("deploy/other.py", "update_receiver")

    def test_apk_contract_requires_exact_role_version_url_and_sha(self):
        apk = b"PK\x03\x04signed-apk-placeholder"
        update = {
            "schemaVersion": 1,
            "profile": "driver",
            "versionCode": 999,
            "versionName": "9.9.9",
            "apkUrl": "https://driverform.ru/media/apk/driver-9.9.9.apk",
            "sha256": receiver.digest(apk),
            "releaseNotes": "test",
        }
        payload = {
            "media/apk/driver-9.9.9.apk": apk,
            "media/apk/driver-update.json": json.dumps(update).encode(),
        }
        parsed = receiver.validate_apk_payload(
            "driver", "media/apk/driver-9.9.9.apk", payload
        )
        self.assertEqual(parsed["versionCode"], 999)
        payload["media/apk/driver-9.9.9.apk"] += b"changed"
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_apk_payload(
                "driver", "media/apk/driver-9.9.9.apk", payload
            )

    def test_builder_loads_only_apk_named_by_generated_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            apk = b"PK\x03\x04apk"
            (dist / "excavator-1.2.3.apk").write_bytes(apk)
            (dist / "excavator-update.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "profile": "excavator",
                        "versionCode": 123,
                        "versionName": "1.2.3",
                        "apkUrl": "https://driverform.ru/media/apk/excavator-1.2.3.apk",
                        "sha256": builder.sha256(apk),
                    }
                ),
                encoding="utf-8",
            )
            paths = builder.load_apk_paths(dist, "excavator")
            self.assertEqual(
                [target.as_posix() for target, _ in paths],
                [
                    "media/apk/excavator-1.2.3.apk",
                    "media/apk/excavator-update.json",
                ],
            )

    def test_diagnostic_metadata_is_strictly_allowlisted_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            inputs = {
                "diagnostic_operation": "trip_accounting_incident_v1",
                "diagnostic_equipment": "EXC-TEST-9",
                "diagnostic_from_utc": "2026-01-01T00:00:00Z",
                "diagnostic_to_utc": "2026-01-01T02:00:00Z",
                "diagnostic_max_rows": "500",
            }
            event_path.write_text(json.dumps({"inputs": inputs}), encoding="utf-8")
            self.assertEqual(builder.load_diagnostic_metadata(event_path), self.diagnostic_metadata())

            invalid_cases = (
                ("diagnostic_operation", "sql"),
                ("diagnostic_equipment", "../../etc/passwd"),
                ("diagnostic_from_utc", "2026-01-01 00:00"),
                ("diagnostic_to_utc", "2026-01-02T02:00:00Z"),
                ("diagnostic_max_rows", "501"),
            )
            for field, value in invalid_cases:
                with self.subTest(field=field, value=value):
                    candidate = dict(inputs)
                    candidate[field] = value
                    event_path.write_text(json.dumps({"inputs": candidate}), encoding="utf-8")
                    with self.assertRaises(SystemExit):
                        builder.load_diagnostic_metadata(event_path)

    def test_infra_capacity_metadata_has_no_free_form_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            inputs = {
                "diagnostic_operation": "infra_capacity_v1",
                "diagnostic_equipment": "",
                "diagnostic_from_utc": "",
                "diagnostic_to_utc": "",
                "diagnostic_max_rows": "500",
            }
            event_path.write_text(json.dumps({"inputs": inputs}), encoding="utf-8")
            self.assertEqual(builder.load_diagnostic_metadata(event_path), self.infra_metadata())
            self.assertEqual(receiver.validate_diagnostic_metadata(self.infra_metadata()), self.infra_metadata())
            receiver.validate_mode_contract({
                "schema": 2,
                "mode": "diagnose",
                "commit": "a" * 40,
                "files": [],
                "metadata": self.infra_metadata(),
            }, {})

            for field, value in (
                ("diagnostic_equipment", "EXC-1"),
                ("diagnostic_from_utc", "2026-09-27T00:00:00Z"),
                ("diagnostic_to_utc", "2026-09-27T01:00:00Z"),
                ("diagnostic_max_rows", "100"),
            ):
                with self.subTest(field=field):
                    candidate = dict(inputs)
                    candidate[field] = value
                    event_path.write_text(json.dumps({"inputs": candidate}), encoding="utf-8")
                    with self.assertRaises(SystemExit):
                        builder.load_diagnostic_metadata(event_path)
            with self.assertRaises(receiver.ReleaseError):
                receiver.validate_diagnostic_metadata({"operation": "infra_capacity_v1", "command": "id"})

    def test_infra_capacity_package_contains_only_the_fixed_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_path = root / "event.json"
            package = root / "infra-capacity.tar.gz"
            files_path = root / "files.txt"
            files_path.write_text("", encoding="utf-8")
            event_path.write_text(json.dumps({"inputs": {
                "diagnostic_operation": "infra_capacity_v1",
                "diagnostic_equipment": "",
                "diagnostic_from_utc": "",
                "diagnostic_to_utc": "",
                "diagnostic_max_rows": "500",
            }}), encoding="utf-8")
            argv = [
                "build_release.py", "--root", str(root), "--files", str(files_path),
                "--output", str(package), "--commit", "b" * 40, "--mode", "diagnose",
                "--event-file", str(event_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(sys, "stdout", new_callable=io.StringIO) as captured,
            ):
                builder.main()
            self.assertEqual(captured.getvalue(), "PACKAGE_READY mode=diagnose files=0\n")
            with tarfile.open(package, "r:gz") as archive:
                self.assertEqual(archive.getnames(), ["release-manifest.json"])
            manifest, payload = receiver.load_release(package)
            self.assertEqual(manifest["metadata"], self.infra_metadata())
            self.assertEqual(payload, {})

    def test_diagnostic_package_has_no_payload_or_executable_operation(self):
        manifest = {
            "schema": 2,
            "mode": "diagnose",
            "commit": "a" * 40,
            "files": [],
            "metadata": self.diagnostic_metadata(),
        }
        receiver.validate_mode_contract(manifest, {})
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_target("trips/models.py", "diagnose")
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_mode_contract(manifest, {"deploy/data_updates/read.py": b"print(1)"})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_path = root / "event.json"
            package = root / "diagnostic.tar.gz"
            inputs = {
                "diagnostic_operation": "trip_accounting_incident_v1",
                "diagnostic_equipment": "EXC-TEST-9",
                "diagnostic_from_utc": "2026-01-01T00:00:00Z",
                "diagnostic_to_utc": "2026-01-01T02:00:00Z",
                "diagnostic_max_rows": "500",
            }
            event_path.write_text(json.dumps({"inputs": inputs}), encoding="utf-8")
            files_path = root / "files.txt"
            files_path.write_text("", encoding="utf-8")
            argv = [
                "build_release.py", "--root", str(root), "--files", str(files_path),
                "--output", str(package), "--commit", "a" * 40, "--mode", "diagnose",
                "--event-file", str(event_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(sys, "stdout", new_callable=io.StringIO) as captured,
            ):
                builder.main()
            output = captured.getvalue()
            self.assertEqual(output, "PACKAGE_READY mode=diagnose files=0\n")
            self.assertNotIn("SHA256", output)
            self.assertNotIn(inputs["diagnostic_equipment"], output)
            self.assertNotIn(inputs["diagnostic_from_utc"], output)
            with tarfile.open(package, "r:gz") as archive:
                self.assertEqual(archive.getnames(), ["release-manifest.json"])
            loaded_manifest, payload = receiver.load_release(package)
            self.assertEqual(loaded_manifest["metadata"], self.diagnostic_metadata())
            self.assertEqual(payload, {})

    def test_diagnostic_report_requires_read_only_proof_and_sanitized_fields(self):
        report = self.diagnostic_report()
        raw = json.dumps(report, ensure_ascii=False).encode("utf-8")
        parsed = receiver.validate_diagnostic_report(raw, self.diagnostic_metadata())
        self.assertEqual(parsed["summary"]["row_count"], 1)

        unsafe_reports = []
        no_read_only = json.loads(json.dumps(report, ensure_ascii=False))
        no_read_only["database"]["transaction_read_only"] = False
        unsafe_reports.append(no_read_only)
        leaked_url = json.loads(json.dumps(report, ensure_ascii=False))
        leaked_url["sections"]["trips"]["rows"][0]["source"] = "https://private.invalid/path"
        unsafe_reports.append(leaked_url)
        leaked_secret = json.loads(json.dumps(report, ensure_ascii=False))
        leaked_secret["sections"]["trips"]["rows"][0]["session_key"] = "secret-session"
        unsafe_reports.append(leaked_secret)
        unapproved_field = json.loads(json.dumps(report, ensure_ascii=False))
        unapproved_field["sections"]["trips"]["rows"][0]["harmless_but_unknown"] = "value"
        unsafe_reports.append(unapproved_field)
        too_many = json.loads(json.dumps(report, ensure_ascii=False))
        too_many["summary"]["row_count"] = 501
        unsafe_reports.append(too_many)
        for candidate in unsafe_reports:
            with self.assertRaises(receiver.ReleaseError):
                receiver.validate_diagnostic_report(
                    json.dumps(candidate, ensure_ascii=False).encode("utf-8"),
                    self.diagnostic_metadata(),
                )

    def test_infra_capacity_report_is_exact_bounded_and_contains_no_sensitive_details(self):
        report = self.infra_report()
        parsed = receiver.validate_diagnostic_report(
            json.dumps(report).encode("utf-8"),
            self.infra_metadata(),
        )
        self.assertEqual(parsed["host"]["logical_cpu_count"], 8)
        self.assertEqual(parsed["postgresql"]["observed_database_client_connections"], 12)
        self.assertEqual(parsed["postgresql"]["activity_details_visibility"], "partial")

        unsafe_reports = []
        historical = json.loads(json.dumps(report))
        historical["scope"]["historical_window_available"] = True
        unsafe_reports.append(historical)
        writable = json.loads(json.dumps(report))
        writable["postgresql"]["transaction_read_only"] = False
        unsafe_reports.append(writable)
        redis_acl = json.loads(json.dumps(report))
        redis_acl["redis"]["6379"]["acl_details_returned"] = True
        unsafe_reports.append(redis_acl)
        leaked_path = json.loads(json.dumps(report))
        leaked_path["services"]["nginx"]["path"] = "/etc/nginx/nginx.conf"
        unsafe_reports.append(leaked_path)
        inconsistent_cpu = json.loads(json.dumps(report))
        inconsistent_cpu["host"]["cpu_usage_percent_average"] = 99.0
        unsafe_reports.append(inconsistent_cpu)
        for candidate in unsafe_reports:
            with self.assertRaises(receiver.ReleaseError):
                receiver.validate_diagnostic_report(
                    json.dumps(candidate).encode("utf-8"),
                    self.infra_metadata(),
                )

    def test_infra_capacity_disk_space_accepts_reserved_blocks_but_rejects_impossible_values(self):
        report = self.infra_report()
        report["host"]["disk_bytes"]["root"] = {"total": 1000, "used": 400, "free": 500}
        receiver.validate_diagnostic_report(json.dumps(report).encode(), self.infra_metadata())

        no_reserve = json.loads(json.dumps(report))
        no_reserve["host"]["disk_bytes"]["root"] = {"total": 1000, "used": 400, "free": 600}
        receiver.validate_diagnostic_report(json.dumps(no_reserve).encode(), self.infra_metadata())

        for disk in (
            {"total": 1000, "used": -1, "free": 500},
            {"total": 1000, "used": 600, "free": 500},
        ):
            invalid = json.loads(json.dumps(report))
            invalid["host"]["disk_bytes"]["root"] = disk
            with self.assertRaises(receiver.ReleaseError):
                receiver.validate_diagnostic_report(json.dumps(invalid).encode(), self.infra_metadata())

    def test_infra_capacity_cpu_totals_do_not_double_count_guest_time(self):
        tree = ast.parse(receiver.INFRA_CAPACITY_SOURCE)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "cpu_totals"
        )
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<cpu-totals>", "exec"), namespace)

        zero_guest = "cpu 100 0 0 100 20 0 0 10 0 0\n"
        nonzero_guest = "cpu 100 0 0 100 20 0 0 10 50 25\n"
        values = []
        for sample in (zero_guest, nonzero_guest):
            namespace["read_text"] = lambda path, maximum, sample=sample: sample
            total, idle = namespace["cpu_totals"]()
            values.append((total, idle, round(100 * (total - idle) / total, 3)))
        self.assertEqual(values, [(230, 120, 47.826), (230, 120, 47.826)])
        self.assertIn("iowait is treated as idle; steal remains busy", receiver.INFRA_CAPACITY_SOURCE)

    def test_infra_capacity_postgresql_visibility_and_reserve_semantics_are_explicit(self):
        partial = self.infra_report()
        receiver.validate_diagnostic_report(json.dumps(partial).encode(), self.infra_metadata())
        postgres = partial["postgresql"]
        self.assertNotIn("cluster_connections", postgres)
        self.assertFalse(postgres["client_backend_count_complete"])
        self.assertFalse(postgres["database_client_backend_count_complete"])
        self.assertEqual(postgres["superuser_reserved_connections"], 3)
        self.assertEqual(postgres["reserved_connections"], 5)

        full = json.loads(json.dumps(partial))
        full_postgres = full["postgresql"]
        full_postgres["server_process_rows"] = 20
        full_postgres["rows_with_unknown_backend_type"] = 0
        full_postgres["client_backend_count_complete"] = True
        full_postgres["database_process_rows"] = 12
        full_postgres["database_rows_with_unknown_backend_type"] = 0
        full_postgres["database_rows_with_known_nonclient_backend_type"] = 0
        full_postgres["database_client_backend_count_complete"] = True
        full_postgres["observed_database_client_connections_with_visible_details"] = 12
        full_postgres["observed_database_client_connections_with_hidden_details"] = 0
        full_postgres["activity_details_visibility"] = "full"
        receiver.validate_diagnostic_report(json.dumps(full).encode(), self.infra_metadata())

        disabled = json.loads(json.dumps(full))
        disabled_postgres = disabled["postgresql"]
        disabled_postgres["observed_database_client_connections_with_visible_details"] = 11
        disabled_postgres["observed_database_client_connections_with_disabled_tracking"] = 1
        disabled_postgres["activity_details_visibility"] = "partial"
        receiver.validate_diagnostic_report(json.dumps(disabled).encode(), self.infra_metadata())

        pre_v16 = json.loads(json.dumps(full))
        pre_v16["postgresql"]["server_version_num"] = 150000
        pre_v16["postgresql"]["reserved_connections_supported"] = False
        pre_v16["postgresql"]["reserved_connections"] = None
        receiver.validate_diagnostic_report(json.dumps(pre_v16).encode(), self.infra_metadata())

        for mutate in (
            lambda item: item["postgresql"].update(activity_details_visibility="full"),
            lambda item: item["postgresql"].update(reserved_connections_supported=False),
            lambda item: item["postgresql"].update(client_backend_count_complete=True),
            lambda item: item["postgresql"].update(database_process_rows=13),
        ):
            invalid = json.loads(json.dumps(partial))
            mutate(invalid)
            with self.assertRaises(receiver.ReleaseError):
                receiver.validate_diagnostic_report(json.dumps(invalid).encode(), self.infra_metadata())
        self.assertIn("backend_type = 'client backend'", receiver.INFRA_CAPACITY_SOURCE)
        self.assertIn("state IS NULL", receiver.INFRA_CAPACITY_SOURCE)

    def test_infra_capacity_postgresql_producer_predicates_preserve_unknown_backend_rows(self):
        tree = ast.parse(receiver.INFRA_CAPACITY_SOURCE)
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "POSTGRES_ACTIVITY_COUNT_SQL" for target in node.targets)
        )
        sql = ast.literal_eval(assignment.value).replace("current_database()", "'appdb'")
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.execute(
            "CREATE TABLE pg_stat_activity "
            "(backend_type TEXT, datname TEXT, state TEXT, wait_event_type TEXT)"
        )
        database.executemany(
            "INSERT INTO pg_stat_activity VALUES (?, ?, ?, ?)",
            (
                ("client backend", "appdb", "active", None),
                (None, "appdb", None, None),
                ("parallel worker", "appdb", "active", None),
            ),
        )
        row = database.execute(sql).fetchone()
        self.assertEqual(row[:10], (3, 1, 1, 3, 1, 1, 1, 1, 0, 0))

        report = self.infra_report()
        postgres = report["postgresql"]
        postgres.update({
            "server_process_rows": row[0],
            "observed_client_backend_connections": row[1],
            "rows_with_unknown_backend_type": row[2],
            "client_backend_count_complete": False,
            "database_process_rows": row[3],
            "observed_database_client_connections": row[4],
            "database_rows_with_unknown_backend_type": row[5],
            "database_rows_with_known_nonclient_backend_type": row[6],
            "database_client_backend_count_complete": False,
            "observed_database_client_connections_with_visible_details": row[7],
            "observed_database_client_connections_with_hidden_details": row[8],
            "observed_database_client_connections_with_disabled_tracking": row[9],
            "activity_details_visibility": "partial",
            "visible_active_database_client_connections": row[10],
            "visible_idle_in_transaction_database_client_connections": row[11],
            "visible_lock_waiting_database_client_connections": row[12],
        })
        receiver.validate_diagnostic_report(json.dumps(report).encode(), self.infra_metadata())

    def test_infra_capacity_rejects_nonfinite_numbers_and_python_equality_type_aliases(self):
        variants = []
        sample_nan = self.infra_report()
        sample_nan["scope"]["sample_seconds"] = float("nan")
        variants.append(sample_nan)
        average_nan = self.infra_report()
        average_nan["host"]["cpu_usage_percent_average"] = float("nan")
        variants.append(average_nan)
        uptime_infinity = self.infra_report()
        uptime_infinity["host"]["uptime_seconds"] = float("inf")
        variants.append(uptime_infinity)
        boolean_schema = self.infra_report()
        boolean_schema["schema"] = True
        variants.append(boolean_schema)
        float_port = self.infra_report()
        float_port["redis"]["6379"]["port"] = 6379.0
        variants.append(float_port)
        boolean_rows = self.infra_report()
        boolean_rows["summary"]["row_count"] = False
        variants.append(boolean_rows)
        integer_truncated = self.infra_report()
        integer_truncated["summary"]["truncated"] = 0
        variants.append(integer_truncated)

        for candidate in variants:
            with self.assertRaises(receiver.ReleaseError):
                receiver.validate_diagnostic_report(
                    json.dumps(candidate).encode("utf-8"),
                    self.infra_metadata(),
                )

    def test_infra_capacity_helper_uses_only_fixed_local_probes(self):
        compile(receiver.INFRA_CAPACITY_SOURCE, "<infra-capacity>", "exec")
        source = receiver.INFRA_CAPACITY_SOURCE
        self.assertIn('SERVICE_ALLOWLIST = (', source)
        for value in (
            '"accounting-mvp.service"', '"nginx.service"', '"postgresql.service"',
            '"redis-server.service"', 'REDIS_PORTS = (6379, 6381)',
            'socket.create_connection(("127.0.0.1", port)',
            'cursor.execute("SET TRANSACTION READ ONLY")',
        ):
            self.assertIn(value, source)
        for forbidden in ("shell=True", "os.system", "os.popen", "eval(", "exec(", "nginx -T", "/etc/"):
            self.assertNotIn(forbidden, source)
        for forbidden in ("from trips", "from users", "from assignments", "SELECT *", "pg_read_file"):
            self.assertNotIn(forbidden, source)

    def test_fixed_diagnostic_helper_enforces_postgresql_read_only_and_timeouts(self):
        compile(receiver.DIAGNOSTIC_QUERY_SOURCE, "<diagnostic-query>", "exec")
        self.assertIn("with transaction.atomic():", receiver.DIAGNOSTIC_QUERY_SOURCE)
        self.assertIn(
            'cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")',
            receiver.DIAGNOSTIC_QUERY_SOURCE,
        )
        self.assertIn("statement_timeout = '12000ms'", receiver.DIAGNOSTIC_QUERY_SOURCE)
        self.assertIn("lock_timeout = '2000ms'", receiver.DIAGNOSTIC_QUERY_SOURCE)
        self.assertIn('cursor.execute("SHOW transaction_isolation")', receiver.DIAGNOSTIC_QUERY_SOURCE)
        self.assertIn("AdminConflict.objects.filter", receiver.DIAGNOSTIC_QUERY_SOURCE)
        self.assertIn('operational_scope = Q(event_type="test_shift_data_reset")', receiver.DIAGNOSTIC_QUERY_SOURCE)
        self.assertIn("operationally_closed_at__isnull=True", receiver.DIAGNOSTIC_QUERY_SOURCE)
        self.assertIn('Q(status="accepted")', receiver.DIAGNOSTIC_QUERY_SOURCE)
        self.assertNotIn('Q(status__in=("requested", "accepted", "used"))', receiver.DIAGNOSTIC_QUERY_SOURCE)
        self.assertEqual(receiver.DIAGNOSTIC_QUERY_SOURCE.count("cursor.execute("), 5)
        self.assertNotIn("subprocess", receiver.DIAGNOSTIC_QUERY_SOURCE)

    def test_client_error_hash_uses_the_raw_message_before_sanitizing(self):
        tree = ast.parse(receiver.DIAGNOSTIC_QUERY_SOURCE)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "raw_text_sha256"
        )
        namespace = {"hashlib": hashlib}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<raw-hash>", "exec"), namespace)
        raw_hash = namespace["raw_text_sha256"]
        for message in ("https://internal.invalid/path", "X" * 500):
            self.assertEqual(raw_hash(message), hashlib.sha256(message.encode()).hexdigest())
        self.assertIn('raw_row[field + "_sha256"] = raw_text_sha256(raw_row.pop(field, None))',
                      receiver.DIAGNOSTIC_QUERY_SOURCE)

    def test_diagnostic_execution_has_process_timeout_and_returns_only_validated_json(self):
        manifest = {"metadata": self.diagnostic_metadata()}
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(self.diagnostic_report(), ensure_ascii=False).encode("utf-8"),
            stderr=b"",
        )
        with mock.patch.object(receiver.subprocess, "run", return_value=completed) as execute:
            result = receiver.run_diagnostic(manifest)
        self.assertEqual(result["summary"]["row_count"], 1)
        self.assertEqual(execute.call_args.kwargs["timeout"], receiver.DIAGNOSTIC_PROCESS_TIMEOUT_SECONDS)
        self.assertEqual(execute.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(execute.call_args.kwargs["user"], receiver.DIAGNOSTIC_OS_USER)
        self.assertEqual(execute.call_args.kwargs["group"], receiver.DIAGNOSTIC_OS_GROUP)
        self.assertEqual(execute.call_args.kwargs["extra_groups"], ())
        self.assertEqual(execute.call_args.kwargs["umask"], 0o077)
        self.assertEqual(execute.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertIn("default_transaction_read_only=on", execute.call_args.kwargs["env"]["PGOPTIONS"])
        self.assertTrue(execute.call_args.kwargs["start_new_session"])
        command = execute.call_args.args[0]
        self.assertEqual(command[1:3], ["-c", receiver.DIAGNOSTIC_QUERY_SOURCE])
        self.assertNotIn("shell", execute.call_args.kwargs)

    def test_infra_capacity_execution_passes_no_user_parameters_to_fixed_helper(self):
        manifest = {"metadata": self.infra_metadata()}
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(self.infra_report()).encode("utf-8"),
            stderr=b"",
        )
        with mock.patch.object(receiver.subprocess, "run", return_value=completed) as execute:
            result = receiver.run_diagnostic(manifest)
        self.assertEqual(result["operation"], "infra_capacity_v1")
        command = execute.call_args.args[0]
        self.assertEqual(command, [str(receiver.APP / ".venv/bin/python"), "-c", receiver.INFRA_CAPACITY_SOURCE])
        self.assertNotIn("shell", execute.call_args.kwargs)
        self.assertEqual(execute.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(execute.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(execute.call_args.kwargs["user"], receiver.DIAGNOSTIC_OS_USER)

    def test_receiver_encrypts_report_before_returning_it_to_actions(self):
        report = self.diagnostic_report()
        ciphertext = b"\x30\x82server-encrypted-cms"
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=ciphertext, stderr=b"")
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            openssl = runtime / "openssl"
            openssl.write_bytes(b"test executable placeholder")
            with (
                mock.patch.object(receiver, "DIAGNOSTIC_OPENSSL", openssl),
                mock.patch.object(receiver, "DIAGNOSTIC_CERT_TEMP_DIR", runtime),
                mock.patch.object(receiver.subprocess, "run", return_value=completed) as execute,
            ):
                envelope = receiver.encrypt_diagnostic_report(report)
        self.assertEqual(envelope["kind"], "production_diagnostic_ciphertext")
        self.assertEqual(envelope["ciphertext_base64"], "MIJzZXJ2ZXItZW5jcnlwdGVkLWNtcw==")
        self.assertEqual(envelope["ciphertext_sha256"], receiver.digest(ciphertext))
        public_envelope = json.dumps(envelope, sort_keys=True)
        self.assertNotIn(self.diagnostic_metadata()["equipment"], public_envelope)
        self.assertNotIn(self.diagnostic_metadata()["from_utc"], public_envelope)
        self.assertEqual(execute.call_args.kwargs["input"], json.dumps(
            report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"))
        self.assertEqual(execute.call_args.kwargs["user"], receiver.DIAGNOSTIC_OS_USER)

    def test_workflow_keeps_diagnostic_parameters_and_plaintext_out_of_logs_and_artifacts(self):
        workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
        for input_name in (
            "diagnostic_operation", "diagnostic_equipment", "diagnostic_from_utc", "diagnostic_to_utc",
            "diagnostic_max_rows",
        ):
            self.assertNotIn("${{ inputs." + input_name + " }}", workflow)
        self.assertIn('--event-file "$GITHUB_EVENT_PATH"', workflow)
        self.assertNotIn("production-diagnostic-plaintext", workflow)
        self.assertIn("production-diagnostic-envelope.json", workflow)
        self.assertIn("production_diagnostic_ciphertext", workflow)
        self.assertIn("- infra_capacity_v1", workflow)
        self.assertIn('{"trip_accounting_incident_v1", "infra_capacity_v1"}', workflow)
        self.assertNotIn("openssl cms -encrypt", workflow)
        self.assertIn("production-diagnostic-${{ github.run_id }}.cms", workflow)
        certificate = (ROOT / ".github" / "deploy" / "diagnostic-recipient-cert.pem").read_text(encoding="ascii")
        self.assertTrue(certificate.startswith("-----BEGIN CERTIFICATE-----"))
        self.assertNotIn("PRIVATE " "KEY", certificate)
        self.assertEqual(certificate.encode("ascii"), receiver.DIAGNOSTIC_RECIPIENT_CERTIFICATE)
        certificate_digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest().upper()
        calculated_fingerprint = ":".join(
            certificate_digest[index:index + 2] for index in range(0, len(certificate_digest), 2)
        )
        self.assertEqual(calculated_fingerprint, receiver.DIAGNOSTIC_RECIPIENT_FINGERPRINT)
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("diagnostic-recipient-private.pem", ignore)
        self.assertIn(".github/deploy/diagnostic-recipient-private.pem", ignore)

    def test_sensitive_modes_require_control_branch_and_actions_are_sha_pinned(self):
        workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
        self.assertIn('[[ "$MODE" == "update_receiver" || "$MODE" == "diagnose" || "$MODE" == *fcm || "$MODE" == *sse_qa ]]', workflow)
        self.assertIn('test "$GITHUB_REF_NAME" = "codex/github-production-deploy-20260916"', workflow)
        uses_lines = [line.strip() for line in workflow.splitlines() if line.strip().startswith("uses:")]
        self.assertTrue(uses_lines)
        for line in uses_lines:
            reference = line.split("@", 1)[1].split()[0]
            self.assertRegex(reference, r"^[0-9a-f]{40}$")
    # SSE-QA source provenance is independent from the protected control commit.
    def test_sse_qa_source_is_fixed_separately_from_control_commit(self):
        workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn(
            f"SSE_QA_CANDIDATE_SHA: {receiver.SSE_QA_CANDIDATE_COMMIT}", workflow
        )
        self.assertIn('git fetch --no-tags --depth=1 origin "$SSE_QA_CANDIDATE_SHA"', workflow)
        self.assertIn('test "$candidate_resolved" = "$SSE_QA_CANDIDATE_SHA"', workflow)
        self.assertIn('controller_source="deployment/server/sse_qa_ctl.py"', workflow)
        self.assertIn("SSE_QA_SOURCE control_sha=%s candidate_sha=%s", workflow)
        inputs = workflow.split("permissions:", 1)[0]
        self.assertNotIn("candidate_sha:", inputs)
        self.assertNotIn("candidate_commit:", inputs)
        controller = ROOT / "deployment" / "server" / "sse_qa_ctl.py"
        controller_blob = controller.read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(controller_blob).hexdigest(), receiver.SSE_QA_CONTROLLER_SHA256)
    # Existing FCM validation remains a separate protocol contract.
    def test_fcm_mode_accepts_only_a_complete_matching_service_account(self):
        credentials = {
            "type": "service_account",
            "project_id": "copper-driver-test",
            "private_key_id": "key-id",
            "private_key": "-----BEGIN PRIVATE " "KEY-----\ntest\n-----END PRIVATE " "KEY-----\n",
            "client_email": "push@copper-driver-test.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        payload = {receiver.FCM_PAYLOAD: json.dumps(credentials).encode("utf-8")}
        manifest = {
            "mode": "configure_fcm",
            "metadata": {"project_id": "copper-driver-test"},
        }
        self.assertEqual(
            receiver.validate_target(receiver.FCM_PAYLOAD, "configure_fcm").as_posix(),
            receiver.FCM_PAYLOAD,
        )
        self.assertEqual(receiver.validate_fcm_payload(manifest, payload), credentials)
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_target("users/secret.json", "configure_fcm")
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_fcm_payload(
                {"mode": "configure_fcm", "metadata": {"project_id": "other"}},
                payload,
            )

    def test_fcm_environment_update_preserves_unrelated_values_and_is_idempotent(self):
        current = (
            b"DJANGO_SECRET_KEY=unchanged\n"
            b"DJANGO_FCM_PROJECT_ID=old\n"
            b"DJANGO_FCM_SERVICE_ACCOUNT_FILE=/old/key.json\n"
            b"OTHER=value\n"
        )
        rendered = receiver.render_fcm_env(current, "copper-driver-test")
        self.assertIn(b"DJANGO_SECRET_KEY=unchanged\n", rendered)
        self.assertIn(b"OTHER=value\n", rendered)
        self.assertIn(b"DJANGO_FCM_PROJECT_ID=copper-driver-test\n", rendered)
        self.assertIn(
            f"DJANGO_FCM_SERVICE_ACCOUNT_FILE={receiver.FCM_CONFIG_PATH}\n".encode(),
            rendered,
        )
        self.assertEqual(rendered, receiver.render_fcm_env(rendered, "copper-driver-test"))

    def test_fcm_workflow_uses_a_secret_tempfile_and_exact_confirmations(self):
        workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(encoding="utf-8")
        self.assertIn("verify_fcm) expected=VERIFY_FCM", workflow)
        self.assertIn("configure_fcm) expected=CONFIGURE_FCM", workflow)
        self.assertIn("secrets.FCM_SERVICE_ACCOUNT_JSON", workflow)
        self.assertIn('chmod 0600 "$fcm_credentials"', workflow)
        self.assertIn("trap 'rm -f \"$fcm_credentials\"' EXIT", workflow)
        self.assertNotIn("firebase-service-account.json", "\n".join(
            (ROOT / ".github" / "deploy" / "production-files.txt").read_text(encoding="utf-8").splitlines()
        ))


if __name__ == "__main__":
    unittest.main()
