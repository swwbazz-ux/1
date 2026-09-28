import contextlib
import io
import os
from pathlib import Path
import sys
import unittest


package = Path(__file__).resolve().parent
sys.path.insert(0, str(package))
log_path = Path(os.environ.get('P28_I1_R1_LOG', package / 'raw-run-r1-utf8.log'))


class Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, value):
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self):
        for stream in self.streams:
            stream.flush()


with log_path.open('w', encoding='utf-8', newline='\n') as log:
    tee = Tee(sys.__stdout__, log)
    with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
        print(f'P28_I1_R1_REQUIRED_BASE={os.environ.get("P28_I1_R1_REQUIRED_BASE", "")}')
        print(f'P28_I1_R1_ACTUAL_HEAD={os.environ.get("P28_I1_R1_ACTUAL_HEAD", "")}')
        print(f'P28_I1_R1_RELEASE_SHA={os.environ.get("P28_I1_R1_RELEASE_SHA", "")}')
        print(f'P28_I1_R1_CLEAN_GATE={os.environ.get("P28_I1_R1_CLEAN_GATE", "")}')
        suite = unittest.defaultTestLoader.discover(str(package), pattern='test_*.py')
        result = unittest.TextTestRunner(stream=tee, verbosity=2).run(suite)
        exit_code = int(not result.wasSuccessful())
        print(f'P28_I1_R1_TESTS_RUN={result.testsRun}')
        print(f'P28_I1_R1_FAILURES={len(result.failures)}')
        print(f'P28_I1_R1_ERRORS={len(result.errors)}')
        print(f'P28_I1_R1_EXIT_CODE={exit_code}')
raise SystemExit(exit_code)
