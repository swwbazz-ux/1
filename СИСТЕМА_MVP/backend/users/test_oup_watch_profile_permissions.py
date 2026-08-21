from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from rotations.models import EmployeeWatchProfileChange

from .forms import AdminEmployeeForm
from .models import (
    Employee,
    EmployeeAccess,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
    WatchComposition,
    WorkSchedule,
)


class OupWatchProfilePermissionsTests(TestCase):
    def setUp(self):
        self.oup_role, _created = Role.objects.update_or_create(
            code='oup',
            defaults={'name': 'Специалист ОУП', 'is_active': True},
        )
        self.driver_role, _created = Role.objects.update_or_create(
            code='driver',
            defaults={'name': 'Водитель самосвала', 'is_active': True},
        )
        self.schedule_a, _created = WorkSchedule.objects.update_or_create(
            code='oup-profile-a',
            defaults={
                'name': 'ТЕСТ ОУП график A',
                'brigade_count': 2,
                'is_active': True,
            },
        )
        self.schedule_b, _created = WorkSchedule.objects.update_or_create(
            code='oup-profile-b',
            defaults={
                'name': 'ТЕСТ ОУП график B',
                'brigade_count': 4,
                'is_active': True,
            },
        )
        self.composition_a = WatchComposition.objects.create(
            code='oup-profile-composition-a',
            name='ТЕСТ ОУП состав A',
        )
        self.composition_b = WatchComposition.objects.create(
            code='oup-profile-composition-b',
            name='ТЕСТ ОУП состав B',
        )
        self.specialization_a = ProductionSpecialization.objects.create(
            code='oup-profile-specialization-a',
            name='ТЕСТ ОУП специализация A',
        )
        self.specialization_b = ProductionSpecialization.objects.create(
            code='oup-profile-specialization-b',
            name='ТЕСТ ОУП специализация B',
        )
        self.position_a = PersonnelPosition.objects.create(
            code='oup-profile-position-a',
            name='ТЕСТ ОУП должность A',
            requires_specialization=True,
            default_specialization=self.specialization_a,
        )
        self.position_a.allowed_specializations.add(
            self.specialization_a,
            self.specialization_b,
        )
        self.position_b = PersonnelPosition.objects.create(
            code='oup-profile-position-b',
            name='ТЕСТ ОУП должность B',
            requires_specialization=True,
            default_specialization=self.specialization_b,
        )
        self.position_b.allowed_specializations.add(self.specialization_b)

        self.oup_employee = Employee.objects.create(
            full_name='Тестовый Специалист ОУП',
            phone='+79000002001',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.oup_access = EmployeeAccess.objects.create(
            employee=self.oup_employee,
            role=self.oup_role,
            access_code='920001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.employee = Employee.objects.create(
            full_name='Тестовый Действующий Сотрудник',
            personnel_number='OUP-PROFILE-001',
            phone='+79000002002',
            personnel_position=self.position_a,
            base_specialization=self.specialization_a,
            position=self.position_a.name,
            work_category=Employee.WorkCategory.OTHER,
            hired_at=timezone.localdate(),
            work_schedule=self.schedule_a,
            brigade_number=1,
            watch_composition=self.composition_a,
            rotation='Исходное legacy-значение',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self._login(self.oup_access)

    def _login(self, access):
        session = self.client.session
        session['employee_access_id'] = access.pk
        session.save()

    def _start_editing(self):
        response = self.client.post(
            reverse('oup_shift_start'),
            {'next': reverse('oup_employees')},
        )
        self.assertEqual(response.status_code, 302)

    def _profile_values(self, employee=None):
        employee = employee or self.employee
        return (
            employee.work_schedule_id,
            employee.brigade_number,
            employee.watch_composition_id,
            employee.rotation,
        )

    def _edit_payload(self, **overrides):
        payload = {
            'full_name': self.employee.full_name,
            'birth_date': '',
            'sex': self.employee.sex,
            'personnel_number': self.employee.personnel_number,
            'phone': self.employee.phone,
            'personnel_position': str(self.employee.personnel_position_id or ''),
            'base_specialization': str(self.employee.base_specialization_id or ''),
            'position': self.employee.position,
            'personnel_department': str(self.employee.personnel_department_id or ''),
            'work_category': self.employee.work_category,
            'hired_at': self.employee.hired_at.isoformat(),
            'residence_text': self.employee.residence_text,
            'comment': self.employee.comment,
            'hr_data': self.employee.hr_data,
        }
        payload.update(overrides)
        return payload

    def _assert_profile_tamper_rejected(self, **tamper):
        self._start_editing()
        original_profile = self._profile_values()
        original_name = self.employee.full_name
        response = self.client.post(
            reverse('oup_employee_detail', args=[self.employee.pk]),
            self._edit_payload(full_name='Недопустимо Изменённое Имя', **tamper),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Изменение графика, бригады и состава вахты действующего сотрудника '
            'выполняет Табельщик.',
        )
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.full_name, original_name)
        self.assertEqual(self._profile_values(), original_profile)
        self.assertFalse(EmployeeWatchProfileChange.objects.exists())

    def test_oup_can_assign_watch_profile_when_creating_employee(self):
        self._start_editing()
        response = self.client.post(
            reverse('oup_employee_create'),
            {
                'full_name': 'Новый Тестовый Сотрудник',
                'birth_date': '',
                'sex': Employee.Sex.UNKNOWN,
                'personnel_number': 'OUP-PROFILE-NEW',
                'phone': '+79000002003',
                'personnel_position': self.position_b.pk,
                'base_specialization': self.specialization_b.pk,
                'position': '',
                'personnel_department': '',
                'work_category': Employee.WorkCategory.OTHER,
                'hired_at': timezone.localdate().isoformat(),
                'work_schedule': self.schedule_b.pk,
                'brigade_number': 2,
                'watch_composition': self.composition_b.pk,
                'residence_text': '',
                'comment': '',
                'hr_data': '',
            },
        )

        created = Employee.objects.get(personnel_number='OUP-PROFILE-NEW')
        self.assertRedirects(
            response,
            reverse('oup_employee_detail', args=[created.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(created.work_schedule, self.schedule_b)
        self.assertEqual(created.brigade_number, 2)
        self.assertEqual(created.watch_composition, self.composition_b)
        self.assertEqual(created.rotation, f'{self.schedule_b.name} Бригада №2')
        self.assertFalse(EmployeeWatchProfileChange.objects.exists())

    def test_existing_employee_profile_is_disabled_and_explained(self):
        self._start_editing()
        response = self.client.get(
            reverse('oup_employee_detail', args=[self.employee.pk]),
        )

        form = response.context['form']
        for field_name in ('work_schedule', 'brigade_number', 'watch_composition'):
            self.assertTrue(form.fields[field_name].disabled)
        self.assertFalse(form.fields['personnel_position'].disabled)
        self.assertContains(
            response,
            'Изменение графика, бригады и состава вахты действующего сотрудника '
            'выполняет Табельщик.',
        )
        self.assertNotContains(response, 'name="rotation"', html=False)

    def test_crafted_post_cannot_change_work_schedule(self):
        self._assert_profile_tamper_rejected(work_schedule=self.schedule_b.pk)

    def test_crafted_post_cannot_change_brigade_number(self):
        self._assert_profile_tamper_rejected(brigade_number=2)

    def test_crafted_post_cannot_change_watch_composition(self):
        self._assert_profile_tamper_rejected(watch_composition=self.composition_b.pk)

    def test_crafted_post_cannot_change_legacy_rotation(self):
        self._assert_profile_tamper_rejected(rotation='Подложная вахта')

    def test_allowed_position_change_preserves_watch_profile(self):
        self._start_editing()
        original_profile = self._profile_values()
        response = self.client.post(
            reverse('oup_employee_detail', args=[self.employee.pk]),
            self._edit_payload(
                personnel_position=self.position_b.pk,
                base_specialization=self.specialization_b.pk,
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.personnel_position, self.position_b)
        self.assertEqual(self.employee.base_specialization, self.specialization_b)
        self.assertEqual(self._profile_values(), original_profile)

    def test_allowed_specialization_change_preserves_watch_profile(self):
        self._start_editing()
        original_profile = self._profile_values()
        response = self.client.post(
            reverse('oup_employee_detail', args=[self.employee.pk]),
            self._edit_payload(base_specialization=self.specialization_b.pk),
        )

        self.assertEqual(response.status_code, 302)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.base_specialization, self.specialization_b)
        self.assertEqual(self.employee.personnel_position, self.position_a)
        self.assertEqual(self._profile_values(), original_profile)

    def test_dismissal_still_works_and_preserves_watch_profile(self):
        self._start_editing()
        original_profile = self._profile_values()
        response = self.client.post(
            reverse('oup_employee_dismiss', args=[self.employee.pk]),
            {
                'dismissed_at': timezone.localdate().isoformat(),
                'reason': 'Тестовое кадровое основание',
            },
        )

        self.assertRedirects(
            response,
            reverse('oup_dismissed_employees'),
            fetch_redirect_response=False,
        )
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.status, Employee.Status.DISMISSED)
        self.assertFalse(self.employee.is_active)
        self.assertEqual(self._profile_values(), original_profile)

    def test_open_period_guard_still_blocks_other_employee_changes(self):
        response = self.client.post(
            reverse('oup_employee_detail', args=[self.employee.pk]),
            self._edit_payload(full_name='Имя Без Открытого Периода'),
        )

        self.assertRedirects(
            response,
            reverse('oup_employee_detail', args=[self.employee.pk]),
            fetch_redirect_response=False,
        )
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.full_name, 'Тестовый Действующий Сотрудник')

    def test_wrong_role_guard_still_blocks_employee_card(self):
        driver = Employee.objects.create(
            full_name='Тестовый Водитель Доступа',
            phone='+79000002004',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        driver_access = EmployeeAccess.objects.create(
            employee=driver,
            role=self.driver_role,
            access_code='920004',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self._login(driver_access)

        response = self.client.get(
            reverse('oup_employee_detail', args=[self.employee.pk]),
        )

        self.assertRedirects(
            response,
            reverse('role_home'),
            fetch_redirect_response=False,
        )

    def test_admin_employee_form_remains_editable(self):
        form = AdminEmployeeForm(instance=self.employee)

        for field_name in ('work_schedule', 'brigade_number', 'watch_composition'):
            self.assertFalse(form.fields[field_name].disabled)
