import re
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

from .forms import AdminEmployeeForm, PersonnelPositionReferenceForm
from .models import (
    Employee,
    EmployeeAccess,
    PersonnelDepartment,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
    WorkSchedule,
)
from .oup_forms import OupEmployeeForm


class UnifiedEmployeeCardTests(TestCase):
    def setUp(self):
        self.admin_role, _ = Role.objects.update_or_create(code='admin', defaults={'name': 'Администратор'})
        self.oup_role, _ = Role.objects.update_or_create(code='oup', defaults={'name': 'Специалист ОУП'})
        self.driver_role, _ = Role.objects.update_or_create(code='driver', defaults={'name': 'Водитель самосвала'})
        self.truck_specialization = ProductionSpecialization.objects.get(code='haul_truck_driver')
        self.truck_specialization.access_role = self.driver_role
        self.truck_specialization.save(update_fields=['access_role'])
        self.truck_position = PersonnelPosition.objects.get(
            name='Водитель автомобиля, занятый на транспортировании горной массы в технологическом процессе',
        )
        self.department = PersonnelDepartment.objects.get(code='department_001')
        self.schedule = WorkSchedule.objects.get(code='schedule_12')
        self.admin_employee = Employee.objects.create(
            full_name='Администратор Системы',
            phone='+79000000001',
            status=Employee.Status.ACTIVE,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin_employee,
            role=self.admin_role,
            access_code='101010',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.employee = Employee.objects.create(
            full_name='Петров Петр',
            phone='+79001112233',
            personnel_position=self.truck_position,
            base_specialization=self.truck_specialization,
            position=self.truck_position.name,
            department=self.department.name,
            personnel_department=self.department,
            work_schedule=self.schedule,
            brigade_number=1,
            rotation=f'{self.schedule.name} Бригада №1',
            status=Employee.Status.ACTIVE,
        )
        EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.driver_role,
            access_code='202020',
            status=EmployeeAccess.Status.ACTIVATED,
        )

    def login_as(self, access):
        session = self.client.session
        session['employee_access_id'] = access.id
        session.save()

    def test_admin_create_and_edit_use_the_same_template(self):
        self.login_as(self.admin_access)
        create_response = self.client.get(reverse('system_admin_employee_create'))
        edit_response = self.client.get(reverse('system_admin_employee_detail', args=[self.employee.id]))

        self.assertTemplateUsed(create_response, 'users/employee_card.html')
        self.assertTemplateUsed(edit_response, 'users/employee_card.html')
        self.assertContains(create_response, 'data-copy-card')
        self.assertContains(create_response, 'data-print-card')
        self.assertContains(create_response, 'form="employee-card-form"', html=False)
        self.assertEqual(create_response.content.decode().count('>Создать сотрудника</button>'), 1)
        self.assertContains(create_response, 'id="employee-status-readonly"', html=False)
        self.assertContains(create_response, 'name="personnel_department"', html=False)
        self.assertContains(create_response, 'name="work_schedule"', html=False)
        self.assertContains(create_response, 'name="brigade_number"', html=False)
        self.assertContains(create_response, 'data-brigade-count="0"', html=False)
        self.assertContains(create_response, 'value="Активен"', html=False)
        self.assertNotContains(create_response, 'id="id_status"', html=False)
        self.assertNotContains(create_response, 'employee-card-submit-row')
        self.assertContains(create_response, '>Выберите доступ</option>', html=False)
        self.assertContains(create_response, '<details class="employee-card-section employee-card-notes"', html=False)
        self.assertContains(edit_response, 'data-copy-target="#id_phone"', html=False)

    def test_oup_create_and_edit_use_the_same_template_as_admin(self):
        oup_employee = Employee.objects.create(
            full_name='Иванова Анна',
            phone='+79000000002',
            status=Employee.Status.ACTIVE,
        )
        oup_access = EmployeeAccess.objects.create(
            employee=oup_employee,
            role=self.oup_role,
            access_code='303030',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.login_as(oup_access)
        self.client.post(reverse('oup_shift_start'), {'next': reverse('oup_employees')})

        create_response = self.client.get(reverse('oup_employee_create'))
        edit_response = self.client.get(reverse('oup_employee_detail', args=[self.employee.id]))

        self.assertTemplateUsed(create_response, 'users/employee_card.html')
        self.assertTemplateUsed(edit_response, 'users/employee_card.html')
        self.assertContains(create_response, 'employee-card-unified.css')
        self.assertContains(create_response, 'form="employee-card-form"', html=False)
        self.assertEqual(create_response.content.decode().count('>Создать сотрудника</button>'), 1)
        self.assertNotContains(create_response, 'employee-card-submit-row')
        self.assertContains(create_response, 'id="employee-status-readonly"', html=False)
        self.assertContains(create_response, 'value="Активен"', html=False)
        self.assertNotContains(create_response, 'id="id_status"', html=False)
        self.assertContains(create_response, 'data-copy-target="#id_access_role"', html=False)
        self.assertContains(create_response, '>Выберите доступ</option>', html=False)
        self.assertNotContains(
            create_response,
            'Именно специализация определяет доступность для расстановки и подходящее приложение.',
        )
        self.assertContains(
            create_response,
            '<select id="employee-assignment-shift-readonly" disabled',
            html=False,
        )
        self.assertContains(
            create_response,
            '<select id="employee-assignment-equipment-readonly" disabled',
            html=False,
        )
        self.assertContains(edit_response, 'employee-card-unified.js')

    def test_personnel_number_is_not_a_visible_card_field(self):
        self.login_as(self.admin_access)
        response = self.client.get(reverse('system_admin_employee_detail', args=[self.employee.id]))

        self.assertNotContains(response, 'Табельный номер')
        self.assertContains(response, 'type="hidden" name="personnel_number"', html=False)

    def test_phone_validation_is_identical_for_admin_and_oup(self):
        common = {
            'full_name': 'Сидоров Сидор',
            'phone': '+7 800 111-22-33',
            'status': Employee.Status.ACTIVE,
            'personnel_position': self.truck_position.id,
            'base_specialization': self.truck_specialization.id,
        }
        admin_form = AdminEmployeeForm(data={**common, 'role': self.driver_role.id})
        oup_form = OupEmployeeForm(data=common)

        self.assertFalse(admin_form.is_valid())
        self.assertFalse(oup_form.is_valid())
        self.assertEqual(admin_form.errors['phone'], oup_form.errors['phone'])

    def test_phone_is_normalized_before_save(self):
        form = AdminEmployeeForm(data={
            'full_name': 'Смирнов Семен',
            'phone': '8 (900) 555-44-33',
            'status': Employee.Status.ACTIVE,
            'personnel_position': self.truck_position.id,
            'base_specialization': self.truck_specialization.id,
            'role': self.driver_role.id,
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['phone'], '+79005554433')

    def test_personnel_catalogue_is_available_from_admin_references(self):
        self.login_as(self.admin_access)

        registry = self.client.get(reverse('system_admin_references'))
        detail = self.client.get(
            reverse('system_admin_reference_detail', args=['personnel-positions']),
        )

        self.assertContains(registry, 'Кадровые должности')
        self.assertContains(registry, 'Производственные специализации')
        self.assertContains(registry, 'Подразделения')
        self.assertContains(registry, 'Графики работы')
        self.assertContains(detail, 'Официальные должности из 1С')
        self.assertContains(detail, 'Разрешенные производственные специализации')

    def test_personnel_position_reference_rejects_default_outside_allowed_list(self):
        excavator_specialization = ProductionSpecialization.objects.get(code='excavator_operator')
        form = PersonnelPositionReferenceForm(data={
            'name': 'Тестовая кадровая должность',
            'code': 'test-personnel-position',
            'requires_specialization': 'on',
            'allowed_specializations': [self.truck_specialization.id],
            'default_specialization': excavator_specialization.id,
            'is_active': 'on',
        })

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors['default_specialization'],
            ['Специализация по умолчанию должна входить в разрешенный список.'],
        )

    def test_shared_employee_card_shells_use_new_cache_versions(self):
        expected_versions = {
            'system_admin_service_worker': 'system-admin-shell-v14',
            'oup_service_worker': 'oup-shell-v16',
        }

        for view_name, expected_version in expected_versions.items():
            response = self.client.get(reverse(view_name))

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, expected_version)
            self.assertContains(response, 'new Request(url, { cache: "reload" })')
            self.assertContains(response, 'fetch(request, { cache: "reload" })')

    def test_shared_desktop_header_uses_standard_geometry(self):
        static_css = Path(__file__).resolve().parents[1] / 'static' / 'css'
        app_stylesheet = (static_css / 'app.css').read_text(encoding='utf-8')
        oup_stylesheet = (static_css / 'oup-workplace-v1.css').read_text(encoding='utf-8')
        oup_period_stylesheet = (static_css / 'oup-work-period-v1.css').read_text(encoding='utf-8')
        oup_responsive_stylesheet = (static_css / 'oup-responsive-layout-v2.css').read_text(
            encoding='utf-8',
        )

        self.assertIn('--admin-console-header-height: 112px;', app_stylesheet)
        self.assertIn('--admin-header-control-height: 40px;', app_stylesheet)
        self.assertIn('--admin-header-icon-size: 40px;', app_stylesheet)
        self.assertIn('--admin-header-identity-column: minmax(210px, 260px);', app_stylesheet)
        self.assertIn('--admin-header-actions-column: minmax(230px, 300px);', app_stylesheet)
        self.assertIn('--admin-header-title-column: clamp(220px, 18vw, 360px);', app_stylesheet)
        self.assertIn('--admin-header-title-column: 280px;', app_stylesheet)
        self.assertIn('--admin-header-title-column: 440px;', app_stylesheet)
        self.assertIn('--admin-header-utility-width: 96px;', app_stylesheet)
        self.assertIn('grid-template-columns: 52px minmax(0, 1fr);', app_stylesheet)
        self.assertIn(
            'grid-template-columns: var(--admin-header-title-column) minmax(0, 1fr);',
            app_stylesheet,
        )
        self.assertIn('font-size: 32px;', app_stylesheet)
        self.assertIn('--oup-header-control-height: 40px;', oup_stylesheet)
        self.assertIn('--oup-header-control-font-size: 13px;', oup_stylesheet)
        self.assertNotIn('clamp(260px, 22vw, 460px)', oup_stylesheet)
        self.assertNotIn('clamp(260px, 22vw, 460px)', oup_responsive_stylesheet)
        self.assertIn(
            'grid-template-columns: var(--oup-header-theme-size) var(--admin-header-utility-width);',
            oup_stylesheet,
        )
        self.assertNotIn('.oup-shift-state', oup_stylesheet)
        self.assertNotIn('.oup-shift-button', oup_stylesheet)
        self.assertIn('grid-template-columns: 12px minmax(0, 1fr) auto;', oup_period_stylesheet)
        self.assertIn('min-height: 58px;', oup_period_stylesheet)
        self.assertNotIn('min-height: 46px;', oup_period_stylesheet)
        self.assertIn(
            'grid-template-columns: 44px var(--admin-header-utility-width);',
            oup_stylesheet,
        )
        self.assertIn('--oup-header-control-height: 44px;', oup_stylesheet)

        include_root = Path(__file__).resolve().parents[1] / 'templates' / 'includes'
        header_template = (include_root / 'oup_header.html').read_text(encoding='utf-8')
        editing_template = (include_root / 'oup_editing_access.html').read_text(encoding='utf-8')
        self.assertNotIn('Рабочий период', header_template)
        self.assertNotIn("url 'oup_shift_start'", header_template)
        self.assertNotIn("url 'oup_shift_close'", header_template)
        self.assertIn('aria-label="Тема и выход"', header_template)
        self.assertIn("url 'oup_shift_start'", editing_template)
        self.assertIn("url 'oup_shift_close'", editing_template)
        self.assertIn('Включить редактирование', editing_template)
        self.assertIn('Завершить редактирование', editing_template)

        template_root = Path(__file__).resolve().parents[1] / 'templates' / 'users'
        for template_name in ('oup_employees.html', 'oup_employee_dismiss.html', 'oup_logs.html'):
            template = (template_root / template_name).read_text(encoding='utf-8')
            self.assertIn('oup-workplace-v1.css\' %}?v=20260718-4', template)
            self.assertIn('oup-work-period-v1.css\' %}?v=20260718-3', template)
            self.assertIn('oup-responsive-layout-v2.css\' %}?v=20260718-2', template)

    def test_standard_header_theme_buttons_are_icon_only(self):
        backend_root = Path(__file__).resolve().parents[1]
        user_templates = backend_root / 'templates' / 'users'
        templates = list(user_templates.glob('system_admin_*.html')) + [
            user_templates / 'employee_card.html',
            backend_root / 'templates' / 'includes' / 'oup_header.html',
        ]

        for template_path in templates:
            template = template_path.read_text(encoding='utf-8')
            matches = re.findall(
                r'(<button\b[^>]*data-admin-theme-toggle[^>]*>)(.*?)</button>',
                template,
                flags=re.DOTALL,
            )
            self.assertTrue(matches, template_path.name)
            for opening_tag, body in matches:
                self.assertIn('data-theme-icon="sun"', opening_tag)
                self.assertIn('aria-label=', opening_tag)
                self.assertEqual(body.strip(), '')

    def test_new_employee_cannot_reuse_an_existing_phone(self):
        form = AdminEmployeeForm(data={
            'full_name': 'Смирнов Семен',
            'phone': '8 (900) 111-22-33',
            'status': Employee.Status.ACTIVE,
            'personnel_position': self.truck_position.id,
            'base_specialization': self.truck_specialization.id,
            'role': self.driver_role.id,
        })

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors['phone'],
            ['Этот мобильный номер уже указан в карточке другого сотрудника.'],
        )

    def test_unchanged_legacy_phone_does_not_block_editing(self):
        duplicate = Employee.objects.create(
            full_name='Сотрудник Дубликат',
            phone='89001112233',
            status=Employee.Status.ACTIVE,
        )
        form = OupEmployeeForm(data={
            'full_name': duplicate.full_name,
            'phone': duplicate.phone,
            'status': duplicate.status,
        }, instance=duplicate)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['phone'], '+79001112233')

    def test_create_forms_force_active_employee_status(self):
        common = {
            'full_name': 'Смирнов Семен',
            'phone': '+79005554433',
            'status': Employee.Status.DEACTIVATED,
            'personnel_position': self.truck_position.id,
            'base_specialization': self.truck_specialization.id,
        }
        admin_form = AdminEmployeeForm(data={**common, 'role': self.driver_role.id})
        oup_form = OupEmployeeForm(data=common)

        self.assertTrue(admin_form.is_valid(), admin_form.errors)
        self.assertTrue(oup_form.is_valid(), oup_form.errors)
        self.assertEqual(admin_form.cleaned_data['status'], Employee.Status.ACTIVE)
        self.assertEqual(oup_form.cleaned_data['status'], Employee.Status.ACTIVE)

    def test_schedule_and_department_are_shared_structured_fields(self):
        common = {
            'full_name': 'Смирнов Семен',
            'phone': '+79005554433',
            'status': Employee.Status.ACTIVE,
            'personnel_position': self.truck_position.id,
            'base_specialization': self.truck_specialization.id,
            'personnel_department': self.department.id,
            'work_schedule': self.schedule.id,
            'brigade_number': 4,
        }
        admin_form = AdminEmployeeForm(data={**common, 'role': self.driver_role.id})
        oup_form = OupEmployeeForm(data=common)

        self.assertTrue(admin_form.is_valid(), admin_form.errors)
        self.assertTrue(oup_form.is_valid(), oup_form.errors)
        employee = admin_form.save()
        self.assertEqual(employee.personnel_department, self.department)
        self.assertEqual(employee.work_schedule, self.schedule)
        self.assertEqual(employee.brigade_number, 4)
        self.assertEqual(employee.department, self.department.name)
        self.assertEqual(employee.rotation, f'{self.schedule.name} Бригада №4')

    def test_brigade_must_belong_to_selected_schedule(self):
        two_brigade_schedule = WorkSchedule.objects.get(code='schedule_11')
        form = AdminEmployeeForm(data={
            'full_name': 'Смирнов Семен',
            'phone': '+79005554433',
            'status': Employee.Status.ACTIVE,
            'personnel_position': self.truck_position.id,
            'base_specialization': self.truck_specialization.id,
            'personnel_department': self.department.id,
            'work_schedule': two_brigade_schedule.id,
            'brigade_number': 3,
            'role': self.driver_role.id,
        })

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors['brigade_number'],
            [f'Для графика «{two_brigade_schedule}» доступны бригады с 1 по 2.'],
        )

    def test_schedule_without_brigades_is_valid(self):
        schedule = WorkSchedule.objects.get(code='individual_permanent_site')
        common = {
            'full_name': 'Смирнов Семен',
            'phone': '+79005554433',
            'status': Employee.Status.ACTIVE,
            'personnel_position': self.truck_position.id,
            'base_specialization': self.truck_specialization.id,
            'personnel_department': self.department.id,
            'work_schedule': schedule.id,
            'brigade_number': '',
        }
        admin_form = AdminEmployeeForm(data={**common, 'role': self.driver_role.id})
        oup_form = OupEmployeeForm(data=common)

        self.assertTrue(admin_form.is_valid(), admin_form.errors)
        self.assertTrue(oup_form.is_valid(), oup_form.errors)
        employee = admin_form.save()
        self.assertEqual(employee.work_schedule, schedule)
        self.assertIsNone(employee.brigade_number)
        self.assertEqual(employee.rotation, schedule.name)

    def test_schedule_without_brigades_rejects_brigade(self):
        schedule = WorkSchedule.objects.get(code='individual_permanent_site')
        form = AdminEmployeeForm(data={
            'full_name': 'Смирнов Семен',
            'phone': '+79005554433',
            'status': Employee.Status.ACTIVE,
            'personnel_position': self.truck_position.id,
            'base_specialization': self.truck_specialization.id,
            'personnel_department': self.department.id,
            'work_schedule': schedule.id,
            'brigade_number': 1,
            'role': self.driver_role.id,
        })

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors['brigade_number'],
            ['Для выбранного графика бригада не назначается.'],
        )

    def test_oup_registry_filters_by_department_and_schedule_references(self):
        oup_employee = Employee.objects.create(
            full_name='Иванова Анна',
            phone='+79000000002',
            status=Employee.Status.ACTIVE,
        )
        oup_access = EmployeeAccess.objects.create(
            employee=oup_employee,
            role=self.oup_role,
            access_code='303030',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        other_department = PersonnelDepartment.objects.get(code='department_002')
        other_schedule = WorkSchedule.objects.get(code='schedule_11')
        Employee.objects.create(
            full_name='Сотрудник Другого Подразделения',
            phone='+79000000003',
            personnel_department=other_department,
            work_schedule=other_schedule,
            brigade_number=1,
            status=Employee.Status.ACTIVE,
        )
        self.login_as(oup_access)

        response = self.client.get(reverse('oup_employees'), {
            'department': self.department.id,
            'rotation': self.schedule.id,
        })

        self.assertContains(response, self.employee.full_name)
        self.assertNotContains(response, 'Сотрудник Другого Подразделения')
        self.assertContains(response, f'value="{self.department.id}" selected', html=False)
        self.assertContains(response, f'value="{self.schedule.id}" selected', html=False)

    def test_admin_create_forces_active_employee_lifecycle(self):
        self.login_as(self.admin_access)

        response = self.client.post(reverse('system_admin_employee_create'), {
            'full_name': 'Смирнов Семен',
            'phone': '+79005554433',
            'status': Employee.Status.DEACTIVATED,
            'personnel_position': self.truck_position.id,
            'base_specialization': self.truck_specialization.id,
            'role': self.driver_role.id,
        })

        self.assertEqual(response.status_code, 302)
        employee = Employee.objects.get(phone='+79005554433')
        self.assertEqual(employee.status, Employee.Status.ACTIVE)
        self.assertTrue(employee.is_active)
