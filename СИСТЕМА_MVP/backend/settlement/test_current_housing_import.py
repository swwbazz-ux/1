from datetime import date, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core import signing
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from openpyxl import Workbook
from openpyxl.styles import Color, PatternFill

from core.production_time import production_day_bounds
from references.models import Dormitory
from users.models import Employee, EmployeeAccess, PersonnelPosition, Role

from .models import (
    EmployeeBedOccupancy, PhysicalBed, PhysicalRoom, SettlementBedPreparation, SettlementPlacementEvent,
    SettlementResident,
)


class CurrentHousingPreviewTests(TestCase):
    as_of = date(2026, 11, 23)
    starts = date(2026, 11, 20)
    ends = date(2026, 12, 14)

    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(self.settings(PORTAL_PRIVATE_MEDIA_ROOT=Path(root)))
        self.draft_revision = ''

    @classmethod
    def setUpTestData(cls):
        clerk = Employee.objects.create(
            full_name='Тестовый Делопроизводитель Отдела', phone='+79005550601', status=Employee.Status.ACTIVE,
        )
        role, _ = Role.objects.get_or_create(code='settlement_clerk', defaults={'name': 'Делопроизводитель'})
        cls.access = EmployeeAccess.objects.create(
            employee=clerk, role=role, access_code='510001', status=EmployeeAccess.Status.ACTIVATED,
        )
        worker = PersonnelPosition.objects.create(code='qa-worker', name='Рабочая должность QA', settlement_category='worker')
        itr = PersonnelPosition.objects.create(code='qa-itr', name='Должность ИТР QA', settlement_category='itr')
        cls.workers = [
            Employee.objects.create(
                full_name=name, phone=f'+7900555061{index}', personnel_position=worker,
                status=Employee.Status.ACTIVE, sex='male',
            )
            for index, name in enumerate([
                'Иванов Иван Иванович', 'Петров Петр Петрович', 'Крылов Кирилл Игоревич',
                'Междувахтов Иван Иванович', 'Занятый Игорь Иванович', 'Ранний Иван Иванович',
                'Текущий Игорь Иванович',
            ])
        ]
        cls.itr = Employee.objects.create(
            full_name='Сидоров Сергей Сергеевич', phone='+79005550619', personnel_position=itr,
            status=Employee.Status.ACTIVE, sex='male',
        )
        cls.resident = SettlementResident.objects.create(employee=cls.workers[4], resident_type='EMPLOYEE')
        SettlementResident.objects.create(employee=cls.workers[5], resident_type='EMPLOYEE')
        cls.current_resident = SettlementResident.objects.create(employee=cls.workers[6], resident_type='EMPLOYEE')
        starts, _ = production_day_bounds(cls.starts)
        ends, _ = production_day_bounds(cls.ends)
        dormitory, _ = Dormitory.objects.get_or_create(number='5')
        for number in range(1, 5):
            room, _ = PhysicalRoom.objects.get_or_create(
                dormitory=dormitory, floor=1, number=number,
                defaults={'transfer_status': 'transferred', 'capacity': 6, 'corridor_side': 'left', 'side_position': number},
            )
            for position in range(1, 4):
                PhysicalBed.objects.get_or_create(
                    room=room, block='A', position=position,
                    defaults={'stable_id': f'KIS5-F1-R{number:02}-A{position}'},
                )
        for room, resident in [(2, cls.resident), (4, cls.current_resident)]:
            bed = PhysicalBed.objects.get(stable_id=f'KIS5-F1-R{room:02}-A1')
            EmployeeBedOccupancy.objects.create(
                resident=resident, physical_bed=bed, assignment_type='temporary',
                starts_at=starts, ends_at=ends, work_shift='day', settled_by=clerk,
                shift_source_kind='clerk_selected', shift_source_fingerprint='test-current-housing',
                shift_selected_by_access=cls.access, shift_selected_at=starts,
                shift_selection_basis='Проверка предпросмотра Excel.',
            )

    def upload(self, rows, *, shift='День', edits=None):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'КИС 5'
        for col, title in {
            'C': 'Номер кровати', 'J': 'Ф.И.О. сотрудника', 'K': 'Смена',
            'O': 'Дата заселения', 'P': 'Дата выезда',
        }.items():
            sheet[f'{col}2'] = title
        current_room = None
        for index, (room, bed, person, blue, ends) in enumerate(rows, start=3):
            if room != current_room:
                if current_room is not None:
                    sheet.merge_cells(start_row=room_start, end_row=index - 1, start_column=2, end_column=2)
                current_room, room_start = room, index
                sheet[f'B{index}'] = room
            sheet[f'C{index}'] = bed
            sheet[f'J{index}'] = person.full_name
            sheet[f'K{index}'] = shift
            sheet[f'O{index}'] = self.starts
            sheet[f'P{index}'] = ends
            if blue:
                sheet[f'J{index}'].fill = PatternFill('solid', fgColor=Color(theme=4, tint=.6))
        sheet.merge_cells(start_row=room_start, end_row=index, start_column=2, end_column=2)
        for coordinate, value in (edits or {}).items():
            sheet[coordinate] = value
        data = BytesIO()
        workbook.save(data)
        workbook.close()
        return SimpleUploadedFile('housing.xlsx', data.getvalue())

    def use_clerk_session(self):
        session = self.client.session
        session['employee_access_id'] = self.access.pk
        session.save()

    def test_protected_preview_reports_interwatch_and_conflicts_without_writes(self):
        url = reverse('settlement_current_housing_import')
        self.assertEqual(self.client.get(url).status_code, 302)
        self.use_clerk_session()

        workbook = self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (1, '01А-2', self.itr, False, self.ends),
            (1, '01А-3', self.workers[3], True, self.ends),
            (2, '02А-1', self.workers[1], False, self.ends),
            (2, '02А-2', self.workers[5], False, self.as_of),
            (3, '03А-1', self.workers[2], False, self.ends),
            (3, '03А-2', self.workers[5], False, self.ends),
            (4, '04А-1', self.workers[6], False, self.ends),
            (999, '999А-1', self.workers[4], False, self.ends),
        ])
        response = self.client.post(url, {'workbook': workbook, 'as_of': self.as_of.isoformat()})
        self.assertContains(response, 'Предпросмотр на 23.11.2026')
        self.assertIn('no-store', response['Cache-Control'])
        groups = {item['code']: item['rows'] for item in response.context['report_groups']}
        self.assertEqual({key: len(rows) for key, rows in groups.items()}, {
            'ready': 1, 'needs_resident': 3, 'conflict': 2, 'interwatch': 1, 'outside': 1, 'existing': 1,
        })
        self.assertEqual(groups['interwatch'][0].source, 'КИС 5!J5')
        self.assertEqual([row.bed_id for row in groups['needs_resident']], [
            'KIS5-F1-R01-A1', 'KIS5-F1-R01-A2', 'KIS5-F1-R03-A1',
        ])
        self.assertEqual(groups['ready'][0].bed_id, 'KIS5-F1-R03-A2')
        self.assertContains(response, f'На карте: {self.resident.display_name}')
        self.assertContains(response, 'Комната отсутствует в жилфонде')
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)
        self.assertEqual(SettlementResident.objects.count(), 3)

    def test_invalid_excel_returns_inline_error(self):
        self.use_clerk_session()
        response = self.client.post(reverse('settlement_current_housing_import'), {
            'workbook': SimpleUploadedFile('housing.xlsx', b'invalid'),
            'as_of': self.as_of.isoformat(),
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['form'].errors.as_data()['workbook'][0].code, 'unsafe_xlsx')

    def test_preview_reports_prepared_bed_and_keeps_reservation(self):
        self.use_clerk_session()
        starts, ends = production_day_bounds(self.as_of)
        hold = SettlementBedPreparation.objects.create(
            resident=self.resident, physical_bed=PhysicalBed.objects.get(stable_id='KIS5-F1-R03-A1'),
            arrival_date=self.as_of, hold_until=ends, created_by_access=self.access,
        )
        SettlementBedPreparation.objects.filter(pk=hold.pk).update(created_at=starts)
        response = self.client.post(reverse('settlement_current_housing_import'), {
            'workbook': self.upload([(3, '03А-1', self.workers[2], False, self.ends)]),
            'as_of': self.as_of.isoformat(),
        })
        self.assertContains(response, 'Койка подготовлена и удерживается до приезда жильца.')
        hold.refresh_from_db()
        self.assertEqual(hold.status, SettlementBedPreparation.Status.PREPARED)

    def submit(self, payload=None, *, filename='housing.xlsx', **extra):
        data = {'as_of': self.as_of.isoformat(), 'draft_revision': self.draft_revision, **extra}
        if payload is not None:
            data['workbook'] = SimpleUploadedFile(filename, payload)
        response = self.client.post(reverse('settlement_current_housing_import'), data)
        if response.context and response.context.get('draft'):
            self.draft_revision = response.context['draft'].revision
        return response

    def test_apply_creates_missing_resident_and_is_idempotent(self):
        self.use_clerk_session()
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (1, '01А-2', self.workers[3], True, self.ends),
            (2, '02А-1', self.workers[1], False, self.ends),
            (3, '03А-2', self.workers[5], False, self.ends),
        ]).read()
        start, _ = production_day_bounds(self.as_of)
        with patch('django.utils.timezone.now', return_value=start + timedelta(hours=4)):
            preview = self.submit(payload)
            self.assertEqual(preview.context['applicable_count'], 2)
            self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)
            self.assertEqual(SettlementResident.objects.count(), 3)
            receipt = preview.context['receipt']
            applied = self.submit(action='apply', receipt=receipt)
            self.assertEqual(applied.status_code, 200)
            self.assertEqual(applied.context['application'], {'placed': 2, 'residents': 1})
            repeated = self.submit(action='apply', receipt=receipt)
            self.assertEqual(repeated.context['application'], {'placed': 0, 'residents': 0})
        imported = EmployeeBedOccupancy.objects.get(resident__employee=self.workers[0])
        self.assertEqual(imported.physical_bed.stable_id, 'KIS5-F1-R01-A1')
        self.assertEqual(imported.starts_at, production_day_bounds(self.starts)[0])
        self.assertEqual(imported.ends_at, production_day_bounds(self.ends)[0])
        self.assertEqual(imported.work_shift, 'day')
        self.assertEqual(imported.shift_selected_by_access, self.access)
        event = SettlementPlacementEvent.objects.get(resident=imported.resident)
        self.assertEqual(event.payload['source_row'], 'КИС 5!J3')
        self.assertEqual(event.actor_access, self.access)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 4)
        self.assertEqual(SettlementResident.objects.count(), 4)
        self.assertEqual(SettlementPlacementEvent.objects.count(), 2)

    def test_map_tooltip_shows_imported_shift_and_the_field_for_each_personnel_category(self):
        self.use_clerk_session()
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (1, '01А-2', self.itr, False, self.ends),
        ], shift='Ночь').read()
        start, _ = production_day_bounds(self.as_of)
        with patch('django.utils.timezone.now', return_value=start + timedelta(hours=4)):
            preview = self.submit(payload)
            result = self.submit(payload, action='apply', receipt=preview.context['receipt'])
            self.assertEqual(result.context['application']['placed'], 2)
            response = self.client.get(reverse('settlement_map'))
        self.assertContains(response, 'data-shift-label="Ночь"', count=2)
        self.assertContains(response, 'Смена: Ночь', count=2)
        self.assertContains(response, 'Смена: День', count=2)
        self.assertContains(response, f'<strong>{self.workers[0].full_name}</strong>')
        self.assertContains(response, f'<strong>{self.itr.full_name}</strong>')
        self.assertContains(response, '<span>Техника: не назначена</span>')
        self.assertContains(response, f'<span>Должность: {self.itr.personnel_position.name}</span>')

    def test_factual_import_uses_dated_duplicate_and_ignores_previous_resident_note(self):
        self.use_clerk_session()
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, None),
            (1, '01А-1', self.workers[0], False, self.ends),
            (1, '01А-2', self.workers[1], False, None),
            (1, '01А-3', self.workers[3], True, self.ends),
        ], edits={
            'J4': f'заезд 20.11. {self.workers[0].full_name}',
            'R4': 'Прежний Жилец Комнаты увольнение 19.11.',
            'O3': None, 'O5': None, 'K5': None,
        }).read()
        filename = 'РАССЕЛЕНИЕ на 30.10. по 14.12. АБК.xlsx'
        start, _ = production_day_bounds(self.as_of)
        with patch('django.utils.timezone.now', return_value=start + timedelta(hours=4)):
            preview = self.submit(payload, filename=filename)
            self.assertEqual(preview.context['applicable_count'], 2)
            self.assertContains(preview, 'Дубли строк')
            changed_period = self.submit(
                payload, filename='РАССЕЛЕНИЕ на 30.10. по 15.12.xlsx',
                action='apply', receipt=preview.context['receipt'],
            )
            self.assertContains(changed_period, 'Сначала проверьте Excel', status_code=400)
            result = self.submit(payload, filename=filename, action='apply', receipt=preview.context['receipt'])
            self.assertEqual(result.context['application'], {'placed': 2, 'residents': 2})
        first = EmployeeBedOccupancy.objects.get(resident__employee=self.workers[0])
        second = EmployeeBedOccupancy.objects.get(resident__employee=self.workers[1])
        self.assertEqual(first.starts_at, production_day_bounds(self.starts)[0])
        self.assertEqual(second.starts_at, start)
        self.assertEqual(second.ends_at, production_day_bounds(self.ends)[0])
        self.assertIsNone(second.work_shift)

    def test_factual_import_keeps_competing_residents_out_of_apply(self):
        self.use_clerk_session()
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, None),
            (1, '01А-1', self.workers[1], False, self.ends),
            (1, '01А-2', self.workers[2], False, self.ends),
        ], edits={'O3': None}).read()
        result = self.submit(payload, filename='РАССЕЛЕНИЕ на 30.10. по 14.12.xlsx')
        self.assertEqual(result.context['applicable_count'], 1)
        self.assertContains(result, 'Кто занимает койку 01А-1?', count=1)
        self.assertContains(result, 'Выбрать жильца', count=2)

    def test_choose_one_row_undo_and_apply_without_editing_excel(self):
        self.use_clerk_session()
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (1, '01А-1', self.workers[1], False, self.ends),
        ]).read()
        start, _ = production_day_bounds(self.as_of)
        with patch('django.utils.timezone.now', return_value=start + timedelta(hours=4)):
            self.submit(payload)
            choice = self.submit(review_action='keep:КИС 5!J4')
            self.assertEqual(choice.context['applicable_count'], 1)
            self.assertContains(choice, 'Исключено при сверке')
            undo = self.submit(review_action='undo:КИС 5!J4')
            self.assertContains(undo, 'Кто занимает койку 01А-1?', count=1)
            self.assertContains(undo, 'Выбрать жильца', count=2)
            self.assertIsNone(undo.context.get('receipt'))
            self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)
            choice = self.submit(review_action='keep:КИС 5!J4')
            result = self.submit(action='apply', receipt=choice.context['receipt'])
            self.assertEqual(result.context['application'], {'placed': 1, 'residents': 1})
        employee = EmployeeBedOccupancy.objects.get(physical_bed__stable_id='KIS5-F1-R01-A1').resident.employee
        self.assertEqual(employee, self.workers[1])

    def test_review_can_match_employee_and_complete_period_in_place(self):
        self.use_clerk_session()
        self.workers[0].personnel_number = 'QA-42'
        self.workers[0].save(update_fields=['personnel_number'])
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, None),
        ], edits={'J3': 'Опечатка В Имени', 'O3': None}).read()
        self.submit(payload)
        fields = {'personnel_number:КИС 5!J3': 'QA-42', 'starts:КИС 5!J3': self.starts.isoformat(),
                  'ends:КИС 5!J3': self.ends.isoformat()}
        rejected = self.submit(review_action='edit:КИС 5!J3', draft_revision='forged', **fields)
        self.assertContains(rejected, 'Черновик обновился', status_code=400)
        result = self.submit(review_action='edit:КИС 5!J3', **fields)
        self.assertEqual(result.context['applicable_count'], 1)
        self.assertContains(result, self.workers[0].full_name)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)

    def test_review_choice_does_not_override_live_bed_occupancy(self):
        self.use_clerk_session()
        payload = self.upload([
            (2, '02А-1', self.workers[0], False, self.ends),
            (2, '02А-1', self.workers[1], False, self.ends),
        ]).read()
        self.submit(payload)
        choice = self.submit(review_action='keep:КИС 5!J3')
        self.assertContains(choice, f'На карте: {self.resident.display_name}')
        self.assertIsNone(choice.context.get('receipt'))
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)

    def test_review_map_uses_current_draft_and_shows_live_and_excel_occupants(self):
        url = reverse('settlement_current_housing_import')
        query = {'place_map': 'КИС 5!J3'}
        self.assertEqual(self.client.get(url, query).status_code, 302)
        self.use_clerk_session()
        self.submit(self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (1, '01А-3', self.workers[0], True, self.ends),
            (1, '01А-2', self.workers[1], False, self.ends),
        ]).read())
        self.assertEqual(self.client.get(url, {**query, 'revision': 'old'}).status_code, 409)
        response = self.client.get(url, {**query, 'revision': self.draft_revision})
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        payload = response.json()
        plan = payload['plan']
        self.assertIn('class="settlement-floor-plan"', plan)
        self.assertIn('class="settlement-corridor"', plan)
        self.assertIn('data-picker-bed-id=', plan)
        self.assertIn(self.workers[4].full_name, plan)
        self.assertNotIn('data-bed-drag-handle', plan)
        self.assertEqual(payload['resident'], self.workers[0].full_name)
        beds = {(room['number'], bed['label']): bed for room in payload['rooms'] for bed in room['beds']}
        self.assertEqual(beds[1, 'А-1']['status'], 'free')
        self.assertEqual(beds[1, 'А-2']['status'], 'draft')
        self.assertEqual(beds[1, 'А-2']['draft'], [self.workers[1].full_name])
        self.assertEqual(beds[1, 'А-3']['status'], 'free')
        self.assertEqual(beds[2, 'А-1']['status'], 'occupied')
        self.assertEqual(beds[2, 'А-1']['residents'][0]['name'], self.workers[4].full_name)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)

    def test_manual_bed_resolves_interwatch_and_is_used_when_applying_saved_draft(self):
        self.use_clerk_session()
        person = self.workers[0]
        destination = PhysicalBed.objects.get(stable_id='KIS5-F1-R03-A2')
        payload = self.upload([
            (1, '01Б-1', person, False, self.ends),
            (1, '01Б-2', person, True, self.ends),
        ]).read()
        start, _ = production_day_bounds(self.as_of)
        with patch('django.utils.timezone.now', return_value=start + timedelta(hours=4)):
            initial = self.submit(payload)
            self.assertContains(initial, 'Выбрать другое место')
            self.assertContains(initial, 'сейчас живёт здесь или на межвахте?')
            corrected = self.submit(review_action='move:КИС 5!J3', review_bed_id=destination.pk)
            self.assertEqual(corrected.context['applicable_count'], 1)
            self.assertContains(corrected, 'КИС-5 · 1 этаж · комната 3 · койка А-2')
            self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)
            reopened = self.client.get(reverse('settlement_current_housing_import'))
            self.assertContains(reopened, 'Выбрано вручную')
            self.assertContains(reopened, 'КИС-5 · 1 этаж · комната 3 · койка А-2')
            applied = self.submit(action='apply', receipt=reopened.context['receipt'])
            self.assertEqual(applied.context['application'], {'placed': 1, 'residents': 1})
        occupancy = EmployeeBedOccupancy.objects.get(resident__employee=person)
        self.assertEqual(occupancy.physical_bed, destination)

    def test_manual_bed_replaces_same_person_alternative_and_keeps_other_person(self):
        self.use_clerk_session()
        self.submit(self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (1, '01А-1', self.workers[1], False, self.ends),
            (3, '03А-1', self.workers[0], False, self.ends),
        ]).read())
        destination = PhysicalBed.objects.get(stable_id='KIS5-F1-R01-A2')
        choice = self.submit(review_action='move:КИС 5!J3', review_bed_id=destination.pk)
        self.assertEqual(choice.context['applicable_count'], 2)
        groups = {group['code']: group['rows'] for group in choice.context['report_groups']}
        self.assertEqual([row.source for row in groups['omitted']], ['КИС 5!J5'])
        self.assertCountEqual(
            [(row.source, row.physical_bed_id) for row in groups['needs_resident']],
            [('КИС 5!J3', destination.pk), ('КИС 5!J4', PhysicalBed.objects.get(stable_id='KIS5-F1-R01-A1').pk)],
        )
        undo = self.submit(review_action='undo:КИС 5!J3')
        self.assertEqual([case['kind'] for case in undo.context['review_cases']], ['linked'])

    def test_manual_bed_rejects_map_and_workbook_conflicts_without_saving(self):
        self.use_clerk_session()
        self.submit(self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (1, '01А-1', self.workers[1], False, self.ends),
            (1, '01А-2', self.workers[2], False, self.ends),
        ]).read())
        original_revision = self.draft_revision
        for stable_id, issue in (
            ('KIS5-F1-R02-A1', 'Койка занята другим жильцом'),
            ('KIS5-F1-R01-A2', 'На эту койку есть несколько записей'),
        ):
            with self.subTest(stable_id=stable_id):
                bed = PhysicalBed.objects.get(stable_id=stable_id)
                result = self.submit(review_action='move:КИС 5!J3', review_bed_id=bed.pk)
                self.assertContains(result, issue)
                self.assertEqual(result.context['draft'].review, {})
                self.assertEqual(self.draft_revision, original_revision)
        missing = self.submit(review_action='move:КИС 5!J3', review_bed_id=2**62)
        self.assertContains(missing, 'Такой койки нет в жилфонде.')
        self.assertEqual(missing.context['draft'].review, {})
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)

    def test_same_person_options_share_one_question_and_show_both_rooms(self):
        self.use_clerk_session()
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (3, '03А-1', self.workers[0], False, self.ends),
        ]).read()
        response = self.submit(payload)
        self.assertEqual(len(response.context['review_cases']), 1)
        self.assertContains(response, f'Где живёт {self.workers[0].full_name}?', count=1)
        self.assertContains(response, 'Выбрать место 01А-1')
        self.assertContains(response, 'Выбрать место 03А-1')

    def test_apply_rechecks_holds_and_leaves_unconfirmed_rows_alone(self):
        self.use_clerk_session()
        self.starts = date(2026, 11, 22)
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (2, '02А-1', self.workers[1], False, self.ends),
            (3, '03А-2', self.workers[5], False, self.ends),
        ]).read()
        start, end = production_day_bounds(self.as_of)
        with patch('django.utils.timezone.now', return_value=start + timedelta(hours=4)):
            preview = self.submit(payload)
            self.assertEqual(preview.context['applicable_count'], 2)
            SettlementBedPreparation.objects.create(
                resident=self.resident, physical_bed=PhysicalBed.objects.get(stable_id='KIS5-F1-R01-A1'),
                arrival_date=self.as_of, hold_until=end, created_by_access=self.access,
            )
            occupancy = EmployeeBedOccupancy.objects.get(resident=self.resident)
            occupancy.terminated_at = start - timedelta(days=1, hours=1)
            occupancy.save(update_fields=['terminated_at'])
            applied = self.submit(payload, action='apply', receipt=preview.context['receipt'])
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.context['application'], {'placed': 1, 'residents': 0})
        self.assertContains(applied, 'Койка подготовлена и удерживается до приезда жильца.')
        self.assertContains(applied, 'Строка изменилась после предпросмотра.')
        self.assertFalse(SettlementResident.objects.filter(employee=self.workers[0]).exists())

    def test_apply_requires_original_preview_and_rolls_back_rejected_row(self):
        self.use_clerk_session()
        payload = self.upload([(1, '01А-1', self.workers[0], False, self.ends)]).read()
        before = self.submit(payload, action='apply', receipt='invalid')
        self.assertContains(before, 'Сначала проверьте Excel', status_code=400)
        receipt = self.submit(payload).context['receipt']
        different = self.upload([(1, '01А-2', self.workers[0], False, self.ends)]).read()
        self.assertContains(self.submit(different, action='apply', receipt=receipt), 'Сначала проверьте Excel', status_code=400)
        start, _ = production_day_bounds(self.as_of)
        with (
            patch('django.utils.timezone.now', return_value=start + timedelta(hours=4)),
            patch('settlement.current_housing_import._validate_room_sex',
                  side_effect=ValidationError('Сверьте назначение комнаты.')),
        ):
            response = self.submit(payload, action='apply', receipt=receipt)
        self.assertContains(response, 'Сверьте назначение комнаты.')
        self.assertEqual(response.context['application'], {'placed': 0, 'residents': 0})
        self.assertFalse(SettlementResident.objects.filter(employee=self.workers[0]).exists())
        self.assertEqual(SettlementPlacementEvent.objects.count(), 0)

    def test_saved_file_and_choices_reopen_after_login_and_stale_tabs_cannot_edit(self):
        self.use_clerk_session()
        url = reverse('settlement_current_housing_import')
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (3, '03А-1', self.workers[0], False, self.ends),
        ]).read()
        original = self.submit(payload, filename='Общежитие.xlsx').context['draft']
        choice = self.submit(review_action='keep:КИС 5!J4')
        stale = self.submit(review_action='keep:КИС 5!J3', draft_revision=original.revision)
        self.assertContains(stale, 'Черновик обновился', status_code=400)
        self.client = Client()
        self.use_clerk_session()
        reopened = self.client.get(url)
        self.assertContains(reopened, 'Общежитие.xlsx')
        self.assertContains(reopened, 'Сохранённый Excel')
        self.assertEqual(reopened.context['draft'].revision, choice.context['draft'].revision)
        self.assertEqual(reopened.context['draft'].review, {'КИС 5!J4': {'peers': ['КИС 5!J3']}})
        self.assertEqual(reopened.context['applicable_count'], 1)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)

    def test_replacing_draft_requires_valid_file_and_owner_revision(self):
        self.use_clerk_session()
        url = reverse('settlement_current_housing_import')
        original = self.upload([(1, '01А-1', self.workers[0], False, self.ends)]).read()
        saved = self.submit(original, filename='Сохранённый.xlsx').context['draft']
        invalid = self.submit(b'not an xlsx', filename='Неверный.xlsx')
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.context['draft'].revision, saved.revision)

        other = EmployeeAccess.objects.create(
            employee=self.workers[4], role=self.access.role, status=EmployeeAccess.Status.ACTIVATED,
            access_code='510011',
        )
        other_client = Client()
        session = other_client.session
        session['employee_access_id'] = other.pk
        session.save()
        self.assertNotContains(other_client.get(url), 'Сохранённый.xlsx')
        response = other_client.post(url, {'as_of': self.as_of.isoformat(), 'draft_revision': saved.revision})
        self.assertContains(response, 'Выберите Excel', status_code=400)

        replacement = self.upload([(3, '03А-1', self.workers[2], False, self.ends)]).read()
        replaced = self.submit(replacement, filename='Другой.xlsx')
        self.assertEqual(replaced.context['draft'].filename, 'Другой.xlsx')
        self.assertNotEqual(replaced.context['draft'].revision, saved.revision)
        self.assertEqual(replaced.context['draft'].review, {})

    def test_first_saved_draft_keeps_choices_from_open_preview(self):
        self.use_clerk_session()
        payload = self.upload([
            (1, '01А-1', self.workers[0], False, self.ends),
            (3, '03А-1', self.workers[0], False, self.ends),
        ]).read()
        filename = 'Расселение.xlsx'
        review = {'КИС 5!J4': {'peers': ['КИС 5!J3']}}
        scope = {
            'access': self.access.pk, 'session': sha256(self.client.session.session_key.encode()).hexdigest(),
            'workbook': sha256(payload).hexdigest(), 'filename': filename, 'date': self.as_of.isoformat(),
        }
        state = signing.dumps({'scope': scope, 'review': review}, salt='settlement.current_housing.review')
        saved = self.submit(payload, filename=filename, review_state=state)
        self.assertEqual(saved.context['draft'].review, review)
        self.assertEqual(saved.context['applicable_count'], 1)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)
