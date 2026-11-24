import json
import re
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from PIL import Image

from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    Equipment,
    EquipmentModel,
    EquipmentType,
)
from rotations.models import RotationCollectionCycle, RotationResponse, WatchExtensionCase
from shifts.models import EmployeeShift, WatchPeriod
from users.models import (
    DriverPrimaryRegistration,
    Employee,
    EmployeeAccess,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
    TemporaryWorkTransfer,
    WorkSchedule,
)

from .models import AssignmentStatus, CrewPlan, CrewPlanSlot, CrewPlanStatus, EquipmentAssignment, WorkShiftType
from .services import (
    get_active_equipment_assignment,
    get_or_create_crew_draft,
    production_work_date,
    set_active_equipment_assignment,
)


class DeputyPlanningViewTests(TestCase):
    def setUp(self):
        self.deputy_role, _created = Role.objects.update_or_create(
            code='deputy_mining_manager',
            defaults={
                'name': 'Зам. начальника горного участка',
                'is_active': True,
            },
        )
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.excavator_role = Role.objects.create(
            code='excavator_operator',
            name='Машинист экскаватора',
        )
        self.deputy, self.deputy_access = self.create_employee_with_access(
            'Заместитель начальника участка',
            self.deputy_role,
            phone='+79000000010',
            access_code='610001',
        )
        self.driver, self.driver_access = self.create_employee_with_access(
            'Иванов Сергей Петрович',
            self.driver_role,
            phone='+79000000001',
            access_code='210001',
            personnel_number='Т-001',
            position='Водитель самосвала',
            rotation='Бригада 1',
        )

        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.truck_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='БелАЗ тестовый',
            fuel_capacity_limit_l='2000',
        )
        self.truck_1 = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='Т-01',
        )
        self.truck_2 = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='Т-02',
        )
        self.original_assignment, _created = set_active_equipment_assignment(
            employee=self.driver,
            role=self.driver_role,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.deputy,
        )

        dormitory = Dormitory.objects.create(number='Тест')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок')
        section = DormitorySection.objects.create(block=block, name='Секция')
        DriverPrimaryRegistration.objects.create(
            employee=self.driver,
            dormitory_section=section,
        )

        self.authenticate(self.client, self.deputy_access)

    def create_employee_with_access(
        self,
        full_name,
        role,
        *,
        phone,
        access_code,
        **employee_fields,
    ):
        employee = Employee.objects.create(
            full_name=full_name,
            phone=phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
            **employee_fields,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee, access

    def authenticate(self, client, access):
        session = client.session
        session['employee_access_id'] = access.id
        session.save()

    def create_draft(self):
        plan, _created = get_or_create_crew_draft(
            role=self.driver_role,
            actor=self.deputy,
        )
        return plan

    def crew_excel_bytes(self, rows):
        workbook = Workbook()
        workbook.active.title = 'Старая версия'
        sheet = workbook.create_sheet('Лист1')
        sheet.append([None, None, 'смена 1', 'смена 2'])
        for row in rows:
            sheet.append(row)
        data = BytesIO()
        workbook.save(data)
        return data.getvalue()

    def upload_crew_excel(self, rows, *, work_date=None, confirmation=''):
        payload = rows if isinstance(rows, bytes) else self.crew_excel_bytes(rows)
        return self.client.post(
            reverse('deputy_mining_manager_import'),
            {
                'work_date': (work_date or production_work_date()).isoformat(),
                'workbook': SimpleUploadedFile('Расстановка.xlsx', payload),
                'draft_confirmation': confirmation,
            },
        )

    def test_excel_import_publishes_a_swap_and_repeat_is_a_no_op(self):
        second, _ = self.create_employee_with_access(
            'Петров Николай Иванович',
            self.driver_role,
            phone='+79000000011',
            access_code='210011',
            position='Водитель самосвала',
        )
        set_active_equipment_assignment(
            employee=second, role=self.driver_role, equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_2, assigned_by=self.deputy,
        )
        rows = [
            ['Самосвал БЕЛАЗ', self.truck_1.garage_number, 'Петров Н. И.', self.driver.full_name],
            ['Автокран', '103', None, 'Машинист крана'],
        ]
        response = self.upload_crew_excel(rows)
        self.assertContains(response, 'Расстановка опубликована')
        self.assertContains(response, 'Автокран')
        plan = CrewPlan.objects.get(role=self.driver_role, status=CrewPlanStatus.PUBLISHED)
        self.assertEqual(plan.published_by, self.deputy)
        self.assertEqual(get_active_equipment_assignment(second, 'driver').shift_type, WorkShiftType.SHIFT_1)
        self.assertEqual(get_active_equipment_assignment(self.driver, 'driver').shift_type, WorkShiftType.SHIFT_2)
        self.assertContains(self.upload_crew_excel(rows), 'Назначения уже совпадают')
        self.assertEqual(CrewPlan.objects.filter(role=self.driver_role, published_at__isnull=False).count(), 1)

    def test_auxiliary_fleet_tabs_and_publication(self):
        from .crew_categories import AUXILIARY_CATEGORIES

        call_command('prepare_deputy_fleet')
        call_command('prepare_deputy_fleet')
        for code, category in AUXILIARY_CATEGORIES.items():
            with self.subTest(category=code):
                response = self.client.get(reverse('deputy_mining_manager_placement'), {'role': code})
                payload = response.context['planning_payload']
                self.assertEqual(len(payload['categories']), 10)
                self.assertEqual(len(payload['rows']), len(category.numbers))
                self.assertTrue(all(len(row['slots']) == 2 for row in payload['rows']))
                self.assertFalse(payload['role']['supports_secondary'])
                self.assertNotIn(self.driver.id, {e['id'] for e in payload['employees']})
        self.driver.base_specialization = ProductionSpecialization.objects.get(code='bulldozer_operator')
        self.driver.save(update_fields=['base_specialization'])
        self.original_assignment.ended_at = timezone.now()
        self.original_assignment.save(update_fields=['ended_at'])
        rows = [['Колесник', 1, None, self.driver.full_name], ['Бульдозер', 1, 'Снять', None]]
        self.assertContains(self.upload_crew_excel(rows), 'Расстановка опубликована')
        plan = CrewPlan.objects.get(role__code='crew_wheel_dozer', status=CrewPlanStatus.PUBLISHED)
        slot = plan.slots.get(employee=self.driver)
        self.assertEqual(slot.shift_type, WorkShiftType.SHIFT_2)
        self.assertEqual(slot.equipment.equipment_type.name, 'Колёсный бульдозер')
        report = self.client.get(reverse('deputy_mining_manager_placement'), {'role': plan.role.code, 'plan': plan.id})
        self.assertFalse(report.context['planning_payload']['plan']['editable'])
        trucks = self.client.get(reverse('deputy_mining_manager_placement')).context['planning_payload']
        robots = [row['equipment'] for row in trucks['rows'] if row['equipment']['is_robot']]
        self.assertEqual([item['label'] for item in robots], ['53 · Робот'])

    def test_reported_primary_visible_in_publication_without_operational_assignment(self):
        call_command('prepare_deputy_fleet')
        before = list(EmployeeAccess.objects.values_list('id', 'role_id', 'is_active'))
        book = self.crew_excel_bytes([['Каток', '', self.driver.full_name, None]])
        self.assertContains(self.upload_crew_excel(book), 'специализация', status_code=400)
        request = {
            'work_date': production_work_date().isoformat(), 'factual': '1',
            'workbook': SimpleUploadedFile('Расстановка.xlsx', book),
        }
        self.assertContains(self.client.post(reverse('deputy_mining_manager_import'), request), 'Расстановка опубликована')
        plan = CrewPlan.objects.get(role__code='crew_roller', status=CrewPlanStatus.PUBLISHED)
        slot = plan.slots.get(shift_type=WorkShiftType.SHIFT_1)
        self.assertEqual(slot.reported_employee, self.driver)
        self.assertIsNone(slot.employee_id)
        self.assertFalse(EquipmentAssignment.objects.filter(role=plan.role).exists())
        self.assertEqual(before, list(EmployeeAccess.objects.values_list('id', 'role_id', 'is_active')))
        for params in [{'role': plan.role.code, 'plan': plan.pk}, {'role': plan.role.code}]:
            page = self.client.get(reverse('deputy_mining_manager_placement'), params)
            payload = page.context['planning_payload']
            self.assertEqual(payload['rows'][0]['slots'][0]['reported_name'], self.driver.full_name)
            self.assertEqual(payload['summary']['assigned_count'], 1)
        clear = self.upload_crew_excel([['Каток', '', 'Снять', None]])
        self.assertContains(clear, 'Расстановка опубликована')
        self.assertFalse(CrewPlan.objects.filter(role=plan.role, status=CrewPlanStatus.PUBLISHED, slots__reported_employee=self.driver).exists())

    def test_reported_truck_driver_has_no_operational_grant(self):
        person = Employee.objects.create(full_name='Тестовый водитель без специализации', status=Employee.Status.ACTIVE)
        response = self.client.post(reverse('deputy_mining_manager_import'), {
            'work_date': production_work_date().isoformat(), 'factual': '1',
            'workbook': SimpleUploadedFile('Расстановка.xlsx', self.crew_excel_bytes([
                ['Самосвал', self.truck_2.garage_number, person.full_name, None],
            ])),
        })
        self.assertContains(response, 'Расстановка опубликована')
        slot = CrewPlanSlot.objects.get(plan__status=CrewPlanStatus.PUBLISHED, reported_employee=person)
        self.assertEqual(slot.equipment, self.truck_2)
        self.assertIsNone(slot.employee_id)
        self.assertFalse(EquipmentAssignment.objects.filter(employee=person).exists())
        self.assertFalse(EmployeeAccess.objects.filter(employee=person).exists())

    def test_excel_import_rolls_back_all_categories_on_an_invalid_row(self):
        response = self.upload_crew_excel([
            ['Самосвал', self.truck_1.garage_number, 'Снять', None],
            ['Экскаватор 3000', '999', self.driver.full_name, None],
        ])
        self.assertContains(response, 'C3. Номер техники', status_code=400)
        self.assertFalse(CrewPlan.objects.filter(published_at__isnull=False).exists())
        self.original_assignment.refresh_from_db()
        self.assertIsNone(self.original_assignment.ended_at)

    def test_excel_import_publishes_excavator_and_truck_plans_together(self):
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        excavator_model = EquipmentModel.objects.create(
            equipment_type=excavator_type, name='Экскаватор 3000',
        )
        excavator = Equipment.objects.create(
            equipment_type=excavator_type, model=excavator_model, garage_number='3',
        )
        operator, _ = self.create_employee_with_access(
            'Машинист Александр Викторович',
            self.excavator_role,
            phone='+79000000012',
            access_code='210012',
            position='Машинист экскаватора',
        )
        trainee, _ = self.create_employee_with_access(
            'Машинист Иван Викторович',
            self.excavator_role,
            phone='+79000000014',
            access_code='210014',
            position='Машинист экскаватора',
        )
        response = self.upload_crew_excel([
            ['Самосвал', self.truck_1.garage_number, 'Снять', self.driver.full_name],
            ['Экскаватор 3000', '003', None, f'{operator.full_name}\n{trainee.full_name}'],
        ])
        self.assertContains(response, 'Расстановка опубликована')
        self.assertEqual(CrewPlan.objects.filter(status=CrewPlanStatus.PUBLISHED).count(), 2)
        assignment = get_active_equipment_assignment(operator, 'excavator_operator')
        self.assertEqual(assignment.equipment, excavator)
        self.assertEqual(assignment.shift_type, WorkShiftType.SHIFT_2)
        slot = CrewPlanSlot.objects.get(
            plan__status=CrewPlanStatus.PUBLISHED,
            equipment=excavator,
            shift_type=WorkShiftType.SHIFT_2,
        )
        self.assertEqual(slot.secondary_employee, trainee)
        self.assertIsNone(get_active_equipment_assignment(trainee, 'excavator_operator'))

    def test_excavator_second_place_distinguishes_assistant_and_trainee(self):
        excavator = Equipment.objects.create(
            equipment_type=EquipmentType.objects.create(name='Экскаватор'),
            garage_number='3',
        )
        operator, _ = self.create_employee_with_access(
            'Машинист Иван Викторович',
            self.excavator_role,
            phone='+79000000014',
            access_code='210014',
            position='Машинист экскаватора',
        )
        assistant = Employee.objects.create(
            full_name='Помощник Иван Викторович',
            status=Employee.Status.ACTIVE,
            base_specialization=ProductionSpecialization.objects.get(code='assistant_excavator_operator'),
        )
        plan, _created = get_or_create_crew_draft(role=self.excavator_role, actor=self.deputy)
        response = self.client.get(
            reverse('deputy_mining_manager_placement'),
            {'role': self.excavator_role.code},
            HTTP_HOST='localhost',
        )
        payload = response.context['planning_payload']
        self.assertEqual(payload['secondary_employee_label'], 'Помощник или стажёр')
        eligible_positions = {row['id']: row['eligible_positions'] for row in payload['employees']}
        self.assertEqual(eligible_positions[operator.id], ['primary', 'secondary'])
        self.assertEqual(eligible_positions[assistant.id], ['secondary'])

        for employee, label in ((operator, 'Стажёр'), (assistant, 'Помощник машиниста')):
            response, plan = self.autosave_slot(
                plan,
                equipment=excavator,
                shift_type=WorkShiftType.SHIFT_1,
                employee=employee,
                position='secondary',
            )
            slot = response.json()['payload']['rows'][0]['slots'][0]
            self.assertEqual(slot['secondary_label'], label)
            self.assertEqual(slot['secondary_employee']['id'], employee.id)

    def test_excel_import_preserves_unspecified_slots_and_rejects_duplicates(self):
        response = self.upload_crew_excel([['Самосвал', self.truck_2.garage_number, self.driver.full_name, None]])
        self.assertContains(response, 'Сотрудник оказался назначен дважды', status_code=400)
        self.assertFalse(CrewPlan.objects.filter(published_at__isnull=False).exists())
        self.assertEqual(get_active_equipment_assignment(self.driver, 'driver'), self.original_assignment)

    def test_excel_import_does_not_publish_hand_edited_draft(self):
        plan = self.create_draft()
        self.autosave_slot(plan, equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_1, employee=self.driver)
        response = self.upload_crew_excel([['Самосвал', self.truck_1.garage_number, self.driver.full_name, None]])
        self.assertContains(response, 'есть ручные правки', status_code=409)
        self.assertContains(response, 'Заменить черновик и опубликовать', status_code=409)
        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.DRAFT)
        self.assertEqual(plan.slots.get(equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_1).employee, self.driver)

    def test_excel_import_replaces_draft_and_publishes_only_excel_changes(self):
        plan = self.create_draft()
        self.autosave_slot(plan, equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_1, employee=self.driver)
        workbook = self.crew_excel_bytes([
            ['Самосвал', self.truck_1.garage_number, 'Снять', self.driver.full_name],
        ])
        preview = self.upload_crew_excel(workbook)
        self.assertEqual(preview.status_code, 409)
        self.assertFalse(CrewPlan.objects.filter(status=CrewPlanStatus.PUBLISHED).exists())
        response = self.upload_crew_excel(workbook, confirmation=preview.context['draft_confirmation'])
        self.assertContains(response, 'Расстановка опубликована')
        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.PUBLISHED)
        self.assertIsNone(plan.slots.get(equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_1).employee)
        current = get_active_equipment_assignment(self.driver, 'driver')
        self.assertEqual((current.equipment, current.shift_type), (self.truck_1, WorkShiftType.SHIFT_2))

    def test_excel_import_requires_confirmation_again_if_file_or_draft_changed(self):
        plan = self.create_draft()
        self.autosave_slot(plan, equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_1, employee=self.driver)
        workbook = self.crew_excel_bytes([['Самосвал', self.truck_1.garage_number, 'Снять', self.driver.full_name]])
        preview = self.upload_crew_excel(workbook)
        token = preview.context['draft_confirmation']
        changed_file = self.crew_excel_bytes([['Самосвал', self.truck_1.garage_number, 'Снять', None]])
        self.assertContains(
            self.upload_crew_excel(changed_file, confirmation=token),
            'Подтверди замену заново',
            status_code=409,
        )
        plan.refresh_from_db()
        self.autosave_slot(plan, equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_2, employee=self.driver)
        response = self.upload_crew_excel(workbook, confirmation=token)
        self.assertContains(response, 'Подтверди замену заново', status_code=409)
        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.DRAFT)
        self.assertEqual(plan.slots.get(equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_2).employee, self.driver)
        self.assertEqual(get_active_equipment_assignment(self.driver, 'driver'), self.original_assignment)

    def test_excel_import_keeps_the_draft_if_confirmed_workbook_has_an_error(self):
        plan = self.create_draft()
        self.autosave_slot(plan, equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_1, employee=self.driver)
        workbook = self.crew_excel_bytes([['Самосвал', '999', self.driver.full_name, None]])
        preview = self.upload_crew_excel(workbook)
        response = self.upload_crew_excel(workbook, confirmation=preview.context['draft_confirmation'])
        self.assertContains(response, 'C2. Номер техники', status_code=400)
        self.assertFalse(CrewPlan.objects.filter(status=CrewPlanStatus.PUBLISHED).exists())
        self.assertEqual(plan.slots.get(equipment=self.truck_2, shift_type=WorkShiftType.SHIFT_1).employee, self.driver)

    def test_excel_import_rejects_an_old_date_and_other_roles(self):
        url = reverse('deputy_mining_manager_import')
        self.assertContains(self.client.get(url), 'Загрузить и опубликовать')
        response = self.upload_crew_excel(
            [['Самосвал', self.truck_1.garage_number, 'Снять', None]],
            work_date=production_work_date() - timedelta(days=1),
        )
        self.assertContains(response, 'Производственные сутки изменились', status_code=400)
        self.authenticate(self.client, self.driver_access)
        self.assertRedirects(self.client.get(url), reverse('role_home'), fetch_redirect_response=False)
        response = self.upload_crew_excel([['Самосвал', self.truck_1.garage_number, 'Снять', None]])
        self.assertRedirects(response, reverse('role_home'), fetch_redirect_response=False)
        self.assertFalse(CrewPlan.objects.filter(published_at__isnull=False).exists())

    def autosave_slot(self, plan, *, equipment, shift_type, employee, position='primary'):
        response = self.client.post(
            reverse('deputy_mining_manager_slot'),
            data=json.dumps({
                'plan_id': plan.id,
                'expected_version': plan.version,
                'equipment_id': equipment.id,
                'shift_type': shift_type,
                'employee_id': employee.id if employee else None,
                'position': position,
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 200, response.content)
        plan.refresh_from_db()
        return response, plan

    def autosave_driver_on_second_truck(self, plan):
        return self.autosave_slot(
            plan,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.driver,
        )

    def publish(self, plan):
        response = self.client.post(
            reverse('deputy_mining_manager_publish'),
            data=json.dumps({
                'plan_id': plan.id,
                'expected_version': plan.version,
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def test_category_publication_statuses_are_independent_by_role(self):
        driver_plan = self.create_draft()
        self.publish(driver_plan)
        excavator_plan, _created = get_or_create_crew_draft(
            role=self.excavator_role,
            work_date=driver_plan.work_date,
            actor=self.deputy,
        )

        response = self.client.get(
            reverse('deputy_mining_manager_placement'),
            {'role': self.excavator_role.code, 'date': driver_plan.work_date.isoformat()},
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        categories = {
            category['code']: category
            for category in response.context['planning_payload']['categories']
        }
        self.assertEqual(categories['driver']['status_label'], 'Опубликовано')
        self.assertEqual(categories['driver']['revision'], driver_plan.revision)
        self.assertTrue(categories['driver']['published_at_label'])
        self.assertEqual(categories['excavator_operator']['status_label'], 'Черновик')
        self.assertEqual(categories['excavator_operator']['revision'], excavator_plan.revision)
        self.assertEqual(categories['excavator_operator']['published_at_label'], '')

    def test_profile_is_available_only_to_deputy_and_role_home_routes_to_board(self):
        response = self.client.get(reverse('role_home'), HTTP_HOST='localhost')
        self.assertRedirects(
            response,
            reverse('deputy_mining_manager_placement'),
            fetch_redirect_response=False,
        )

        driver_client = Client()
        self.authenticate(driver_client, self.driver_access)
        for view_name in (
            'deputy_mining_manager_placement',
            'deputy_mining_manager_reports',
        ):
            forbidden_page = driver_client.get(reverse(view_name), HTTP_HOST='localhost')
            self.assertRedirects(
                forbidden_page,
                reverse('role_home'),
                fetch_redirect_response=False,
            )

        plan = self.create_draft()
        forbidden_slot = driver_client.post(
            reverse('deputy_mining_manager_slot'),
            data=json.dumps({
                'plan_id': plan.id,
                'expected_version': plan.version,
                'equipment_id': self.truck_2.id,
                'shift_type': WorkShiftType.SHIFT_1,
                'employee_id': self.driver.id,
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )
        forbidden_publish = driver_client.post(
            reverse('deputy_mining_manager_publish'),
            data=json.dumps({'plan_id': plan.id, 'expected_version': plan.version}),
            content_type='application/json',
            HTTP_HOST='localhost',
        )
        self.assertEqual(forbidden_slot.status_code, 403)
        self.assertEqual(forbidden_publish.status_code, 403)

    def test_deputy_pages_expose_shared_pwa_metadata(self):
        for view_name in (
            'deputy_mining_manager_placement',
            'deputy_mining_manager_reports',
        ):
            response = self.client.get(reverse(view_name), HTTP_HOST='localhost')

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, reverse('deputy_mining_manager_manifest'))
            self.assertContains(response, 'rel="manifest"')
            self.assertContains(response, 'name="theme-color" content="#2E7D52"')
            self.assertContains(response, 'name="apple-mobile-web-app-capable" content="yes"')
            self.assertContains(response, 'deputy-mining-manager-180.png')
            self.assertContains(response, 'deputy-mining-manager-pwa-v1.js')

    def test_deputy_pages_expose_standard_header_subtitles(self):
        expected_subtitles = {
            'deputy_mining_manager_placement': (
                'Расстановка сотрудников по технике на две смены.'
            ),
            'deputy_mining_manager_reports': (
                'История опубликованных расстановок по технике.'
            ),
        }

        for view_name, subtitle in expected_subtitles.items():
            with self.subTest(view_name=view_name):
                response = self.client.get(reverse(view_name), HTTP_HOST='localhost')

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'<p>{subtitle}</p>', html=True)

    def test_deputy_can_submit_bounded_transfer_request_for_oup_review(self):
        haul_specialization = ProductionSpecialization.objects.get(code='haul_truck_driver')
        haul_specialization.access_role = self.driver_role
        haul_specialization.save(update_fields=['access_role'])
        kdm_specialization = ProductionSpecialization.objects.get(code='cargo_kdm_driver')
        generic_driver_position = PersonnelPosition.objects.get(name='Водитель автомобиля грузового')
        candidate = Employee.objects.create(
            full_name='Петров Водитель КДМ',
            phone='+79000000020',
            status=Employee.Status.ACTIVE,
            personnel_position=generic_driver_position,
            base_specialization=kdm_specialization,
            position=generic_driver_position.name,
        )
        watch_period = WatchPeriod.objects.create(
            name='Текущая вахта',
            starts_on=timezone.localdate(),
            ends_on=timezone.localdate(),
            is_active=True,
        )
        plan = self.create_draft()

        screen_response = self.client.get(
            f'{reverse("deputy_mining_manager_placement")}?role=driver',
            HTTP_HOST='localhost',
        )
        self.assertContains(screen_response, 'data-temporary-transfer-open', html=False)
        self.assertContains(screen_response, 'data-temporary-transfer-dialog', html=False)
        self.assertContains(screen_response, 'Действует только до конца выбранной текущей вахты.')

        response = self.client.post(
            reverse('deputy_mining_manager_temporary_transfer_request'),
            data=json.dumps({
                'plan_id': plan.id,
                'employee_id': candidate.id,
                'target_specialization_id': haul_specialization.id,
                'watch_period_id': watch_period.id,
                'reason': 'Подмена на самосвале',
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200, response.content)
        transfer = TemporaryWorkTransfer.objects.get(employee=candidate)
        self.assertEqual(transfer.status, TemporaryWorkTransfer.Status.REQUESTED)
        self.assertEqual(transfer.target_specialization, haul_specialization)
        self.assertEqual(transfer.requested_by, self.deputy)
        self.assertEqual(transfer.effective_to, watch_period.ends_on)

    def test_deputy_manifest_is_installable_landscape_pwa(self):
        response = Client().get(
            reverse('deputy_mining_manager_manifest'),
            HTTP_HOST='localhost',
        )
        manifest = json.loads(response.content.decode('utf-8'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/manifest+json; charset=utf-8')
        self.assertEqual(response['Cache-Control'], 'no-cache')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(manifest['id'], reverse('deputy_mining_manager_placement'))
        self.assertEqual(manifest['start_url'], reverse('deputy_mining_manager_placement'))
        self.assertEqual(manifest['scope'], '/deputy-mining-manager/')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['orientation'], 'landscape')
        self.assertTrue(any(icon.get('sizes') == '192x192' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('sizes') == '512x512' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('purpose') == 'maskable' for icon in manifest['icons']))
        self.assertEqual(
            {shortcut['url'] for shortcut in manifest['shortcuts']},
            {
                reverse('deputy_mining_manager_placement'),
                reverse('deputy_mining_manager_reports'),
            },
        )

    def test_deputy_pwa_icons_have_declared_dimensions(self):
        icon_root = Path(__file__).resolve().parents[1] / 'static' / 'img' / 'pwa'
        expected_sizes = {
            'deputy-mining-manager-180.png': (180, 180),
            'deputy-mining-manager-192.png': (192, 192),
            'deputy-mining-manager-512.png': (512, 512),
            'deputy-mining-manager-maskable-512.png': (512, 512),
        }

        for filename, expected_size in expected_sizes.items():
            with self.subTest(filename=filename):
                with Image.open(icon_root / filename) as icon:
                    self.assertEqual(icon.size, expected_size)
                    self.assertEqual(icon.format, 'PNG')

        manifest_response = Client().get(
            reverse('deputy_mining_manager_manifest'),
            HTTP_HOST='localhost',
        )
        manifest = json.loads(manifest_response.content.decode('utf-8'))
        for icon in manifest['icons']:
            with self.subTest(manifest_icon=icon['src']):
                static_path = icon['src'].removeprefix('/static/')
                resolved_path = finders.find(static_path)
                self.assertIsNotNone(resolved_path)
                declared_size = tuple(int(value) for value in icon['sizes'].split('x'))
                with Image.open(resolved_path) as image:
                    self.assertEqual(image.size, declared_size)

    def test_deputy_service_worker_keeps_planning_data_network_only(self):
        response = Client().get(
            reverse('deputy_mining_manager_service_worker'),
            HTTP_HOST='localhost',
        )
        script = response.content.decode('utf-8')
        core_assets = script.split('const CORE_ASSETS = [', 1)[1].split('];', 1)[0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/javascript; charset=utf-8')
        self.assertEqual(response['Cache-Control'], 'no-cache')
        self.assertEqual(response['Service-Worker-Allowed'], '/deputy-mining-manager/')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('deputy-mining-manager-desktop-shell-', script)
        self.assertIn(
            'const CACHE_NAME = "deputy-mining-manager-desktop-shell-v18";',
            script,
        )
        self.assertIn('/static/js/role-readonly.js', script)
        self.assertIn('key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME', script)
        self.assertIn('removeCachedPlanningDocuments()', script)
        self.assertIn('LEGACY_ROOT_FALLBACK_URL', script)
        self.assertIn('cacheName.startsWith(LEGACY_CACHE_PREFIX)', script)
        self.assertIn('cachedHtml.includes("data-deputy-planning-root")', script)
        self.assertIn('if (request.method !== "GET") return;', script)
        self.assertIn('networkOnlyNavigation(request)', script)
        self.assertIn('STATIC_ASSET_PATHS.has(url.pathname)', script)
        self.assertIn('networkFirstStatic(request)', script)
        self.assertIn('const cacheKey = canonicalStaticRequest(request);', script)
        self.assertIn('`${url.origin}${url.pathname}${url.search}`', script)
        self.assertNotIn('ignoreSearch', script)
        self.assertIn('fetch(request, { cache: "no-store" })', script)
        self.assertNotIn('cacheFirst(request)', script)
        self.assertIn('Для работы с расстановкой требуется сеть.', script)
        self.assertIn('SKIP_WAITING', script)
        self.assertIn('GET_VERSION', script)
        self.assertIn('/static/css/deputy-mining-manager-v3.css', core_assets)
        self.assertIn('/static/js/deputy-mining-manager-v3.js', core_assets)
        self.assertIn('/static/js/deputy-mining-manager-pwa-v1.js', core_assets)
        self.assertIn('/static/img/equipment/truck-green.png', core_assets)
        self.assertIn('/static/img/equipment/excavator-green.png', core_assets)
        self.assertNotIn('"/deputy-mining-manager/"', core_assets)
        self.assertNotIn('/reports/', core_assets)
        self.assertNotIn('/slot/', core_assets)
        self.assertNotIn('/publish/', core_assets)
        self.assertNotIn('/media/', core_assets)

        for static_url in re.findall(r'"(/static/[^"?]+)"', core_assets):
            with self.subTest(core_asset=static_url):
                self.assertIsNotNone(finders.find(static_url.removeprefix('/static/')))

    def test_deputy_pwa_registration_updates_without_forced_page_reload(self):
        registration_script = (
            Path(__file__).resolve().parents[1]
            / 'static'
            / 'js'
            / 'deputy-mining-manager-pwa-v1.js'
        ).read_text(encoding='utf-8')

        self.assertIn('navigator.serviceWorker.register(WORKER_URL, { scope: PWA_SCOPE })', registration_script)
        self.assertIn(
            f'var WORKER_URL = "{reverse("deputy_mining_manager_service_worker")}";',
            registration_script,
        )
        self.assertIn(
            f'var APP_PATH_PREFIX = "{reverse("deputy_mining_manager_placement")}";',
            registration_script,
        )
        self.assertIn('meta[name="role-app-scope"]', registration_script)
        self.assertIn('registrationRef.update()', registration_script)
        self.assertIn('visibilitychange', registration_script)
        self.assertIn('window.addEventListener("focus"', registration_script)
        self.assertIn('window.addEventListener("online"', registration_script)
        self.assertIn('SKIP_WAITING', registration_script)
        self.assertIn('removeCachedPlanningDocuments()', registration_script)
        self.assertNotIn('/mining-master/assignments/', registration_script)
        self.assertNotIn('controllerchange', registration_script)
        self.assertNotIn('location.reload', registration_script)

    def test_deputy_layout_uses_standard_header_full_scaled_viewport_and_balanced_columns(self):
        stylesheet = (
            Path(__file__).resolve().parents[1]
            / 'static'
            / 'css'
            / 'deputy-mining-manager-v3.css'
        ).read_text(encoding='utf-8')

        self.assertIn('--admin-console-header-height: 112px;', stylesheet)
        self.assertIn('--admin-console-header-height: 108px;', stylesheet)
        self.assertIn(
            'grid-template-columns: 40px var(--admin-header-utility-width);',
            stylesheet,
        )
        self.assertIn('justify-content: end;', stylesheet)
        self.assertIn(
            '.deputy-shell .admin-theme-button {\n'
            '    width: 40px;\n'
            '    min-width: 40px;\n'
            '    font-size: 0;',
            stylesheet,
        )
        self.assertIn('font-size: 32px;', stylesheet)
        self.assertIn('min-height: 44px;', stylesheet)
        self.assertIn(
            'top: calc(var(--admin-console-header-height) + 16px);',
            stylesheet,
        )
        self.assertIn(
            'height: calc((100dvh / var(--admin-interface-scale)) - var(--admin-console-header-height));',
            stylesheet,
        )

        header_template = (
            Path(__file__).resolve().parents[1]
            / 'templates'
            / 'includes'
            / 'deputy_mining_manager_header.html'
        ).read_text(encoding='utf-8')
        self.assertIn('data-theme-icon="sun"', header_template)
        self.assertIn('<p>{{ screen_subtitle|default:', header_template)
        self.assertNotRegex(
            header_template,
            r'<button\b[^>]*data-admin-theme-toggle[^>]*>\s*\S+.*?</button>',
        )
        self.assertIn(
            'grid-template-columns: clamp(360px, 21vw, 400px) minmax(0, 1fr);',
            stylesheet,
        )
        self.assertRegex(
            stylesheet,
            r'\.deputy-assignment-table th:first-child\s*\{\s*width:\s*24%;',
        )
        self.assertRegex(
            stylesheet,
            r'\.deputy-assignment-table th:not\(:first-child\)\s*\{\s*width:\s*38%;',
        )

    def test_get_board_builds_driver_plan_with_day_and_night_slots(self):
        response = self.client.get(
            reverse('deputy_mining_manager_placement'),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'assignments/deputy_mining_manager_placement.html')
        payload = response.context['planning_payload']
        self.assertEqual(payload['role']['code'], 'driver')
        self.assertEqual(payload['summary']['equipment_total'], 2)
        self.assertEqual(payload['summary']['assigned_count'], 1)
        self.assertEqual(payload['summary']['unfilled_count'], 3)
        self.assertEqual(len(payload['rows']), 2)
        self.assertTrue(all(len(row['slots']) == 2 for row in payload['rows']))
        self.assertEqual(
            payload['endpoints']['export'],
            reverse('deputy_mining_manager_export', args=[payload['plan']['id']]),
        )
        self.assertContains(response, 'data-export-excel', count=1)

    def test_board_and_excel_use_natural_equipment_number_order(self):
        self.truck_1.garage_number = '10'
        self.truck_1.save(update_fields=['garage_number'])
        self.truck_2.garage_number = '2'
        self.truck_2.save(update_fields=['garage_number'])

        response = self.client.get(
            reverse('deputy_mining_manager_placement'),
            HTTP_HOST='localhost',
        )
        payload = response.context['planning_payload']
        self.assertEqual(
            [row['equipment']['label'] for row in payload['rows']],
            ['2', '10'],
        )

        export_response = self.client.get(
            payload['endpoints']['export'],
            HTTP_HOST='localhost',
        )
        sheet = load_workbook(BytesIO(export_response.content))['Расстановка']
        self.assertEqual(
            [sheet.cell(row_index, 1).value for row_index in range(8, sheet.max_row + 1)],
            ['2', '10'],
        )

    def test_excel_export_contains_current_draft_and_is_ready_for_print(self):
        plan = self.create_draft()
        _response, plan = self.autosave_slot(
            plan,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_2,
            employee=self.driver,
        )

        response = self.client.get(
            reverse('deputy_mining_manager_export', args=[plan.id]),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('attachment; filename="crew_plan_driver_', response['Content-Disposition'])
        self.assertIn("filename*=UTF-8''", response['Content-Disposition'])
        self.assertEqual(response['Cache-Control'], 'private, no-store, max-age=0')
        workbook = load_workbook(BytesIO(response.content))
        sheet = workbook['Расстановка']
        self.assertEqual(sheet['A1'].value, 'COPPER RESOURCES · РАССТАНОВКА ПО ТЕХНИКЕ')
        self.assertIn('Статус: Черновик', sheet['C3'].value)
        rows_by_equipment = {
            sheet.cell(row_index, 1).value: row_index
            for row_index in range(8, sheet.max_row + 1)
        }
        truck_2_row = rows_by_equipment['Т-02']
        self.assertEqual(sheet.cell(truck_2_row, 3).value, 'Не назначен')
        self.assertIn('Иванов Сергей Петрович', sheet.cell(truck_2_row, 4).value)
        self.assertIn('Таб. № Т-001', sheet.cell(truck_2_row, 4).value)
        self.assertIn('Бригада 1', sheet.cell(truck_2_row, 4).value)
        self.assertEqual(sheet.page_setup.orientation, 'landscape')
        self.assertEqual(sheet.page_setup.paperSize, 9)
        self.assertEqual(sheet.page_setup.fitToWidth, 1)
        self.assertEqual(sheet.page_setup.fitToHeight, 0)
        self.assertEqual(sheet.freeze_panes, 'C8')
        self.assertEqual(sheet.print_title_rows, '$7:$7')
        self.assertIn('$A$1:$E$', str(sheet.print_area))
        self.assertEqual(sheet.oddFooter.center.text, 'Страница &P из &N')

    def test_excel_export_never_turns_employee_text_into_formula(self):
        self.driver.full_name = '=HYPERLINK("https://example.invalid";"Открыть")'
        self.driver.save(update_fields=['full_name'])
        plan = self.create_draft()

        response = self.client.get(
            reverse('deputy_mining_manager_export', args=[plan.id]),
            HTTP_HOST='localhost',
        )

        workbook = load_workbook(BytesIO(response.content), data_only=False)
        sheet = workbook['Расстановка']
        employee_cell = next(
            sheet.cell(row_index, 3)
            for row_index in range(8, sheet.max_row + 1)
            if sheet.cell(row_index, 1).value == 'Т-01'
        )
        self.assertTrue(employee_cell.value.startswith("'="))
        self.assertEqual(employee_cell.data_type, 's')

    def test_excel_export_is_restricted_to_deputy_and_known_plan(self):
        plan = self.create_draft()
        driver_client = Client()
        self.authenticate(driver_client, self.driver_access)

        forbidden_response = driver_client.get(
            reverse('deputy_mining_manager_export', args=[plan.id]),
            HTTP_HOST='localhost',
        )
        missing_response = self.client.get(
            reverse('deputy_mining_manager_export', args=[plan.id + 9999]),
            HTTP_HOST='localhost',
        )

        self.assertRedirects(
            forbidden_response,
            reverse('role_home'),
            fetch_redirect_response=False,
        )
        self.assertEqual(missing_response.status_code, 404)

    def create_watch_roster(self):
        work_date = production_work_date()
        period = WatchPeriod.objects.create(name='Вахта для продления', starts_on=work_date, ends_on=work_date)
        cycle = RotationCollectionCycle.objects.create(
            name='Сбор', target_watch_period=period, response_deadline=timezone.now(),
        )
        people = {}
        for index, (label, brigade, status, start, end) in enumerate([
            ('Своя', 2, '', 0, 0),
            ('Чужая', 3, '', 0, 0),
            ('Продление', 4, 'approved', 0, 0),
            ('Ожидание', 3, 'pending', -1, 1),
            ('Отказ', 3, 'rejected', -1, 1),
            ('Истекло', 4, 'approved', -2, -1),
            ('Будущее', 4, 'approved', 1, 2),
        ]):
            person, _ = self.create_employee_with_access(
                label, self.driver_role, phone=f'+7900010100{index}', access_code=f'29000{index}',
                rotation=f'Бригада {brigade}',
            )
            people[label] = person
            if status:
                response = RotationResponse.objects.create(cycle=cycle, employee=person)
                WatchExtensionCase.objects.create(
                    response=response, extension_start=work_date + timedelta(days=start),
                    extension_end=work_date + timedelta(days=end), decision_status=status,
                    decision_by=self.deputy if status != 'pending' else None,
                    decision_at=timezone.now() if status != 'pending' else None,
                )
        return people

    def test_watch_roster_includes_only_own_brigades_and_current_approved_extensions(self):
        work_date = production_work_date()
        people = self.create_watch_roster()
        other_deputy, other_access = self.create_employee_with_access(
            'Второй заместитель', self.deputy_role, phone='+79000102000', access_code='620001',
        )

        def test_identity(employee):
            return sha256(f'|{employee.full_name.casefold()}'.encode()).hexdigest()

        watch_map = {test_identity(self.deputy): ('1', '2'), test_identity(other_deputy): ('3', '4')}
        url = reverse('deputy_mining_manager_placement')
        with patch('assignments.deputy_views.DEPUTY_BRIGADES_BY_IDENTITY', watch_map):
            response = self.client.get(url)
            self.assertNotContains(response, 'data-brigade-filter="3"')
            payload = response.context['planning_payload']
            self.assertEqual({item['full_name'] for item in payload['employees']}, {'Своя', 'Продление'})
            plan = CrewPlan.objects.get(pk=payload['plan']['id'])
            blocked = self.client.post(reverse('deputy_mining_manager_slot'), {
                'plan_id': plan.pk, 'expected_version': plan.version, 'equipment_id': self.truck_2.pk,
                'shift_type': WorkShiftType.SHIFT_2, 'employee_id': people['Ожидание'].pk,
            }, content_type='application/json')
            self.assertEqual(blocked.status_code, 400)
            for shift in (WorkShiftType.SHIFT_1, WorkShiftType.SHIFT_2):
                saved, plan = self.autosave_slot(plan, equipment=self.truck_2, shift_type=shift, employee=people['Продление'])
                board = saved.json()['payload']
                self.assertEqual(board['brigades'], ['1', '2'])
                slot = next(slot for row in board['rows'] for slot in row['slots']
                            if slot['employee'] and slot['employee']['id'] == people['Продление'].pk)
                self.assertEqual(slot['shift_type'], shift)
                self.assertEqual(slot['employee']['extension_until'], work_date.strftime('%d.%m.%Y'))
            self.authenticate(self.client, other_access)
            response = self.client.get(url)
            self.assertNotContains(response, 'data-brigade-filter="1"')
            board = response.context['planning_payload']
            self.assertEqual({item['full_name'] for item in board['employees']},
                             {'Чужая', 'Ожидание', 'Отказ', 'Истекло', 'Будущее'})
            self.assertFalse(any(item['extension_until'] for item in board['employees']))
            self.assertEqual(board['summary']['assigned_count'], 2)

    def test_board_payload_exposes_brigade_and_record_details_without_new_schema(self):
        second_driver, _second_access = self.create_employee_with_access(
            'Петров Алексей Иванович',
            self.driver_role,
            phone='+79000000002',
            access_code='210002',
            rotation='Вахта 2',
        )
        unclassified_driver, _unclassified_access = self.create_employee_with_access(
            'Сидоров Николай Петрович',
            self.driver_role,
            phone='+79000000003',
            access_code='210003',
            rotation='Вахта 15/15',
        )
        schedule = WorkSchedule.objects.get(code='schedule_12')
        third_brigade_driver, _third_access = self.create_employee_with_access(
            'Кузнецов Андрей Сергеевич',
            self.driver_role,
            phone='+79000000004',
            access_code='210004',
            work_schedule=schedule,
            brigade_number=3,
            rotation='устаревшее значение',
        )
        fourth_brigade_driver, _fourth_access = self.create_employee_with_access(
            'Орлов Михаил Андреевич',
            self.driver_role,
            phone='+79000000005',
            access_code='210005',
            work_schedule=schedule,
            brigade_number=4,
        )

        response = self.client.get(
            reverse('deputy_mining_manager_placement'),
            HTTP_HOST='localhost',
        )

        payload = response.context['planning_payload']
        assigned_employee = next(
            slot['employee']
            for row in payload['rows']
            for slot in row['slots']
            if slot['employee'] and slot['employee']['id'] == self.driver.id
        )
        free_employee = next(item for item in payload['employees'] if item['id'] == second_driver.id)
        unclassified_employee = next(item for item in payload['employees'] if item['id'] == unclassified_driver.id)
        third_brigade_employee = next(item for item in payload['employees'] if item['id'] == third_brigade_driver.id)
        fourth_brigade_employee = next(item for item in payload['employees'] if item['id'] == fourth_brigade_driver.id)
        equipment = payload['rows'][0]['equipment']
        self.assertEqual(assigned_employee['brigade_code'], '1')
        self.assertEqual(assigned_employee['brigade_label'], 'Бригада 1')
        self.assertEqual(assigned_employee['phone'], '+79000000001')
        self.assertEqual(free_employee['brigade_code'], '2')
        self.assertEqual(unclassified_employee['brigade_code'], '')
        self.assertEqual(third_brigade_employee['brigade_code'], '3')
        self.assertEqual(third_brigade_employee['rotation_label'], schedule.name)
        self.assertEqual(fourth_brigade_employee['brigade_code'], '4')
        self.assertEqual(equipment['type_label'], 'Самосвал')
        self.assertEqual(equipment['ownership_label'], 'Собственная')
        self.assertEqual(equipment['status_label'], '')
        self.assertContains(response, 'data-brigade-filter="1"')
        self.assertContains(response, 'data-brigade-filter="2"')
        self.assertContains(response, 'data-brigade-filter="3"')
        self.assertContains(response, 'data-brigade-filter="4"')
        self.assertContains(response, 'data-record-dialog')

    def test_autosave_changes_draft_but_not_equipment_assignment(self):
        plan = self.create_draft()

        response, plan = self.autosave_driver_on_second_truck(plan)

        self.assertTrue(response.json()['ok'])
        self.original_assignment.refresh_from_db()
        self.assertIsNone(self.original_assignment.ended_at)
        self.assertEqual(
            get_active_equipment_assignment(self.driver, 'driver').equipment,
            self.truck_1,
        )
        self.assertEqual(
            plan.slots.get(
                equipment=self.truck_2,
                shift_type=WorkShiftType.SHIFT_1,
            ).employee,
            self.driver,
        )

    def test_autosave_moves_employee_between_equipment_and_shift(self):
        plan = self.create_draft()

        response, plan = self.autosave_slot(
            plan,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_2,
            employee=self.driver,
        )

        self.assertTrue(response.json()['ok'])
        self.assertIsNone(
            plan.slots.get(
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_1,
            ).employee,
        )
        self.assertEqual(
            plan.slots.get(
                equipment=self.truck_2,
                shift_type=WorkShiftType.SHIFT_2,
            ).employee,
            self.driver,
        )
        active_assignment = get_active_equipment_assignment(self.driver, 'driver')
        self.assertEqual(active_assignment.equipment, self.truck_1)
        self.assertEqual(active_assignment.shift_type, WorkShiftType.SHIFT_1)

    def test_autosave_clears_slot_and_returns_employee_to_free_pool(self):
        plan = self.create_draft()

        response, plan = self.autosave_slot(
            plan,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            employee=None,
        )

        self.assertIsNone(
            plan.slots.get(
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_1,
            ).employee,
        )
        free_employee_ids = {
            item['id'] for item in response.json()['payload']['employees']
        }
        self.assertIn(self.driver.id, free_employee_ids)
        self.assertEqual(
            get_active_equipment_assignment(self.driver, 'driver').equipment,
            self.truck_1,
        )

    def test_autosave_locks_only_slot_table_with_nullable_employee_join(self):
        plan = self.create_draft()

        with patch.object(
            CrewPlanSlot.objects,
            'select_for_update',
            wraps=CrewPlanSlot.objects.select_for_update,
        ) as slot_lock:
            self.autosave_slot(
                plan,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_1,
                employee=None,
            )

        self.assertTrue(any(
            call.kwargs.get('of') == ('self',)
            for call in slot_lock.call_args_list
        ))

    def test_publish_replaces_base_equipment_assignment(self):
        plan = self.create_draft()
        _response, plan = self.autosave_driver_on_second_truck(plan)

        response = self.publish(plan)

        self.assertTrue(response.json()['published'])
        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.PUBLISHED)
        published_export_url = response.json()['payload']['endpoints']['export']
        self.assertEqual(
            published_export_url,
            reverse('deputy_mining_manager_export', args=[plan.id]),
        )
        exported_sheet = load_workbook(BytesIO(self.client.get(
            published_export_url,
            HTTP_HOST='localhost',
        ).content))['Расстановка']
        self.assertIn('Статус: Опубликован', exported_sheet['C3'].value)
        self.original_assignment.refresh_from_db()
        self.assertIsNotNone(self.original_assignment.ended_at)
        self.assertEqual(self.original_assignment.ended_by, self.deputy)
        active_assignment = get_active_equipment_assignment(self.driver, 'driver')
        self.assertEqual(active_assignment.equipment, self.truck_2)
        self.assertEqual(active_assignment.shift_type, WorkShiftType.SHIFT_1)
        self.assertEqual(active_assignment.assigned_by, self.deputy)
        self.assertEqual(
            EquipmentAssignment.objects.filter(
                employee=self.driver,
                role=self.driver_role,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
            ).count(),
            1,
        )

    def test_publish_locks_only_slot_table_with_nullable_employee_joins(self):
        plan = self.create_draft()

        with patch.object(
            CrewPlanSlot.objects,
            'select_for_update',
            wraps=CrewPlanSlot.objects.select_for_update,
        ) as slot_lock:
            self.publish(plan)

        self.assertTrue(any(
            call.kwargs.get('of') == ('self',)
            for call in slot_lock.call_args_list
        ))

    def test_published_move_is_visible_in_admin_employee_card(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        _admin, admin_access = self.create_employee_with_access(
            'Администратор тестовый',
            admin_role,
            phone='+79000000099',
            access_code='110099',
        )
        admin_client = Client()
        self.authenticate(admin_client, admin_access)
        plan = self.create_draft()
        _response, plan = self.autosave_slot(
            plan,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_2,
            employee=self.driver,
        )

        self.publish(plan)
        response = admin_client.get(
            reverse('system_admin_employee_detail', args=[self.driver.id]),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        active_assignment = response.context['active_equipment_assignment']
        self.assertEqual(active_assignment.equipment, self.truck_2)
        self.assertEqual(active_assignment.shift_type, WorkShiftType.SHIFT_2)

    def test_published_removal_is_visible_in_admin_employee_card(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        _admin, admin_access = self.create_employee_with_access(
            'Администратор тестовый',
            admin_role,
            phone='+79000000099',
            access_code='110099',
        )
        admin_client = Client()
        self.authenticate(admin_client, admin_access)
        plan = self.create_draft()
        _response, plan = self.autosave_slot(
            plan,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            employee=None,
        )

        self.publish(plan)
        response = admin_client.get(
            reverse('system_admin_employee_detail', args=[self.driver.id]),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['active_equipment_assignment'])
        self.assertContains(response, 'Рабочее назначение не задано')

    def test_publish_does_not_change_snapshot_of_already_open_shift(self):
        open_shift = EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            opened_at=timezone.now(),
            opened_by=self.driver,
        )
        plan = self.create_draft()
        _response, plan = self.autosave_driver_on_second_truck(plan)

        self.publish(plan)

        open_shift.refresh_from_db()
        self.assertEqual(open_shift.equipment, self.truck_1)
        self.assertEqual(open_shift.shift_type, WorkShiftType.SHIFT_1)
        self.assertIsNone(open_shift.closed_at)
        self.assertEqual(
            get_active_equipment_assignment(self.driver, 'driver').equipment,
            self.truck_2,
        )

    def test_driver_login_sees_new_equipment_after_publication(self):
        plan = self.create_draft()
        _response, plan = self.autosave_driver_on_second_truck(plan)
        self.publish(plan)
        driver_client = Client()

        response = driver_client.post(
            reverse('login'),
            {
                'phone': '+7 (900) 000-00-01',
                'access_code': '210001',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(driver_client.session['employee_access_id'], self.driver_access.id)
        self.assertEqual(response.context['work_assignment_state'], 'assigned')
        self.assertEqual(response.context['work_assignment_equipment'], self.truck_2)
        self.assertEqual(response.context['work_assignment'].shift_type, WorkShiftType.SHIFT_1)

    def test_reports_page_lists_published_crew_plan(self):
        plan = self.create_draft()
        self.publish(plan)

        response = self.client.get(
            reverse('deputy_mining_manager_reports'),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'reports/deputy_mining_manager_reports.html')
        publications = response.context['publications']
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0].id, plan.id)
        self.assertEqual(publications[0].slot_count, 4)
        self.assertEqual(publications[0].assigned_count, 1)
        self.assertIn(f'plan={plan.id}', publications[0].url)

        detail_response = self.client.get(publications[0].url, HTTP_HOST='localhost')
        self.assertEqual(detail_response.status_code, 200)
        detail_payload = detail_response.context['planning_payload']
        self.assertEqual(detail_payload['plan']['id'], plan.id)
        self.assertEqual(detail_payload['plan']['status'], CrewPlanStatus.PUBLISHED)
        self.assertFalse(detail_payload['plan']['editable'])
        self.assertEqual(detail_payload['employees'], [])

    def test_deputy_rejects_not_activated_access(self):
        _, access = self.create_employee_with_access(
            'Депутат без активации T-021',
            self.deputy_role,
            phone='+79000000031',
            access_code='610031',
        )
        access.status = EmployeeAccess.Status.NOT_ACTIVATED
        access.save(update_fields=['status'])

        deputy_client = Client()
        self.authenticate(deputy_client, access)
        response = deputy_client.get(
            reverse('deputy_mining_manager_placement'),
            HTTP_HOST='localhost',
        )

        self.assertRedirects(
            response,
            reverse('role_home'),
            fetch_redirect_response=False,
        )

    def test_deputy_rejects_stale_session_generation(self):
        deputy_client = Client()
        self.authenticate(deputy_client, self.deputy_access)
        session = deputy_client.session
        session['active_role_access_id'] = self.deputy_access.id
        session['active_role_code'] = self.deputy_role.code
        session['active_role_login_at'] = (
            timezone.now() - timedelta(days=2)
        ).isoformat()
        session.save()
        self.deputy_access.last_login_at = timezone.now()
        self.deputy_access.save(update_fields=['last_login_at'])

        response = deputy_client.get(
            reverse('deputy_mining_manager_placement'),
            HTTP_HOST='localhost',
        )

        self.assertRedirects(
            response,
            reverse('role_home'),
            fetch_redirect_response=False,
        )
