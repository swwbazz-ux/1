from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from rotations.models import EmployeeWatchProfileChange

from .models import Employee, EmployeeAccess, Role, WatchComposition, WorkSchedule


class AdminWatchProfilePermissionsTests(TestCase):
    def setUp(self):
        self.admin_role, _created = Role.objects.update_or_create(
            code='admin',
            defaults={'name': 'Администратор', 'is_active': True},
        )
        self.driver_role, _created = Role.objects.update_or_create(
            code='driver',
            defaults={'name': 'Водитель самосвала', 'is_active': True},
        )
        self.schedule_a, _created = WorkSchedule.objects.update_or_create(
            code='admin-profile-a',
            defaults={
                'name': 'ТЕСТ Администратор график A',
                'brigade_count': 2,
                'is_active': True,
            },
        )
        self.schedule_b, _created = WorkSchedule.objects.update_or_create(
            code='admin-profile-b',
            defaults={
                'name': 'ТЕСТ Администратор график B',
                'brigade_count': 4,
                'is_active': True,
            },
        )
        self.composition_a = WatchComposition.objects.create(
            code='admin-profile-composition-a',
            name='ТЕСТ Администратор состав A',
        )
        self.composition_b = WatchComposition.objects.create(
            code='admin-profile-composition-b',
            name='ТЕСТ Администратор состав B',
        )
        self.admin_employee = Employee.objects.create(
            full_name='Тестовый Системный Администратор',
            phone='+79000003001',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin_employee,
            role=self.admin_role,
            access_code='930001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.employee = Employee.objects.create(
            full_name='Тестовый Сотрудник Админкарточки',
            personnel_number='ADMIN-PROFILE-001',
            phone='+79000003002',
            position='Тестовая должность',
            work_category=Employee.WorkCategory.OTHER,
            hired_at=timezone.localdate(),
            work_schedule=self.schedule_a,
            brigade_number=1,
            watch_composition=self.composition_a,
            rotation='Исходное legacy-значение администратора',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self._login(self.admin_access)

    def _login(self, access):
        session = self.client.session
        session['employee_access_id'] = access.pk
        session.save()

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
            'personnel_position': '',
            'base_specialization': '',
            'position': self.employee.position,
            'personnel_department': '',
            'work_category': self.employee.work_category,
            'hired_at': self.employee.hired_at.isoformat(),
            'residence_text': self.employee.residence_text,
            'comment': self.employee.comment,
            'hr_data': self.employee.hr_data,
            'assignment_role': '',
            'assignment_shift_type': '',
            'assignment_equipment': '',
        }
        payload.update(overrides)
        return payload

    def _assert_profile_tamper_rejected(self, **tamper):
        original_profile = self._profile_values()
        original_name = self.employee.full_name
        original_comment = self.employee.comment
        response = self.client.post(
            reverse('system_admin_employee_detail', args=[self.employee.pk]),
            self._edit_payload(
                full_name='Недопустимо Изменённое Имя',
                comment='Это поле не должно сохраниться частично',
                **tamper,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Изменение графика, бригады и состава вахты действующего сотрудника '
            'выполняет Табельщик.',
        )
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.full_name, original_name)
        self.assertEqual(self.employee.comment, original_comment)
        self.assertEqual(self._profile_values(), original_profile)
        self.assertFalse(EmployeeWatchProfileChange.objects.exists())

    def test_exact_admin_opens_existing_employee_card(self):
        response = self.client.get(
            reverse('system_admin_employee_detail', args=[self.employee.pk]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'users/employee_card.html')

    def test_existing_employee_watch_profile_is_disabled_and_explained(self):
        response = self.client.get(
            reverse('system_admin_employee_detail', args=[self.employee.pk]),
        )

        form = response.context['form']
        for field_name in ('work_schedule', 'brigade_number', 'watch_composition'):
            self.assertTrue(form.fields[field_name].disabled)
        self.assertFalse(form.fields['full_name'].disabled)
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
        self._assert_profile_tamper_rejected(rotation='Подложное legacy-значение')

    def test_allowed_admin_change_preserves_watch_profile(self):
        original_profile = self._profile_values()
        response = self.client.post(
            reverse('system_admin_employee_detail', args=[self.employee.pk]),
            self._edit_payload(
                full_name='Разрешённо Изменённое Имя',
                comment='Разрешённое административное примечание',
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.full_name, 'Разрешённо Изменённое Имя')
        self.assertEqual(self.employee.comment, 'Разрешённое административное примечание')
        self.assertEqual(self._profile_values(), original_profile)
        self.assertFalse(EmployeeWatchProfileChange.objects.exists())

    def test_admin_create_scenario_keeps_initial_watch_profile_assignment(self):
        response = self.client.post(
            reverse('system_admin_employee_create'),
            {
                'full_name': 'Новый Сотрудник Администратора',
                'birth_date': '',
                'sex': Employee.Sex.UNKNOWN,
                'personnel_number': 'ADMIN-PROFILE-NEW',
                'phone': '+79000003003',
                'personnel_position': '',
                'base_specialization': '',
                'position': 'Новая тестовая должность',
                'personnel_department': '',
                'work_category': Employee.WorkCategory.OTHER,
                'hired_at': timezone.localdate().isoformat(),
                'work_schedule': self.schedule_b.pk,
                'brigade_number': 2,
                'watch_composition': self.composition_b.pk,
                'residence_text': '',
                'comment': '',
                'hr_data': '',
                'role': '',
                'assignment_shift_type': '',
                'assignment_equipment': '',
            },
        )

        created = Employee.objects.get(personnel_number='ADMIN-PROFILE-NEW')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(created.work_schedule, self.schedule_b)
        self.assertEqual(created.brigade_number, 2)
        self.assertEqual(created.watch_composition, self.composition_b)
        self.assertEqual(created.rotation, f'{self.schedule_b.name} Бригада №2')
        self.assertFalse(EmployeeWatchProfileChange.objects.exists())

    def test_wrong_role_inactive_and_blocked_accesses_are_rejected(self):
        access_cases = []
        for suffix, role, status, is_active in (
            ('wrong', self.driver_role, EmployeeAccess.Status.ACTIVATED, True),
            ('inactive', self.admin_role, EmployeeAccess.Status.ACTIVATED, False),
            ('blocked', self.admin_role, EmployeeAccess.Status.BLOCKED, True),
        ):
            actor = Employee.objects.create(
                full_name=f'Тестовый Доступ {suffix}',
                phone={
                    'wrong': '+79000003004',
                    'inactive': '+79000003005',
                    'blocked': '+79000003006',
                }[suffix],
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
            access_cases.append(
                EmployeeAccess.objects.create(
                    employee=actor,
                    role=role,
                    access_code={
                        'wrong': '930004',
                        'inactive': '930005',
                        'blocked': '930006',
                    }[suffix],
                    status=status,
                    is_active=is_active,
                )
            )

        for access in access_cases:
            with self.subTest(access_status=access.status, role=access.role.code):
                self._login(access)
                response = self.client.get(
                    reverse('system_admin_employee_detail', args=[self.employee.pk]),
                )
                self.assertRedirects(
                    response,
                    reverse('role_home'),
                    fetch_redirect_response=False,
                )
