from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from rotations.models import EmployeeWatchProfileChange

from .admin import EmployeeAdminForm
from .models import Employee, WatchComposition, WorkSchedule


class DjangoAdminWatchProfilePermissionTests(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username='django-admin-watch-profile',
            password='test-password',
            email='django-admin-watch-profile@example.test',
        )
        self.client.force_login(self.superuser)
        self.schedule_11 = WorkSchedule.objects.create(
            code='django_admin_schedule_11',
            name='График 11/1',
            brigade_count=2,
        )
        self.schedule_12 = WorkSchedule.objects.create(
            code='django_admin_schedule_12',
            name='График 12/1',
            brigade_count=4,
        )
        self.composition_one = WatchComposition.objects.create(
            code='composition_one',
            name='Состав вахты 1',
        )
        self.composition_two = WatchComposition.objects.create(
            code='composition_two',
            name='Состав вахты 2',
        )
        self.employee = Employee.objects.create(
            full_name='Сотрудник Django Admin',
            sex=Employee.Sex.MALE,
            work_category=Employee.WorkCategory.OTHER,
            status=Employee.Status.ACTIVE,
            work_schedule=self.schedule_11,
            brigade_number=1,
            watch_composition=self.composition_one,
            rotation='Вахта 1',
            comment='Исходный комментарий',
        )

    def change_url(self):
        return reverse('admin:users_employee_change', args=[self.employee.pk])

    @staticmethod
    def add_url():
        return reverse('admin:users_employee_add')

    def valid_change_payload(self, **overrides):
        payload = {
            'full_name': self.employee.full_name,
            'birth_date': '',
            'sex': self.employee.sex,
            'personnel_position': '',
            'base_specialization': '',
            'position': self.employee.position,
            'department': self.employee.department,
            'personnel_department': '',
            'work_category': self.employee.work_category,
            'personnel_number': self.employee.personnel_number,
            'phone': self.employee.phone,
            'status': self.employee.status,
            'comment': self.employee.comment,
            'hired_at': '',
            'dismissed_at': '',
            'residence_text': self.employee.residence_text,
            'hr_data': self.employee.hr_data,
            'is_active': 'on',
            '_save': 'Сохранить',
        }
        payload.update(overrides)
        return payload

    def assert_profile_unchanged(self):
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.work_schedule_id, self.schedule_11.pk)
        self.assertEqual(self.employee.brigade_number, 1)
        self.assertEqual(self.employee.watch_composition_id, self.composition_one.pk)
        self.assertEqual(self.employee.rotation, 'Вахта 1')
        self.assertFalse(EmployeeWatchProfileChange.objects.exists())

    def assert_crafted_profile_post_rejected(self, field_name, value):
        response = self.client.post(
            self.change_url(),
            self.valid_change_payload(
                full_name='Не должно сохраниться',
                comment='Не должно сохраниться',
                **{field_name: value},
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, EmployeeAdminForm.WATCH_PROFILE_EDIT_ERROR)
        self.assert_profile_unchanged()
        self.assertEqual(self.employee.full_name, 'Сотрудник Django Admin')
        self.assertEqual(self.employee.comment, 'Исходный комментарий')

    def test_superuser_opens_add_page_and_profile_fields_are_editable(self):
        response = self.client.get(self.add_url())

        self.assertEqual(response.status_code, 200)
        form = response.context['adminform'].form
        for field_name in EmployeeAdminForm.PROTECTED_WATCH_PROFILE_FIELDS:
            self.assertFalse(form.fields[field_name].disabled)

    def test_add_scenario_preserves_initial_watch_profile_assignment(self):
        response = self.client.post(self.add_url(), {
            'full_name': 'Новый сотрудник Django Admin',
            'birth_date': '',
            'sex': Employee.Sex.FEMALE,
            'personnel_position': '',
            'base_specialization': '',
            'position': '',
            'department': '',
            'personnel_department': '',
            'work_category': Employee.WorkCategory.OTHER,
            'personnel_number': '',
            'phone': '',
            'status': Employee.Status.ACTIVE,
            'comment': '',
            'hired_at': '',
            'dismissed_at': '',
            'rotation': 'Вахта 2',
            'work_schedule': str(self.schedule_12.pk),
            'brigade_number': '4',
            'watch_composition': str(self.composition_two.pk),
            'residence_text': '',
            'hr_data': '',
            'is_active': 'on',
            '_save': 'Сохранить',
        })

        self.assertEqual(response.status_code, 302)
        employee = Employee.objects.get(full_name='Новый сотрудник Django Admin')
        self.assertEqual(employee.work_schedule_id, self.schedule_12.pk)
        self.assertEqual(employee.brigade_number, 4)
        self.assertEqual(employee.watch_composition_id, self.composition_two.pk)
        self.assertEqual(employee.rotation, 'Вахта 2')
        self.assertFalse(EmployeeWatchProfileChange.objects.exists())

    def test_change_page_disables_watch_profile_fields_and_explains_policy(self):
        response = self.client.get(self.change_url())

        self.assertEqual(response.status_code, 200)
        form = response.context['adminform'].form
        for field_name in EmployeeAdminForm.PROTECTED_WATCH_PROFILE_FIELDS:
            self.assertTrue(form.fields[field_name].disabled)
        self.assertContains(response, EmployeeAdminForm.WATCH_PROFILE_EDIT_ERROR, count=4)

    def test_crafted_post_cannot_change_work_schedule(self):
        self.assert_crafted_profile_post_rejected('work_schedule', str(self.schedule_12.pk))

    def test_crafted_post_cannot_change_brigade_number(self):
        self.assert_crafted_profile_post_rejected('brigade_number', '2')

    def test_crafted_post_cannot_change_watch_composition(self):
        self.assert_crafted_profile_post_rejected('watch_composition', str(self.composition_two.pk))

    def test_crafted_post_cannot_change_rotation(self):
        self.assert_crafted_profile_post_rejected('rotation', 'Подложная вахта')

    def test_allowed_change_saves_without_changing_watch_profile(self):
        response = self.client.post(
            self.change_url(),
            self.valid_change_payload(
                full_name='Разрешённое новое ФИО',
                comment='Разрешённый комментарий',
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assert_profile_unchanged()
        self.assertEqual(self.employee.full_name, 'Разрешённое новое ФИО')
        self.assertEqual(self.employee.comment, 'Разрешённый комментарий')
