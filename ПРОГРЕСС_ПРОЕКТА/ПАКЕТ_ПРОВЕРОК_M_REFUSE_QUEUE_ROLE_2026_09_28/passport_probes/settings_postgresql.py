import os

os.environ['DJANGO_DB_ENGINE'] = 'postgres'

from config.settings import *  # noqa: F401,F403,E402

test_name = os.environ.get('PASSPORT_TEST_DB', '').strip()
if not test_name or not test_name.startswith('passport_r2_test_'):
    raise RuntimeError('PASSPORT_TEST_DB must start with passport_r2_test_')

DATABASES['default']['ENGINE'] = 'django.db.backends.postgresql'
DATABASES['default']['TEST'] = {'NAME': test_name}
DATABASES['default']['CONN_MAX_AGE'] = 0
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'passport-r2-postgresql',
    },
}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
