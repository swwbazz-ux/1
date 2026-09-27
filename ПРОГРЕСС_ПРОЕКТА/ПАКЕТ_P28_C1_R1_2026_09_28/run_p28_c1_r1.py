import os
import sys
import tempfile
from pathlib import Path


backend = Path(os.environ['P28_R1_BACKEND']).resolve()
package = Path(__file__).resolve().parent
sys.path[:0] = [str(package), str(backend)]
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

log_path = Path(os.environ.get('P28_R1_LOG', package / 'raw-run-utf8.log'))
log_handle = log_path.open('w', encoding='utf-8', newline='\n')


class Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, value):
        for stream in self.streams:
            stream.write(value); stream.flush()
        return len(value)
    def flush(self):
        for stream in self.streams: stream.flush()


sys.stdout = Tee(sys.__stdout__, log_handle)
sys.stderr = Tee(sys.__stderr__, log_handle)
print(f'P28_R1_BACKEND={backend}', flush=True)
print(f'P28_R1_BACKEND_EXISTS={backend.exists()}', flush=True)

import django
django.setup()
from django.conf import settings
settings.MEDIA_ROOT = tempfile.mkdtemp(prefix='p28-c1-r1-media-')
from django.test.runner import DiscoverRunner

print(f'P28_R1_SOURCE_SHA={os.environ.get("P28_R1_SOURCE_SHA", "")}', flush=True)
print(f'P28_R1_SOURCE_CLEAN={os.environ.get("P28_R1_SOURCE_CLEAN", "")}', flush=True)
labels = [
    'p28_c1_r1_django.P28R1EnvironmentTest',
    'p28_c1_r1_django.P28R1FreeBucketTests',
    'p28_c1_r1_django.P28R1DriverBranchesTests',
    'p28_c1_r1_django.P28R1ProjectionTests.test_real_server_projections_follow_p1_p2_unload_p3',
]
failures = DiscoverRunner(verbosity=2, interactive=False).run_tests(labels)
exit_code = int(bool(failures))
print(f'P28_R1_EXIT_CODE={exit_code}', flush=True)
raise SystemExit(exit_code)
