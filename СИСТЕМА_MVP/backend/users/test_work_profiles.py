from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from assignments.services import employee_matches_work_role
from shifts.models import WatchPeriod

from .models import Employee, EmployeeAccess, PersonnelPosition, ProductionSpecialization, Role, TemporaryWorkTransfer
from .work_profiles import (
    approve_temporary_work_transfer,
    cancel_temporary_work_transfer,
    eligible_employee_ids_for_work_role,
    expire_due_temporary_work_transfers,
    request_temporary_work_transfer,
)


class TemporaryWorkTransferTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.driver_role, _ = Role.objects.update_or_create(
            code='driver',
            defaults={'name': 'Водитель самосвала', 'is_active': True},
        )
        self.excavator_role, _ = Role.objects.update_or_create(
            code='excavator_operator',
            defaults={'name': 'Машинист экскаватора', 'is_active': True},
        )
        self.haul_specialization = ProductionSpecialization.objects.get(code='haul_truck_driver')
        self.kdm_specialization = ProductionSpecialization.objects.get(code='cargo_kdm_driver')
        self.excavator_specialization = ProductionSpecialization.objects.get(code='excavator_operator')
        self.haul_specialization.access_role = self.driver_role
        self.haul_specialization.save(update_fields=['access_role'])
        self.excavator_specialization.access_role = self.excavator_role
        self.excavator_specialization.save(update_fields=['access_role'])
        self.generic_driver_position = PersonnelPosition.objects.get(
            name='Водитель автомобиля грузового',
        )
        self.haul_driver_position = PersonnelPosition.objects.get(
            name='Водитель автомобиля, занятый на транспортировании горной массы в технологическом процессе',
        )
        self.watch_period = WatchPeriod.objects.create(
            name='Текущая вахта',
            starts_on=self.today,
            ends_on=self.today,
            is_active=True,
        )
        self.requester = Employee.objects.create(
            full_name='Заместитель Начальника',
            phone='+79000000001',
            status=Employee.Status.ACTIVE,
        )
        self.kdm_driver = Employee.objects.create(
            full_name='Водитель КДМ',
            phone='+79000000002',
            status=Employee.Status.ACTIVE,
            personnel_position=self.generic_driver_position,
            base_specialization=self.kdm_specialization,
            position=self.generic_driver_position.name,
        )

    def request_and_approve(self, *, employee, target_specialization):
        transfer = request_temporary_work_transfer(
            employee=employee,
            target_specialization=target_specialization,
            watch_period=self.watch_period,
            requested_by=self.requester,
            reason='Подмена на время вахты',
        )
        return approve_temporary_work_transfer(
            transfer=transfer,
            reviewed_by=self.requester,
        )

    def test_kdm_driver_is_not_eligible_for_haul_truck_until_oup_approves_transfer(self):
        self.assertFalse(employee_matches_work_role(self.kdm_driver, self.driver_role))
        self.assertNotIn(self.kdm_driver.id, eligible_employee_ids_for_work_role('driver'))

        transfer, access = self.request_and_approve(
            employee=self.kdm_driver,
            target_specialization=self.haul_specialization,
        )

        self.assertEqual(transfer.status, TemporaryWorkTransfer.Status.APPROVED)
        self.assertEqual(access.role, self.driver_role)
        self.assertTrue(employee_matches_work_role(self.kdm_driver, self.driver_role))
        self.assertIn(self.kdm_driver.id, eligible_employee_ids_for_work_role('driver'))

    def test_transfer_expires_after_watch_and_returns_employee_to_base_specialization(self):
        transfer, access = self.request_and_approve(
            employee=self.kdm_driver,
            target_specialization=self.haul_specialization,
        )

        expired = expire_due_temporary_work_transfers(as_of=self.today + timedelta(days=1))
        transfer.refresh_from_db()
        access.refresh_from_db()

        self.assertEqual([item.id for item in expired], [transfer.id])
        self.assertEqual(transfer.status, TemporaryWorkTransfer.Status.EXPIRED)
        self.assertFalse(access.is_active)
        self.assertNotIn(
            self.kdm_driver.id,
            eligible_employee_ids_for_work_role('driver', as_of=self.today + timedelta(days=1)),
        )

    def test_production_pin_is_reused_when_temporary_transfer_changes_app(self):
        haul_driver = Employee.objects.create(
            full_name='Водитель Самосвала',
            phone='+79000000003',
            status=Employee.Status.ACTIVE,
            personnel_position=self.haul_driver_position,
            base_specialization=self.haul_specialization,
            position=self.haul_driver_position.name,
        )
        driver_access = EmployeeAccess.objects.create(
            employee=haul_driver,
            role=self.driver_role,
            access_code='778899',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=timezone.now(),
        )

        _transfer, updated_access = self.request_and_approve(
            employee=haul_driver,
            target_specialization=self.excavator_specialization,
        )
        driver_access.refresh_from_db()

        self.assertEqual(updated_access.id, driver_access.id)
        self.assertEqual(updated_access.role, self.excavator_role)
        self.assertEqual(updated_access.access_code, '778899')
        self.assertEqual(updated_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(updated_access.is_active)

    def test_admin_cancellation_returns_employee_to_base_specialization_before_watch_ends(self):
        transfer, access = self.request_and_approve(
            employee=self.kdm_driver,
            target_specialization=self.haul_specialization,
        )

        transfer, restored_access = cancel_temporary_work_transfer(
            transfer=transfer,
            cancelled_by=self.requester,
            comment='Расстановка изменена',
        )
        transfer.refresh_from_db()
        access.refresh_from_db()

        self.assertEqual(transfer.status, TemporaryWorkTransfer.Status.CANCELLED)
        self.assertEqual(restored_access, None)
        self.assertFalse(access.is_active)
        self.assertNotIn(self.kdm_driver.id, eligible_employee_ids_for_work_role('driver'))
