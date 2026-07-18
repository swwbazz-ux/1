from datetime import datetime, time, timedelta
from pathlib import Path

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from shifts.models import WatchPeriod
from users.models import AdminConflict, Employee, EmployeeAccess, Role

from .models import RotationCollectionCycle, RotationResponse, WatchExtensionCase
from .services import close_cycle, open_cycle


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost', '.localhost'])
class RotationWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.roles = {}
        for code, name in (
            ('timekeeper', 'Табельщик'),
            ('site_manager', 'Начальник участка'),
            ('employee_portal', 'Сотрудник'),
            ('driver', 'Водитель самосвала'),
        ):
            cls.roles[code], _created = Role.objects.get_or_create(
                code=code,
                defaults={'name': name},
            )
        cls.timekeeper = cls._employee(
            'Тестовый Табельщик', '+79990001001', 'timekeeper', '710001'
        )
        cls.site_manager = cls._employee(
            'Тестовый Начальник', '+79990001002', 'site_manager', '710002'
        )
        cls.employee = cls._employee(
            'Иванов Иван Иванович', '+79990001003', 'employee_portal', '710003'
        )
        cls.other_employee = cls._employee(
            'Петров Петр Петрович', '+79990001004', 'driver', '710004'
        )
        cls.watch = WatchPeriod.objects.create(
            name='Вахта августа 2026',
            starts_on=timezone.localdate() + timedelta(days=30),
            ends_on=timezone.localdate() + timedelta(days=59),
            is_active=True,
        )

    @classmethod
    def _employee(cls, full_name, phone, role_code, access_code):
        employee = Employee.objects.create(
            full_name=full_name,
            phone=phone,
            personnel_number=access_code[-3:],
            position='Работник участка',
            department='Горный участок',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=employee,
            role=cls.roles[role_code],
            access_code=access_code,
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=timezone.now(),
            is_active=True,
        )
        return employee

    def setUp(self):
        self.cycle = RotationCollectionCycle.objects.create(
            name='Контрольный сбор',
            target_watch_period=self.watch,
            response_deadline=timezone.now() + timedelta(days=5),
            created_by=self.timekeeper,
        )
        open_cycle(self.cycle, actor=self.timekeeper)
        self.cycle.refresh_from_db()

    def _client_for(self, employee, role_code):
        client = Client()
        access = EmployeeAccess.objects.get(employee=employee, role=self.roles[role_code])
        session = client.session
        session['employee_access_id'] = access.pk
        session['device_kind'] = 'personal'
        session.save()
        return client

    def _response_for(self, employee=None):
        return RotationResponse.objects.get(
            cycle=self.cycle,
            employee=employee or self.employee,
        )

    def _extension_payload(self, *, shift='day'):
        return {
            'intent': 'extension',
            'next_shift_type': shift,
            'departure_on': '',
            'arrival_on': '',
            'route_text': '',
            'travel_mode': '',
            'transfer_mode': '',
            'transport_details': '',
            'comment': 'Готов продолжить работу.',
            'extension_start': self.watch.starts_on.isoformat(),
            'extension_end': (self.watch.starts_on + timedelta(days=14)).isoformat(),
        }

    def test_open_cycle_seeds_every_active_employee_as_pending_snapshot(self):
        responses = self.cycle.responses.order_by('snapshot_full_name')

        self.assertEqual(responses.count(), 4)
        response = self._response_for()
        self.assertEqual(response.state, 'pending')
        self.assertEqual(response.intent, '')
        self.assertEqual(response.snapshot_full_name, self.employee.full_name)
        self.assertEqual(response.snapshot_personnel_number, self.employee.personnel_number)

    def test_employee_submits_extension_and_site_manager_returns_approval(self):
        employee_client = self._client_for(self.employee, 'employee_portal')
        response = self._response_for()

        saved = employee_client.post(
            f'/my/rotation/{response.pk}/',
            self._extension_payload(),
        )

        self.assertRedirects(
            saved,
            f'/my/rotation/{response.pk}/',
            fetch_redirect_response=False,
        )
        response.refresh_from_db()
        self.assertEqual(response.state, 'submitted')
        self.assertEqual(response.intent, 'extension')
        self.assertEqual(response.shift_source, 'employee')
        case = response.extension_case
        self.assertEqual(case.decision_status, 'pending')

        manager_client = self._client_for(self.site_manager, 'site_manager')
        approved = manager_client.post(
            f'/site-manager/extensions/{case.pk}/approved/',
            {'comment': 'Сменность подтверждена.'},
        )

        self.assertRedirects(
            approved,
            '/site-manager/extensions/',
            fetch_redirect_response=False,
        )
        case.refresh_from_db()
        self.assertEqual(case.decision_status, 'approved')
        self.assertEqual(case.decision_by, self.site_manager)
        self.assertIsNotNone(case.decision_at)

    def test_rejection_requires_comment(self):
        response = self._response_for()
        client = self._client_for(self.employee, 'employee_portal')
        client.post(f'/my/rotation/{response.pk}/', self._extension_payload())
        case = WatchExtensionCase.objects.get(response=response)
        manager_client = self._client_for(self.site_manager, 'site_manager')

        result = manager_client.post(
            f'/site-manager/extensions/{case.pk}/rejected/',
            {'comment': ''},
            follow=True,
        )

        case.refresh_from_db()
        self.assertEqual(case.decision_status, 'pending')
        self.assertContains(result, 'При отклонении укажите причину.')

    def test_employee_cannot_open_another_employee_response(self):
        client = self._client_for(self.employee, 'employee_portal')
        other_response = self._response_for(self.other_employee)

        result = client.get(f'/my/rotation/{other_response.pk}/')

        self.assertEqual(result.status_code, 404)

    def test_wrong_role_cannot_open_timekeeper_or_site_manager_workplaces(self):
        client = self._client_for(self.other_employee, 'driver')

        timekeeper = client.get('/timekeeper/')
        manager = client.get('/site-manager/extensions/')

        self.assertRedirects(timekeeper, '/home/', fetch_redirect_response=False)
        self.assertRedirects(manager, '/home/', fetch_redirect_response=False)

    def test_deadline_blocks_employee_but_timekeeper_can_record_late_answer(self):
        RotationCollectionCycle.objects.filter(pk=self.cycle.pk).update(
            response_deadline=timezone.now() - timedelta(minutes=1)
        )
        response = self._response_for()
        payload = {
            'intent': 'not_travelling',
            'next_shift_type': '',
            'departure_on': '',
            'arrival_on': '',
            'route_text': '',
            'travel_mode': '',
            'transfer_mode': '',
            'transport_details': '',
            'comment': 'Не еду.',
            'extension_start': '',
            'extension_end': '',
        }
        employee_client = self._client_for(self.employee, 'employee_portal')

        blocked = employee_client.post(f'/my/rotation/{response.pk}/', payload)

        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, 'Срок самостоятельного ответа завершен')
        response.refresh_from_db()
        self.assertEqual(response.state, 'pending')

        timekeeper_client = self._client_for(self.timekeeper, 'timekeeper')
        saved = timekeeper_client.post(
            f'/timekeeper/campaigns/{self.cycle.pk}/responses/{response.pk}/',
            payload,
        )
        self.assertRedirects(
            saved,
            f'/timekeeper/campaigns/{self.cycle.pk}/',
            fetch_redirect_response=False,
        )
        response.refresh_from_db()
        self.assertEqual(response.state, 'submitted')
        self.assertEqual(response.shift_source, 'timekeeper')

    def test_closed_cycle_blocks_response_change(self):
        close_cycle(self.cycle, actor=self.timekeeper)
        response = self._response_for()
        client = self._client_for(self.employee, 'employee_portal')

        result = client.post(
            f'/my/rotation/{response.pk}/',
            self._extension_payload(),
        )

        self.assertEqual(result.status_code, 200)
        self.assertContains(result, 'Сбор закрыт')
        response.refresh_from_db()
        self.assertEqual(response.state, 'pending')

    def test_approved_response_is_locked_from_later_employee_changes(self):
        response = self._response_for()
        employee_client = self._client_for(self.employee, 'employee_portal')
        employee_client.post(f'/my/rotation/{response.pk}/', self._extension_payload())
        case = WatchExtensionCase.objects.get(response=response)
        manager_client = self._client_for(self.site_manager, 'site_manager')
        manager_client.post(
            f'/site-manager/extensions/{case.pk}/approved/',
            {'comment': 'Одобрено.'},
        )

        changed = employee_client.post(
            f'/my/rotation/{response.pk}/',
            self._extension_payload(shift='night'),
        )

        self.assertContains(changed, 'заблокирована от изменений')
        response.refresh_from_db()
        self.assertEqual(response.next_shift_type, 'day')

    def test_timekeeper_marks_only_approved_documentation_complete(self):
        response = self._response_for()
        employee_client = self._client_for(self.employee, 'employee_portal')
        employee_client.post(f'/my/rotation/{response.pk}/', self._extension_payload())
        case = WatchExtensionCase.objects.get(response=response)
        timekeeper_client = self._client_for(self.timekeeper, 'timekeeper')

        blocked = timekeeper_client.post(
            f'/timekeeper/extensions/{case.pk}/complete/',
            {'note': 'До решения.'},
            follow=True,
        )
        self.assertContains(blocked, 'только одобренное продление')

        manager_client = self._client_for(self.site_manager, 'site_manager')
        manager_client.post(
            f'/site-manager/extensions/{case.pk}/approved/',
            {'comment': 'Одобрено.'},
        )
        completed = timekeeper_client.post(
            f'/timekeeper/extensions/{case.pk}/complete/',
            {'note': 'Передано в ОУП.'},
        )
        self.assertRedirects(
            completed,
            f'/timekeeper/campaigns/{self.cycle.pk}/',
            fetch_redirect_response=False,
        )
        case.refresh_from_db()
        self.assertEqual(case.documentation_status, 'completed')
        self.assertEqual(case.documentation_by, self.timekeeper)

    def test_timekeeper_download_routes_return_real_xlsx_and_docx_files(self):
        response = self._response_for()
        employee_client = self._client_for(self.employee, 'employee_portal')
        employee_client.post(f'/my/rotation/{response.pk}/', self._extension_payload())
        case = WatchExtensionCase.objects.get(response=response)
        manager_client = self._client_for(self.site_manager, 'site_manager')
        manager_client.post(
            f'/site-manager/extensions/{case.pk}/approved/',
            {'comment': 'Одобрено для выгрузки.'},
        )
        timekeeper_client = self._client_for(self.timekeeper, 'timekeeper')

        workbook = timekeeper_client.get(
            f'/timekeeper/campaigns/{self.cycle.pk}/export.xlsx'
        )
        packet = timekeeper_client.get(
            f'/timekeeper/campaigns/{self.cycle.pk}/extension-data.docx'
        )

        self.assertEqual(workbook.status_code, 200)
        self.assertEqual(
            workbook['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertTrue(workbook.content.startswith(b'PK'))
        self.assertIn('attachment;', workbook['Content-Disposition'])
        self.assertEqual(packet.status_code, 200)
        self.assertEqual(
            packet['Content-Type'],
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
        self.assertTrue(packet.content.startswith(b'PK'))
        self.assertIn('attachment;', packet['Content-Disposition'])

        denied = self._client_for(self.other_employee, 'driver').get(
            f'/timekeeper/campaigns/{self.cycle.pk}/export.xlsx'
        )
        self.assertRedirects(denied, '/home/', fetch_redirect_response=False)

    def test_shared_login_preserves_safe_rotation_next_path(self):
        client = Client()

        response = client.post(
            '/',
            {
                'phone': self.employee.phone,
                'access_code': '710003',
                'device_kind': 'personal',
                'next': '/my/rotation/',
            },
        )

        self.assertRedirects(response, '/my/rotation/', fetch_redirect_response=False)

    def test_new_role_manifests_have_shared_and_isolated_scopes(self):
        for slug, legacy_scope in (
            ('timekeeper', '/timekeeper/'),
            ('site-manager', '/site-manager/'),
        ):
            with self.subTest(slug=slug, mode='shared'):
                response = self.client.get(f'/{slug}.webmanifest', HTTP_HOST='localhost')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['scope'], legacy_scope)
            with self.subTest(slug=slug, mode='isolated'):
                response = self.client.get(
                    f'/{slug}.webmanifest',
                    HTTP_HOST=f'{slug}.localhost',
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['scope'], '/')

    def test_rotation_role_service_workers_use_updated_header_shells(self):
        expected_versions = {
            'timekeeper': 'timekeeper-shell-v5',
            'site-manager': 'site-manager-shell-v5',
        }

        for slug, expected_version in expected_versions.items():
            with self.subTest(slug=slug):
                response = self.client.get(f'/{slug}-sw.js', HTTP_HOST='localhost')
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_version)
                self.assertContains(response, 'new Request(url, { cache: "reload" })')
                self.assertContains(response, 'fetch(request, { cache: "reload" })')

    def test_rotation_header_uses_shared_compact_action_grid(self):
        backend_root = Path(__file__).resolve().parents[1]
        stylesheet = (backend_root / 'static' / 'css' / 'rotation-workplace-v2.css').read_text(
            encoding='utf-8',
        )

        self.assertIn(
            'grid-template-columns: var(--admin-header-icon-size) var(--admin-header-utility-width);',
            stylesheet,
        )
        self.assertIn('.rotation-shell .rotation-console-actions .admin-theme-button', stylesheet)
        self.assertNotIn('display: flex;\n    grid-template-columns: none;', stylesheet)
        self.assertIn(
            '.rotation-console-header .admin-console-actions {\n'
            '        grid-row: 3;',
            stylesheet,
        )
        self.assertIn(
            '.rotation-console-header .admin-console-main {\n'
            '        grid-row: 2;',
            stylesheet,
        )

        for template_name in (
            'cycle_create.html',
            'response_form.html',
            'site_manager_queue.html',
            'timekeeper_cycle.html',
            'timekeeper_dashboard.html',
        ):
            template = (
                backend_root / 'templates' / 'rotations' / template_name
            ).read_text(encoding='utf-8')
            self.assertIn('data-theme-icon="sun"', template)
            self.assertNotRegex(
                template,
                r'<button\b[^>]*data-admin-theme-toggle[^>]*>\s*\S+.*?</button>',
            )

    def test_admin_delete_is_blocked_when_employee_has_rotation_history(self):
        admin_role, _created = Role.objects.get_or_create(
            code='admin',
            defaults={'name': 'Администратор'},
        )
        admin_employee = Employee.objects.create(
            full_name='Администратор перевахты',
            phone='+79990001005',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        admin_access = EmployeeAccess.objects.create(
            employee=admin_employee,
            role=admin_role,
            access_code='710005',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=timezone.now(),
            is_active=True,
        )
        client = Client()
        session = client.session
        session['employee_access_id'] = admin_access.pk
        session['device_kind'] = 'personal'
        session.save()

        result = client.post(
            f'/system-admin/employees/{self.employee.id}/delete/',
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(result.status_code, 200)
        self.assertTrue(Employee.objects.filter(pk=self.employee.pk).exists())
        self.assertTrue(
            AdminConflict.objects.filter(
                employee=self.employee,
                conflict_type='Попытка удаления сотрудника с историей',
            ).exists()
        )
        self.assertContains(result, 'Удаление запрещено')
