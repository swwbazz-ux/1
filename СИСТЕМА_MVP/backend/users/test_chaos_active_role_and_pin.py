import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from assignments.views import get_shift_state
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
)
from shifts.models import EmployeeShift, ShiftType
from trips.dispatcher_header import close_dispatcher_shift, get_active_dispatcher_shift
from trips.models import Trip, TripStatus
from users.models import (
    AdminActionLog,
    DriverPrimaryRegistration,
    Employee,
    EmployeeAccess,
    Role,
)
from users.oup_undo import OUP_ACTION_ACCESS_REISSUED


ROLE_HOST_SETTINGS = override_settings(
    ALLOWED_HOSTS=['localhost', '.localhost'],
    ROLE_APP_BASE_DOMAINS=('localhost',),
)


class LiveRoleReadOnlyClientContractTests(TestCase):
    def test_live_role_change_disables_mutations_without_reload(self):
        base_template = (
            Path(settings.BASE_DIR) / 'templates' / 'base.html'
        ).read_text(encoding='utf-8')
        readonly_client = (
            Path(settings.BASE_DIR) / 'static' / 'js' / 'role-readonly.js'
        ).read_text(encoding='utf-8')
        realtime_client = (
            Path(settings.BASE_DIR) / 'static' / 'js' / 'realtime-client.js'
        ).read_text(encoding='utf-8')
        dispatcher_template = (
            Path(settings.BASE_DIR)
            / 'templates'
            / 'trips'
            / 'dispatcher_control.html'
        ).read_text(encoding='utf-8')
        driver_template = (
            Path(settings.BASE_DIR)
            / 'templates'
            / 'users'
            / 'driver_shift.html'
        ).read_text(encoding='utf-8')

        self.assertIn("static 'js/role-readonly.js'", base_template)
        self.assertNotIn(
            'var inactiveRole = document.body.dataset.roleAccessActive === "false"',
            base_template,
        )
        self.assertIn('active-role-state-changed', readonly_client)
        self.assertIn('applyAppRoleReadonlyState', readonly_client)
        self.assertIn('data-role-readonly-blocked', readonly_client)
        self.assertIn('MutationObserver', readonly_client)
        self.assertIn('window.fetch = function', readonly_client)
        self.assertIn('HTMLFormElement.prototype.submit', readonly_client)
        self.assertIn('method === "GET" || method === "HEAD"', readonly_client)
        self.assertIn('window.applyAppRoleReadonlyState', realtime_client)
        self.assertGreaterEqual(
            base_template.count('^/driver/(?:shift(?:/close)?/?)?$'),
            2,
        )
        self.assertIn('dispatcherRoleIsReadonly', dispatcher_template)
        self.assertIn('dispatcherInactiveRoleError', dispatcher_template)
        self.assertIn(
            'window.addEventListener("active-role-state-changed", resetOnInactiveRole)',
            dispatcher_template,
        )
        self.assertIn(
            'window.addEventListener("active-role-state-changed", closeConfirmOnInactiveRole)',
            base_template,
        )
        self.assertIn('pendingConfirmTimer = null;', base_template)
        self.assertIn('window.createDriverRoleHoldGuard', driver_template)
        self.assertGreaterEqual(
            driver_template.count('createDriverRoleHoldGuard({'),
            2,
        )
        self.assertIn('form.dataset.driverShiftConfirmed = "false";', driver_template)
        self.assertIn('delete holdForm.dataset.holdComplete;', driver_template)
        self.assertIn('holdButton.classList.remove("is-holding", "is-pending");', driver_template)
        self.assertNotIn('resetHold();', driver_template)
        self.assertGreaterEqual(
            driver_template.count('driverRoleIsReadonly()'),
            8,
        )

    def test_role_readonly_runtime_contract(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js нужен для исполняемой JS-регрессии read-only.')
        test_path = (
            Path(settings.BASE_DIR)
            / 'static'
            / 'js'
            / 'tests'
            / 'role-readonly-runtime.test.js'
        )

        result = subprocess.run(
            [node, '--test', str(test_path)],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f'JS runtime regression failed.\n{result.stdout}\n{result.stderr}',
        )


class RoleRegressionFixtureMixin:
    driver_host = 'driver.localhost'
    dispatcher_host = 'dispatcher.localhost'
    mining_master_host = 'mining-master.localhost'
    oup_host = 'oup.localhost'

    def create_role(self, code, name):
        role, _created = Role.objects.update_or_create(
            code=code,
            defaults={'name': name, 'is_active': True},
        )
        return role

    def create_employee(self, *, name, phone):
        return Employee.objects.create(
            full_name=name,
            phone=phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )

    def create_access(self, *, employee, role, code):
        return EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=code,
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=timezone.now() - timedelta(days=1),
            is_active=True,
        )

    def login(
        self,
        client,
        *,
        host,
        employee,
        access,
        device_kind='personal',
        follow=False,
    ):
        return client.post(
            reverse('login'),
            {
                'phone': employee.phone,
                'access_code': access.access_code,
                'device_kind': device_kind,
            },
            follow=follow,
            HTTP_HOST=host,
        )

    def create_truck(self, *, garage_number='CHAOS-TRUCK-001'):
        truck_type, _created = EquipmentType.objects.get_or_create(name='Самосвал')
        model, _created = EquipmentModel.objects.get_or_create(
            equipment_type=truck_type,
            name='БелАЗ CHAOS',
            defaults={'fuel_capacity_limit_l': 2000},
        )
        return Equipment.objects.create(
            equipment_type=truck_type,
            model=model,
            garage_number=garage_number,
        )

    def register_driver(self, employee):
        dormitory, _created = Dormitory.objects.get_or_create(number='CHAOS')
        block, _created = DormitoryBlock.objects.get_or_create(
            dormitory=dormitory,
            name='Блок 1',
        )
        section, _created = DormitorySection.objects.get_or_create(
            block=block,
            name='А',
        )
        return DriverPrimaryRegistration.objects.create(
            employee=employee,
            dormitory_section=section,
        )

    def assign_driver(self, *, employee, role, truck):
        return EquipmentAssignment.objects.create(
            employee=employee,
            role=role,
            equipment=truck,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=employee,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

    def create_driver_shift(self, *, employee, truck, readings_complete):
        end_readings = (
            {
                'end_fuel': '900.00',
                'end_mileage': '10100.00',
                'end_engine_hours': '510.00',
            }
            if readings_complete
            else {}
        )
        return EmployeeShift.objects.create(
            employee=employee,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=truck,
            start_fuel='1000.00',
            start_mileage='10000.00',
            start_engine_hours='500.00',
            opened_at=timezone.now() - timedelta(hours=2),
            opened_by=employee,
            **end_readings,
        )


@ROLE_HOST_SETTINGS
class ActiveRoleSwitchRegressionTests(RoleRegressionFixtureMixin, TestCase):
    """QA-CHAOS-P1-001: one active server role across host-only PWA sessions."""

    def setUp(self):
        self.driver_role = self.create_role('driver', 'Водитель самосвала')
        self.dispatcher_role = self.create_role('dispatcher', 'Горный диспетчер')
        self.mining_master_role = self.create_role(
            'mining_master',
            'Горный мастер',
        )
        self.employee = self.create_employee(
            name='Многоролевой сотрудник CHAOS',
            phone='+79990000100',
        )
        self.driver_access = self.create_access(
            employee=self.employee,
            role=self.driver_role,
            code='110001',
        )
        self.dispatcher_access = self.create_access(
            employee=self.employee,
            role=self.dispatcher_role,
            code='110002',
        )
        self.mining_master_access = self.create_access(
            employee=self.employee,
            role=self.mining_master_role,
            code='110003',
        )
        self.truck = self.create_truck()
        self.register_driver(self.employee)
        self.assign_driver(
            employee=self.employee,
            role=self.driver_role,
            truck=self.truck,
        )

    def switch_from_driver_to_dispatcher(self, *, driver_client, dispatcher_client):
        driver_login = self.login(
            driver_client,
            host=self.driver_host,
            employee=self.employee,
            access=self.driver_access,
        )
        self.assertEqual(driver_login.status_code, 302)
        dispatcher_login = self.login(
            dispatcher_client,
            host=self.dispatcher_host,
            employee=self.employee,
            access=self.dispatcher_access,
            device_kind='shared',
        )
        return dispatcher_login

    def assert_dispatcher_switch_blocked(self):
        driver_client = Client()
        dispatcher_client = Client()
        driver_login = self.login(
            driver_client,
            host=self.driver_host,
            employee=self.employee,
            access=self.driver_access,
        )
        self.assertEqual(driver_login.status_code, 302)

        response = self.login(
            dispatcher_client,
            host=self.dispatcher_host,
            employee=self.employee,
            access=self.dispatcher_access,
            device_kind='shared',
            follow=True,
        )

        self.assertNotIn('employee_access_id', dispatcher_client.session)
        self.assertEqual(
            driver_client.session.get('employee_access_id'),
            self.driver_access.id,
        )
        return response

    def test_second_host_switches_role_and_old_host_is_read_only(self):
        driver_client = Client()
        dispatcher_client = Client()

        dispatcher_login = self.switch_from_driver_to_dispatcher(
            driver_client=driver_client,
            dispatcher_client=dispatcher_client,
        )
        old_driver_get = driver_client.get(
            reverse('driver_work'),
            HTTP_HOST=self.driver_host,
        )
        old_driver_post = driver_client.post(
            reverse('driver_work'),
            {
                'start_fuel': '1000.00',
                'start_mileage': '10000.00',
                'start_engine_hours': '500.00',
                'client_action_id': 'chaos-stale-driver-open-shift',
            },
            HTTP_HOST=self.driver_host,
        )

        with self.subTest('host-only sessions stay independent'):
            self.assertEqual(dispatcher_login.status_code, 302)
            self.assertNotEqual(
                driver_client.cookies['sessionid'].value,
                dispatcher_client.cookies['sessionid'].value,
            )
        with self.subTest('old realtime GET remains readable'):
            self.assertEqual(old_driver_get.status_code, 200)
            self.assertTrue(
                'Роль неактивна — доступен только просмотр'
                in old_driver_get.content.decode('utf-8'),
                'Старая PWA не перешла в явно обозначенный read-only.',
            )
        with self.subTest('old working POST is rejected server-side'):
            self.assertIn(old_driver_post.status_code, {403, 409})
            self.assertFalse(
                EmployeeShift.objects.filter(
                    employee=self.employee,
                    workplace_code='driver',
                    closed_at__isnull=True,
                ).exists()
            )

    def test_old_inactive_host_can_logout_safely(self):
        driver_client = Client()
        dispatcher_client = Client()
        dispatcher_login = self.switch_from_driver_to_dispatcher(
            driver_client=driver_client,
            dispatcher_client=dispatcher_client,
        )
        self.assertEqual(dispatcher_login.status_code, 302)

        response = driver_client.get(
            reverse('logout'),
            HTTP_HOST=self.driver_host,
        )

        self.assertRedirects(
            response,
            reverse('login'),
            fetch_redirect_response=False,
        )
        self.assertNotIn('employee_access_id', driver_client.session)
        self.assertEqual(
            dispatcher_client.session.get('employee_access_id'),
            self.dispatcher_access.id,
        )

    def test_driver_switches_to_mining_master_and_only_driver_shift_closes(self):
        driver_client = Client()
        mining_master_client = Client()
        driver_login = self.login(
            driver_client,
            host=self.driver_host,
            employee=self.employee,
            access=self.driver_access,
        )
        self.assertEqual(driver_login.status_code, 302)
        driver_shift = self.create_driver_shift(
            employee=self.employee,
            truck=self.truck,
            readings_complete=True,
        )

        mining_master_login = self.login(
            mining_master_client,
            host=self.mining_master_host,
            employee=self.employee,
            access=self.mining_master_access,
        )

        driver_shift.refresh_from_db()
        self.assertEqual(mining_master_login.status_code, 302)
        self.assertEqual(
            mining_master_client.session.get('employee_access_id'),
            self.mining_master_access.id,
        )
        self.assertIsNotNone(driver_shift.closed_at)
        self.assertEqual(driver_shift.closed_by_id, self.employee.id)
        self.assertEqual(driver_shift.workplace_code, 'driver')
        self.assertFalse(
            EmployeeShift.objects.filter(
                employee=self.employee,
                workplace_code='mining_master',
                closed_at__isnull=True,
            ).exists()
        )

    def test_old_host_realtime_get_reports_inactive_role(self):
        driver_client = Client()
        dispatcher_client = Client()
        dispatcher_login = self.switch_from_driver_to_dispatcher(
            driver_client=driver_client,
            dispatcher_client=dispatcher_client,
        )
        self.assertEqual(dispatcher_login.status_code, 302)

        old_role_response = driver_client.get(
            reverse('operational_state_version'),
            {'include_events': '0'},
            HTTP_HOST=self.driver_host,
        )
        active_role_response = dispatcher_client.get(
            reverse('operational_state_version'),
            {'include_events': '0'},
            HTTP_HOST=self.dispatcher_host,
        )

        old_role_payload = old_role_response.json()
        active_role_payload = active_role_response.json()
        self.assertEqual(old_role_response.status_code, 200)
        self.assertTrue(old_role_payload['authenticated'])
        self.assertFalse(old_role_payload['role_active'])
        self.assertEqual(old_role_payload['active_role_code'], 'dispatcher')
        self.assertEqual(active_role_response.status_code, 200)
        self.assertTrue(active_role_payload['authenticated'])
        self.assertTrue(active_role_payload['role_active'])
        self.assertEqual(active_role_payload['active_role_code'], 'dispatcher')

    def test_switch_is_blocked_by_open_trip(self):
        shift = self.create_driver_shift(
            employee=self.employee,
            truck=self.truck,
            readings_complete=True,
        )
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='CHAOS-EXC-001',
        )
        rock_type = RockType.objects.create(name='CHAOS руда')
        dump_point = DumpPoint.objects.create(name='CHAOS склад')
        trip = Trip.objects.create(
            excavator=excavator,
            truck=self.truck,
            driver=self.employee,
            loading_shift=shift,
            rock_type=rock_type,
            dump_point=dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
        )

        response = self.assert_dispatcher_switch_blocked()

        shift.refresh_from_db()
        trip.refresh_from_db()
        self.assertIsNone(shift.closed_at)
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertTrue(
            'рейс' in response.content.decode('utf-8').lower(),
            'Ответ не перечисляет открытый рейс как блокировщик.',
        )

    def test_switch_is_blocked_by_open_downtime(self):
        shift = self.create_driver_shift(
            employee=self.employee,
            truck=self.truck,
            readings_complete=True,
        )
        reason = DowntimeReason.objects.create(
            name='CHAOS простой самосвала',
            equipment_type=self.truck.equipment_type,
            show_for_truck_driver=True,
        )
        downtime = DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.employee,
            reason=reason,
            started_at=timezone.now() - timedelta(minutes=30),
        )

        response = self.assert_dispatcher_switch_blocked()

        shift.refresh_from_db()
        downtime.refresh_from_db()
        self.assertIsNone(shift.closed_at)
        self.assertIsNone(downtime.ended_at)
        self.assertTrue(
            'простой' in response.content.decode('utf-8').lower(),
            'Ответ не перечисляет открытый простой как блокировщик.',
        )

    def test_switch_is_blocked_by_missing_end_readings(self):
        shift = self.create_driver_shift(
            employee=self.employee,
            truck=self.truck,
            readings_complete=False,
        )

        response = self.assert_dispatcher_switch_blocked()

        shift.refresh_from_db()
        self.assertIsNone(shift.closed_at)
        self.assertIsNone(shift.end_fuel)
        self.assertIsNone(shift.end_mileage)
        self.assertIsNone(shift.end_engine_hours)
        self.assertTrue(
            'показан' in response.content.decode('utf-8').lower(),
            'Ответ не перечисляет незаполненные показания как блокировщик.',
        )

    def test_blocked_first_pin_activation_rolls_back_access_and_session(self):
        self.dispatcher_access.status = EmployeeAccess.Status.NOT_ACTIVATED
        self.dispatcher_access.access_code = '440004'
        self.dispatcher_access.primary_code_issued_at = timezone.now()
        self.dispatcher_access.activated_at = None
        self.dispatcher_access.last_login_at = None
        self.dispatcher_access.save(
            update_fields=[
                'status',
                'access_code',
                'primary_code_issued_at',
                'activated_at',
                'last_login_at',
            ],
        )
        driver_client = Client()
        dispatcher_client = Client()
        self.assertEqual(
            self.login(
                driver_client,
                host=self.driver_host,
                employee=self.employee,
                access=self.driver_access,
            ).status_code,
            302,
        )
        shift = self.create_driver_shift(
            employee=self.employee,
            truck=self.truck,
            readings_complete=False,
        )
        self.driver_access.refresh_from_db()
        driver_generation_before = self.driver_access.last_login_at

        pending_login = self.login(
            dispatcher_client,
            host=self.dispatcher_host,
            employee=self.employee,
            access=self.dispatcher_access,
        )
        self.assertEqual(pending_login.status_code, 302)
        self.assertEqual(
            dispatcher_client.session.get('pending_activation_access_id'),
            self.dispatcher_access.id,
        )

        response = dispatcher_client.post(
            reverse('activate_access'),
            {
                'new_access_code': '482619',
                'confirm_access_code': '482619',
            },
            HTTP_HOST=self.dispatcher_host,
        )

        self.dispatcher_access.refresh_from_db()
        self.driver_access.refresh_from_db()
        shift.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'переключение роли заблокировано',
            response.content.decode('utf-8').lower(),
        )
        self.assertEqual(
            self.dispatcher_access.status,
            EmployeeAccess.Status.NOT_ACTIVATED,
        )
        self.assertEqual(self.dispatcher_access.access_code, '440004')
        self.assertIsNone(self.dispatcher_access.activated_at)
        self.assertIsNone(self.dispatcher_access.last_login_at)
        self.assertEqual(self.driver_access.last_login_at, driver_generation_before)
        self.assertIsNone(shift.closed_at)
        self.assertNotIn('employee_access_id', dispatcher_client.session)
        self.assertEqual(
            dispatcher_client.session.get('pending_activation_access_id'),
            self.dispatcher_access.id,
        )


@ROLE_HOST_SETTINGS
class OupToDispatcherSwitchRegressionTests(
    RoleRegressionFixtureMixin,
    TestCase,
):
    """QA-CHAOS-P1-001: OUP period closes before dispatcher role activates."""

    def setUp(self):
        self.oup_role = self.create_role('oup', 'Специалист ОУП')
        self.dispatcher_role = self.create_role('dispatcher', 'Горный диспетчер')
        self.employee = self.create_employee(
            name='Многоролевой сотрудник ОУП/Диспетчер',
            phone='+79990000150',
        )
        self.oup_access = self.create_access(
            employee=self.employee,
            role=self.oup_role,
            code='115001',
        )
        self.dispatcher_access = self.create_access(
            employee=self.employee,
            role=self.dispatcher_role,
            code='115002',
        )

    def test_oup_to_dispatcher_closes_only_current_oup_period(self):
        oup_client = Client()
        dispatcher_client = Client()
        oup_login = self.login(
            oup_client,
            host=self.oup_host,
            employee=self.employee,
            access=self.oup_access,
        )
        self.assertEqual(oup_login.status_code, 302)

        historical_driver_closed_at = timezone.now() - timedelta(hours=3)
        historical_driver_shift = EmployeeShift.objects.create(
            employee=self.employee,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            opened_at=timezone.now() - timedelta(hours=4),
            closed_at=historical_driver_closed_at,
            opened_by=self.employee,
            closed_by=self.employee,
        )
        oup_shift = EmployeeShift.objects.create(
            employee=self.employee,
            shift_type=ShiftType.DAY,
            workplace_code='oup',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=self.employee,
        )
        unrelated_employee = self.create_employee(
            name='Другой сотрудник с открытой сменой',
            phone='+79990000151',
        )
        unrelated_shift = EmployeeShift.objects.create(
            employee=unrelated_employee,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=unrelated_employee,
        )

        dispatcher_login = self.login(
            dispatcher_client,
            host=self.dispatcher_host,
            employee=self.employee,
            access=self.dispatcher_access,
            device_kind='shared',
        )

        oup_shift.refresh_from_db()
        historical_driver_shift.refresh_from_db()
        unrelated_shift.refresh_from_db()
        self.assertEqual(dispatcher_login.status_code, 302)
        self.assertEqual(
            dispatcher_client.session.get('employee_access_id'),
            self.dispatcher_access.id,
        )
        self.assertIsNotNone(oup_shift.closed_at)
        self.assertEqual(oup_shift.closed_by_id, self.employee.id)
        self.assertEqual(oup_shift.workplace_code, 'oup')
        self.assertEqual(
            historical_driver_shift.closed_at,
            historical_driver_closed_at,
        )
        self.assertEqual(
            historical_driver_shift.closed_by_id,
            self.employee.id,
        )
        self.assertIsNone(unrelated_shift.closed_at)
        self.assertIsNone(unrelated_shift.closed_by_id)
        self.assertFalse(
            EmployeeShift.objects.filter(
                employee=self.employee,
                workplace_code='dispatcher',
                closed_at__isnull=True,
            ).exists()
        )


@ROLE_HOST_SETTINGS
class WorkplaceCodeIsolationRegressionTests(RoleRegressionFixtureMixin, TestCase):
    """QA-CHAOS-P1-001: driver/dispatcher/mining-master periods never alias."""

    def setUp(self):
        self.driver_role = self.create_role('driver', 'Водитель самосвала')
        self.dispatcher_role = self.create_role('dispatcher', 'Горный диспетчер')
        self.mining_master_role = self.create_role('mining_master', 'Горный мастер')
        self.employee = self.create_employee(
            name='Многоролевой сотрудник для workplace_code',
            phone='+79990000200',
        )
        self.driver_access = self.create_access(
            employee=self.employee,
            role=self.driver_role,
            code='220001',
        )
        self.dispatcher_access = self.create_access(
            employee=self.employee,
            role=self.dispatcher_role,
            code='220002',
        )
        self.mining_master_access = self.create_access(
            employee=self.employee,
            role=self.mining_master_role,
            code='220003',
        )
        self.truck = self.create_truck(garage_number='CHAOS-TRUCK-002')
        self.register_driver(self.employee)
        self.assign_driver(
            employee=self.employee,
            role=self.driver_role,
            truck=self.truck,
        )

    def create_workplace_shift(self, workplace_code):
        return EmployeeShift.objects.create(
            employee=self.employee,
            shift_type=ShiftType.DAY,
            workplace_code=workplace_code,
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=self.employee,
        )

    def test_driver_does_not_treat_other_workplace_periods_as_driver_shift(self):
        client = Client()
        login_response = self.login(
            client,
            host=self.driver_host,
            employee=self.employee,
            access=self.driver_access,
        )
        self.assertEqual(login_response.status_code, 302)

        for workplace_code in ('dispatcher', 'mining_master'):
            with self.subTest(workplace_code=workplace_code):
                shift = self.create_workplace_shift(workplace_code)
                try:
                    response = client.get(
                        reverse('driver_work'),
                        HTTP_HOST=self.driver_host,
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertIsNone(response.context['open_shift'])
                finally:
                    shift.delete()

    def test_dispatcher_lookup_ignores_driver_and_mining_master_periods(self):
        for workplace_code in ('driver', 'mining_master'):
            with self.subTest(workplace_code=workplace_code):
                shift = self.create_workplace_shift(workplace_code)
                try:
                    self.assertIsNone(
                        get_active_dispatcher_shift(self.dispatcher_access)
                    )
                finally:
                    shift.delete()

    def test_mining_master_lookup_ignores_driver_and_dispatcher_periods(self):
        for workplace_code in ('driver', 'dispatcher'):
            with self.subTest(workplace_code=workplace_code):
                shift = self.create_workplace_shift(workplace_code)
                try:
                    current_shift, blocking_shift = get_shift_state(self.employee)
                    self.assertIsNone(current_shift)
                    self.assertIsNone(blocking_shift)
                finally:
                    shift.delete()

    def test_dispatcher_close_does_not_close_driver_shift(self):
        driver_shift = self.create_workplace_shift('driver')

        closed_shift = close_dispatcher_shift(self.dispatcher_access)

        driver_shift.refresh_from_db()
        self.assertIsNone(closed_shift)
        self.assertIsNone(driver_shift.closed_at)
        self.assertIsNone(driver_shift.closed_by)


@ROLE_HOST_SETTINGS
class PinReissueRegressionTests(RoleRegressionFixtureMixin, TestCase):
    """QA-CHAOS-P1-003: reissue during any open period is a complete no-op."""

    def test_not_activated_access_session_cannot_use_realtime_or_working_post(self):
        driver_role = self.create_role('driver', 'Водитель самосвала')
        employee = self.create_employee(
            name='Водитель с перевыпущенным PIN CHAOS',
            phone='+79990000309',
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=driver_role,
            access_code='339999',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            is_active=True,
        )
        client = Client()
        session = client.session
        session['employee_access_id'] = access.id
        session.save()

        realtime_response = client.get(
            reverse('operational_state_version'),
            HTTP_HOST=self.driver_host,
        )
        working_response = client.post(
            reverse('driver_work'),
            {'client_action_id': 'invalidated-access-working-post'},
            HTTP_HOST=self.driver_host,
        )

        self.assertEqual(realtime_response.status_code, 401)
        self.assertFalse(realtime_response.json()['authenticated'])
        self.assertEqual(working_response.status_code, 409)
        self.assertIn(
            'роль неактивна',
            working_response.content.decode('utf-8').lower(),
        )

    def test_oup_reissue_during_open_driver_shift_changes_nothing(self):
        oup_role = self.create_role('oup', 'Специалист ОУП')
        driver_role = self.create_role('driver', 'Водитель самосвала')
        oup_employee = self.create_employee(
            name='Специалист ОУП CHAOS',
            phone='+79990000300',
        )
        target_employee = self.create_employee(
            name='Водитель с открытой сменой CHAOS',
            phone='+79990000301',
        )
        oup_access = self.create_access(
            employee=oup_employee,
            role=oup_role,
            code='330001',
        )
        target_access = self.create_access(
            employee=target_employee,
            role=driver_role,
            code='330003',
        )
        target_access.primary_code_issued_at = timezone.now() - timedelta(days=2)
        target_access.last_login_at = timezone.now() - timedelta(hours=2)
        target_access.blocked_at = None
        target_access.block_reason = ''
        target_access.deactivated_at = None
        target_access.save(
            update_fields=[
                'primary_code_issued_at',
                'last_login_at',
                'blocked_at',
                'block_reason',
                'deactivated_at',
            ]
        )

        oup_client = Client()
        target_client = Client()
        self.assertEqual(
            self.login(
                oup_client,
                host=self.oup_host,
                employee=oup_employee,
                access=oup_access,
            ).status_code,
            302,
        )
        self.assertEqual(
            self.login(
                target_client,
                host=self.driver_host,
                employee=target_employee,
                access=target_access,
            ).status_code,
            302,
        )
        oup_shift = EmployeeShift.objects.create(
            employee=oup_employee,
            shift_type=ShiftType.DAY,
            workplace_code='oup',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=oup_employee,
        )
        truck = self.create_truck(garage_number='CHAOS-TRUCK-003')
        target_shift = self.create_driver_shift(
            employee=target_employee,
            truck=truck,
            readings_complete=False,
        )

        target_access.refresh_from_db()
        access_before = {
            field.attname: getattr(target_access, field.attname)
            for field in target_access._meta.concrete_fields
        }
        target_session_key_before = target_client.session.session_key
        target_session_before = dict(target_client.session.items())
        all_audit_count_before = AdminActionLog.objects.count()
        reissue_audit_count_before = AdminActionLog.objects.filter(
            action_code=OUP_ACTION_ACCESS_REISSUED,
        ).count()

        response = oup_client.post(
            reverse('oup_employee_access_issue', args=[target_employee.id]),
            {'role': str(driver_role.id)},
            HTTP_HOST=self.oup_host,
        )

        target_access.refresh_from_db()
        target_shift.refresh_from_db()
        oup_shift.refresh_from_db()
        access_after = {
            field.attname: getattr(target_access, field.attname)
            for field in target_access._meta.concrete_fields
        }
        self.assertEqual(response.status_code, 302)
        self.assertEqual(access_after, access_before)
        self.assertEqual(
            AdminActionLog.objects.count(),
            all_audit_count_before,
        )
        self.assertEqual(
            AdminActionLog.objects.filter(
                action_code=OUP_ACTION_ACCESS_REISSUED,
            ).count(),
            reissue_audit_count_before,
        )
        self.assertEqual(
            target_client.session.session_key,
            target_session_key_before,
        )
        self.assertEqual(
            dict(target_client.session.items()),
            target_session_before,
        )
        self.assertIsNone(target_shift.closed_at)
        self.assertIsNone(oup_shift.closed_at)
