from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from .login_security import (
    LOGIN_BLOCK_SECONDS,
    LoginAllowance,
    _cache_key,
    check_login_allowed,
    clear_login_failures,
    record_login_failure,
)


TEST_CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'portal-login-security-tests',
    },
}


@override_settings(CACHES=TEST_CACHES, SECRET_KEY='portal-login-security-test-key')
class PortalLoginSecurityTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.request = RequestFactory().post(
            '/portal/login/',
            REMOTE_ADDR='192.0.2.10',
        )

    def tearDown(self):
        cache.clear()

    def test_result_supports_attributes_and_tuple_unpacking(self):
        result = check_login_allowed(self.request, '+7 (900) 000-00-01')

        self.assertIsInstance(result, LoginAllowance)
        allowed, retry_after = result
        self.assertTrue(allowed)
        self.assertEqual(retry_after, 0)

    def test_equivalent_phone_formats_share_state_for_same_remote_addr(self):
        with patch('portal.login_security.time.time', return_value=1000.0):
            record_login_failure(self.request, '8 (900) 000-00-01')
            result = check_login_allowed(self.request, '+7 900 000 00 01')

        self.assertFalse(result.allowed)
        self.assertEqual(result.retry_after, 1)

    def test_phone_and_remote_addr_are_both_part_of_the_bucket(self):
        other_ip_request = RequestFactory().post(
            '/portal/login/',
            REMOTE_ADDR='192.0.2.11',
        )
        with patch('portal.login_security.time.time', return_value=1000.0):
            record_login_failure(self.request, '+79000000001')
            same_phone_other_ip = check_login_allowed(other_ip_request, '+79000000001')
            same_ip_other_phone = check_login_allowed(self.request, '+79000000002')

        self.assertTrue(same_phone_other_ip.allowed)
        self.assertTrue(same_ip_other_phone.allowed)

    def test_failure_delays_increase_without_sleeping(self):
        moments_and_delays = (
            (1000.0, 1),
            (1001.0, 2),
            (1003.0, 4),
            (1007.0, 8),
        )

        for moment, expected_delay in moments_and_delays:
            with patch('portal.login_security.time.time', return_value=moment):
                self.assertTrue(check_login_allowed(self.request, '+79000000001').allowed)
                result = record_login_failure(self.request, '+79000000001')
            self.assertEqual(result, LoginAllowance(False, expected_delay))

    def test_fifth_failure_blocks_for_fifteen_minutes_then_resets(self):
        for moment in (1000.0, 1001.0, 1003.0, 1007.0):
            with patch('portal.login_security.time.time', return_value=moment):
                record_login_failure(self.request, '+79000000001')

        with patch('portal.login_security.time.time', return_value=1015.0):
            blocked = record_login_failure(self.request, '+79000000001')
        self.assertEqual(blocked.retry_after, LOGIN_BLOCK_SECONDS)

        with patch('portal.login_security.time.time', return_value=1500.0):
            still_blocked = check_login_allowed(self.request, '+79000000001')
        self.assertFalse(still_blocked.allowed)
        self.assertEqual(still_blocked.retry_after, 415)

        with patch('portal.login_security.time.time', return_value=1915.0):
            allowed_again = check_login_allowed(self.request, '+79000000001')
        self.assertTrue(allowed_again.allowed)
        self.assertEqual(allowed_again.retry_after, 0)

        with patch('portal.login_security.time.time', return_value=1915.0):
            first_new_failure = record_login_failure(self.request, '+79000000001')
        self.assertEqual(first_new_failure.retry_after, 1)

    def test_success_reset_removes_failure_state(self):
        with patch('portal.login_security.time.time', return_value=1000.0):
            record_login_failure(self.request, '+79000000001')
            clear_login_failures(self.request, '+79000000001')
            result = check_login_allowed(self.request, '+79000000001')

        self.assertTrue(result.allowed)
        self.assertEqual(result.retry_after, 0)

    def test_cache_contains_only_hmac_key_and_nonsensitive_state(self):
        raw_phone = '+7 (900) 000-00-01'
        normalized_phone = '79000000001'
        remote_addr = '192.0.2.10'
        key = _cache_key(self.request, raw_phone)

        self.assertNotIn(raw_phone, key)
        self.assertNotIn(normalized_phone, key)
        self.assertNotIn(remote_addr, key)

        with patch('portal.login_security.cache.set') as cache_set:
            with patch('portal.login_security.time.time', return_value=1000.0):
                record_login_failure(self.request, raw_phone)

        stored_key, stored_value = cache_set.call_args.args[:2]
        stored_representation = f'{stored_key!r}{stored_value!r}'
        self.assertNotIn(raw_phone, stored_representation)
        self.assertNotIn(normalized_phone, stored_representation)
        self.assertNotIn(remote_addr, stored_representation)
        self.assertEqual(set(stored_value), {'failures', 'retry_until'})

    def test_bad_cache_payload_is_discarded_and_does_not_break_login(self):
        key = _cache_key(self.request, '+79000000001')
        cache.set(key, {'failures': 'broken', 'retry_until': object()}, timeout=60)

        result = check_login_allowed(self.request, '+79000000001')

        self.assertTrue(result.allowed)
        self.assertIsNone(cache.get(key))
