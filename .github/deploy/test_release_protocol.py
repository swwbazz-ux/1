from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
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
receiver = load_module(
    "accounting_github_deploy_receiver",
    ROOT / "deployment" / "server" / "accounting_github_deploy_receiver.py",
)


class ReleaseProtocolTests(unittest.TestCase):
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
        self.assertNotIn("openssl cms -encrypt", workflow)
        self.assertIn("production-diagnostic-${{ github.run_id }}.cms", workflow)
        certificate = (ROOT / ".github" / "deploy" / "diagnostic-recipient-cert.pem").read_text(encoding="ascii")
        self.assertTrue(certificate.startswith("-----BEGIN CERTIFICATE-----"))
        self.assertNotIn("PRIVATE KEY", certificate)
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
        self.assertIn('[[ "$MODE" == "update_receiver" || "$MODE" == "diagnose" || "$MODE" == *fcm ]]', workflow)
        self.assertIn('test "$GITHUB_REF_NAME" = "codex/github-production-deploy-20260916"', workflow)
        uses_lines = [line.strip() for line in workflow.splitlines() if line.strip().startswith("uses:")]
        self.assertTrue(uses_lines)
        for line in uses_lines:
            reference = line.split("@", 1)[1].split()[0]
            self.assertRegex(reference, r"^[0-9a-f]{40}$")

    def test_fcm_mode_accepts_only_a_complete_matching_service_account(self):
        credentials = {
            "type": "service_account",
            "project_id": "copper-driver-test",
            "private_key_id": "key-id",
            "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
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
