from unittest.mock import patch

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift
from users.models import Employee

from .services import schedule_haul_assignment, schedule_haul_release


class DriverAssignmentWakeupTests(TestCase):
    def setUp(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(equipment_type=truck_type, garage_number='WAKE-1')
        self.excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='WAKE-99')
        self.driver = Employee.objects.create(full_name='Тест пробуждения', phone='79000000991')
        self.shift = EmployeeShift.objects.create(
            employee=self.driver, equipment=self.truck, workplace_code='driver', shift_type='day', opened_at=timezone.now(),
        )

    @patch('users.native_push.notify_employee_devices', return_value=1)
    def test_pending_assignment_wakes_only_active_driver_after_commit(self, notify):
        with self.captureOnCommitCallbacks(execute=True):
            assignment, created = schedule_haul_assignment(truck=self.truck, excavator=self.excavator)
            self.assertTrue(created)
            notify.assert_not_called()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args, (self.driver,))
        self.assertEqual(notify.call_args.kwargs['kind'], 'driver_assignment_changed')
        self.assertEqual(notify.call_args.kwargs['app_ids'], {
            'ru.copperresources.driver', 'ru.copperresources.driver.qa',
        })
        self.assertGreater(int(notify.call_args.kwargs['state_version']), 0)
        with self.captureOnCommitCallbacks(execute=True):
            repeated, created = schedule_haul_assignment(truck=self.truck, excavator=self.excavator)
        self.assertFalse(created)
        self.assertEqual(repeated.pk, assignment.pk)
        self.assertEqual(notify.call_count, 1)

    @patch('users.native_push.notify_employee_devices', return_value=1)
    def test_rollback_never_wakes_driver(self, notify):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    schedule_haul_assignment(truck=self.truck, excavator=self.excavator)
                    raise RuntimeError('rollback')
        notify.assert_not_called()

    @patch('users.native_push.notify_employee_devices', return_value=1)
    def test_closed_driver_shift_never_wakes_driver(self, notify):
        self.shift.closed_at = timezone.now()
        self.shift.save(update_fields=['closed_at'])
        with self.captureOnCommitCallbacks(execute=True):
            schedule_haul_assignment(truck=self.truck, excavator=self.excavator)
        notify.assert_not_called()

    @patch('users.native_push.notify_employee_devices', return_value=1)
    def test_release_wakes_driver(self, notify):
        schedule_haul_assignment(truck=self.truck, excavator=self.excavator)
        with self.captureOnCommitCallbacks(execute=True):
            schedule_haul_release(truck=self.truck)
        notify.assert_called_once()

    @patch('users.native_push.notify_employee_devices', side_effect=RuntimeError('push unavailable'))
    def test_push_failure_preserves_committed_assignment(self, notify):
        with self.captureOnCommitCallbacks(execute=True):
            assignment, created = schedule_haul_assignment(truck=self.truck, excavator=self.excavator)
        self.assertTrue(created)
        assignment.refresh_from_db()
        self.assertEqual(assignment.truck_id, self.truck.pk)
