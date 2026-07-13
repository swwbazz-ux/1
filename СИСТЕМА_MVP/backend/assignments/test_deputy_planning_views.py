import json
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    Equipment,
    EquipmentModel,
    EquipmentType,
)
from shifts.models import EmployeeShift
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role

from .models import AssignmentStatus, CrewPlanSlot, CrewPlanStatus, EquipmentAssignment, WorkShiftType
from .services import get_active_equipment_assignment, get_or_create_crew_draft, set_active_equipment_assignment


class DeputyPlanningViewTests(TestCase):
    def setUp(self):
        self.deputy_role, _created = Role.objects.update_or_create(
            code='deputy_mining_manager',
            defaults={
                'name': 'Зам. начальника горного участка',
                'is_active': True,
            },
        )
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        self.excavator_role = Role.objects.create(
            code='excavator_operator',
            name='Машинист экскаватора',
        )
        self.deputy, self.deputy_access = self.create_employee_with_access(
            'Заместитель начальника участка',
            self.deputy_role,
            phone='+79000000010',
            access_code='610001',
        )
        self.driver, self.driver_access = self.create_employee_with_access(
            'Иванов Сергей Петрович',
            self.driver_role,
            phone='+79000000001',
            access_code='210001',
        )

        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.truck_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='БелАЗ тестовый',
            fuel_capacity_limit_l='2000',
        )
        self.truck_1 = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='Т-01',
        )
        self.truck_2 = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='Т-02',
        )
        self.original_assignment, _created = set_active_equipment_assignment(
            employee=self.driver,
            role=self.driver_role,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.deputy,
        )

        dormitory = Dormitory.objects.create(number='Тест')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='Блок')
        section = DormitorySection.objects.create(block=block, name='Секция')
        DriverPrimaryRegistration.objects.create(
            employee=self.driver,
            dormitory_section=section,
        )

        self.authenticate(self.client, self.deputy_access)

    def create_employee_with_access(self, full_name, role, *, phone, access_code):
        employee = Employee.objects.create(
            full_name=full_name,
            phone=phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee, access

    def authenticate(self, client, access):
        session = client.session
        session['employee_access_id'] = access.id
        session.save()

    def create_draft(self):
        plan, _created = get_or_create_crew_draft(
            role=self.driver_role,
            actor=self.deputy,
        )
        return plan

    def autosave_slot(self, plan, *, equipment, shift_type, employee):
        response = self.client.post(
            reverse('deputy_mining_manager_slot'),
            data=json.dumps({
                'plan_id': plan.id,
                'expected_version': plan.version,
                'equipment_id': equipment.id,
                'shift_type': shift_type,
                'employee_id': employee.id if employee else None,
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 200, response.content)
        plan.refresh_from_db()
        return response, plan

    def autosave_driver_on_second_truck(self, plan):
        return self.autosave_slot(
            plan,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.driver,
        )

    def publish(self, plan):
        response = self.client.post(
            reverse('deputy_mining_manager_publish'),
            data=json.dumps({
                'plan_id': plan.id,
                'expected_version': plan.version,
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def test_profile_is_available_only_to_deputy_and_role_home_routes_to_board(self):
        response = self.client.get(reverse('role_home'), HTTP_HOST='localhost')
        self.assertRedirects(
            response,
            reverse('deputy_mining_manager_placement'),
            fetch_redirect_response=False,
        )

        driver_client = Client()
        self.authenticate(driver_client, self.driver_access)
        for view_name in (
            'deputy_mining_manager_placement',
            'deputy_mining_manager_reports',
        ):
            forbidden_page = driver_client.get(reverse(view_name), HTTP_HOST='localhost')
            self.assertRedirects(
                forbidden_page,
                reverse('role_home'),
                fetch_redirect_response=False,
            )

        plan = self.create_draft()
        forbidden_slot = driver_client.post(
            reverse('deputy_mining_manager_slot'),
            data=json.dumps({
                'plan_id': plan.id,
                'expected_version': plan.version,
                'equipment_id': self.truck_2.id,
                'shift_type': WorkShiftType.SHIFT_1,
                'employee_id': self.driver.id,
            }),
            content_type='application/json',
            HTTP_HOST='localhost',
        )
        forbidden_publish = driver_client.post(
            reverse('deputy_mining_manager_publish'),
            data=json.dumps({'plan_id': plan.id, 'expected_version': plan.version}),
            content_type='application/json',
            HTTP_HOST='localhost',
        )
        self.assertEqual(forbidden_slot.status_code, 403)
        self.assertEqual(forbidden_publish.status_code, 403)

    def test_get_board_builds_driver_plan_with_day_and_night_slots(self):
        response = self.client.get(
            reverse('deputy_mining_manager_placement'),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'assignments/deputy_mining_manager_placement.html')
        payload = response.context['planning_payload']
        self.assertEqual(payload['role']['code'], 'driver')
        self.assertEqual(payload['summary']['equipment_total'], 2)
        self.assertEqual(payload['summary']['assigned_count'], 1)
        self.assertEqual(payload['summary']['unfilled_count'], 3)
        self.assertEqual(len(payload['rows']), 2)
        self.assertTrue(all(len(row['slots']) == 2 for row in payload['rows']))

    def test_autosave_changes_draft_but_not_equipment_assignment(self):
        plan = self.create_draft()

        response, plan = self.autosave_driver_on_second_truck(plan)

        self.assertTrue(response.json()['ok'])
        self.original_assignment.refresh_from_db()
        self.assertIsNone(self.original_assignment.ended_at)
        self.assertEqual(
            get_active_equipment_assignment(self.driver, 'driver').equipment,
            self.truck_1,
        )
        self.assertEqual(
            plan.slots.get(
                equipment=self.truck_2,
                shift_type=WorkShiftType.SHIFT_1,
            ).employee,
            self.driver,
        )

    def test_autosave_moves_employee_between_equipment_and_shift(self):
        plan = self.create_draft()

        response, plan = self.autosave_slot(
            plan,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_2,
            employee=self.driver,
        )

        self.assertTrue(response.json()['ok'])
        self.assertIsNone(
            plan.slots.get(
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_1,
            ).employee,
        )
        self.assertEqual(
            plan.slots.get(
                equipment=self.truck_2,
                shift_type=WorkShiftType.SHIFT_2,
            ).employee,
            self.driver,
        )
        active_assignment = get_active_equipment_assignment(self.driver, 'driver')
        self.assertEqual(active_assignment.equipment, self.truck_1)
        self.assertEqual(active_assignment.shift_type, WorkShiftType.SHIFT_1)

    def test_autosave_clears_slot_and_returns_employee_to_free_pool(self):
        plan = self.create_draft()

        response, plan = self.autosave_slot(
            plan,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            employee=None,
        )

        self.assertIsNone(
            plan.slots.get(
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_1,
            ).employee,
        )
        free_employee_ids = {
            item['id'] for item in response.json()['payload']['employees']
        }
        self.assertIn(self.driver.id, free_employee_ids)
        self.assertEqual(
            get_active_equipment_assignment(self.driver, 'driver').equipment,
            self.truck_1,
        )

    def test_autosave_locks_only_slot_table_with_nullable_employee_join(self):
        plan = self.create_draft()

        with patch.object(
            CrewPlanSlot.objects,
            'select_for_update',
            wraps=CrewPlanSlot.objects.select_for_update,
        ) as slot_lock:
            self.autosave_slot(
                plan,
                equipment=self.truck_1,
                shift_type=WorkShiftType.SHIFT_1,
                employee=None,
            )

        self.assertTrue(any(
            call.kwargs.get('of') == ('self',)
            for call in slot_lock.call_args_list
        ))

    def test_publish_replaces_base_equipment_assignment(self):
        plan = self.create_draft()
        _response, plan = self.autosave_driver_on_second_truck(plan)

        response = self.publish(plan)

        self.assertTrue(response.json()['published'])
        plan.refresh_from_db()
        self.assertEqual(plan.status, CrewPlanStatus.PUBLISHED)
        self.original_assignment.refresh_from_db()
        self.assertIsNotNone(self.original_assignment.ended_at)
        self.assertEqual(self.original_assignment.ended_by, self.deputy)
        active_assignment = get_active_equipment_assignment(self.driver, 'driver')
        self.assertEqual(active_assignment.equipment, self.truck_2)
        self.assertEqual(active_assignment.shift_type, WorkShiftType.SHIFT_1)
        self.assertEqual(active_assignment.assigned_by, self.deputy)
        self.assertEqual(
            EquipmentAssignment.objects.filter(
                employee=self.driver,
                role=self.driver_role,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
            ).count(),
            1,
        )

    def test_publish_locks_only_slot_table_with_nullable_employee_joins(self):
        plan = self.create_draft()

        with patch.object(
            CrewPlanSlot.objects,
            'select_for_update',
            wraps=CrewPlanSlot.objects.select_for_update,
        ) as slot_lock:
            self.publish(plan)

        self.assertTrue(any(
            call.kwargs.get('of') == ('self',)
            for call in slot_lock.call_args_list
        ))

    def test_published_move_is_visible_in_admin_employee_card(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        _admin, admin_access = self.create_employee_with_access(
            'Администратор тестовый',
            admin_role,
            phone='+79000000099',
            access_code='110099',
        )
        admin_client = Client()
        self.authenticate(admin_client, admin_access)
        plan = self.create_draft()
        _response, plan = self.autosave_slot(
            plan,
            equipment=self.truck_2,
            shift_type=WorkShiftType.SHIFT_2,
            employee=self.driver,
        )

        self.publish(plan)
        response = admin_client.get(
            reverse('system_admin_employee_detail', args=[self.driver.id]),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        active_assignment = response.context['active_equipment_assignment']
        self.assertEqual(active_assignment.equipment, self.truck_2)
        self.assertEqual(active_assignment.shift_type, WorkShiftType.SHIFT_2)

    def test_published_removal_is_visible_in_admin_employee_card(self):
        admin_role = Role.objects.create(code='admin', name='Администратор')
        _admin, admin_access = self.create_employee_with_access(
            'Администратор тестовый',
            admin_role,
            phone='+79000000099',
            access_code='110099',
        )
        admin_client = Client()
        self.authenticate(admin_client, admin_access)
        plan = self.create_draft()
        _response, plan = self.autosave_slot(
            plan,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            employee=None,
        )

        self.publish(plan)
        response = admin_client.get(
            reverse('system_admin_employee_detail', args=[self.driver.id]),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['active_equipment_assignment'])
        self.assertContains(response, 'Рабочее назначение не задано')

    def test_publish_does_not_change_snapshot_of_already_open_shift(self):
        open_shift = EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck_1,
            shift_type=WorkShiftType.SHIFT_1,
            opened_at=timezone.now(),
            opened_by=self.driver,
        )
        plan = self.create_draft()
        _response, plan = self.autosave_driver_on_second_truck(plan)

        self.publish(plan)

        open_shift.refresh_from_db()
        self.assertEqual(open_shift.equipment, self.truck_1)
        self.assertEqual(open_shift.shift_type, WorkShiftType.SHIFT_1)
        self.assertIsNone(open_shift.closed_at)
        self.assertEqual(
            get_active_equipment_assignment(self.driver, 'driver').equipment,
            self.truck_2,
        )

    def test_driver_login_sees_new_equipment_after_publication(self):
        plan = self.create_draft()
        _response, plan = self.autosave_driver_on_second_truck(plan)
        self.publish(plan)
        driver_client = Client()

        response = driver_client.post(
            reverse('login'),
            {
                'phone': '+7 (900) 000-00-01',
                'access_code': '210001',
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(driver_client.session['employee_access_id'], self.driver_access.id)
        self.assertEqual(response.context['work_assignment_state'], 'assigned')
        self.assertEqual(response.context['work_assignment_equipment'], self.truck_2)
        self.assertEqual(response.context['work_assignment'].shift_type, WorkShiftType.SHIFT_1)

    def test_reports_page_lists_published_crew_plan(self):
        plan = self.create_draft()
        self.publish(plan)

        response = self.client.get(
            reverse('deputy_mining_manager_reports'),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'reports/deputy_mining_manager_reports.html')
        publications = response.context['publications']
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0].id, plan.id)
        self.assertEqual(publications[0].slot_count, 4)
        self.assertEqual(publications[0].assigned_count, 1)
        self.assertIn(f'plan={plan.id}', publications[0].url)

        detail_response = self.client.get(publications[0].url, HTTP_HOST='localhost')
        self.assertEqual(detail_response.status_code, 200)
        detail_payload = detail_response.context['planning_payload']
        self.assertEqual(detail_payload['plan']['id'], plan.id)
        self.assertEqual(detail_payload['plan']['status'], CrewPlanStatus.PUBLISHED)
        self.assertFalse(detail_payload['plan']['editable'])
        self.assertEqual(detail_payload['employees'], [])
