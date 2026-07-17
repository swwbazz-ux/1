from django.test import TestCase
from django.urls import reverse

from .forms import AdminEmployeeForm
from .models import Employee, EmployeeAccess, Role
from .oup_forms import OupEmployeeForm


class UnifiedEmployeeCardTests(TestCase):
    def setUp(self):
        self.admin_role, _ = Role.objects.update_or_create(code='admin', defaults={'name': 'Администратор'})
        self.oup_role, _ = Role.objects.update_or_create(code='oup', defaults={'name': 'Специалист ОУП'})
        self.driver_role, _ = Role.objects.update_or_create(code='driver', defaults={'name': 'Водитель самосвала'})
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
            position='Водитель',
            department='Горный участок',
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
            'role': self.driver_role.id,
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['phone'], '+79005554433')

    def test_new_employee_cannot_reuse_an_existing_phone(self):
        form = AdminEmployeeForm(data={
            'full_name': 'Смирнов Семен',
            'phone': '8 (900) 111-22-33',
            'status': Employee.Status.ACTIVE,
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
