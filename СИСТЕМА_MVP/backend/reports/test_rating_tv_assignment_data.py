from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from core.production_time import production_work_date
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment
from references.models import Equipment, EquipmentModel, EquipmentType
from shifts.models import ShiftType
from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from users.models import Employee, EmployeeAccess, Role, WorkSchedule

from .driver_rating_materialization import (
    refresh_driver_rating_assignment_group,
)
from .models import RatingPeriod


@override_settings(
    RATING_TV_SCREEN_ENABLED=True,
    PORTAL_WORKING_DRIVER_RATING_ENABLED=False,
    PORTAL_SITE_CODE='rating-tv-assignment-tests',
    PORTAL_EMPLOYEE_SCOPE_PROVIDER='',
    DRIVER_RATING_SNAPSHOT_REFRESH_SECONDS=300,
    DRIVER_RATING_SNAPSHOT_SOFT_STALE_SECONDS=600,
    DRIVER_RATING_SNAPSHOT_HARD_EXPIRE_SECONDS=1800,
)
class DriverRatingTvAssignmentDataTests(TestCase):
    def setUp(self):
        super().setUp()
        work_date = production_work_date()
        self.rating_period = RatingPeriod.objects.create(
            name='Период TV-рейтинга по назначениям',
            starts_on=work_date - timedelta(days=1),
            ends_before=work_date + timedelta(days=2),
            comment='Короткое окно только для адресной API-регрессии.',
        )
        self.schedule_a = WorkSchedule.objects.create(
            code='rating-tv-schedule-a',
            name='График TV A',
            brigade_count=4,
        )
        self.schedule_b = WorkSchedule.objects.create(
            code='rating-tv-schedule-b',
            name='График TV B',
            brigade_count=4,
        )
        self.driver_role = Role.objects.create(
            code='driver',
            name='Водитель TV-рейтинга',
        )
        dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Диспетчер TV-рейтинга по назначениям',
        )
        dispatcher = Employee.objects.create(
            full_name='Диспетчер TV-рейтинга по назначениям',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=dispatcher,
            role=dispatcher_role,
            access_code='910001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.truck_type = EquipmentType.objects.create(
            name='Самосвал TV-рейтинга по назначениям',
        )
        self.truck_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='Модель TV-рейтинга по назначениям',
            payload_tons=Decimal('100'),
            body_volume_m3=Decimal('50'),
        )
        self._equipment_ordinal = 0
        self._login()

    def _login(self):
        login_at = timezone.now()
        self.dispatcher_access.last_login_at = login_at
        self.dispatcher_access.save(update_fields=['last_login_at'])
        session = self.client.session
        session['employee_access_id'] = self.dispatcher_access.id
        session[ACTIVE_ROLE_SESSION_KEY] = self.dispatcher_access.id
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = (
            self.dispatcher_access.role.code
        )
        session.save()

    def _equipment(self):
        self._equipment_ordinal += 1
        return Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number=f'TV-GROUP-{self._equipment_ordinal:03d}',
        )

    def _driver(
        self,
        name,
        *,
        schedule=None,
        brigade=1,
        shift_type=ShiftType.DAY,
        assigned=True,
    ):
        employee = Employee.objects.create(
            full_name=name,
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
            work_schedule=schedule or self.schedule_a,
            brigade_number=brigade,
            watch_composition=None,
        )
        if assigned:
            EquipmentAssignment.objects.create(
                employee=employee,
                role=self.driver_role,
                equipment=self._equipment(),
                shift_type=shift_type,
                status=AssignmentStatus.ACCEPTED,
                accepted_at=timezone.now(),
            )
        return employee

    def _publish(
        self,
        *,
        schedule=None,
        brigade=1,
        shift_type=ShiftType.DAY,
    ):
        return refresh_driver_rating_assignment_group(
            self.rating_period,
            schedule or self.schedule_a,
            brigade_number=brigade,
            shift_type=shift_type,
        )

    def _data(
        self,
        *,
        schedule=None,
        brigade=1,
        shift_type=ShiftType.DAY,
    ):
        schedule = schedule or self.schedule_a
        return self.client.get(
            reverse('driver_rating_tv_data_api'),
            {
                'rating_period': self.rating_period.id,
                'work_schedule': schedule.id,
                'brigade_number': brigade,
                'shift_type': shift_type,
            },
        )

    def test_two_assigned_drivers_are_returned_without_unassigned_driver(self):
        first = self._driver('Назначенный водитель один')
        second = self._driver('Назначенный водитель два')
        unassigned = self._driver(
            'Неназначенный водитель',
            assigned=False,
        )
        self._publish()

        response = self._data()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload['entries']), 2)
        self.assertEqual(
            {entry['employee_id'] for entry in payload['entries']},
            {first.id, second.id},
        )
        self.assertNotIn(
            unassigned.id,
            {entry['employee_id'] for entry in payload['entries']},
        )
        for entry in payload['entries']:
            self.assertEqual(entry['row_status'], 'not_observed')
            self.assertEqual(entry['status_label'], 'Нет результата')
            self.assertIsNone(entry['place'])
            self.assertIsNone(entry['score'])
        self.assertEqual(
            payload['rating_group']['work_schedule']['id'],
            self.schedule_a.id,
        )
        self.assertEqual(payload['rating_group']['brigade_number'], 1)
        self.assertNotIn('watch_composition', payload)
        self.assertNotIn('available_watch_compositions', payload)

    def test_group_of_forty_one_has_exactly_forty_one_rows(self):
        drivers = {
            self._driver(
                f'Ночной водитель {index:02d}',
                schedule=self.schedule_b,
                brigade=3,
                shift_type=ShiftType.NIGHT,
            ).id
            for index in range(1, 42)
        }
        self._publish(
            schedule=self.schedule_b,
            brigade=3,
            shift_type=ShiftType.NIGHT,
        )

        response = self._data(
            schedule=self.schedule_b,
            brigade=3,
            shift_type=ShiftType.NIGHT,
        )

        self.assertEqual(response.status_code, 200)
        entries = response.json()['entries']
        self.assertEqual(len(entries), 41)
        self.assertEqual({entry['employee_id'] for entry in entries}, drivers)
        self.assertEqual(
            sorted(entry['display_order'] for entry in entries),
            list(range(1, 42)),
        )

    def test_day_and_night_use_independent_assignment_groups(self):
        day_ids = {
            self._driver(f'Дневной водитель {index}').id
            for index in range(1, 3)
        }
        night_ids = {
            self._driver(
                f'Ночной водитель {index}',
                schedule=self.schedule_b,
                brigade=2,
                shift_type=ShiftType.NIGHT,
            ).id
            for index in range(1, 4)
        }
        self._publish(shift_type=ShiftType.DAY)
        self._publish(
            schedule=self.schedule_b,
            brigade=2,
            shift_type=ShiftType.NIGHT,
        )

        day_response = self._data(shift_type=ShiftType.DAY)
        stale_day_group_response = self._data(shift_type=ShiftType.NIGHT)
        night_response = self._data(
            schedule=self.schedule_b,
            brigade=2,
            shift_type=ShiftType.NIGHT,
        )

        self.assertEqual(day_response.status_code, 200)
        self.assertEqual(stale_day_group_response.status_code, 400)
        self.assertEqual(night_response.status_code, 200)
        self.assertEqual(
            stale_day_group_response.json()['available_rating_groups'][0]
            ['work_schedule']['id'],
            self.schedule_b.id,
        )
        self.assertEqual(
            {entry['employee_id'] for entry in day_response.json()['entries']},
            day_ids,
        )
        self.assertEqual(
            {
                entry['employee_id']
                for entry in night_response.json()['entries']
            },
            night_ids,
        )
        self.assertEqual(day_response.json()['shift_type'], ShiftType.DAY)
        self.assertEqual(night_response.json()['shift_type'], ShiftType.NIGHT)

    def test_group_outside_current_site_employee_scope_is_not_exposed(self):
        allowed = self._driver('Водитель в доступной области')
        self._driver('Водитель вне доступной области')
        self._publish()

        with patch(
            'reports.views._rating_tv_assignment_site_employee_ids',
            return_value=(allowed.id,),
        ):
            response = self.client.get(
                reverse('driver_rating_tv_data_api'),
                {
                    'rating_period': self.rating_period.id,
                    'shift_type': ShiftType.DAY,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['available'])
        self.assertEqual(payload['available_rating_groups'], [])
        self.assertEqual(payload['entries'], [])

    def test_regular_tv_path_does_not_call_legacy_watch_resolver(self):
        self._driver('Водитель без вахтовых сущностей')
        self._publish()

        with (
            patch(
                'reports.views.get_rating_site_scope',
                side_effect=AssertionError('legacy site scope called'),
            ),
            patch(
                'reports.views.get_materialized_driver_rating_period',
                side_effect=AssertionError('legacy getter called'),
            ),
            patch(
                'reports.views.materialized_driver_rating_rows',
                side_effect=AssertionError('legacy rows called'),
            ),
            patch(
                'reports.views.discover_driver_rating_current_scope',
                side_effect=AssertionError('legacy scope called'),
            ),
            CaptureQueriesContext(connection) as queries,
        ):
            page_response = self.client.get(reverse('driver_rating_tv'))
            data_response = self._data()

        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(data_response.status_code, 200)
        payload = data_response.json()
        self.assertNotIn('watch_composition', payload)
        self.assertNotIn('available_watch_compositions', payload)
        sql = ' '.join(query['sql'].lower() for query in queries)
        self.assertNotIn('users_watchcomposition', sql)
        self.assertNotIn('shifts_watchperiod', sql)
