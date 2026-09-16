from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
