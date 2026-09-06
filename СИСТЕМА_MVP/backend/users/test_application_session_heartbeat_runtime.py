import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class ApplicationSessionHeartbeatRuntimeTests(SimpleTestCase):
    def test_client_environment_detection_runtime(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js нужен для исполняемой проверки heartbeat.')
        test_path = (
            Path(settings.BASE_DIR)
            / 'static'
            / 'js'
            / 'tests'
            / 'application-session-heartbeat-runtime.test.js'
        )
        result = subprocess.run(
            [node, '--test', str(test_path)],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f'Application session heartbeat JS failed.\n{result.stdout}\n{result.stderr}',
        )
