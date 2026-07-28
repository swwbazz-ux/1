import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class OperationalFragmentRuntimeTests(SimpleTestCase):
    def test_production_operational_fragment_runtime(self):
        node = shutil.which("node")
        if not node:
            self.skipTest(
                "Node.js нужен для исполняемой JS-регрессии operational fragments."
            )
        test_path = (
            Path(settings.BASE_DIR)
            / "static"
            / "js"
            / "tests"
            / "operational-fragment-runtime.test.js"
        )

        result = subprocess.run(
            [node, "--test", str(test_path)],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "Operational fragment JS runtime regression failed.\n"
                f"{result.stdout}\n{result.stderr}"
            ),
        )
