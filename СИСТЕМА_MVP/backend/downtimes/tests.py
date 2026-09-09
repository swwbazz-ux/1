from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import OperationalStateEvent
from references.equipment_states import upsert_default_equipment_states
from references.models import Equipment, EquipmentState, EquipmentType
from users.models import Employee, EmployeeAccess, Role

from .driver_workflow import (
    DRIVER_DOWNTIME_FLOW_WAITING_LOADING,
    DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD,
    TRUCK_UNLOADING_WAIT_REASON_NAMES,
    close_truck_unloading_wait_downtimes,
    close_truck_waiting_loading_downtimes,
    driver_downtime_flow,
    driver_downtime_opens_work,
    driver_downtime_requires_empty_truck,
    driver_downtime_requires_loaded_trip,
)
from .models import DowntimeEvent, DowntimeReason


class DowntimeReasonStateSemanticsTests(TestCase):
    def setUp(self):
        upsert_default_equipment_states()

    def test_non_emergency_field_reasons_fallback_to_yellow_waiting(self):
        for reason_name in ('Тестовая зачистка забоя', 'Тестовый перегон экскаватора', 'Тестовое ожидание разгрузки ККД'):
            reason = DowntimeReason.objects.create(name=reason_name)

            self.assertEqual(reason.effective_equipment_state_code, 'waiting')
            self.assertEqual(reason.effective_color_group, 'yellow')

    def test_critical_reason_fallbacks_to_red_breakdown(self):
        reason = DowntimeReason.objects.create(name='Тестовая аварийная поломка')

        self.assertEqual(reason.effective_equipment_state_code, 'breakdown')
        self.assertEqual(reason.effective_color_group, 'red')

    def test_technical_reason_fallbacks_to_orange_state(self):
        repair = DowntimeReason.objects.create(name='Тестовый текущий ремонт')
        maintenance = DowntimeReason.objects.create(name='Тестовое ТО и обслуживание')

        self.assertEqual(repair.effective_equipment_state_code, 'repair')
        self.assertEqual(repair.effective_color_group, 'orange')
        self.assertEqual(maintenance.effective_equipment_state_code, 'maintenance')
        self.assertEqual(maintenance.effective_color_group, 'orange')

    def test_explicit_equipment_state_overrides_fallback(self):
        state = EquipmentState.objects.get(code='breakdown')
        reason = DowntimeReason.objects.create(name='Тестовое ожидание самосвалов', equipment_state=state)

        self.assertEqual(reason.effective_equipment_state_code, 'breakdown')
        self.assertEqual(reason.effective_color_group, 'red')


class DriverDowntimeWorkflowTests(TestCase):
    def setUp(self):
        truck_type = EquipmentType.objects.create(
            name='Самосвал тестов сценария простоя водителя',
        )
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='DRIVER-DOWNTIME-FLOW',
        )

    def test_classifies_loading_unloading_and_ordinary_driver_reasons(self):
        waiting_loading = DowntimeReason.objects.get(name='Ожидание погрузки')
        self.assertEqual(
            driver_downtime_flow(waiting_loading),
            DRIVER_DOWNTIME_FLOW_WAITING_LOADING,
        )
        self.assertTrue(driver_downtime_requires_empty_truck(waiting_loading))
        self.assertFalse(driver_downtime_requires_loaded_trip(waiting_loading))
        self.assertTrue(driver_downtime_opens_work(waiting_loading))

        for reason_name in TRUCK_UNLOADING_WAIT_REASON_NAMES:
            with self.subTest(reason_name=reason_name):
                reason = DowntimeReason.objects.get(name=reason_name)
                self.assertEqual(
                    driver_downtime_flow(reason),
                    DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD,
                )
                self.assertFalse(driver_downtime_requires_empty_truck(reason))
                self.assertTrue(driver_downtime_requires_loaded_trip(reason))
                self.assertTrue(driver_downtime_opens_work(reason))

        ordinary_reason = DowntimeReason.objects.get(name='Заправка')
        self.assertEqual(driver_downtime_flow(ordinary_reason), '')
        self.assertFalse(driver_downtime_requires_empty_truck(ordinary_reason))
        self.assertFalse(driver_downtime_requires_loaded_trip(ordinary_reason))
        self.assertFalse(driver_downtime_opens_work(ordinary_reason))

    def test_close_helpers_only_close_their_driver_workflow_group(self):
        waiting_loading_reason = DowntimeReason.objects.get(
            name='Ожидание погрузки',
        )
        waiting_loading_event = DowntimeEvent.objects.create(
            equipment=self.truck,
            reason=waiting_loading_reason,
            started_at=timezone.now() - timedelta(minutes=12),
        )
        unloading_events = []
        for index, reason_name in enumerate(TRUCK_UNLOADING_WAIT_REASON_NAMES):
            reason = DowntimeReason.objects.get(name=reason_name)
            unloading_events.append(
                DowntimeEvent.objects.create(
                    equipment=self.truck,
                    reason=reason,
                    started_at=timezone.now() - timedelta(minutes=9 - index),
                )
            )
        ordinary_reason = DowntimeReason.objects.get(name='Ремонт')
        ordinary_event = DowntimeEvent.objects.create(
            equipment=self.truck,
            reason=ordinary_reason,
            started_at=timezone.now() - timedelta(minutes=5),
        )
        unloading_ended_at = timezone.now()

        closed_unloading_count = close_truck_unloading_wait_downtimes(
            self.truck,
            ended_at=unloading_ended_at,
        )

        self.assertEqual(
            closed_unloading_count,
            len(TRUCK_UNLOADING_WAIT_REASON_NAMES),
        )
        for event in unloading_events:
            event.refresh_from_db()
            self.assertEqual(event.ended_at, unloading_ended_at)
        waiting_loading_event.refresh_from_db()
        ordinary_event.refresh_from_db()
        self.assertIsNone(waiting_loading_event.ended_at)
        self.assertIsNone(ordinary_event.ended_at)

        loading_ended_at = unloading_ended_at + timedelta(seconds=1)
        closed_loading_count = close_truck_waiting_loading_downtimes(
            self.truck,
            ended_at=loading_ended_at,
        )

        self.assertEqual(closed_loading_count, 1)
        waiting_loading_event.refresh_from_db()
        ordinary_event.refresh_from_db()
        self.assertEqual(waiting_loading_event.ended_at, loading_ended_at)
        self.assertIsNone(ordinary_event.ended_at)


class MechanicDowntimeCloseRegressionTests(TestCase):
    def setUp(self):
        mechanic_role = Role.objects.create(code='mechanic', name='Механик')
        self.mechanic = Employee.objects.create(
            full_name='Механик QA-CHAOS-P1-005-R1',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        mechanic_access = EmployeeAccess.objects.create(
            employee=self.mechanic,
            role=mechanic_role,
            access_code='CHAOS-P1-005-R1',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        excavator_type = EquipmentType.objects.create(name='Экскаватор QA-CHAOS-P1-005-R1')
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='CHAOS-P1-005-R1',
        )
        self.reason = DowntimeReason.objects.create(
            name='Ремонт QA-CHAOS-P1-005-R1',
            equipment_type=excavator_type,
            show_for_mechanic=True,
        )
        session = self.client.session
        session['employee_access_id'] = mechanic_access.pk
        session.save()

    def test_repeated_mechanic_close_preserves_first_end_boundary(self):
        event = DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.mechanic,
            reason=self.reason,
            started_at=timezone.now() - timedelta(minutes=20),
        )

        first_response = self.client.post(
            reverse('mechanic_close_downtime', args=[event.pk]),
            HTTP_HOST='localhost',
        )
        event.refresh_from_db()
        first_ended_at = event.ended_at

        second_response = self.client.post(
            reverse('mechanic_close_downtime', args=[event.pk]),
            HTTP_HOST='localhost',
        )
        event.refresh_from_db()
        close_audits = OperationalStateEvent.objects.filter(
            object_type='DowntimeEvent',
            object_id=str(event.pk),
            payload__action='downtime_closed',
        )

        self.assertRedirects(
            first_response,
            reverse('mechanic_dashboard'),
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            second_response,
            reverse('mechanic_dashboard'),
            fetch_redirect_response=False,
        )
        self.assertIsNotNone(first_ended_at)
        self.assertEqual(event.ended_at, first_ended_at)
        self.assertEqual(close_audits.count(), 1)
