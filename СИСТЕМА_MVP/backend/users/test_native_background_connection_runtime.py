from pathlib import Path
import shutil
import subprocess

from django.test import SimpleTestCase


class NativeBackgroundConnectionRuntimeTests(SimpleTestCase):
    def test_native_background_connection_runtime(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js нужен для исполняемой проверки фоновой связи.')
        script = (
            Path(__file__).resolve().parents[1]
            / 'static'
            / 'js'
            / 'tests'
            / 'native-background-connection-runtime.test.js'
        )
        result = subprocess.run(
            [node, '--test', str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f'Native background connection JS failed.\n{result.stdout}\n{result.stderr}',
        )
