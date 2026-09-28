from __future__ import annotations

import importlib.util
import tarfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "app-overlay/users/management/commands/seed_sse_qa.py"
RUNTIME = ROOT / "generated/runtime.tar.gz"


def load_seed_module():
    spec = importlib.util.spec_from_file_location("sse_qa_seed_contract", SEED)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class SeedBusinessSmokeContractTests(unittest.TestCase):
    def test_runtime_contains_the_exact_seed_and_behavior_test_sources(self):
        expected = {
            "backend/users/management/commands/seed_sse_qa.py": SEED.read_bytes(),
            "backend/users/test_sse_qa_seed.py": (
                ROOT / "app-overlay/users/test_sse_qa_seed.py"
            ).read_bytes(),
        }
        with tarfile.open(RUNTIME, "r:gz") as archive:
            for member_name, source in expected.items():
                with self.subTest(member=member_name):
                    extracted = archive.extractfile(member_name)
                    self.assertIsNotNone(extracted)
                    self.assertEqual(extracted.read(), source)

    def test_serialized_event_matcher_requires_new_trip_identity_and_version(self):
        seed = load_seed_module()
        expected = {
            "version": 27,
            "type": "trip_changed",
            "object_type": "Trip",
            "object_id": "81",
            "payload": {"trip_id": 81},
        }
        self.assertTrue(seed.is_expected_trip_event(expected, trip_id=81, version=27))
        self.assertFalse(seed.is_expected_trip_event(expected, trip_id=80, version=27))
        self.assertFalse(seed.is_expected_trip_event(expected, trip_id=81, version=26))
        self.assertFalse(
            seed.is_expected_trip_event(
                {**expected, "type": None, "event_type": "trip_changed"},
                trip_id=81,
                version=27,
            )
        )

    def test_fixture_constants_follow_application_phone_and_measurement_rules(self):
        seed = load_seed_module()
        for phone in (seed.DRIVER_PHONE, seed.EXCAVATOR_PHONE):
            digits = "".join(character for character in phone if character.isdigit())
            self.assertEqual(len(digits), 11)
            self.assertTrue(digits.startswith("79"))
        self.assertNotEqual(seed.DRIVER_PHONE, seed.EXCAVATOR_PHONE)
        self.assertGreater(seed.TRUCK_BODY_VOLUME_M3, 0)
        self.assertGreater(seed.ROCK_DENSITY, 0)


if __name__ == "__main__":
    unittest.main()
