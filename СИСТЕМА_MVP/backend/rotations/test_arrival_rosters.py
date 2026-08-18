import hashlib
import io
import json
import tempfile
import zipfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from settlement.models import EmployeeBedOccupancy, SettlementCohort, SettlementResident
from shifts.models import WatchPeriod
from users.models import Employee, EmployeeAccess, Role

from .arrival_roster_parser import UnsafeArrivalWorkbook, parse_arrival_workbook
from .arrival_rosters import upload_arrival_roster
from .models import (
    ArrivalRosterEvent,
    ArrivalRosterIssue,
    ArrivalRosterMatch,
    ArrivalRosterMatchCandidate,
    ArrivalRosterNormalizedRow,
    ArrivalRosterParserProfile,
    ArrivalRosterSourceFile,
    ArrivalRosterSourceRow,
    ArrivalRosterVersion,
)


def _workbook_bytes(*, full_name='Иванов Иван Иванович', position='Водитель',
                    phone='+7 999 123-45-67', arrival_value='14.08.2026',
                    primary_name=None, formula_name=False, omit_sheet='',
                    shift_hint=1):
    workbook = Workbook()
    workbook.remove(workbook.active)
    sheets = {
        'билеты': ['№', 'ФИО', 'Должность', 'Смена', 'Дата прилёта в Хабаровск', 'Маршрут', 'Телефон'],
        'трансфер,сам': ['№', 'ФИО сотрудника', 'Должность', 'сам/трансфер', 'Смена', 'Комментарий'],
        'продление 14.04.26-14.05.26': ['№', 'ФИО', 'Должность', 'Согласование', 'Решение', 'Дата окончания'],
        'Список сотрудников': ['№', 'ФИО', 'Должность', 'Комментарий'],
        'Числ': ['Сверка', 'Значение', '', '', '', '', '', '', '', ''],
    }
    for sheet_name, headers in sheets.items():
        if sheet_name == omit_sheet:
            continue
        sheet = workbook.create_sheet(sheet_name)
        sheet.append(headers)

    tickets = workbook['билеты']
    tickets.append([1, full_name, position, shift_hint, arrival_value, 'Хабаровск', phone])
    primary = workbook['Список сотрудников']
    primary.append([1, primary_name or full_name, position, ''])
    if formula_name:
        primary['B2'] = '=CONCAT("Иванов"," Иван")'
    if 'Числ' in workbook.sheetnames:
        workbook['Числ'].append(['Всего', '=COUNTA(\'Список сотрудников\'!B:B)-1'])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _with_zip_entry(payload, name, content):
    source = zipfile.ZipFile(io.BytesIO(payload))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr(name, content)
    return output.getvalue()


def _mark_first_zip_entry_encrypted(payload):
    data = bytearray(payload)
    local = data.find(b'PK\x03\x04')
    central = data.find(b'PK\x01\x02')
    if local < 0 or central < 0:
        raise AssertionError('Не найдены служебные заголовки ZIP.')
    local_flags = int.from_bytes(data[local + 6:local + 8], 'little') | 1
    central_flags = int.from_bytes(data[central + 8:central + 10], 'little') | 1
    data[local + 6:local + 8] = local_flags.to_bytes(2, 'little')
    data[central + 8:central + 10] = central_flags.to_bytes(2, 'little')
    return bytes(data)


def _with_content_outside_profile(payload):
    workbook = load_workbook(io.BytesIO(payload))
    workbook['Список сотрудников']['B250'] = 'Скрытая строка'
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _with_cell_value(payload, sheet_name, coordinate, value):
    workbook = load_workbook(io.BytesIO(payload))
    workbook[sheet_name][coordinate] = value
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _source_row_sha256(raw_values, raw_styles):
    payload = json.dumps(
        {'values': raw_values, 'styles': raw_styles},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


class ArrivalRosterT11Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.timekeeper_role = Role.objects.get(code='timekeeper')
        cls.other_role = Role.objects.create(
            code='driver-test-t11', name='Водитель тест', is_active=True,
        )
        cls.actor = Employee.objects.create(
            full_name='Табельщик Тестовый',
            phone='+79990000001',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.access = EmployeeAccess.objects.create(
            employee=cls.actor,
            role=cls.timekeeper_role,
            access_code='710001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.other_access = EmployeeAccess.objects.create(
            employee=cls.actor,
            role=cls.other_role,
            access_code='710002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.period = WatchPeriod.objects.create(
            name='Заезд 14.08.2026',
            starts_on=date(2026, 8, 14),
            ends_on=date(2026, 9, 13),
            is_active=True,
        )
        cls.employee = Employee.objects.create(
            full_name='Иванов Иван Иванович',
            position='Водитель',
            phone='+7 999 123-45-67',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.resident = SettlementResident(
            employee=cls.employee,
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            status=SettlementResident.Status.ACTIVE,
        )
        cls.resident.save()

    def setUp(self):
        self.private_directory = tempfile.TemporaryDirectory(prefix='arrival-roster-test-')
        self.settings_override = override_settings(
            ROTATIONS_PRIVATE_MEDIA_ROOT=Path(self.private_directory.name),
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.private_directory.cleanup)

    def _upload(self, payload=None, *, access=None, name='реестр.xlsx'):
        payload = payload if payload is not None else _workbook_bytes()
        uploaded = SimpleUploadedFile(
            name,
            payload,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        return upload_arrival_roster(
            uploaded_file=uploaded,
            watch_period_id=self.period.pk,
            actor_access_id=(access or self.access).pk,
        )

    def _login(self, client, access=None):
        session = client.session
        session['employee_access_id'] = (access or self.access).pk
        session.save()

    def _source_file_kwargs(self, marker):
        payload = f'публичная подмена {marker}'.encode('utf-8')
        return {
            'sha256': hashlib.sha256(payload).hexdigest(),
            'original_name': f'подмена-{marker}.xlsx',
            'byte_size': len(payload),
            'content_type': 'application/octet-stream',
            'file': ContentFile(payload, name=f'подмена-{marker}.xlsx'),
            'uploaded_by_access': self.access,
        }

    def _source_row(self, version, row_number):
        raw_values = [{'coordinate': f'A{row_number}', 'value': 'подмена'}]
        raw_styles = [{'coordinate': f'A{row_number}', 'style_id': 0}]
        return ArrivalRosterSourceRow(
            version=version,
            sheet_name='Список сотрудников',
            row_number=row_number,
            row_kind=ArrivalRosterSourceRow.RowKind.PERSON,
            raw_values=raw_values,
            raw_styles=raw_styles,
            row_sha256=_source_row_sha256(raw_values, raw_styles),
        )

    def _assert_public_write_forbidden(self, operation):
        with self.assertRaises(ValidationError) as caught:
            operation()
        self.assertEqual(
            caught.exception.code,
            'rotations.arrival_roster.public_write_forbidden',
        )

    def test_upload_saves_private_immutable_version_and_exact_match(self):
        before = {
            'employees': Employee.objects.count(),
            'accesses': EmployeeAccess.objects.count(),
            'residents': SettlementResident.objects.count(),
            'cohorts': SettlementCohort.objects.count(),
            'occupancies': EmployeeBedOccupancy.objects.count(),
        }

        version, created = self._upload()

        self.assertTrue(created)
        self.assertEqual(version.status, ArrivalRosterVersion.Status.REVIEW_REQUIRED)
        self.assertEqual(version.watch_period, self.period)
        self.assertEqual(version.uploaded_by_access, self.access)
        self.assertRegex(version.source_file.sha256, r'^[0-9a-f]{64}$')
        self.assertTrue(version.snapshot_sha256)
        self.assertTrue(version.source_file.file.storage.exists(version.source_file.file.name))
        with self.assertRaises(ValueError):
            version.source_file.file.storage.url(version.source_file.file.name)
        match = ArrivalRosterMatch.objects.get(version=version)
        self.assertEqual(match.status, ArrivalRosterMatch.Status.EXACT)
        self.assertEqual(match.matched_resident, self.resident)
        self.assertEqual(match.row_links.count(), 2)
        self.assertEqual(
            before,
            {
                'employees': Employee.objects.count(),
                'accesses': EmployeeAccess.objects.count(),
                'residents': SettlementResident.objects.count(),
                'cohorts': SettlementCohort.objects.count(),
                'occupancies': EmployeeBedOccupancy.objects.count(),
            },
        )
        self.assertEqual(
            set(version.events.values_list('action', flat=True)),
            {ArrivalRosterEvent.Action.UPLOADED, ArrivalRosterEvent.Action.PARSED},
        )
        self.assertTrue(version.issues.filter(code='summary_requires_review').exists())

    def test_same_file_and_period_is_idempotent(self):
        payload = _workbook_bytes()
        first, first_created = self._upload(payload)
        second, second_created = self._upload(payload)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ArrivalRosterVersion.objects.count(), 1)
        self.assertEqual(ArrivalRosterSourceFile.objects.count(), 1)
        self.assertTrue(
            ArrivalRosterEvent.objects.filter(
                version=first, action=ArrivalRosterEvent.Action.REUSED,
            ).exists()
        )

    def test_formula_in_content_is_blocking_but_formula_in_number_is_allowed(self):
        payload = _workbook_bytes(formula_name=True)
        version, _created = self._upload(payload)
        self.assertTrue(
            version.issues.filter(code='formula_in_content', severity='error').exists()
        )
        formula_columns = [
            issue.details['columns']
            for issue in version.issues.filter(code='formula_in_content')
        ]
        self.assertNotIn([1], formula_columns)

    def test_excel_date_and_unambiguous_text_are_parsed(self):
        version, _created = self._upload(_workbook_bytes(arrival_value=date(2026, 8, 14)))
        ticket = ArrivalRosterNormalizedRow.objects.get(
            source_row__version=version,
            source_row__sheet_name='билеты',
        )
        self.assertEqual(ticket.arrival_date_candidate, date(2026, 8, 14))
        self.assertEqual(ticket.date_comment, '')

    def test_ambiguous_date_is_preserved_without_guess(self):
        version, _created = self._upload(
            _workbook_bytes(arrival_value='примерно 13 или 14.08.2026'),
        )
        ticket = ArrivalRosterNormalizedRow.objects.get(
            source_row__version=version,
            source_row__sheet_name='билеты',
        )
        self.assertIsNone(ticket.arrival_date_candidate)
        self.assertEqual(ticket.raw_date, 'примерно 13 или 14.08.2026')
        self.assertTrue(version.issues.filter(code='date_requires_review').exists())

    def test_name_and_position_only_is_probable_not_exact(self):
        version, _created = self._upload(_workbook_bytes(phone=''))
        match = version.matches.filter(
            row_links__normalized_row__source_row__sheet_name='Список сотрудников',
        ).get()
        self.assertEqual(match.status, ArrivalRosterMatch.Status.PROBABLE)
        self.assertIsNone(match.matched_resident)
        self.assertEqual(match.candidates.get().resident, self.resident)

    def test_unique_phone_with_disagreeing_name_is_conflict(self):
        version, _created = self._upload(
            _workbook_bytes(full_name='Петров Пётр Петрович', primary_name='Петров Пётр Петрович'),
        )
        match = version.matches.get(status=ArrivalRosterMatch.Status.CONFLICT)
        self.assertEqual(match.status, ArrivalRosterMatch.Status.CONFLICT)
        self.assertIsNone(match.matched_resident)

    def test_missing_person_is_not_created(self):
        resident_count = SettlementResident.objects.count()
        employee_count = Employee.objects.count()
        version, _created = self._upload(
            _workbook_bytes(
                full_name='Неизвестный Человек Тестовый',
                primary_name='Неизвестный Человек Тестовый',
                phone='+79998887766',
            ),
        )
        self.assertTrue(
            version.matches.filter(status=ArrivalRosterMatch.Status.UNMATCHED).exists()
        )
        self.assertEqual(SettlementResident.objects.count(), resident_count)
        self.assertEqual(Employee.objects.count(), employee_count)

    def test_wrong_or_inactive_exact_access_has_no_fallback(self):
        with self.assertRaises(ValidationError):
            self._upload(access=self.other_access)
        self.access.status = EmployeeAccess.Status.DEACTIVATED
        self.access.save(update_fields=['status'])
        replacement = EmployeeAccess.objects.create(
            employee=self.actor,
            role=self.timekeeper_role,
            access_code='710003',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        with self.assertRaises(ValidationError):
            self._upload(access=self.access)
        self.assertTrue(replacement.is_active)
        self.assertEqual(ArrivalRosterVersion.objects.count(), 0)

    def test_unsafe_archives_are_rejected_without_partial_rows_or_files(self):
        cases = (
            _with_zip_entry(_workbook_bytes(), 'xl/vbaProject.bin', b'macro'),
            _with_zip_entry(_workbook_bytes(), 'xl/externalLinks/externalLink1.xml', b'<externalLink/>'),
            _with_zip_entry(
                _workbook_bytes(),
                '_rels/unsafe.rels',
                b"<Relationships><Relationship TargetMode='External'/></Relationships>",
            ),
            _with_zip_entry(
                _workbook_bytes(),
                'customXml/content.xml',
                b'<Override ContentType="application/vnd.ms-office.vbaProject"/>',
            ),
            _with_zip_entry(_workbook_bytes(), 'xl/media/bomb.bin', b'0' * 1024 * 1024),
            _mark_first_zip_entry_encrypted(_workbook_bytes()),
            b'EncryptedPackage is not an Open XML archive',
        )
        for payload in cases:
            with self.subTest(size=len(payload)):
                with self.assertRaises((ValidationError, UnsafeArrivalWorkbook)):
                    self._upload(payload)
                self.assertEqual(ArrivalRosterVersion.objects.count(), 0)
                self.assertEqual(ArrivalRosterSourceFile.objects.count(), 0)
                self.assertEqual(list(Path(self.private_directory.name).rglob('*')), [])

    def test_archive_entry_and_unpacked_size_limits_are_enforced(self):
        payload = _workbook_bytes()
        for setting_name in ('MAX_ARCHIVE_ENTRIES', 'MAX_UNCOMPRESSED_SIZE'):
            with self.subTest(setting_name=setting_name):
                with patch(f'rotations.arrival_roster_parser.{setting_name}', 1):
                    with self.assertRaises(UnsafeArrivalWorkbook):
                        self._upload(payload)
                self.assertEqual(ArrivalRosterVersion.objects.count(), 0)

    def test_only_xlsx_and_size_limit_are_accepted(self):
        for filename in ('реестр.xls', 'реестр.xlsm'):
            with self.subTest(filename=filename):
                with self.assertRaises(ValidationError):
                    self._upload(_workbook_bytes(), name=filename)
        oversized = SimpleUploadedFile(
            'реестр.xlsx',
            b'x' * (10 * 1024 * 1024 + 1),
            content_type='application/octet-stream',
        )
        with self.assertRaises(ValidationError):
            upload_arrival_roster(
                uploaded_file=oversized,
                watch_period_id=self.period.pk,
                actor_access_id=self.access.pk,
            )
        self.assertEqual(ArrivalRosterVersion.objects.count(), 0)

    def test_result_rows_and_final_version_are_immutable(self):
        version, _created = self._upload()
        row = version.source_rows.filter(row_kind='person').first()
        row.sheet_name = 'подмена'
        with self.assertRaises(ValidationError):
            row.save()
        with self.assertRaises(ValidationError):
            ArrivalRosterSourceRow.objects.filter(pk=row.pk).update(sheet_name='подмена')
        with self.assertRaises(ValidationError):
            ArrivalRosterSourceRow.objects.bulk_update([row], ['sheet_name'])
        with self.assertRaises(ValidationError):
            ArrivalRosterSourceRow.objects.bulk_create([])
        with self.assertRaises(ValidationError):
            row.delete()
        version.status = ArrivalRosterVersion.Status.DRAFT
        with self.assertRaises(ValidationError):
            version.save()

    def test_public_source_file_creation_paths_are_forbidden(self):
        initial_count = ArrivalRosterSourceFile.objects.count()
        operations = [
            lambda: ArrivalRosterSourceFile(**self._source_file_kwargs('save')).save(),
            lambda: ArrivalRosterSourceFile.objects.create(
                **self._source_file_kwargs('create')
            ),
            lambda: ArrivalRosterSourceFile.objects.get_or_create(
                sha256=self._source_file_kwargs('get-or-create')['sha256'],
                defaults={
                    key: value
                    for key, value in self._source_file_kwargs('get-or-create').items()
                    if key != 'sha256'
                },
            ),
            lambda: ArrivalRosterSourceFile.objects.update_or_create(
                sha256=self._source_file_kwargs('update-or-create')['sha256'],
                defaults={
                    key: value
                    for key, value in self._source_file_kwargs('update-or-create').items()
                    if key != 'sha256'
                },
            ),
            lambda: ArrivalRosterSourceFile.objects.bulk_create([
                ArrivalRosterSourceFile(**self._source_file_kwargs('bulk-create')),
            ]),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                self._assert_public_write_forbidden(operation)
                self.assertEqual(ArrivalRosterSourceFile.objects.count(), initial_count)
        self.assertEqual(
            [path for path in Path(self.private_directory.name).rglob('*') if path.is_file()],
            [],
        )

    def test_public_source_row_creation_paths_are_forbidden(self):
        version, _created = self._upload()
        initial_count = ArrivalRosterSourceRow.objects.count()
        operations = [
            lambda: self._source_row(version, 901).save(),
            lambda: ArrivalRosterSourceRow.objects.create(
                **{
                    field: value
                    for field, value in self._source_row(version, 902).__dict__.items()
                    if field not in {'_state', 'id'}
                }
            ),
            lambda: ArrivalRosterSourceRow.objects.get_or_create(
                version=version,
                sheet_name='Список сотрудников',
                row_number=903,
                defaults={
                    'row_kind': ArrivalRosterSourceRow.RowKind.PERSON,
                    'raw_values': self._source_row(version, 903).raw_values,
                    'raw_styles': self._source_row(version, 903).raw_styles,
                    'row_sha256': self._source_row(version, 903).row_sha256,
                },
            ),
            lambda: ArrivalRosterSourceRow.objects.update_or_create(
                version=version,
                sheet_name='Список сотрудников',
                row_number=904,
                defaults={
                    'row_kind': ArrivalRosterSourceRow.RowKind.PERSON,
                    'raw_values': self._source_row(version, 904).raw_values,
                    'raw_styles': self._source_row(version, 904).raw_styles,
                    'row_sha256': self._source_row(version, 904).row_sha256,
                },
            ),
            lambda: ArrivalRosterSourceRow.objects.bulk_create([
                self._source_row(version, 905),
            ]),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                self._assert_public_write_forbidden(operation)
                self.assertEqual(ArrivalRosterSourceRow.objects.count(), initial_count)

    def test_trusted_service_recomputes_file_and_row_hashes(self):
        payload = _workbook_bytes()
        version, _created = self._upload(payload)
        self.assertEqual(version.source_file.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(version.source_file.byte_size, len(payload))
        for source_row in version.source_rows.all():
            self.assertEqual(
                source_row.row_sha256,
                _source_row_sha256(source_row.raw_values, source_row.raw_styles),
            )

    def test_tampered_prepared_file_hash_rolls_back_everything(self):
        payload = _workbook_bytes()
        prepared = {
            'payload': payload,
            'sha256': '0' * 64,
            'byte_size': len(payload),
            'original_name': 'реестр.xlsx',
            'content_type': 'application/octet-stream',
        }
        with patch('rotations.arrival_rosters._read_uploaded_xlsx', return_value=prepared):
            with self.assertRaises(ValidationError) as caught:
                self._upload(payload)
        self.assertEqual(
            caught.exception.code,
            'rotations.arrival_roster.source_file_integrity_error',
        )
        self.assertEqual(ArrivalRosterSourceFile.objects.count(), 0)
        self.assertEqual(ArrivalRosterVersion.objects.count(), 0)
        self.assertEqual(ArrivalRosterSourceRow.objects.count(), 0)
        self.assertEqual(
            [path for path in Path(self.private_directory.name).rglob('*') if path.is_file()],
            [],
        )

    def test_one_tampered_row_hash_rolls_back_file_and_all_rows(self):
        payload = _workbook_bytes()
        parsed = parse_arrival_workbook(payload)
        tampered_rows = list(parsed.source_rows)
        tampered_rows[-1] = replace(tampered_rows[-1], row_sha256='0' * 64)
        tampered = replace(parsed, source_rows=tampered_rows)
        with patch('rotations.arrival_rosters.parse_arrival_workbook', return_value=tampered):
            with self.assertRaises(ValidationError) as caught:
                self._upload(payload)
        self.assertEqual(
            caught.exception.code,
            'rotations.arrival_roster.source_row_integrity_error',
        )
        self.assertEqual(ArrivalRosterSourceFile.objects.count(), 0)
        self.assertEqual(ArrivalRosterVersion.objects.count(), 0)
        self.assertEqual(ArrivalRosterSourceRow.objects.count(), 0)
        self.assertEqual(
            [path for path in Path(self.private_directory.name).rglob('*') if path.is_file()],
            [],
        )

    def test_failure_after_private_write_removes_file_and_database_rows(self):
        with patch(
            'rotations.arrival_rosters._persist_parse_result',
            side_effect=RuntimeError('synthetic persistence failure'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'synthetic persistence failure'):
                self._upload()
        self.assertEqual(ArrivalRosterSourceFile.objects.count(), 0)
        self.assertEqual(ArrivalRosterVersion.objects.count(), 0)
        self.assertEqual(
            [path for path in Path(self.private_directory.name).rglob('*') if path.is_file()],
            [],
        )

    def test_shift_values_remain_hints_and_unknown_value_requires_review(self):
        version, _created = self._upload(_workbook_bytes(shift_hint='неясно'))
        ticket = ArrivalRosterNormalizedRow.objects.get(
            source_row__version=version,
            source_row__sheet_name='билеты',
        )
        self.assertEqual(ticket.raw_shift_hint, 'неясно')
        self.assertTrue(version.issues.filter(code='unknown_shift_hint').exists())
        self.assertEqual(version.status, ArrivalRosterVersion.Status.REVIEW_REQUIRED)

    def test_content_outside_profile_range_is_not_silently_ignored(self):
        version, _created = self._upload(
            _with_content_outside_profile(_workbook_bytes()),
        )
        issue = version.issues.get(code='content_outside_profile')
        self.assertEqual(issue.severity, ArrivalRosterIssue.Severity.ERROR)
        self.assertIn('B250', issue.details['sample_coordinates'])

    def test_different_file_creates_next_version(self):
        first, _created = self._upload()
        second, _created = self._upload(_workbook_bytes(arrival_value='15.08.2026'))
        self.assertEqual(first.version_number, 1)
        self.assertEqual(second.version_number, 2)
        self.assertEqual(ArrivalRosterVersion.objects.count(), 2)

    def test_upload_endpoint_is_post_only_csrf_protected_and_hides_phone(self):
        client = Client(enforce_csrf_checks=True)
        self._login(client)
        upload_url = reverse('arrival_roster_upload')
        self.assertEqual(client.get(upload_url).status_code, 405)
        without_csrf = client.post(
            upload_url,
            {'watch_period': self.period.pk, 'workbook': SimpleUploadedFile('реестр.xlsx', _workbook_bytes())},
        )
        self.assertEqual(without_csrf.status_code, 403)
        form_response = client.get(reverse('arrival_roster_upload_form'))
        token = client.cookies['csrftoken'].value
        response = client.post(
            upload_url,
            {
                'csrfmiddlewaretoken': token,
                'watch_period': self.period.pk,
                'workbook': SimpleUploadedFile(
                    'реестр.xlsx',
                    _workbook_bytes(),
                    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                ),
                'employee_access_id': self.other_access.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        version = ArrivalRosterVersion.objects.get()
        self.assertEqual(version.uploaded_by_access, self.access)
        review = client.get(response['Location'])
        self.assertEqual(review.status_code, 200)
        self.assertNotContains(review, '+7 999 123-45-67')
        self.assertContains(review, 'Результат проверки')
        self.assertContains(form_response, 'Загрузка реестра заезда')

    def test_missing_required_sheet_becomes_review_issue(self):
        version, _created = self._upload(_workbook_bytes(omit_sheet='Числ'))
        self.assertEqual(version.status, ArrivalRosterVersion.Status.REVIEW_REQUIRED)
        issue = version.issues.get(code='missing_sheet')
        self.assertEqual(issue.severity, ArrivalRosterIssue.Severity.ERROR)
        self.assertEqual(issue.details, {})

    def test_header_issue_without_normalized_row_is_visible_on_review_screen(self):
        version, _created = self._upload(
            _with_cell_value(_workbook_bytes(), 'билеты', 'B1', 'Неизвестный столбец'),
        )
        client = Client()
        self._login(client)
        response = client.get(reverse('arrival_roster_review', args=[version.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Заголовок листа не соответствует выбранному профилю.',
        )

    def test_profile_snapshot_and_source_sha_are_reproducible(self):
        payload = _workbook_bytes()
        version, _created = self._upload(payload)
        self.assertEqual(version.source_file.sha256, hashlib.sha256(payload).hexdigest())
        profile = ArrivalRosterParserProfile.objects.get()
        self.assertRegex(profile.configuration_sha256, r'^[0-9a-f]{64}$')
        parsed = parse_arrival_workbook(payload)
        self.assertEqual(version.source_row_count, len(parsed.source_rows))
        self.assertEqual(version.normalized_row_count, len(parsed.normalized_rows))
        self.assertEqual(ArrivalRosterMatchCandidate.objects.filter(match__version=version).count(), 0)
