import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class RealtimeRuntimeTests(SimpleTestCase):
    def test_mobile_worker_foreground_reconciliation_runtime(self):
        node = shutil.which("node")
        if not node:
            self.skipTest(
                "Node.js нужен для исполняемой JS-регрессии пробуждения рабочих приложений."
            )
        test_path = (
            Path(settings.BASE_DIR)
            / "static"
            / "js"
            / "tests"
            / "realtime-auth-runtime.test.js"
        )

        result = subprocess.run(
            [node, "--test", str(test_path)],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "Realtime foreground reconciliation JS regression failed.\n"
                f"{result.stdout}\n{result.stderr}"
            ),
        )
