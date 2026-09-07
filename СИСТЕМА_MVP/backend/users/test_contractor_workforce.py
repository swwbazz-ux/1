from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import WorkShiftType
from assignments.services import validate_work_assignment
from references.models import Equipment, EquipmentModel, EquipmentType

from .models import (
    ContractorOrganization,
    Employee,
    EmployeeAccess,
    ProductionSpecialization,
    Role,
)
from .access_auth import find_unactivated_accesses_by_phone
from .active_role import active_access_for_employee_role
from .work_profiles import eligible_employee_ids_for_work_role


@override_settings(ALLOWED_HOSTS=['testserver'])
class ContractorWorkforceTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.role, _ = Role.objects.update_or_create(
            code='excavator_operator',
            defaults={'name': 'Машинист экскаватора', 'is_active': True},
        )
        self.equipment_type, _ = EquipmentType.objects.update_or_create(
            name='Экскаватор',
            defaults={'is_active': True},
        )
        self.specialization, _ = ProductionSpecialization.objects.update_or_create(
            code='excavator_operator',
            defaults={
                'name': 'Машинист экскаватора',
                'equipment_type': self.equipment_type,
                'access_role': self.role,
                'is_active': True,
            },
        )
        if self.specialization.access_role_id != self.role.id:
            self.specialization.access_role = self.role
            self.specialization.equipment_type = self.equipment_type
            self.specialization.is_active = True
            self.specialization.save(update_fields=['access_role', 'equipment_type', 'is_active'])

        self.organization = ContractorOrganization.objects.create(
            name='ООО Подрядчик Восток',
            short_name='Подрядчик Восток',
            contract_number='П-01',
            contract_valid_from=self.today - timedelta(days=30),
            contract_valid_until=self.today + timedelta(days=30),
        )
        self.other_organization = ContractorOrganization.objects.create(
            name='ООО Другой Подрядчик',
            contract_valid_from=self.today - timedelta(days=30),
            contract_valid_until=self.today + timedelta(days=30),
        )
        model, _ = EquipmentModel.objects.update_or_create(
            equipment_type=self.equipment_type,
            name='Тестовый экскаватор подрядчика',
            defaults={'is_active': True},
        )
        self.equipment = Equipment.objects.create(
            equipment_type=self.equipment_type,
            model=model,
            garage_number='ПЭ-001',
            is_own=False,
            contractor_organization=self.organization,
        )
        self.other_equipment = Equipment.objects.create(
            equipment_type=self.equipment_type,
            model=model,
            garage_number='ПЭ-002',
            is_own=False,
            contractor_organization=self.other_organization,
        )

    def create_employee(self, *, phone='+79990000801', access_until=None):
        return Employee.objects.create(
            full_name='Подрядчиков Петр Петрович',
            phone=phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
            employment_type=Employee.EmploymentType.CONTRACTOR,
            contractor_organization=self.organization,
            contractor_access_from=self.today - timedelta(days=1),
            contractor_access_until=access_until or self.today + timedelta(days=10),
            base_specialization=self.specialization,
            work_category=Employee.WorkCategory.EXCAVATOR_OPERATOR,
        )

    def test_active_contractor_is_available_for_excavator_placement(self):
        employee = self.create_employee()

        self.assertIn(employee.id, eligible_employee_ids_for_work_role('excavator_operator'))

    def test_expired_contractor_is_hidden_from_placement_and_start_page(self):
        employee = self.create_employee(access_until=self.today - timedelta(days=1))
        EmployeeAccess.objects.create(
            employee=employee,
            role=self.role,
            access_code='771122',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )

        self.assertNotIn(employee.id, eligible_employee_ids_for_work_role('excavator_operator'))
        self.assertEqual(find_unactivated_accesses_by_phone(employee.phone), [])
        self.assertIsNone(active_access_for_employee_role(employee, 'excavator_operator'))

    def test_active_contractor_gets_only_the_excavator_app(self):
        employee = self.create_employee()
        EmployeeAccess.objects.create(
            employee=employee,
            role=self.role,
            access_code='771123',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )

        response = self.client.post(reverse('universal_start'), {'phone': employee.phone}, follow=True)

        self.assertTrue(response.context['found'])
        self.assertEqual([item['app'].role_code for item in response.context['apps']], ['excavator_operator'])

    def test_contractor_can_be_assigned_to_equipment_of_own_organization(self):
        employee = self.create_employee()

        validate_work_assignment(
            employee=employee,
            role=self.role,
            equipment=self.equipment,
            shift_type=WorkShiftType.SHIFT_1,
        )

    def test_contractor_cannot_be_assigned_to_another_organizations_equipment(self):
        employee = self.create_employee()

        with self.assertRaisesMessage(
            ValidationError,
            'Организация сотрудника не совпадает с владельцем техники.',
        ):
            validate_work_assignment(
                employee=employee,
                role=self.role,
                equipment=self.other_equipment,
                shift_type=WorkShiftType.SHIFT_1,
            )

    def test_contractor_card_requires_organization_and_access_dates(self):
        employee = Employee(
            full_name='Подрядчиков Без Допуска',
            employment_type=Employee.EmploymentType.CONTRACTOR,
        )

        with self.assertRaises(ValidationError) as error:
            employee.full_clean()

        self.assertIn('contractor_organization', error.exception.message_dict)
        self.assertIn('contractor_access_from', error.exception.message_dict)
        self.assertIn('contractor_access_until', error.exception.message_dict)

    def test_database_rejects_contractor_without_required_fields(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Employee.objects.create(
                full_name='Подрядчиков Без Реквизитов',
                employment_type=Employee.EmploymentType.CONTRACTOR,
            )
