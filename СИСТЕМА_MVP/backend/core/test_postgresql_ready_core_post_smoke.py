import json
from unittest import skipUnless

from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
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
from shifts.models import EmployeeShift
from trips.models import (
    DispatcherActionLog,
    DispatcherActionType,
    Trip,
    TripClientAction,
    TripStatus,
)
from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from users.models import (
    AdminActionLog,
    DriverPrimaryRegistration,
    Employee,
    EmployeeAccess,
    Role,
)
from users.oup_services import issue_employee_access


@skipUnless(
    connection.vendor == 'postgresql',
    'Одиночные ready-core POST-smoke выполняются только на тестовой PostgreSQL.',
)
class PostgreSQLReadyCorePostSmokeTests(TestCase):
    """Sequential PostgreSQL coverage for every ready-core POST path changed by QA-PG-P1-001."""

    def assert_domain_response(self, response, expected_status):
        exc_info = getattr(response, 'exc_info', None)
        exception_label = None
        if exc_info:
            exception_label = f'{exc_info[0].__name__}: {exc_info[1]}'
        self.assertIsNone(exception_label, exception_label)
        self.assertEqual(
            response.status_code,
            expected_status,
            response.content.decode('utf-8', errors='replace')[:1000],
        )

    def role(self, code, name):
        role, _ = Role.objects.update_or_create(
            code=code,
            defaults={'name': name, 'is_active': True},
        )
        return role

    def access(
        self,
        role_code,
        role_name,
        *,
        full_name,
        access_code,
        phone='',
        employee=None,
        status=EmployeeAccess.Status.ACTIVATED,
        last_login_at=None,
    ):
        role = self.role(role_code, role_name)
        if employee is None:
            employee = Employee.objects.create(
                full_name=full_name,
                phone=phone,
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=status,
            is_active=True,
            primary_code_issued_at=(
                timezone.now()
                if status == EmployeeAccess.Status.NOT_ACTIVATED
                else None
            ),
            last_login_at=last_login_at,
        )
        return employee, access

    def active_client(self, access):
        if access.last_login_at is None:
            access.last_login_at = timezone.now()
            access.save(update_fields=['last_login_at'])
        client = Client(raise_request_exception=False)
        session = client.session
        session['employee_access_id'] = access.id
        session[ACTIVE_ROLE_SESSION_KEY] = access.id
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = access.last_login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = access.role.code
        session.save()
        return client

    def equipment_pair(self, suffix):
        excavator_type = EquipmentType.objects.create(
            name=f'Экскаватор PG smoke {suffix}',
        )
        truck_type = EquipmentType.objects.create(
            name=f'Самосвал PG smoke {suffix}',
        )
        excavator_model = EquipmentModel.objects.create(
            equipment_type=excavator_type,
            name=f'Модель экскаватора PG smoke {suffix}',
            fuel_capacity_limit_l=2000,
        )
        truck_model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name=f'Модель самосвала PG smoke {suffix}',
            body_volume_m3='40.00',
        )
        excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number=f'PG-EX-{suffix}',
        )
        truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number=f'PG-TR-{suffix}',
        )
        return excavator, truck

    def references(self, suffix):
        rock = RockType.objects.create(
            name=f'Руда PG smoke {suffix}',
            density='2.5000',
        )
        dump_point = DumpPoint.objects.create(
            name=f'Точка PG smoke {suffix}',
        )
        return rock, dump_point

    def operator_context(self, excavator, suffix):
        operator, access = self.access(
            'excavator_operator',
            'Машинист экскаватора',
            full_name=f'Машинист PG smoke {suffix}',
            access_code='310001',
        )
        shift = EmployeeShift.objects.create(
            employee=operator,
            shift_type='day',
            workplace_code='excavator_operator',
            equipment=excavator,
            start_fuel='900.00',
            start_engine_hours='100.00',
            opened_at=timezone.now(),
            opened_by=operator,
        )
        return operator, access, shift

    def registered_driver_context(self, truck, suffix):
        driver, access = self.access(
            'driver',
            'Водитель',
            full_name=f'Водитель PG smoke {suffix}',
            access_code='210001',
        )
        dormitory = Dormitory.objects.create(number=f'PG{suffix}'[:16])
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок 1')
        section = DormitorySection.objects.create(block=block, name='А')
        DriverPrimaryRegistration.objects.create(
            employee=driver,
            dormitory_section=section,
        )
        shift = EmployeeShift.objects.create(
            employee=driver,
            shift_type='day',
            workplace_code='driver',
            equipment=truck,
            start_fuel='700.00',
            start_mileage='1000.00',
            start_engine_hours='200.00',
            opened_at=timezone.now(),
            opened_by=driver,
        )
        return driver, access, shift

    def dispatcher_context(self, suffix, *, open_shift=True):
        dispatcher, access = self.access(
            'dispatcher',
            'Диспетчер',
            full_name=f'Диспетчер PG smoke {suffix}',
            access_code='510001',
        )
        shift = None
        if open_shift:
            shift = EmployeeShift.objects.create(
                employee=dispatcher,
                shift_type='day',
                workplace_code='dispatcher',
                equipment=None,
                opened_at=timezone.now(),
                opened_by=dispatcher,
            )
        return dispatcher, access, shift

    def open_trip(
        self,
        *,
        excavator,
        truck,
        rock,
        dump_point,
        operator=None,
        loading_shift=None,
    ):
        return Trip.objects.create(
            excavator=excavator,
            truck=truck,
            excavator_operator=operator,
            loading_shift=loading_shift,
            rock_type=rock,
            dump_point=dump_point,
            assigned_dump_point=dump_point,
            actual_dump_point=dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
        )

    def test_login_switches_active_role_without_nullable_join_server_error(self):
        switched_at = timezone.now()
        employee = Employee.objects.create(
            full_name='Сотрудник двух ролей PG smoke login',
            phone='+79000001001',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        _, dispatcher_access = self.access(
            'dispatcher',
            'Диспетчер',
            full_name=employee.full_name,
            access_code='510011',
            employee=employee,
            last_login_at=switched_at,
        )
        _, admin_access = self.access(
            'admin',
            'Администратор',
            full_name=employee.full_name,
            access_code='110011',
            employee=employee,
        )
        shift = EmployeeShift.objects.create(
            employee=employee,
            shift_type='day',
            workplace_code='dispatcher',
            equipment=None,
            opened_at=timezone.now(),
            opened_by=employee,
        )
        client = Client(raise_request_exception=False)

        response = client.post(
            reverse('login'),
            {
                'phone': employee.phone,
                'access_code': admin_access.access_code,
                'device_kind': 'personal',
            },
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 302)
        self.assertEqual(response.url, reverse('role_home'))
        shift.refresh_from_db()
        admin_access.refresh_from_db()
        self.assertIsNotNone(shift.closed_at)
        self.assertEqual(client.session['employee_access_id'], admin_access.id)
        self.assertEqual(client.session[ACTIVE_ROLE_SESSION_KEY], admin_access.id)
        self.assertEqual(
            client.session[ACTIVE_ROLE_GENERATION_SESSION_KEY],
            admin_access.last_login_at.isoformat(),
        )
        self.assertEqual(client.session[ACTIVE_ROLE_CODE_SESSION_KEY], 'admin')
        dispatcher_access.refresh_from_db()
        self.assertLess(dispatcher_access.last_login_at, admin_access.last_login_at)

    def test_activate_access_switches_active_role_without_nullable_join_server_error(self):
        employee = Employee.objects.create(
            full_name='Сотрудник двух ролей PG smoke activate',
            phone='+79000001002',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        _, dispatcher_access = self.access(
            'dispatcher',
            'Диспетчер',
            full_name=employee.full_name,
            access_code='510012',
            employee=employee,
            last_login_at=timezone.now(),
        )
        _, target_access = self.access(
            'admin',
            'Администратор',
            full_name=employee.full_name,
            access_code='246824',
            employee=employee,
            status=EmployeeAccess.Status.NOT_ACTIVATED,
        )
        shift = EmployeeShift.objects.create(
            employee=employee,
            shift_type='day',
            workplace_code='dispatcher',
            equipment=None,
            opened_at=timezone.now(),
            opened_by=employee,
        )
        client = self.active_client(dispatcher_access)
        session = client.session
        session['pending_activation_access_id'] = target_access.id
        session.save()

        response = client.post(
            reverse('activate_access'),
            {
                'new_access_code': '864286',
                'confirm_access_code': '864286',
            },
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 302)
        self.assertEqual(response.url, reverse('role_home'))
        target_access.refresh_from_db()
        shift.refresh_from_db()
        self.assertEqual(target_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertEqual(target_access.access_code, '864286')
        self.assertIsNotNone(shift.closed_at)
        self.assertEqual(client.session['employee_access_id'], target_access.id)
        self.assertEqual(client.session[ACTIVE_ROLE_SESSION_KEY], target_access.id)
        self.assertEqual(client.session[ACTIVE_ROLE_CODE_SESSION_KEY], 'admin')

    def test_system_admin_undo_oup_action_post_has_domain_success(self):
        admin, admin_access = self.access(
            'admin',
            'Администратор',
            full_name='Администратор PG smoke OUP undo',
            access_code='110017',
        )
        target_employee = Employee.objects.create(
            full_name='Сотрудник PG smoke OUP undo',
            phone='+79000001017',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        driver_role = self.role('driver', 'Водитель')
        issued_access, _code, created = issue_employee_access(
            employee=target_employee,
            role=driver_role,
            actor=None,
        )
        self.assertTrue(created)
        original_log = AdminActionLog.objects.get(
            object_type='EmployeeAccess',
            object_id=str(issued_access.id),
        )
        self.assertIsNone(original_log.actor_id)
        client = self.active_client(admin_access)

        response = client.post(
            reverse('system_admin_undo_oup_action', args=[original_log.id]),
            {
                'comment': 'PG smoke отмены действия ОУП',
                'next': reverse('system_admin_logs'),
            },
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 302)
        self.assertEqual(response.url, reverse('system_admin_logs'))
        self.assertFalse(EmployeeAccess.objects.filter(pk=issued_access.pk).exists())
        reversal = AdminActionLog.objects.get(reversal_of=original_log)
        self.assertEqual(reversal.actor, admin)
        self.assertEqual(reversal.comment, 'PG smoke отмены действия ОУП')

    def test_excavator_truck_loaded_post_has_domain_success(self):
        excavator, truck = self.equipment_pair('load')
        rock, dump_point = self.references('load')
        operator, access, loading_shift = self.operator_context(excavator, 'load')
        self.registered_driver_context(truck, 'load')
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            assigned_by=operator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        client = self.active_client(access)

        response = client.post(
            reverse('excavator_truck_loaded'),
            data=json.dumps(
                {
                    'client_action_id': 'pg-smoke-load',
                    'truck_id': truck.id,
                    'excavator_id': excavator.id,
                    'dump_point_id': dump_point.id,
                    'rock_type_id': rock.id,
                }
            ),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['action'], 'truck_loaded')
        trip = Trip.objects.get(pk=payload['trip_id'])
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.loading_shift, loading_shift)
        self.assertEqual(trip.truck, assignment.truck)
        self.assertTrue(
            TripClientAction.objects.filter(
                action_type='truck_loaded',
                client_action_id='pg-smoke-load',
                trip=trip,
            ).exists()
        )

    def test_excavator_truck_loaded_cancel_post_has_domain_success(self):
        excavator, truck = self.equipment_pair('cancel-load')
        rock, dump_point = self.references('cancel-load')
        operator, access, loading_shift = self.operator_context(
            excavator,
            'cancel-load',
        )
        trip = self.open_trip(
            excavator=excavator,
            truck=truck,
            rock=rock,
            dump_point=dump_point,
            operator=operator,
            loading_shift=loading_shift,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('excavator_truck_loaded_cancel'),
            data=json.dumps(
                {
                    'client_action_id': 'pg-smoke-cancel-load',
                    'trip_id': trip.id,
                    'truck_id': truck.id,
                    'dump_point_id': dump_point.id,
                }
            ),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 200)
        payload = response.json()
        trip.refresh_from_db()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['status'], TripStatus.CANCELLED)
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertTrue(
            TripClientAction.objects.filter(
                action_type='truck_loaded_cancel',
                client_action_id='pg-smoke-cancel-load',
                trip=trip,
            ).exists()
        )

    def test_excavator_work_settings_post_has_domain_success(self):
        excavator, _ = self.equipment_pair('settings')
        rock, dump_point = self.references('settings')
        operator, access, _ = self.operator_context(excavator, 'settings')
        client = self.active_client(access)

        response = client.post(
            reverse('excavator_work_settings'),
            data=json.dumps(
                {
                    'client_action_id': 'pg-smoke-settings',
                    'rock_type_id': rock.id,
                    'dump_point_ids': [dump_point.id],
                    'loading_horizon': '75',
                    'loading_block': '52',
                }
            ),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['action'], 'excavator_work_settings')
        placement = ExcavatorPlacement.objects.get(excavator=excavator)
        self.assertEqual(placement.work_rock_type, rock)
        self.assertEqual(placement.work_dump_point, dump_point)
        self.assertEqual(placement.changed_by, operator)

    def test_excavator_legacy_trip_post_has_domain_success(self):
        excavator, truck = self.equipment_pair('legacy')
        rock, dump_point = self.references('legacy')
        operator, access, loading_shift = self.operator_context(excavator, 'legacy')
        self.registered_driver_context(truck, 'legacy')
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            assigned_by=operator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        client = self.active_client(access)

        response = client.post(
            reverse('excavator_work'),
            {
                'client_action_id': 'pg-smoke-legacy-load',
                'assignment': assignment.id,
                'rock_type': rock.id,
                'dump_point': dump_point.id,
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['action'], 'truck_loaded')
        trip = Trip.objects.get(pk=payload['trip_id'])
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.loading_shift, loading_shift)
        self.assertTrue(
            TripClientAction.objects.filter(
                action_type='truck_loaded',
                client_action_id='pg-smoke-legacy-load',
                trip=trip,
            ).exists()
        )

    def test_excavator_downtime_post_has_domain_success(self):
        excavator, _ = self.equipment_pair('exc-downtime')
        operator, access, _ = self.operator_context(excavator, 'exc-downtime')
        reason = DowntimeReason.objects.create(
            name='Причина PG smoke машиниста',
            equipment_type=excavator.equipment_type,
            show_for_excavator_operator=True,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('excavator_downtime_action'),
            data=json.dumps({'action': 'start', 'reason_id': reason.id}),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['action'], 'downtime_started')
        event = DowntimeEvent.objects.get(
            equipment=excavator,
            ended_at__isnull=True,
        )
        self.assertEqual(event.employee, operator)
        self.assertEqual(event.reason, reason)

    def test_excavator_shift_close_post_has_domain_success(self):
        excavator, _ = self.equipment_pair('shift-close')
        _, access, shift = self.operator_context(excavator, 'shift-close')
        client = self.active_client(access)

        response = client.post(
            reverse('excavator_shift_action'),
            data=json.dumps(
                {
                    'action': 'close',
                    'client_action_id': 'pg-smoke-exc-shift-close',
                    'fuel': '850',
                    'engine_hours': '108',
                }
            ),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['action'], 'excavator_shift_closed')
        shift.refresh_from_db()
        self.assertIsNotNone(shift.closed_at)
        self.assertEqual(str(shift.end_fuel), '850.00')
        self.assertEqual(str(shift.end_engine_hours), '108.00')

    def test_driver_accept_assignment_post_has_domain_success(self):
        excavator, truck = self.equipment_pair('accept')
        _, access, _ = self.registered_driver_context(truck, 'accept')
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.PENDING,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('driver_accept_assignment', args=[assignment.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(response.json()['action'], 'assign')
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNotNone(assignment.accepted_at)

    def test_driver_downtime_post_has_domain_success(self):
        _, truck = self.equipment_pair('driver-downtime')
        driver, access, _ = self.registered_driver_context(
            truck,
            'driver-downtime',
        )
        reason = DowntimeReason.objects.create(
            name='Причина PG smoke водителя',
            equipment_type=truck.equipment_type,
            show_for_truck_driver=True,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('driver_downtime_action'),
            data=json.dumps({'action': 'start', 'reason_id': reason.id}),
            content_type='application/json',
            HTTP_ACCEPT='application/json',
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['action'], 'downtime_started')
        event = DowntimeEvent.objects.get(equipment=truck, ended_at__isnull=True)
        self.assertEqual(event.employee, driver)
        self.assertEqual(event.reason, reason)

    def test_driver_complete_trip_post_has_domain_success(self):
        excavator, truck = self.equipment_pair('driver-unload')
        rock, dump_point = self.references('driver-unload')
        driver, access, unloading_shift = self.registered_driver_context(
            truck,
            'driver-unload',
        )
        trip = self.open_trip(
            excavator=excavator,
            truck=truck,
            rock=rock,
            dump_point=dump_point,
            loading_shift=None,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('driver_complete_trip', args=[trip.id]),
            {'client_action_id': 'pg-smoke-driver-unload'},
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 302)
        self.assertEqual(response.url, reverse('driver_shift'))
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, driver)
        self.assertEqual(trip.unloading_shift, unloading_shift)
        self.assertTrue(
            TripClientAction.objects.filter(
                action_type='trip_unloaded',
                client_action_id='pg-smoke-driver-unload',
                trip=trip,
            ).exists()
        )

    def test_driver_change_unload_point_post_has_domain_success(self):
        excavator, truck = self.equipment_pair('driver-dump')
        rock, assigned_dump = self.references('driver-dump')
        actual_dump = DumpPoint.objects.create(name='Новая точка PG smoke driver-dump')
        _, access, _ = self.registered_driver_context(truck, 'driver-dump')
        trip = self.open_trip(
            excavator=excavator,
            truck=truck,
            rock=rock,
            dump_point=assigned_dump,
            loading_shift=None,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('driver_change_unload_point', args=[trip.id]),
            {
                'client_action_id': 'pg-smoke-driver-dump',
                'dump_point': actual_dump.id,
            },
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 302)
        self.assertEqual(response.url, reverse('driver_shift'))
        trip.refresh_from_db()
        self.assertEqual(trip.assigned_dump_point, assigned_dump)
        self.assertEqual(trip.actual_dump_point, actual_dump)
        self.assertEqual(trip.dump_point, actual_dump)
        self.assertTrue(
            TripClientAction.objects.filter(
                action_type='change_actual_unload_point',
                client_action_id='pg-smoke-driver-dump',
                trip=trip,
            ).exists()
        )

    def test_dispatcher_complete_trip_post_has_domain_success(self):
        dispatcher, access, _ = self.dispatcher_context('complete')
        excavator, truck = self.equipment_pair('dispatcher-complete')
        rock, dump_point = self.references('dispatcher-complete')
        driver, _, unloading_shift = self.registered_driver_context(
            truck,
            'dispatcher-complete',
        )
        trip = self.open_trip(
            excavator=excavator,
            truck=truck,
            rock=rock,
            dump_point=dump_point,
            loading_shift=None,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('dispatcher_complete_trip', args=[trip.id]),
            {'reason': 'PG smoke служебного завершения'},
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 302)
        self.assertEqual(response.url, reverse('dispatcher_control'))
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, driver)
        self.assertEqual(trip.unloading_shift, unloading_shift)
        self.assertTrue(
            DispatcherActionLog.objects.filter(
                actor=dispatcher,
                action_type=DispatcherActionType.COMPLETE_TRIP,
                trip=trip,
            ).exists()
        )

    def test_dispatcher_service_close_shift_post_has_domain_success(self):
        dispatcher, access, _ = self.dispatcher_context(
            'service-close-current',
            open_shift=False,
        )
        previous_dispatcher, _ = self.access(
            'dispatcher',
            'Диспетчер',
            full_name='Предыдущий диспетчер PG smoke',
            access_code='510099',
        )
        target_shift = EmployeeShift.objects.create(
            employee=previous_dispatcher,
            shift_type='day',
            workplace_code='dispatcher',
            equipment=None,
            opened_at=timezone.now(),
            opened_by=previous_dispatcher,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('dispatcher_service_close_shift', args=[target_shift.id]),
            {'reason': 'PG smoke служебного закрытия'},
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 302)
        self.assertEqual(response.url, reverse('dispatcher_control'))
        target_shift.refresh_from_db()
        self.assertIsNotNone(target_shift.closed_at)
        self.assertTrue(target_shift.is_service_closed)
        self.assertEqual(target_shift.closed_by, dispatcher)
        self.assertTrue(
            DispatcherActionLog.objects.filter(
                actor=dispatcher,
                action_type=DispatcherActionType.SERVICE_CLOSE_SHIFT,
                shift=target_shift,
            ).exists()
        )

    def test_dispatcher_cancel_trip_post_has_domain_success(self):
        dispatcher, access, _ = self.dispatcher_context('cancel-trip')
        excavator, truck = self.equipment_pair('dispatcher-cancel-trip')
        rock, dump_point = self.references('dispatcher-cancel-trip')
        trip = self.open_trip(
            excavator=excavator,
            truck=truck,
            rock=rock,
            dump_point=dump_point,
            loading_shift=None,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('dispatcher_cancel_trip', args=[trip.id]),
            {'reason': 'PG smoke отмены рейса'},
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 302)
        self.assertEqual(response.url, reverse('dispatcher_control'))
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertTrue(
            DispatcherActionLog.objects.filter(
                actor=dispatcher,
                action_type=DispatcherActionType.CANCEL_TRIP,
                trip=trip,
            ).exists()
        )

    def test_dispatcher_cancel_assignment_post_has_domain_success(self):
        dispatcher, access, _ = self.dispatcher_context('cancel-assignment')
        excavator, truck = self.equipment_pair('dispatcher-cancel-assignment')
        assignment = HaulAssignment.objects.create(
            excavator=excavator,
            truck=truck,
            assigned_by=dispatcher,
            status=AssignmentStatus.PENDING,
        )
        client = self.active_client(access)

        response = client.post(
            reverse('dispatcher_cancel_assignment', args=[assignment.id]),
            {'reason': 'PG smoke отмены назначения'},
            HTTP_HOST='localhost',
        )

        self.assert_domain_response(response, 302)
        self.assertEqual(response.url, reverse('dispatcher_control'))
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
        self.assertIsNotNone(assignment.ended_at)
        self.assertTrue(
            DispatcherActionLog.objects.filter(
                actor=dispatcher,
                action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
                haul_assignment=assignment,
            ).exists()
        )
