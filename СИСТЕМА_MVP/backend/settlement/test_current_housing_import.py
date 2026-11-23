from datetime import date, timedelta
from io import BytesIO
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
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
        self.assertContains(response, 'Койка занята другим жильцом')
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

    def submit(self, payload, *, filename='housing.xlsx', **extra):
        return self.client.post(reverse('settlement_current_housing_import'), {
            'workbook': SimpleUploadedFile(filename, payload), 'as_of': self.as_of.isoformat(), **extra,
        })

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
            applied = self.submit(payload, action='apply', receipt=receipt)
            self.assertEqual(applied.status_code, 200)
            self.assertEqual(applied.context['application'], {'placed': 2, 'residents': 1})
            repeated = self.submit(payload, action='apply', receipt=receipt)
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
            self.assertContains(changed_period, 'Снова нажмите', status_code=400)
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
        self.assertContains(result, 'На эту койку есть несколько записей на выбранную дату.', count=2)

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
        self.assertContains(before, 'Снова нажмите', status_code=400)
        receipt = self.submit(payload).context['receipt']
        different = self.upload([(1, '01А-2', self.workers[0], False, self.ends)]).read()
        self.assertContains(self.submit(different, action='apply', receipt=receipt), 'Снова нажмите', status_code=400)
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
