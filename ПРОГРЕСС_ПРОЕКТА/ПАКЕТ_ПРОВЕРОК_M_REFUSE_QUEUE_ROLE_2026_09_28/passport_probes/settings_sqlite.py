import os
from pathlib import Path

os.environ['DJANGO_DB_ENGINE'] = 'sqlite'

from config.settings import *  # noqa: F401,F403,E402

test_name = os.environ.get('PASSPORT_TEST_DB', '').strip()
if not test_name:
    raise RuntimeError('PASSPORT_TEST_DB is required for the isolated SQLite probe')

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': Path(test_name).with_name('passport_r2_source.sqlite3'),
        'TEST': {'NAME': test_name},
    },
}
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'passport-r2-sqlite',
    },
}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
