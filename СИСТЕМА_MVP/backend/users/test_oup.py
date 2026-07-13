import shutil
import tempfile
from io import BytesIO
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from assignments.models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from references.models import Equipment, EquipmentModel, EquipmentType
from shifts.models import EmployeeShift, ShiftType

from .models import AdminActionLog, Employee, EmployeeAccess, Role


class OupWorkplaceTests(TestCase):
    def setUp(self):
        self.oup_role, _created = Role.objects.update_or_create(
            code='oup', defaults={'name': 'Специалист ОУП', 'is_active': True}
        )
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.oup_employee = Employee.objects.create(
            full_name='Иванова Анна Сергеевна',
            personnel_number='ОУП-001',
            phone='+79000000008',
            position='Специалист ОУП',
            department='ОУП',
            work_category=Employee.WorkCategory.OTHER,
            hired_at=timezone.localdate(),
            rotation='Вахта А',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.oup_access = EmployeeAccess.objects.create(
            employee=self.oup_employee,
            role=self.oup_role,
            access_code='800000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        session = self.client.session
        session['employee_access_id'] = self.oup_access.id
        session.save()

    def employee_payload(self, **overrides):
        payload = {
            'full_name': 'Петров Петр Петрович',
            'birth_date': '1988-03-12',
            'personnel_number': 'CR-1001',
            'phone': '+7 900 111-22-33',
            'position': 'Водитель автомобиля',
            'department': 'Горный участок №2',
            'work_category': Employee.WorkCategory.DRIVER,
            'hired_at': timezone.localdate().isoformat(),
            'rotation': 'Вахта 1',
            'comment': 'Карточка для производственного контура',
        }
        payload.update(overrides)
        return payload

    def start_shift(self):
        response = self.client.post(reverse('oup_shift_start'), {'next': reverse('oup_employees')})
        self.assertEqual(response.status_code, 302)
        return EmployeeShift.objects.get(employee=self.oup_employee, closed_at__isnull=True)

    def test_role_home_routes_oup_to_workplace(self):
        response = self.client.get(reverse('role_home'))
        self.assertRedirects(response, reverse('oup_home'), fetch_redirect_response=False)

    def test_oup_pages_reject_other_role(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        admin_employee = Employee.objects.create(full_name='Администратор', status=Employee.Status.ACTIVE)
        admin_access = EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='100000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = admin_access.id
        session.save()
        response = self.client.get(reverse('oup_employees'))
        self.assertRedirects(response, reverse('role_home'), fetch_redirect_response=False)

    def test_oup_shift_is_always_day_and_only_one_specialist_can_open_it(self):
        shift = self.start_shift()
        self.assertEqual(shift.shift_type, ShiftType.DAY)
        self.assertEqual(shift.workplace_code, 'oup')
        self.assertIsNone(shift.equipment_id)

        second_employee = Employee.objects.create(
            full_name='Сидорова Ольга Викторовна',
            phone='+79000000009',
            status=Employee.Status.ACTIVE,
        )
        second_access = EmployeeAccess.objects.create(
            employee=second_employee,
            role=self.oup_role,
            access_code='800001',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = second_access.id
        session.save()
        response = self.client.post(reverse('oup_shift_start'))
        self.assertRedirects(response, reverse('oup_employees'), fetch_redirect_response=False)
        self.assertFalse(EmployeeShift.objects.filter(employee=second_employee, closed_at__isnull=True).exists())

    def test_oup_does_not_treat_another_workplace_shift_as_its_own(self):
        other_shift = EmployeeShift.objects.create(
            employee=self.oup_employee,
            shift_type=ShiftType.DAY,
            opened_at=timezone.now(),
            opened_by=self.oup_employee,
        )
        response = self.client.post(reverse('oup_shift_start'))
        self.assertRedirects(response, reverse('oup_employees'), fetch_redirect_response=False)
        other_shift.refresh_from_db()
        self.assertIsNone(other_shift.closed_at)
        self.assertFalse(EmployeeShift.objects.filter(
            employee=self.oup_employee,
            workplace_code='oup',
            closed_at__isnull=True,
        ).exists())

        self.client.post(reverse('oup_shift_close'))
        other_shift.refresh_from_db()
        self.assertIsNone(other_shift.closed_at)

    def test_shift_return_target_rejects_external_url(self):
        response = self.client.post(reverse('oup_shift_start'), {'next': 'https://example.com/phishing'})
        self.assertRedirects(response, reverse('oup_employees'), fetch_redirect_response=False)

    def test_create_requires_open_shift(self):
        response = self.client.post(reverse('oup_employee_create'), self.employee_payload())
        self.assertRedirects(response, reverse('oup_employees'), fetch_redirect_response=False)
        self.assertFalse(Employee.objects.filter(personnel_number='CR-1001').exists())

    def test_employee_card_fields_are_disabled_without_oup_shift(self):
        employee = Employee.objects.create(
            full_name='Карточка Только Для Просмотра',
            personnel_number='READ-1',
            status=Employee.Status.ACTIVE,
        )
        response = self.client.get(reverse('oup_employee_detail', args=[employee.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].fields['full_name'].disabled)
        self.assertContains(response, 'Режим просмотра')

    def test_employee_dates_render_in_html_date_format(self):
        employee = Employee.objects.create(
            full_name='Сотрудник С Датами Полей',
            personnel_number='DATE-HTML-1',
            birth_date=timezone.localdate().replace(year=1990),
            hired_at=timezone.localdate(),
            status=Employee.Status.ACTIVE,
        )
        response = self.client.get(reverse('oup_employee_detail', args=[employee.id]))
        self.assertContains(response, f'value="{employee.birth_date:%Y-%m-%d}"')
        self.assertContains(response, f'value="{employee.hired_at:%Y-%m-%d}"')

    def test_photo_input_has_one_explicit_accessible_name(self):
        self.start_shift()
        response = self.client.get(reverse('oup_employee_create'))
        self.assertContains(response, 'aria-label="Выбрать фото сотрудника"')

    def test_create_employee_does_not_create_access_or_assignment(self):
        self.start_shift()
        response = self.client.post(
            reverse('oup_employee_create'),
            self.employee_payload(status=Employee.Status.DISMISSED, is_active=''),
        )
        employee = Employee.objects.get(personnel_number='CR-1001')
        self.assertRedirects(response, reverse('oup_employee_detail', args=[employee.id]), fetch_redirect_response=False)
        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
        self.assertEqual(employee.work_category, Employee.WorkCategory.DRIVER)
        self.assertFalse(EmployeeAccess.objects.filter(employee=employee).exists())
        self.assertFalse(EquipmentAssignment.objects.filter(employee=employee).exists())
        self.assertTrue(AdminActionLog.objects.filter(
            actor=self.oup_employee,
            action='ОУП: создан сотрудник',
            object_type='Employee',
            object_id=str(employee.id),
        ).exists())

    def test_duplicate_personnel_number_is_rejected(self):
        self.start_shift()
        Employee.objects.create(full_name='Существующий сотрудник', personnel_number='CR-1001')
        response = self.client.post(reverse('oup_employee_create'), self.employee_payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Сотрудник с таким табельным номером уже существует')

    def test_edit_logs_changes_by_stable_employee_id(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Старое Имя Сотрудника',
            personnel_number='CR-2001',
            phone='+79001112233',
            position='Водитель',
            department='Участок',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate(),
            rotation='Вахта 1',
            status=Employee.Status.ACTIVE,
        )
        response = self.client.post(
            reverse('oup_employee_detail', args=[employee.id]),
            self.employee_payload(full_name='Новое Имя Сотрудника', personnel_number='CR-2001'),
        )
        self.assertRedirects(response, reverse('oup_employee_detail', args=[employee.id]), fetch_redirect_response=False)
        log = AdminActionLog.objects.get(action='ОУП: изменена карточка сотрудника')
        self.assertEqual(log.object_id, str(employee.id))
        self.assertIn('Старое Имя Сотрудника', log.old_value)
        self.assertIn('Новое Имя Сотрудника', log.old_value)
        detail = self.client.get(reverse('oup_employee_detail', args=[employee.id]))
        self.assertContains(detail, 'изменена карточка сотрудника')

    def test_work_category_change_is_blocked_by_active_assignment(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Категория С Назначением',
            personnel_number='CR-ROLE-1',
            phone='+79001112233',
            position='Водитель',
            department='Участок',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate(),
            rotation='Вахта 1',
            status=Employee.Status.ACTIVE,
        )
        equipment_type = EquipmentType.objects.create(name='Самосвал')
        equipment_model = EquipmentModel.objects.create(equipment_type=equipment_type, name='БелАЗ')
        equipment = Equipment.objects.create(equipment_type=equipment_type, model=equipment_model, garage_number='ROLE-1')
        EquipmentAssignment.objects.create(
            employee=employee,
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.oup_employee,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        response = self.client.post(
            reverse('oup_employee_detail', args=[employee.id]),
            self.employee_payload(
                full_name=employee.full_name,
                personnel_number=employee.personnel_number,
                work_category=Employee.WorkCategory.EXCAVATOR_OPERATOR,
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'есть действующее назначение на технику')
        employee.refresh_from_db()
        self.assertEqual(employee.work_category, Employee.WorkCategory.DRIVER)

    def test_dismissal_is_blocked_by_open_shift(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Работающий Водитель',
            personnel_number='CR-3001',
            status=Employee.Status.ACTIVE,
        )
        EmployeeShift.objects.create(
            employee=employee,
            shift_type=ShiftType.DAY,
            opened_at=timezone.now(),
            opened_by=employee,
        )
        response = self.client.post(
            reverse('oup_employee_dismiss', args=[employee.id]),
            {'dismissed_at': timezone.localdate().isoformat(), 'reason': ''},
        )
        self.assertEqual(response.status_code, 200)
        employee.refresh_from_db()
        self.assertTrue(employee.is_active)
        self.assertContains(response, 'Увольнение сейчас заблокировано')

    def test_successful_dismissal_deactivates_access_and_closes_assignment(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Водитель Для Увольнения',
            personnel_number='CR-4001',
            phone='+79002223344',
            work_category=Employee.WorkCategory.DRIVER,
            status=Employee.Status.ACTIVE,
        )
        employee_access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code='200001',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        equipment_type = EquipmentType.objects.create(name='Самосвал')
        equipment_model = EquipmentModel.objects.create(equipment_type=equipment_type, name='БелАЗ')
        equipment = Equipment.objects.create(equipment_type=equipment_type, model=equipment_model, garage_number='101')
        assignment = EquipmentAssignment.objects.create(
            employee=employee,
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.oup_employee,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        response = self.client.post(
            reverse('oup_employee_dismiss', args=[employee.id]),
            {'dismissed_at': timezone.localdate().isoformat(), 'reason': 'Получено подтверждение из 1С'},
        )
        self.assertRedirects(response, reverse('oup_dismissed_employees'), fetch_redirect_response=False)
        employee.refresh_from_db()
        employee_access.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(employee.status, Employee.Status.DISMISSED)
        self.assertFalse(employee.is_active)
        self.assertFalse(employee_access.is_active)
        self.assertEqual(employee_access.status, EmployeeAccess.Status.DEACTIVATED)
        self.assertIsNotNone(assignment.ended_at)
        self.assertTrue(AdminActionLog.objects.filter(
            action='ОУП: уволен сотрудник', object_id=str(employee.id)
        ).exists())

    def test_deactivated_employee_can_be_formally_dismissed_by_oup(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Деактивированный Сотрудник',
            personnel_number='CR-DEACT-1',
            hired_at=timezone.localdate(),
            status=Employee.Status.DEACTIVATED,
            is_active=False,
        )
        response = self.client.post(
            reverse('oup_employee_dismiss', args=[employee.id]),
            {'dismissed_at': timezone.localdate().isoformat(), 'reason': ''},
        )
        self.assertRedirects(response, reverse('oup_dismissed_employees'), fetch_redirect_response=False)
        employee.refresh_from_db()
        self.assertEqual(employee.status, Employee.Status.DISMISSED)

    def test_dismissal_date_cannot_precede_hire_date(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Сотрудник С Датами',
            personnel_number='CR-DATE-1',
            hired_at=timezone.localdate(),
            status=Employee.Status.ACTIVE,
        )
        response = self.client.post(
            reverse('oup_employee_dismiss', args=[employee.id]),
            {'dismissed_at': (timezone.localdate() - timedelta(days=1)).isoformat(), 'reason': ''},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Дата увольнения не может быть раньше даты приема')
        employee.refresh_from_db()
        self.assertEqual(employee.status, Employee.Status.ACTIVE)

    def test_dismissal_clears_current_production_draft_before_seven(self):
        self.start_shift()
        employee = Employee.objects.create(
            full_name='Сотрудник В Текущем Черновике',
            personnel_number='CR-DRAFT-1',
            work_category=Employee.WorkCategory.DRIVER,
            hired_at=timezone.localdate() - timedelta(days=10),
            status=Employee.Status.ACTIVE,
        )
        equipment_type = EquipmentType.objects.create(name='Самосвал')
        equipment_model = EquipmentModel.objects.create(equipment_type=equipment_type, name='БелАЗ')
        equipment = Equipment.objects.create(equipment_type=equipment_type, model=equipment_model, garage_number='DRAFT-1')
        production_date = timezone.localdate() - timedelta(days=1)
        plan = CrewPlan.objects.create(
            work_date=production_date,
            role=self.driver_role,
            status=CrewPlanStatus.DRAFT,
            created_by=self.oup_employee,
        )
        slot = CrewPlanSlot.objects.create(
            plan=plan,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            employee=employee,
        )
        with patch('users.oup_services.production_work_date', return_value=production_date):
            response = self.client.post(
                reverse('oup_employee_dismiss', args=[employee.id]),
                {'dismissed_at': timezone.localdate().isoformat(), 'reason': ''},
            )
        self.assertRedirects(response, reverse('oup_dismissed_employees'), fetch_redirect_response=False)
        slot.refresh_from_db()
        self.assertIsNone(slot.employee_id)

    def test_oup_log_contains_only_current_specialist_actions(self):
        self.start_shift()
        AdminActionLog.objects.create(actor=None, action='ОУП: чужое действие', object_repr='Другой сотрудник')
        response = self.client.get(reverse('oup_logs'))
        self.assertContains(response, 'начата дневная смена')
        self.assertNotContains(response, 'чужое действие')

    def test_oup_log_is_paginated_without_hiding_older_actions(self):
        AdminActionLog.objects.bulk_create([
            AdminActionLog(
                actor=self.oup_employee,
                action='ОУП: тестовое действие',
                object_repr=f'Сотрудник {index}',
            )
            for index in range(55)
        ])
        response = self.client.get(reverse('oup_logs'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 55)
        self.assertEqual(len(response.context['logs']), 50)
        self.assertContains(response, 'Дальше')

        second_page = self.client.get(reverse('oup_logs'), {'page': 2})
        self.assertEqual(len(second_page.context['logs']), 5)

    def test_dismissed_registry_does_not_offer_employee_creation(self):
        self.start_shift()
        response = self.client.get(reverse('oup_dismissed_employees'))
        self.assertNotContains(response, '+ Добавить сотрудника')


class OupPhotoUploadTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix='oup-photo-tests-')
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        role, _created = Role.objects.update_or_create(
            code='oup', defaults={'name': 'Специалист ОУП', 'is_active': True}
        )
        employee = Employee.objects.create(
            full_name='Фотограф ОУП', phone='+79000000008', status=Employee.Status.ACTIVE
        )
        access = EmployeeAccess.objects.create(
            employee=employee, role=role, access_code='800000', status=EmployeeAccess.Status.ACTIVATED
        )
        session = self.client.session
        session['employee_access_id'] = access.id
        session.save()
        EmployeeShift.objects.create(
            employee=employee,
            shift_type=ShiftType.DAY,
            workplace_code='oup',
            opened_at=timezone.now(),
            opened_by=employee,
        )

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_photo_upload_is_saved_and_optimized(self):
        source = BytesIO()
        Image.new('RGB', (900, 700), color=(35, 120, 70)).save(source, format='PNG')
        photo = SimpleUploadedFile('employee.png', source.getvalue(), content_type='image/png')
        response = self.client.post(
            reverse('oup_employee_create'),
            {
                'full_name': 'Сотрудник С Фотографией',
                'personnel_number': 'PHOTO-1',
                'phone': '+79001234567',
                'position': 'Специалист',
                'department': 'ОУП',
                'work_category': Employee.WorkCategory.OTHER,
                'hired_at': timezone.localdate().isoformat(),
                'rotation': 'Вахта 1',
                'comment': '',
                'photo': photo,
            },
        )
        employee = Employee.objects.get(personnel_number='PHOTO-1')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(employee.photo.name.endswith('.jpg'))
        with Image.open(employee.photo.path) as saved:
            self.assertLessEqual(max(saved.size), 512)
