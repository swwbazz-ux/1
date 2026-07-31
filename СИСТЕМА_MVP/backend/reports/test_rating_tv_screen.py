import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.production_time import production_work_date
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import connection
from django.http import JsonResponse
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from users.models import Employee, EmployeeAccess, Role, WatchComposition

from .driver_watch_rating import DRIVER_RATING_LEVELS
from .models import RatingPeriod
from .rating_tv_live_qa import RatingTvLiveQaStateError
from .rating_tv_replay import (
    RATING_TV_REPLAY_DAY_COUNT,
    RATING_TV_REPLAY_EMPLOYEE_COUNT,
    RatingTvReplayError,
    attach_replay_integrity,
    load_rating_tv_replay,
    replay_snapshot_source_fingerprint,
)


LIVE_TV_SETTINGS = {
    'PORTAL_WORKING_DRIVER_RATING_ENABLED': True,
    'RATING_TV_SCREEN_ENABLED': True,
}
QA_TV_SETTINGS = {
    'DEBUG': True,
    'RATING_TV_QA_PREVIEW_ENABLED': True,
}
QA_REPLAY_SETTINGS = {
    **QA_TV_SETTINGS,
    'RATING_TV_QA_REPLAY_ENABLED': True,
}
QA_LIVE_SETTINGS = {
    **LIVE_TV_SETTINGS,
    'PORTAL_WORKING_DRIVER_RATING_ENABLED': False,
    'DEBUG': True,
    'RATING_TV_QA_LIVE_ENABLED': True,
    'RATING_TV_QA_LIVE_RUN_ID': 'rating-live-test-run',
    'PORTAL_SITE_CODE': 'site-2',
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

    def _assert_private_photo_not_found(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertIn('private', response.headers['Cache-Control'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(
            response.headers['X-Content-Type-Options'],
            'nosniff',
        )

    def _qa_live_state(self, **overrides):
        if not hasattr(self, '_qa_live_rating_period'):
            work_date = production_work_date()
            self._qa_live_rating_period = RatingPeriod.objects.create(
                name='QA-live период TV-рейтинга',
                starts_on=work_date - timedelta(days=1),
                ends_before=work_date + timedelta(days=2),
                comment='Изолированная проверка QA-live API.',
            )
        state = {
            'schema': 'driver-rating-qa-live-state',
            'schema_version': 1,
            'synthetic': True,
            'official': False,
            'official_rating_eligible': False,
            'run_id': 'rating-live-test-run',
            'site_code': 'site-2',
            'step': 2,
            'virtual_at': '2026-07-30T20:00:00+04:00',
            'shift_type': 'night',
            'rating_period_id': self._qa_live_rating_period.id,
            'watch_composition_id': self.composition.id,
            'placeholders': [
                {
                    'employee_id': self.driver.id,
                    'status': 'withheld',
                    'reasons': ['blocking_quality:data_conflict'],
                },
            ],
        }
        state.update(overrides)
        return state

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
        RATING_TV_QA_LIVE_ENABLED=False,
        RATING_TV_QA_LIVE_RUN_ID='',
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
        self._assert_private_photo_not_found(photo_response)
        data_response = self.client.get(
            reverse('driver_rating_tv_data_api'),
            {'shift_type': 'night'},
        )
        self.assertEqual(data_response.status_code, 404)
        self.assertIn('no-store', data_response['Cache-Control'])
        replay_response = self.client.get(
            reverse('driver_rating_tv_qa_replay_api'),
        )
        self.assertEqual(replay_response.status_code, 404)
        self.assertIn('no-store', replay_response['Cache-Control'])
        qa_live_response = self.client.get(
            reverse('driver_rating_tv_qa_live'),
        )
        qa_live_state_response = self.client.get(
            reverse('driver_rating_tv_qa_live_state_api'),
        )
        qa_live_data_response = self.client.get(
            reverse('driver_rating_tv_qa_live_data_api'),
            {
                'rating_period': 1,
                'watch_composition': self.composition.id,
                'shift_type': 'night',
            },
        )
        self.assertEqual(qa_live_response.status_code, 404)
        self.assertEqual(qa_live_state_response.status_code, 404)
        self.assertEqual(qa_live_data_response.status_code, 404)
        self.assertIn(
            'no-store',
            qa_live_state_response['Cache-Control'],
        )
        self.assertIn(
            'no-store',
            qa_live_data_response['Cache-Control'],
        )

    def test_tv_interface_flag_matrix_is_independent_from_portal_flag(self):
        self._login_as(self.dispatcher_access)
        image_bytes = (
            b'GIF89a\x01\x00\x01\x00\x00\x00\x00\x00\x00'
            b'!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00'
            b'\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
        )

        with TemporaryDirectory(prefix='rating-tv-flag-matrix-') as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self.driver.photo.save(
                    'rating-tv-flag-matrix.gif',
                    ContentFile(image_bytes),
                    save=True,
                )
                for tv_enabled, portal_enabled in (
                    (False, False),
                    (True, False),
                    (False, True),
                    (True, True),
                ):
                    with (
                        self.subTest(
                            tv_enabled=tv_enabled,
                            portal_enabled=portal_enabled,
                        ),
                        override_settings(
                            RATING_TV_SCREEN_ENABLED=tv_enabled,
                            PORTAL_WORKING_DRIVER_RATING_ENABLED=(
                                portal_enabled
                            ),
                        ),
                    ):
                        page_response = self.client.get(
                            reverse('driver_rating_tv'),
                        )
                        data_response = self.client.get(
                            reverse('driver_rating_tv_data_api'),
                            {'shift_type': 'night'},
                        )
                        photo_response = self.client.get(
                            reverse(
                                'driver_rating_employee_photo',
                                args=[self.driver.pk],
                            ),
                        )

                        expected_status = 200 if tv_enabled else 404
                        self.assertEqual(
                            page_response.status_code,
                            expected_status,
                        )
                        self.assertEqual(
                            data_response.status_code,
                            expected_status,
                        )
                        self.assertEqual(
                            photo_response.status_code,
                            expected_status,
                        )
                        self.assertIn(
                            'no-store',
                            data_response['Cache-Control'],
                        )
                        self.assertEqual(
                            data_response['X-Content-Type-Options'],
                            'nosniff',
                        )
                        if not tv_enabled:
                            self._assert_private_photo_not_found(
                                photo_response,
                            )
                            continue

                        try:
                            body = b''.join(
                                photo_response.streaming_content,
                            )
                        finally:
                            for resource_closer in (
                                photo_response._resource_closers
                            ):
                                resource_closer()
                            photo_response._resource_closers.clear()
                        self.assertEqual(body, image_bytes)
                        self.assertIn(
                            'private',
                            photo_response['Cache-Control'],
                        )
                        self.assertIn(
                            'no-store',
                            photo_response['Cache-Control'],
                        )
                        self.assertEqual(
                            photo_response['X-Content-Type-Options'],
                            'nosniff',
                        )

    @override_settings(**QA_LIVE_SETTINGS)
    def test_qa_live_screen_is_separate_loopback_only_and_never_query_enabled(
        self,
    ):
        self._login_as(self.dispatcher_access)
        live_url = reverse('driver_rating_tv')
        qa_live_url = reverse('driver_rating_tv_qa_live')

        live_response = self.client.get(f'{live_url}?qa_live=1')
        qa_live_response = self.client.get(qa_live_url)
        forwarded_only_response = self.client.get(
            qa_live_url,
            REMOTE_ADDR='192.0.2.15',
            HTTP_X_FORWARDED_FOR='127.0.0.1',
        )

        self.assertEqual(live_response.status_code, 200)
        self.assertFalse(live_response.context['rating_tv_qa_live'])
        self.assertNotContains(
            live_response,
            'СИНТЕТИЧЕСКИЙ ТЕСТ',
        )
        self.assertEqual(qa_live_response.status_code, 200)
        self.assertTrue(qa_live_response.context['rating_tv_qa_live'])
        self.assertEqual(
            qa_live_response.context['rating_tv_config']['apiUrl'],
            reverse('driver_rating_tv_qa_live_data_api'),
        )
        self.assertEqual(
            qa_live_response.context['rating_tv_config']['qaLiveStateUrl'],
            reverse('driver_rating_tv_qa_live_state_api'),
        )
        self.assertEqual(
            qa_live_response.context['rating_tv_config']['refreshSeconds'],
            10,
        )
        self.assertContains(
            qa_live_response,
            (
                'СИНТЕТИЧЕСКИЙ ТЕСТ · НЕ РЕАЛЬНЫЕ ДАННЫЕ '
                '· НЕ ДЛЯ ПРЕМИРОВАНИЯ'
            ),
        )
        self.assertIn('private', qa_live_response['Cache-Control'])
        self.assertIn('no-store', qa_live_response['Cache-Control'])
        self.assertEqual(
            qa_live_response['X-Content-Type-Options'],
            'nosniff',
        )
        self.assertEqual(forwarded_only_response.status_code, 404)

    @override_settings(**{
        **QA_LIVE_SETTINGS,
        'RATING_TV_QA_LIVE_RUN_ID': '',
    })
    def test_qa_live_gate_fails_closed_without_expected_run_id(self):
        self._login_as(self.dispatcher_access)

        response = self.client.get(reverse('driver_rating_tv_qa_live'))
        state_response = self.client.get(
            reverse('driver_rating_tv_qa_live_state_api'),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(state_response.status_code, 404)
        self.assertIn('private', state_response['Cache-Control'])
        self.assertIn('no-store', state_response['Cache-Control'])
        self.assertEqual(
            state_response['X-Content-Type-Options'],
            'nosniff',
        )

    @override_settings(**QA_LIVE_SETTINGS)
    def test_qa_live_keeps_management_authentication_boundary(self):
        page_url = reverse('driver_rating_tv_qa_live')
        state_url = reverse('driver_rating_tv_qa_live_state_api')

        unauthenticated_page = self.client.get(page_url)
        unauthenticated_state = self.client.get(state_url)

        self.assertRedirects(
            unauthenticated_page,
            reverse('login'),
            fetch_redirect_response=False,
        )
        self.assertEqual(unauthenticated_state.status_code, 401)
        self.assertIn(
            'no-store',
            unauthenticated_state['Cache-Control'],
        )

        driver_access = self._create_access(
            'driver',
            'Водитель без доступа к QA-live TV-рейтингу',
        )
        self._login_as(driver_access)
        forbidden_page = self.client.get(page_url)
        forbidden_state = self.client.get(state_url)

        self.assertRedirects(
            forbidden_page,
            reverse('role_home'),
            fetch_redirect_response=False,
        )
        self.assertEqual(forbidden_state.status_code, 403)
        self.assertIn('no-store', forbidden_state['Cache-Control'])

    @override_settings(**QA_LIVE_SETTINGS)
    def test_qa_live_state_is_strict_scoped_and_contains_no_rating_values(self):
        self._login_as(self.dispatcher_access)
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / 'rating-live-state.json'
            live_state = self._qa_live_state()
            state_path.write_text(
                json.dumps(live_state),
                encoding='utf-8',
            )
            with override_settings(
                RATING_TV_QA_LIVE_STATE_PATH=state_path,
            ):
                response = self.client.get(
                    reverse('driver_rating_tv_qa_live_state_api'),
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['synthetic'])
        self.assertFalse(payload['official'])
        self.assertFalse(payload['official_rating_eligible'])
        self.assertEqual(payload['run_id'], 'rating-live-test-run')
        self.assertEqual(payload['step'], 2)
        self.assertEqual(payload['shift_type'], 'night')
        self.assertEqual(
            payload['placeholders'],
            [
                {
                    'employee_id': self.driver.id,
                    'status': 'withheld',
                    'reasons': ['blocking_quality:data_conflict'],
                    'full_name': self.driver.full_name,
                },
            ],
        )
        serialized = json.dumps(payload)
        for forbidden_key in (
            'score',
            'place',
            'blocks',
            'weights',
            'source_fingerprint',
            'shift_score_fingerprint',
            'snapshot_revision',
            'employee_count',
            'placeholder_count',
        ):
            self.assertNotIn(f'"{forbidden_key}"', serialized)
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    @override_settings(**QA_LIVE_SETTINGS)
    def test_qa_live_state_rejects_stale_foreign_and_calculated_sidecars(self):
        self._login_as(self.dispatcher_access)
        variants = (
            ('foreign-run', {'run_id': 'other-run'}, False),
            ('foreign-site', {'site_code': 'other-site'}, False),
            ('wrong-schema', {'schema': 'other-schema'}, False),
            (
                'official-eligible',
                {'official_rating_eligible': True},
                False,
            ),
            ('calculated', {'score': '99.0000'}, False),
            ('counted', {'placeholder_count': 1}, False),
            (
                'empty-reasons',
                {
                    'placeholders': [
                        {
                            'employee_id': self.driver.id,
                            'status': 'withheld',
                            'reasons': [],
                        },
                    ],
                },
                False,
            ),
            ('stale', {}, True),
        )
        with TemporaryDirectory() as directory:
            for name, overrides, stale in variants:
                with self.subTest(name=name):
                    state_path = Path(directory) / f'{name}.json'
                    state_path.write_text(
                        json.dumps(self._qa_live_state(**overrides)),
                        encoding='utf-8',
                    )
                    if stale:
                        stale_at = (
                            timezone.now() - timedelta(minutes=10)
                        ).timestamp()
                        os.utime(state_path, (stale_at, stale_at))
                    with override_settings(
                        RATING_TV_QA_LIVE_STATE_PATH=state_path,
                        RATING_TV_QA_LIVE_MAX_AGE_SECONDS=120,
                    ):
                        response = self.client.get(
                            reverse(
                                'driver_rating_tv_qa_live_state_api'
                            ),
                        )
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(
                        response.json(),
                        {
                            'error': (
                                'Ожидание актуального шага синтетического '
                                'QA-live прогона.'
                            ),
                        },
                    )
                    self.assertIn('no-store', response['Cache-Control'])

    @override_settings(**QA_LIVE_SETTINGS)
    def test_qa_live_state_rejects_linked_parent_path(self):
        self._login_as(self.dispatcher_access)
        with TemporaryDirectory() as directory:
            linked_parent = Path(directory) / 'linked-parent'
            linked_parent.mkdir()
            state_path = linked_parent / 'rating-live-state.json'
            state_path.write_text(
                json.dumps(self._qa_live_state()),
                encoding='utf-8',
            )
            with (
                override_settings(
                    RATING_TV_QA_LIVE_STATE_PATH=state_path,
                ),
                patch(
                    (
                        'reports.rating_tv_live_qa.'
                        '_path_is_link_or_junction'
                    ),
                    side_effect=lambda path: path == linked_parent,
                ),
            ):
                response = self.client.get(
                    reverse('driver_rating_tv_qa_live_state_api'),
                )

        self.assertEqual(response.status_code, 503)
        self.assertIn('no-store', response['Cache-Control'])

    @override_settings(**QA_LIVE_SETTINGS)
    def test_qa_live_state_rejects_file_changed_during_read(self):
        self._login_as(self.dispatcher_access)
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / 'rating-live-state.json'
            state_path.write_text(
                json.dumps(self._qa_live_state()),
                encoding='utf-8',
            )
            real_fstat = os.fstat
            fstat_call_count = 0

            def changing_fstat(descriptor):
                nonlocal fstat_call_count
                fstat_call_count += 1
                result = real_fstat(descriptor)
                if fstat_call_count == 1:
                    return result

                class ChangedStat:
                    st_mode = result.st_mode
                    st_dev = result.st_dev
                    st_ino = result.st_ino
                    st_size = result.st_size
                    st_mtime_ns = result.st_mtime_ns + 1

                return ChangedStat()

            with (
                override_settings(
                    RATING_TV_QA_LIVE_STATE_PATH=state_path,
                ),
                patch(
                    'reports.rating_tv_live_qa.os.fstat',
                    side_effect=changing_fstat,
                ),
            ):
                response = self.client.get(
                    reverse('driver_rating_tv_qa_live_state_api'),
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(fstat_call_count, 2)
        self.assertIn('no-store', response['Cache-Control'])

    @override_settings(**QA_LIVE_SETTINGS)
    def test_qa_live_data_allows_only_sidecar_group_and_passes_materialized(
        self,
    ):
        self._login_as(self.dispatcher_access)
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / 'rating-live-state.json'
            live_state = self._qa_live_state()
            state_path.write_text(
                json.dumps(live_state),
                encoding='utf-8',
            )
            with override_settings(
                RATING_TV_QA_LIVE_STATE_PATH=state_path,
            ):
                mismatch = self.client.get(
                    reverse('driver_rating_tv_qa_live_data_api'),
                    {
                        'rating_period': 999,
                        'watch_composition': self.composition.id,
                        'shift_type': 'night',
                        'qa_run_id': live_state['run_id'],
                        'qa_step': live_state['step'],
                    },
                )

                materialized_response = JsonResponse({
                    'available': True,
                    'official': False,
                    'rating_period': {
                        'id': live_state['rating_period_id'],
                    },
                    'watch_composition': {'id': self.composition.id},
                    'shift_type': 'night',
                    'snapshot_revision': 7,
                    'source_fingerprint': 'source-from-materialized',
                    'shift_score_fingerprint': 'scores-from-materialized',
                    'entries': [],
                })
                materialized_response.headers['Cache-Control'] = (
                    'private, no-store'
                )
                materialized_response.headers['X-Content-Type-Options'] = (
                    'nosniff'
                )
                with patch(
                    'reports.views._resolve_driver_period_rating',
                    return_value=materialized_response,
                ) as materialized_api:
                    success = self.client.get(
                        reverse('driver_rating_tv_qa_live_data_api'),
                        {
                            'rating_period': (
                                live_state['rating_period_id']
                            ),
                            'watch_composition': self.composition.id,
                            'shift_type': 'night',
                            'qa_run_id': live_state['run_id'],
                            'qa_step': live_state['step'],
                        },
                    )

        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.json()['snapshot_revision'], 7)
        self.assertEqual(
            success.json()['source_fingerprint'],
            'source-from-materialized',
        )
        self.assertEqual(
            success.json()['shift_score_fingerprint'],
            'scores-from-materialized',
        )
        self.assertNotIn('step', success.json())
        self.assertEqual(materialized_api.call_count, 1)
        self.assertIn('private', success['Cache-Control'])
        self.assertIn('no-store', success['Cache-Control'])

    @override_settings(**QA_LIVE_SETTINGS)
    def test_qa_live_data_discards_payload_when_state_identity_changes(self):
        self._login_as(self.dispatcher_access)
        state_before = self._qa_live_state()
        state_after = deepcopy(state_before)
        state_after.update({
            'step': state_before['step'] + 1,
            'virtual_at': '2026-07-30T21:00:00+04:00',
        })
        materialized_response = JsonResponse({
            'available': True,
            'snapshot_revision': 11,
            'source_fingerprint': 'must-not-leak',
            'entries': [{'employee_id': self.driver.id}],
        })
        with (
            patch(
                'reports.views._rating_tv_qa_live_state_for_scope',
                side_effect=[state_before, state_after],
            ) as state_reader,
            patch(
                'reports.views._resolve_driver_period_rating',
                return_value=materialized_response,
            ) as materialized_reader,
        ):
            response = self.client.get(
                reverse('driver_rating_tv_qa_live_data_api'),
                {
                    'rating_period': state_before['rating_period_id'],
                    'watch_composition': (
                        state_before['watch_composition_id']
                    ),
                    'shift_type': state_before['shift_type'],
                    'qa_run_id': state_before['run_id'],
                    'qa_step': state_before['step'],
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {
                'error': (
                    'Ожидание актуального шага синтетического '
                    'QA-live прогона.'
                ),
            },
        )
        self.assertNotIn('snapshot_revision', response.content.decode())
        self.assertNotIn('must-not-leak', response.content.decode())
        self.assertEqual(state_reader.call_count, 2)
        self.assertEqual(materialized_reader.call_count, 1)
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    @override_settings(**QA_LIVE_SETTINGS)
    def test_qa_live_data_discards_payload_when_second_state_read_fails(self):
        self._login_as(self.dispatcher_access)
        state_before = self._qa_live_state()
        materialized_response = JsonResponse({
            'available': True,
            'snapshot_revision': 12,
            'source_fingerprint': 'must-not-leak-after-stale',
            'entries': [{'employee_id': self.driver.id}],
        })
        with (
            patch(
                'reports.views._rating_tv_qa_live_state_for_scope',
                side_effect=[
                    state_before,
                    RatingTvLiveQaStateError('state became stale'),
                ],
            ) as state_reader,
            patch(
                'reports.views._resolve_driver_period_rating',
                return_value=materialized_response,
            ) as materialized_reader,
        ):
            response = self.client.get(
                reverse('driver_rating_tv_qa_live_data_api'),
                {
                    'rating_period': state_before['rating_period_id'],
                    'watch_composition': (
                        state_before['watch_composition_id']
                    ),
                    'shift_type': state_before['shift_type'],
                    'qa_run_id': state_before['run_id'],
                    'qa_step': state_before['step'],
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn('snapshot_revision', response.content.decode())
        self.assertNotIn(
            'must-not-leak-after-stale',
            response.content.decode(),
        )
        self.assertEqual(state_reader.call_count, 2)
        self.assertEqual(materialized_reader.call_count, 1)
        self.assertIn('no-store', response['Cache-Control'])

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

    @override_settings(**QA_REPLAY_SETTINGS)
    def test_qa_replay_api_returns_one_verified_30_day_artifact(self):
        self._login_as(self.dispatcher_access)

        response = self.client.get(
            reverse('driver_rating_tv_qa_replay_api'),
            {
                'file': r'C:\forbidden\other.json',
                'day': '12',
                'watch_composition': '999',
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload['snapshots']), 30)
        self.assertEqual(
            [snapshot['day'] for snapshot in payload['snapshots']],
            list(range(1, 31)),
        )
        self.assertTrue(payload['synthetic'])
        self.assertFalse(payload['official'])
        self.assertFalse(payload['official_rating_eligible'])
        self.assertFalse(payload['replay']['formula_evaluated'])
        self.assertEqual(
            len(payload['snapshots'][0]['payload']['entries']),
            53,
        )
        self.assertEqual(
            response['X-Rating-Replay-SHA256'],
            hashlib.sha256(
                Path(settings.RATING_TV_QA_REPLAY_ARTIFACT).read_bytes(),
            ).hexdigest().upper(),
        )
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(
            response['X-Content-Type-Options'],
            'nosniff',
        )

    @override_settings(**QA_REPLAY_SETTINGS)
    def test_qa_replay_api_reads_only_and_keeps_fixed_synthetic_scope(self):
        self._login_as(self.dispatcher_access)
        before_counts = {
            'employee': Employee.objects.count(),
            'access': EmployeeAccess.objects.count(),
            'role': Role.objects.count(),
            'composition': WatchComposition.objects.count(),
        }

        with CaptureQueriesContext(connection) as captured_queries:
            response = self.client.get(
                reverse('driver_rating_tv_qa_replay_api'),
            )

        self.assertEqual(response.status_code, 200)
        replay = response.json()
        fixed_scope = replay['scope']
        employee_ids = None
        for snapshot in replay['snapshots']:
            payload = snapshot['payload']
            self.assertEqual(
                payload['rating_period'],
                fixed_scope['rating_period'],
            )
            self.assertEqual(
                payload['watch_composition'],
                fixed_scope['watch_composition'],
            )
            self.assertEqual(
                payload['shift_type'],
                fixed_scope['shift_type'],
            )
            current_ids = {
                entry['employee_id']
                for entry in payload['entries']
            }
            self.assertTrue(all(value < 0 for value in current_ids))
            self.assertTrue(
                all(
                    entry['full_name'].startswith('ТЕСТ_')
                    for entry in payload['entries']
                ),
            )
            if employee_ids is None:
                employee_ids = current_ids
            else:
                self.assertEqual(current_ids, employee_ids)
        self.assertEqual(
            {
                'employee': Employee.objects.count(),
                'access': EmployeeAccess.objects.count(),
                'role': Role.objects.count(),
                'composition': WatchComposition.objects.count(),
            },
            before_counts,
        )
        write_sql = re.compile(
            r'^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b',
            flags=re.IGNORECASE,
        )
        self.assertEqual(
            [
                query['sql']
                for query in captured_queries.captured_queries
                if write_sql.match(query['sql'])
            ],
            [],
        )

    def test_qa_replay_api_requires_every_debug_flag_and_current_access(self):
        replay_url = reverse('driver_rating_tv_qa_replay_api')
        self._login_as(self.dispatcher_access)

        for debug, preview_enabled, replay_enabled, expected in (
            (False, True, True, 404),
            (True, False, True, 404),
            (True, True, False, 404),
            (True, True, True, 200),
        ):
            with self.subTest(
                debug=debug,
                preview_enabled=preview_enabled,
                replay_enabled=replay_enabled,
            ):
                with override_settings(
                    DEBUG=debug,
                    RATING_TV_QA_PREVIEW_ENABLED=preview_enabled,
                    RATING_TV_QA_REPLAY_ENABLED=replay_enabled,
                ):
                    response = self.client.get(replay_url)
                self.assertEqual(response.status_code, expected)

        self.client = Client()
        with override_settings(**QA_REPLAY_SETTINGS):
            unauthenticated = self.client.get(replay_url)
        self.assertEqual(unauthenticated.status_code, 401)

        driver_access = self._create_access(
            'driver',
            'Водитель без доступа к replay TV-рейтинга',
        )
        self._login_as(driver_access)
        with override_settings(**QA_REPLAY_SETTINGS):
            forbidden = self.client.get(replay_url)
        self.assertEqual(forbidden.status_code, 403)

    @override_settings(
        **QA_REPLAY_SETTINGS,
        RATING_TV_QA_REPLAY_SHA256='0' * 64,
    )
    def test_qa_replay_api_fails_closed_on_external_checksum_mismatch(self):
        self._login_as(self.dispatcher_access)

        response = self.client.get(
            reverse('driver_rating_tv_qa_replay_api'),
        )

        self.assertEqual(response.status_code, 409)
        self.assertNotIn('snapshots', response.json())
        self.assertIn('целостности', response.json()['error'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(
            response['X-Content-Type-Options'],
            'nosniff',
        )

    @override_settings(**QA_REPLAY_SETTINGS)
    def test_qa_preview_exposes_replay_url_only_as_explicit_qa_mode(self):
        self._login_as(self.dispatcher_access)

        qa_response = self.client.get(
            reverse('driver_rating_tv_qa_preview'),
        )
        live_response = self.client.get(
            reverse('driver_rating_tv') + '?qa_replay=1',
        )

        self.assertEqual(qa_response.status_code, 200)
        self.assertTrue(
            qa_response.context['rating_tv_config']['qaReplayEnabled'],
        )
        self.assertEqual(
            qa_response.context['rating_tv_config']['qaReplayUrl'],
            reverse('driver_rating_tv_qa_replay_api'),
        )
        self.assertEqual(live_response.status_code, 404)

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
                try:
                    body = b''.join(response.streaming_content)
                finally:
                    # FileResponse.close() also emits request_finished. With
                    # PostgreSQL that closes the TestCase class transaction's
                    # connection and contaminates every following test.
                    for resource_closer in response._resource_closers:
                        resource_closer()
                    response._resource_closers.clear()

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
        self._assert_private_photo_not_found(unauthenticated)

        driver_access = self._create_access(
            'driver',
            'Водитель без доступа к фотографиям TV-рейтинга',
        )
        self._login_as(driver_access)
        wrong_role = self.client.get(scoped_url)
        self._assert_private_photo_not_found(wrong_role)

        self.client = Client()
        login_at = self._login_as(self.dispatcher_access)
        EmployeeAccess.objects.filter(
            pk=self.dispatcher_access.pk,
        ).update(last_login_at=login_at + timedelta(seconds=1))
        stale_generation = self.client.get(scoped_url)
        self._assert_private_photo_not_found(stale_generation)

        self.client = Client()
        self._login_as(self.dispatcher_access)
        no_photo = self.client.get(scoped_url)
        self._assert_private_photo_not_found(no_photo)

        with TemporaryDirectory(prefix='rating-tv-missing-photo-') as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self.driver.photo = 'employee_photos/missing.gif'
                self.driver.save(update_fields=['photo'])
                missing_file = self.client.get(scoped_url)
        self._assert_private_photo_not_found(missing_file)

        outside_scope = self.client.get(outside_url)
        self._assert_private_photo_not_found(outside_scope)


class DriverRatingTvReplayContractTests(SimpleTestCase):
    def _artifact_bytes(self):
        return Path(settings.RATING_TV_QA_REPLAY_ARTIFACT).read_bytes()

    def _artifact_document(self):
        return json.loads(self._artifact_bytes().decode('utf-8'))

    def _write_document(self, directory, document, filename='replay.json'):
        path = Path(directory) / filename
        raw = (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + '\n'
        ).encode('utf-8')
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest().upper()

    def test_saved_replay_contains_exactly_30_contiguous_days_and_53_people(self):
        document, raw_sha256 = load_rating_tv_replay(
            settings.RATING_TV_QA_REPLAY_ARTIFACT,
            expected_sha256=settings.RATING_TV_QA_REPLAY_SHA256,
        )

        self.assertEqual(raw_sha256, settings.RATING_TV_QA_REPLAY_SHA256)
        self.assertEqual(
            [snapshot['day'] for snapshot in document['snapshots']],
            list(range(1, RATING_TV_REPLAY_DAY_COUNT + 1)),
        )
        baseline_ids = {
            entry['employee_id']
            for entry in document['snapshots'][0]['payload']['entries']
        }
        self.assertEqual(
            len(baseline_ids),
            RATING_TV_REPLAY_EMPLOYEE_COUNT,
        )
        self.assertTrue(all(employee_id < 0 for employee_id in baseline_ids))
        for snapshot in document['snapshots']:
            entries = snapshot['payload']['entries']
            self.assertEqual(len(entries), RATING_TV_REPLAY_EMPLOYEE_COUNT)
            self.assertEqual(
                {entry['employee_id'] for entry in entries},
                baseline_ids,
            )
            self.assertTrue(
                all(entry['full_name'].startswith('ТЕСТ_') for entry in entries),
            )
            self.assertFalse(snapshot['payload']['official'])
            self.assertFalse(
                snapshot['payload']['official_rating_eligible'],
            )
            self.assertTrue(snapshot['payload']['synthetic'])

    def test_saved_position_delta_is_bound_to_previous_calendar_day(self):
        document, _raw_sha256 = load_rating_tv_replay(
            settings.RATING_TV_QA_REPLAY_ARTIFACT,
            expected_sha256=settings.RATING_TV_QA_REPLAY_SHA256,
        )
        previous_places = None

        for snapshot in document['snapshots']:
            entries = snapshot['payload']['entries']
            places = {
                entry['employee_id']: entry['place']
                for entry in entries
            }
            for entry in entries:
                expected_delta = (
                    0
                    if previous_places is None
                    else (
                        previous_places[entry['employee_id']]
                        - entry['place']
                    )
                )
                self.assertEqual(entry['position_delta'], expected_delta)
                self.assertIsInstance(entry['position_delta'], int)
                self.assertRegex(entry['score'], r'^\d{1,3}\.\d{2}$')
            previous_places = places

    def test_saved_replay_uses_dense_places_for_equal_scores(self):
        document, _raw_sha256 = load_rating_tv_replay(
            settings.RATING_TV_QA_REPLAY_ARTIFACT,
            expected_sha256=settings.RATING_TV_QA_REPLAY_SHA256,
        )
        tie_count = 0

        for snapshot in document['snapshots']:
            entries = sorted(
                snapshot['payload']['entries'],
                key=lambda entry: entry['display_order'],
            )
            dense_place_by_score = {}
            previous_score = None
            for display_order, entry in enumerate(entries, start=1):
                score = Decimal(entry['score'])
                if previous_score is not None:
                    self.assertLessEqual(score, previous_score)
                if score not in dense_place_by_score:
                    dense_place_by_score[score] = (
                        len(dense_place_by_score) + 1
                    )
                else:
                    tie_count += 1
                expected_place = dense_place_by_score[score]
                self.assertEqual(entry['display_order'], display_order)
                self.assertEqual(entry['place'], expected_place)
                self.assertEqual(
                    entry['shared_score_place'],
                    expected_place,
                )
                self.assertEqual(
                    entry['level'],
                    DRIVER_RATING_LEVELS.get(expected_place, ''),
                )
                previous_score = score

        self.assertGreater(tie_count, 0)

    def test_replay_rejects_split_places_for_one_equal_score(self):
        document = self._artifact_document()
        first_payload = document['snapshots'][0]['payload']
        entries = first_payload['entries']
        first_score = entries[0]['score']
        split_entry = next(
            entry
            for entry in entries[1:]
            if entry['score'] != first_score
        )
        split_entry['score'] = first_score
        first_payload['source_fingerprint'] = (
            replay_snapshot_source_fingerprint(
                document['replay']['id'],
                1,
                entries,
            )
        )
        document = attach_replay_integrity(document)

        with TemporaryDirectory(prefix='rating-tv-replay-tie-') as temp_dir:
            path, raw_sha256 = self._write_document(
                temp_dir,
                document,
            )
            with self.assertRaises(RatingTvReplayError):
                load_rating_tv_replay(
                    path,
                    expected_sha256=raw_sha256,
                )

    def test_replay_rejects_unexpected_fields_at_every_contract_level(self):
        base = self._artifact_document()
        mutations = {}

        top_level = deepcopy(base)
        top_level['employee_private_dump'] = {'passport': 'forbidden'}
        mutations['top_level'] = top_level

        replay_level = deepcopy(base)
        replay_level['replay']['employee_private_dump'] = {'snils': 'x'}
        mutations['replay'] = replay_level

        scope_level = deepcopy(base)
        scope_level['scope']['employee_private_dump'] = {'phone': 'x'}
        mutations['scope'] = scope_level

        snapshot_level = deepcopy(base)
        snapshot_level['snapshots'][0]['employee_private_dump'] = {'pin': 'x'}
        mutations['snapshot'] = snapshot_level

        payload_level = deepcopy(base)
        payload_level['snapshots'][0]['payload']['employee_private_dump'] = {
            'email': 'x',
        }
        mutations['payload'] = payload_level

        entry_level = deepcopy(base)
        entry_payload = entry_level['snapshots'][0]['payload']
        entry_payload['entries'][0]['snils'] = 'forbidden'
        entry_payload['source_fingerprint'] = (
            replay_snapshot_source_fingerprint(
                entry_level['replay']['id'],
                1,
                entry_payload['entries'],
            )
        )
        mutations['entry'] = entry_level

        summary_level = deepcopy(base)
        summary_level['snapshots'][0]['payload']['summary'][
            'employee_private_dump'
        ] = {'passport': 'x'}
        mutations['summary'] = summary_level

        with TemporaryDirectory(prefix='rating-tv-replay-fields-') as temp_dir:
            for name, document in mutations.items():
                with self.subTest(name=name):
                    document = attach_replay_integrity(document)
                    path, raw_sha256 = self._write_document(
                        temp_dir,
                        document,
                        filename=f'{name}.json',
                    )
                    with self.assertRaisesRegex(
                        RatingTvReplayError,
                        'строгий список полей',
                    ):
                        load_rating_tv_replay(
                            path,
                            expected_sha256=raw_sha256,
                        )

    def test_replay_rejects_unavailable_saved_day(self):
        document = self._artifact_document()
        document['snapshots'][11]['payload']['available'] = False
        document = attach_replay_integrity(document)

        with TemporaryDirectory(
            prefix='rating-tv-replay-unavailable-',
        ) as temp_dir:
            path, raw_sha256 = self._write_document(
                temp_dir,
                document,
            )
            with self.assertRaisesRegex(
                RatingTvReplayError,
                'available',
            ):
                load_rating_tv_replay(
                    path,
                    expected_sha256=raw_sha256,
                )

    def test_exact_byte_external_sha_rejects_reformatted_valid_json(self):
        raw = self._artifact_bytes()
        with TemporaryDirectory(prefix='rating-tv-replay-byte-sha-') as temp_dir:
            path = Path(temp_dir) / 'replay.json'
            path.write_bytes(raw + b' ')

            with self.assertRaises(RatingTvReplayError):
                load_rating_tv_replay(
                    path,
                    expected_sha256=settings.RATING_TV_QA_REPLAY_SHA256,
                )

    def test_replay_rejects_missing_day_changed_employee_and_false_official(self):
        base = self._artifact_document()
        mutations = {}

        missing_day = deepcopy(base)
        missing_day['snapshots'].pop(10)
        mutations['missing_day'] = attach_replay_integrity(missing_day)

        changed_employee = deepcopy(base)
        changed_entry = changed_employee['snapshots'][1]['payload']['entries'][0]
        changed_entry['employee_id'] = -9999
        changed_payload = changed_employee['snapshots'][1]['payload']
        changed_payload['source_fingerprint'] = (
            replay_snapshot_source_fingerprint(
                changed_employee['replay']['id'],
                2,
                changed_payload['entries'],
            )
        )
        mutations['changed_employee'] = attach_replay_integrity(
            changed_employee,
        )

        false_official = deepcopy(base)
        false_official['snapshots'][5]['payload']['official'] = True
        mutations['false_official'] = attach_replay_integrity(
            false_official,
        )

        wrong_delta = deepcopy(base)
        delta_payload = wrong_delta['snapshots'][6]['payload']
        delta_payload['entries'][0]['position_delta'] += 1
        delta_payload['source_fingerprint'] = (
            replay_snapshot_source_fingerprint(
                wrong_delta['replay']['id'],
                7,
                delta_payload['entries'],
            )
        )
        mutations['wrong_delta'] = attach_replay_integrity(wrong_delta)

        with TemporaryDirectory(prefix='rating-tv-replay-mutations-') as temp_dir:
            for name, document in mutations.items():
                with self.subTest(name=name):
                    path, raw_sha256 = self._write_document(
                        temp_dir,
                        document,
                        filename=f'{name}.json',
                    )
                    with self.assertRaises(RatingTvReplayError):
                        load_rating_tv_replay(
                            path,
                            expected_sha256=raw_sha256,
                        )

    def test_replay_rejects_positive_id_fractional_delta_and_nan(self):
        base = self._artifact_document()
        mutations = {}

        positive_id = deepcopy(base)
        positive_payload = positive_id['snapshots'][0]['payload']
        positive_payload['entries'][0]['employee_id'] = 1
        positive_payload['source_fingerprint'] = (
            replay_snapshot_source_fingerprint(
                positive_id['replay']['id'],
                1,
                positive_payload['entries'],
            )
        )
        mutations['positive_id'] = attach_replay_integrity(positive_id)

        fractional_delta = deepcopy(base)
        fractional_payload = fractional_delta['snapshots'][2]['payload']
        fractional_payload['entries'][0]['position_delta'] = 0.5
        fractional_payload['source_fingerprint'] = (
            replay_snapshot_source_fingerprint(
                fractional_delta['replay']['id'],
                3,
                fractional_payload['entries'],
            )
        )
        mutations['fractional_delta'] = attach_replay_integrity(
            fractional_delta,
        )

        with TemporaryDirectory(prefix='rating-tv-replay-values-') as temp_dir:
            for name, document in mutations.items():
                with self.subTest(name=name):
                    path, raw_sha256 = self._write_document(
                        temp_dir,
                        document,
                        filename=f'{name}.json',
                    )
                    with self.assertRaises(RatingTvReplayError):
                        load_rating_tv_replay(
                            path,
                            expected_sha256=raw_sha256,
                        )

            nan_path = Path(temp_dir) / 'nan.json'
            nan_raw = self._artifact_bytes().replace(
                b'"score": "',
                b'"score": NaN, "ignored_score": "',
                1,
            )
            nan_path.write_bytes(nan_raw)
            with self.assertRaises(RatingTvReplayError):
                load_rating_tv_replay(
                    nan_path,
                    expected_sha256=hashlib.sha256(
                        nan_raw,
                    ).hexdigest().upper(),
                )
