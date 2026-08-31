import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from .native_handoff import (
    NATIVE_HANDOFF_CERT_SHA256,
    NATIVE_HANDOFF_SESSION_KEY,
    NATIVE_HANDOFF_TTL_SECONDS,
    build_native_handoff_url,
    consume_native_handoff,
)


TEST_CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'native-handoff-tests',
    },
}
DRIVER_NATIVE_UA = (
    'Mozilla/5.0 (Linux; Android 14; wv) '
    'CopperResourcesNative/driver/0.1.8 Mobile Safari/537.36'
)
EXCAVATOR_NATIVE_UA = (
    'Mozilla/5.0 (Linux; Android 14; wv) '
    'CopperResourcesNative/excavator/0.1.12 Mobile Safari/537.36'
)


@override_settings(
    ALLOWED_HOSTS=['localhost', '.localhost'],
    CACHES=TEST_CACHES,
)
class NativeHandoffTests(TestCase):
    phone = '79990000071'

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def issue(self, role_code='driver'):
        request = self.factory.get('/start/', HTTP_HOST='localhost:8000')
        url = build_native_handoff_url(
            request,
            phone=self.phone,
            role_code=role_code,
        )
        self.assertTrue(url)
        marker = '#token='
        self.assertIn(marker, url)
        return url, url.split(marker, 1)[1]

    def csrf_client(self, *, host='driver.localhost', user_agent=DRIVER_NATIVE_UA):
        client = Client(enforce_csrf_checks=True)
        response = client.get(
            reverse('native_handoff_open'),
            HTTP_HOST=host,
            HTTP_USER_AGENT=user_agent,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('csrftoken', client.cookies)
        return client, client.cookies['csrftoken'].value

    def redeem(self, client, csrf_token, token, *, host='driver.localhost',
               user_agent=DRIVER_NATIVE_UA):
        return client.post(
            reverse('native_handoff_redeem'),
            data=json.dumps({'token': token}),
            content_type='application/json',
            HTTP_HOST=host,
            HTTP_USER_AGENT=user_agent,
            HTTP_X_CSRFTOKEN=csrf_token,
        )

    def test_handoff_url_contains_only_an_opaque_fragment(self):
        url, token = self.issue()

        self.assertEqual(len(token), 43)
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        self.assertEqual(
            url.split('#', 1)[0],
            'http://driver.localhost:8000/native-handoff/open/',
        )
        self.assertNotIn(self.phone, url)
        self.assertNotIn('?token=', url)

    def test_one_time_consume_is_atomic_under_two_callers(self):
        _url, token = self.issue()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda _index: consume_native_handoff(
                    token=token,
                    role_code='driver',
                ),
                range(2),
            ))

        self.assertEqual(sorted(result.status for result in results), ['gone', 'ok'])
        self.assertEqual(
            [result.phone for result in results if result.status == 'ok'],
            [self.phone],
        )

    def test_wrong_role_does_not_burn_the_ticket(self):
        _url, token = self.issue('driver')

        wrong = consume_native_handoff(
            token=token,
            role_code='excavator_operator',
        )
        correct = consume_native_handoff(token=token, role_code='driver')

        self.assertEqual(wrong.status, 'wrong_role')
        self.assertEqual(correct.status, 'ok')
        self.assertEqual(correct.phone, self.phone)

    def test_repeated_issue_reuses_brief_ticket_but_redeem_allows_a_new_one(self):
        first_url, first_token = self.issue('driver')
        second_url, second_token = self.issue('driver')

        self.assertEqual(second_url, first_url)
        self.assertEqual(second_token, first_token)
        self.assertEqual(
            consume_native_handoff(token=first_token, role_code='driver').status,
            'ok',
        )

        third_url, third_token = self.issue('driver')
        self.assertNotEqual(third_url, first_url)
        self.assertNotEqual(third_token, first_token)

    def test_concurrent_issue_publishes_one_shared_ticket(self):
        barrier = Barrier(8)
        counter_lock = Lock()
        token_index = 0

        def forced_token(_byte_count):
            nonlocal token_index
            # Каждый поток уже прошёл начальный cache.get(recent) и теперь
            # одновременно входит в окно публикации собственного data-ticket.
            barrier.wait(timeout=2)
            with counter_lock:
                token_index += 1
                return f'{token_index:043d}'

        def issue_once(_index):
            request = self.factory.get('/start/', HTTP_HOST='localhost:8000')
            return build_native_handoff_url(
                request,
                phone=self.phone,
                role_code='driver',
            )

        with patch(
            'users.native_handoff.secrets.token_urlsafe',
            side_effect=forced_token,
        ):
            with ThreadPoolExecutor(max_workers=8) as executor:
                urls = list(executor.map(issue_once, range(8)))

        self.assertTrue(urls[0])
        self.assertEqual(set(urls), {urls[0]})

    def test_partial_consume_failure_never_reuses_burned_ticket(self):
        first_url, first_token = self.issue('driver')

        with self.assertLogs('users.native_handoff', level='WARNING'):
            with patch.object(cache, 'delete', side_effect=RuntimeError('test')):
                failed = consume_native_handoff(
                    token=first_token,
                    role_code='driver',
                )

        self.assertEqual(failed.status, 'unavailable')
        second_url, second_token = self.issue('driver')
        self.assertNotEqual(second_url, first_url)
        self.assertNotEqual(second_token, first_token)

    def test_assetlinks_are_host_specific_and_use_the_release_certificate(self):
        cases = (
            ('driver.localhost', 'ru.copperresources.driver'),
            ('excavator.localhost', 'ru.copperresources.excavator'),
        )
        for host, package_name in cases:
            with self.subTest(host=host):
                response = self.client.get(
                    reverse('native_handoff_assetlinks'),
                    HTTP_HOST=host,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'application/json')
                self.assertEqual(response.json()[0]['target'], {
                    'namespace': 'android_app',
                    'package_name': package_name,
                    'sha256_cert_fingerprints': [NATIVE_HANDOFF_CERT_SHA256],
                })
                self.assertIn('public', response['Cache-Control'])

        shared = self.client.get(
            reverse('native_handoff_assetlinks'),
            HTTP_HOST='localhost',
        )
        self.assertEqual(shared.status_code, 404)

    def test_browser_get_shows_fallback_without_redeem_script(self):
        response = self.client.get(
            reverse('native_handoff_open'),
            HTTP_HOST='driver.localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Откройте установленное приложение')
        self.assertNotContains(response, 'data-native-handoff-loading')
        self.assertNotContains(response, 'native_handoff_redeem')
        self.assertContains(response, 'window.history.replaceState')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')

    def test_native_redeem_prefills_login_once_and_never_prefills_pin(self):
        _url, token = self.issue()
        client, csrf_token = self.csrf_client()

        redeemed = self.redeem(client, csrf_token, token)

        self.assertEqual(redeemed.status_code, 200)
        self.assertEqual(redeemed.json(), {'ok': True, 'redirect_url': '/'})
        self.assertNotIn('Access-Control-Allow-Origin', redeemed.headers)
        self.assertEqual(
            client.session[NATIVE_HANDOFF_SESSION_KEY]['phone'],
            self.phone,
        )

        login = client.get(
            reverse('login'),
            HTTP_HOST='driver.localhost',
            HTTP_USER_AGENT=DRIVER_NATIVE_UA,
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.context['submitted_phone'], self.phone)
        self.assertTrue(login.context['phone_is_prefill_only'])
        self.assertNotContains(login, 'value="000000"')
        self.assertNotIn(NATIVE_HANDOFF_SESSION_KEY, client.session)

        second_login = client.get(
            reverse('login'),
            HTTP_HOST='driver.localhost',
            HTTP_USER_AGENT=DRIVER_NATIVE_UA,
        )
        self.assertEqual(second_login.context['submitted_phone'], '')

    def test_session_prefill_has_its_own_expiry(self):
        client = Client()
        session = client.session
        session[NATIVE_HANDOFF_SESSION_KEY] = {
            'phone': self.phone,
            'role_code': 'driver',
            'issued_at': 1,
        }
        session.save()

        response = client.get(
            reverse('login'),
            HTTP_HOST='driver.localhost',
            HTTP_USER_AGENT=DRIVER_NATIVE_UA,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['submitted_phone'], '')
        self.assertNotIn(NATIVE_HANDOFF_SESSION_KEY, client.session)
        self.assertGreater(NATIVE_HANDOFF_TTL_SECONDS, 0)

    def test_replay_expiry_malformed_profile_and_csrf_fail_closed(self):
        _url, token = self.issue()
        client, csrf_token = self.csrf_client()

        wrong_profile = self.redeem(
            client,
            csrf_token,
            token,
            user_agent=EXCAVATOR_NATIVE_UA,
        )
        self.assertEqual(wrong_profile.status_code, 403)

        cookie_only = self.redeem(
            client,
            csrf_token,
            token,
            user_agent='Mozilla/5.0',
        )
        self.assertEqual(cookie_only.status_code, 403)

        missing_csrf = Client(enforce_csrf_checks=True).post(
            reverse('native_handoff_redeem'),
            data=json.dumps({'token': token}),
            content_type='application/json',
            HTTP_HOST='driver.localhost',
            HTTP_USER_AGENT=DRIVER_NATIVE_UA,
        )
        self.assertEqual(missing_csrf.status_code, 403)

        accepted = self.redeem(client, csrf_token, token)
        replay = self.redeem(client, csrf_token, token)
        malformed = self.redeem(client, csrf_token, 'not-a-token')

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(replay.status_code, 410)
        self.assertEqual(malformed.status_code, 400)

        _expired_url, expired_token = self.issue()
        cache.clear()
        expired = self.redeem(client, csrf_token, expired_token)
        self.assertEqual(expired.status_code, 410)
