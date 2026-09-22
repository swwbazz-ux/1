from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("validate_qa_firebase.py")
SPEC = importlib.util.spec_from_file_location("validate_qa_firebase", MODULE_PATH)
validator = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(validator)


def google_services(*packages: str, project_id: str = "copper-resources-qa") -> dict:
    return {
        "project_info": {"project_id": project_id, "project_number": "123456789"},
        "client": [
            {
                "client_info": {
                    "android_client_info": {"package_name": package},
                },
                "api_key": [{"current_key": "test-public-client-key"}],
            }
            for package in packages
        ],
    }


def service_account(project_id: str = "copper-resources-qa") -> dict:
    return {
        "type": "service_account",
        "project_id": project_id,
        "client_email": "qa-fcm@example.invalid",
        "private_key_id": "test-key-id",
        "private_key": "test-only-private-key",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


class ValidateQAFirebaseTests(unittest.TestCase):
    def validate(self, clients: dict, server: dict) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_path = root / "google-services.json"
            server_path = root / "service-account.json"
            client_path.write_text(json.dumps(clients), encoding="utf-8")
            server_path.write_text(json.dumps(server), encoding="utf-8")
            fingerprint = validator.validate_google_services(client_path, "copper-resources-qa")
            validator.validate_service_account(server_path, "copper-resources-qa")
            return fingerprint

    def test_accepts_exact_two_qa_packages_in_one_project(self):
        fingerprint = self.validate(
            google_services(*sorted(validator.QA_PACKAGES)),
            service_account(),
        )
        self.assertEqual(len(fingerprint), 64)

    def test_rejects_production_or_other_android_clients(self):
        with self.assertRaisesRegex(SystemExit, "exactly the two approved packages"):
            self.validate(
                google_services(*sorted(validator.QA_PACKAGES), "ru.copperresources.excavator"),
                service_account(),
            )

    def test_rejects_missing_qa_package(self):
        with self.assertRaisesRegex(SystemExit, "missing=ru.copperresources.driver.qa"):
            self.validate(
                google_services("ru.copperresources.excavator.qa"),
                service_account(),
            )

    def test_rejects_mismatched_server_project(self):
        with self.assertRaisesRegex(SystemExit, "not the approved QA project"):
            self.validate(
                google_services(*sorted(validator.QA_PACKAGES)),
                service_account(project_id="not-the-client-project"),
            )

    def test_rejects_server_private_key_inside_client_config(self):
        clients = google_services(*sorted(validator.QA_PACKAGES))
        clients["private_key"] = "must-not-be-here"
        with self.assertRaisesRegex(SystemExit, "must not be embedded"):
            self.validate(clients, service_account())

    def test_client_check_does_not_require_server_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            client_path = Path(directory) / "google-services.json"
            client_path.write_text(
                json.dumps(google_services(*sorted(validator.QA_PACKAGES))),
                encoding="utf-8",
            )
            fingerprint = validator.validate_google_services(
                client_path,
                "copper-resources-qa",
            )
        self.assertEqual(len(fingerprint), 64)


if __name__ == "__main__":
    unittest.main()
