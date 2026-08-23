from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from core.pwa_performance_qa import (
    EXPECTED_PWA_PERFORMANCE_QA_IDENTITY,
    PWA_PERFORMANCE_QA_SCHEMA,
    PWA_PERFORMANCE_QA_SCHEMA_VERSION,
    PwaPerformanceQaError,
    pwa_performance_qa_fingerprint,
    validate_configured_pwa_performance_qa_database,
)


QA_RUN_ID = 'PWA-PERF-20260823-DISPATCHER-01'
QA_SETTINGS = {
    'DEBUG': True,
    'ALLOWED_HOSTS': ['.localhost', 'localhost', '127.0.0.1'],
    'PWA_TRAFFIC_QA_PREFLIGHT_ENABLED': True,
    'PWA_TRAFFIC_QA_RUN_ID': QA_RUN_ID,
}


class PwaPerformanceQaDatabaseContractTests(SimpleTestCase):
    def test_configured_database_must_match_exact_identity_and_empty_password(self):
        database = {
            'ENGINE': EXPECTED_PWA_PERFORMANCE_QA_IDENTITY.engine,
            'NAME': EXPECTED_PWA_PERFORMANCE_QA_IDENTITY.name,
            'USER': EXPECTED_PWA_PERFORMANCE_QA_IDENTITY.user,
            'PASSWORD': '',
            'HOST': EXPECTED_PWA_PERFORMANCE_QA_IDENTITY.host,
            'PORT': EXPECTED_PWA_PERFORMANCE_QA_IDENTITY.port,
        }

        self.assertEqual(
            validate_configured_pwa_performance_qa_database(database),
            EXPECTED_PWA_PERFORMANCE_QA_IDENTITY,
        )
        for key, wrong_value in (
            ('ENGINE', 'django.db.backends.sqlite3'),
            ('NAME', 'accounting_mvp'),
            ('USER', 'accounting_mvp'),
            ('HOST', '77.91.93.47'),
            ('PORT', '5432'),
            ('PASSWORD', 'not-empty'),
        ):
            wrong = dict(database)
            wrong[key] = wrong_value
            with self.subTest(key=key):
                with self.assertRaises(PwaPerformanceQaError):
                    validate_configured_pwa_performance_qa_database(wrong)

    def test_fingerprint_is_deterministic_and_run_scoped(self):
        first = pwa_performance_qa_fingerprint(
            QA_RUN_ID,
            EXPECTED_PWA_PERFORMANCE_QA_IDENTITY,
        )
        second = pwa_performance_qa_fingerprint(
            QA_RUN_ID,
            EXPECTED_PWA_PERFORMANCE_QA_IDENTITY,
        )
        other = pwa_performance_qa_fingerprint(
            'PWA-PERF-20260823-DISPATCHER-02',
            EXPECTED_PWA_PERFORMANCE_QA_IDENTITY,
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertRegex(first, r'^[A-F0-9]{64}$')


class PwaPerformanceQaPreflightViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.url = reverse('pwa_performance_qa_preflight')
        self.payload = {
            'schema': PWA_PERFORMANCE_QA_SCHEMA,
            'schema_version': PWA_PERFORMANCE_QA_SCHEMA_VERSION,
            'fingerprint': 'A' * 64,
        }

    def _request(self, **extra):
        return self.client.get(
            self.url,
            HTTP_HOST='dispatcher.localhost',
            REMOTE_ADDR='127.0.0.1',
            HTTP_X_COPPER_QA_RUN_ID=QA_RUN_ID,
            **extra,
        )

    @override_settings(**QA_SETTINGS)
    @patch(
        'core.qa_views.verify_pwa_performance_qa_database',
        autospec=True,
    )
    def test_preflight_returns_only_opaque_fingerprint_and_no_store(
        self,
        verify,
    ):
        verify.return_value = self.payload

        response = self._request()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'status': 'ok', **self.payload},
        )
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        body = response.content.decode('utf-8')
        self.assertNotIn('copper_pwa_performance', body)
        self.assertNotIn('password', body.lower())
        verify.assert_called_once_with(QA_RUN_ID)

    @override_settings(**QA_SETTINGS)
    def test_gate_is_hidden_for_wrong_host_peer_header_method_or_disabled_flag(self):
        requests = (
            self.client.get(
                self.url,
                HTTP_HOST='driver.localhost',
                REMOTE_ADDR='127.0.0.1',
                HTTP_X_COPPER_QA_RUN_ID=QA_RUN_ID,
            ),
            self.client.get(
                self.url,
                HTTP_HOST='dispatcher.localhost',
                REMOTE_ADDR='192.0.2.1',
                HTTP_X_FORWARDED_FOR='127.0.0.1',
                HTTP_X_COPPER_QA_RUN_ID=QA_RUN_ID,
            ),
            self.client.get(
                self.url,
                HTTP_HOST='dispatcher.localhost',
                REMOTE_ADDR='127.0.0.1',
                HTTP_X_COPPER_QA_RUN_ID='PWA-PERF-20260823-WRONG-01',
            ),
            self.client.post(
                self.url,
                HTTP_HOST='dispatcher.localhost',
                REMOTE_ADDR='127.0.0.1',
                HTTP_X_COPPER_QA_RUN_ID=QA_RUN_ID,
            ),
        )
        for response in requests:
            self.assertEqual(response.status_code, 404)

        with override_settings(PWA_TRAFFIC_QA_PREFLIGHT_ENABLED=False):
            self.assertEqual(self._request().status_code, 404)

    @override_settings(**QA_SETTINGS)
    @patch(
        'core.qa_views.verify_pwa_performance_qa_database',
        autospec=True,
        side_effect=RuntimeError('database unavailable'),
    )
    def test_preflight_hides_unexpected_database_failure_as_503(self, _verify):
        response = self._request()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})
        self.assertIn('no-store', response['Cache-Control'])
        self.assertNotIn('database unavailable', response.content.decode())
