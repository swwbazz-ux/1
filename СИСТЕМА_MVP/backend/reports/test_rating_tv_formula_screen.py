import hashlib
import json
import re
from copy import deepcopy
from datetime import timedelta
from tempfile import TemporaryDirectory

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
from users.models import Employee, EmployeeAccess, Role

from .rating_tv_formula_replay import (
    RATING_TV_FORMULA_REPLAY_SCHEMA,
    attach_formula_replay_integrity,
)
from .test_rating_tv_formula_replay import FormulaReplayDocumentFactory


FORMULA_COMMON_SETTINGS = {
    'DEBUG': True,
    'RATING_TV_QA_PREVIEW_ENABLED': True,
}


class DriverRatingTvFormulaScreenTests(TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory(
            prefix='rating-tv-formula-screen-',
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.day_path, self.day_sha256 = self._write_document('day')
        self.night_path, self.night_sha256 = self._write_document('night')
        self.dispatcher_access = self._create_access(
            'dispatcher',
            'Диспетчер формульного TV-рейтинга',
        )

    def _create_access(self, role_code, employee_name):
        role, _created = Role.objects.get_or_create(
            code=role_code,
            defaults={
                'name': f'Роль {role_code} формульного TV-рейтинга',
            },
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
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = (
            login_at.isoformat()
        )
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = access.role.code
        session.save()
        return login_at

    def _formula_document(self, shift_type):
        document = FormulaReplayDocumentFactory().build()
        if shift_type == 'night':
            return document

        document = deepcopy(document)
        document['replay']['id'] += '-DAY'
        document['scope']['shift_type'] = 'day'
        document['scope']['shift_type_label'] = 'Дневная'
        document['scope']['watch_composition'] = {
            'id': -3202,
            'code': 'qa-formula-replay-day',
            'name': 'Тестовый дневной состав формульного replay',
            'is_active': True,
        }
        for snapshot in document['snapshots']:
            snapshot.pop('previous_payload_sha256', None)
            snapshot.pop('payload_sha256', None)
            payload = snapshot['payload']
            payload['replay_run_id'] = document['replay']['id']
            payload['shift_type'] = 'day'
            payload['shift_type_label'] = 'Дневная'
            payload['source_raw_path'] = payload[
                'source_raw_path'
            ].replace('raw_formula/night/', 'raw_formula/day/')
            payload['watch_composition'] = deepcopy(
                document['scope']['watch_composition'],
            )
            payload['available_watch_compositions'] = [
                deepcopy(document['scope']['watch_composition']),
            ]
        document.pop('integrity', None)
        return attach_formula_replay_integrity(document)

    def _write_document(self, shift_type):
        path = (
            f'{self.temporary_directory.name}'
            f'/formula-{shift_type}.json'
        )
        raw = json.dumps(
            self._formula_document(shift_type),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
        with open(path, 'wb') as artifact:
            artifact.write(raw)
        return path, hashlib.sha256(raw).hexdigest().upper()

    def _settings(self, *, day=True, night=True):
        return {
            **FORMULA_COMMON_SETTINGS,
            'RATING_TV_QA_FORMULA_REPLAY_DAY_ENABLED': day,
            'RATING_TV_QA_FORMULA_REPLAY_DAY_ARTIFACT': self.day_path,
            'RATING_TV_QA_FORMULA_REPLAY_DAY_SHA256': (
                self.day_sha256
            ),
            'RATING_TV_QA_FORMULA_REPLAY_NIGHT_ENABLED': night,
            'RATING_TV_QA_FORMULA_REPLAY_NIGHT_ARTIFACT': (
                self.night_path
            ),
            'RATING_TV_QA_FORMULA_REPLAY_NIGHT_SHA256': (
                self.night_sha256
            ),
        }

    def assert_private_json(self, response):
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(
            response['X-Content-Type-Options'],
            'nosniff',
        )

    def test_formula_preview_is_separate_explicit_and_private(self):
        self._login_as(self.dispatcher_access)

        with override_settings(**self._settings()):
            response = self.client.get(
                reverse('driver_rating_tv_formula_qa_preview'),
            )
            visual_response = self.client.get(
                reverse('driver_rating_tv_qa_preview'),
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(
            reverse('driver_rating_tv_formula_qa_preview'),
            reverse('driver_rating_tv_qa_preview'),
        )
        config = response.context['rating_tv_config']
        self.assertEqual(config['qaReplayKind'], 'formula')
        self.assertEqual(
            config['qaReplaySchema'],
            RATING_TV_FORMULA_REPLAY_SCHEMA,
        )
        self.assertEqual(
            config['qaReplayUrl'],
            reverse('driver_rating_tv_formula_qa_replay_api'),
        )
        self.assertEqual(
            config['qaFormulaEnabledShiftTypes'],
            ['day', 'night'],
        )
        self.assertEqual(config['initialShiftType'], 'night')
        self.assertTrue(config['qaReplayEnabled'])
        self.assertIsNone(
            response.context['rating_tv_preview_payload'],
        )
        self.assertNotContains(response, 'rating-tv-preview-payload')
        for label in (
            'Синтетические данные',
            'формула рассчитана',
            'результат неофициальный',
            'не для премирования',
        ):
            self.assertContains(response, label)
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(
            response['X-Content-Type-Options'],
            'nosniff',
        )

        self.assertEqual(visual_response.status_code, 200)
        self.assertEqual(
            visual_response.context['rating_tv_config']['qaReplayKind'],
            'visual',
        )
        self.assertEqual(
            visual_response.context['rating_tv_config']['qaReplayUrl'],
            reverse('driver_rating_tv_qa_replay_api'),
        )

    def test_formula_preview_requires_common_and_one_per_shift_gate(self):
        self._login_as(self.dispatcher_access)
        preview_url = reverse('driver_rating_tv_formula_qa_preview')

        for debug, common, day, night, expected in (
            (False, True, True, True, 404),
            (True, False, True, True, 404),
            (True, True, False, False, 404),
            (True, True, True, False, 200),
            (True, True, False, True, 200),
        ):
            with self.subTest(
                debug=debug,
                common=common,
                day=day,
                night=night,
            ):
                settings_override = self._settings(
                    day=day,
                    night=night,
                )
                settings_override.update({
                    'DEBUG': debug,
                    'RATING_TV_QA_PREVIEW_ENABLED': common,
                })
                with override_settings(**settings_override):
                    response = self.client.get(preview_url)
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    expected_initial = 'night' if night else 'day'
                    self.assertEqual(
                        response.context[
                            'rating_tv_config'
                        ]['initialShiftType'],
                        expected_initial,
                    )

    def test_formula_api_serves_only_fixed_verified_day_and_night_files(self):
        self._login_as(self.dispatcher_access)
        api_url = reverse('driver_rating_tv_formula_qa_replay_api')

        with override_settings(**self._settings()):
            for shift_type, expected_sha256 in (
                ('day', self.day_sha256),
                ('night', self.night_sha256),
            ):
                with self.subTest(shift_type=shift_type):
                    with CaptureQueriesContext(connection) as queries:
                        response = self.client.get(
                            api_url,
                            {'shift_type': shift_type},
                        )

                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertEqual(
                        payload['scope']['shift_type'],
                        shift_type,
                    )
                    self.assertTrue(payload['synthetic'])
                    self.assertTrue(payload['formula_evaluated'])
                    self.assertFalse(payload['official'])
                    self.assertFalse(
                        payload['official_rating_eligible'],
                    )
                    self.assertEqual(
                        response['X-Rating-Replay-SHA256'],
                        expected_sha256,
                    )
                    self.assertEqual(
                        response['X-Rating-Replay-Kind'],
                        'formula',
                    )
                    self.assert_private_json(response)
                    write_sql = re.compile(
                        (
                            r'^\s*(INSERT|UPDATE|DELETE|REPLACE|'
                            r'CREATE|ALTER|DROP)\b'
                        ),
                        flags=re.IGNORECASE,
                    )
                    self.assertEqual(
                        [
                            query['sql']
                            for query in queries.captured_queries
                            if write_sql.match(query['sql'])
                        ],
                        [],
                    )

    def test_formula_api_requires_debug_common_and_selected_shift_gate(self):
        self._login_as(self.dispatcher_access)
        api_url = reverse('driver_rating_tv_formula_qa_replay_api')

        for debug, common, night_enabled, expected in (
            (False, True, True, 404),
            (True, False, True, 404),
            (True, True, False, 404),
            (True, True, True, 200),
        ):
            with self.subTest(
                debug=debug,
                common=common,
                night_enabled=night_enabled,
            ):
                settings_override = self._settings(
                    day=False,
                    night=night_enabled,
                )
                settings_override.update({
                    'DEBUG': debug,
                    'RATING_TV_QA_PREVIEW_ENABLED': common,
                })
                with override_settings(**settings_override):
                    response = self.client.get(
                        api_url,
                        {'shift_type': 'night'},
                    )
                self.assertEqual(response.status_code, expected)
                self.assert_private_json(response)

    def test_formula_api_requires_exactly_one_shift_selector(self):
        self._login_as(self.dispatcher_access)
        api_url = reverse('driver_rating_tv_formula_qa_replay_api')
        invalid_urls = (
            api_url,
            f'{api_url}?shift_type=',
            f'{api_url}?shift_type=evening',
            f'{api_url}?shift_type=day&shift_type=night',
            f'{api_url}?shift_type=day&path=C%3A%5Cother.json',
            f'{api_url}?shift_type=day&file=other.json',
            f'{api_url}?shift_type=day&sha256={"0" * 64}',
        )

        with override_settings(**self._settings()):
            for invalid_url in invalid_urls:
                with self.subTest(url=invalid_url):
                    response = self.client.get(invalid_url)
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn('snapshots', response.json())
                    self.assert_private_json(response)

    def test_formula_api_applies_selected_shift_gate_and_scope_match(self):
        self._login_as(self.dispatcher_access)
        api_url = reverse('driver_rating_tv_formula_qa_replay_api')

        with override_settings(**self._settings(day=True, night=False)):
            day_response = self.client.get(
                api_url,
                {'shift_type': 'day'},
            )
            night_response = self.client.get(
                api_url,
                {'shift_type': 'night'},
            )
        self.assertEqual(day_response.status_code, 200)
        self.assertEqual(night_response.status_code, 404)
        self.assert_private_json(night_response)

        mismatch_settings = self._settings()
        mismatch_settings.update({
            'RATING_TV_QA_FORMULA_REPLAY_DAY_ARTIFACT': self.night_path,
            'RATING_TV_QA_FORMULA_REPLAY_DAY_SHA256': self.night_sha256,
        })
        with override_settings(**mismatch_settings):
            mismatch = self.client.get(
                api_url,
                {'shift_type': 'day'},
            )
        self.assertEqual(mismatch.status_code, 409)
        self.assertNotIn('snapshots', mismatch.json())
        self.assert_private_json(mismatch)

    def test_formula_api_keeps_strict_current_access_boundary(self):
        api_url = reverse('driver_rating_tv_formula_qa_replay_api')
        query = {'shift_type': 'night'}

        with override_settings(**self._settings()):
            unauthenticated = self.client.get(api_url, query)
        self.assertEqual(unauthenticated.status_code, 401)
        self.assert_private_json(unauthenticated)

        driver_access = self._create_access(
            'driver',
            'Водитель без доступа к формульному TV-рейтингу',
        )
        driver_client = Client()
        self._login_as(driver_access, client=driver_client)
        with override_settings(**self._settings()):
            wrong_role = driver_client.get(api_url, query)
        self.assertEqual(wrong_role.status_code, 403)
        self.assert_private_json(wrong_role)

        stale_client = Client()
        login_at = self._login_as(
            self.dispatcher_access,
            client=stale_client,
        )
        EmployeeAccess.objects.filter(
            pk=self.dispatcher_access.pk,
        ).update(last_login_at=login_at + timedelta(seconds=1))
        with override_settings(**self._settings()):
            stale_generation = stale_client.get(api_url, query)
        self.assertEqual(stale_generation.status_code, 401)
        self.assert_private_json(stale_generation)

        not_activated_access = self._create_access(
            'dispatcher',
            'Неактивированный диспетчер формульного TV-рейтинга',
        )
        not_activated_access.status = (
            EmployeeAccess.Status.NOT_ACTIVATED
        )
        not_activated_access.save(update_fields=['status'])
        not_activated_client = Client()
        self._login_as(
            not_activated_access,
            client=not_activated_client,
        )
        with override_settings(**self._settings()):
            not_activated = not_activated_client.get(api_url, query)
        self.assertEqual(not_activated.status_code, 401)
        self.assert_private_json(not_activated)

        inactive_access = self._create_access(
            'dispatcher',
            'Неактивный доступ диспетчера формульного TV-рейтинга',
        )
        inactive_access.is_active = False
        inactive_access.save(update_fields=['is_active'])
        inactive_access_client = Client()
        self._login_as(
            inactive_access,
            client=inactive_access_client,
        )
        with override_settings(**self._settings()):
            inactive = inactive_access_client.get(api_url, query)
        self.assertEqual(inactive.status_code, 401)
        self.assert_private_json(inactive)

        dismissed_access = self._create_access(
            'dispatcher',
            'Уволенный диспетчер формульного TV-рейтинга',
        )
        dismissed_access.employee.status = Employee.Status.DISMISSED
        dismissed_access.employee.is_active = False
        dismissed_access.employee.save(
            update_fields=['status', 'is_active'],
        )
        dismissed_client = Client()
        self._login_as(dismissed_access, client=dismissed_client)
        with override_settings(**self._settings()):
            dismissed = dismissed_client.get(api_url, query)
        self.assertEqual(dismissed.status_code, 401)
        self.assert_private_json(dismissed)

        inactive_role_access = self._create_access(
            'manager',
            'Руководитель с неактивной ролью формульного TV-рейтинга',
        )
        inactive_role_access.role.is_active = False
        inactive_role_access.role.save(update_fields=['is_active'])
        inactive_role_client = Client()
        self._login_as(
            inactive_role_access,
            client=inactive_role_client,
        )
        with override_settings(**self._settings()):
            inactive_role = inactive_role_client.get(api_url, query)
        self.assertEqual(inactive_role.status_code, 401)
        self.assert_private_json(inactive_role)
