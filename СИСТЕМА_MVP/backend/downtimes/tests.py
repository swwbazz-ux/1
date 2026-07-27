from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import OperationalStateEvent
from references.equipment_states import upsert_default_equipment_states
from references.models import Equipment, EquipmentState, EquipmentType
from users.models import Employee, EmployeeAccess, Role

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
