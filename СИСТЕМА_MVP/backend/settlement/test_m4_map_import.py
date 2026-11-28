"""Тесты инструментов T-036 W1.1: карта M4 и пол комнат.

Проверяют fail-closed импорт CSV, отсутствие записи в dry-run, идемпотентность,
место общего пула (`personnel_position IS NULL`) и загрузку пола комнат.
Рабочая БД не используется: Django test runner создаёт отдельную тестовую базу.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from references.models import Dormitory, Equipment, EquipmentType
from shifts.models import WatchPeriod
from users.models import (
    Employee,
    EmployeeAccess,
    PersonnelPosition,
    Role,
    WatchComposition,
)

from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    AccommodationAnchorCalendarSlot,
    EmployeeAccommodationBinding,
    PhysicalBed,
    PhysicalRoom,
    SettlementResident,
    SettlementRevision,
    SettlementSource,
)


class M4MapImportFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.composition = WatchComposition.objects.create(
            code='m4-import-composition',
            name='M4 import composition',
            is_active=True,
        )
        cls.other_composition = WatchComposition.objects.create(
            code='m4-import-other',
            name='M4 import other composition',
            is_active=True,
        )
        cls.period = WatchPeriod.objects.create(
            name='M4 import watch',
            watch_composition=cls.composition,
            starts_on=date(2033, 1, 1),
            ends_on=date(2033, 1, 31),
            is_active=True,
        )
        cls.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='M4 import source',
            version='1',
            file_sha256='a' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=cls.now,
            confirmed_by_label='T-036 importer',
        )
        cls.revision = SettlementRevision.objects.create(
            code='M4-IMPORT-REV-1',
            source=cls.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=cls.now,
            confirmed_at=cls.now,
            confirmed_by_label='T-036 importer',
            reason='Карта мест для теста импорта.',
        )
        cls.dormitory = Dormitory.objects.create(number='5')
        cls.room_one = cls._room(1, transferred=True)
        cls.room_two = cls._room(2, transferred=True)
        cls.room_closed = cls._room(3, transferred=False)
        cls.bed_one = cls._bed(cls.room_one, 'KIS5-F1-R01-A1', 'A', 1)
        cls.bed_two_a1 = cls._bed(cls.room_two, 'KIS5-F1-R02-A1', 'A', 1)
        cls.bed_two_a2 = cls._bed(cls.room_two, 'KIS5-F1-R02-A2', 'A', 2)
        cls.bed_closed = cls._bed(cls.room_closed, 'KIS5-F1-R03-A1', 'A', 1)
        cls.driver = PersonnelPosition.objects.create(code='driver', name='Водитель')
        cls.excavator = PersonnelPosition.objects.create(code='excavator', name='Машинист')
        cls.equipment_type = EquipmentType.objects.create(name='Импорт техники')
        cls.equipment = Equipment.objects.create(
            equipment_type=cls.equipment_type,
            garage_number='EXC-001',
            is_active=True,
        )
        cls.employee = cls._employee(
            'Импортный Сотрудник',
            personnel_number='T-000123',
            composition=cls.composition,
        )
        cls.inactive_employee = cls._employee(
            'Неактивный Сотрудник',
            personnel_number='T-INACTIVE',
            composition=cls.composition,
            active=False,
        )
        cls.foreign_employee = cls._employee(
            'Чужая Вахта',
            personnel_number='T-OTHER',
            composition=cls.other_composition,
        )
        cls.approver = cls._employee(
            'Подтверждающий',
            personnel_number='T-APPROVER',
            composition=cls.composition,
        )
        cls.driver_employee = cls._employee(
            'Водитель Без Прав',
            personnel_number='T-DRIVER',
            composition=cls.composition,
        )
        cls.clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель M4',
        )
        cls.driver_role = Role.objects.create(code='driver', name='Водитель M4')
        cls.approver_access = EmployeeAccess.objects.create(
            employee=cls.approver,
            role=cls.clerk_role,
            access_code='M4-APPROVER',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.driver_access = EmployeeAccess.objects.create(
            employee=cls.driver_employee,
            role=cls.driver_role,
            access_code='M4-DRIVER',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    @classmethod
    def _room(cls, number, *, transferred):
        return PhysicalRoom.objects.create(
            dormitory=cls.dormitory,
            floor=1,
            number=number,
            transfer_status=(
                PhysicalRoom.TransferStatus.TRANSFERRED
                if transferred else PhysicalRoom.TransferStatus.NOT_TRANSFERRED
            ),
            sex_restriction=PhysicalRoom.SexRestriction.UNKNOWN,
            capacity=2,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=number,
        )

    @classmethod
    def _bed(cls, room, stable_id, block, position):
        return PhysicalBed.objects.create(
            room=room,
            stable_id=stable_id,
            block=block,
            position=position,
        )

    @classmethod
    def _employee(cls, name, *, personnel_number, composition, active=True):
        return Employee.objects.create(
            full_name=name,
            personnel_number=personnel_number,
            status=Employee.Status.ACTIVE if active else Employee.Status.DEACTIVATED,
            is_active=active,
            watch_composition=composition,
        )

    def _write_csv(self, text: str, *, name='map.csv') -> Path:
        directory = Path(self._tempdir.name)
        path = directory / name
        path.write_text(text, encoding='utf-8')
        return path

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)

    def _map_csv(self, rows: list[str]) -> Path:
        header = (
            'room,bed,position_code,equipment_code,personnel_number,'
            'valid_from,valid_to\n'
        )
        return self._write_csv(header + '\n'.join(rows) + '\n')

    def _typed_csv(self, rows: list[str]) -> Path:
        header = (
            'room,bed,position_code,equipment_code,personnel_number,'
            'anchor_type,valid_from,valid_to\n'
        )
        return self._write_csv(header + '\n'.join(rows) + '\n')

    def _call_map(self, path, *, apply=False, report=None):
        kwargs = {
            'watch_period': str(self.period.pk),
            'csv': str(path),
            'approved_by': str(self.approver.pk),
            'stdout': StringIO(),
            'stderr': StringIO(),
        }
        if apply:
            kwargs['apply'] = True
        if report is not None:
            kwargs['report'] = str(report)
        return call_command('load_m4_watch_map', **kwargs)

    def _counts(self):
        return (
            AccommodationAnchor.objects.count(),
            AccommodationAnchorBedAssignment.objects.count(),
            AccommodationAnchorCalendarSlot.objects.count(),
            EmployeeAccommodationBinding.objects.count(),
            SettlementResident.objects.count(),
        )


class M4MapImportTests(M4MapImportFixture):
    def test_dry_run_writes_nothing(self):
        path = self._map_csv([
            'KIS5-F1-R01,A1,driver,,,',
            '5/1/2,A1,,,,',
        ])

        self._call_map(path)

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))

    def test_valid_import_creates_anchor_slot_assignment_and_binding(self):
        path = self._map_csv([
            'KIS5-F1-R01,A1,driver,,,',
            '5/1/2,A1,,,,',
            '5/1/2,A2,,EXC-001,T-000123,2033-01-05,2033-01-20',
        ])

        self._call_map(path, apply=True)

        anchors = {
            item.code: item
            for item in AccommodationAnchor.objects.select_related(
                'personnel_position', 'equipment',
            )
        }
        self.assertEqual(len(anchors), 3)
        driver_anchor = anchors['M4-KIS5-F1-R01-A1']
        self.assertEqual(driver_anchor.anchor_type, AccommodationAnchor.AnchorType.FUNCTION)
        self.assertEqual(driver_anchor.personnel_position, self.driver)
        pool_anchor = anchors['M4-KIS5-F1-R02-A1']
        self.assertIsNone(pool_anchor.personnel_position)
        self.assertIsNone(pool_anchor.equipment_id)
        self.assertEqual(pool_anchor.anchor_type, AccommodationAnchor.AnchorType.GROUP)
        equipment_anchor = anchors['M4-KIS5-F1-R02-A2']
        self.assertEqual(
            equipment_anchor.anchor_type,
            AccommodationAnchor.AnchorType.EQUIPMENT,
        )
        self.assertEqual(equipment_anchor.equipment, self.equipment)

        self.assertEqual(AccommodationAnchorBedAssignment.objects.count(), 3)
        self.assertTrue(all(
            item.status == AccommodationAnchorBedAssignment.Status.CONFIRMED
            for item in AccommodationAnchorBedAssignment.objects.all()
        ))
        self.assertEqual(AccommodationAnchorCalendarSlot.objects.count(), 3)
        self.assertTrue(all(
            item.status == AccommodationAnchorCalendarSlot.Status.CONFIRMED
            for item in AccommodationAnchorCalendarSlot.objects.all()
        ))
        binding = EmployeeAccommodationBinding.objects.select_related('resident').get()
        self.assertEqual(binding.resident.employee_id, self.employee.pk)
        self.assertEqual(binding.status, EmployeeAccommodationBinding.Status.CONFIRMED)
        self.assertEqual(binding.valid_from, date(2033, 1, 5))
        self.assertEqual(binding.valid_to, date(2033, 1, 20))

    def test_bad_rows_abort_all_and_report_every_line(self):
        path = self._map_csv([
            'KIS5-F9-R99,A1,driver,,,',      # строка 2: нет комнаты
            'KIS5-F1-R01,A9,driver,,,',      # строка 3: нет койки
            'KIS5-F1-R01,A1,unknown,,,',     # строка 4: нет должности
            'KIS5-F1-R02,A1,,,T-MISSING,',   # строка 5: нет сотрудника
            'KIS5-F1-R02,A2,,NO-EQUIPMENT,,,',  # строка 6: нет техники
        ])
        stderr = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                'load_m4_watch_map',
                watch_period=str(self.period.pk),
                csv=str(path),
                approved_by=str(self.approver.pk),
                apply=True,
                stdout=StringIO(),
                stderr=stderr,
            )

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))
        output = stderr.getvalue()
        for line in (2, 3, 4, 5, 6):
            self.assertIn(f'строка {line}:', output)

    def test_repeat_import_is_idempotent(self):
        path = self._map_csv([
            'KIS5-F1-R01,A1,driver,,,',
            '5/1/2,A2,,EXC-001,T-000123,2033-01-05,2033-01-20',
        ])

        self._call_map(path, apply=True)
        first = self._counts()
        self._call_map(path, apply=True)

        self.assertEqual(self._counts(), first)
        self.assertEqual(AccommodationAnchor.objects.count(), 2)
        self.assertEqual(EmployeeAccommodationBinding.objects.count(), 1)

    def test_bed_from_another_room_is_rejected(self):
        path = self._map_csv([
            'KIS5-F1-R01,A1,driver,,,',
            'KIS5-F1-R01,KIS5-F1-R02-A2,driver,,,',
        ])
        stderr = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                'load_m4_watch_map',
                watch_period=str(self.period.pk),
                csv=str(path),
                approved_by=str(self.approver.pk),
                apply=True,
                stdout=StringIO(),
                stderr=stderr,
            )

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))
        self.assertIn('другой комнате', stderr.getvalue())

    def test_not_transferred_room_is_rejected(self):
        path = self._map_csv(['KIS5-F1-R03,A1,driver,,,'])
        stderr = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                'load_m4_watch_map',
                watch_period=str(self.period.pk),
                csv=str(path),
                approved_by=str(self.approver.pk),
                apply=True,
                stdout=StringIO(),
                stderr=stderr,
            )

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))
        self.assertIn('не передана', stderr.getvalue())

    def test_foreign_watch_employee_and_inactive_employee_are_rejected(self):
        path = self._map_csv([
            'KIS5-F1-R01,A1,,,T-OTHER,',
            'KIS5-F1-R02,A1,,,T-INACTIVE,',
        ])
        stderr = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                'load_m4_watch_map',
                watch_period=str(self.period.pk),
                csv=str(path),
                approved_by=str(self.approver.pk),
                apply=True,
                stdout=StringIO(),
                stderr=stderr,
            )

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))
        output = stderr.getvalue()
        self.assertIn('не входит в состав вахты', output)
        self.assertIn('неактивен', output)

    def test_non_clerk_approved_by_is_rejected(self):
        path = self._map_csv(['KIS5-F1-R01,A1,driver,,,'])
        stderr = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                'load_m4_watch_map',
                watch_period=str(self.period.pk),
                csv=str(path),
                approved_by=str(self.driver_employee.pk),
                apply=True,
                stdout=StringIO(),
                stderr=stderr,
            )

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))
        self.assertIn('settlement_clerk', stderr.getvalue())

    def test_default_approver_is_reported_explicitly(self):
        path = self._map_csv(['KIS5-F1-R01,A1,driver,,,'])
        report_path = Path(self._tempdir.name) / 'default-approver.json'

        call_command(
            'load_m4_watch_map',
            watch_period=str(self.period.pk),
            csv=str(path),
            report=str(report_path),
            stdout=StringIO(),
            stderr=StringIO(),
        )

        payload = json.loads(report_path.read_text(encoding='utf-8'))
        self.assertEqual(payload['approved_by']['id'], self.approver.pk)
        self.assertEqual(payload['revision']['id'], self.revision.pk)

    def test_report_is_written_in_dry_run(self):
        path = self._map_csv(['KIS5-F1-R01,A1,driver,,,'])
        report_path = Path(self._tempdir.name) / 'report.json'

        self._call_map(path, report=report_path)

        payload = json.loads(report_path.read_text(encoding='utf-8'))
        self.assertEqual(payload['mode'], 'dry-run')
        self.assertEqual(payload['rows_total'], 1)
        self.assertEqual(payload['errors'], [])
        self.assertEqual(payload['approved_by']['id'], self.approver.pk)
        self.assertEqual(payload['revision']['code'], self.revision.code)
        self.assertEqual(payload['rows'][0]['anchor_code'], 'M4-KIS5-F1-R01-A1')

    def test_anchor_type_column_creates_special_anchors(self):
        # T-036 backend-15: protected/reserve задаются данными CSV.
        path = self._typed_csv([
            'KIS5-F1-R01,A1,,,,protected,,',
            'KIS5-F1-R02,A1,,,T-000123,protected,,',
            'KIS5-F1-R02,A2,,,,reserve,,',
        ])

        self._call_map(path, apply=True)

        types = dict(
            AccommodationAnchor.objects.values_list('code', 'anchor_type'),
        )
        self.assertEqual(
            types['M4-KIS5-F1-R01-A1'],
            AccommodationAnchor.AnchorType.PROTECTED,
        )
        self.assertEqual(
            types['M4-KIS5-F1-R02-A1'],
            AccommodationAnchor.AnchorType.PROTECTED,
        )
        self.assertEqual(
            types['M4-KIS5-F1-R02-A2'],
            AccommodationAnchor.AnchorType.RESERVE,
        )
        binding = EmployeeAccommodationBinding.objects.get()
        self.assertEqual(binding.resident.employee_id, self.employee.pk)
        self.assertEqual(
            binding.anchor_calendar_slot.anchor.anchor_type,
            AccommodationAnchor.AnchorType.PROTECTED,
        )

    def test_protected_anchor_rejects_position_or_equipment(self):
        path = self._typed_csv([
            'KIS5-F1-R01,A1,driver,,,protected,,',
        ])
        stderr = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                'load_m4_watch_map',
                watch_period=str(self.period.pk),
                csv=str(path),
                approved_by=str(self.approver.pk),
                apply=True,
                stdout=StringIO(),
                stderr=stderr,
            )

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))
        self.assertIn('должность и техника', stderr.getvalue())

    def test_help_documents_special_room_anchor_types(self):
        # T-036 backend-17: инструкция по 36/37/38 и предупреждение capacity<=2.
        from settlement.management.commands.load_m4_watch_map import Command

        text = Command.help
        for token in (
            'anchor_type=protected',
            'anchor_type=reserve',
            'personnel_number',
            'capacity<=2',
            'BUSINESS_TRIP',
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_reserve_anchor_rejects_holder(self):
        path = self._typed_csv([
            'KIS5-F1-R01,A1,,,T-000123,reserve,,',
        ])
        stderr = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                'load_m4_watch_map',
                watch_period=str(self.period.pk),
                csv=str(path),
                approved_by=str(self.approver.pk),
                apply=True,
                stdout=StringIO(),
                stderr=stderr,
            )

        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))
        self.assertIn('держатель допустим только', stderr.getvalue())
