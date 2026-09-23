from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import validate_ci_test_matrix as validator


class CiTestMatrixValidatorTests(unittest.TestCase):
    def _manifest(self, payload: dict[str, object]) -> Path:
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        )
        self.addCleanup(lambda: Path(temporary.name).unlink(missing_ok=True))
        with temporary:
            json.dump(payload, temporary)
        return Path(temporary.name)

    def test_current_manifest_covers_every_installed_app_and_test_module(self):
        manifest = validator.BACKEND_ROOT.parents[1] / ".github" / "ci" / (
            "django-test-matrix.json"
        )

        matrix = validator.validate_manifest(manifest)

        self.assertTrue(matrix["sqlite"])
        self.assertTrue(matrix["postgresql"])

    def test_split_app_rejects_an_unlisted_test_module(self):
        manifest = self._manifest({
            "sqlite": [{
                "name": "demo-core",
                "apps": ["demo"],
                "tests": "demo.tests",
            }],
            "postgresql": [{"name": "demo-pg", "tests": "demo.pg_tests"}],
        })

        with (
            patch.object(validator, "_installed_first_party_apps", return_value={"demo"}),
            patch.object(
                validator,
                "_discovered_test_modules",
                return_value={"demo.tests", "demo.test_new_contract"},
            ),
        ):
            with self.assertRaisesRegex(ValueError, "demo.test_new_contract"):
                validator.validate_manifest(manifest)

    def test_rejects_shell_metacharacters_in_test_labels(self):
        manifest = self._manifest({
            "sqlite": [{
                "name": "demo",
                "apps": ["demo"],
                "tests": "demo; echo unsafe",
            }],
            "postgresql": [{"name": "demo-pg", "tests": "demo.pg_tests"}],
        })

        with patch.object(
            validator,
            "_installed_first_party_apps",
            return_value={"demo"},
        ):
            with self.assertRaisesRegex(ValueError, "unsafe test labels"):
                validator.validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
