import ast
import json
import os
import sys
import tempfile
from pathlib import Path

variant, repo, output = sys.argv[1:]
output = str(Path(output).resolve())
Path(output).parent.mkdir(parents=True, exist_ok=True)
backend = Path(repo) / 'СИСТЕМА_MVP/backend'
package = Path(__file__).parent / 'package'
assert not (backend / '.env').exists(), 'isolated checkout unexpectedly has .env'
os.chdir(backend)
sys.path[:0] = [str(backend), str(package)]
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
os.environ['DJANGO_DB_ENGINE'] = 'sqlite'
import config.settings as cfg

with tempfile.TemporaryDirectory(prefix='s106-root-test-') as temp:
    cfg.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:', 'TEST': {'NAME': ':memory:'}}}
    cfg.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
    cfg.MEDIA_ROOT = Path(temp) / 'media'
    cfg.MEDIA_ROOT.mkdir()
    cfg.PORTAL_PRIVATE_MEDIA_ROOT = Path(temp) / 'private'
    cfg.ROTATIONS_PRIVATE_MEDIA_ROOT = Path(temp) / 'rotations'
    cfg.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    import django
    django.setup()
    from django.db import connection
    from django.test.runner import DiscoverRunner
    assert connection.vendor == 'sqlite'
    assert str(connection.settings_dict['NAME']) == ':memory:'

    cls = next(x for x in ast.parse((package / 'test_s106_c1_probe.py').read_text()).body if isinstance(x, ast.ClassDef))
    probes = ['test_s106_c1_probe.' + cls.name + '.' + x.name for x in cls.body if isinstance(x, ast.FunctionDef) and x.name.startswith('test_')]
    prefix = 'core.test_offline_sync.OfflineEventSyncTests.'
    if variant == 'release':
        existing = [
            'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_manual_load_under_bucket_cancelled_by_server_ttl_is_recorded_by_fact',
            'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_already_rejected_cancelled_bucket_load_is_accepted_on_resend',
            prefix + 'test_dependency_chain_recovers_when_only_the_parent_clock_was_ahead',
            prefix + 'test_real_conflict_chain_is_not_reopened_by_clock_recovery',
        ]
        expected_failures = {probes[0], probes[1], probes[2]}
    else:
        assert variant == 'pr106'
        existing = [prefix + n for n in [
            'test_dependency_chain_recovers_when_only_the_parent_clock_was_ahead',
            'test_legacy_open_trip_changed_chain_replays_worker_truth',
            'test_legacy_dependency_receipts_reprocess_independently',
            'test_legacy_unload_before_load_receipt_reprocesses_without_duplicate',
        ]]
        expected_failures = set()

    class RecordingRunner(DiscoverRunner):
        def suite_result(self, suite, result, **kwargs):
            details = {
                'variant': variant, 'testsRun': result.testsRun,
                'failures': [{'test': t.id(), 'trace': trace} for t, trace in result.failures],
                'errors': [{'test': t.id(), 'trace': trace} for t, trace in result.errors],
                'skipped': [(t.id(), reason) for t, reason in result.skipped],
                'python': sys.version, 'django': django.get_version(),
                'database': 'isolated sqlite :memory:', 'permanent_database_touched': False,
            }
            self.observed = details
            Path(output).write_text(json.dumps(details, ensure_ascii=False, indent=2) + '\n')
            return super().suite_result(suite, result, **kwargs)

    runner = RecordingRunner(verbosity=2, interactive=False)
    test_exit = runner.run_tests(existing + probes)
    observed = runner.observed
    exact = (observed['testsRun'] == 9 and not observed['errors'] and not observed['skipped'] and {x['test'] for x in observed['failures']} == expected_failures)
    print(json.dumps({'variant': variant, 'testsRun': observed['testsRun'], 'test_failure_count': test_exit, 'expected_behavior_reproduced': exact}, ensure_ascii=False))
    sys.exit(0 if exact else 1)
