import re
from datetime import timedelta
from tempfile import TemporaryDirectory

from core.production_time import production_work_date
from django.core.files.base import ContentFile
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from users.models import Employee, EmployeeAccess, Role, WatchComposition

from .models import RatingPeriod


LIVE_TV_SETTINGS = {
    'PORTAL_WORKING_DRIVER_RATING_ENABLED': True,
    'RATING_TV_SCREEN_ENABLED': True,
}
QA_TV_SETTINGS = {
    'DEBUG': True,
    'RATING_TV_QA_PREVIEW_ENABLED': True,
}


class DriverRatingTvScreenTests(TestCase):
    def setUp(self):
        self.composition = WatchComposition.objects.create(
            code='rating-tv-tests',
            name='Тестовый состав TV-рейтинга',
        )
        self.driver = Employee.objects.create(
            full_name='Водитель в области TV-рейтинга',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
            watch_composition=self.composition,
        )
        self.dispatcher_access = self._create_access(
            'dispatcher',
            'Диспетчер TV-рейтинга',
        )

    def _create_access(self, role_code, employee_name):
        role, _created = Role.objects.get_or_create(
            code=role_code,
            defaults={'name': f'Роль {role_code} TV-рейтинга'},
        )
        employee = Employee.objects.create(
            full_name=employee_name,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        return EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=f'{employee.pk:06d}'[-6:],
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def _login_as(self, access, *, client=None):
        client = client or self.client
        login_at = timezone.now()
        access.last_login_at = login_at
        access.save(update_fields=['last_login_at'])
        session = client.session
        session['employee_access_id'] = access.id
        session[ACTIVE_ROLE_SESSION_KEY] = access.id
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = access.role.code
        session.save()
        return login_at

    @override_settings(**LIVE_TV_SETTINGS)
    def test_tv_screen_allows_only_current_management_roles(self):
        accesses = {
            'dispatcher': self.dispatcher_access,
            'admin': self._create_access(
                'admin',
                'Администратор TV-рейтинга',
            ),
            'manager': self._create_access(
                'manager',
                'Руководитель TV-рейтинга',
            ),
        }

        for role_code, access in accesses.items():
            with self.subTest(role_code=role_code):
                client = Client()
                self._login_as(access, client=client)
                response = client.get(reverse('driver_rating_tv'))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context['rating_tv_access'].id,
                    access.id,
                )
                self.assertFalse(
                    response.context['rating_tv_qa_preview'],
                )
                self.assertIn(
                    'private',
                    response.headers['Cache-Control'],
                )
                self.assertIn(
                    'no-store',
                    response.headers['Cache-Control'],
                )

    @override_settings(**LIVE_TV_SETTINGS)
    def test_api_bootstrap_keeps_current_period_when_future_period_exists(self):
        second_composition = WatchComposition.objects.create(
            code='rating-tv-tests-second',
            name='Второй тестовый состав TV-рейтинга',
        )
        Employee.objects.create(
            full_name='Водитель второго состава TV-рейтинга',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
            watch_composition=second_composition,
        )
        work_date = production_work_date()
        current_period = RatingPeriod.objects.create(
            name='Текущий период TV-рейтинга',
            starts_on=work_date - timedelta(days=1),
            ends_before=work_date + timedelta(days=29),
            comment='Адресная проверка выбора текущего периода.',
        )
        future_period = RatingPeriod.objects.create(
            name='Будущий период TV-рейтинга',
            starts_on=work_date + timedelta(days=30),
            ends_before=work_date + timedelta(days=60),
            comment='Адресная проверка защиты от будущего периода.',
        )
        self._login_as(self.dispatcher_access)

        response = self.client.get(
            reverse('driver_period_rating_api'),
            {'shift_type': 'night'},
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(
            payload['rating_period']['id'],
            current_period.id,
        )
        self.assertNotEqual(
            payload['rating_period']['id'],
            future_period.id,
        )
        self.assertEqual(
            payload['available_rating_periods'][0]['id'],
            future_period.id,
        )

    @override_settings(**LIVE_TV_SETTINGS)
    def test_api_does_not_infer_future_period_during_calendar_gap(self):
        second_composition = WatchComposition.objects.create(
            code='rating-tv-gap-second',
            name='Второй состав проверки календарного разрыва',
        )
        Employee.objects.create(
            full_name='Водитель второго состава календарного разрыва',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
            watch_composition=second_composition,
        )
        work_date = production_work_date()
        future_period = RatingPeriod.objects.create(
            name='Будущий период после календарного разрыва',
            starts_on=work_date + timedelta(days=30),
            ends_before=work_date + timedelta(days=60),
            comment='Проверка запрета автоматического выбора будущего периода.',
        )
        self._login_as(self.dispatcher_access)

        response = self.client.get(
            reverse('driver_period_rating_api'),
            {'shift_type': 'night'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['available'])
        self.assertIsNone(payload['rating_period'])
        self.assertIsNone(payload['watch_composition'])
        self.assertEqual(
            payload['available_rating_periods'][0]['id'],
            future_period.id,
        )
        self.assertIn(
            'активный период рейтинга не задан',
            payload['status'],
        )

    @override_settings(**LIVE_TV_SETTINGS)
    def test_tv_screen_redirects_unauthenticated_and_driver_roles(self):
        unauthenticated = self.client.get(reverse('driver_rating_tv'))
        self.assertRedirects(
            unauthenticated,
            reverse('login'),
            fetch_redirect_response=False,
        )

        driver_access = self._create_access(
            'driver',
            'Водитель без доступа к TV-рейтингу',
        )
        self._login_as(driver_access)
        forbidden = self.client.get(reverse('driver_rating_tv'))

        self.assertRedirects(
            forbidden,
            reverse('role_home'),
            fetch_redirect_response=False,
        )

    @override_settings(**LIVE_TV_SETTINGS)
    def test_tv_screen_rejects_not_activated_and_deactivated_access(self):
        for status, is_active in (
            (EmployeeAccess.Status.NOT_ACTIVATED, True),
            (EmployeeAccess.Status.DEACTIVATED, False),
        ):
            with self.subTest(status=status):
                self.dispatcher_access.status = status
                self.dispatcher_access.is_active = is_active
                self.dispatcher_access.save(
                    update_fields=['status', 'is_active'],
                )
                self._login_as(self.dispatcher_access)

                response = self.client.get(reverse('driver_rating_tv'))

                self.assertRedirects(
                    response,
                    reverse('login'),
                    fetch_redirect_response=False,
                )
                self.dispatcher_access.status = (
                    EmployeeAccess.Status.ACTIVATED
                )
                self.dispatcher_access.is_active = True
                self.dispatcher_access.save(
                    update_fields=['status', 'is_active'],
                )
                self.client = Client()

    @override_settings(**LIVE_TV_SETTINGS)
    def test_tv_screen_rejects_dismissed_employee_and_inactive_role(self):
        self._login_as(self.dispatcher_access)
        self.dispatcher_access.employee.status = Employee.Status.DISMISSED
        self.dispatcher_access.employee.is_active = False
        self.dispatcher_access.employee.save(
            update_fields=['status', 'is_active'],
        )

        dismissed = self.client.get(reverse('driver_rating_tv'))

        self.assertRedirects(
            dismissed,
            reverse('login'),
            fetch_redirect_response=False,
        )

        self.dispatcher_access.employee.status = Employee.Status.ACTIVE
        self.dispatcher_access.employee.is_active = True
        self.dispatcher_access.employee.save(
            update_fields=['status', 'is_active'],
        )
        self.dispatcher_access.role.is_active = False
        self.dispatcher_access.role.save(update_fields=['is_active'])
        self.client = Client()
        self._login_as(self.dispatcher_access)

        inactive_role = self.client.get(reverse('driver_rating_tv'))

        self.assertRedirects(
            inactive_role,
            reverse('login'),
            fetch_redirect_response=False,
        )

    @override_settings(**LIVE_TV_SETTINGS)
    def test_tv_screen_rejects_stale_active_role_generation(self):
        login_at = self._login_as(self.dispatcher_access)
        EmployeeAccess.objects.filter(
            pk=self.dispatcher_access.pk,
        ).update(last_login_at=login_at + timedelta(seconds=1))

        response = self.client.get(reverse('driver_rating_tv'))

        self.assertRedirects(
            response,
            reverse('login'),
            fetch_redirect_response=False,
        )

    @override_settings(
        PORTAL_WORKING_DRIVER_RATING_ENABLED=False,
        RATING_TV_SCREEN_ENABLED=False,
        RATING_TV_QA_PREVIEW_ENABLED=False,
    )
    def test_tv_routes_fail_closed_with_default_disabled_flags(self):
        self._login_as(self.dispatcher_access)

        live_response = self.client.get(reverse('driver_rating_tv'))
        qa_response = self.client.get(
            reverse('driver_rating_tv_qa_preview'),
        )
        photo_response = self.client.get(
            reverse(
                'driver_rating_employee_photo',
                args=[self.driver.pk],
            ),
        )

        self.assertEqual(live_response.status_code, 404)
        self.assertEqual(qa_response.status_code, 404)
        self.assertEqual(photo_response.status_code, 404)
        data_response = self.client.get(
            reverse('driver_rating_tv_data_api'),
            {'shift_type': 'night'},
        )
        self.assertEqual(data_response.status_code, 404)
        self.assertIn('no-store', data_response['Cache-Control'])

    @override_settings(**LIVE_TV_SETTINGS, **QA_TV_SETTINGS)
    def test_qa_preview_has_separate_url_and_live_query_cannot_enable_it(self):
        self._login_as(self.dispatcher_access)

        live_url = reverse('driver_rating_tv')
        qa_url = reverse('driver_rating_tv_qa_preview')
        live_response = self.client.get(f'{live_url}?qa_preview=1')
        qa_response = self.client.get(qa_url)

        self.assertNotEqual(live_url, qa_url)
        self.assertEqual(live_response.status_code, 200)
        self.assertFalse(
            live_response.context['rating_tv_qa_preview'],
        )
        self.assertEqual(
            live_response.context['rating_tv_config']['apiUrl'],
            reverse('driver_rating_tv_data_api'),
        )
        self.assertIsNone(
            live_response.context['rating_tv_preview_payload'],
        )
        self.assertNotContains(
            live_response,
            'rating-tv-preview-payload',
        )
        self.assertEqual(qa_response.status_code, 200)
        self.assertTrue(
            qa_response.context['rating_tv_qa_preview'],
        )
        self.assertIn(
            'private',
            qa_response.headers['Cache-Control'],
        )
        self.assertIn(
            'no-store',
            qa_response.headers['Cache-Control'],
        )
        self.assertContains(
            qa_response,
            'rating-tv-preview-payload',
        )

    def test_qa_preview_requires_debug_and_explicit_flag(self):
        self._login_as(self.dispatcher_access)
        qa_url = reverse('driver_rating_tv_qa_preview')

        setting_pairs = (
            (False, False, 404),
            (False, True, 404),
            (True, False, 404),
            (True, True, 200),
        )
        for debug, preview_enabled, expected_status in setting_pairs:
            with self.subTest(
                debug=debug,
                preview_enabled=preview_enabled,
            ):
                with override_settings(
                    DEBUG=debug,
                    RATING_TV_QA_PREVIEW_ENABLED=preview_enabled,
                ):
                    response = self.client.get(qa_url)
                self.assertEqual(response.status_code, expected_status)

    @override_settings(**QA_TV_SETTINGS)
    def test_qa_preview_keeps_management_authentication_boundary(self):
        qa_url = reverse('driver_rating_tv_qa_preview')

        unauthenticated = self.client.get(qa_url)
        self.assertRedirects(
            unauthenticated,
            reverse('login'),
            fetch_redirect_response=False,
        )

        driver_access = self._create_access(
            'driver',
            'Водитель без доступа к QA TV-рейтингу',
        )
        self._login_as(driver_access)
        forbidden = self.client.get(qa_url)
        self.assertRedirects(
            forbidden,
            reverse('role_home'),
            fetch_redirect_response=False,
        )

    @override_settings(**QA_TV_SETTINGS)
    def test_qa_preview_contains_53_synthetic_rows_and_writes_nothing(self):
        self._login_as(self.dispatcher_access)
        before_counts = {
            'employee': Employee.objects.count(),
            'access': EmployeeAccess.objects.count(),
            'role': Role.objects.count(),
            'composition': WatchComposition.objects.count(),
        }

        with CaptureQueriesContext(connection) as captured_queries:
            response = self.client.get(
                reverse('driver_rating_tv_qa_preview'),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.context['rating_tv_preview_payload']
        entries = payload['entries']
        self.assertEqual(len(entries), 53)
        self.assertEqual(payload['summary']['employee_count'], 53)
        self.assertEqual(payload['qa_day_count'], 30)
        self.assertEqual(
            [entry['display_order'] for entry in entries],
            list(range(1, 54)),
        )
        self.assertEqual(
            [entry['employee_id'] for entry in entries],
            list(range(-1, -54, -1)),
        )
        self.assertTrue(
            all(
                entry['full_name'].startswith('Тестов ')
                for entry in entries
            ),
        )
        self.assertTrue(all(entry['score'] for entry in entries))
        after_counts = {
            'employee': Employee.objects.count(),
            'access': EmployeeAccess.objects.count(),
            'role': Role.objects.count(),
            'composition': WatchComposition.objects.count(),
        }
        self.assertEqual(after_counts, before_counts)

        write_sql = re.compile(
            r'^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b',
            flags=re.IGNORECASE,
        )
        writes = [
            query['sql']
            for query in captured_queries.captured_queries
            if write_sql.match(query['sql'])
        ]
        self.assertEqual(writes, [])

    @override_settings(**LIVE_TV_SETTINGS)
    def test_rating_photo_is_private_and_limited_to_scoped_driver(self):
        self._login_as(self.dispatcher_access)
        image_bytes = (
            b'GIF89a\x01\x00\x01\x00\x00\x00\x00\x00\x00'
            b'!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00'
            b'\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
        )

        with TemporaryDirectory(prefix='rating-tv-photo-tests-') as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self.driver.photo.save(
                    'rating-tv-driver.gif',
                    ContentFile(image_bytes),
                    save=True,
                )
                response = self.client.get(
                    reverse(
                        'driver_rating_employee_photo',
                        args=[self.driver.pk],
                    ),
                )
                body = b''.join(response.streaming_content)
                response.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, image_bytes)
        self.assertEqual(response.headers['Content-Type'], 'image/gif')
        self.assertIn('private', response.headers['Cache-Control'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(
            response.headers['X-Content-Type-Options'],
            'nosniff',
        )

    @override_settings(**LIVE_TV_SETTINGS)
    def test_rating_photo_returns_404_without_scope_or_strict_access(self):
        out_of_scope = Employee.objects.create(
            full_name='Сотрудник вне водительской области TV-рейтинга',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        out_of_scope.photo = 'employee_photos/out-of-scope.gif'
        out_of_scope.save(update_fields=['photo'])
        scoped_url = reverse(
            'driver_rating_employee_photo',
            args=[self.driver.pk],
        )
        outside_url = reverse(
            'driver_rating_employee_photo',
            args=[out_of_scope.pk],
        )

        unauthenticated = self.client.get(scoped_url)
        self.assertEqual(unauthenticated.status_code, 404)

        driver_access = self._create_access(
            'driver',
            'Водитель без доступа к фотографиям TV-рейтинга',
        )
        self._login_as(driver_access)
        wrong_role = self.client.get(scoped_url)
        self.assertEqual(wrong_role.status_code, 404)

        self.client = Client()
        login_at = self._login_as(self.dispatcher_access)
        EmployeeAccess.objects.filter(
            pk=self.dispatcher_access.pk,
        ).update(last_login_at=login_at + timedelta(seconds=1))
        stale_generation = self.client.get(scoped_url)
        self.assertEqual(stale_generation.status_code, 404)

        self.client = Client()
        self._login_as(self.dispatcher_access)
        outside_scope = self.client.get(outside_url)
        self.assertEqual(outside_scope.status_code, 404)
