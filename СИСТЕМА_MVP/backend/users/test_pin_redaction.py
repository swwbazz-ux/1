from io import StringIO

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .admin import AdminActionLogAdmin, EmployeeAccessAdmin
from .models import AdminActionLog, Employee, EmployeeAccess, Role
from .redaction import REDACTED_ACCESS_CODE


class PinRedactionWriteTests(TestCase):
    """T-021: новые записи журнала не содержат PIN."""

    def setUp(self):
        self.admin_role, _created = Role.objects.update_or_create(
            code='admin',
            defaults={'name': 'Администратор', 'is_active': True},
        )
        self.driver_role, _created = Role.objects.update_or_create(
            code='driver',
            defaults={'name': 'Водитель самосвала', 'is_active': True},
        )
        self.admin = Employee.objects.create(
            full_name='Администратор T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='100023',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.client.post('/', {'access_code': '100023'}, HTTP_HOST='localhost')

    def test_created_employee_pin_is_not_written_to_journal(self):
        response = self.client.post(
            '/system-admin/employees/create/',
            {
                'full_name': 'Новый водитель T-021',
                'personnel_number': 'T021-1',
                'phone': '+79990000221',
                'status': Employee.Status.NOT_ACTIVATED,
                'role': self.driver_role.id,
                'generate_access': 'on',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        employee = Employee.objects.get(full_name='Новый водитель T-021')
        access = EmployeeAccess.objects.get(employee=employee)
        log = AdminActionLog.objects.get(action='Создан сотрудник и выдан первичный пинкод')
        self.assertNotIn(access.access_code, log.new_value)
        self.assertNotIn(access.access_code, log.old_value)
        self.assertIn('пинкод выдан', log.new_value)

        # Сообщение о выдаче PIN остаётся, но журнал его не печатает.
        logs_page = self.client.get(reverse('system_admin_logs'), HTTP_HOST='localhost')
        self.assertEqual(logs_page.status_code, 200)
        self.assertNotContains(logs_page, access.access_code)

    def test_reissued_pin_is_not_written_to_journal(self):
        employee = Employee.objects.create(
            full_name='Сотрудник с перевыпуском T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.driver_role,
            access_code='',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            is_active=True,
        )

        response = self.client.post(
            reverse('system_admin_generate_access', args=[employee.pk]),
            {'role': self.driver_role.id},
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 302)
        access.refresh_from_db()
        log = AdminActionLog.objects.get(action='Выдан новый первичный пинкод')
        self.assertEqual(log.new_value, 'PIN изменён')
        self.assertNotIn(access.access_code, log.new_value)
        self.assertNotIn(access.access_code, log.old_value)


class PinRedactionReadTests(TestCase):
    """T-021: исторические записи журнала не печатают PIN и не находятся по нему."""

    def setUp(self):
        self.admin_role, _created = Role.objects.update_or_create(
            code='admin',
            defaults={'name': 'Администратор', 'is_active': True},
        )
        self.admin = Employee.objects.create(
            full_name='Администратор T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='100024',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.client.post('/', {'access_code': '100024'}, HTTP_HOST='localhost')

    def test_journal_screen_does_not_print_historical_pin(self):
        pin = '321654'
        AdminActionLog.objects.create(
            actor=self.admin,
            action='Выдан новый первичный пинкод',
            object_type='EmployeeAccess',
            object_repr='Исторический доступ',
            new_value=f'первичный PIN: {pin}',
        )
        AdminActionLog.objects.create(
            actor=self.admin,
            action='Выдан новый первичный пинкод',
            object_type='EmployeeAccess',
            object_repr='Исторический доступ без метки',
            new_value=pin,
        )

        response = self.client.get(reverse('system_admin_logs'), HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, pin)
        self.assertContains(response, REDACTED_ACCESS_CODE)

    def test_oup_log_screen_and_search_do_not_expose_pin(self):
        oup_role, _created = Role.objects.update_or_create(
            code='oup',
            defaults={'name': 'Специалист ОУП', 'is_active': True},
        )
        oup_employee = Employee.objects.create(
            full_name='Специалист ОУП T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        oup_access = EmployeeAccess.objects.create(
            employee=oup_employee,
            role=oup_role,
            access_code='200024',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        session = self.client.session
        session['employee_access_id'] = oup_access.id
        session.save()

        pin = '987123'
        AdminActionLog.objects.create(
            actor=oup_employee,
            action='ОУП: выдан первичный PIN',
            action_code='oup_access_issued',
            object_type='EmployeeAccess',
            object_id='1',
            object_repr='Иванов Иван Иванович — Водитель самосвала',
            new_value=f'Сотрудник: Иванов Иван Иванович; роль: Водитель; первичный PIN: {pin}',
        )

        logs_page = self.client.get(reverse('oup_logs'), HTTP_HOST='localhost')
        self.assertEqual(logs_page.status_code, 200)
        self.assertNotContains(logs_page, pin)

        pin_search = self.client.get(reverse('oup_logs'), {'q': pin}, HTTP_HOST='localhost')
        self.assertNotContains(pin_search, 'Иванов Иван Иванович')

        name_search = self.client.get(reverse('oup_logs'), {'q': 'Иванов'}, HTTP_HOST='localhost')
        self.assertContains(name_search, 'Иванов Иван Иванович')

    def test_oup_log_search_finds_result_text_but_not_pin(self):
        oup_role, _created = Role.objects.update_or_create(
            code='oup',
            defaults={'name': 'Специалист ОУП', 'is_active': True},
        )
        oup_employee = Employee.objects.create(
            full_name='Специалист ОУП поиск T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        oup_access = EmployeeAccess.objects.create(
            employee=oup_employee,
            role=oup_role,
            access_code='200026',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        session = self.client.session
        session['employee_access_id'] = oup_access.id
        session.save()

        pin = '654987'
        AdminActionLog.objects.create(
            actor=oup_employee,
            action='ОУП: отключён доступ сотрудника',
            action_code='oup_access_deactivated',
            object_type='EmployeeAccess',
            object_id='2',
            object_repr='Петров Пётр Петрович — Водитель самосвала',
            new_value='Доступ отключён; прежний PIN: ' + pin,
        )

        text_search = self.client.get(
            reverse('oup_logs'),
            {'q': 'Доступ отключён'},
            HTTP_HOST='localhost',
        )
        self.assertContains(text_search, 'Петров Пётр Петрович')

        pin_search = self.client.get(
            reverse('oup_logs'),
            {'q': pin},
            HTTP_HOST='localhost',
        )
        self.assertNotContains(pin_search, 'Петров Пётр Петрович')

        # Обычный текст «Результата» по-прежнему ищется (не только PIN-запросы).
        AdminActionLog.objects.create(
            actor=oup_employee,
            action='ОУП: отключён доступ сотрудника',
            action_code='oup_access_deactivated',
            object_type='EmployeeAccess',
            object_id='2',
            object_repr='Петров Пётр Петрович — Водитель самосвала',
            new_value='Доступ отключён; причина: плановое обслуживание',
        )
        result_search = self.client.get(
            reverse('oup_logs'),
            {'q': 'плановое обслуживание'},
            HTTP_HOST='localhost',
        )
        self.assertContains(result_search, 'Петров Пётр Петрович')


class AdminSurfaceTests(TestCase):
    """T-021: админские поверхности не ищут и не печатают PIN."""

    def test_access_admin_search_fields_exclude_access_code(self):
        model_admin = EmployeeAccessAdmin(EmployeeAccess, AdminSite())
        self.assertNotIn('access_code', model_admin.search_fields)

    def test_action_log_admin_masks_values_and_payload(self):
        log = AdminActionLog(
            old_value='Старое',
            new_value='первичный PIN: 445566',
            undo_payload={'before': {'access_code': '445566'}},
        )
        model_admin = AdminActionLogAdmin(AdminActionLog, AdminSite())

        self.assertNotIn('new_value', model_admin.readonly_fields)
        self.assertNotIn('undo_payload', model_admin.readonly_fields)
        self.assertNotIn('445566', model_admin.new_value_display(log))
        self.assertEqual(model_admin.undo_payload_display(log), '— снимок скрыт —')

    def test_pin_like_query_is_not_used_to_search_result_text(self):
        from .redaction import looks_like_access_code

        self.assertTrue(looks_like_access_code('445566'))
        self.assertTrue(looks_like_access_code('44-55-66'))
        self.assertFalse(looks_like_access_code('Доступ отключён'))
        self.assertFalse(looks_like_access_code(''))

    def test_pin_like_query_is_not_used_to_search_result_text(self):
        from .redaction import looks_like_access_code

        self.assertTrue(looks_like_access_code('445566'))
        self.assertTrue(looks_like_access_code('44-55-66'))
        self.assertFalse(looks_like_access_code('Доступ отключён'))
        self.assertFalse(looks_like_access_code(''))


class ScrubAdminLogPinsCommandTests(TestCase):
    """T-021: исторические записи очищаются management-командой, без миграции."""

    def setUp(self):
        self.actor = Employee.objects.create(
            full_name='Администратор команды T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.pin = '778899'
        self.log = AdminActionLog.objects.create(
            actor=self.actor,
            action='Выдан новый первичный пинкод',
            object_type='EmployeeAccess',
            object_id='1',
            object_repr='Исторический доступ',
            new_value=f'первичный PIN: {self.pin}',
            undo_payload={
                'version': 1,
                'before': {'access_code': '', 'status': 'activated'},
                'after': {'access_code': self.pin, 'status': 'not_activated'},
                'accesses': [
                    {'id': 1, 'before': {'access_code': '112233'}},
                ],
            },
        )

    def test_dry_run_reports_but_does_not_change_rows(self):
        output = StringIO()
        call_command('scrub_admin_log_pins', stdout=output)

        self.log.refresh_from_db()
        self.assertIn(self.pin, self.log.new_value)
        self.assertEqual(
            self.log.undo_payload['after']['access_code'],
            self.pin,
        )
        self.assertIn('dry-run', output.getvalue())
        self.assertNotIn(self.pin, output.getvalue())

    def test_apply_redacts_values_and_undo_payload(self):
        output = StringIO()
        call_command('scrub_admin_log_pins', '--apply', stdout=output)

        self.log.refresh_from_db()
        self.assertNotIn(self.pin, self.log.new_value)
        self.assertIn(REDACTED_ACCESS_CODE, self.log.new_value)
        self.assertEqual(
            self.log.undo_payload['after']['access_code'],
            REDACTED_ACCESS_CODE,
        )
        self.assertEqual(
            self.log.undo_payload['before']['access_code'],
            '',
        )
        self.assertEqual(
            self.log.undo_payload['accesses'][0]['before']['access_code'],
            REDACTED_ACCESS_CODE,
        )
        self.assertIn('применено', output.getvalue())


class AdminActionLogChangePageTests(TestCase):
    """T-021: HTML-страница журнала в Django-admin не печатает сырой PIN.

    Проверяется реальный ответ `/admin/users/adminactionlog/<id>/change/`, а не
    helper-методы: именно read-only страница отдавала PIN до правки.
    """

    def setUp(self):
        self.pin = '445566'
        self.log = AdminActionLog.objects.create(
            action='Выдан новый первичный пинкод',
            object_type='EmployeeAccess',
            object_id='1',
            object_repr='Исторический доступ',
            old_value='Старое значение',
            new_value=f'первичный PIN: {self.pin}',
            undo_payload={'after': {'access_code': self.pin}},
        )
        self.change_url = reverse(
            'admin:users_adminactionlog_change',
            args=[self.log.pk],
        )

    def staff_viewer(self):
        user = get_user_model().objects.create_user(
            username='t021-log-viewer',
            password='test-password-t021',
            is_staff=True,
        )
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='users',
                content_type__model='adminactionlog',
                codename='view_adminactionlog',
            )
        )
        return user

    def test_staff_without_change_permission_does_not_see_raw_pin(self):
        self.client.force_login(self.staff_viewer())

        response = self.client.get(self.change_url, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.pin)
        self.assertContains(response, REDACTED_ACCESS_CODE)
        self.assertNotContains(response, '"access_code"')

    def test_superuser_does_not_see_raw_pin(self):
        superuser = get_user_model().objects.create_superuser(
            username='t021-log-superuser',
            password='test-password-t021',
            email='t021-log-superuser@example.test',
        )
        self.client.force_login(superuser)

        response = self.client.get(self.change_url, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.pin)
        self.assertContains(response, REDACTED_ACCESS_CODE)

    def test_changelist_does_not_print_pin(self):
        superuser = get_user_model().objects.create_superuser(
            username='t021-log-list',
            password='test-password-t021',
            email='t021-log-list@example.test',
        )
        self.client.force_login(superuser)

        response = self.client.get(
            reverse('admin:users_adminactionlog_changelist'),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.pin)
        self.assertNotContains(response, 'access_code')
