from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase
from django.utils import timezone

from assignments.models import CrewPlan, CrewPlanSlot, CrewPlanStatus, WorkShiftType
from references.models import Equipment, EquipmentType

from .models import Employee, EmployeeAccess, Role


class AdminRegistrationDashboardTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.admin_role = Role.objects.create(code='admin', name='Администратор')
        self.manager_role = Role.objects.create(code='manager', name='Руководство')
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.excavator_role = Role.objects.create(
            code='excavator_operator',
            name='Машинист экскаватора',
        )
        self.admin = Employee.objects.create(
            full_name='Администратор отчёта',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='910001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.manager = Employee.objects.create(
            full_name='Руководитель отчёта',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.manager_access = EmployeeAccess.objects.create(
            employee=self.manager,
            role=self.manager_role,
            access_code='910005',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.equipment = [
            Equipment.objects.create(equipment_type=truck_type, garage_number='101'),
            Equipment.objects.create(equipment_type=truck_type, garage_number='102'),
            Equipment.objects.create(equipment_type=truck_type, garage_number='103'),
            Equipment.objects.create(equipment_type=excavator_type, garage_number='ЭКС-1'),
        ]

        self.driver_active = Employee.objects.create(
            full_name='Алексеев Активированный',
            status=Employee.Status.ACTIVE,
        )
        self.driver_waiting = Employee.objects.create(
            full_name='Борисов Ожидающий',
            status=Employee.Status.ACTIVE,
        )
        self.driver_without_access = Employee.objects.create(
            full_name='Волков Без Доступа',
            status=Employee.Status.ACTIVE,
        )
        self.excavator_activated = Employee.objects.create(
            full_name='Громов Экскаваторщик',
            status=Employee.Status.ACTIVE,
        )

        self.driver_ready_at = self.now - timedelta(days=3)
        self.driver_active_access = EmployeeAccess.objects.create(
            employee=self.driver_active,
            role=self.driver_role,
            access_code='910002',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=self.driver_ready_at,
            # Legacy activated access: issued timestamp is intentionally absent.
            primary_code_issued_at=None,
            last_login_at=self.now - timedelta(hours=2),
        )
        self.driver_waiting_access = EmployeeAccess.objects.create(
            employee=self.driver_waiting,
            role=self.driver_role,
            access_code='910003',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            primary_code_issued_at=self.now - timedelta(hours=30),
        )
        self.excavator_ready_at = self.now - timedelta(days=1)
        self.excavator_issued_at = self.excavator_ready_at - timedelta(hours=6)
        self.excavator_access = EmployeeAccess.objects.create(
            employee=self.excavator_activated,
            role=self.excavator_role,
            access_code='910004',
            status=EmployeeAccess.Status.ACTIVATED,
            primary_code_issued_at=self.excavator_issued_at,
            activated_at=self.excavator_ready_at,
        )

        work_date = timezone.localdate()
        self.driver_plan = CrewPlan.objects.create(
            work_date=work_date,
            role=self.driver_role,
            status=CrewPlanStatus.PUBLISHED,
            published_by=self.admin,
            published_at=self.now,
        )
        self.excavator_plan = CrewPlan.objects.create(
            work_date=work_date,
            role=self.excavator_role,
            status=CrewPlanStatus.PUBLISHED,
            published_by=self.admin,
            published_at=self.now,
        )
        self.driver_day_slot = CrewPlanSlot.objects.create(
            plan=self.driver_plan,
            equipment=self.equipment[0],
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.driver_active,
        )
        CrewPlanSlot.objects.create(
            plan=self.driver_plan,
            equipment=self.equipment[1],
            shift_type=WorkShiftType.SHIFT_2,
            employee=self.driver_waiting,
        )
        CrewPlanSlot.objects.create(
            plan=self.driver_plan,
            equipment=self.equipment[2],
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.driver_without_access,
        )
        self.excavator_day_slot = CrewPlanSlot.objects.create(
            plan=self.excavator_plan,
            equipment=self.equipment[3],
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.excavator_activated,
        )

    def authenticate_admin(self):
        session = self.client.session
        session['employee_access_id'] = self.admin_access.id
        session.save()

    def authenticate_manager(self):
        session = self.client.session
        session['employee_access_id'] = self.manager_access.id
        session.save()

    def admin_dashboard(self, query=''):
        self.authenticate_admin()
        suffix = f'?{query}' if query else ''
        return self.client.get(
            f'/system-admin/registrations/{suffix}',
            HTTP_HOST='localhost',
        )

    @staticmethod
    def row_by_name(context, full_name):
        return next(
            row
            for row in context['all_rows']
            if row['employee'].full_name == full_name
        )

    @staticmethod
    def breakdown_by_code(context, key, code):
        return next(item for item in context[key] if item['code'] == code)

    def test_dashboard_builds_readiness_funnel_and_invariants(self):
        response = self.admin_dashboard()
        context = response.context

        self.assertEqual(response.status_code, 200)
        self.assertEqual(context['total'], 4)
        self.assertEqual(context['prepared'], 3)
        self.assertEqual(context['ready'], 2)
        self.assertEqual(context['requires_action'], 2)
        self.assertEqual(context['missing_access'], 1)
        self.assertEqual(context['awaiting_activation'], 1)
        self.assertEqual(context['blocked'], 0)
        self.assertEqual(context['inactive_employee'], 0)
        self.assertEqual(context['deactivated'], 0)
        self.assertEqual(context['prepared_percent'], 75)
        self.assertEqual(context['ready_percent'], 50)
        self.assertEqual(context['activation_conversion_percent'], 67)
        self.assertEqual(context['lost_before_prepared'], 1)
        self.assertEqual(context['lost_after_prepared'], 1)
        self.assertLessEqual(context['ready'], context['prepared'])
        self.assertLessEqual(context['prepared'], context['total'])
        self.assertEqual(
            context['ready']
            + context['blocked']
            + context['inactive_employee']
            + context['deactivated']
            + context['missing_access']
            + context['awaiting_activation'],
            context['total'],
        )
        self.assertEqual(
            [item['count'] for item in context['funnel_steps']],
            [4, 3, 2],
        )
        self.assertEqual(
            [item['loss_count'] for item in context['funnel_steps']],
            [1, 1, 0],
        )
        self.assertEqual(
            [item['conversion_percent'] for item in context['funnel_steps'][:2]],
            [75, 67],
        )
        self.assertEqual(
            sum(item['count'] for item in context['attention_items']),
            context['requires_action'],
        )
        self.assertTrue(
            all(item['url'] == item['filter_url'] for item in context['funnel_steps'])
        )
        self.assertEqual(
            [item['code'] for item in context['state_tabs']],
            [
                'all',
                'needs_attention',
                'prepared',
                'ready',
                'awaiting_activation',
                'missing_access',
                'inactive_employee',
                'deactivated',
                'blocked',
            ],
        )
        self.assertEqual(
            [item['days'] for item in context['period_links']],
            [7, 14, 30, 90],
        )
        self.assertEqual(sum(item['new_count'] for item in context['chart']['days']), 2)
        self.assertEqual(context['chart']['days'][-1]['cumulative'], 2)
        self.assertEqual(context['chart']['days'][-1]['cumulative_percent'], 50)
        self.assertEqual(context['chart']['max_cumulative'], context['total'])
        chart_days = context['chart']['days']
        chart_x = [item['svg_x'] for item in chart_days]
        self.assertEqual(len(context['chart']['polyline'].split()), len(chart_days))
        self.assertAlmostEqual(chart_x[0], 500 / len(chart_days), places=2)
        self.assertAlmostEqual(
            chart_x[-1],
            1000 - (500 / len(chart_days)),
            places=2,
        )
        self.assertTrue(all(left < right for left, right in zip(chart_x, chart_x[1:])))
        self.assertIsNone(context['median_activation_hours'])
        self.assertEqual(context['median_activation_sample'], 1)
        self.assertEqual(context['new_ready_7'], 2)
        self.assertEqual(context['previous_ready_7'], 0)
        self.assertEqual(context['delta_abs'], 2)
        self.assertIsNone(context['delta_percent'])
        self.assertEqual(context['plan_published_at'], self.now)
        waiting_row = self.row_by_name(context, self.driver_waiting.full_name)
        self.assertEqual(waiting_row['waiting_hours'], 30)
        self.assertEqual(waiting_row['waiting_label'], 'Ждёт 1 д')

    def test_median_activation_time_requires_three_valid_samples(self):
        self.driver_active_access.primary_code_issued_at = (
            self.driver_ready_at - timedelta(hours=4)
        )
        self.driver_active_access.save(update_fields=['primary_code_issued_at'])
        third_ready_at = self.now - timedelta(hours=12)
        EmployeeAccess.objects.create(
            employee=self.driver_without_access,
            role=self.driver_role,
            access_code='910090',
            status=EmployeeAccess.Status.ACTIVATED,
            primary_code_issued_at=third_ready_at - timedelta(hours=10),
            activated_at=third_ready_at,
        )

        context = self.admin_dashboard().context

        self.assertEqual(context['median_activation_sample'], 3)
        self.assertEqual(context['median_activation_hours'], 6.0)

    def test_server_filters_and_links_preserve_selected_context(self):
        driver_response = self.admin_dashboard('role=driver&period=7')
        driver_context = driver_response.context
        night_context = self.admin_dashboard('shift=night').context
        prepared_context = self.admin_dashboard('state=prepared').context
        ready_context = self.admin_dashboard('state=ready').context
        legacy_ready_context = self.admin_dashboard('state=activated').context
        attention_context = self.admin_dashboard('state=needs_attention').context
        missing_context = self.admin_dashboard('state=missing_access').context

        self.assertEqual(driver_context['total'], 3)
        self.assertEqual(driver_context['prepared'], 2)
        self.assertEqual(driver_context['ready'], 1)
        self.assertEqual(driver_context['period_days'], 7)
        self.assertEqual(night_context['total'], 1)
        self.assertEqual(night_context['ready'], 0)
        self.assertEqual(prepared_context['visible_total'], 3)
        self.assertEqual(ready_context['visible_total'], 2)
        self.assertEqual(legacy_ready_context['selected_state'], 'ready')
        self.assertEqual(legacy_ready_context['visible_total'], 2)
        self.assertEqual(attention_context['visible_total'], 2)
        self.assertEqual(missing_context['visible_total'], 1)

        ready_tab = next(
            item for item in driver_context['state_tabs'] if item['code'] == 'ready'
        )
        query = parse_qs(urlsplit(ready_tab['url']).query)
        self.assertEqual(query['role'], ['driver'])
        self.assertEqual(query['period'], ['7'])
        self.assertEqual(query['state'], ['ready'])
        self.assertEqual(ready_tab['url'], ready_tab['filter_url'])

    def test_inactive_blocked_and_deactivated_accesses_are_distinct(self):
        self.driver_waiting_access.status = EmployeeAccess.Status.BLOCKED
        self.driver_waiting_access.is_active = False
        self.driver_waiting_access.blocked_at = self.now
        self.driver_waiting_access.last_login_at = self.now - timedelta(hours=1)
        self.driver_waiting_access.save(
            update_fields=['status', 'is_active', 'blocked_at', 'last_login_at'],
        )
        EmployeeAccess.objects.create(
            employee=self.driver_without_access,
            role=self.driver_role,
            access_code='910099',
            status=EmployeeAccess.Status.DEACTIVATED,
            is_active=False,
            deactivated_at=self.now,
        )

        context = self.admin_dashboard().context
        blocked_row = self.row_by_name(context, self.driver_waiting.full_name)
        deactivated_row = self.row_by_name(
            context,
            self.driver_without_access.full_name,
        )

        self.assertEqual(context['ready'], 2)
        self.assertEqual(context['prepared'], 2)
        self.assertEqual(context['blocked'], 1)
        self.assertEqual(context['deactivated'], 1)
        self.assertEqual(context['missing_access'], 0)
        self.assertEqual(context['awaiting_activation'], 0)
        self.assertEqual(blocked_row['code'], 'blocked')
        self.assertEqual(deactivated_row['code'], 'deactivated')
        self.assertEqual(self.admin_dashboard('state=blocked').context['visible_total'], 1)
        self.assertEqual(
            self.admin_dashboard('state=missing_access').context['visible_total'],
            0,
        )
        self.assertEqual(
            self.admin_dashboard('state=deactivated').context['visible_total'],
            1,
        )
        deactivated_item = next(
            item for item in context['attention_items'] if item['code'] == 'deactivated'
        )
        self.assertEqual(
            parse_qs(urlsplit(deactivated_item['url']).query)['state'],
            ['deactivated'],
        )

    def test_blank_pin_is_missing_not_prepared(self):
        self.driver_waiting_access.access_code = ''
        self.driver_waiting_access.primary_code_issued_at = None
        self.driver_waiting_access.save(
            update_fields=['access_code', 'primary_code_issued_at'],
        )

        context = self.admin_dashboard().context
        row = self.row_by_name(context, self.driver_waiting.full_name)

        self.assertEqual(context['prepared'], 2)
        self.assertEqual(context['ready'], 2)
        self.assertEqual(context['missing_access'], 2)
        self.assertEqual(context['awaiting_activation'], 0)
        self.assertEqual(row['code'], 'missing_access')
        self.assertEqual(row['role_statuses'][0]['reason_code'], 'missing_pin')
        self.assertIn('PIN не сформирован', row['issue_summary'])

    def test_secondary_employee_is_included_once(self):
        secondary = Employee.objects.create(
            full_name='Зайцев Дополнительный',
            status=Employee.Status.ACTIVE,
        )
        EmployeeAccess.objects.create(
            employee=secondary,
            role=self.driver_role,
            access_code='910050',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=self.now - timedelta(days=2),
        )
        self.driver_day_slot.secondary_employee = secondary
        self.driver_day_slot.save(update_fields=['secondary_employee'])

        context = self.admin_dashboard().context
        driver_breakdown = self.breakdown_by_code(
            context,
            'role_breakdown',
            'driver',
        )

        self.assertEqual(context['total'], 5)
        self.assertEqual(context['prepared'], 4)
        self.assertEqual(context['ready'], 3)
        self.assertEqual(
            sum(row['employee'].id == secondary.id for row in context['rows']),
            1,
        )
        self.assertEqual(driver_breakdown['total'], 4)
        self.assertEqual(driver_breakdown['ready'], 2)

    def test_multirole_readiness_uses_exact_pair_and_latest_activation(self):
        self.excavator_day_slot.secondary_employee = self.driver_active
        self.excavator_day_slot.save(update_fields=['secondary_employee'])

        before = self.admin_dashboard().context
        before_row = self.row_by_name(before, self.driver_active.full_name)
        before_status = {
            item['role_code']: item
            for item in before_row['role_statuses']
        }
        driver_breakdown = self.breakdown_by_code(
            before,
            'role_breakdown',
            'driver',
        )
        excavator_breakdown = self.breakdown_by_code(
            before,
            'role_breakdown',
            'excavator_operator',
        )

        self.assertEqual(before['total'], 4)
        self.assertEqual(before['ready'], 1)
        self.assertEqual(before_row['code'], 'missing_access')
        self.assertTrue(before_status['driver']['is_ready'])
        self.assertFalse(before_status['excavator_operator']['is_ready'])
        self.assertEqual(driver_breakdown['ready'], 1)
        self.assertEqual(excavator_breakdown['total'], 2)
        self.assertEqual(excavator_breakdown['ready'], 1)

        later_activation = self.now - timedelta(hours=5)
        EmployeeAccess.objects.create(
            employee=self.driver_active,
            role=self.excavator_role,
            access_code='910060',
            status=EmployeeAccess.Status.ACTIVATED,
            primary_code_issued_at=later_activation - timedelta(hours=10),
            activated_at=later_activation,
        )

        after = self.admin_dashboard().context
        after_row = self.row_by_name(after, self.driver_active.full_name)
        excavator_breakdown = self.breakdown_by_code(
            after,
            'role_breakdown',
            'excavator_operator',
        )

        self.assertEqual(after['ready'], 2)
        self.assertTrue(after_row['is_ready'])
        self.assertEqual(after_row['ready_at'], later_activation)
        self.assertEqual(excavator_breakdown['ready'], 2)

    def test_ready_without_activation_date_is_kept_as_undated(self):
        self.driver_active_access.activated_at = None
        self.driver_active_access.save(update_fields=['activated_at'])

        context = self.admin_dashboard().context
        row = self.row_by_name(context, self.driver_active.full_name)

        self.assertEqual(context['ready'], 2)
        self.assertIsNone(row['ready_at'])
        self.assertEqual(context['chart']['undated_ready'], 1)
        self.assertEqual(sum(item['new_count'] for item in context['chart']['days']), 1)
        self.assertEqual(context['chart']['days'][-1]['cumulative'], 2)

    def test_delta_ready_on_filter_and_chart_links(self):
        previous_activation = self.now - timedelta(days=8)
        current_activation = self.now - timedelta(days=2)
        self.driver_active_access.activated_at = previous_activation
        self.driver_active_access.save(update_fields=['activated_at'])
        self.excavator_access.activated_at = current_activation
        self.excavator_access.save(update_fields=['activated_at'])

        context = self.admin_dashboard('period=7').context
        current_date = timezone.localtime(current_activation).date()

        self.assertEqual(context['new_ready_7'], 1)
        self.assertEqual(context['previous_ready_7'], 1)
        self.assertEqual(context['delta_abs'], 0)
        self.assertEqual(context['delta_percent'], 0)

        filtered = self.admin_dashboard(
            f'period=7&state=ready&ready_on={current_date.isoformat()}',
        ).context
        self.assertEqual(filtered['visible_total'], 1)
        self.assertEqual(
            filtered['rows'][0]['employee'].full_name,
            self.excavator_activated.full_name,
        )
        chart_day = next(
            item for item in filtered['chart']['days'] if item['date'] == current_date
        )
        self.assertTrue(chart_day['is_selected'])
        self.assertEqual(chart_day['url'], chart_day['filter_url'])
        self.assertEqual(
            parse_qs(urlsplit(chart_day['url']).query)['state'],
            ['ready'],
        )
        self.assertTrue(
            all(
                'ready_on' not in parse_qs(urlsplit(item['url']).query)
                for item in filtered['state_tabs']
            )
        )
        self.assertTrue(
            all(
                'ready_on' not in parse_qs(urlsplit(item['url']).query)
                for item in filtered['period_links']
            )
        )

    def test_inactive_employee_in_published_plan_is_not_ready(self):
        self.driver_active.is_active = False
        self.driver_active.save(update_fields=['is_active'])

        context = self.admin_dashboard().context
        row = self.row_by_name(context, self.driver_active.full_name)
        driver_breakdown = self.breakdown_by_code(
            context,
            'role_breakdown',
            'driver',
        )

        self.assertEqual(context['ready'], 1)
        self.assertEqual(context['prepared'], 2)
        self.assertEqual(context['missing_access'], 1)
        self.assertEqual(context['inactive_employee'], 1)
        self.assertEqual(row['code'], 'inactive_employee')
        self.assertEqual(row['tone'], 'danger')
        self.assertIn('Сотрудник неактивен', row['issue_summary'])
        self.assertEqual(driver_breakdown['ready'], 0)
        self.assertEqual(
            self.admin_dashboard('state=missing_access').context['visible_total'],
            1,
        )
        inactive_context = self.admin_dashboard('state=inactive_employee').context
        self.assertEqual(inactive_context['visible_total'], 1)
        inactive_tab = next(
            item
            for item in inactive_context['state_tabs']
            if item['code'] == 'inactive_employee'
        )
        self.assertEqual(inactive_tab['label'], 'Сотрудник неактивен')
        self.assertEqual(inactive_tab['count'], 1)
        self.assertTrue(inactive_tab['is_active'])
        self.assertEqual(
            parse_qs(urlsplit(inactive_tab['url']).query)['state'],
            ['inactive_employee'],
        )

        attention_by_code = {
            item['code']: item for item in context['attention_items']
        }
        self.assertEqual(attention_by_code['missing_access']['count'], 1)
        self.assertEqual(attention_by_code['inactive_employee']['count'], 1)
        self.assertEqual(
            sum(item['count'] for item in context['attention_items']),
            context['requires_action'],
        )

    def test_latest_terminal_access_wins_over_older_active_duplicate(self):
        EmployeeAccess.objects.create(
            employee=self.driver_active,
            role=self.driver_role,
            access_code='910091',
            status=EmployeeAccess.Status.BLOCKED,
            is_active=False,
            blocked_at=timezone.now(),
            block_reason='Контроль актуального статуса',
        )

        context = self.admin_dashboard().context
        row = self.row_by_name(context, self.driver_active.full_name)

        self.assertEqual(row['code'], 'blocked')
        self.assertFalse(row['is_ready'])
        self.assertEqual(context['ready'], 1)
        self.assertEqual(context['blocked'], 1)

    def test_breakdowns_rank_largest_readiness_gap_first(self):
        context = self.admin_dashboard().context

        self.assertEqual(context['role_breakdown'][0]['code'], 'driver')
        self.assertEqual(context['role_breakdown'][0]['not_ready'], 2)
        self.assertEqual(context['shift_breakdown'][0]['code'], WorkShiftType.SHIFT_2)
        self.assertEqual(context['shift_breakdown'][0]['percent'], 0)
        self.assertEqual(context['shift_breakdown'][1]['code'], WorkShiftType.SHIFT_1)
        self.assertEqual(context['shift_breakdown'][1]['percent'], 67)

    def test_empty_published_plan_scope_has_zero_safe_metrics(self):
        CrewPlan.objects.all().delete()

        context = self.admin_dashboard().context

        self.assertFalse(context['has_published_plans'])
        self.assertEqual(context['total'], 0)
        self.assertEqual(context['prepared_percent'], 0)
        self.assertEqual(context['ready_percent'], 0)
        self.assertEqual(context['activation_conversion_percent'], 0)
        self.assertEqual(context['chart']['max_cumulative'], 0)

    def test_dashboard_requires_admin_access(self):
        response = self.client.get('/system-admin/registrations/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/home/')

    def test_system_admin_summary_links_to_registration_dashboard(self):
        self.authenticate_admin()

        response = self.client.get('/system-admin/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/system-admin/registrations/"')
        self.assertContains(response, 'Подключение сотрудников')

    def test_manager_opens_same_dashboard_in_management_shell(self):
        self.authenticate_manager()

        response = self.client.get(
            '/reports/management/registrations/',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], 4)
        self.assertEqual(response.context['prepared'], 3)
        self.assertEqual(response.context['ready'], 2)
        self.assertTrue(response.context['management_mode'])
        self.assertContains(response, 'Руководство MVP')
        self.assertContains(response, 'href="/reports/management/registrations/"')
        self.assertNotContains(
            response,
            f'href="/system-admin/employees/{self.driver_active.id}/"',
        )
        self.assertNotContains(response, 'Телефон не указан')

    def test_management_summary_links_manager_to_registration_dashboard(self):
        self.authenticate_manager()

        response = self.client.get('/reports/management/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/reports/management/registrations/"')
        self.assertContains(response, '>Подключение</a>', html=False)

    def test_dispatcher_cannot_open_management_registration_dashboard(self):
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(
            full_name='Диспетчер без доступа к подключениям',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        dispatcher_access = EmployeeAccess.objects.create(
            employee=dispatcher,
            role=dispatcher_role,
            access_code='910006',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        session = self.client.session
        session['employee_access_id'] = dispatcher_access.id
        session.save()

        response = self.client.get(
            '/reports/management/registrations/',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/home/')
