from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from references.models import Equipment, EquipmentType
from shifts.models import WatchPeriod
from users.models import Employee, EmployeeAccess, PersonnelPosition, Role, WatchComposition

from .models import CrewOffEquipment, CrewPlan, CrewPlanSlot, OffEquipmentStatus, WorkShiftType
from .services import production_work_date


class OffEquipmentOverviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.day = production_work_date()
        role, _ = Role.objects.get_or_create(code='deputy_mining_manager', defaults={'name': 'Замначальника'})
        deputy = Employee.objects.create(full_name='Проверочный Заместитель', status=Employee.Status.ACTIVE)
        cls.access = EmployeeAccess.objects.create(
            employee=deputy, role=role, access_code='184200', status=EmployeeAccess.Status.ACTIVATED,
        )
        position = PersonnelPosition.objects.create(code='overview_operator', name='Машинист тестовой машины')
        composition = WatchComposition.objects.create(code='overview_watch', name='Проверочный состав')
        WatchPeriod.objects.create(name='Текущая вахта', watch_composition=composition,
                                   starts_on=cls.day, ends_on=cls.day)

        def worker(name, **kwargs):
            return Employee.objects.create(
                full_name=name, work_category=Employee.WorkCategory.DRIVER,
                status=Employee.Status.ACTIVE, **kwargs,
            )

        cls.review = worker('Однофамилов Пётр', personnel_number='OA001', personnel_position=position,
                            watch_composition=composition, brigade_number=1)
        cls.reserve = worker('Резерв Сотрудник', position='Водитель тестовой машины', brigade_number=1,
                             watch_composition=composition)
        cls.suspended = worker('Отстранение Сотрудник', brigade_number=2)
        cls.foreign = worker('Чужая Бригада', brigade_number=3)
        cls.extra = worker('Однофамилов Второй', brigade_number=1, personnel_number='OA002')
        cls.assigned = worker('Назначенный Стажёр', watch_composition=composition, brigade_number=1)
        equipment_type = EquipmentType.objects.create(name='Тестовая техника')
        equipment = Equipment.objects.create(equipment_type=equipment_type, garage_number='QA-018')
        plan = CrewPlan.objects.create(work_date=cls.day, role=role)
        CrewPlanSlot.objects.create(plan=plan, equipment=equipment, shift_type=WorkShiftType.SHIFT_2,
                                    secondary_employee=cls.assigned)
        for employee, status, day in [
            (cls.reserve, OffEquipmentStatus.RESERVE, cls.day),
            (cls.foreign, OffEquipmentStatus.OTHER_WORK, cls.day),
            (cls.suspended, OffEquipmentStatus.SUSPENDED, cls.day - timedelta(days=1)),
        ]:
            CrewOffEquipment.objects.create(employee=employee, status=status, work_date=day,
                                           shift_type=WorkShiftType.SHIFT_1)

    def setUp(self):
        session = self.client.session
        session['employee_access_id'] = self.access.pk
        session.save()
        self.url = reverse('deputy_off_equipment')
        self.enterContext(patch('assignments.off_equipment_views._deputy_brigades', return_value=('1', '2')))

    def test_summary_excludes_other_brigades_and_assignments_and_retains_all_states(self):
        overview = self.client.get(self.url).context
        self.assertEqual(overview['summary_total'], 3)
        self.assertEqual(overview['status_counts'], {'review': 1, 'reserve': 1, 'other_work': 0, 'suspended': 1})
        self.assertEqual(overview['filtered_count'], 3)
        self.assertEqual(overview['outside_count'], 0)
        self.assertEqual({row['employee'].pk for row in overview['rows']},
                         {self.review.pk, self.reserve.pk, self.suspended.pk})
        self.assertEqual([card['count'] for card in overview['status_cards']], [3, 1, 1, 0, 1])
        self.assertIn('Машинист тестовой машины', overview['profession_options'])
        with patch('assignments.off_equipment_views.approved_watch_extensions_on',
                   return_value={self.foreign.pk: object()}):
            extended = self.client.get(self.url).context
        self.assertEqual(extended['summary_total'], 4)
        self.assertEqual(extended['status_counts']['other_work'], 1)

    def test_name_lookup_includes_outside_daily_list_but_keeps_totals(self):
        for query in ['Однофамилов', 'однофамилов', 'OA00']:
            with self.subTest(query=query):
                context = self.client.get(self.url, {'q': query}).context
                self.assertEqual(context['summary_total'], 3)
                self.assertEqual(context['filtered_count'], 2)
                self.assertEqual(context['outside_count'], 1)
                self.assertEqual({row['employee'].pk: row['outside_roster'] for row in context['rows']},
                                 {self.review.pk: False, self.extra.pk: True})
        for name in [self.foreign.full_name, self.assigned.full_name]:
            self.assertEqual(self.client.get(self.url, {'q': name}).context['filtered_count'], 0)
        for query in ['петр однофамилов', 'однофамилов OA001']:
            self.assertEqual(self.client.get(self.url, {'q': query}).context['filtered_count'], 1)
        self.assertEqual(self.client.get(self.url, {'q': '.*'}).context['filtered_count'], 0)

    def test_profession_and_status_intersect_without_changing_summary(self):
        filters = {'status': 'reserve', 'profession': self.reserve.position}
        response = self.client.get(self.url, filters, headers={'X-Duty-Overview': '1'})
        self.assertTemplateUsed(response, 'assignments/includes/deputy_duty_overview.html')
        self.assertTemplateNotUsed(response, 'assignments/deputy_off_equipment.html')
        self.assertEqual(response.context['summary_total'], 3)
        self.assertEqual([row['employee'].pk for row in response.context['rows']], [self.reserve.pk])
        no_matches = self.client.get(self.url, {**filters, 'status': 'review'}).context
        self.assertEqual(no_matches['filtered_count'], 0)
        self.assertEqual(no_matches['summary_total'], 3)

    def test_validation_error_preserves_the_selected_row_input(self):
        response = self.client.post(self.url, {
            'employee_id': self.review.pk, 'work_date': self.day.isoformat(),
            'status': 'reserve', 'shift_type': '', 'location': 'Участок проверки', 'expected_version': '',
        })
        self.assertEqual(response.status_code, 400)
        row = next(row for row in response.context['rows'] if row['employee'].pk == self.review.pk)
        self.assertEqual((row['form_status'], row['form_shift'], row['form_location']),
                         ('reserve', '', 'Участок проверки'))
        self.assertFalse(CrewOffEquipment.objects.filter(employee=self.review).exists())

    def test_empty_brigade_keeps_zero_counts_and_can_find_no_foreign_employee(self):
        with patch('assignments.off_equipment_views._deputy_brigades', return_value=('4',)):
            response = self.client.get(self.url, {'q': 'Однофамилов'}, headers={'X-Duty-Overview': '1'})
        self.assertEqual(response.context['summary_total'], 0)
        self.assertEqual(response.context['filtered_count'], 0)
        self.assertEqual([card['count'] for card in response.context['status_cards']], [0] * 5)

    def test_overlapping_draft_and_protected_records_explain_readonly_rows(self):
        CrewOffEquipment.objects.create(employee=self.assigned, work_date=self.day,
                                       status=OffEquipmentStatus.RESERVE, shift_type=WorkShiftType.SHIFT_1)
        Employee.objects.filter(pk=self.review.pk).update(is_protected=True)
        rows = {row['employee'].pk: row for row in self.client.get(self.url).context['rows']}
        self.assertFalse(rows[self.assigned.pk]['editable'])
        self.assertEqual(rows[self.assigned.pk]['readonly_reason'], 'Есть назначение на технику')
        self.assertFalse(rows[self.review.pk]['editable'])
        self.assertEqual(rows[self.review.pk]['readonly_reason'], 'Карточка только для просмотра')
        self.assertTrue(rows[self.reserve.pk]['editable'])

    def test_stale_editor_shows_latest_values_and_does_not_overwrite_other_users(self):
        response = self.client.post(self.url, {
            'employee_id': self.reserve.pk, 'work_date': self.day.isoformat(),
            'status': 'other_work', 'shift_type': '', 'location': 'Устаревший ввод', 'expected_version': 'stale',
        })
        self.assertContains(response, 'Статус уже изменился', status_code=400)
        row = next(row for row in response.context['rows'] if row['employee'].pk == self.reserve.pk)
        self.assertEqual((row['form_status'], row['form_shift'], row['form_location']), ('reserve', 'day', ''))
        self.assertEqual(CrewOffEquipment.objects.get(employee=self.reserve).status, OffEquipmentStatus.RESERVE)
