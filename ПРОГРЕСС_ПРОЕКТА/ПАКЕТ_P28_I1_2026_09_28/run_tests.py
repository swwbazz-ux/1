import io
import os
from pathlib import Path
import sys
import unittest


package = Path(__file__).resolve().parent
sys.path.insert(0, str(package))
log_path = Path(os.environ.get('P28_I1_LOG', package / 'raw-run-utf8.log'))


class Tee(io.TextIOBase):
    def __init__(self, *streams): self.streams = streams
    def write(self, value):
        for stream in self.streams:
            stream.write(value); stream.flush()
        return len(value)
    def flush(self):
        for stream in self.streams: stream.flush()


with log_path.open('w', encoding='utf-8', newline='\n') as log:
    tee = Tee(sys.stdout, log)
    print(f'P28_I1_DOCS_SHA={os.environ.get("P28_I1_DOCS_SHA", "")}', file=tee)
    print(f'P28_I1_RELEASE_SHA={os.environ.get("P28_I1_RELEASE_SHA", "")}', file=tee)
    print(f'P28_I1_SOURCE_CLEAN={os.environ.get("P28_I1_SOURCE_CLEAN", "")}', file=tee)
    suite = unittest.defaultTestLoader.discover(str(package), pattern='test_route_core.py')
    result = unittest.TextTestRunner(stream=tee, verbosity=2).run(suite)
    exit_code = int(not result.wasSuccessful())
    print(f'P28_I1_TESTS_RUN={result.testsRun}', file=tee)
    print(f'P28_I1_FAILURES={len(result.failures)}', file=tee)
    print(f'P28_I1_ERRORS={len(result.errors)}', file=tee)
    print(f'P28_I1_EXIT_CODE={exit_code}', file=tee)
raise SystemExit(exit_code)
