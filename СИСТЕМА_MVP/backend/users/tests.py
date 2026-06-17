from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
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
from reports.models import ReportTemplate, ReportType
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus

from .models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role


class AccessLoginTests(TestCase):
    def setUp(self):
        self.role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.employee = Employee.objects.create(full_name='Тестовый водитель')
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.role,
            access_code='2000',
        )

    def test_registered_driver_opens_shift_screen(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок 1')
        section = DormitorySection.objects.create(block=block, name='А')
        DriverPrimaryRegistration.objects.create(
            employee=self.employee,
            shift_type='day',
            truck=truck,
            dormitory_section=section,
        )

        response = self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Смена водителя')
        self.assertContains(response, 'Открыть смену')
        self.assertEqual(self.client.session.get('employee_access_id'), self.access.id)

    def test_wrong_access_code_stays_on_login(self):
        response = self.client.post('/', {'access_code': 'wrong'}, follow=True, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Доступ не найден или отключен.')
        self.assertIsNone(self.client.session.get('employee_access_id'))

    def test_driver_primary_registration_flow(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок 1')
        section = DormitorySection.objects.create(block=block, name='А')

        login_response = self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        self.assertRedirects(login_response, '/driver/registration/', target_status_code=200)

        registration_response = self.client.post(
            '/driver/registration/',
            {
                'shift_type': 'day',
                'truck': truck.id,
                'dormitory_section': section.id,
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(registration_response.status_code, 200)
        self.assertContains(registration_response, 'Открыть смену')
        self.assertTrue(self.employee.driver_registration)

    def test_driver_can_open_shift_after_registration(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок 1')
        section = DormitorySection.objects.create(block=block, name='А')

        self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        self.client.post(
            '/driver/registration/',
            {'shift_type': 'day', 'truck': truck.id, 'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        response = self.client.post(
            '/driver/shift/',
            {'start_fuel': '100', 'start_mileage': '2500', 'start_engine_hours': '700'},
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Смена открыта')
        self.assertTrue(self.employee.employeeshift_set.filter(closed_at__isnull=True).exists())

    def test_driver_can_close_shift_and_next_opening_uses_last_end_values(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок 1')
        section = DormitorySection.objects.create(block=block, name='А')

        self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        self.client.post(
            '/driver/registration/',
            {'shift_type': 'day', 'truck': truck.id, 'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        self.client.post(
            '/driver/shift/',
            {'start_fuel': '100', 'start_mileage': '2500', 'start_engine_hours': '700'},
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
        self.assertContains(close_response, 'Смена закрыта')
        self.assertIsNotNone(shift.closed_at)
        self.assertEqual(shift.end_fuel, 90)
        self.assertEqual(shift.end_mileage, 2600)
        self.assertEqual(shift.end_engine_hours, 712)

        next_open_response = self.client.get('/driver/shift/', HTTP_HOST='localhost')
        self.assertContains(next_open_response, 'value="90')
        self.assertContains(next_open_response, 'value="2600')
        self.assertContains(next_open_response, 'value="712')

    def test_driver_can_accept_haul_assignment(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок 1')
        section = DormitorySection.objects.create(block=block, name='А')

        self.client.post('/', {'access_code': '2000'}, follow=True, HTTP_HOST='localhost')
        self.client.post(
            '/driver/registration/',
            {'shift_type': 'day', 'truck': truck.id, 'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment = HaulAssignment.objects.create(truck=truck, excavator=excavator)

        shift_response = self.client.get('/driver/shift/', HTTP_HOST='localhost')
        self.assertContains(shift_response, 'Новое назначение')
        self.assertContains(shift_response, 'Принял')

        accept_response = self.client.post(
            f'/driver/assignment/{assignment.id}/accept/',
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment.refresh_from_db()

        self.assertEqual(accept_response.status_code, 200)
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNotNone(assignment.accepted_at)

    def test_excavator_creates_trip_and_driver_completes_it(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок 1')
        section = DormitorySection.objects.create(block=block, name='А')
        excavator_role = Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
        excavator_operator = Employee.objects.create(full_name='Тестовый машинист')
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
            {'shift_type': 'day', 'truck': truck.id, 'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        driver_client.post(
            '/driver/shift/',
            {'start_fuel': '100', 'start_mileage': '2500', 'start_engine_hours': '700'},
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment = HaulAssignment.objects.create(truck=truck, excavator=excavator, status=AssignmentStatus.ACCEPTED)

        operator_client = self.client_class(HTTP_HOST='localhost')
        operator_client.post('/', {'access_code': '3000'}, follow=True, HTTP_HOST='localhost')
        trip_response = operator_client.post(
            '/excavator/work/',
            {'assignment': assignment.id, 'rock_type': rock.id, 'dump_point': dump_point.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        self.assertEqual(trip_response.status_code, 200)
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.ACTIVE)
        self.assertEqual(trip.loading_shift, operator_shift)

        driver_shift_response = driver_client.get('/driver/shift/', HTTP_HOST='localhost')
        self.assertContains(driver_shift_response, 'Активный рейс')
        self.assertContains(driver_shift_response, 'Выполнено')

        complete_response = driver_client.post(
            f'/driver/trip/{trip.id}/complete/',
            follow=True,
            HTTP_HOST='localhost',
        )
        trip.refresh_from_db()

        self.assertEqual(complete_response.status_code, 200)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, self.employee)
        self.assertIsNotNone(trip.completed_at)
        self.assertFalse(trip.is_carryover)

    def test_trip_becomes_carryover_when_loading_and_unloading_shift_types_differ(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dormitory = Dormitory.objects.create(number='5')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок 1')
        section = DormitorySection.objects.create(block=block, name='А')
        excavator_operator = Employee.objects.create(full_name='Тестовый машинист')
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
            {'shift_type': 'night', 'truck': truck.id, 'dormitory_section': section.id},
            follow=True,
            HTTP_HOST='localhost',
        )
        self.client.post(
            '/driver/shift/',
            {'start_fuel': '100', 'start_mileage': '2500', 'start_engine_hours': '700'},
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

        self.client.post(f'/driver/trip/{trip.id}/complete/', follow=True, HTTP_HOST='localhost')
        trip.refresh_from_db()

        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertTrue(trip.is_carryover)
        self.assertEqual(trip.unloading_shift.shift_type, 'night')

    def test_trip_volume_and_tonnage_are_calculated_from_capacity_rule_and_density(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck_model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='БЕЛАЗ тест',
            body_volume_m3='40.00',
        )
        truck = Equipment.objects.create(equipment_type=truck_type, model=truck_model, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда', density='2.50')
        dump_point = DumpPoint.objects.create(name='ККД')
        excavator_role = Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
        excavator_operator = Employee.objects.create(full_name='Тестовый машинист')
        EmployeeAccess.objects.create(employee=excavator_operator, role=excavator_role, access_code='3000')
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
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
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
        self.assertContains(response, 'Диспетчерский пульт')

        report_response = self.client.get('/reports/volume/', HTTP_HOST='localhost')
        self.assertEqual(report_response.status_code, 200)
        self.assertContains(report_response, 'Отчет по объемам')
        self.assertContains(report_response, '57')

        export_response = self.client.get('/reports/volume/export/', HTTP_HOST='localhost')
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

    def test_dispatcher_opens_control_panel_with_active_trips_and_pending_assignments(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        second_truck = Equipment.objects.create(equipment_type=truck_type, garage_number='11')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
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

        response = self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Диспетчерский пульт')
        self.assertContains(response, 'Активные рейсы')
        self.assertContains(response, 'Назначения ждут подтверждения')
        self.assertContains(response, '57')
        self.assertContains(response, 'Открыть отчет по объемам')

    def test_volume_report_can_filter_by_loading_shift_type(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        operator = Employee.objects.create(full_name='Машинист')
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

    def test_volume_report_uses_selected_report_template_columns(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        template = ReportTemplate.objects.create(
            name='Короткий отчет',
            report_type=ReportType.SHIFT_VOLUME,
            columns=['truck', 'volume_m3'],
        )
        Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            volume_m3='11.00',
            completed_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get(f'/reports/volume/?template={template.id}', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<th>Самосвал</th>', html=True)
        self.assertContains(response, '<th>Объем, м3</th>', html=True)
        self.assertNotContains(response, '<th>Экскаватор</th>', html=True)
        self.assertContains(response, '11')

# Create your tests here.
