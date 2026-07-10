from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from references.models import Equipment, EquipmentType
from users.models import Employee, EmployeeAccess, Role

from .checks import media_storage_writable_check
from .models import OperationalStateEvent, OperationalStateVersion


class MediaStorageWritableCheckTests(SimpleTestCase):
    def test_existing_media_and_employee_photo_directories_pass_check(self):
        with TemporaryDirectory() as media_root:
            Path(media_root, 'employee_photos').mkdir()

            with override_settings(MEDIA_ROOT=media_root):
                errors = media_storage_writable_check(None)

        self.assertEqual(errors, [])

    def test_missing_media_root_returns_clear_error(self):
        with TemporaryDirectory() as parent_dir:
            missing_media_root = Path(parent_dir, 'missing-media')

            with override_settings(MEDIA_ROOT=missing_media_root):
                errors = media_storage_writable_check(None)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, 'core.E001')


class OperationalStateVersionViewTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(
            full_name='Тестовый диспетчер',
            phone='79000000999',
            is_active=True,
            status=Employee.Status.ACTIVE,
        )
        self.role = Role.objects.create(code='dispatcher', name='Диспетчер', is_active=True)
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.role,
            access_code='999999',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.url = reverse('operational_state_version')

    def authorize(self):
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()

    def test_realtime_state_requires_employee_session(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['authenticated'], False)

    def test_realtime_state_returns_current_version_for_authorized_user(self):
        OperationalStateVersion.objects.update_or_create(
            key='production',
            defaults={'version': 7, 'reason': 'test'},
        )
        self.authorize()

        response = self.client.get(self.url, {'after': 7})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['authenticated'], True)
        self.assertEqual(payload['key'], 'production')
        self.assertEqual(payload['version'], 7)
        self.assertEqual(payload['events'], [])

    def test_equipment_save_bumps_operational_state_version(self):
        equipment_type = EquipmentType.objects.create(name='Экскаватор')

        Equipment.objects.create(equipment_type=equipment_type, garage_number='Э-99', is_active=True)

        state = OperationalStateVersion.objects.get(key='production')
        self.assertGreaterEqual(state.version, 1)
        self.assertEqual(state.reason, 'Equipment:save')
        event = OperationalStateEvent.objects.filter(event_type='equipment_changed').latest('version')
        self.assertEqual(event.object_type, 'Equipment')
        self.assertEqual(event.payload['action'], 'save')

    def test_employee_save_bumps_operational_state_for_open_workplaces(self):
        after = OperationalStateVersion.objects.get(key='production').version

        self.employee.full_name = 'Updated dispatcher'
        self.employee.save(update_fields=['full_name'])
        self.authorize()

        response = self.client.get(self.url, {'after': after})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload['events']), 1)
        self.assertEqual(payload['events'][0]['type'], 'employee_changed')
        self.assertEqual(payload['events'][0]['object_type'], 'Employee')
        self.assertEqual(payload['events'][0]['object_id'], str(self.employee.id))
        self.assertEqual(payload['events'][0]['payload']['employee_id'], self.employee.id)
        self.assertEqual(payload['events'][0]['payload']['action'], 'save')

    def test_realtime_state_returns_events_after_requested_version(self):
        equipment_type = EquipmentType.objects.create(name='Экскаватор')
        after = OperationalStateVersion.objects.get(key='production').version
        equipment = Equipment.objects.create(equipment_type=equipment_type, garage_number='Э-100', is_active=True)
        self.authorize()

        response = self.client.get(self.url, {'after': after})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['version'], OperationalStateVersion.objects.get(key='production').version)
        self.assertEqual(len(payload['events']), 1)
        self.assertEqual(payload['events'][0]['type'], 'equipment_changed')
        self.assertEqual(payload['events'][0]['object_type'], 'Equipment')
        self.assertEqual(payload['events'][0]['object_id'], str(equipment.id))

    def test_realtime_state_can_skip_events_for_light_polling(self):
        equipment_type = EquipmentType.objects.create(name='Экскаватор')
        after = OperationalStateVersion.objects.get(key='production').version
        Equipment.objects.create(equipment_type=equipment_type, garage_number='Э-102', is_active=True)
        self.authorize()

        response = self.client.get(self.url, {'after': after, 'include_events': '0'})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['version'], OperationalStateVersion.objects.get(key='production').version)
        self.assertEqual(payload['events'], [])
        self.assertEqual(payload['events_truncated'], False)

    def test_disabling_active_excavator_releases_complex_and_frees_trucks(self):
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='Э-101',
            is_active=True,
        )
        truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='С-101',
            is_active=True,
        )
        placement = ExcavatorPlacement.objects.create(
            excavator=excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
        )
        assignment = HaulAssignment.objects.create(
            excavator=excavator,
            truck=truck,
            status=AssignmentStatus.ACCEPTED,
        )

        excavator.is_active = False
        excavator.save(update_fields=['is_active'])

        placement.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(placement.zone, ExcavatorPlacement.Zone.INACTIVE)
        self.assertIsNotNone(assignment.ended_at)
        self.assertTrue(
            OperationalStateEvent.objects.filter(
                event_type='assignment_changed',
                object_type='Equipment',
                object_id=str(excavator.id),
                payload__action='release_disabled_excavator',
            ).exists()
        )
