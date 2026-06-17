from django.test import TestCase
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from references.models import Dormitory, DormitoryBlock, DormitorySection, DumpPoint, Equipment, EquipmentType, RockType
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
        self.assertContains(response, 'Отчет по объемам')
        self.assertContains(response, '57')

        export_response = self.client.get('/reports/volume/export/', HTTP_HOST='localhost')
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

# Create your tests here.
