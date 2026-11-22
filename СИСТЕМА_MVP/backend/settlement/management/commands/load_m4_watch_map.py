"""Импорт карты мест M4 на вахту из CSV (T-036 W1.1).

Создаёт подтверждённые M4-места: якорь → закрепление за койкой → календарный
слот; при указании табельного номера — подтверждённую привязку жильца. Место с
пустым ``position_code`` становится местом общего пула вахты
(``personnel_position IS NULL``), как в решении T-036 architect-1 (R1).

Режим по умолчанию — dry-run. Запись выполняется только с ``--apply``.
Файл проверяется целиком до записи: любая ошибка отменяет импорт и выводит
все ошибки с номерами строк.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from references.models import Equipment
from users.models import Employee, PersonnelPosition

from settlement.calendar_bindings import (
    confirm_calendar_slot,
    confirm_employee_accommodation_binding,
    create_calendar_slot,
    create_employee_accommodation_binding,
)
from settlement.map_import import (
    BED_CODE_RE,
    CsvFormatError,
    CsvIssue,
    parse_iso_date,
    read_csv,
    resolve_room,
    resolve_watch_period,
    write_report,
)
from settlement.models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    AccommodationAnchorCalendarSlot,
    EmployeeAccommodationBinding,
    PhysicalBed,
    SettlementResident,
    SettlementRevision,
)
from settlement.residents import get_or_create_employee_resident


REQUIRED_COLUMNS = ('room', 'bed', 'position_code')
OPTIONAL_COLUMNS = (
    'equipment_code',
    'personnel_number',
    'anchor_type',
    'valid_from',
    'valid_to',
)

ANCHOR_TYPES = {
    'equipment': AccommodationAnchor.AnchorType.EQUIPMENT,
    'function': AccommodationAnchor.AnchorType.FUNCTION,
    'group': AccommodationAnchor.AnchorType.GROUP,
    'protected': AccommodationAnchor.AnchorType.PROTECTED,
    'service': AccommodationAnchor.AnchorType.SERVICE,
    'reserve': AccommodationAnchor.AnchorType.RESERVE,
}

SPECIAL_ANCHOR_TYPES = frozenset({
    AccommodationAnchor.AnchorType.PROTECTED,
    AccommodationAnchor.AnchorType.SERVICE,
    AccommodationAnchor.AnchorType.RESERVE,
})


@dataclass(frozen=True, slots=True)
class _MapRow:
    line: int
    room: object
    bed: PhysicalBed
    position: PersonnelPosition | None
    equipment: Equipment | None
    employee: Employee | None
    valid_from: date
    valid_to: date
    anchor_code: str
    anchor_type: str


def _anchor_code(bed: PhysicalBed) -> str:
    code = f'M4-{bed.stable_id}'
    if len(code) <= 64:
        return code
    digest = hashlib.sha1(bed.stable_id.encode('utf-8')).hexdigest()[:40]
    return f'M4-{digest}'


def _resolve_bed(room, value: str) -> PhysicalBed:
    raw = (value or '').strip()
    if not raw:
        raise ValidationError('Не указана койка.')
    bed = PhysicalBed.objects.filter(room=room, stable_id=raw).first()
    if bed is not None:
        return bed
    global_bed = PhysicalBed.objects.filter(stable_id=raw).first()
    if global_bed is not None:
        raise ValidationError(
            f'Койка «{raw}» принадлежит другой комнате '
            f'(KIS{global_bed.room.dormitory.number}-F{global_bed.room.floor}-'
            f'R{global_bed.room.number:02d}).',
        )
    match = BED_CODE_RE.fullmatch(raw.upper())
    if match:
        beds = list(
            PhysicalBed.objects.filter(
                room=room,
                block=match.group('block'),
                position=int(match.group('position')),
            ),
        )
    elif raw.isdigit():
        beds = list(
            PhysicalBed.objects.filter(room=room, position=int(raw)).order_by('block'),
        )
        if len(beds) > 1:
            raise ValidationError(
                f'Койка «{raw}» неоднозначна в комнате: укажите блок (A1/B1/ITR1).',
            )
    else:
        beds = []
    if not beds:
        raise ValidationError(f'Койка «{raw}» не найдена в комнате.')
    if len(beds) > 1:
        raise ValidationError(f'Койка «{raw}» неоднозначна.')
    return beds[0]


def _resolve_employee(value: str) -> Employee:
    raw = (value or '').strip()
    if not raw:
        raise ValidationError('Пустой табельный номер.')
    employees = list(
        Employee.objects.filter(personnel_number=raw).order_by('pk')[:2],
    )
    if not employees:
        raise ValidationError(f'Сотрудник с табельным номером «{raw}» не найден.')
    if len(employees) > 1:
        raise ValidationError(
            f'Табельный номер «{raw}» неоднозначен: найдено несколько сотрудников.',
        )
    employee = employees[0]
    if not employee.is_active or employee.status != Employee.Status.ACTIVE:
        raise ValidationError(f'Сотрудник «{raw}» неактивен.')
    return employee


def _resolve_revision(value: str | None) -> SettlementRevision:
    queryset = SettlementRevision.objects.filter(
        status=SettlementRevision.Status.CONFIRMED,
    )
    if value:
        raw = value.strip()
        revisions = list(queryset.filter(pk=int(raw))) if raw.isdigit() else list(
            queryset.filter(code=raw),
        )
        if not revisions:
            raise ValidationError(
                f'Подтверждённая ревизия «{raw}» не найдена.',
            )
        if len(revisions) > 1:
            raise ValidationError(f'Ревизия «{raw}» неоднозначна.')
        return revisions[0]
    revisions = list(queryset.order_by('-effective_at', '-pk')[:2])
    if not revisions:
        raise ValidationError(
            'Нет ни одной подтверждённой ревизии M4; создайте её до импорта.',
        )
    return revisions[0]


APPROVER_ROLE_CODES = ('settlement_clerk', 'admin')


def _approver_employees() -> dict[int, Employee]:
    from users.models import EmployeeAccess

    accesses = (
        EmployeeAccess.objects.filter(
            role__code__in=APPROVER_ROLE_CODES,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
        )
        .select_related('employee')
        .order_by('employee_id', 'pk')
    )
    return {item.employee_id: item.employee for item in accesses}


def _resolve_approved_by(value: str | None) -> Employee:
    raw = (value or '').strip()
    approvers = _approver_employees()
    if not raw:
        if len(approvers) != 1:
            raise ValidationError(
                'Укажите --approved-by: активный делопроизводитель '
                'не найден однозначно.',
            )
        return next(iter(approvers.values()))
    if raw.isdigit():
        employee = Employee.objects.filter(pk=int(raw)).first()
    else:
        employees = list(Employee.objects.filter(personnel_number=raw).order_by('pk')[:2])
        if len(employees) > 1:
            raise ValidationError(f'--approved-by «{raw}» неоднозначен.')
        employee = employees[0] if employees else None
    if employee is None:
        raise ValidationError(f'Сотрудник --approved-by «{raw}» не найден.')
    if not employee.is_active or employee.status != Employee.Status.ACTIVE:
        raise ValidationError(f'Сотрудник --approved-by «{raw}» неактивен.')
    if employee.pk not in approvers:
        raise ValidationError(
            f'Сотрудник --approved-by «{raw}» не имеет активной роли '
            'settlement_clerk или admin и не может подтверждать карту.',
        )
    return employee


class Command(BaseCommand):
    help = (
        'Импортирует карту мест M4 на вахту из CSV.\n\n'
        'Колонки (заголовок обязателен):\n'
        '  room            — KIS5-F1-R01 или 5/1/1 (корпус/этаж/номер)\n'
        '  bed             — stable_id (KIS5-F1-R01-A1) или A1/B1/ITR1\n'
        '  position_code   — код PersonnelPosition; пусто = место общего пула\n'
        '  equipment_code  — гаражный номер техники (опционально)\n'
        '  personnel_number— табельный номер жильца (опционально, создаёт привязку)\n'
        '  anchor_type     — equipment|function|group|protected|service|reserve\n'
        '                    (опционально; по умолчанию выводится из полей)\n'
        '  valid_from      — YYYY-MM-DD (опционально, по умолчанию начало вахты)\n'
        '  valid_to        — YYYY-MM-DD (опционально, по умолчанию конец вахты)\n\n'
        'Специальные комнаты задаются ТОЛЬКО данными карты, не кодом:\n'
        '  36 — два места anchor_type=protected, на каждом свой personnel_number\n'
        '       (это держатели; без держателей protected-место уходит в ИТР-ветку);\n'
        '  37 — anchor_type=protected без personnel_number\n'
        '       (только ручной ИТР по подтверждению и основанию);\n'
        '  38 — anchor_type=reserve без personnel_number\n'
        '       (ВИП-резерв: только resident_type=BUSINESS_TRIP из центрального\n'
        '       офиса; EXTERNAL_OTHER, сотрудники и подрядчики не вселяются).\n'
        'Комнаты с capacity<=2 авторасселением не используются: их заселяет\n'
        'только делопроизводитель вручную по правилам SET-R097–R099.\n\n'
        'Пример:\n'
        '  room,bed,position_code,equipment_code,personnel_number,anchor_type,valid_from,valid_to\n'
        '  KIS5-F1-R01,A1,driver,,,,,\n'
        '  5/1/1,B1,,,,,,\n'
        '  5/1/2,A1,,EXC-001,,,,\n'
        '  5/1/2,A2,,,T-000123,,2026-12-01,2026-12-31\n'
        '  5/1/3,A1,,,,protected,,\n'
        '  5/1/3,A2,,,T-000123,protected,,\n'
        '  5/1/4,A1,,,,reserve,,\n\n'
        'По умолчанию выполняется dry-run; запись — только с --apply.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--watch-period',
            required=True,
            help='ID или точное название вахты (WatchPeriod).',
        )
        parser.add_argument('--csv', required=True, help='Путь к CSV-файлу карты.')
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            '--apply',
            action='store_true',
            help='Записать изменения. Без флага — только проверка (dry-run).',
        )
        mode.add_argument(
            '--dry-run',
            action='store_true',
            help='Только проверить файл (режим по умолчанию).',
        )
        parser.add_argument(
            '--revision',
            help='ID или код подтверждённой ревизии; по умолчанию последняя подтверждённая.',
        )
        parser.add_argument(
            '--approved-by',
            help=(
                'ID или табельный номер подтверждающего сотрудника; по умолчанию '
                'единственный активный settlement_clerk/admin.'
            ),
        )
        parser.add_argument(
            '--report',
            help='Путь JSON-отчёта (создаётся в любом режиме).',
        )

    def handle(self, *args, **options):
        mode = 'apply' if options['apply'] else 'dry-run'
        report = {
            'mode': mode,
            'csv': options['csv'],
            'watch_period': options['watch_period'],
            'rows_total': 0,
            'errors': [],
            'rows': [],
            'approved_by': None,
            'revision': None,
            'created': {},
            'reused': {},
            'confirmed': {},
            'created_details': {},
            'reused_details': {},
        }
        try:
            period = resolve_watch_period(options['watch_period'])
            revision = _resolve_revision(options['revision'])
            approved_by = _resolve_approved_by(options['approved_by'])
            table = read_csv(
                options['csv'],
                required=REQUIRED_COLUMNS,
                optional=OPTIONAL_COLUMNS,
            )
        except (CsvFormatError, ValidationError) as error:
            messages = (
                [str(item) for item in error.messages]
                if isinstance(error, ValidationError)
                else [str(error)]
            )
            report['errors'] = messages
            write_report(options['report'], report)
            for message in messages:
                self.stderr.write(self.style.ERROR(message))
            raise CommandError('Импорт карты не выполнен: ' + '; '.join(messages))

        report.update({
            'watch_period_id': period.pk,
            'watch_period_name': period.name,
            'watch_composition_id': period.watch_composition_id,
            'revision': {
                'id': revision.pk,
                'code': revision.code,
            },
            'revision_id': revision.pk,
            'revision_code': revision.code,
            'approved_by': {
                'id': approved_by.pk,
                'personnel_number': approved_by.personnel_number,
                'full_name': approved_by.full_name,
            },
            'approved_by_id': approved_by.pk,
            'rows_total': len(table.rows),
        })
        rows, issues = self._validate_rows(
            table=table,
            period=period,
        )
        report['rows'] = [self._row_payload(row) for row in rows]
        if issues:
            report['errors'] = [item.format() for item in issues]
            write_report(options['report'], report)
            for item in issues:
                self.stderr.write(self.style.ERROR(item.format()))
            raise CommandError(
                f'Импорт карты не выполнен: {len(issues)} ошибок(и). '
                'База данных не изменена.'
            )

        if mode == 'dry-run':
            report['planned'] = {
                'anchors': len(rows),
                'assignments': len(rows),
                'slots': len(rows),
                'bindings': sum(1 for row in rows if row.employee is not None),
            }
            write_report(options['report'], report)
            self.stdout.write(self.style.SUCCESS(
                f'Dry-run: {len(rows)} строк проверено, ошибок нет. '
                'Для записи повторите с --apply.'
            ))
            return

        try:
            with transaction.atomic():
                self._apply_rows(
                    rows=rows,
                    period=period,
                    revision=revision,
                    approved_by=approved_by,
                    report=report,
                )
        except ValidationError as error:
            messages = [str(item) for item in error.messages]
            report['errors'] = messages
            write_report(options['report'], report)
            for message in messages:
                self.stderr.write(self.style.ERROR(message))
            raise CommandError(
                'Импорт карты отменён: ' + '; '.join(messages),
            )

        write_report(options['report'], report)
        self.stdout.write(self.style.SUCCESS(
            'Карта M4 импортирована: '
            f'создано якорей {report["created"].get("anchors", 0)}, '
            f'слотов {report["created"].get("slots", 0)}, '
            f'привязок {report["created"].get("bindings", 0)}; '
            f'повторно использовано якорей {report["reused"].get("anchors", 0)}.'
        ))

    def _validate_rows(self, *, table, period):
        issues = []
        rows = []
        seen_beds: dict[int, int] = {}
        seen_resident_windows: dict[int, list[tuple[date, date, int]]] = {}
        period_start = period.starts_on
        period_end = period.ends_on

        for line, raw in table.rows:
            try:
                room = resolve_room(raw['room'])
            except ValidationError as error:
                issues.append(CsvIssue(
                    line,
                    '; '.join(str(item) for item in error.messages),
                ))
                continue
            if room.transfer_status != room.TransferStatus.TRANSFERRED:
                issues.append(CsvIssue(
                    line,
                    f'комната {raw["room"]} не передана для расселения',
                ))
                continue
            try:
                bed = _resolve_bed(room, raw['bed'])
            except ValidationError as error:
                issues.append(CsvIssue(
                    line,
                    '; '.join(str(item) for item in error.messages),
                ))
                continue
            if bed.pk in seen_beds:
                issues.append(CsvIssue(
                    line,
                    f'койка {bed.stable_id} уже указана в строке {seen_beds[bed.pk]}',
                ))
                continue
            seen_beds[bed.pk] = line

            position = None
            position_code = raw.get('position_code', '').strip()
            if position_code:
                position = PersonnelPosition.objects.filter(code=position_code).first()
                if position is None:
                    issues.append(CsvIssue(
                        line,
                        f'должность «{position_code}» не найдена',
                    ))
                    continue

            equipment = None
            equipment_code = raw.get('equipment_code', '').strip()
            if equipment_code:
                equipment = Equipment.objects.filter(garage_number=equipment_code).first()
                if equipment is None:
                    issues.append(CsvIssue(
                        line,
                        f'техника «{equipment_code}» не найдена',
                    ))
                    continue

            employee = None
            personnel_number = raw.get('personnel_number', '').strip()
            if personnel_number:
                try:
                    employee = _resolve_employee(personnel_number)
                except ValidationError as error:
                    issues.append(CsvIssue(
                        line,
                        '; '.join(str(item) for item in error.messages),
                    ))
                    continue
                if employee.watch_composition_id != period.watch_composition_id:
                    issues.append(CsvIssue(
                        line,
                        f'сотрудник «{personnel_number}» не входит в состав вахты',
                    ))
                    continue

            try:
                valid_from = (
                    parse_iso_date(raw['valid_from'], field='valid_from')
                    if raw.get('valid_from') else period_start
                )
                valid_to = (
                    parse_iso_date(raw['valid_to'], field='valid_to')
                    if raw.get('valid_to') else period_end
                )
            except ValidationError as error:
                issues.append(CsvIssue(
                    line,
                    '; '.join(str(item) for item in error.messages),
                ))
                continue
            if valid_from > valid_to:
                issues.append(CsvIssue(line, 'valid_from позже valid_to'))
                continue
            if valid_from < period_start or valid_to > period_end:
                issues.append(CsvIssue(
                    line,
                    'даты привязки должны находиться внутри периода вахты '
                    f'{period_start}..{period_end}',
                ))
                continue

            anchor_code = _anchor_code(bed)
            raw_anchor_type = (raw.get('anchor_type') or '').strip().lower()
            if raw_anchor_type:
                anchor_type = ANCHOR_TYPES.get(raw_anchor_type)
                if anchor_type is None:
                    issues.append(CsvIssue(
                        line,
                        'anchor_type должен быть одним из: '
                        + ', '.join(sorted(ANCHOR_TYPES)),
                    ))
                    continue
            else:
                anchor_type = (
                    AccommodationAnchor.AnchorType.EQUIPMENT
                    if equipment is not None
                    else AccommodationAnchor.AnchorType.FUNCTION
                    if position is not None
                    else AccommodationAnchor.AnchorType.GROUP
                )
            if anchor_type in SPECIAL_ANCHOR_TYPES:
                if position is not None or equipment is not None:
                    issues.append(CsvIssue(
                        line,
                        f'для anchor_type={raw_anchor_type} должность и техника '
                        'должны быть пустыми',
                    ))
                    continue
                if (
                    anchor_type != AccommodationAnchor.AnchorType.PROTECTED
                    and employee is not None
                ):
                    issues.append(CsvIssue(
                        line,
                        'держатель допустим только на protected-якоре',
                    ))
                    continue
            issues.extend(self._validate_existing_anchor(
                line=line,
                anchor_code=anchor_code,
                anchor_type=anchor_type,
                position=position,
                equipment=equipment,
                bed=bed,
                room=room,
                period=period,
            ))
            if employee is not None:
                issues.extend(self._validate_existing_binding(
                    line=line,
                    employee=employee,
                    period=period,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    anchor_code=anchor_code,
                ))
                windows = seen_resident_windows.setdefault(employee.pk, [])
                for other_start, other_end, other_line in windows:
                    if valid_from <= other_end and other_start <= valid_to:
                        issues.append(CsvIssue(
                            line,
                            f'у сотрудника «{personnel_number}» пересекается привязка '
                            f'со строкой {other_line}',
                        ))
                        break
                windows.append((valid_from, valid_to, line))
            rows.append(_MapRow(
                line=line,
                room=room,
                bed=bed,
                position=position,
                equipment=equipment,
                employee=employee,
                valid_from=valid_from,
                valid_to=valid_to,
                anchor_code=anchor_code,
                anchor_type=anchor_type,
            ))
        return rows, issues

    def _row_payload(self, row: _MapRow) -> dict:
        return {
            'line': row.line,
            'room': f'KIS{row.room.dormitory.number}-F{row.room.floor}-R{row.room.number:02d}',
            'bed_stable_id': row.bed.stable_id,
            'anchor_code': row.anchor_code,
            'anchor_type': row.anchor_type,
            'position_code': row.position.code if row.position else '',
            'equipment_code': row.equipment.garage_number if row.equipment else '',
            'personnel_number': (
                row.employee.personnel_number if row.employee else ''
            ),
            'valid_from': row.valid_from.isoformat(),
            'valid_to': row.valid_to.isoformat(),
        }

    def _validate_existing_anchor(
        self, *, line, anchor_code, anchor_type, position, equipment, bed, room, period,
    ):
        issues = []
        anchor = AccommodationAnchor.objects.filter(code=anchor_code).first()
        expected = (
            anchor_type,
            equipment.pk if equipment else None,
            position.pk if position else None,
            position.code if position else '',
            f'room:{room.pk}',
            bed.position,
        )
        if anchor is not None:
            actual = (
                anchor.anchor_type,
                anchor.equipment_id,
                anchor.personnel_position_id,
                anchor.function_key,
                anchor.group_key,
                anchor.ordinal,
            )
            if actual != expected or anchor.status != AccommodationAnchor.Status.ACTIVE:
                issues.append(CsvIssue(
                    line,
                    f'якорь {anchor_code} уже существует с другим составом',
                ))
        assignment = (
            AccommodationAnchorBedAssignment.objects
            .filter(
                physical_bed=bed,
                status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            )
            .select_related('anchor')
            .first()
        )
        if assignment is not None and assignment.anchor.code != anchor_code:
            issues.append(CsvIssue(
                line,
                f'койка {bed.stable_id} уже закреплена за якорем '
                f'{assignment.anchor.code}',
            ))
        if anchor is not None:
            slot = (
                AccommodationAnchorCalendarSlot.objects
                .filter(anchor=anchor, watch_period=period)
                .order_by('pk')
                .first()
            )
            if slot is not None:
                if slot.status == AccommodationAnchorCalendarSlot.Status.CLOSED:
                    issues.append(CsvIssue(
                        line,
                        f'слот якоря {anchor_code} закрыт и не может быть использован',
                    ))
                elif slot.calendar_relation_is_stale:
                    issues.append(CsvIssue(
                        line,
                        f'слот якоря {anchor_code} устарел относительно вахты',
                    ))
        return issues

    def _validate_existing_binding(
        self, *, line, employee, period, valid_from, valid_to, anchor_code,
    ):
        issues = []
        resident = SettlementResident.objects.filter(employee=employee).first()
        if resident is None:
            return issues
        if resident.status != SettlementResident.Status.ACTIVE:
            issues.append(CsvIssue(
                line,
                f'карточка жильца сотрудника «{employee.personnel_number}» неактивна',
            ))
            return issues
        existing = list(
            EmployeeAccommodationBinding.objects.filter(
                resident=resident,
                status__in=(
                    EmployeeAccommodationBinding.Status.CONFIRMED,
                    EmployeeAccommodationBinding.Status.CLOSED,
                ),
                valid_from__lte=valid_to,
                valid_to__gte=valid_from,
            )
            .select_related('anchor_calendar_slot__anchor')
            .order_by('pk'),
        )
        for binding in existing:
            same_slot = binding.anchor_calendar_slot.anchor.code == anchor_code
            if (
                same_slot
                and binding.valid_from == valid_from
                and binding.valid_to == valid_to
                and binding.status == EmployeeAccommodationBinding.Status.CONFIRMED
            ):
                continue
            issues.append(CsvIssue(
                line,
                'у сотрудника уже есть привязка на пересекающийся период',
            ))
            break
        return issues

    def _apply_rows(self, *, rows, period, revision, approved_by, report):
        created = {
            'anchors': 0,
            'assignments': 0,
            'slots': 0,
            'bindings': 0,
        }
        reused = {
            'anchors': 0,
            'assignments': 0,
            'slots': 0,
            'bindings': 0,
        }
        confirmed = {'slots': 0, 'bindings': 0}
        period_start = timezone.make_aware(
            datetime.combine(period.starts_on, time.min),
            timezone.get_current_timezone(),
        )

        created_details = {
            'anchors': [],
            'assignments': [],
            'slots': [],
            'bindings': [],
        }
        reused_details = {
            'anchors': [],
            'assignments': [],
            'slots': [],
            'bindings': [],
        }

        for row in rows:
            anchor = AccommodationAnchor.objects.filter(code=row.anchor_code).first()
            if anchor is None:
                anchor = AccommodationAnchor(
                    code=row.anchor_code,
                    display_name=f'Место {row.bed.stable_id}',
                    anchor_type=row.anchor_type,
                    equipment=row.equipment,
                    personnel_position=row.position,
                    function_key=row.position.code if row.position else '',
                    group_key=f'room:{row.room.pk}',
                    ordinal=row.bed.position,
                    status=AccommodationAnchor.Status.ACTIVE,
                    created_revision=revision,
                )
                anchor.save()
                created['anchors'] += 1
                created_details['anchors'].append({
                    'line': row.line,
                    'code': anchor.code,
                })
            else:
                reused['anchors'] += 1
                reused_details['anchors'].append({
                    'line': row.line,
                    'code': anchor.code,
                })

            assignment = (
                AccommodationAnchorBedAssignment.objects
                .filter(
                    physical_bed=row.bed,
                    status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
                )
                .first()
            )
            if assignment is None:
                assignment = AccommodationAnchorBedAssignment(
                    anchor=anchor,
                    physical_bed=row.bed,
                    valid_from=period_start,
                    status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
                    started_revision=revision,
                )
                assignment.save()
                created['assignments'] += 1
                created_details['assignments'].append({
                    'line': row.line,
                    'id': assignment.pk,
                    'bed_stable_id': row.bed.stable_id,
                })
            else:
                reused['assignments'] += 1
                reused_details['assignments'].append({
                    'line': row.line,
                    'id': assignment.pk,
                    'bed_stable_id': row.bed.stable_id,
                })

            slot = (
                AccommodationAnchorCalendarSlot.objects
                .filter(anchor=anchor, watch_period=period)
                .select_related('watch_period', 'anchor', 'source_revision')
                .first()
            )
            if slot is None:
                slot = create_calendar_slot(
                    anchor_id=anchor.pk,
                    watch_period_id=period.pk,
                    source_revision_id=revision.pk,
                )
                created['slots'] += 1
                created_details['slots'].append({
                    'line': row.line,
                    'id': slot.pk,
                    'anchor_code': anchor.code,
                })
            else:
                reused['slots'] += 1
                reused_details['slots'].append({
                    'line': row.line,
                    'id': slot.pk,
                    'anchor_code': anchor.code,
                })
            if slot.status == AccommodationAnchorCalendarSlot.Status.DRAFT:
                slot = confirm_calendar_slot(
                    slot_id=slot.pk,
                    approved_by_id=approved_by.pk,
                )
                confirmed['slots'] += 1

            if row.employee is None:
                continue
            resident, _created = get_or_create_employee_resident(
                employee_id=row.employee.pk,
            )
            binding = (
                EmployeeAccommodationBinding.objects
                .filter(
                    resident=resident,
                    anchor_calendar_slot=slot,
                    valid_from=row.valid_from,
                )
                .first()
            )
            if binding is None:
                binding = create_employee_accommodation_binding(
                    resident_id=resident.pk,
                    calendar_slot_id=slot.pk,
                    valid_from=row.valid_from,
                    valid_to=row.valid_to,
                    basis_type='m4_map_import',
                    basis_id=f'{row.anchor_code}:{row.valid_from.isoformat()}',
                    basis_snapshot={
                        'source': 'load_m4_watch_map',
                        'bed_stable_id': row.bed.stable_id,
                        'anchor_code': row.anchor_code,
                        'line': row.line,
                    },
                    source_revision_id=revision.pk,
                )
                created['bindings'] += 1
                created_details['bindings'].append({
                    'line': row.line,
                    'id': binding.pk,
                    'personnel_number': row.employee.personnel_number,
                })
            else:
                reused['bindings'] += 1
                reused_details['bindings'].append({
                    'line': row.line,
                    'id': binding.pk,
                    'personnel_number': row.employee.personnel_number,
                })
            if binding.status == EmployeeAccommodationBinding.Status.DRAFT:
                confirm_employee_accommodation_binding(
                    binding_id=binding.pk,
                    approved_by_id=approved_by.pk,
                )
                confirmed['bindings'] += 1

        report['created'] = created
        report['reused'] = reused
        report['confirmed'] = confirmed
        report['created_details'] = created_details
        report['reused_details'] = reused_details
