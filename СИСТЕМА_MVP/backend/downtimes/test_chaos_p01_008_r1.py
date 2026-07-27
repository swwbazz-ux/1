from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import OperationalStateEvent
from references.models import Equipment, EquipmentType
from users.models import Employee, EmployeeAccess, Role

from .models import DowntimeEvent, DowntimeReason


class MechanicTruckDowntimeOwnershipTests(TestCase):
    def setUp(self):
        self.mechanic_role = Role.objects.create(code='mechanic', name='Механик')
        self.mechanic = Employee.objects.create(
            full_name='Механик QA-CHAOS-P1-008-R1',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.mechanic_access = EmployeeAccess.objects.create(
            employee=self.mechanic,
            role=self.mechanic_role,
            access_code='CHAOS-P1-008-R1',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            last_login_at=timezone.now(),
        )
        self.driver = Employee.objects.create(
            full_name='Водитель — владелец простоя',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='TRUCK-P01-008-R1',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='EXC-P01-008-R1',
        )
        self.truck_reason = DowntimeReason.objects.create(
            name='Простой самосвала P01-008-R1',
            equipment_type=truck_type,
            show_for_truck_driver=True,
            show_for_mechanic=True,
        )
        self.excavator_reason = DowntimeReason.objects.create(
            name='Ремонт экскаватора P01-008-R1',
            equipment_type=excavator_type,
            show_for_excavator_operator=True,
            show_for_mechanic=True,
        )
        session = self.client.session
        session['employee_access_id'] = self.mechanic_access.pk
        session['active_role_access_id'] = self.mechanic_access.pk
        session['active_role_login_at'] = self.mechanic_access.last_login_at.isoformat()
        session['active_role_code'] = self.mechanic_role.code
        session.save()

    @staticmethod
    def close_audits(event):
        return OperationalStateEvent.objects.filter(
            object_type='DowntimeEvent',
            object_id=str(event.pk),
            payload__action='downtime_closed',
        )

    def test_direct_mechanic_post_cannot_close_truck_downtime(self):
        event = DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=self.truck_reason,
            started_at=timezone.now() - timedelta(minutes=20),
        )

        response = self.client.post(
            reverse('mechanic_close_downtime', args=[event.pk]),
            HTTP_HOST='localhost',
        )

        event.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(event.ended_at)
        self.assertEqual(self.close_audits(event).count(), 0)

    def test_mechanic_dashboard_keeps_truck_downtime_readonly(self):
        event = DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=self.truck_reason,
            started_at=timezone.now() - timedelta(minutes=20),
        )

        response = self.client.get(reverse('mechanic_dashboard'), HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.truck))
        self.assertContains(response, 'Закрывает только водитель')
        self.assertNotContains(
            response,
            reverse('mechanic_close_downtime', args=[event.pk]),
        )

    def test_mechanic_still_closes_excavator_once_and_retry_preserves_boundary(self):
        event = DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.mechanic,
            reason=self.excavator_reason,
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

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertIsNotNone(first_ended_at)
        self.assertEqual(event.ended_at, first_ended_at)
        self.assertEqual(self.close_audits(event).count(), 1)
