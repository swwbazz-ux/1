from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from openpyxl import load_workbook

from assignments.models import AssignmentStatus, HaulAssignment
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
from shifts.models import EmployeeShift
from trips.models import DispatcherActionLog, DispatcherActionType, Trip, TripStatus

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

    def test_interface_map_opens_without_login(self):
        response = self.client.get('/interfaces/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Карта интерфейсов MVP')
        self.assertContains(response, '/reports/volume/')
        self.assertContains(response, '/reports/templates/')
        self.assertContains(response, '/reports/management/')
        self.assertContains(response, '/reports/management/export/')
        self.assertContains(response, '/reports/pilot-checklist/')
        self.assertContains(response, '/reports/pilot-scenario/')
        self.assertContains(response, '/reports/pilot-feedback/')
        self.assertContains(response, 'Excel-выгрузка витрины руководства')
        self.assertContains(response, 'Чеклист пилотной проверки отчетов')
        self.assertContains(response, 'Сценарий пилотного запуска')
        self.assertContains(response, 'Журнал замечаний пилота')
        self.assertContains(response, '6000')

    def test_manager_can_open_pilot_report_checklist(self):
        manager_role = Role.objects.create(code='manager', name='Руководство')
        manager = Employee.objects.create(full_name='Тестовое руководство')
        EmployeeAccess.objects.create(employee=manager, role=manager_role, access_code='6000')

        self.client.post('/', {'access_code': '6000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/pilot-checklist/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Чеклист пилотной проверки отчетов')
        self.assertContains(response, '9 из 10')
        self.assertContains(response, '99%')
        self.assertContains(response, 'Сверка со старыми Excel-формами')
        self.assertContains(response, 'Отчет_Коппер. Рисорсез_Март.xlsx')
        self.assertContains(response, 'почасовой Март.xlsx')
        self.assertContains(response, '/reports/volume/?group_by=completed_hour')
        self.assertContains(response, 'ОР ККД СКДР март.xlsx')
        self.assertContains(response, 'удельный_веса_руд_и_пород_Малмыжского_местородения.xlsx')
        self.assertContains(response, '/admin/references/rocktype/')
        self.assertContains(response, 'КИП/КТГ и КИО/КТГ')
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
        manager_role = Role.objects.create(code='manager', name='Руководство')
        manager = Employee.objects.create(full_name='Тестовое руководство')
        EmployeeAccess.objects.create(employee=manager, role=manager_role, access_code='6000')

        self.client.post('/', {'access_code': '6000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/pilot-scenario/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Сценарий пилотного запуска')
        self.assertContains(response, '9 из 10')
        self.assertContains(response, '99%')
        self.assertContains(response, 'Расстановка техники')
        self.assertContains(response, 'Работа водителя')
        self.assertContains(response, 'Диспетчерский контроль')
        self.assertContains(response, 'Вопросы для фиксации во время пилота')
        self.assertContains(response, '/reports/pilot-feedback/')
        self.assertContains(response, '31_ЖУРНАЛ_ЗАМЕЧАНИЙ_ПИЛОТА.md')

    def test_manager_can_create_pilot_feedback_and_export_it(self):
        manager_role = Role.objects.create(code='manager', name='Руководство')
        manager = Employee.objects.create(full_name='Тестовое руководство')
        EmployeeAccess.objects.create(employee=manager, role=manager_role, access_code='6000')

        self.client.post('/', {'access_code': '6000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            '/reports/pilot-feedback/',
            {
                'title': 'Не хватает столбца для сверки',
                'category': 'report',
                'priority': 'p1',
                'status': 'new',
                'screen': 'Суточный отчет',
                'description': 'На пилоте нужно сверить старую форму заказчика.',
                'decision': 'Добавить в список доработок после проверки.',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Журнал замечаний пилота')
        self.assertContains(response, 'Не хватает столбца для сверки')
        self.assertContains(response, 'P1 - исправить до запуска')
        self.assertEqual(PilotFeedback.objects.count(), 1)
        feedback = PilotFeedback.objects.first()
        self.assertEqual(feedback.created_by, manager)

        export_response = self.client.get('/reports/pilot-feedback/export/', HTTP_HOST='localhost')

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        workbook = load_workbook(BytesIO(export_response.content))
        self.assertIn('Замечания пилота', workbook.sheetnames)
        sheet = workbook['Замечания пилота']
        self.assertEqual(sheet['A1'].value, 'Журнал замечаний пилотного запуска')
        self.assertEqual(sheet['F5'].value, 'Не хватает столбца для сверки')

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
            {
                'assignment': assignment.id,
                'rock_type': rock.id,
                'dump_point': dump_point.id,
                'planned_volume_m3': '7000',
                'loading_horizon': '75',
                'loading_block': '52',
                'transport_distance_km': '3.10',
                'downtime_text': 'зачистка забоя',
                'note': 'проверка параметров отчета',
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
        self.assertEqual(trip.downtime_text, 'зачистка забоя')
        self.assertEqual(trip.note, 'проверка параметров отчета')

        next_trip_form_response = operator_client.get('/excavator/work/', HTTP_HOST='localhost')
        self.assertContains(next_trip_form_response, 'value="7000')
        self.assertContains(next_trip_form_response, 'value="75"')
        self.assertContains(next_trip_form_response, 'value="52"')
        self.assertContains(next_trip_form_response, 'value="3.10"')

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
        HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        response = self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Диспетчерский пульт')
        self.assertContains(response, 'Активные рейсы')
        self.assertContains(response, 'Назначения ждут подтверждения')
        self.assertContains(response, 'Принятые назначения в работе')
        self.assertContains(response, '57')
        self.assertContains(response, 'Открыть отчет по объемам')
        self.assertContains(response, 'Суточный отчет заказчику')

    def test_dispatcher_control_panel_can_filter_by_truck(self):
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
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/dispatcher/control/?show_accepted_assignments=0', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Принятых назначений в работе сейчас нет.')

    def test_dispatcher_service_action_preserves_current_filters(self):
        truck_type = EquipmentType.objects.create(name='РЎР°РјРѕСЃРІР°Р»')
        excavator_type = EquipmentType.objects.create(name='Р­РєСЃРєР°РІР°С‚РѕСЂ')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        second_truck = Equipment.objects.create(equipment_type=truck_type, garage_number='11')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Р СѓРґР°')
        dump_point = DumpPoint.objects.create(name='РљРљР”')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Р”РёСЃРїРµС‚С‡РµСЂ')
        dispatcher = Employee.objects.create(full_name='РўРµСЃС‚РѕРІС‹Р№ РґРёСЃРїРµС‚С‡РµСЂ')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
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
                'reason': 'Р¤РёР»СЊС‚СЂ РґРѕР»Р¶РµРЅ СЃРѕС…СЂР°РЅРёС‚СЊСЃСЏ',
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
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        driver_role, _ = Role.objects.get_or_create(code='driver', defaults={'name': 'Водитель самосвала'})
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        driver = Employee.objects.create(full_name='Тестовый водитель')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeAccess.objects.create(employee=driver, role=driver_role, access_code='2100')
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
        self.assertContains(response, 'Незакрытые смены')
        self.assertContains(response, 'Тестовый водитель')
        self.assertContains(response, 'Водитель самосвала')
        self.assertContains(response, 'Закрыть служебно')

        close_response = self.client.post(
            f'/dispatcher/shifts/{shift.id}/service-close/',
            {'reason': 'Сотрудник не смог закрыть смену'},
            follow=True,
            HTTP_HOST='localhost',
        )
        shift.refresh_from_db()

        self.assertEqual(close_response.status_code, 200)
        self.assertIsNotNone(shift.closed_at)
        self.assertTrue(shift.is_service_closed)
        self.assertEqual(shift.closed_by, dispatcher)
        self.assertContains(close_response, 'Открытых смен сейчас нет.')
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.actor, dispatcher)
        self.assertEqual(action.action_type, DispatcherActionType.SERVICE_CLOSE_SHIFT)
        self.assertEqual(action.reason, 'Сотрудник не смог закрыть смену')

    def test_manager_cannot_service_close_shift(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        manager_role = Role.objects.create(code='manager', name='Руководство')
        driver_role, _ = Role.objects.get_or_create(code='driver', defaults={'name': 'Водитель самосвала'})
        manager = Employee.objects.create(full_name='Тестовый руководитель')
        driver = Employee.objects.create(full_name='Тестовый водитель')
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
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.PENDING,
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.post(
            f'/dispatcher/assignments/{assignment.id}/cancel/',
            {'reason': 'Переназначение техники'},
            follow=True,
            HTTP_HOST='localhost',
        )
        assignment.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
        self.assertIsNotNone(assignment.ended_at)
        self.assertContains(response, 'Ожидающих подтверждения назначений нет.')
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.action_type, DispatcherActionType.CANCEL_ASSIGNMENT)
        self.assertEqual(action.reason, 'Переназначение техники')

    def test_dispatcher_can_cancel_accepted_assignment_from_control_panel(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
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
        self.assertContains(response, 'Принятых назначений в работе сейчас нет.')

    def test_dispatcher_can_service_complete_active_trip_from_control_panel(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        driver_role, _ = Role.objects.get_or_create(code='driver', defaults={'name': 'Водитель самосвала'})
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        driver = Employee.objects.create(full_name='Тестовый водитель')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        EmployeeAccess.objects.create(employee=driver, role=driver_role, access_code='2102')
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
            {'reason': 'Водитель потерял связь'},
            follow=True,
            HTTP_HOST='localhost',
        )
        trip.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, driver)
        self.assertEqual(trip.unloading_shift, unloading_shift)
        self.assertIsNotNone(trip.completed_at)
        self.assertContains(response, 'Выполненных рейсов пока нет.', count=0)
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.action_type, DispatcherActionType.COMPLETE_TRIP)
        self.assertEqual(action.reason, 'Водитель потерял связь')

    def test_dispatcher_cannot_service_complete_active_trip_without_open_shift(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
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
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
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
            {'reason': 'Ошибочно созданный рейс'},
            follow=True,
            HTTP_HOST='localhost',
        )
        trip.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertContains(response, 'Активных рейсов сейчас нет.')
        action = DispatcherActionLog.objects.get()
        self.assertEqual(action.action_type, DispatcherActionType.CANCEL_TRIP)
        self.assertEqual(action.reason, 'Ошибочно созданный рейс')

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

    def test_volume_report_can_group_by_completed_hour_and_export_excel(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
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
        self.assertContains(response, '<th>Час выполнения рейса</th>', html=True)
        self.assertContains(response, 'первая MVP-замена старой формы')
        self.assertContains(response, '10:00')
        self.assertContains(response, '11:00')
        self.assertContains(response, '<th>Рейсы</th>', html=True)

        export_response = self.client.get('/reports/volume/export/?group_by=completed_hour', HTTP_HOST='localhost')
        workbook = load_workbook(BytesIO(export_response.content))
        values = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell not in {None, ''}
        ]

        self.assertEqual(export_response.status_code, 200)
        self.assertIn('Час выполнения рейса', values)
        self.assertIn('10:00', values)
        self.assertIn('11:00', values)
        self.assertIn('Итого', values)

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
        self.assertContains(response, '<th>Самосвал</th>', html=True)
        self.assertContains(response, '<th>План, м3</th>', html=True)
        self.assertContains(response, '<th>Объем, м3</th>', html=True)
        self.assertContains(response, '<th>Отклонение, м3</th>', html=True)
        self.assertContains(response, '<th>Выполнение, %</th>', html=True)
        self.assertNotContains(response, '<th>Экскаватор</th>', html=True)
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
        self.assertIn('Отклонение, м3', values)
        self.assertIn('Выполнение, %', values)
        self.assertIn(Decimal('1.00'), values)
        self.assertIn(Decimal('110.00'), values)

    def test_dispatcher_can_create_report_template_in_builder(self):
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
        self.assertContains(builder_response, 'Конструктор шаблонов отчетов')

        create_response = self.client.post(
            '/reports/templates/',
            {
                'name': 'Шаблон для заказчика',
                'columns': ['truck', 'volume_m3'],
                'column_label_truck': 'БелАЗ',
                'column_label_volume_m3': 'Факт, м3',
                'group_by': 'truck',
                'truck': str(truck.id),
                'is_active': 'on',
            },
            follow=True,
            HTTP_HOST='localhost',
        )
        template = ReportTemplate.objects.get(name='Шаблон для заказчика')

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(template.report_type, ReportType.SHIFT_VOLUME)
        self.assertEqual(template.columns, ['truck', 'volume_m3'])
        self.assertEqual(template.column_labels, {'truck': 'БелАЗ', 'volume_m3': 'Факт, м3'})
        self.assertEqual(template.filters, {'truck': str(truck.id)})
        self.assertEqual(template.group_by, 'truck')
        self.assertEqual(template.created_by, dispatcher)
        self.assertEqual(template.updated_by, dispatcher)

        report_response = self.client.get(f'/reports/volume/?template={template.id}', HTTP_HOST='localhost')

        self.assertEqual(report_response.status_code, 200)
        self.assertContains(report_response, '<th>БелАЗ</th>', html=True)
        self.assertContains(report_response, '<th>Факт, м3</th>', html=True)
        self.assertContains(report_response, '<th>Тоннаж</th>', html=True)
        self.assertContains(report_response, '<th>Рейсы</th>', html=True)
        self.assertNotContains(report_response, '<th>Экскаватор</th>', html=True)
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
        self.assertIn('Отчет по объемам', values)
        self.assertIn('Шаблон для заказчика', values)
        self.assertIn('БелАЗ', values)
        self.assertIn('Факт, м3', values)
        self.assertIn('Итого', values)
        self.assertNotIn(Decimal('22.00'), values)

    def test_dispatcher_can_open_customer_daily_report_and_export_it(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Первичная сульфидная')
        dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        operator = Employee.objects.create(full_name='Машинист')
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
            downtime_text='зачистка забоя',
            note='ожидание разгрузки',
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
        self.assertContains(response, 'Суточный отчет заказчику')
        self.assertContains(response, 'Первичная сульфидная')
        self.assertContains(response, 'ККД')
        self.assertContains(response, '57')
        self.assertContains(response, '7000')
        self.assertContains(response, 'Отклонение')
        self.assertContains(response, '-6943')
        self.assertContains(response, '75')
        self.assertContains(response, '52')
        self.assertContains(response, '3,10')
        self.assertContains(response, 'зачистка забоя')
        self.assertContains(response, 'ожидание разгрузки')
        self.assertContains(response, 'С начала месяца')
        self.assertContains(response, '8000')
        self.assertContains(response, '157')
        self.assertContains(response, '-7843')
        self.assertContains(response, 'Сверка со старой Excel-формой заказчика')
        self.assertContains(response, 'Работа выемочного оборудования')
        self.assertContains(response, 'Средневзвешенное плечо')
        self.assertContains(response, 'Расчет выполненных работ по самосвалам')

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
        self.assertIn('Сверка с Excel', workbook.sheetnames)
        reconciliation_values = [
            cell.value
            for row in workbook['Сверка с Excel'].iter_rows()
            for cell in row
        ]
        self.assertIn('Эталон для сверки: Отчет_Коппер. Рисорсез_Март.xlsx', reconciliation_values)
        self.assertIn('Работа выемочного оборудования', reconciliation_values)
        self.assertIn('Средневзвешенное плечо', reconciliation_values)

    def test_seed_demo_scenario_command_creates_ready_demo_data(self):
        call_command('seed_demo_scenario')

        self.assertTrue(EmployeeAccess.objects.filter(access_code='2000', is_active=True).exists())
        self.assertTrue(EmployeeAccess.objects.filter(access_code='5000', is_active=True).exists())
        self.assertTrue(EmployeeAccess.objects.filter(access_code='6000', is_active=True).exists())
        self.assertTrue(EmployeeAccess.objects.filter(access_code='7000', is_active=True).exists())
        self.assertTrue(DriverPrimaryRegistration.objects.exists())
        self.assertTrue(EmployeeShift.objects.filter(closed_at__isnull=True).exists())
        self.assertTrue(HaulAssignment.objects.filter(status=AssignmentStatus.ACCEPTED).exists())
        self.assertTrue(Trip.objects.filter(status=TripStatus.ACTIVE).exists())
        self.assertTrue(DowntimeEvent.objects.filter(ended_at__isnull=True).exists())
        self.assertTrue(DowntimeEvent.objects.filter(ended_at__isnull=False).exists())
        self.assertTrue(DowntimeEvent.objects.filter(reason__is_critical=True, ended_at__isnull=True).exists())
        self.assertTrue(DowntimeEvent.objects.filter(reason__is_critical=False, ended_at__isnull=False).exists())
        self.assertTrue(ReportTemplate.objects.filter(name='Демо отчет по объемам', is_active=True).exists())

    def test_mechanic_opens_dashboard_and_creates_downtime_event(self):
        excavator_type = EquipmentType.objects.create(name='Р­РєСЃРєР°РІР°С‚РѕСЂ')
        truck_type = EquipmentType.objects.create(name='РЎР°РјРѕСЃРІР°Р»')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        rock = RockType.objects.create(name='Р СѓРґР°')
        dump_point = DumpPoint.objects.create(name='РљРљР”')
        mechanic_role = Role.objects.create(code='mechanic', name='РњРµС…Р°РЅРёРє')
        operator_role = Role.objects.create(code='excavator_operator', name='РњР°С€РёРЅРёСЃС‚ СЌРєСЃРєР°РІР°С‚РѕСЂР°')
        mechanic = Employee.objects.create(full_name='РўРµСЃС‚РѕРІС‹Р№ РјРµС…Р°РЅРёРє')
        operator = Employee.objects.create(full_name='РўРµСЃС‚РѕРІС‹Р№ РјР°С€РёРЅРёСЃС‚')
        EmployeeAccess.objects.create(employee=mechanic, role=mechanic_role, access_code='7000')
        EmployeeAccess.objects.create(employee=operator, role=operator_role, access_code='3000')
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=rock,
            dump_point=dump_point,
            excavator_operator=operator,
            status=TripStatus.ACTIVE,
            downtime_text='РѕР¶РёРґР°РЅРёРµ РјРµС…Р°РЅРёРєР°',
        )
        reason = DowntimeReason.objects.create(name='Р”РёР°РіРЅРѕСЃС‚РёРєР°', equipment_type=excavator_type)

        login_response = self.client.post('/', {'access_code': '7000'}, follow=True, HTTP_HOST='localhost')
        dashboard_response = self.client.get('/mechanic/downtimes/', HTTP_HOST='localhost')
        create_response = self.client.post(
            f'/mechanic/downtimes/create/{trip.id}/',
            {
                f'trip_{trip.id}-reason': str(reason.id),
                f'trip_{trip.id}-comment': 'Р’С‹РµС…Р°Р»Рё РЅР° РґРёР°РіРЅРѕСЃС‚РёРєСѓ',
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
        reason = DowntimeReason.objects.create(name='Diagnostics', equipment_type=excavator_type)
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
        excavator_type = EquipmentType.objects.create(name='Р­РєСЃРєР°РІР°С‚РѕСЂ')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        mechanic_role = Role.objects.create(code='mechanic', name='РњРµС…Р°РЅРёРє')
        mechanic = Employee.objects.create(full_name='РўРµСЃС‚РѕРІС‹Р№ РјРµС…Р°РЅРёРє')
        EmployeeAccess.objects.create(employee=mechanic, role=mechanic_role, access_code='7000')
        reason = DowntimeReason.objects.create(name='РўРµРєСѓС‰РёР№ СЂРµРјРѕРЅС‚', equipment_type=excavator_type)
        event = DowntimeEvent.objects.create(
            equipment=excavator,
            employee=mechanic,
            reason=reason,
            started_at=timezone.now() - timedelta(minutes=25),
            comment='РџСЂРѕРІРµСЂРєР°',
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
        self.assertContains(response, '1 ч 30 мин')

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
        self.assertContains(response, '200 из 201')
        self.assertNotContains(response, 'Hidden old open event')
        self.assertContains(response, '17.06.2026')
        self.assertNotContains(response, 'Closed event')
        self.assertEqual(
            export_response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        workbook = load_workbook(BytesIO(export_response.content))
        values = [cell.value for row in workbook.active.iter_rows() for cell in row]
        self.assertIn('Сводка по датам', values)
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
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck_10 = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        truck_25 = Equipment.objects.create(equipment_type=truck_type, garage_number='25')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        EmployeeAccess.objects.create(employee=dispatcher, role=dispatcher_role, access_code='5000')
        kkd_reason = DowntimeReason.objects.create(name='Ожидание разгрузки ККД', equipment_type=truck_type)
        skdr_reason = DowntimeReason.objects.create(name='Ожидание разгрузки СКДР', equipment_type=truck_type)
        started_at = timezone.make_aware(datetime(2026, 6, 17, 9, 0))
        DowntimeEvent.objects.create(
            equipment=truck_10,
            employee=dispatcher,
            reason=kkd_reason,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=45),
            comment='Очередь на ККД',
        )
        DowntimeEvent.objects.create(
            equipment=truck_25,
            employee=dispatcher,
            reason=skdr_reason,
            started_at=started_at + timedelta(hours=1),
            ended_at=started_at + timedelta(hours=1, minutes=30),
            comment='Очередь на СКДР',
        )

        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')
        response = self.client.get('/reports/downtimes/?date_from=2026-06-17&date_to=2026-06-17', HTTP_HOST='localhost')
        export_response = self.client.get('/reports/downtimes/export/?date_from=2026-06-17&date_to=2026-06-17', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Сверка ОР ККД/СКДР')
        self.assertContains(response, 'Покрытие старой формы ОР ККД/СКДР')
        self.assertContains(response, 'Ожидание разгрузки ККД')
        self.assertContains(response, 'Ожидание разгрузки СКДР')
        self.assertContains(response, '75,00 мин')
        workbook = load_workbook(BytesIO(export_response.content))
        self.assertIn('ОР ККД СКДР', workbook.sheetnames)
        values = [cell.value for row in workbook['ОР ККД СКДР'].iter_rows() for cell in row]
        self.assertIn('Сверка ожидания разгрузки ККД/СКДР', values)
        self.assertIn('Ожидание разгрузки ККД', values)
        self.assertIn('Ожидание разгрузки СКДР', values)
        self.assertIn('Источник старой формы: ОР ККД СКДР март.xlsx', values)

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
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        rock = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='ККД')
        manager_role = Role.objects.create(code='manager', name='Руководство')
        manager = Employee.objects.create(full_name='Тестовое руководство')
        operator = Employee.objects.create(full_name='Тестовый машинист')
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
        self.assertContains(dashboard_response, 'Витрина руководства')
        self.assertContains(dashboard_response, 'Выгрузить витрину в Excel')
        self.assertContains(dashboard_response, 'Чеклист пилотной проверки')
        self.assertContains(dashboard_response, '/reports/pilot-checklist/')
        self.assertContains(dashboard_response, 'Факт за сутки')
        self.assertContains(dashboard_response, 'План за сутки')
        self.assertContains(dashboard_response, 'Выполнение плана')
        self.assertContains(dashboard_response, 'Отклонение за сутки')
        self.assertContains(dashboard_response, 'День против ночи')
        self.assertContains(dashboard_response, 'Дневная смена')
        self.assertContains(dashboard_response, 'Ночная смена')
        self.assertContains(dashboard_response, 'Динамика за 7 дней')
        self.assertContains(dashboard_response, 'Итог за 7 дней')
        self.assertContains(dashboard_response, 'План за 7 дней')
        self.assertContains(dashboard_response, 'Выполнение за неделю')
        self.assertContains(dashboard_response, 'Лучший день')
        self.assertContains(dashboard_response, 'Самая сильная просадка')
        self.assertContains(dashboard_response, '16.06')
        self.assertContains(dashboard_response, '17.06')
        self.assertContains(dashboard_response, '57,00')
        self.assertContains(dashboard_response, '60,00')
        self.assertContains(dashboard_response, '95,0%')
        self.assertContains(dashboard_response, '-3,00')
        self.assertContains(dashboard_response, '22,00')
        self.assertContains(dashboard_response, '110,0%')
        self.assertContains(dashboard_response, 'Рейсы за сутки')
        self.assertContains(dashboard_response, 'Экскаваторы за сутки')
        self.assertContains(dashboard_response, 'Породы и грузы за сутки')
        self.assertContains(dashboard_response, 'Общая накопленная картина')
        self.assertContains(dashboard_response, '79 м3')
        self.assertContains(dashboard_response, '80,00 м3')
        self.assertContains(dashboard_response, '98,8%')
        self.assertContains(dashboard_response, '57,00')
        self.assertContains(dashboard_response, '142,50')
        workbook = load_workbook(BytesIO(export_response.content))
        self.assertIn('Сводка', workbook.sheetnames)
        self.assertIn('Динамика 7 дней', workbook.sheetnames)
        self.assertIn('День ночь', workbook.sheetnames)
        values = [cell.value for sheet in workbook.worksheets for row in sheet.iter_rows() for cell in row]
        self.assertIn('Витрина руководства', values)
        self.assertIn('Факт за 7 дней, м3', values)
        self.assertIn('Выполнение за неделю, %', values)
        self.assertIn('Дневная смена', values)
        self.assertIn(Decimal('79.00'), values)
        self.assertIn(98.8, values)

# Create your tests here.
