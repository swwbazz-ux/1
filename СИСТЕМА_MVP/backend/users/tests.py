import json
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO
from tempfile import TemporaryDirectory

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from PIL import Image

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from core.models import OperationalStateEvent
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
    TruckCapacityRule,
)
from reports.models import PilotFeedback, ReportTemplate, ReportType
from shifts.models import EmployeeShift, EquipmentShiftPlan, ShiftPlan
from trips.models import DispatcherActionLog, DispatcherActionType, Trip, TripClientAction, TripStatus

from .forms import AdminEmployeeEditForm
from .models import AdminActionLog, AdminConflict, DriverPrimaryRegistration, Employee, EmployeeAccess, Role


class AccessLoginTests(TestCase):
    def setUp(self):
        self.role = Role.objects.create(code='driver', name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°')
        self.employee = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ')
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.role,
            access_code='2000',
        )

    def create_registered_driver_shift(self, truck=None):
        truck_type = truck.equipment_type if truck else EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        truck = truck or Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')
        DriverPrimaryRegistration.objects.create(
            employee=self.employee,
            dormitory_section=section,
        )
        EmployeeShift.objects.create(
            employee=self.employee,
            shift_type='day',
            equipment=truck,
            opened_at=timezone.now(),
            opened_by=self.employee,
        )
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()
        return truck

    def test_registered_driver_opens_shift_screen(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')
        DriverPrimaryRegistration.objects.create(
            employee=self.employee,
            dormitory_section=section,
        )

        response = self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р Р°Р±РѕС‚Р° РІРѕРґРёС‚РµР»СЏ')
        self.assertContains(response, 'РћС‚РєСЂС‹С‚СЊ СЃРјРµРЅСѓ')
        self.assertEqual(self.client.session.get('employee_access_id'), self.access.id)

    def test_driver_screen_includes_own_pwa_install_metadata(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')
        DriverPrimaryRegistration.objects.create(
            employee=self.employee,
            dormitory_section=section,
        )
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()

        response = self.client.get(reverse('driver_work'), HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('driver_manifest'))
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, '/driver-sw.js')
        self.assertContains(response, 'driver-mobile-shell-v27')
        self.assertContains(response, 'data-driver-pwa-update-modal')
        self.assertContains(response, 'data-driver-pwa-update-badge')
        self.assertContains(response, 'mode: "custom", path: "^/driver/(?:shift/?)?$"')
        self.assertContains(response, 'window.applyOperationalStateRefresh')
        self.assertContains(response, 'window.bindDriverMobileShell')

    def test_driver_manifest_is_installable_pwa_manifest(self):
        response = self.client.get(reverse('driver_manifest'), HTTP_HOST='localhost')
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertIn('application/manifest+json', response['Content-Type'])
        self.assertEqual(payload['name'], 'Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°')
        self.assertEqual(payload['start_url'], '/driver/')
        self.assertEqual(payload['scope'], '/driver/')
        self.assertEqual(payload['display'], 'standalone')
        self.assertEqual(payload['orientation'], 'portrait')
        self.assertTrue(payload['icons'])

    def test_driver_service_worker_caches_driver_assets_and_reports_version(self):
        response = self.client.get(reverse('driver_service_worker'), HTTP_HOST='localhost')
        script = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Service-Worker-Allowed'], '/driver/')
        self.assertIn('driver-mobile-shell-v21', script)
        self.assertIn('/driver/', script)
        self.assertIn('/driver/shift/', script)
        self.assertIn('/driver.webmanifest', script)
        self.assertIn('/static/css/app.css', script)
        self.assertIn('ignoreSearch: true', script)
        self.assertIn('GET_VERSION', script)
        self.assertIn('SKIP_WAITING', script)

    def test_admin_opens_system_admin_dashboard(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )

        login_response = self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        dashboard_response = self.client.get('/system-admin/', HTTP_HOST='localhost')

        self.assertRedirects(login_response, '/system-admin/', target_status_code=200)
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertContains(dashboard_response, 'Р С’Р Т‘Р СР С‘Р Р…Р С”Р В° MVP')
        self.assertContains(dashboard_response, 'Р РЋР С•Р В·Р Т‘Р В°РЎвЂљРЎРЉ РЎРѓР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С”Р В°')
        self.assertContains(dashboard_response, 'Р РЋР С—РЎР‚Р В°Р Р†Р С•РЎвЂЎР Р…Р С‘Р С”Р С‘')
        self.assertContains(dashboard_response, 'href="/system-admin/employees/"')
        self.assertContains(dashboard_response, 'href="/system-admin/employees/?status=active"')
        self.assertContains(dashboard_response, 'href="/system-admin/employees/?access_status=not_activated"')
        self.assertContains(dashboard_response, 'href="/system-admin/employees/?access_status=blocked"')
        self.assertContains(dashboard_response, 'href="/system-admin/employees/?access_status=deactivated"')
        self.assertContains(dashboard_response, 'Р вЂ“РЎС“РЎР‚Р Р…Р В°Р В» Р Т‘Р ВµР в„–РЎРѓРЎвЂљР Р†Р С‘Р в„–')

    def test_admin_can_reset_shift_test_data_without_deleting_base_data(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        driver_role = Role.objects.create(code='driver_reset_test', name='Водитель самосвала')
        admin_employee = Employee.objects.create(full_name='Администратор MVP', status=Employee.Status.ACTIVE)
        driver = Employee.objects.create(full_name='Тестовый водитель', status=Employee.Status.ACTIVE)
        admin_access = EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=driver,
            role=driver_role,
            access_code='2000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        equipment_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=equipment_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='8')
        rock_type = RockType.objects.create(name='Руда', density=Decimal('2.6'))
        dump_point = DumpPoint.objects.create(name='ККД')
        shift = EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=truck,
            opened_at=timezone.now(),
            opened_by=driver,
        )
        plan = ShiftPlan.objects.create(name='Дневной план', plan_volume_m3=Decimal('1000'))
        downtime_reason, _ = DowntimeReason.objects.get_or_create(
            name='Ожидание разгрузки',
            defaults={'show_for_truck_driver': True},
        )
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            driver=driver,
            unloading_shift=shift,
            rock_type=rock_type,
            dump_point=dump_point,
            volume_m3=Decimal('49.4'),
            tonnage=Decimal('128.44'),
            status=TripStatus.COMPLETED,
            completed_at=timezone.now(),
        )
        TripClientAction.objects.create(
            action_type='trip_unloaded',
            client_action_id='reset-test-action',
            trip=trip,
            actor=driver,
        )
        DispatcherActionLog.objects.create(
            actor=admin_employee,
            action_type=DispatcherActionType.COMPLETE_TRIP,
            trip=trip,
            target_summary='Тестовый рейс',
        )
        DowntimeEvent.objects.create(
            equipment=truck,
            employee=driver,
            reason=downtime_reason,
            started_at=timezone.now() - timedelta(minutes=20),
            ended_at=timezone.now(),
        )
        OperationalStateEvent.objects.create(
            key='production',
            version=1,
            event_type='trip_changed',
            object_type='Trip',
            object_id=str(trip.id),
        )

        session = self.client.session
        session['employee_access_id'] = admin_access.id
        session.save()
        response = self.client.post(reverse('system_admin_reset_shift_test_data'), follow=True, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Trip.objects.count(), 0)
        self.assertEqual(TripClientAction.objects.count(), 0)
        self.assertEqual(DispatcherActionLog.objects.count(), 0)
        self.assertEqual(DowntimeEvent.objects.count(), 0)
        self.assertTrue(Employee.objects.filter(id=admin_employee.id).exists())
        self.assertTrue(Employee.objects.filter(id=driver.id).exists())
        self.assertTrue(Equipment.objects.filter(id=truck.id).exists())
        self.assertTrue(Equipment.objects.filter(id=excavator.id).exists())
        self.assertTrue(RockType.objects.filter(id=rock_type.id).exists())
        self.assertTrue(DumpPoint.objects.filter(id=dump_point.id).exists())
        self.assertTrue(EmployeeShift.objects.filter(id=shift.id).exists())
        self.assertTrue(ShiftPlan.objects.filter(id=plan.id).exists())
        self.assertTrue(OperationalStateEvent.objects.filter(event_type='test_shift_data_reset').exists())
        self.assertTrue(AdminActionLog.objects.filter(action='Сброшены тестовые показатели смены').exists())

    def test_admin_employee_list_can_filter_by_access_status(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        driver_role = Role.objects.create(code='driver_access_filter', name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        waiting_employee = Employee.objects.create(full_name='Р С›Р В¶Р С‘Р Т‘Р В°Р ВµРЎвЂљ Р В°Р С”РЎвЂљР С‘Р Р†Р В°РЎвЂ Р С‘Р С‘', status=Employee.Status.NOT_ACTIVATED)
        active_employee = Employee.objects.create(full_name='Р С’Р С”РЎвЂљР С‘Р Р†Р Р…РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=waiting_employee,
            role=driver_role,
            access_code='200001',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=active_employee,
            role=driver_role,
            access_code='200002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/system-admin/employees/?access_status=not_activated', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р С›Р В¶Р С‘Р Т‘Р В°Р ВµРЎвЂљ Р В°Р С”РЎвЂљР С‘Р Р†Р В°РЎвЂ Р С‘Р С‘')
        self.assertNotContains(response, 'Р С’Р С”РЎвЂљР С‘Р Р†Р Р…РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ')
        self.assertContains(response, 'name="access_status"')

    def test_admin_cannot_block_own_access(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        admin_access = EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/system-admin/accesses/{admin_access.id}/block/',
            {'reason': 'Р РЋР В»РЎС“РЎвЂЎР В°Р в„–Р Р…Р В°РЎРЏ РЎРѓР В°Р СР С•Р В±Р В»Р С•Р С”Р С‘РЎР‚Р С•Р Р†Р С”Р В°'},
            follow=True,
            HTTP_HOST='localhost',
        )
        admin_access.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(admin_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(admin_access.is_active)

    def test_admin_cannot_deactivate_own_employee_card(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/system-admin/employees/{admin_employee.id}/deactivate/',
            follow=True,
            HTTP_HOST='localhost',
        )
        admin_employee.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(admin_employee.status, Employee.Status.ACTIVE)
        self.assertTrue(admin_employee.is_active)

    def test_admin_employee_card_has_photo_upload_block(self):
        admin_role = Role.objects.create(code='admin', name='Admin')
        admin_employee = Employee.objects.create(full_name='Admin MVP', status=Employee.Status.ACTIVE)
        employee = Employee.objects.create(full_name='Employee With Photo')
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get(f'/system-admin/employees/{employee.id}/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'employee-profile-head')
        self.assertContains(response, 'employee-photo-card')
        self.assertContains(response, 'employee-photo-plus')
        self.assertContains(response, 'employee-photo-controls')
        self.assertContains(response, 'name="position"')
        self.assertContains(response, 'Р вЂќР С•Р В»Р В¶Р Р…Р С•РЎРѓРЎвЂљРЎРЉ')
        self.assertContains(response, 'type="file"')

    def test_admin_employee_card_keeps_selected_role_and_primary_pin_status(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        driver_role = Role.objects.create(code='driver_primary_pin', name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        employee = Employee.objects.create(full_name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓ Р Т‘Р С•РЎРѓРЎвЂљРЎС“Р С—Р С•Р С', status=Employee.Status.ACTIVE)
        employee_access = EmployeeAccess.objects.create(
            employee=employee,
            role=driver_role,
            access_code='246824',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            primary_code_issued_at=timezone.now(),
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get(f'/system-admin/employees/{employee.id}/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'<option value="{driver_role.id}" selected>', html=False)
        self.assertContains(response, '2468')
        self.assertContains(response, 'Р С•Р В¶Р С‘Р Т‘Р В°Р ВµРЎвЂљ Р С—Р ВµРЎР‚Р Р†Р С•Р С–Р С• Р Р†РЎвЂ¦Р С•Р Т‘Р В°')

        employee_access.access_code = '8642'
        employee_access.status = EmployeeAccess.Status.ACTIVATED
        employee_access.activated_at = timezone.now()
        employee_access.last_login_at = timezone.now()
        employee_access.save(update_fields=['access_code', 'status', 'activated_at', 'last_login_at'])

        activated_response = self.client.get(f'/system-admin/employees/{employee.id}/', HTTP_HOST='localhost')

        self.assertContains(activated_response, f'<option value="{driver_role.id}" selected>', html=False)
        self.assertContains(activated_response, 'Р СџР С‘Р Р…Р С”Р С•Р Т‘ Р В°Р С”РЎвЂљР С‘Р Р†Р С‘РЎР‚Р С•Р Р†Р В°Р Р…')
        self.assertNotContains(activated_response, '8642')

        reset_response = self.client.post(
            f'/system-admin/employees/{employee.id}/generate-access/',
            {'role': driver_role.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        employee_access.refresh_from_db()

        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(employee_access.status, EmployeeAccess.Status.NOT_ACTIVATED)
        self.assertIsNone(employee_access.activated_at)
        self.assertNotEqual(employee_access.access_code, '8642')
        self.assertContains(reset_response, employee_access.access_code)
        self.assertContains(reset_response, 'Р С•Р В¶Р С‘Р Т‘Р В°Р ВµРЎвЂљ Р С—Р ВµРЎР‚Р Р†Р С•Р С–Р С• Р Р†РЎвЂ¦Р С•Р Т‘Р В°')
        self.assertNotContains(reset_response, '8642')

    def test_admin_employee_card_with_photo_has_remove_confirmation_and_modal(self):
        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                admin_role = Role.objects.create(code='admin', name='Admin')
                admin_employee = Employee.objects.create(full_name='Admin MVP', status=Employee.Status.ACTIVE)
                employee = Employee.objects.create(full_name='Employee With Photo', status=Employee.Status.ACTIVE)
                employee.photo.save('employee_photos/current.jpg', ContentFile(b'photo'), save=True)
                EmployeeAccess.objects.create(
                    employee=admin_employee,
                    role=admin_role,
                    access_code='1000',
                    status=EmployeeAccess.Status.ACTIVATED,
                    is_active=True,
                )

                self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
                response = self.client.get(f'/system-admin/employees/{employee.id}/', HTTP_HOST='localhost')

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'employee-photo-control add')
                self.assertContains(response, 'employee-photo-control remove')
                self.assertContains(response, 'data-confirm="Р Р€Р Т‘Р В°Р В»Р С‘РЎвЂљРЎРЉ РЎвЂћР С•РЎвЂљР С• РЎРѓР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С”Р В°?"')
                self.assertContains(response, 'app-confirm-modal')
                self.assertContains(response, 'data-confirm-accept')
                self.assertContains(response, 'data-confirm-cancel')
                self.assertNotContains(response, 'onclick="return window.confirm')
                self.assertContains(response, 'employee-photo-modal')

    def test_employee_photo_rejects_non_image_file(self):
        employee = Employee.objects.create(full_name='Employee Photo Validation')
        upload = SimpleUploadedFile('note.txt', b'not-image', content_type='text/plain')

        form = AdminEmployeeEditForm(
            data={'full_name': employee.full_name, 'status': Employee.Status.ACTIVE},
            files={'photo': upload},
            instance=employee,
        )

        self.assertFalse(form.is_valid())
        self.assertIn('photo', form.errors)

    def test_employee_photo_upload_is_converted_to_jpeg(self):
        employee = Employee.objects.create(full_name='Employee Photo Compression')
        image_buffer = BytesIO()
        image = Image.new('RGB', (900, 700), color=(40, 180, 150))
        image.save(image_buffer, format='PNG')
        upload = SimpleUploadedFile('photo.png', image_buffer.getvalue(), content_type='image/png')

        form = AdminEmployeeEditForm(
            data={'full_name': employee.full_name, 'status': Employee.Status.ACTIVE},
            files={'photo': upload},
            instance=employee,
        )

        self.assertTrue(form.is_valid(), form.errors)
        photo = form.cleaned_data['photo']
        self.assertTrue(photo.name.endswith('.jpg'))
        self.assertLessEqual(photo.size, 5 * 1024 * 1024)

    def test_admin_can_replace_existing_employee_photo(self):
        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                admin_role = Role.objects.create(code='admin', name='Admin')
                admin_employee = Employee.objects.create(full_name='Admin MVP', status=Employee.Status.ACTIVE)
                employee = Employee.objects.create(full_name='Employee Replace Photo', status=Employee.Status.ACTIVE)
                employee.photo.save('employee_photos/old.jpg', ContentFile(b'old-photo'), save=True)
                old_photo_name = employee.photo.name
                EmployeeAccess.objects.create(
                    employee=admin_employee,
                    role=admin_role,
                    access_code='1000',
                    status=EmployeeAccess.Status.ACTIVATED,
                    is_active=True,
                )
                image_buffer = BytesIO()
                Image.new('RGB', (700, 700), color=(120, 80, 40)).save(image_buffer, format='PNG')
                upload = SimpleUploadedFile('new-photo.png', image_buffer.getvalue(), content_type='image/png')

                self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
                response = self.client.post(
                    f'/system-admin/employees/{employee.id}/',
                    {'full_name': employee.full_name, 'status': Employee.Status.ACTIVE, 'photo': upload},
                    follow=True,
                    HTTP_HOST='localhost',
                )
                employee.refresh_from_db()

                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(employee.photo.name, old_photo_name)
                self.assertTrue(employee.photo.name.endswith('.jpg'))
                self.assertFalse(employee.photo.storage.exists(old_photo_name))
                self.assertTrue(employee.photo.storage.exists(employee.photo.name))

    def test_admin_can_remove_existing_employee_photo(self):
        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                admin_role = Role.objects.create(code='admin', name='Admin')
                admin_employee = Employee.objects.create(full_name='Admin MVP', status=Employee.Status.ACTIVE)
                employee = Employee.objects.create(full_name='Employee Remove Photo', status=Employee.Status.ACTIVE)
                employee.photo.save('employee_photos/remove.jpg', ContentFile(b'old-photo'), save=True)
                old_photo_name = employee.photo.name
                EmployeeAccess.objects.create(
                    employee=admin_employee,
                    role=admin_role,
                    access_code='1000',
                    status=EmployeeAccess.Status.ACTIVATED,
                    is_active=True,
                )

                self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
                response = self.client.post(
                    f'/system-admin/employees/{employee.id}/',
                    {'remove_photo': '1'},
                    follow=True,
                    HTTP_HOST='localhost',
                )
                employee.refresh_from_db()

                self.assertEqual(response.status_code, 200)
                self.assertFalse(employee.photo)
                self.assertFalse(employee.photo.storage.exists(old_photo_name))
                self.assertTrue(
                    AdminActionLog.objects.filter(
                        object_repr=str(employee),
                        action='Р Р€Р Т‘Р В°Р В»Р ВµР Р…Р С• РЎвЂћР С•РЎвЂљР С• РЎРѓР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С”Р В°',
                    ).exists()
                )

    def test_admin_opens_references_registry(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/system-admin/references/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р РЋР С—РЎР‚Р В°Р Р†Р С•РЎвЂЎР Р…Р С‘Р С”Р С‘ Р В°Р Т‘Р СР С‘Р Р…Р С”Р С‘')
        self.assertContains(response, 'Р вЂ™Р С‘Р Т‘РЎвЂ№ РЎвЂљР ВµРЎвЂ¦Р Р…Р С‘Р С”Р С‘')
        self.assertContains(response, 'Р СџР С•РЎР‚Р С•Р Т‘РЎвЂ№')
        self.assertContains(response, 'Р СћР С•РЎвЂЎР С”Р С‘ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘')
        self.assertContains(response, 'Р РЋР СР ВµР Р…Р Р…РЎвЂ№Р Вµ Р С—Р В»Р В°Р Р…РЎвЂ№')
        self.assertContains(response, 'Р СџР В»Р В°Р Р…РЎвЂ№ РЎвЂљР ВµРЎвЂ¦Р Р…Р С‘Р С”Р С‘')
        self.assertContains(response, '/admin/references/equipmenttype/')
        self.assertContains(response, '/system-admin/references/equipment/')
        self.assertContains(response, '/system-admin/references/shift-plans/')
        self.assertContains(response, '/system-admin/references/equipment-shift-plans/')

        detail_response = self.client.get('/system-admin/references/equipment/', HTTP_HOST='localhost')

        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, 'reference-detail-page')
        self.assertContains(detail_response, '/admin/references/equipment/')

    def test_admin_saves_shift_plans_from_reference_screen(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        equipment_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        equipment = Equipment.objects.create(equipment_type=equipment_type, garage_number='25', is_active=True)

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        shift_plan_page = self.client.get('/system-admin/references/shift-plans/', HTTP_HOST='localhost')
        self.assertContains(shift_plan_page, 'Р СћР С‘Р С— Р С—Р В»Р В°Р Р…Р В°')
        self.assertContains(shift_plan_page, 'Р СџР В»Р В°Р Р… Р С•Р В±РЎР‰Р ВµР СР В°, Р С3')
        self.assertNotContains(shift_plan_page, 'Р вЂќР В°РЎвЂљР В° РЎРѓР СР ВµР Р…РЎвЂ№')
        self.assertNotContains(shift_plan_page, 'Р СџР В»Р В°Р Р… РЎвЂљР С•Р Р…Р Р…Р В°Р В¶Р В°')
        self.assertNotContains(shift_plan_page, 'Р СџР В»Р В°Р Р… РЎР‚Р ВµР в„–РЎРѓР С•Р Р†')

        shift_plan_response = self.client.post(
            '/system-admin/references/shift-plans/',
            {
                'plan_scope': 'day_shift',
                'name': 'Р СџР В»Р В°Р Р… Р Т‘Р Р…Р ВµР Р†Р Р…Р С•Р в„– РЎРѓР СР ВµР Р…РЎвЂ№',
                'plan_volume_m3': '2500.00',
                'is_active': 'on',
                'comment': 'Р В РЎС“РЎвЂЎР Р…Р С•Р в„– Р С—Р В»Р В°Р Р… Р В°Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚Р В°',
            },
            HTTP_HOST='localhost',
        )
        shift_plan = ShiftPlan.objects.get(name='Р СџР В»Р В°Р Р… Р Т‘Р Р…Р ВµР Р†Р Р…Р С•Р в„– РЎРѓР СР ВµР Р…РЎвЂ№')

        self.assertEqual(shift_plan_response.status_code, 302)
        self.assertEqual(shift_plan.plan_scope, 'day_shift')
        self.assertEqual(shift_plan.plan_volume_m3, Decimal('2500.00'))
        self.assertIsNone(shift_plan.plan_trips)
        self.assertIsNone(shift_plan.plan_tonnage)

        equipment_plan_response = self.client.post(
            '/system-admin/references/equipment-shift-plans/',
            {
                'shift_plan': str(shift_plan.id),
                'equipment': str(equipment.id),
                'employee': '',
                'plan_trips': '20',
                'plan_volume_m3': '500.00',
                'calculation_mode': 'trips',
                'is_active': 'on',
                'comment': 'Р СџР В»Р В°Р Р… РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°',
            },
            HTTP_HOST='localhost',
        )
        equipment_plan = EquipmentShiftPlan.objects.get(shift_plan=shift_plan, equipment=equipment)

        self.assertEqual(equipment_plan_response.status_code, 302)
        self.assertEqual(equipment_plan.plan_trips, 20)
        self.assertIsNone(equipment_plan.plan_tonnage)
        self.assertEqual(equipment_plan.calculation_mode, 'trips')

    def test_reference_detail_save_keeps_selected_record_and_filters(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        equipment_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        equipment = Equipment.objects.create(equipment_type=equipment_type, garage_number='A-101', is_active=True)

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/system-admin/references/equipment/?q=A&status=active&edit={equipment.id}',
            {
                'action': 'save',
                'record_id': str(equipment.id),
                'equipment_type': str(equipment_type.id),
                'model': '',
                'garage_number': 'A-102',
                'vin': '',
                'is_own': 'on',
                'is_active': 'on',
            },
            HTTP_HOST='localhost',
        )
        equipment.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(equipment.garage_number, 'A-102')
        self.assertIn(f'/system-admin/references/equipment/?q=A&status=active&edit={equipment.id}', response['Location'])

    def test_admin_opens_conflicts_registry(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        employee = Employee.objects.create(full_name='Р РЋР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С” РЎРѓ Р С”Р С•Р Р…РЎвЂћР В»Р С‘Р С”РЎвЂљР С•Р С')
        AdminConflict.objects.create(
            employee=employee,
            role=admin_role,
            conflict_type='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р С”Р С•Р Р…РЎвЂћР В»Р С‘Р С”РЎвЂљ',
            process='Р С’Р Т‘Р СР С‘Р Р…Р С”Р В° MVP',
            description='Р СџРЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р В° Р В¶РЎС“РЎР‚Р Р…Р В°Р В»Р В° Р С”Р С•Р Р…РЎвЂћР В»Р С‘Р С”РЎвЂљР С•Р Р†',
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/system-admin/conflicts/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р С™Р С•Р Р…РЎвЂћР В»Р С‘Р С”РЎвЂљРЎвЂ№ Р В°Р Т‘Р СР С‘Р Р…Р С”Р С‘')
        self.assertContains(response, 'Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р С”Р С•Р Р…РЎвЂћР В»Р С‘Р С”РЎвЂљ')
        self.assertContains(response, 'Р РЋР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С” РЎРѓ Р С”Р С•Р Р…РЎвЂћР В»Р С‘Р С”РЎвЂљР С•Р С')
        self.assertContains(response, 'Excel')

    def test_admin_updates_conflict_status(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        conflict = AdminConflict.objects.create(
            employee=admin_employee,
            role=admin_role,
            conflict_type='Р СџРЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р В° РЎРѓРЎвЂљР В°РЎвЂљРЎС“РЎРѓР В°',
            process='Р С’Р Т‘Р СР С‘Р Р…Р С”Р В° MVP',
            description='Р СџРЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р В° РЎРѓР СР ВµР Р…РЎвЂ№ РЎРѓРЎвЂљР В°РЎвЂљРЎС“РЎРѓР В° Р С”Р С•Р Р…РЎвЂћР В»Р С‘Р С”РЎвЂљР В°',
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/system-admin/conflicts/{conflict.id}/in-progress/',
            follow=True,
            HTTP_HOST='localhost',
        )
        conflict.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(conflict.status, AdminConflict.Status.IN_PROGRESS)
        self.assertEqual(conflict.resolved_by, admin_employee)
        self.assertIsNotNone(conflict.resolved_at)
        self.assertTrue(AdminActionLog.objects.filter(action='Р ВР В·Р СР ВµР Р…Р ВµР Р… РЎРѓРЎвЂљР В°РЎвЂљРЎС“РЎРѓ Р В°Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С‘Р Р†Р Р…Р С•Р С–Р С• Р С”Р С•Р Р…РЎвЂћР В»Р С‘Р С”РЎвЂљР В°').exists())

    def test_admin_opens_action_log_registry(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        AdminActionLog.objects.create(
            actor=admin_employee,
            action='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†Р С•Р Вµ Р Т‘Р ВµР в„–РЎРѓРЎвЂљР Р†Р С‘Р Вµ',
            object_type='Employee',
            object_repr='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р С•Р В±РЎР‰Р ВµР С”РЎвЂљ',
            comment='Р СџРЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р В° Р В¶РЎС“РЎР‚Р Р…Р В°Р В»Р В° Р Т‘Р ВµР в„–РЎРѓРЎвЂљР Р†Р С‘Р в„–',
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/system-admin/logs/?q=Р СћР ВµРЎРѓРЎвЂљР С•Р Р†Р С•Р Вµ', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р вЂ“РЎС“РЎР‚Р Р…Р В°Р В» Р Т‘Р ВµР в„–РЎРѓРЎвЂљР Р†Р С‘Р в„– Р В°Р Т‘Р СР С‘Р Р…Р С”Р С‘')
        self.assertContains(response, 'Р СћР ВµРЎРѓРЎвЂљР С•Р Р†Р С•Р Вµ Р Т‘Р ВµР в„–РЎРѓРЎвЂљР Р†Р С‘Р Вµ')
        self.assertContains(response, 'Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р С•Р В±РЎР‰Р ВµР С”РЎвЂљ')
        self.assertContains(response, 'Excel')

    def test_admin_creates_employee_with_primary_pin_and_exports_accesses(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        driver_role = self.role
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        create_response = self.client.post(
            '/system-admin/employees/create/',
            {
                'full_name': 'Р СњР С•Р Р†РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ',
                'personnel_number': '001',
                'phone': '+79990000000',
                'status': Employee.Status.NOT_ACTIVATED,
                'comment': 'Р СџР ВµРЎР‚Р Р†Р С‘РЎвЂЎР Р…Р В°РЎРЏ Р В·Р В°Р С–РЎР‚РЎС“Р В·Р С”Р В°',
                'role': driver_role.id,
                'generate_access': 'on',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        employee = Employee.objects.get(full_name='Р СњР С•Р Р†РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ')
        access = EmployeeAccess.objects.get(employee=employee)

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(access.role, driver_role)
        self.assertEqual(access.status, EmployeeAccess.Status.NOT_ACTIVATED)
        self.assertEqual(len(access.access_code), 6)
        self.assertTrue(access.access_code.isdigit())
        self.assertTrue(AdminActionLog.objects.filter(action='Р РЋР С•Р В·Р Т‘Р В°Р Р… РЎРѓР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С” Р С‘ Р Р†РЎвЂ№Р Т‘Р В°Р Р… Р С—Р ВµРЎР‚Р Р†Р С‘РЎвЂЎР Р…РЎвЂ№Р в„– Р С—Р С‘Р Р…Р С”Р С•Р Т‘').exists())

        block_response = self.client.post(
            f'/system-admin/accesses/{access.id}/block/',
            {'reason': 'Р СџРЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р В° Р В±Р В»Р С•Р С”Р С‘РЎР‚Р С•Р Р†Р С”Р С‘'},
            follow=True,
            HTTP_HOST='localhost',
        )
        access.refresh_from_db()

        self.assertEqual(block_response.status_code, 200)
        self.assertEqual(access.status, EmployeeAccess.Status.BLOCKED)
        self.assertFalse(access.is_active)

        export_response = self.client.get('/system-admin/export/accesses/', HTTP_HOST='localhost')
        self.assertEqual(export_response.status_code, 200)
        workbook = load_workbook(BytesIO(export_response.content))
        self.assertIn('Р вЂќР С•РЎРѓРЎвЂљРЎС“Р С—РЎвЂ№', workbook.sheetnames)
        values = [cell.value for row in workbook['Р вЂќР С•РЎРѓРЎвЂљРЎС“Р С—РЎвЂ№'].iter_rows() for cell in row]
        self.assertIn('Р СњР С•Р Р†РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ', values)
        self.assertIn('Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°', values)

    def test_primary_pin_requires_activation_and_becomes_invalid(self):
        driver_role = self.role
        employee = Employee.objects.create(full_name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓ Р С—Р ВµРЎР‚Р Р†Р С‘РЎвЂЎР Р…РЎвЂ№Р С Р С—Р С‘Р Р…Р С”Р С•Р Т‘Р С•Р С', phone='+79000001111')
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=driver_role,
            access_code='246824',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            primary_code_issued_at=timezone.now(),
        )

        login_response = self.client.post(
            '/',
            {'phone': '+79000001111', 'access_code': '246824'},
            follow=True,
            HTTP_HOST='localhost',
        )
        self.assertRedirects(login_response, '/activate-access/', target_status_code=200)
        self.assertContains(login_response, 'Р С’Р С”РЎвЂљР С‘Р Р†Р С‘РЎР‚Р С•Р Р†Р В°РЎвЂљРЎРЉ Р Т‘Р С•РЎРѓРЎвЂљРЎС“Р С—')
        self.assertContains(login_response, 'name="phone"')
        self.assertContains(login_response, 'name="new_access_code"')

        activation_response = self.client.post(
            '/activate-access/',
            {'phone': '+7 (900) 000-11-11', 'new_access_code': '864286', 'confirm_access_code': '864286'},
            follow=True,
            HTTP_HOST='localhost',
        )
        access.refresh_from_db()
        employee.refresh_from_db()

        self.assertEqual(activation_response.status_code, 200)
        self.assertEqual(access.access_code, '864286')
        self.assertEqual(access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertIsNone(EmployeeAccess.objects.filter(access_code='246824').first())

        self.client.get('/logout/', follow=True, HTTP_HOST='localhost')
        old_code_response = self.client.post('/', {'access_code': '246824'}, follow=True, HTTP_HOST='localhost')
        self.assertContains(old_code_response, 'Р СћР ВµР В»Р ВµРЎвЂћР С•Р Р… Р С‘Р В»Р С‘ Р С—Р С‘Р Р…Р С”Р С•Р Т‘ РЎС“Р С”Р В°Р В·Р В°Р Р…РЎвЂ№ Р Р…Р ВµР Р†Р ВµРЎР‚Р Р…Р С•.')

        new_code_response = self.client.post(
            '/',
            {'phone': '+79000001111', 'access_code': access.access_code},
            follow=True,
            HTTP_HOST='localhost',
        )
        self.assertEqual(new_code_response.status_code, 200)
        self.assertEqual(self.client.session.get('employee_access_id'), access.id)

    def test_activation_allows_same_pin_for_different_phone_numbers(self):
        driver_role = self.role
        first_employee = Employee.objects.create(full_name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓ Р С—Р С•РЎРѓРЎвЂљР С•РЎРЏР Р…Р Р…РЎвЂ№Р С Р С—Р С‘Р Р…Р С”Р С•Р Т‘Р С•Р С', phone='+79000001111')
        EmployeeAccess.objects.create(
            employee=first_employee,
            role=driver_role,
            access_code='864286',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        second_employee = Employee.objects.create(full_name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓ Р С—Р ВµРЎР‚Р Р†Р С‘РЎвЂЎР Р…РЎвЂ№Р С Р С—Р С‘Р Р…Р С”Р С•Р Т‘Р С•Р С', phone='+79000002222')
        EmployeeAccess.objects.create(
            employee=second_employee,
            role=driver_role,
            access_code='246824',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            primary_code_issued_at=timezone.now(),
            is_active=True,
        )

        self.client.post('/', {'phone': '+79000002222', 'access_code': '246824'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            '/activate-access/',
            {'phone': '+7 900 000-22-22', 'new_access_code': '864286', 'confirm_access_code': '864286'},
            follow=True,
            HTTP_HOST='localhost',
        )
        second_access = EmployeeAccess.objects.get(employee=second_employee)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Р В­РЎвЂљР С•РЎвЂљ Р С—Р С‘Р Р…Р С”Р С•Р Т‘ Р Р…Р ВµР В»РЎРЉР В·РЎРЏ Р С‘РЎРѓР С—Р С•Р В»РЎРЉР В·Р С•Р Р†Р В°РЎвЂљРЎРЉ')
        self.assertNotContains(response, 'Р СћР В°Р С”Р С•Р в„– Р С—Р С‘Р Р…Р С”Р С•Р Т‘ РЎС“Р В¶Р Вµ Р С‘РЎРѓР С—Р С•Р В»РЎРЉР В·РЎС“Р ВµРЎвЂљРЎРѓРЎРЏ')
        self.assertEqual(second_access.access_code, '864286')
        self.assertEqual(second_access.status, EmployeeAccess.Status.ACTIVATED)

    def test_admin_can_delete_employee_without_production_history(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        employee = Employee.objects.create(full_name='Р РЋР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С” Р В±Р ВµР В· Р С‘РЎРѓРЎвЂљР С•РЎР‚Р С‘Р С‘')

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/system-admin/employees/{employee.id}/delete/',
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Employee.objects.filter(id=employee.id).exists())
        self.assertContains(response, 'РЎС“Р Т‘Р В°Р В»Р ВµР Р…')

    def test_admin_cannot_delete_employee_with_production_history(self):
        admin_role = Role.objects.create(code='admin', name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚')
        admin_employee = Employee.objects.create(full_name='Р С’Р Т‘Р СР С‘Р Р…Р С‘РЎРѓРЎвЂљРЎР‚Р В°РЎвЂљР С•РЎР‚ MVP', status=Employee.Status.ACTIVE)
        EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='1000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        employee = Employee.objects.create(full_name='Р РЋР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С” РЎРѓ Р С‘РЎРѓРЎвЂљР С•РЎР‚Р С‘Р ВµР в„–')
        EmployeeShift.objects.create(
            employee=employee,
            shift_type='day',
            opened_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '1000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/system-admin/employees/{employee.id}/delete/',
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Employee.objects.filter(id=employee.id).exists())
        self.assertTrue(AdminConflict.objects.filter(employee=employee, conflict_type='Р СџР С•Р С—РЎвЂ№РЎвЂљР С”Р В° РЎС“Р Т‘Р В°Р В»Р ВµР Р…Р С‘РЎРЏ РЎРѓР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С”Р В° РЎРѓ Р С‘РЎРѓРЎвЂљР С•РЎР‚Р С‘Р ВµР в„–').exists())
        self.assertContains(response, 'Р Р€Р Т‘Р В°Р В»Р ВµР Р…Р С‘Р Вµ Р В·Р В°Р С—РЎР‚Р ВµРЎвЂ°Р ВµР Р…Р С•')

    def test_wrong_access_code_stays_on_login(self):
        response = self.client.post('/', {'access_code': 'wrong'}, follow=True, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р СћР ВµР В»Р ВµРЎвЂћР С•Р Р… Р С‘Р В»Р С‘ Р С—Р С‘Р Р…Р С”Р С•Р Т‘ РЎС“Р С”Р В°Р В·Р В°Р Р…РЎвЂ№ Р Р…Р ВµР Р†Р ВµРЎР‚Р Р…Р С•.')
        self.assertIsNone(self.client.session.get('employee_access_id'))
        self.assertContains(response, 'login-page')

    def test_interface_map_opens_without_login(self):
        response = self.client.get('/interfaces/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р СњР В°Р В·Р В°Р Т‘')
        self.assertContains(response, 'Р вЂњР В»Р В°Р Р†Р Р…Р В°РЎРЏ')
        self.assertContains(response, 'Р С™Р В°РЎР‚РЎвЂљР В° Р С‘Р Р…РЎвЂљР ВµРЎР‚РЎвЂћР ВµР в„–РЎРѓР С•Р Р†')
        self.assertContains(response, 'Р С™Р В°РЎР‚РЎвЂљР В° Р С‘Р Р…РЎвЂљР ВµРЎР‚РЎвЂћР ВµР в„–РЎРѓР С•Р Р† MVP')
        self.assertContains(response, '/reports/volume/')
        self.assertContains(response, '/reports/templates/')
        self.assertContains(response, '/reports/management/')
        self.assertContains(response, '/reports/management/export/')
        self.assertContains(response, '/reports/pilot-checklist/')
        self.assertContains(response, '/reports/pilot-scenario/')
        self.assertContains(response, '/reports/pilot-feedback/')
        self.assertContains(response, '/system-admin/employees/')
        self.assertContains(response, '/system-admin/references/')
        self.assertContains(response, '/system-admin/conflicts/')
        self.assertContains(response, '/system-admin/logs/')
        self.assertContains(response, 'Excel-Р Р†РЎвЂ№Р С–РЎР‚РЎС“Р В·Р С”Р В° Р Р†Р С‘РЎвЂљРЎР‚Р С‘Р Р…РЎвЂ№ РЎР‚РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р В°')
        self.assertContains(response, 'Р В§Р ВµР С”Р В»Р С‘РЎРѓРЎвЂљ Р С—Р С‘Р В»Р С•РЎвЂљР Р…Р С•Р в„– Р С—РЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р С‘ Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљР С•Р Р†')
        self.assertContains(response, 'Р РЋРЎвЂ Р ВµР Р…Р В°РЎР‚Р С‘Р в„– Р С—Р С‘Р В»Р С•РЎвЂљР Р…Р С•Р С–Р С• Р В·Р В°Р С—РЎС“РЎРѓР С”Р В°')
        self.assertContains(response, 'Р вЂ“РЎС“РЎР‚Р Р…Р В°Р В» Р В·Р В°Р СР ВµРЎвЂЎР В°Р Р…Р С‘Р в„– Р С—Р С‘Р В»Р С•РЎвЂљР В°')
        self.assertContains(response, '6000')

    def test_manager_can_open_pilot_report_checklist(self):
        manager_role = Role.objects.create(code='manager', name='Р В РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р С•')
        manager = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†Р С•Р Вµ РЎР‚РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р С•')
        EmployeeAccess.objects.create(employee=manager, role=manager_role, access_code='6000')

        self.client.post('/', {'access_code': '6000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/pilot-checklist/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р В§Р ВµР С”Р В»Р С‘РЎРѓРЎвЂљ Р С—Р С‘Р В»Р С•РЎвЂљР Р…Р С•Р в„– Р С—РЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р С‘ Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљР С•Р Р†')
        self.assertContains(response, '9 Р С‘Р В· 10')
        self.assertContains(response, '99%')
        self.assertContains(response, 'Р РЋР Р†Р ВµРЎР‚Р С”Р В° РЎРѓР С• РЎРѓРЎвЂљР В°РЎР‚РЎвЂ№Р СР С‘ Excel-РЎвЂћР С•РЎР‚Р СР В°Р СР С‘')
        self.assertContains(response, 'Р С›РЎвЂљРЎвЂЎР ВµРЎвЂљ_Р С™Р С•Р С—Р С—Р ВµРЎР‚. Р В Р С‘РЎРѓР С•РЎР‚РЎРѓР ВµР В·_Р СљР В°РЎР‚РЎвЂљ.xlsx')
        self.assertContains(response, 'Р С—Р С•РЎвЂЎР В°РЎРѓР С•Р Р†Р С•Р в„– Р СљР В°РЎР‚РЎвЂљ.xlsx')
        self.assertContains(response, '/reports/volume/?group_by=completed_hour')
        self.assertContains(response, 'Р С›Р В  Р С™Р С™Р вЂќ Р РЋР С™Р вЂќР В  Р СР В°РЎР‚РЎвЂљ.xlsx')
        self.assertContains(response, 'РЎС“Р Т‘Р ВµР В»РЎРЉР Р…РЎвЂ№Р в„–_Р Р†Р ВµРЎРѓР В°_РЎР‚РЎС“Р Т‘_Р С‘_Р С—Р С•РЎР‚Р С•Р Т‘_Р СљР В°Р В»Р СРЎвЂ№Р В¶РЎРѓР С”Р С•Р С–Р С•_Р СР ВµРЎРѓРЎвЂљР С•РЎР‚Р С•Р Т‘Р ВµР Р…Р С‘РЎРЏ.xlsx')
        self.assertContains(response, '/admin/references/rocktype/')
        self.assertContains(response, 'Р С™Р ВР Сџ/Р С™Р СћР вЂњ Р С‘ Р С™Р ВР С›/Р С™Р СћР вЂњ')
        self.assertContains(response, '/reports/management/')
        self.assertContains(response, '/reports/management/export/')
        self.assertContains(response, '/dispatcher/control/')
        self.assertContains(response, '/reports/volume/')
        self.assertContains(response, '/reports/volume/export/')
        self.assertContains(response, '/reports/templates/')
        self.assertContains(response, '/reports/customer-daily/')
        self.assertContains(response, '/reports/customer-daily/export/')
        self.assertContains(response, '/reports/downtimes/')
        self.assertContains(response, '/reports/downtimes/export/')
        self.assertContains(response, '/reports/pilot-scenario/')
        self.assertContains(response, '/reports/pilot-feedback/')

    def test_manager_can_open_pilot_launch_scenario(self):
        manager_role = Role.objects.create(code='manager', name='Р В РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р С•')
        manager = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†Р С•Р Вµ РЎР‚РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р С•')
        EmployeeAccess.objects.create(employee=manager, role=manager_role, access_code='6000')

        self.client.post('/', {'access_code': '6000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/pilot-scenario/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р РЋРЎвЂ Р ВµР Р…Р В°РЎР‚Р С‘Р в„– Р С—Р С‘Р В»Р С•РЎвЂљР Р…Р С•Р С–Р С• Р В·Р В°Р С—РЎС“РЎРѓР С”Р В°')
        self.assertContains(response, '9 Р С‘Р В· 10')
        self.assertContains(response, '99%')
        self.assertContains(response, 'Р В Р В°РЎРѓРЎРѓРЎвЂљР В°Р Р…Р С•Р Р†Р С”Р В° РЎвЂљР ВµРЎвЂ¦Р Р…Р С‘Р С”Р С‘')
        self.assertContains(response, 'Р В Р В°Р В±Р С•РЎвЂљР В° Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЏ')
        self.assertContains(response, 'Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚РЎРѓР С”Р С‘Р в„– Р С”Р С•Р Р…РЎвЂљРЎР‚Р С•Р В»РЎРЉ')
        self.assertContains(response, 'Р вЂ™Р С•Р С—РЎР‚Р С•РЎРѓРЎвЂ№ Р Т‘Р В»РЎРЏ РЎвЂћР С‘Р С”РЎРѓР В°РЎвЂ Р С‘Р С‘ Р Р†Р С• Р Р†РЎР‚Р ВµР СРЎРЏ Р С—Р С‘Р В»Р С•РЎвЂљР В°')
        self.assertContains(response, '/reports/pilot-feedback/')
        self.assertContains(response, '31_Р вЂ“Р Р€Р В Р СњР С’Р вЂє_Р вЂ”Р С’Р СљР вЂўР В§Р С’Р СњР ВР в„ў_Р СџР ВР вЂєР С›Р СћР С’.md')

    def test_manager_can_create_pilot_feedback_and_export_it(self):
        manager_role = Role.objects.create(code='manager', name='Р В РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р С•')
        manager = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†Р С•Р Вµ РЎР‚РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р С•')
        EmployeeAccess.objects.create(employee=manager, role=manager_role, access_code='6000')

        self.client.post('/', {'access_code': '6000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            '/reports/pilot-feedback/',
            {
                'title': 'Р СњР Вµ РЎвЂ¦Р Р†Р В°РЎвЂљР В°Р ВµРЎвЂљ РЎРѓРЎвЂљР С•Р В»Р В±РЎвЂ Р В° Р Т‘Р В»РЎРЏ РЎРѓР Р†Р ВµРЎР‚Р С”Р С‘',
                'category': 'report',
                'priority': 'p1',
                'status': 'new',
                'screen': 'Р РЋРЎС“РЎвЂљР С•РЎвЂЎР Р…РЎвЂ№Р в„– Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљ',
                'description': 'Р СњР В° Р С—Р С‘Р В»Р С•РЎвЂљР Вµ Р Р…РЎС“Р В¶Р Р…Р С• РЎРѓР Р†Р ВµРЎР‚Р С‘РЎвЂљРЎРЉ РЎРѓРЎвЂљР В°РЎР‚РЎС“РЎР‹ РЎвЂћР С•РЎР‚Р СРЎС“ Р В·Р В°Р С”Р В°Р В·РЎвЂЎР С‘Р С”Р В°.',
                'decision': 'Р вЂќР С•Р В±Р В°Р Р†Р С‘РЎвЂљРЎРЉ Р Р† РЎРѓР С—Р С‘РЎРѓР С•Р С” Р Т‘Р С•РЎР‚Р В°Р В±Р С•РЎвЂљР С•Р С” Р С—Р С•РЎРѓР В»Р Вµ Р С—РЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р С‘.',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р вЂ“РЎС“РЎР‚Р Р…Р В°Р В» Р В·Р В°Р СР ВµРЎвЂЎР В°Р Р…Р С‘Р в„– Р С—Р С‘Р В»Р С•РЎвЂљР В°')
        self.assertContains(response, 'Р СњР Вµ РЎвЂ¦Р Р†Р В°РЎвЂљР В°Р ВµРЎвЂљ РЎРѓРЎвЂљР С•Р В»Р В±РЎвЂ Р В° Р Т‘Р В»РЎРЏ РЎРѓР Р†Р ВµРЎР‚Р С”Р С‘')
        self.assertContains(response, 'P1 - Р С‘РЎРѓР С—РЎР‚Р В°Р Р†Р С‘РЎвЂљРЎРЉ Р Т‘Р С• Р В·Р В°Р С—РЎС“РЎРѓР С”Р В°')
        self.assertContains(response, 'Р вЂ™ РЎР‚Р В°Р В±Р С•РЎвЂљРЎС“')
        self.assertContains(response, 'Р В Р ВµРЎв‚¬Р ВµР Р…Р С•')
        self.assertContains(response, 'Р С›РЎвЂљР С”Р В»Р С•Р Р…Р ВµР Р…Р С•')
        self.assertEqual(PilotFeedback.objects.count(), 1)
        feedback = PilotFeedback.objects.first()
        self.assertEqual(feedback.created_by, manager)

        status_response = self.client.post(
            '/reports/pilot-feedback/',
            {
                'action': 'change_status',
                'feedback_id': str(feedback.id),
                'status': 'decided',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(status_response.status_code, 200)
        feedback.refresh_from_db()
        self.assertEqual(feedback.status, 'decided')
        self.assertContains(status_response, 'Р В Р ВµРЎв‚¬Р ВµР Р…Р С‘Р Вµ Р С—РЎР‚Р С‘Р Р…РЎРЏРЎвЂљР С•')

        export_response = self.client.get('/reports/pilot-feedback/export/', HTTP_HOST='localhost')

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        workbook = load_workbook(BytesIO(export_response.content))
        self.assertIn('Р вЂ”Р В°Р СР ВµРЎвЂЎР В°Р Р…Р С‘РЎРЏ Р С—Р С‘Р В»Р С•РЎвЂљР В°', workbook.sheetnames)
        sheet = workbook['Р вЂ”Р В°Р СР ВµРЎвЂЎР В°Р Р…Р С‘РЎРЏ Р С—Р С‘Р В»Р С•РЎвЂљР В°']
        self.assertEqual(sheet['A1'].value, 'Р вЂ“РЎС“РЎР‚Р Р…Р В°Р В» Р В·Р В°Р СР ВµРЎвЂЎР В°Р Р…Р С‘Р в„– Р С—Р С‘Р В»Р С•РЎвЂљР Р…Р С•Р С–Р С• Р В·Р В°Р С—РЎС“РЎРѓР С”Р В°')
        self.assertEqual(sheet['F5'].value, 'Р СњР Вµ РЎвЂ¦Р Р†Р В°РЎвЂљР В°Р ВµРЎвЂљ РЎРѓРЎвЂљР С•Р В»Р В±РЎвЂ Р В° Р Т‘Р В»РЎРЏ РЎРѓР Р†Р ВµРЎР‚Р С”Р С‘')

    def test_driver_primary_registration_flow(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')

        login_response = self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        self.assertRedirects(login_response, '/driver/registration/', target_status_code=200)

        registration_response = self.client.post(
            '/driver/registration/',
            {
                'dormitory_section': section.id,
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(registration_response.status_code, 200)
        self.assertContains(registration_response, 'Р С›РЎвЂљР С”РЎР‚РЎвЂ№РЎвЂљРЎРЉ РЎРѓР СР ВµР Р…РЎС“')
        self.assertTrue(self.employee.driver_registration)

    def test_driver_can_open_shift_after_registration(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')

        self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        self.client.post(
            '/driver/registration/',
            {'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        response = self.client.post(
            '/driver/shift/',
            {'shift_type': 'day', 'truck': truck.id, 'start_fuel': '100', 'start_mileage': '2500', 'start_engine_hours': '700'},
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р РЋР СР ВµР Р…Р В° Р С•РЎвЂљР С”РЎР‚РЎвЂ№РЎвЂљР В°')
        self.assertTrue(self.employee.employeeshift_set.filter(closed_at__isnull=True).exists())

    def test_driver_can_close_shift_and_next_opening_uses_last_end_values(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')

        self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        self.client.post(
            '/driver/registration/',
            {'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        self.client.post(
            '/driver/shift/',
            {'shift_type': 'day', 'truck': truck.id, 'start_fuel': '100', 'start_mileage': '2500', 'start_engine_hours': '700'},
            follow=True,
            HTTP_HOST='localhost',
        )

        close_response = self.client.post(
            '/driver/shift/close/',
            {'end_fuel': '90', 'end_mileage': '2600', 'end_engine_hours': '712'},
            follow=True,
            HTTP_HOST='localhost',
        )
        shift = EmployeeShift.objects.get(employee=self.employee)

        self.assertEqual(close_response.status_code, 200)
        self.assertContains(close_response, 'Р РЋР СР ВµР Р…Р В° Р В·Р В°Р С”РЎР‚РЎвЂ№РЎвЂљР В°')
        self.assertIsNotNone(shift.closed_at)
        self.assertEqual(shift.end_fuel, 90)
        self.assertEqual(shift.end_mileage, 2600)
        self.assertEqual(shift.end_engine_hours, 712)

        next_open_response = self.client.get(f'/driver/shift/?truck={truck.id}', HTTP_HOST='localhost')
        self.assertContains(next_open_response, 'value="90')
        self.assertContains(next_open_response, 'value="2600')
        self.assertContains(next_open_response, 'value="712')

    def test_driver_can_accept_haul_assignment(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')

        self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        self.client.post(
            '/driver/registration/',
            {'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        self.client.post(
            '/driver/shift/',
            {'shift_type': 'day', 'truck': truck.id, 'start_fuel': '100', 'start_mileage': '2500', 'start_engine_hours': '700'},
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment = HaulAssignment.objects.create(truck=truck, excavator=excavator)

        shift_response = self.client.get('/driver/shift/', HTTP_HOST='localhost')
        self.assertContains(shift_response, 'Р СњР С•Р Р†Р С•Р Вµ Р Р…Р В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘Р Вµ')
        self.assertContains(shift_response, 'Р СџРЎР‚Р С‘Р Р…РЎРЏРЎвЂљРЎРЉ')

        accept_response = self.client.post(
            f'/driver/assignment/{assignment.id}/accept/',
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment.refresh_from_db()

        self.assertEqual(accept_response.status_code, 200)
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNotNone(assignment.accepted_at)
        self.assertNotContains(accept_response, 'Р СњР С•Р Р†Р С•Р Вµ Р Р…Р В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘Р Вµ')
        self.assertNotContains(accept_response, 'Р С—РЎР‚Р С‘Р Р…РЎРЏРЎвЂљРЎРЉ Р Р…Р В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘Р Вµ')
        self.assertContains(accept_response, 'Р СџР Р€Р РЋР СћР С›Р в„ў')
        self.assertContains(accept_response, '1')

    def test_excavator_creates_trip_and_driver_completes_it(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        skdr_point = DumpPoint.objects.create(name='Р РЋР С™Р вЂќР В ')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')
        excavator_role = Role.objects.create(code='excavator_operator', name='Р СљР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ РЎРЊР С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚Р В°')
        excavator_operator = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р СР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ')
        EmployeeAccess.objects.create(employee=excavator_operator, role=excavator_role, access_code='3000')
        operator_shift = EmployeeShift.objects.create(
            employee=excavator_operator,
            shift_type='day',
            equipment=excavator,
            opened_at=timezone.now(),
        )

        driver_client = self.client
        driver_client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        driver_client.post(
            '/driver/registration/',
            {'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        driver_client.post(
            '/driver/shift/',
            {'shift_type': 'day', 'truck': truck.id, 'start_fuel': '100', 'start_mileage': '2500', 'start_engine_hours': '700'},
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment = HaulAssignment.objects.create(truck=truck, excavator=excavator, status=AssignmentStatus.ACCEPTED)

        operator_client = self.client_class(HTTP_HOST='localhost')
        operator_client.post('/', {'access_code': '3000'}, follow=True, HTTP_HOST='localhost')
        trip_response = operator_client.post(
            '/excavator/work/',
            {
                'assignment': assignment.id,
                'rock_type': rock.id,
                'dump_point': dump_point.id,
                'planned_volume_m3': '7000',
                'loading_horizon': '75',
                'loading_block': '52',
                'transport_distance_km': '3.10',
                'downtime_text': 'Р В·Р В°РЎвЂЎР С‘РЎРѓРЎвЂљР С”Р В° Р В·Р В°Р В±Р С•РЎРЏ',
                'note': 'Р С—РЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р В° Р С—Р В°РЎР‚Р В°Р СР ВµРЎвЂљРЎР‚Р С•Р Р† Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљР В°',
            },
            follow=True,
            HTTP_HOST='localhost',
        )
        self.assertEqual(trip_response.status_code, 200)
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.ACTIVE)
        self.assertEqual(trip.loading_shift, operator_shift)
        self.assertEqual(trip.planned_volume_m3, Decimal('7000.00'))
        self.assertEqual(trip.loading_horizon, '75')
        self.assertEqual(trip.loading_block, '52')
        self.assertEqual(trip.transport_distance_km, Decimal('3.10'))
        self.assertEqual(trip.downtime_text, 'Р В·Р В°РЎвЂЎР С‘РЎРѓРЎвЂљР С”Р В° Р В·Р В°Р В±Р С•РЎРЏ')
        self.assertEqual(trip.note, 'Р С—РЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р В° Р С—Р В°РЎР‚Р В°Р СР ВµРЎвЂљРЎР‚Р С•Р Р† Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљР В°')

        next_trip_form_response = operator_client.get('/excavator/work/', HTTP_HOST='localhost')
        self.assertContains(next_trip_form_response, 'value="7000')
        self.assertContains(next_trip_form_response, 'value="75"')
        self.assertContains(next_trip_form_response, 'value="52"')
        self.assertContains(next_trip_form_response, 'value="3.10"')

        driver_shift_response = driver_client.get('/driver/shift/', HTTP_HOST='localhost')
        self.assertContains(driver_shift_response, 'Р вЂњРЎР‚РЎС“Р В¶Р ВµР Р…')
        self.assertContains(driver_shift_response, 'РЎС“Р Т‘Р ВµРЎР‚Р В¶Р С‘Р Р†Р В°РЎвЂљРЎРЉ')
        self.assertContains(driver_shift_response, 'Р вЂ™РЎвЂ№Р В±Р С•РЎР‚ РЎвЂљР С•РЎвЂЎР С”Р С‘')
        self.assertNotContains(driver_shift_response, 'Р С’Р С”РЎвЂљР С‘Р Р†Р Р…РЎвЂ№Р в„– РЎР‚Р ВµР в„–РЎРѓ')
        self.assertNotContains(driver_shift_response, 'Р В Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С‘Р В»РЎРѓРЎРЏ')

        change_point_response = driver_client.post(
            f'/driver/trip/{trip.id}/change-unload-point/',
            {'client_action_id': 'driver-change-point-1', 'dump_point': skdr_point.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        trip.refresh_from_db()

        self.assertEqual(change_point_response.status_code, 200)
        self.assertEqual(trip.assigned_dump_point, dump_point)
        self.assertEqual(trip.actual_dump_point, skdr_point)
        self.assertEqual(trip.dump_point, skdr_point)
        self.assertTrue(
            TripClientAction.objects.filter(
                action_type='change_actual_unload_point',
                client_action_id='driver-change-point-1',
                trip=trip,
                actor=self.employee,
            ).exists()
        )

        complete_response = driver_client.post(
            f'/driver/trip/{trip.id}/complete/',
            {'client_action_id': 'driver-unload-test-1'},
            follow=True,
            HTTP_HOST='localhost',
        )
        trip.refresh_from_db()

        self.assertEqual(complete_response.status_code, 200)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, self.employee)
        self.assertIsNotNone(trip.completed_at)
        self.assertFalse(trip.is_carryover)
        self.assertTrue(
            TripClientAction.objects.filter(
                action_type='trip_unloaded',
                client_action_id='driver-unload-test-1',
                trip=trip,
                actor=self.employee,
            ).exists()
        )

    def test_driver_sees_truck_loaded_event_from_excavator_realtime_shell(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')
        excavator_role = Role.objects.create(code='excavator_operator', name='Р СљР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ РЎРЊР С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚Р В°')
        excavator_operator = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р СР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ')
        EmployeeAccess.objects.create(employee=excavator_operator, role=excavator_role, access_code='3000')
        EmployeeShift.objects.create(
            employee=excavator_operator,
            shift_type='day',
            equipment=excavator,
            opened_at=timezone.now(),
        )

        driver_client = self.client
        driver_client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        driver_client.post(
            '/driver/registration/',
            {'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        driver_client.post(
            '/driver/shift/',
            {
                'shift_type': 'day',
                'truck': truck.id,
                'start_fuel': '100',
                'start_mileage': '2500',
                'start_engine_hours': '700',
            },
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.ACCEPTED,
        )

        operator_client = self.client_class(HTTP_HOST='localhost')
        operator_client.post('/', {'access_code': '3000'}, follow=True, HTTP_HOST='localhost')
        response = operator_client.post(
            '/excavator/truck-loaded/',
            data=json.dumps({
                'client_action_id': 'truck-loaded-driver-sync-1',
                'truck_id': truck.id,
                'excavator_id': excavator.id,
                'dump_point_id': dump_point.id,
                'rock_type_id': rock.id,
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )
        trip = Trip.objects.get()
        driver_shift_response = driver_client.get('/driver/shift/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['action'], 'truck_loaded')
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.assigned_dump_point, dump_point)
        self.assertEqual(assignment.truck, truck)
        self.assertContains(driver_shift_response, 'Р вЂњРЎР‚РЎС“Р В¶Р ВµР Р…')
        self.assertContains(driver_shift_response, 'Р С™Р С™Р вЂќ')
        self.assertContains(driver_shift_response, 'window.applyOperationalStateRefresh')
        self.assertContains(driver_shift_response, 'data-realtime-mode="custom"')
        self.assertContains(driver_shift_response, 'driver-mobile-shell-v27')

    def test_driver_downtime_buttons_are_rendered_from_server_reference(self):
        truck = self.create_registered_driver_shift()
        DowntimeReason.objects.all().update(show_for_truck_driver=False)
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        waiting_reason = DowntimeReason.objects.create(
            name='Р СћР ВµРЎРѓРЎвЂљ Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎвЂћРЎР‚Р С•Р Р…РЎвЂљ РЎР‚Р В°Р В±Р С•РЎвЂљ',
            short_label='Р В¤РЎР‚Р С•Р Р…РЎвЂљ',
            show_for_truck_driver=True,
            sort_order=10,
        )
        truck_reason = DowntimeReason.objects.create(
            name='Р СћР ВµРЎРѓРЎвЂљ Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎвЂЎР С‘РЎРѓРЎвЂљР С”Р В° Р С”РЎС“Р В·Р С•Р Р†Р В°',
            short_label='Р С™РЎС“Р В·Р С•Р Р†',
            equipment_type=truck.equipment_type,
            show_for_truck_driver=True,
            sort_order=20,
        )
        hidden_reason = DowntimeReason.objects.create(
            name='Р СћР ВµРЎРѓРЎвЂљ Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР С”РЎР‚РЎвЂ№РЎвЂљРЎвЂ№Р в„– Р С—РЎР‚Р С•РЎРѓРЎвЂљР С•Р в„–',
            short_label='Р РЋР С”РЎР‚РЎвЂ№РЎвЂљРЎвЂ№Р в„–',
            show_for_truck_driver=False,
        )
        excavator_reason = DowntimeReason.objects.create(
            name='Р СћР ВµРЎРѓРЎвЂљ Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎвЂЎРЎС“Р В¶Р С•Р в„– РЎРЊР С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚',
            short_label='Р вЂ”Р В°Р В±Р С•Р в„–',
            equipment_type=excavator_type,
            show_for_truck_driver=True,
        )

        response = self.client.get('/driver/?tab=downtimes', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="driver-downtime-list"')
        self.assertContains(response, f'data-driver-downtime-reason-id="{waiting_reason.id}"')
        self.assertContains(response, f'name="reason_id" value="{waiting_reason.id}"')
        self.assertContains(response, 'Р В¤РЎР‚Р С•Р Р…РЎвЂљ')
        self.assertContains(response, f'data-driver-downtime-reason-id="{truck_reason.id}"')
        self.assertContains(response, 'Р С™РЎС“Р В·Р С•Р Р†')
        self.assertNotContains(response, hidden_reason.button_label)
        self.assertNotContains(response, excavator_reason.button_label)

    def test_driver_downtime_empty_state_uses_reference_message(self):
        self.create_registered_driver_shift()
        DowntimeReason.objects.all().update(show_for_truck_driver=False)

        response = self.client.get('/driver/?tab=downtimes', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р С’Р С”РЎвЂљР С‘Р Р†Р Р…РЎвЂ№Р Вµ Р С—РЎР‚Р С‘РЎвЂЎР С‘Р Р…РЎвЂ№ Р С—РЎР‚Р С•РЎРѓРЎвЂљР С•Р ВµР Р† Р Т‘Р В»РЎРЏ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р С•Р Р† Р Р…Р Вµ Р Р…Р В°Р в„–Р Т‘Р ВµР Р…РЎвЂ№')

    def test_driver_downtime_action_validates_reason_by_workplace_and_equipment_type(self):
        truck = self.create_registered_driver_shift()
        DowntimeReason.objects.all().update(show_for_truck_driver=False)
        allowed_reason = DowntimeReason.objects.create(
            name='Р СћР ВµРЎРѓРЎвЂљ Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р С•Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘',
            short_label='Р В Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р В°',
            show_for_truck_driver=True,
        )
        forbidden_reason = DowntimeReason.objects.create(
            name='Р СћР ВµРЎРѓРЎвЂљ Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р Т‘Р С‘Р В°Р С–Р Р…Р С•РЎРѓРЎвЂљР С‘Р С”Р В° Р СР ВµРЎвЂ¦Р В°Р Р…Р С‘Р С”Р В°',
            short_label='Р вЂќР С‘Р В°Р С–Р Р…Р С•РЎРѓРЎвЂљР С‘Р С”Р В°',
            equipment_type=truck.equipment_type,
            show_for_truck_driver=False,
            show_for_mechanic=True,
        )

        forbidden_response = self.client.post(
            reverse('driver_downtime_action'),
            {'reason_id': forbidden_reason.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        allowed_response = self.client.post(
            reverse('driver_downtime_action'),
            {'reason_id': allowed_reason.id},
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(forbidden_response.status_code, 200)
        self.assertContains(forbidden_response, 'Р СџРЎР‚Р С‘РЎвЂЎР С‘Р Р…Р В° Р С—РЎР‚Р С•РЎРѓРЎвЂљР С•РЎРЏ Р Р…Р Вµ Р Р…Р В°Р в„–Р Т‘Р ВµР Р…Р В°')
        self.assertEqual(DowntimeEvent.objects.count(), 1)
        event = DowntimeEvent.objects.get()
        self.assertEqual(event.reason, allowed_reason)
        self.assertEqual(event.equipment, truck)
        self.assertEqual(event.employee, self.employee)

    def test_driver_downtime_reference_change_is_visible_after_server_refresh(self):
        self.create_registered_driver_shift()
        DowntimeReason.objects.all().update(show_for_truck_driver=False)
        DowntimeReason.objects.create(
            name='Р СћР ВµРЎРѓРЎвЂљ Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р С•Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ Р С—Р С•Р С–РЎР‚РЎС“Р В·Р С”Р С‘',
            short_label='Р СџР С•Р С–РЎР‚РЎС“Р В·Р С”Р В°',
            show_for_truck_driver=True,
        )

        initial_response = self.client.get('/driver/?tab=downtimes', HTTP_HOST='localhost')
        new_reason = DowntimeReason.objects.create(
            name='Р СћР ВµРЎРѓРЎвЂљ Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р С”Р В»Р С‘Р СР В°РЎвЂљР С‘РЎвЂЎР ВµРЎРѓР С”Р С‘Р Вµ РЎС“РЎРѓР В»Р С•Р Р†Р С‘РЎРЏ',
            short_label='Р СџР С•Р С–Р С•Р Т‘Р В°',
            show_for_truck_driver=True,
            sort_order=30,
        )
        refreshed_response = self.client.get(
            '/driver/?tab=downtimes&_driver_refresh=1',
            HTTP_HOST='localhost',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertContains(initial_response, 'Р СџР С•Р С–РЎР‚РЎС“Р В·Р С”Р В°')
        self.assertNotContains(initial_response, 'Р СџР С•Р С–Р С•Р Т‘Р В°')
        self.assertContains(refreshed_response, f'data-driver-downtime-reason-id="{new_reason.id}"')
        self.assertContains(refreshed_response, 'Р СџР С•Р С–Р С•Р Т‘Р В°')
        self.assertContains(refreshed_response, 'window.applyOperationalStateRefresh')
        self.assertTrue(
            OperationalStateEvent.objects.filter(
                event_type='reference_changed',
                object_type='DowntimeReason',
                object_id=str(new_reason.id),
            ).exists()
        )

    def test_trip_becomes_carryover_when_loading_and_unloading_shift_types_differ(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Р вЂР В»Р С•Р С” 1')
        section = DormitorySection.objects.create(block=block, name='Р С’')
        excavator_operator = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р СР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ')
        loading_shift = EmployeeShift.objects.create(
            employee=excavator_operator,
            shift_type='day',
            equipment=excavator,
            opened_at=timezone.now(),
            closed_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        self.client.post(
            '/driver/registration/',
            {'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        self.client.post(
            '/driver/shift/',
            {'shift_type': 'night', 'truck': truck.id, 'start_fuel': '100', 'start_mileage': '2500', 'start_engine_hours': '700'},
            follow=True,
            HTTP_HOST='localhost',
        )
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            excavator_operator=excavator_operator,
            loading_shift=loading_shift,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3='57.00',
        )

        self.client.post(
            f'/driver/trip/{trip.id}/complete/',
            {'client_action_id': 'driver-unload-carryover-1'},
            follow=True,
            HTTP_HOST='localhost',
        )
        trip.refresh_from_db()

        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertTrue(trip.is_carryover)
        self.assertEqual(trip.unloading_shift.shift_type, 'night')

    def test_trip_volume_and_tonnage_are_calculated_from_capacity_rule_and_density(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck_model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='Р вЂР вЂўР вЂєР С’Р вЂ” РЎвЂљР ВµРЎРѓРЎвЂљ',
            body_volume_m3='40.00',
        )
        truck = Equipment.objects.create(equipment_type=truck_type, model=truck_model, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°', density='2.50')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        excavator_role = Role.objects.create(code='excavator_operator', name='Р СљР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ РЎРЊР С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚Р В°')
        excavator_operator = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р СР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ')
        EmployeeAccess.objects.create(employee=excavator_operator, role=excavator_role, access_code='3000')
        EmployeeShift.objects.create(
            employee=excavator_operator,
            shift_type='day',
            equipment=excavator,
            opened_at=timezone.now(),
        )
        assignment = HaulAssignment.objects.create(truck=truck, excavator=excavator, status=AssignmentStatus.ACCEPTED)
        TruckCapacityRule.objects.create(equipment_model=truck_model, rock_type=rock, volume_m3='38.00')

        operator_client = self.client_class(HTTP_HOST='localhost')
        operator_client.post('/', {'access_code': '3000'}, follow=True, HTTP_HOST='localhost')
        operator_client.post(
            '/excavator/work/',
            {'assignment': assignment.id, 'rock_type': rock.id, 'dump_point': dump_point.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        trip = Trip.objects.get()

        self.assertEqual(trip.volume_m3, Decimal('38.00'))
        self.assertEqual(trip.tonnage, Decimal('95.00'))

    def test_dispatcher_can_see_volume_report(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            volume_m3='57.00',
            completed_at=timezone.now(),
        )

        response = self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚РЎРѓР С”Р С‘Р в„– Р С—РЎС“Р В»РЎРЉРЎвЂљ')

        report_response = self.client.get('/reports/volume/', HTTP_HOST='localhost')
        self.assertEqual(report_response.status_code, 200)
        self.assertContains(report_response, 'Р С›РЎвЂљРЎвЂЎР ВµРЎвЂљ Р С—Р С• Р С•Р В±РЎР‰Р ВµР СР В°Р С')
        self.assertContains(report_response, '57')

        export_response = self.client.get('/reports/volume/export/', HTTP_HOST='localhost')
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

    def test_dispatcher_can_open_mining_volumes_dashboard_and_export_it(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            loading_horizon='245',
            loading_block='7',
            status=TripStatus.COMPLETED,
            planned_volume_m3='100.00',
            volume_m3='57.00',
            tonnage='142.50',
            completed_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/mining-volumes/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р вЂњР С•РЎР‚Р Р…РЎвЂ№Р Вµ Р С•Р В±РЎР‰Р ВµР СРЎвЂ№')
        self.assertContains(response, '57')
        self.assertContains(response, 'Р С™Р С™Р вЂќ')

        export_response = self.client.get('/dispatcher/mining-volumes/export/', HTTP_HOST='localhost')
        workbook = load_workbook(BytesIO(export_response.content))
        values = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('Р вЂњР С•РЎР‚Р Р…РЎвЂ№Р Вµ Р С•Р В±РЎР‰Р ВµР СРЎвЂ№ Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚Р В°', values)
        self.assertIn('Р С™Р С™Р вЂќ', values)

    def test_dispatcher_can_open_transport_dashboard_and_export_it(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='15')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        driver = Employee.objects.create(full_name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р В°Р Р†РЎвЂљР С•РЎвЂљРЎР‚Р В°Р Р…РЎРѓР С—Р С•РЎР‚РЎвЂљР В° MVP')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5010')
        selected_date = timezone.localdate()
        opened_at = timezone.make_aware(datetime.combine(selected_date, datetime.min.time().replace(hour=8)))
        closed_at = timezone.make_aware(datetime.combine(selected_date, datetime.min.time().replace(hour=12)))
        EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=truck,
            start_fuel=Decimal('100.00'),
            end_fuel=Decimal('80.00'),
            start_mileage=Decimal('2500.00'),
            end_mileage=Decimal('2600.00'),
            start_engine_hours=Decimal('700.00'),
            end_engine_hours=Decimal('712.00'),
            opened_at=opened_at,
            closed_at=closed_at,
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            volume_m3='57.00',
            tonnage='142.50',
            completed_at=closed_at,
        )

        self.client.post('/', {'access_code': '5010'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/transport/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р С’Р Р†РЎвЂљР С•РЎвЂљРЎР‚Р В°Р Р…РЎРѓР С—Р С•РЎР‚РЎвЂљ')
        self.assertContains(response, '15')
        self.assertContains(response, 'Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р В°Р Р†РЎвЂљР С•РЎвЂљРЎР‚Р В°Р Р…РЎРѓР С—Р С•РЎР‚РЎвЂљР В° MVP')
        self.assertContains(response, '20,0')

        export_response = self.client.get('/dispatcher/transport/export/', HTTP_HOST='localhost')
        workbook = load_workbook(BytesIO(export_response.content))
        values = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('Р С’Р Р†РЎвЂљР С•РЎвЂљРЎР‚Р В°Р Р…РЎРѓР С—Р С•РЎР‚РЎвЂљ Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚Р В°', values)
        self.assertIn('15', values)
        self.assertIn('Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р В°Р Р†РЎвЂљР С•РЎвЂљРЎР‚Р В°Р Р…РЎРѓР С—Р С•РЎР‚РЎвЂљР В° MVP', values)

    def test_dispatcher_can_open_downtimes_dashboard_and_export_it(self):
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='6')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='34')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5020')
        critical_reason = DowntimeReason.objects.create(
            name='Р С’Р Р†Р В°РЎР‚Р С‘Р в„–Р Р…Р В°РЎРЏ Р С—Р С•Р В»Р С•Р СР С”Р В°',
            equipment_type=excavator_type,
            is_critical=True,
        )
        waiting_reason = DowntimeReason.objects.get(name='Р С›Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘')
        waiting_reason.equipment_type = truck_type
        waiting_reason.save(update_fields=['equipment_type'])
        selected_date = timezone.localdate()
        started_at = timezone.make_aware(datetime.combine(selected_date, datetime.min.time().replace(hour=9)))
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=dispatcher,
            reason=critical_reason,
            started_at=started_at,
            comment='Р вЂќР ВµР СР С• Р Т‘Р С‘Р В°Р С–Р Р…Р С•РЎРѓРЎвЂљР С‘Р С”Р В°',
        )
        DowntimeEvent.objects.create(
            equipment=truck,
            employee=dispatcher,
            reason=waiting_reason,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=45),
        )

        self.client.post('/', {'access_code': '5020'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/downtimes/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р СџРЎР‚Р С•РЎРѓРЎвЂљР С•Р С‘ Р С‘ Р С•РЎвЂљР С”Р В»Р С•Р Р…Р ВµР Р…Р С‘РЎРЏ')
        self.assertContains(response, 'Р С’Р Р†Р В°РЎР‚Р С‘Р в„–Р Р…Р В°РЎРЏ Р С—Р С•Р В»Р С•Р СР С”Р В°')
        self.assertContains(response, '6')
        self.assertContains(response, '34')

        export_response = self.client.get('/dispatcher/downtimes/export/', HTTP_HOST='localhost')
        workbook = load_workbook(BytesIO(export_response.content))
        values = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('Р СџРЎР‚Р С•РЎРѓРЎвЂљР С•Р С‘ Р С‘ Р С•РЎвЂљР С”Р В»Р С•Р Р…Р ВµР Р…Р С‘РЎРЏ Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚Р В°', values)
        self.assertIn('Р С’Р Р†Р В°РЎР‚Р С‘Р в„–Р Р…Р В°РЎРЏ Р С—Р С•Р В»Р С•Р СР С”Р В°', values)
        self.assertIn('Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚ 6', values)

    def test_dispatcher_can_open_shift_log_and_export_it(self):
        excavator_type = EquipmentType.objects.create(name='Excavator')
        truck_type = EquipmentType.objects.create(name='Truck')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        rock = RockType.objects.create(name='Oxide')
        dump_point = DumpPoint.objects.create(name='KKD')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Dispatcher')
        dispatcher = Employee.objects.create(full_name='Shift log dispatcher')
        driver = Employee.objects.create(full_name='Shift log driver')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5030')
        event_time = timezone.now()
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            driver=driver,
            rock_type=rock,
            dump_point=dump_point,
            loading_horizon='245',
            loading_block='7',
            status=TripStatus.COMPLETED,
            volume_m3='57.00',
            tonnage='142.50',
            completed_at=event_time,
        )
        DispatcherActionLog.objects.create(
            actor=dispatcher,
            action_type=DispatcherActionType.COMPLETE_TRIP,
            trip=trip,
            target_summary='Trip 10 completed manually',
            reason='Shift reconciliation demo',
        )
        reason = DowntimeReason.objects.create(name='Shift log diagnostics', equipment_type=excavator_type, is_critical=True)
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=dispatcher,
            reason=reason,
            started_at=event_time,
            comment='Shift timeline check',
        )

        self.client.post('/', {'access_code': '5030'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/shift-log/', HTTP_HOST='localhost')
        export_response = self.client.get('/dispatcher/shift-log/export/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Trip 10 completed manually')
        self.assertContains(response, 'Shift log diagnostics')
        self.assertContains(response, 'Truck 10')
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        workbook = load_workbook(BytesIO(export_response.content))
        values = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]
        self.assertIn('Р вЂ“РЎС“РЎР‚Р Р…Р В°Р В» РЎРѓР СР ВµР Р…РЎвЂ№ Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚Р В°', values)
        self.assertIn('Trip 10 completed manually', values)
        self.assertIn('Shift log diagnostics', values)

    def test_dispatcher_can_open_reports_center_and_export_it(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='15')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚ Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљР С•Р Р†')
        driver = Employee.objects.create(full_name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљР С•Р Р†')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5040')
        selected_date = timezone.localdate()
        opened_at = timezone.make_aware(datetime.combine(selected_date, datetime.min.time().replace(hour=8)))
        closed_at = timezone.make_aware(datetime.combine(selected_date, datetime.min.time().replace(hour=12)))
        EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=truck,
            start_fuel=Decimal('100.00'),
            end_fuel=Decimal('80.00'),
            start_mileage=Decimal('2500.00'),
            end_mileage=Decimal('2600.00'),
            start_engine_hours=Decimal('700.00'),
            end_engine_hours=Decimal('712.00'),
            opened_at=opened_at,
            closed_at=closed_at,
        )
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            driver=driver,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            planned_volume_m3='100.00',
            volume_m3='57.00',
            tonnage='142.50',
            completed_at=closed_at,
        )
        DispatcherActionLog.objects.create(
            actor=dispatcher,
            action_type=DispatcherActionType.COMPLETE_TRIP,
            trip=trip,
            target_summary='Р С›РЎвЂљРЎвЂЎР ВµРЎвЂљР Р…РЎвЂ№Р в„– РЎР‚Р ВµР в„–РЎРѓ Р В·Р В°Р С”РЎР‚РЎвЂ№РЎвЂљ',
        )
        ReportTemplate.objects.create(
            name='Р вЂќР ВµР СР С• РЎв‚¬Р В°Р В±Р В»Р С•Р Р… Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚Р В°',
            report_type=ReportType.SHIFT_VOLUME,
            columns=['truck', 'volume_m3'],
            created_by=dispatcher,
        )

        self.client.post('/', {'access_code': '5040'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/reports/', HTTP_HOST='localhost')
        export_response = self.client.get('/dispatcher/reports/export/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р С›РЎвЂљРЎвЂЎР ВµРЎвЂљРЎвЂ№ Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚Р В°')
        self.assertContains(response, 'Р РЋР СР ВµР Р…Р Р…РЎвЂ№Р Вµ Р С•Р В±РЎР‰Р ВµР СРЎвЂ№')
        self.assertContains(response, 'Р С’Р Р†РЎвЂљР С•РЎвЂљРЎР‚Р В°Р Р…РЎРѓР С—Р С•РЎР‚РЎвЂљ')
        self.assertContains(response, 'Р С™Р С•Р Р…РЎРѓРЎвЂљРЎР‚РЎС“Р С”РЎвЂљР С•РЎР‚')
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        workbook = load_workbook(BytesIO(export_response.content))
        values = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]
        self.assertIn('Р С›РЎвЂљРЎвЂЎР ВµРЎвЂљРЎвЂ№ Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚РЎРѓР С”Р С•Р в„–', values)
        self.assertIn('Р РЋР СР ВµР Р…Р Р…РЎвЂ№Р Вµ Р С•Р В±РЎР‰Р ВµР СРЎвЂ№', values)
        self.assertIn('Р вЂќР ВµР СР С• РЎв‚¬Р В°Р В±Р В»Р С•Р Р… Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚Р В°', values)

    def test_dispatcher_can_open_management_showcase_and_export_it(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='15')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚ Р Р†Р С‘РЎвЂљРЎР‚Р С‘Р Р…РЎвЂ№')
        driver = Employee.objects.create(full_name='Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р Р†Р С‘РЎвЂљРЎР‚Р С‘Р Р…РЎвЂ№')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5050')
        selected_date = timezone.localdate()
        opened_at = timezone.make_aware(datetime.combine(selected_date, datetime.min.time().replace(hour=8)))
        closed_at = timezone.make_aware(datetime.combine(selected_date, datetime.min.time().replace(hour=12)))
        EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=truck,
            start_fuel=Decimal('100.00'),
            end_fuel=None,
            start_mileage=Decimal('2500.00'),
            end_mileage=None,
            start_engine_hours=Decimal('700.00'),
            end_engine_hours=None,
            opened_at=opened_at,
        )
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            driver=driver,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            planned_volume_m3='100.00',
            volume_m3='96.00',
            tonnage='240.00',
            loading_horizon='245',
            loading_block='7',
            completed_at=closed_at,
        )
        DispatcherActionLog.objects.create(
            actor=dispatcher,
            action_type=DispatcherActionType.COMPLETE_TRIP,
            trip=trip,
            target_summary='Р В Р ВµР в„–РЎРѓ Р Р†Р С‘РЎвЂљРЎР‚Р С‘Р Р…РЎвЂ№ Р В·Р В°Р С”РЎР‚РЎвЂ№РЎвЂљ',
        )
        reason = DowntimeReason.objects.create(name='Р вЂќР С‘Р В°Р С–Р Р…Р С•РЎРѓРЎвЂљР С‘Р С”Р В° Р Р†Р С‘РЎвЂљРЎР‚Р С‘Р Р…РЎвЂ№', equipment_type=truck_type, is_critical=True)
        DowntimeEvent.objects.create(
            equipment=truck,
            employee=dispatcher,
            reason=reason,
            started_at=opened_at,
            comment='Р СџРЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р В° Р Р†Р С‘РЎвЂљРЎР‚Р С‘Р Р…РЎвЂ№',
        )

        self.client.post('/', {'access_code': '5050'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/management/', HTTP_HOST='localhost')
        export_response = self.client.get('/dispatcher/management/export/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р вЂ™Р С‘РЎвЂљРЎР‚Р С‘Р Р…Р В° Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚РЎРѓР С”Р С•Р в„–')
        self.assertContains(response, 'Р ВРЎвЂљР С•Р С– РЎРѓР СР ВµР Р…РЎвЂ№')
        self.assertContains(response, 'Р С™Р С•Р СР С—Р В»Р ВµР С”РЎРѓРЎвЂ№')
        self.assertContains(response, 'Р С™Р С™Р вЂќ')
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        workbook = load_workbook(BytesIO(export_response.content))
        values = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]
        self.assertIn('Р вЂ™Р С‘РЎвЂљРЎР‚Р С‘Р Р…Р В° Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚РЎРѓР С”Р С•Р в„–', values)
        self.assertIn('Р ВРЎвЂљР С•Р С– РЎРѓР СР ВµР Р…РЎвЂ№', values)
        self.assertIn('Р С™-1', values)
        self.assertIn('Р С™Р С™Р вЂќ', values)

    def test_dispatcher_opens_control_panel_with_active_trips_and_pending_assignments(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        second_truck = Equipment.objects.create(equipment_type=truck_type, garage_number='11')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3='57.00',
        )
        HaulAssignment.objects.create(
            truck=second_truck,
            excavator=excavator,
            status=AssignmentStatus.PENDING,
        )
        HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        response = self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚РЎРѓР С”Р С‘Р в„– Р С—РЎС“Р В»РЎРЉРЎвЂљ')
        self.assertContains(response, 'Р С’Р С”РЎвЂљР С‘Р Р†Р Р…РЎвЂ№Р Вµ РЎР‚Р ВµР в„–РЎРѓРЎвЂ№')
        self.assertContains(response, 'Р СњР В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘РЎРЏ Р В¶Р Т‘РЎС“РЎвЂљ Р С—Р С•Р Т‘РЎвЂљР Р†Р ВµРЎР‚Р В¶Р Т‘Р ВµР Р…Р С‘РЎРЏ')
        self.assertContains(response, 'Р СџРЎР‚Р С‘Р Р…РЎРЏРЎвЂљРЎвЂ№Р Вµ Р Р…Р В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘РЎРЏ Р Р† РЎР‚Р В°Р В±Р С•РЎвЂљР Вµ')
        self.assertContains(response, '57')
        self.assertContains(response, 'Р С›РЎвЂљР С”РЎР‚РЎвЂ№РЎвЂљРЎРЉ Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљ Р С—Р С• Р С•Р В±РЎР‰Р ВµР СР В°Р С')
        self.assertContains(response, 'Р РЋРЎС“РЎвЂљР С•РЎвЂЎР Р…РЎвЂ№Р в„– Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљ Р В·Р В°Р С”Р В°Р В·РЎвЂЎР С‘Р С”РЎС“')

    def test_dispatcher_control_panel_can_filter_by_truck(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        second_truck = Equipment.objects.create(equipment_type=truck_type, garage_number='11')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3='11.00',
        )
        Trip.objects.create(
            excavator=excavator,
            truck=second_truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3='22.00',
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get(f'/dispatcher/control/?truck={truck.id}', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '11')
        self.assertNotContains(response, '22,00')

    def test_dispatcher_control_panel_can_hide_accepted_assignments(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/control/?show_accepted_assignments=0', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р СџРЎР‚Р С‘Р Р…РЎРЏРЎвЂљРЎвЂ№РЎвЂ¦ Р Р…Р В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘Р в„– Р Р† РЎР‚Р В°Р В±Р С•РЎвЂљР Вµ РЎРѓР ВµР в„–РЎвЂЎР В°РЎРѓ Р Р…Р ВµРЎвЂљ.')

    def test_dispatcher_cannot_change_complexes_without_open_shift(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        access = EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        ExcavatorPlacement.objects.create(excavator=excavator, zone=ExcavatorPlacement.Zone.INACTIVE)
        session = self.client.session
        session['employee_access_id'] = access.id
        session.save()

        response = self.client.post(
            '/dispatcher/control/truck/assign/',
            data=json.dumps({
                'action': 'assign',
                'truck_id': truck.id,
                'excavator_id': excavator.id,
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(HaulAssignment.objects.filter(truck=truck, excavator=excavator).exists())
        placement = ExcavatorPlacement.objects.get(excavator=excavator)
        self.assertEqual(placement.zone, ExcavatorPlacement.Zone.INACTIVE)

    def test_dispatcher_shift_metrics_start_from_open_shift(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        access = EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        old_trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3=Decimal('57.00'),
        )
        old_trip.created_at = timezone.now() - timedelta(hours=2)
        old_trip.save(update_fields=['created_at'])
        EmployeeShift.objects.create(
            employee=dispatcher,
            shift_type='day',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=dispatcher,
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3=Decimal('22.00'),
        )
        session = self.client.session
        session['employee_access_id'] = access.id
        session.save()

        response = self.client.get('/dispatcher/control/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['dispatcher_dashboard']['dispatcher_kpis']['fact_tons'], '22')

    def test_dispatcher_service_action_preserves_current_filters(self):
        truck_type = EquipmentType.objects.create(name='Р В Р Р‹Р В Р’В°Р В РЎВР В РЎвЂўР РЋР С“Р В Р вЂ Р В Р’В°Р В Р’В»')
        excavator_type = EquipmentType.objects.create(name='Р В Р’В­Р В РЎвЂќР РЋР С“Р В РЎвЂќР В Р’В°Р В Р вЂ Р В Р’В°Р РЋРІР‚С™Р В РЎвЂўР РЋР вЂљ')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        second_truck = Equipment.objects.create(equipment_type=truck_type, garage_number='11')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В Р’В Р РЋРЎвЂњР В РўвЂР В Р’В°')
        dump_point = DumpPoint.objects.create(name='Р В РЎв„ўР В РЎв„ўР В РІР‚Сњ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р В РІР‚СњР В РЎвЂР РЋР С“Р В РЎвЂ”Р В Р’ВµР РЋРІР‚С™Р РЋРІР‚РЋР В Р’ВµР РЋР вЂљ')
        dispatcher = Employee.objects.create(full_name='Р В РЎС›Р В Р’ВµР РЋР С“Р РЋРІР‚С™Р В РЎвЂўР В Р вЂ Р РЋРІР‚в„–Р В РІвЂћвЂ“ Р В РўвЂР В РЎвЂР РЋР С“Р В РЎвЂ”Р В Р’ВµР РЋРІР‚С™Р РЋРІР‚РЋР В Р’ВµР РЋР вЂљ')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        target_trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3='11.00',
        )
        Trip.objects.create(
            excavator=excavator,
            truck=second_truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3='22.00',
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/dispatcher/trips/{target_trip.id}/cancel/',
            {
                'reason': 'Р В Р’В¤Р В РЎвЂР В Р’В»Р РЋР Р‰Р РЋРІР‚С™Р РЋР вЂљ Р В РўвЂР В РЎвЂўР В Р’В»Р В Р’В¶Р В Р’ВµР В Р вЂ¦ Р РЋР С“Р В РЎвЂўР РЋРІР‚В¦Р РЋР вЂљР В Р’В°Р В Р вЂ¦Р В РЎвЂР РЋРІР‚С™Р РЋР Р‰Р РЋР С“Р РЋР РЏ',
                'truck': str(truck.id),
                'show_active_trips': '1',
                'show_pending_assignments': '0',
                'show_accepted_assignments': '0',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.redirect_chain)
        self.assertTrue(
            response.redirect_chain[-1][0].endswith(
                f'/dispatcher/control/?truck={truck.id}&show_active_trips=1&show_pending_assignments=0&show_accepted_assignments=0'
            )
        )
        self.assertNotContains(response, '22,00')

    def test_dispatcher_sees_open_shifts_and_can_service_close_driver_shift(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        driver_role, _ = Role.objects.get_or_create(code='driver', defaults={'name': 'Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°'})
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        driver = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeAccess.objects.create(employee=driver, role=driver_role, access_code='2100')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        shift = EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=truck,
            opened_at=timezone.now(),
            opened_by=driver,
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/control/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р СњР ВµР В·Р В°Р С”РЎР‚РЎвЂ№РЎвЂљРЎвЂ№Р Вµ РЎРѓР СР ВµР Р…РЎвЂ№')
        self.assertContains(response, 'Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ')
        self.assertContains(response, 'Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°')
        self.assertContains(response, 'Р вЂ”Р В°Р С”РЎР‚РЎвЂ№РЎвЂљРЎРЉ РЎРѓР В»РЎС“Р В¶Р ВµР В±Р Р…Р С•')

        close_response = self.client.post(
            f'/dispatcher/shifts/{shift.id}/service-close/',
            {'reason': 'Р РЋР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С” Р Р…Р Вµ РЎРѓР СР С•Р С– Р В·Р В°Р С”РЎР‚РЎвЂ№РЎвЂљРЎРЉ РЎРѓР СР ВµР Р…РЎС“'},
            follow=True,
            HTTP_HOST='localhost',
        )
        shift.refresh_from_db()

        self.assertEqual(close_response.status_code, 200)
        self.assertIsNotNone(shift.closed_at)
        self.assertTrue(shift.is_service_closed)
        self.assertEqual(shift.closed_by, dispatcher)
        self.assertContains(close_response, 'Р С›РЎвЂљР С”РЎР‚РЎвЂ№РЎвЂљРЎвЂ№РЎвЂ¦ РЎРѓР СР ВµР Р… РЎРѓР ВµР в„–РЎвЂЎР В°РЎРѓ Р Р…Р ВµРЎвЂљ.')
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.actor, dispatcher)
        self.assertEqual(action.action_type, DispatcherActionType.SERVICE_CLOSE_SHIFT)
        self.assertEqual(action.reason, 'Р РЋР С•РЎвЂљРЎР‚РЎС“Р Т‘Р Р…Р С‘Р С” Р Р…Р Вµ РЎРѓР СР С•Р С– Р В·Р В°Р С”РЎР‚РЎвЂ№РЎвЂљРЎРЉ РЎРѓР СР ВµР Р…РЎС“')

    def test_manager_cannot_service_close_shift(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        manager_role = Role.objects.create(code='manager', name='Р В РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р С•')
        driver_role, _ = Role.objects.get_or_create(code='driver', defaults={'name': 'Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°'})
        manager = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– РЎР‚РЎС“Р С”Р С•Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ')
        driver = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ')
        EmployeeAccess.objects.create(employee=manager, role=manager_role, access_code='6000')
        EmployeeAccess.objects.create(employee=driver, role=driver_role, access_code='2101')
        shift = EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=truck,
            opened_at=timezone.now(),
            opened_by=driver,
        )

        self.client.post('/', {'access_code': '6000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/dispatcher/shifts/{shift.id}/service-close/',
            follow=True,
            HTTP_HOST='localhost',
        )
        shift.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(shift.closed_at)
        self.assertFalse(shift.is_service_closed)

    def test_dispatcher_can_cancel_pending_assignment_from_control_panel(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.PENDING,
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/dispatcher/assignments/{assignment.id}/cancel/',
            {'reason': 'Р СџР ВµРЎР‚Р ВµР Р…Р В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘Р Вµ РЎвЂљР ВµРЎвЂ¦Р Р…Р С‘Р С”Р С‘'},
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
        self.assertIsNotNone(assignment.ended_at)
        self.assertContains(response, 'Р С›Р В¶Р С‘Р Т‘Р В°РЎР‹РЎвЂ°Р С‘РЎвЂ¦ Р С—Р С•Р Т‘РЎвЂљР Р†Р ВµРЎР‚Р В¶Р Т‘Р ВµР Р…Р С‘РЎРЏ Р Р…Р В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘Р в„– Р Р…Р ВµРЎвЂљ.')
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.action_type, DispatcherActionType.CANCEL_ASSIGNMENT)
        self.assertEqual(action.reason, 'Р СџР ВµРЎР‚Р ВµР Р…Р В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘Р Вµ РЎвЂљР ВµРЎвЂ¦Р Р…Р С‘Р С”Р С‘')

    def test_dispatcher_can_cancel_accepted_assignment_from_control_panel(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/dispatcher/assignments/{assignment.id}/cancel/',
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
        self.assertIsNotNone(assignment.ended_at)
        self.assertContains(response, 'Р СџРЎР‚Р С‘Р Р…РЎРЏРЎвЂљРЎвЂ№РЎвЂ¦ Р Р…Р В°Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘Р в„– Р Р† РЎР‚Р В°Р В±Р С•РЎвЂљР Вµ РЎРѓР ВµР в„–РЎвЂЎР В°РЎРѓ Р Р…Р ВµРЎвЂљ.')

    def test_dispatcher_can_service_complete_active_trip_from_control_panel(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        driver_role, _ = Role.objects.get_or_create(code='driver', defaults={'name': 'Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°'})
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        driver = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Р†Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeAccess.objects.create(employee=driver, role=driver_role, access_code='2102')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        unloading_shift = EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            equipment=truck,
            opened_at=timezone.now(),
            opened_by=driver,
        )
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3='11.00',
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/dispatcher/trips/{trip.id}/complete/',
            {'reason': 'Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р С—Р С•РЎвЂљР ВµРЎР‚РЎРЏР В» РЎРѓР Р†РЎРЏР В·РЎРЉ'},
            follow=True,
            HTTP_HOST='localhost',
        )
        trip.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, driver)
        self.assertEqual(trip.unloading_shift, unloading_shift)
        self.assertIsNotNone(trip.completed_at)
        self.assertContains(response, 'Р вЂ™РЎвЂ№Р С—Р С•Р В»Р Р…Р ВµР Р…Р Р…РЎвЂ№РЎвЂ¦ РЎР‚Р ВµР в„–РЎРѓР С•Р Р† Р С—Р С•Р С”Р В° Р Р…Р ВµРЎвЂљ.', count=0)
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.action_type, DispatcherActionType.COMPLETE_TRIP)
        self.assertEqual(action.reason, 'Р вЂ™Р С•Р Т‘Р С‘РЎвЂљР ВµР В»РЎРЉ Р С—Р С•РЎвЂљР ВµРЎР‚РЎРЏР В» РЎРѓР Р†РЎРЏР В·РЎРЉ')

    def test_dispatcher_cannot_service_complete_active_trip_without_open_shift(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3='11.00',
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/dispatcher/trips/{trip.id}/complete/',
            follow=True,
            HTTP_HOST='localhost',
        )
        trip.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(trip.status, TripStatus.ACTIVE)
        self.assertIsNone(trip.completed_at)

    def test_dispatcher_can_cancel_active_trip_from_control_panel(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeShift.objects.create(employee=dispatcher, shift_type='day', opened_at=timezone.now(), opened_by=dispatcher)
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
            volume_m3='11.00',
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/dispatcher/trips/{trip.id}/cancel/',
            {'reason': 'Р С›РЎв‚¬Р С‘Р В±Р С•РЎвЂЎР Р…Р С• РЎРѓР С•Р В·Р Т‘Р В°Р Р…Р Р…РЎвЂ№Р в„– РЎР‚Р ВµР в„–РЎРѓ'},
            follow=True,
            HTTP_HOST='localhost',
        )
        trip.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertContains(response, 'Р С’Р С”РЎвЂљР С‘Р Р†Р Р…РЎвЂ№РЎвЂ¦ РЎР‚Р ВµР в„–РЎРѓР С•Р Р† РЎРѓР ВµР в„–РЎвЂЎР В°РЎРѓ Р Р…Р ВµРЎвЂљ.')
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.action_type, DispatcherActionType.CANCEL_TRIP)
        self.assertEqual(action.reason, 'Р С›РЎв‚¬Р С‘Р В±Р С•РЎвЂЎР Р…Р С• РЎРѓР С•Р В·Р Т‘Р В°Р Р…Р Р…РЎвЂ№Р в„– РЎР‚Р ВµР в„–РЎРѓ')

    def test_volume_report_can_filter_by_loading_shift_type(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        operator = Employee.objects.create(full_name='Р СљР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ')
        day_shift = EmployeeShift.objects.create(employee=operator, shift_type='day', opened_at=timezone.now())
        night_shift = EmployeeShift.objects.create(employee=operator, shift_type='night', opened_at=timezone.now())
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            loading_shift=day_shift,
            status=TripStatus.COMPLETED,
            volume_m3='11.00',
            completed_at=timezone.now(),
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            loading_shift=night_shift,
            status=TripStatus.COMPLETED,
            volume_m3='22.00',
            completed_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/volume/?loading_shift_type=day', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '11')
        self.assertNotContains(response, '22,00')

    def test_volume_report_can_group_by_completed_hour_and_export_excel(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        first_hour = timezone.make_aware(datetime(2026, 6, 17, 10, 15))
        second_hour = timezone.make_aware(datetime(2026, 6, 17, 11, 20))
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            volume_m3='11.00',
            tonnage='25.00',
            completed_at=first_hour,
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            volume_m3='22.00',
            tonnage='50.00',
            completed_at=second_hour,
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/volume/?group_by=completed_hour', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<th>Р В§Р В°РЎРѓ Р Р†РЎвЂ№Р С—Р С•Р В»Р Р…Р ВµР Р…Р С‘РЎРЏ РЎР‚Р ВµР в„–РЎРѓР В°</th>', html=True)
        self.assertContains(response, 'Р С—Р ВµРЎР‚Р Р†Р В°РЎРЏ MVP-Р В·Р В°Р СР ВµР Р…Р В° РЎРѓРЎвЂљР В°РЎР‚Р С•Р в„– РЎвЂћР С•РЎР‚Р СРЎвЂ№')
        self.assertContains(response, '10:00')
        self.assertContains(response, '11:00')
        self.assertContains(response, '<th>Р В Р ВµР в„–РЎРѓРЎвЂ№</th>', html=True)

        export_response = self.client.get('/reports/volume/export/?group_by=completed_hour', HTTP_HOST='localhost')
        workbook = load_workbook(BytesIO(export_response.content))
        values = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]

        self.assertEqual(export_response.status_code, 200)
        self.assertIn('Р В§Р В°РЎРѓ Р Р†РЎвЂ№Р С—Р С•Р В»Р Р…Р ВµР Р…Р С‘РЎРЏ РЎР‚Р ВµР в„–РЎРѓР В°', values)
        self.assertIn('10:00', values)
        self.assertIn('11:00', values)
        self.assertIn('Р ВРЎвЂљР С•Р С–Р С•', values)

    def test_volume_report_uses_selected_report_template_columns(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        template = ReportTemplate.objects.create(
            name='Р С™Р С•РЎР‚Р С•РЎвЂљР С”Р С‘Р в„– Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљ',
            report_type=ReportType.SHIFT_VOLUME,
            columns=['truck', 'planned_volume_m3', 'volume_m3', 'deviation_m3', 'plan_completion_percent'],
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            planned_volume_m3='10.00',
            volume_m3='11.00',
            completed_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get(f'/reports/volume/?template={template.id}', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<th>Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»</th>', html=True)
        self.assertContains(response, '<th>Р СџР В»Р В°Р Р…, Р С3</th>', html=True)
        self.assertContains(response, '<th>Р С›Р В±РЎР‰Р ВµР С, Р С3</th>', html=True)
        self.assertContains(response, '<th>Р С›РЎвЂљР С”Р В»Р С•Р Р…Р ВµР Р…Р С‘Р Вµ, Р С3</th>', html=True)
        self.assertContains(response, '<th>Р вЂ™РЎвЂ№Р С—Р С•Р В»Р Р…Р ВµР Р…Р С‘Р Вµ, %</th>', html=True)
        self.assertNotContains(response, '<th>Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚</th>', html=True)
        self.assertContains(response, '11')
        self.assertContains(response, '1,00')
        self.assertContains(response, '110,00')

        export_response = self.client.get(f'/reports/volume/export/?template={template.id}', HTTP_HOST='localhost')
        workbook = load_workbook(BytesIO(export_response.content))
        values = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]

        self.assertEqual(export_response.status_code, 200)
        self.assertIn('Р С›РЎвЂљР С”Р В»Р С•Р Р…Р ВµР Р…Р С‘Р Вµ, Р С3', values)
        self.assertIn('Р вЂ™РЎвЂ№Р С—Р С•Р В»Р Р…Р ВµР Р…Р С‘Р Вµ, %', values)
        self.assertIn(Decimal('1.00'), values)
        self.assertIn(Decimal('110.00'), values)

    def test_dispatcher_can_create_report_template_in_builder(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        second_truck = Equipment.objects.create(equipment_type=truck_type, garage_number='11')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            volume_m3='11.00',
            completed_at=timezone.now(),
        )
        Trip.objects.create(
            excavator=excavator,
            truck=second_truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            volume_m3='22.00',
            completed_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        builder_response = self.client.get('/reports/templates/', HTTP_HOST='localhost')

        self.assertEqual(builder_response.status_code, 200)
        self.assertContains(builder_response, 'Р С™Р С•Р Р…РЎРѓРЎвЂљРЎР‚РЎС“Р С”РЎвЂљР С•РЎР‚ РЎв‚¬Р В°Р В±Р В»Р С•Р Р…Р С•Р Р† Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљР С•Р Р†')

        create_response = self.client.post(
            '/reports/templates/',
            {
                'name': 'Р РЃР В°Р В±Р В»Р С•Р Р… Р Т‘Р В»РЎРЏ Р В·Р В°Р С”Р В°Р В·РЎвЂЎР С‘Р С”Р В°',
                'columns': ['truck', 'volume_m3'],
                'column_label_truck': 'Р вЂР ВµР В»Р С’Р вЂ”',
                'column_label_volume_m3': 'Р В¤Р В°Р С”РЎвЂљ, Р С3',
                'group_by': 'truck',
                'truck': str(truck.id),
                'is_active': 'on',
            },
            follow=True,
            HTTP_HOST='localhost',
        )
        template = ReportTemplate.objects.get(name='Р РЃР В°Р В±Р В»Р С•Р Р… Р Т‘Р В»РЎРЏ Р В·Р В°Р С”Р В°Р В·РЎвЂЎР С‘Р С”Р В°')

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(template.report_type, ReportType.SHIFT_VOLUME)
        self.assertEqual(template.columns, ['truck', 'volume_m3'])
        self.assertEqual(template.column_labels, {'truck': 'Р вЂР ВµР В»Р С’Р вЂ”', 'volume_m3': 'Р В¤Р В°Р С”РЎвЂљ, Р С3'})
        self.assertEqual(template.filters, {'truck': str(truck.id)})
        self.assertEqual(template.group_by, 'truck')
        self.assertEqual(template.created_by, dispatcher)
        self.assertEqual(template.updated_by, dispatcher)

        report_response = self.client.get(f'/reports/volume/?template={template.id}', HTTP_HOST='localhost')

        self.assertEqual(report_response.status_code, 200)
        self.assertContains(report_response, '<th>Р вЂР ВµР В»Р С’Р вЂ”</th>', html=True)
        self.assertContains(report_response, '<th>Р В¤Р В°Р С”РЎвЂљ, Р С3</th>', html=True)
        self.assertContains(report_response, '<th>Р СћР С•Р Р…Р Р…Р В°Р В¶</th>', html=True)
        self.assertContains(report_response, '<th>Р В Р ВµР в„–РЎРѓРЎвЂ№</th>', html=True)
        self.assertNotContains(report_response, '<th>Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚</th>', html=True)
        self.assertContains(report_response, '11')
        self.assertNotContains(report_response, '22,00')

        export_response = self.client.get(f'/reports/volume/export/?template={template.id}', HTTP_HOST='localhost')
        workbook = load_workbook(BytesIO(export_response.content))
        sheet = workbook.active
        values = [
            cell
            for row in sheet.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]

        self.assertEqual(export_response.status_code, 200)
        self.assertIn('Р С›РЎвЂљРЎвЂЎР ВµРЎвЂљ Р С—Р С• Р С•Р В±РЎР‰Р ВµР СР В°Р С', values)
        self.assertIn('Р РЃР В°Р В±Р В»Р С•Р Р… Р Т‘Р В»РЎРЏ Р В·Р В°Р С”Р В°Р В·РЎвЂЎР С‘Р С”Р В°', values)
        self.assertIn('Р вЂР ВµР В»Р С’Р вЂ”', values)
        self.assertIn('Р В¤Р В°Р С”РЎвЂљ, Р С3', values)
        self.assertIn('Р ВРЎвЂљР С•Р С–Р С•', values)
        self.assertNotIn(Decimal('22.00'), values)

    def test_dispatcher_can_open_customer_daily_report_and_export_it(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р СџР ВµРЎР‚Р Р†Р С‘РЎвЂЎР Р…Р В°РЎРЏ РЎРѓРЎС“Р В»РЎРЉРЎвЂћР С‘Р Т‘Р Р…Р В°РЎРЏ')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        operator = Employee.objects.create(full_name='Р СљР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ')
        report_date = datetime(2026, 6, 17, 10, 0)
        report_datetime = timezone.make_aware(report_date)
        previous_datetime = report_datetime - timedelta(days=1)
        loading_shift = EmployeeShift.objects.create(
            employee=operator,
            shift_type='day',
            equipment=excavator,
            opened_at=report_datetime,
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            loading_shift=loading_shift,
            status=TripStatus.COMPLETED,
            planned_volume_m3='7000.00',
            volume_m3='57.00',
            tonnage='142.50',
            loading_horizon='75',
            loading_block='52',
            transport_distance_km='3.10',
            downtime_text='Р В·Р В°РЎвЂЎР С‘РЎРѓРЎвЂљР С”Р В° Р В·Р В°Р В±Р С•РЎРЏ',
            note='Р С•Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘',
            completed_at=report_datetime,
        )
        previous_shift = EmployeeShift.objects.create(
            employee=operator,
            shift_type='day',
            equipment=excavator,
            opened_at=previous_datetime,
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            loading_shift=previous_shift,
            status=TripStatus.COMPLETED,
            planned_volume_m3='1000.00',
            volume_m3='100.00',
            tonnage='250.00',
            loading_horizon='75',
            loading_block='52',
            transport_distance_km='3.10',
            completed_at=previous_datetime,
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get(
            f'/reports/customer-daily/?date={report_datetime:%Y-%m-%d}',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р РЋРЎС“РЎвЂљР С•РЎвЂЎР Р…РЎвЂ№Р в„– Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљ Р В·Р В°Р С”Р В°Р В·РЎвЂЎР С‘Р С”РЎС“')
        self.assertContains(response, 'Р СџР ВµРЎР‚Р Р†Р С‘РЎвЂЎР Р…Р В°РЎРЏ РЎРѓРЎС“Р В»РЎРЉРЎвЂћР С‘Р Т‘Р Р…Р В°РЎРЏ')
        self.assertContains(response, 'Р С™Р С™Р вЂќ')
        self.assertContains(response, '57')
        self.assertContains(response, '7000')
        self.assertContains(response, 'Р С›РЎвЂљР С”Р В»Р С•Р Р…Р ВµР Р…Р С‘Р Вµ')
        self.assertContains(response, '-6943')
        self.assertContains(response, '75')
        self.assertContains(response, '52')
        self.assertContains(response, '3,10')
        self.assertContains(response, 'Р В·Р В°РЎвЂЎР С‘РЎРѓРЎвЂљР С”Р В° Р В·Р В°Р В±Р С•РЎРЏ')
        self.assertContains(response, 'Р С•Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘')
        self.assertContains(response, 'Р РЋ Р Р…Р В°РЎвЂЎР В°Р В»Р В° Р СР ВµРЎРѓРЎРЏРЎвЂ Р В°')
        self.assertContains(response, '8000')
        self.assertContains(response, '157')
        self.assertContains(response, '-7843')
        self.assertContains(response, 'Р РЋР Р†Р ВµРЎР‚Р С”Р В° РЎРѓР С• РЎРѓРЎвЂљР В°РЎР‚Р С•Р в„– Excel-РЎвЂћР С•РЎР‚Р СР С•Р в„– Р В·Р В°Р С”Р В°Р В·РЎвЂЎР С‘Р С”Р В°')
        self.assertContains(response, 'Р В Р В°Р В±Р С•РЎвЂљР В° Р Р†РЎвЂ№Р ВµР СР С•РЎвЂЎР Р…Р С•Р С–Р С• Р С•Р В±Р С•РЎР‚РЎС“Р Т‘Р С•Р Р†Р В°Р Р…Р С‘РЎРЏ')
        self.assertContains(response, 'Р РЋРЎР‚Р ВµР Т‘Р Р…Р ВµР Р†Р В·Р Р†Р ВµРЎв‚¬Р ВµР Р…Р Р…Р С•Р Вµ Р С—Р В»Р ВµРЎвЂЎР С•')
        self.assertContains(response, 'Р В Р В°РЎРѓРЎвЂЎР ВµРЎвЂљ Р Р†РЎвЂ№Р С—Р С•Р В»Р Р…Р ВµР Р…Р Р…РЎвЂ№РЎвЂ¦ РЎР‚Р В°Р В±Р С•РЎвЂљ Р С—Р С• РЎРѓР В°Р СР С•РЎРѓР Р†Р В°Р В»Р В°Р С')

        export_response = self.client.get(
            f'/reports/customer-daily/export/?date={report_datetime:%Y-%m-%d}',
            HTTP_HOST='localhost',
        )
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        workbook = load_workbook(BytesIO(export_response.content))
        self.assertIn('Р РЋР Р†Р ВµРЎР‚Р С”Р В° РЎРѓ Excel', workbook.sheetnames)
        reconciliation_values = [
            cell.value
            for row in workbook['Р РЋР Р†Р ВµРЎР‚Р С”Р В° РЎРѓ Excel'].iter_rows()
            for cell in row
        ]
        self.assertIn('Р В­РЎвЂљР В°Р В»Р С•Р Р… Р Т‘Р В»РЎРЏ РЎРѓР Р†Р ВµРЎР‚Р С”Р С‘: Р С›РЎвЂљРЎвЂЎР ВµРЎвЂљ_Р С™Р С•Р С—Р С—Р ВµРЎР‚. Р В Р С‘РЎРѓР С•РЎР‚РЎРѓР ВµР В·_Р СљР В°РЎР‚РЎвЂљ.xlsx', reconciliation_values)
        self.assertIn('Р В Р В°Р В±Р С•РЎвЂљР В° Р Р†РЎвЂ№Р ВµР СР С•РЎвЂЎР Р…Р С•Р С–Р С• Р С•Р В±Р С•РЎР‚РЎС“Р Т‘Р С•Р Р†Р В°Р Р…Р С‘РЎРЏ', reconciliation_values)
        self.assertIn('Р РЋРЎР‚Р ВµР Т‘Р Р…Р ВµР Р†Р В·Р Р†Р ВµРЎв‚¬Р ВµР Р…Р Р…Р С•Р Вµ Р С—Р В»Р ВµРЎвЂЎР С•', reconciliation_values)

    def test_seed_demo_scenario_command_creates_ready_demo_data(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck_model = EquipmentModel.objects.create(equipment_type=truck_type, name='Р вЂР вЂўР вЂєР С’Р вЂ” РЎвЂљР ВµРЎРѓРЎвЂљ')
        excavator_model = EquipmentModel.objects.create(equipment_type=excavator_type, name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚ РЎвЂљР ВµРЎРѓРЎвЂљ')
        for garage_number in ('10', '11', '12'):
            Equipment.objects.create(
                equipment_type=truck_type,
                model=truck_model,
                garage_number=garage_number,
                is_active=True,
            )
        Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number='1',
            is_active=True,
        )

        call_command('seed_demo_scenario')

        self.assertTrue(EmployeeAccess.objects.filter(access_code='200000', is_active=True).exists())
        self.assertTrue(EmployeeAccess.objects.filter(access_code='500000', is_active=True).exists())
        self.assertTrue(EmployeeAccess.objects.filter(access_code='600000', is_active=True).exists())
        self.assertTrue(EmployeeAccess.objects.filter(access_code='700000', is_active=True).exists())
        self.assertTrue(DriverPrimaryRegistration.objects.exists())
        self.assertTrue(EmployeeShift.objects.filter(closed_at__isnull=True).exists())
        self.assertTrue(HaulAssignment.objects.filter(status=AssignmentStatus.ACCEPTED).exists())
        self.assertTrue(Trip.objects.filter(status=TripStatus.LOADED_WAITING_UNLOAD).exists())
        self.assertTrue(DowntimeEvent.objects.filter(ended_at__isnull=True).exists())
        self.assertTrue(DowntimeEvent.objects.filter(ended_at__isnull=False).exists())
        self.assertTrue(DowntimeEvent.objects.filter(reason__is_critical=True, ended_at__isnull=True).exists())
        self.assertTrue(DowntimeEvent.objects.filter(reason__is_critical=False, ended_at__isnull=False).exists())
        self.assertTrue(ReportTemplate.objects.filter(name='Р вЂќР ВµР СР С• Р С•РЎвЂљРЎвЂЎР ВµРЎвЂљ Р С—Р С• Р С•Р В±РЎР‰Р ВµР СР В°Р С', is_active=True).exists())
        self.assertTrue(PilotFeedback.objects.filter(title__startswith='Р вЂќР ВµР СР С•-Р В·Р В°Р СР ВµРЎвЂЎР В°Р Р…Р С‘Р Вµ').exists())
        self.assertFalse(Equipment.objects.filter(garage_number__startswith='Р вЂќР вЂўР СљР С›').exists())

    def test_seed_demo_scenario_reuses_reference_trucks_without_demo_trucks(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck_model = EquipmentModel.objects.create(equipment_type=truck_type, name='Р вЂР вЂўР вЂєР С’Р вЂ” РЎвЂљР ВµРЎРѓРЎвЂљ')
        excavator_model = EquipmentModel.objects.create(equipment_type=excavator_type, name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚ РЎвЂљР ВµРЎРѓРЎвЂљ')
        for garage_number in ('10', '11', '12'):
            Equipment.objects.create(
                equipment_type=truck_type,
                model=truck_model,
                garage_number=garage_number,
                is_active=True,
            )
        Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number='1',
            is_active=True,
        )

        call_command('seed_demo_scenario')

        self.assertFalse(Equipment.objects.filter(garage_number__startswith='Р вЂќР вЂўР СљР С›').exists())
        self.assertTrue(HaulAssignment.objects.filter(truck__garage_number='11', status=AssignmentStatus.PENDING).exists())
        self.assertTrue(Trip.objects.filter(truck__garage_number='12', status=TripStatus.COMPLETED).exists())

    def test_mechanic_opens_dashboard_and_creates_downtime_event(self):
        excavator_type = EquipmentType.objects.create(name='Р В Р’В­Р В РЎвЂќР РЋР С“Р В РЎвЂќР В Р’В°Р В Р вЂ Р В Р’В°Р РЋРІР‚С™Р В РЎвЂўР РЋР вЂљ')
        truck_type = EquipmentType.objects.create(name='Р В Р Р‹Р В Р’В°Р В РЎВР В РЎвЂўР РЋР С“Р В Р вЂ Р В Р’В°Р В Р’В»')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        rock = RockType.objects.create(name='Р В Р’В Р РЋРЎвЂњР В РўвЂР В Р’В°')
        dump_point = DumpPoint.objects.create(name='Р В РЎв„ўР В РЎв„ўР В РІР‚Сњ')
        mechanic_role = Role.objects.create(code='mechanic', name='Р В РЎС™Р В Р’ВµР РЋРІР‚В¦Р В Р’В°Р В Р вЂ¦Р В РЎвЂР В РЎвЂќ')
        operator_role = Role.objects.create(code='excavator_operator', name='Р В РЎС™Р В Р’В°Р РЋРІвЂљВ¬Р В РЎвЂР В Р вЂ¦Р В РЎвЂР РЋР С“Р РЋРІР‚С™ Р РЋР РЉР В РЎвЂќР РЋР С“Р В РЎвЂќР В Р’В°Р В Р вЂ Р В Р’В°Р РЋРІР‚С™Р В РЎвЂўР РЋР вЂљР В Р’В°')
        mechanic = Employee.objects.create(full_name='Р В РЎС›Р В Р’ВµР РЋР С“Р РЋРІР‚С™Р В РЎвЂўР В Р вЂ Р РЋРІР‚в„–Р В РІвЂћвЂ“ Р В РЎВР В Р’ВµР РЋРІР‚В¦Р В Р’В°Р В Р вЂ¦Р В РЎвЂР В РЎвЂќ')
        operator = Employee.objects.create(full_name='Р В РЎС›Р В Р’ВµР РЋР С“Р РЋРІР‚С™Р В РЎвЂўР В Р вЂ Р РЋРІР‚в„–Р В РІвЂћвЂ“ Р В РЎВР В Р’В°Р РЋРІвЂљВ¬Р В РЎвЂР В Р вЂ¦Р В РЎвЂР РЋР С“Р РЋРІР‚С™')
        EmployeeAccess.objects.create(employee=mechanic, role=mechanic_role, access_code='7000')
        EmployeeAccess.objects.create(employee=operator, role=operator_role, access_code='3000')
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            excavator_operator=operator,
            status=TripStatus.ACTIVE,
            downtime_text='Р В РЎвЂўР В Р’В¶Р В РЎвЂР В РўвЂР В Р’В°Р В Р вЂ¦Р В РЎвЂР В Р’Вµ Р В РЎВР В Р’ВµР РЋРІР‚В¦Р В Р’В°Р В Р вЂ¦Р В РЎвЂР В РЎвЂќР В Р’В°',
        )
        reason = DowntimeReason.objects.create(name='Р В РІР‚СњР В РЎвЂР В Р’В°Р В РЎвЂ“Р В Р вЂ¦Р В РЎвЂўР РЋР С“Р РЋРІР‚С™Р В РЎвЂР В РЎвЂќР В Р’В°', equipment_type=excavator_type, show_for_mechanic=True)

        login_response = self.client.post('/', {'access_code': '7000'}, follow=True, HTTP_HOST='localhost')
        dashboard_response = self.client.get('/mechanic/downtimes/', HTTP_HOST='localhost')
        create_response = self.client.post(
            f'/mechanic/downtimes/create/{trip.id}/',
            {
                f'trip_{trip.id}-reason': str(reason.id),
                f'trip_{trip.id}-comment': 'Р В РІР‚в„ўР РЋРІР‚в„–Р В Р’ВµР РЋРІР‚В¦Р В Р’В°Р В Р’В»Р В РЎвЂ Р В Р вЂ¦Р В Р’В° Р В РўвЂР В РЎвЂР В Р’В°Р В РЎвЂ“Р В Р вЂ¦Р В РЎвЂўР РЋР С“Р РЋРІР‚С™Р В РЎвЂР В РЎвЂќР РЋРЎвЂњ',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertRedirects(login_response, '/mechanic/downtimes/', target_status_code=200)
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertContains(dashboard_response, f'/mechanic/downtimes/create/{trip.id}/')
        self.assertEqual(create_response.status_code, 200)
        self.assertTrue(DowntimeEvent.objects.filter(equipment=excavator, ended_at__isnull=True).exists())
        event = DowntimeEvent.objects.get(equipment=excavator, ended_at__isnull=True)
        self.assertEqual(event.reason, reason)
        self.assertEqual(event.employee, mechanic)

    def test_mechanic_cannot_open_second_active_downtime_for_same_equipment(self):
        excavator_type = EquipmentType.objects.create(name='Excavator')
        truck_type = EquipmentType.objects.create(name='Truck')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        rock = RockType.objects.create(name='Rock')
        dump_point = DumpPoint.objects.create(name='Dump')
        mechanic_role = Role.objects.create(code='mechanic', name='Mechanic')
        operator_role = Role.objects.create(code='excavator_operator', name='Operator')
        mechanic = Employee.objects.create(full_name='Mechanic MVP')
        operator = Employee.objects.create(full_name='Operator MVP')
        EmployeeAccess.objects.create(employee=mechanic, role=mechanic_role, access_code='7000')
        reason = DowntimeReason.objects.create(name='Diagnostics', equipment_type=excavator_type, show_for_mechanic=True)
        existing_reason = DowntimeReason.objects.create(name='Engine', equipment_type=excavator_type)
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=existing_reason,
            started_at=timezone.now() - timedelta(minutes=20),
        )
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            excavator_operator=operator,
            status=TripStatus.ACTIVE,
            downtime_text='waiting mechanic',
        )

        self.client.post('/', {'access_code': '7000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/mechanic/downtimes/create/{trip.id}/',
            {
                f'trip_{trip.id}-reason': str(reason.id),
                f'trip_{trip.id}-comment': 'second event',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(DowntimeEvent.objects.filter(equipment=excavator, ended_at__isnull=True).count(), 1)

    def test_mechanic_can_close_open_downtime_event(self):
        excavator_type = EquipmentType.objects.create(name='Р В Р’В­Р В РЎвЂќР РЋР С“Р В РЎвЂќР В Р’В°Р В Р вЂ Р В Р’В°Р РЋРІР‚С™Р В РЎвЂўР РЋР вЂљ')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        mechanic_role = Role.objects.create(code='mechanic', name='Р В РЎС™Р В Р’ВµР РЋРІР‚В¦Р В Р’В°Р В Р вЂ¦Р В РЎвЂР В РЎвЂќ')
        mechanic = Employee.objects.create(full_name='Р В РЎС›Р В Р’ВµР РЋР С“Р РЋРІР‚С™Р В РЎвЂўР В Р вЂ Р РЋРІР‚в„–Р В РІвЂћвЂ“ Р В РЎВР В Р’ВµР РЋРІР‚В¦Р В Р’В°Р В Р вЂ¦Р В РЎвЂР В РЎвЂќ')
        EmployeeAccess.objects.create(employee=mechanic, role=mechanic_role, access_code='7000')
        reason = DowntimeReason.objects.create(name='Р В РЎС›Р В Р’ВµР В РЎвЂќР РЋРЎвЂњР РЋРІР‚В°Р В РЎвЂР В РІвЂћвЂ“ Р РЋР вЂљР В Р’ВµР В РЎВР В РЎвЂўР В Р вЂ¦Р РЋРІР‚С™', equipment_type=excavator_type)
        event = DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=reason,
            started_at=timezone.now() - timedelta(minutes=25),
            comment='Р В РЎСџР РЋР вЂљР В РЎвЂўР В Р вЂ Р В Р’ВµР РЋР вЂљР В РЎвЂќР В Р’В°',
        )

        self.client.post('/', {'access_code': '7000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/mechanic/downtimes/{event.id}/close/',
            follow=True,
            HTTP_HOST='localhost',
        )
        event.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(event.ended_at)
        self.assertFalse(DowntimeEvent.objects.filter(id=event.id, ended_at__isnull=True).exists())

    def test_mechanic_dashboard_shows_recent_closed_downtimes_with_duration(self):
        excavator_type = EquipmentType.objects.create(name='Excavator')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        mechanic_role = Role.objects.create(code='mechanic', name='Mechanic')
        mechanic = Employee.objects.create(full_name='Mechanic MVP')
        EmployeeAccess.objects.create(employee=mechanic, role=mechanic_role, access_code='7000')
        reason = DowntimeReason.objects.create(name='Hydraulics', equipment_type=excavator_type)
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=reason,
            started_at=timezone.now() - timedelta(hours=1, minutes=30),
            ended_at=timezone.now(),
            comment='Closed downtime',
        )

        self.client.post('/', {'access_code': '7000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/mechanic/downtimes/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Hydraulics')
        self.assertContains(response, 'Closed downtime')
        self.assertContains(response, '1 РЎвЂЎ 30 Р СР С‘Р Р…')

    def test_downtime_report_filters_open_events_and_exports_excel(self):
        excavator_type = EquipmentType.objects.create(name='Excavator')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        mechanic_role = Role.objects.create(code='mechanic', name='Mechanic')
        mechanic = Employee.objects.create(full_name='Mechanic MVP')
        EmployeeAccess.objects.create(employee=mechanic, role=mechanic_role, access_code='7000')
        open_reason = DowntimeReason.objects.create(name='Open diagnostics', equipment_type=excavator_type)
        closed_reason = DowntimeReason.objects.create(name='Closed repair', equipment_type=excavator_type)
        started_at = timezone.make_aware(datetime(2026, 6, 17, 9, 0))
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=open_reason,
            started_at=started_at,
            comment='Hidden old open event',
        )
        for index in range(200):
            DowntimeEvent.objects.create(
                equipment=excavator,
                employee=mechanic,
                reason=open_reason,
                started_at=started_at + timedelta(minutes=index + 1),
                comment=f'Visible open event {index}',
            )
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=closed_reason,
            started_at=started_at,
            ended_at=started_at + timedelta(hours=1),
            comment='Closed event',
        )

        self.client.post('/', {'access_code': '7000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/downtimes/?status=open&date_from=2026-06-17&date_to=2026-06-17', HTTP_HOST='localhost')
        export_response = self.client.get('/reports/downtimes/export/?status=open&date_from=2026-06-17&date_to=2026-06-17', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/reports/customer-daily/?date=2026-06-17')
        self.assertContains(response, 'Open diagnostics')
        self.assertContains(response, 'Visible open event 199')
        self.assertContains(response, '200 Р С‘Р В· 201')
        self.assertNotContains(response, 'Hidden old open event')
        self.assertContains(response, '17.06.2026')
        self.assertNotContains(response, 'Closed event')
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        workbook = load_workbook(BytesIO(export_response.content))
        values = [cell.value for row in workbook.active.iter_rows() for cell in row]
        self.assertIn('Р РЋР Р†Р С•Р Т‘Р С”Р В° Р С—Р С• Р Т‘Р В°РЎвЂљР В°Р С', values)
        self.assertIn('17.06.2026', values)
        self.assertIn('Open diagnostics', values)
        self.assertIn('Hidden old open event', values)
        self.assertIn('Visible open event 199', values)
        self.assertNotIn('Closed repair', values)

        critical_reason = DowntimeReason.objects.create(name='Critical engine', equipment_type=excavator_type, is_critical=True)
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=critical_reason,
            started_at=started_at + timedelta(hours=5),
            comment='Critical only',
        )
        critical_response = self.client.get('/reports/downtimes/?critical=yes', HTTP_HOST='localhost')
        self.assertContains(critical_response, 'Critical engine')
        self.assertContains(critical_response, 'Critical only')
        self.assertNotContains(critical_response, 'Visible open event 199')

    def test_downtime_report_shows_unloading_waiting_reconciliation(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        truck_10 = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        truck_25 = Equipment.objects.create(equipment_type=truck_type, garage_number='25')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р вЂќР С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        dispatcher = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р Т‘Р С‘РЎРѓР С—Р ВµРЎвЂљРЎвЂЎР ВµРЎР‚')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        kkd_reason = DowntimeReason.objects.get(name='Р С›Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘ Р С™Р С™Р вЂќ')
        skdr_reason = DowntimeReason.objects.get(name='Р С›Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘ Р РЋР С™Р вЂќР В ')
        kkd_reason.equipment_type = truck_type
        skdr_reason.equipment_type = truck_type
        kkd_reason.save(update_fields=['equipment_type'])
        skdr_reason.save(update_fields=['equipment_type'])
        started_at = timezone.make_aware(datetime(2026, 6, 17, 9, 0))
        DowntimeEvent.objects.create(
            equipment=truck_10,
            employee=dispatcher,
            reason=kkd_reason,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=45),
            comment='Р С›РЎвЂЎР ВµРЎР‚Р ВµР Т‘РЎРЉ Р Р…Р В° Р С™Р С™Р вЂќ',
        )
        DowntimeEvent.objects.create(
            equipment=truck_25,
            employee=dispatcher,
            reason=skdr_reason,
            started_at=started_at + timedelta(hours=1),
            ended_at=started_at + timedelta(hours=1, minutes=30),
            comment='Р С›РЎвЂЎР ВµРЎР‚Р ВµР Т‘РЎРЉ Р Р…Р В° Р РЋР С™Р вЂќР В ',
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/downtimes/?date_from=2026-06-17&date_to=2026-06-17', HTTP_HOST='localhost')
        export_response = self.client.get('/reports/downtimes/export/?date_from=2026-06-17&date_to=2026-06-17', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Р РЋР Р†Р ВµРЎР‚Р С”Р В° Р С›Р В  Р С™Р С™Р вЂќ/Р РЋР С™Р вЂќР В ')
        self.assertContains(response, 'Р СџР С•Р С”РЎР‚РЎвЂ№РЎвЂљР С‘Р Вµ РЎРѓРЎвЂљР В°РЎР‚Р С•Р в„– РЎвЂћР С•РЎР‚Р СРЎвЂ№ Р С›Р В  Р С™Р С™Р вЂќ/Р РЋР С™Р вЂќР В ')
        self.assertContains(response, 'Р С›Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘ Р С™Р С™Р вЂќ')
        self.assertContains(response, 'Р С›Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘ Р РЋР С™Р вЂќР В ')
        self.assertContains(response, '75,00 Р СР С‘Р Р…')
        workbook = load_workbook(BytesIO(export_response.content))
        self.assertIn('Р С›Р В  Р С™Р С™Р вЂќ Р РЋР С™Р вЂќР В ', workbook.sheetnames)
        values = [cell.value for row in workbook['Р С›Р В  Р С™Р С™Р вЂќ Р РЋР С™Р вЂќР В '].iter_rows() for cell in row]
        self.assertIn('Р РЋР Р†Р ВµРЎР‚Р С”Р В° Р С•Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘РЎРЏ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘ Р С™Р С™Р вЂќ/Р РЋР С™Р вЂќР В ', values)
        self.assertIn('Р С›Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘ Р С™Р С™Р вЂќ', values)
        self.assertIn('Р С›Р В¶Р С‘Р Т‘Р В°Р Р…Р С‘Р Вµ РЎР‚Р В°Р В·Р С–РЎР‚РЎС“Р В·Р С”Р С‘ Р РЋР С™Р вЂќР В ', values)
        self.assertIn('Р ВРЎРѓРЎвЂљР С•РЎвЂЎР Р…Р С‘Р С” РЎРѓРЎвЂљР В°РЎР‚Р С•Р в„– РЎвЂћР С•РЎР‚Р СРЎвЂ№: Р С›Р В  Р С™Р С™Р вЂќ Р РЋР С™Р вЂќР В  Р СР В°РЎР‚РЎвЂљ.xlsx', values)

    def test_dispatcher_control_shows_open_mechanic_downtimes(self):
        excavator_type = EquipmentType.objects.create(name='Excavator')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Dispatcher')
        mechanic_role = Role.objects.create(code='mechanic', name='Mechanic')
        dispatcher = Employee.objects.create(full_name='Dispatcher MVP')
        mechanic = Employee.objects.create(full_name='Mechanic MVP')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        reason = DowntimeReason.objects.create(name='Diagnostics', equipment_type=excavator_type)
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=reason,
            started_at=timezone.now() - timedelta(minutes=10),
            comment='Check hydraulics',
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/control/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/mechanic/downtimes/')
        self.assertContains(response, 'Diagnostics')
        self.assertContains(response, 'Check hydraulics')

    def test_customer_daily_report_shows_mechanic_downtimes_and_exports_them(self):
        excavator_type = EquipmentType.objects.create(name='Excavator')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Dispatcher')
        mechanic_role = Role.objects.create(code='mechanic', name='Mechanic')
        dispatcher = Employee.objects.create(full_name='Dispatcher MVP')
        mechanic = Employee.objects.create(full_name='Mechanic MVP')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        reason = DowntimeReason.objects.create(name='Hydraulics', equipment_type=excavator_type)
        started_at = timezone.make_aware(datetime(2026, 6, 17, 9, 0))
        ended_at = timezone.make_aware(datetime(2026, 6, 17, 10, 30))
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=reason,
            started_at=started_at,
            ended_at=ended_at,
            comment='Replace hose',
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/customer-daily/?date=2026-06-17', HTTP_HOST='localhost')
        export_response = self.client.get('/reports/customer-daily/export/?date=2026-06-17', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Hydraulics')
        self.assertContains(response, 'Replace hose')
        workbook = load_workbook(BytesIO(export_response.content))
        sheet = workbook.active
        values = [cell.value for row in sheet.iter_rows() for cell in row]
        self.assertIn('Hydraulics', values)
        self.assertIn('Replace hose', values)

    def test_management_dashboard_shows_open_mechanic_downtimes(self):
        excavator_type = EquipmentType.objects.create(name='Excavator')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        manager_role = Role.objects.create(code='manager', name='Manager')
        mechanic_role = Role.objects.create(code='mechanic', name='Mechanic')
        manager = Employee.objects.create(full_name='Manager MVP')
        mechanic = Employee.objects.create(full_name='Mechanic MVP')
        EmployeeAccess.objects.create(employee=manager, role=manager_role, access_code='6000')
        reason = DowntimeReason.objects.create(name='Engine', equipment_type=excavator_type)
        DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=reason,
            started_at=timezone.now() - timedelta(minutes=40),
            comment='No start',
        )

        self.client.post('/', {'access_code': '6000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/management/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Engine')
        self.assertContains(response, 'No start')
        self.assertContains(response, '/mechanic/downtimes/')

    def test_manager_opens_management_dashboard(self):
        truck_type = EquipmentType.objects.create(name='Р РЋР В°Р СР С•РЎРѓР Р†Р В°Р В»')
        excavator_type = EquipmentType.objects.create(name='Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р В РЎС“Р Т‘Р В°')
        dump_point = DumpPoint.objects.create(name='Р С™Р С™Р вЂќ')
        manager_role = Role.objects.create(code='manager', name='Р В РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р С•')
        manager = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†Р С•Р Вµ РЎР‚РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р С•')
        operator = Employee.objects.create(full_name='Р СћР ВµРЎРѓРЎвЂљР С•Р Р†РЎвЂ№Р в„– Р СР В°РЎв‚¬Р С‘Р Р…Р С‘РЎРѓРЎвЂљ')
        EmployeeAccess.objects.create(employee=manager, role=manager_role, access_code='6000')
        report_datetime = timezone.make_aware(datetime(2026, 6, 17, 10, 0))
        previous_datetime = report_datetime - timedelta(days=1)
        day_shift = EmployeeShift.objects.create(
            employee=operator,
            shift_type='day',
            equipment=excavator,
            opened_at=report_datetime,
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            loading_shift=day_shift,
            planned_volume_m3='60.00',
            volume_m3='57.00',
            tonnage='142.50',
            completed_at=report_datetime,
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            planned_volume_m3='20.00',
            volume_m3='22.00',
            tonnage='55.00',
            completed_at=previous_datetime,
        )

        login_response = self.client.post('/', {'access_code': '6000'}, follow=True, HTTP_HOST='localhost')
        dashboard_response = self.client.get('/reports/management/?date=2026-06-17', HTTP_HOST='localhost')
        export_response = self.client.get('/reports/management/export/?date=2026-06-17', HTTP_HOST='localhost')

        self.assertRedirects(login_response, '/reports/management/', target_status_code=200)
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertContains(dashboard_response, 'Р вЂ™Р С‘РЎвЂљРЎР‚Р С‘Р Р…Р В° РЎР‚РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р В°')
        self.assertContains(dashboard_response, 'Р вЂ™РЎвЂ№Р С–РЎР‚РЎС“Р В·Р С‘РЎвЂљРЎРЉ Р Р†Р С‘РЎвЂљРЎР‚Р С‘Р Р…РЎС“ Р Р† Excel')
        self.assertContains(dashboard_response, 'Р В§Р ВµР С”Р В»Р С‘РЎРѓРЎвЂљ Р С—Р С‘Р В»Р С•РЎвЂљР Р…Р С•Р в„– Р С—РЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р С‘')
        self.assertContains(dashboard_response, '/reports/pilot-checklist/')
        self.assertContains(dashboard_response, 'Р вЂ“РЎС“РЎР‚Р Р…Р В°Р В» Р В·Р В°Р СР ВµРЎвЂЎР В°Р Р…Р С‘Р в„– Р С—Р С‘Р В»Р С•РЎвЂљР В°')
        self.assertContains(dashboard_response, '/reports/pilot-feedback/')
        self.assertContains(dashboard_response, 'Р В¤Р В°Р С”РЎвЂљ Р В·Р В° РЎРѓРЎС“РЎвЂљР С”Р С‘')
        self.assertContains(dashboard_response, 'Р СџР В»Р В°Р Р… Р В·Р В° РЎРѓРЎС“РЎвЂљР С”Р С‘')
        self.assertContains(dashboard_response, 'Р вЂ™РЎвЂ№Р С—Р С•Р В»Р Р…Р ВµР Р…Р С‘Р Вµ Р С—Р В»Р В°Р Р…Р В°')
        self.assertContains(dashboard_response, 'Р С›РЎвЂљР С”Р В»Р С•Р Р…Р ВµР Р…Р С‘Р Вµ Р В·Р В° РЎРѓРЎС“РЎвЂљР С”Р С‘')
        self.assertContains(dashboard_response, 'Р вЂќР ВµР Р…РЎРЉ Р С—РЎР‚Р С•РЎвЂљР С‘Р Р† Р Р…Р С•РЎвЂЎР С‘')
        self.assertContains(dashboard_response, 'Р вЂќР Р…Р ВµР Р†Р Р…Р В°РЎРЏ РЎРѓР СР ВµР Р…Р В°')
        self.assertContains(dashboard_response, 'Р СњР С•РЎвЂЎР Р…Р В°РЎРЏ РЎРѓР СР ВµР Р…Р В°')
        self.assertContains(dashboard_response, 'Р вЂќР С‘Р Р…Р В°Р СР С‘Р С”Р В° Р В·Р В° 7 Р Т‘Р Р…Р ВµР в„–')
        self.assertContains(dashboard_response, 'Р ВРЎвЂљР С•Р С– Р В·Р В° 7 Р Т‘Р Р…Р ВµР в„–')
        self.assertContains(dashboard_response, 'Р СџР В»Р В°Р Р… Р В·Р В° 7 Р Т‘Р Р…Р ВµР в„–')
        self.assertContains(dashboard_response, 'Р вЂ™РЎвЂ№Р С—Р С•Р В»Р Р…Р ВµР Р…Р С‘Р Вµ Р В·Р В° Р Р…Р ВµР Т‘Р ВµР В»РЎР‹')
        self.assertContains(dashboard_response, 'Р вЂєРЎС“РЎвЂЎРЎв‚¬Р С‘Р в„– Р Т‘Р ВµР Р…РЎРЉ')
        self.assertContains(dashboard_response, 'Р РЋР В°Р СР В°РЎРЏ РЎРѓР С‘Р В»РЎРЉР Р…Р В°РЎРЏ Р С—РЎР‚Р С•РЎРѓР В°Р Т‘Р С”Р В°')
        self.assertContains(dashboard_response, '16.06')
        self.assertContains(dashboard_response, '17.06')
        self.assertContains(dashboard_response, '57,00')
        self.assertContains(dashboard_response, '60,00')
        self.assertContains(dashboard_response, '95,0%')
        self.assertContains(dashboard_response, '-3,00')
        self.assertContains(dashboard_response, '22,00')
        self.assertContains(dashboard_response, '110,0%')
        self.assertContains(dashboard_response, 'Р В Р ВµР в„–РЎРѓРЎвЂ№ Р В·Р В° РЎРѓРЎС“РЎвЂљР С”Р С‘')
        self.assertContains(dashboard_response, 'Р В­Р С”РЎРѓР С”Р В°Р Р†Р В°РЎвЂљР С•РЎР‚РЎвЂ№ Р В·Р В° РЎРѓРЎС“РЎвЂљР С”Р С‘')
        self.assertContains(dashboard_response, 'Р СџР С•РЎР‚Р С•Р Т‘РЎвЂ№ Р С‘ Р С–РЎР‚РЎС“Р В·РЎвЂ№ Р В·Р В° РЎРѓРЎС“РЎвЂљР С”Р С‘')
        self.assertContains(dashboard_response, 'Р С›Р В±РЎвЂ°Р В°РЎРЏ Р Р…Р В°Р С”Р С•Р С—Р В»Р ВµР Р…Р Р…Р В°РЎРЏ Р С”Р В°РЎР‚РЎвЂљР С‘Р Р…Р В°')
        self.assertContains(dashboard_response, '79 Р С3')
        self.assertContains(dashboard_response, '80,00 Р С3')
        self.assertContains(dashboard_response, '98,8%')
        self.assertContains(dashboard_response, '57,00')
        self.assertContains(dashboard_response, '142,50')
        workbook = load_workbook(BytesIO(export_response.content))
        self.assertIn('Р РЋР Р†Р С•Р Т‘Р С”Р В°', workbook.sheetnames)
        self.assertIn('Р вЂќР С‘Р Р…Р В°Р СР С‘Р С”Р В° 7 Р Т‘Р Р…Р ВµР в„–', workbook.sheetnames)
        self.assertIn('Р вЂќР ВµР Р…РЎРЉ Р Р…Р С•РЎвЂЎРЎРЉ', workbook.sheetnames)
        values = [cell.value for sheet in workbook.worksheets for row in sheet.iter_rows() for cell in row]
        self.assertIn('Р вЂ™Р С‘РЎвЂљРЎР‚Р С‘Р Р…Р В° РЎР‚РЎС“Р С”Р С•Р Р†Р С•Р Т‘РЎРѓРЎвЂљР Р†Р В°', values)
        self.assertIn('Р В¤Р В°Р С”РЎвЂљ Р В·Р В° 7 Р Т‘Р Р…Р ВµР в„–, Р С3', values)
        self.assertIn('Р вЂ™РЎвЂ№Р С—Р С•Р В»Р Р…Р ВµР Р…Р С‘Р Вµ Р В·Р В° Р Р…Р ВµР Т‘Р ВµР В»РЎР‹, %', values)
        self.assertIn('Р вЂќР Р…Р ВµР Р†Р Р…Р В°РЎРЏ РЎРѓР СР ВµР Р…Р В°', values)
        self.assertIn(Decimal('79.00'), values)
        self.assertIn(98.8, values)

# Create your tests here.

