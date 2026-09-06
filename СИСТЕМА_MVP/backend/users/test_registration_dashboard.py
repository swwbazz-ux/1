from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from assignments.models import CrewPlan, CrewPlanSlot, CrewPlanStatus, WorkShiftType
from references.models import Equipment, EquipmentType

from .models import Employee, EmployeeAccess, Role


class AdminRegistrationDashboardTests(TestCase):
    def setUp(self):
        self.admin_role = Role.objects.create(code='admin', name='Администратор')
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.excavator_role = Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
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

        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        equipment = [
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

        activated_at = timezone.now() - timedelta(days=3)
        EmployeeAccess.objects.create(
            employee=self.driver_active,
            role=self.driver_role,
            access_code='910002',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=activated_at,
            last_login_at=timezone.now() - timedelta(hours=2),
        )
        self.driver_waiting_access = EmployeeAccess.objects.create(
            employee=self.driver_waiting,
            role=self.driver_role,
            access_code='910003',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
        )
        EmployeeAccess.objects.create(
            employee=self.excavator_activated,
            role=self.excavator_role,
            access_code='910004',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=timezone.now() - timedelta(days=1),
        )

        work_date = timezone.localdate()
        driver_plan = CrewPlan.objects.create(
            work_date=work_date,
            role=self.driver_role,
            status=CrewPlanStatus.PUBLISHED,
            published_by=self.admin,
            published_at=timezone.now(),
        )
        excavator_plan = CrewPlan.objects.create(
            work_date=work_date,
            role=self.excavator_role,
            status=CrewPlanStatus.PUBLISHED,
            published_by=self.admin,
            published_at=timezone.now(),
        )
        CrewPlanSlot.objects.create(
            plan=driver_plan,
            equipment=equipment[0],
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.driver_active,
        )
        CrewPlanSlot.objects.create(
            plan=driver_plan,
            equipment=equipment[1],
            shift_type=WorkShiftType.SHIFT_2,
            employee=self.driver_waiting,
        )
        CrewPlanSlot.objects.create(
            plan=driver_plan,
            equipment=equipment[2],
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.driver_without_access,
        )
        CrewPlanSlot.objects.create(
            plan=excavator_plan,
            equipment=equipment[3],
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.excavator_activated,
        )

    def authenticate_admin(self):
        session = self.client.session
        session['employee_access_id'] = self.admin_access.id
        session.save()

    def test_dashboard_builds_funnel_charts_and_attention_table(self):
        self.authenticate_admin()

        response = self.client.get('/system-admin/registrations/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], 4)
        self.assertEqual(response.context['access_issued'], 3)
        self.assertEqual(response.context['activated'], 2)
        self.assertEqual(response.context['logged_in'], 1)
        self.assertEqual(response.context['awaiting_activation'], 1)
        self.assertEqual(response.context['no_access'], 1)
        self.assertEqual(response.context['coverage_percent'], 50)
        self.assertEqual(sum(item['new_count'] for item in response.context['chart']['days']), 2)
        self.assertContains(response, 'Прирост регистраций по дням')
        self.assertContains(response, 'Охват подключения')
        self.assertContains(response, '<polyline', html=False)
        self.assertContains(response, 'Алексеев Активированный')
        self.assertContains(response, 'Волков Без Доступа')
        self.assertContains(response, 'Установка PWA или APK пока не фиксируется отдельно')

    def test_dashboard_filters_role_shift_and_activated_people(self):
        self.authenticate_admin()

        driver_response = self.client.get(
            '/system-admin/registrations/?role=driver&period=14',
            HTTP_HOST='localhost',
        )
        night_response = self.client.get(
            '/system-admin/registrations/?shift=night',
            HTTP_HOST='localhost',
        )
        activated_response = self.client.get(
            '/system-admin/registrations/?state=activated',
            HTTP_HOST='localhost',
        )

        self.assertEqual(driver_response.context['total'], 3)
        self.assertEqual(driver_response.context['activated'], 1)
        self.assertEqual(driver_response.context['period_days'], 14)
        self.assertEqual(night_response.context['total'], 1)
        self.assertEqual(night_response.context['activated'], 0)
        self.assertEqual(activated_response.context['visible_total'], 2)
        self.assertContains(activated_response, 'Алексеев Активированный')
        self.assertContains(activated_response, 'Громов Экскаваторщик')
        self.assertNotContains(activated_response, 'Борисов Ожидающий')

    def test_dashboard_requires_admin_access(self):
        response = self.client.get('/system-admin/registrations/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/home/')

    def test_blocked_access_is_not_reported_as_current_usage(self):
        self.authenticate_admin()
        self.driver_waiting_access.status = EmployeeAccess.Status.BLOCKED
        self.driver_waiting_access.last_login_at = timezone.now() - timedelta(hours=1)
        self.driver_waiting_access.save(update_fields=['status', 'last_login_at'])

        response = self.client.get('/system-admin/registrations/', HTTP_HOST='localhost')

        self.assertEqual(response.context['logged_in'], 1)
        self.assertEqual(response.context['awaiting_activation'], 1)
        self.assertContains(response, 'Доступ заблокирован')

    def test_system_admin_summary_links_to_registration_dashboard(self):
        self.authenticate_admin()

        response = self.client.get('/system-admin/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/system-admin/registrations/"')
        self.assertContains(response, 'Подключение сотрудников')
