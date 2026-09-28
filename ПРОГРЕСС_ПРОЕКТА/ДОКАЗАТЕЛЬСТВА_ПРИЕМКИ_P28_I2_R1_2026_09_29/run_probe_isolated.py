"""Run selected Django checks against an isolated in-memory SQLite database.

This runner is outside the candidate worktree. It never uses a production DB.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CANDIDATE_ROOT = Path(os.environ.get('P28_I2_CANDIDATE_ROOT', str(ROOT.parent / 'p28-i2-candidate'))).resolve()
BACKEND = CANDIDATE_ROOT / 'СИСТЕМА_MVP' / 'backend'
import subprocess
head = subprocess.check_output(['git', '-C', str(CANDIDATE_ROOT), 'rev-parse', 'HEAD'], text=True).strip()
dirty = subprocess.check_output(['git', '-C', str(CANDIDATE_ROOT), 'status', '--porcelain=v1', '--untracked-files=all'], text=True)
if dirty:
    raise SystemExit('Refuse dirty candidate worktree')
print('CANDIDATE_HEAD=' + head, 'SOURCE_CLEAN=True', flush=True)
os.environ['DJANGO_DB_ENGINE'] = 'sqlite'
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[name] = '1'
if (BACKEND / '.env').exists():
    raise SystemExit('Refuse environment file in isolated checkout')
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT))
os.chdir(BACKEND)
from django.conf import settings
settings.DATABASES = {'default': {
    'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:',
    'TEST': {'NAME': ':memory:'},
}}
settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
settings.MEDIA_ROOT = ROOT / 'runtime-media'
settings.MEDIA_ROOT.mkdir(exist_ok=True)
import django
django.setup()
from django.db import connection
from django.test.utils import get_runner
print('ISOLATED_P28_I2', 'django=' + django.get_version(),
      'vendor=' + connection.vendor, 'db=:memory:', flush=True)
if connection.vendor != 'sqlite':
    raise SystemExit('Unexpected DB vendor')
runner = get_runner(settings)(verbosity=2, interactive=False)
raise SystemExit(bool(runner.run_tests(sys.argv[1:])))
