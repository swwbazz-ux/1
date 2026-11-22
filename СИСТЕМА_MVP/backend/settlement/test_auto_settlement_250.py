"""T-035/T-036: синтетическая вахта на 250 человек — измерение авторасселения.

Файл помечен как тяжёлый и по умолчанию ПРОПУСКАЕТСЯ, чтобы не тормозить
обычный прогон `manage.py test settlement` и CI.

Включение (локально; отдельная тестовая БД создаётся test runner'ом):

    set COPPER_SETTLEMENT_250=1
    python manage.py test settlement.test_auto_settlement_250 -v 2

Необязательные настройки измерения:

    COPPER_SETTLEMENT_250_REPORT=...   JSON-отчёт на диск
    COPPER_SETTLEMENT_STEP_LOG=1       печать прогресса по шагам
    COPPER_SETTLEMENT_SCALE=0.2        уменьшенная вахта (50 из 250)
    COPPER_SETTLEMENT_BALANCED=1       должностных мест ровно по числу людей

Рабочая БД не используется: Django test runner для SQLite создаёт отдельную
(in-memory) тестовую базу. Продуктовый код не меняется.
"""

import json
import os
import time as timer
from collections import Counter, defaultdict
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from references.models import Dormitory, Equipment, EquipmentType
from shifts.models import WatchPeriod
from users.models import (
    Employee,
    EmployeeAccess,
    PersonnelPosition,
    Role,
    WatchComposition,
)

from .apply import apply_confirmed_settlement_preview
from .calendar_bindings import (
    confirm_calendar_slot,
    confirm_employee_accommodation_binding,
    create_calendar_slot,
    create_employee_accommodation_binding,
)
from .cohorts import (
    add_settlement_cohort_member,
    approve_settlement_cohort,
    create_settlement_cohort,
)
from .control import SettlementControlWriteContext, acquire_control_lease
from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementCohortMember,
    SettlementRevision,
    SettlementSource,
)
from .residents import get_or_create_employee_resident
from .resolver import resolve_settlement_cohort
from .saved_previews import (
    confirm_settlement_preview_run,
    create_settlement_preview_run,
)

RUN_SYNTHETIC_WATCH = os.getenv('COPPER_SETTLEMENT_250') == '1'
REPORT_PATH = os.getenv('COPPER_SETTLEMENT_250_REPORT', '').strip()
STEP_LOG = os.getenv('COPPER_SETTLEMENT_STEP_LOG') == '1'
# BALANCED=1 -> должностных мест ровно столько, сколько людей (проверка
# «счастливого пути»); иначе — реальный дефицит мест из POSITION_ANCHORS.
BALANCED = os.getenv('COPPER_SETTLEMENT_BALANCED') == '1'


def _scale_factor():
    raw = (os.getenv('COPPER_SETTLEMENT_SCALE') or '1').strip()
    try:
        value = float(raw)
    except ValueError:
        value = 1.0
    return value if value > 0 else 1.0


SCALE = _scale_factor()


def _s(value, minimum=1):
    """Пропорциональный размер вахты: SCALE=1 -> исходные числа 250."""
    if value <= 0:
        return 0
    if SCALE == 1:
        return value
    return max(minimum, int(round(value * SCALE)))


def _step(label, started):
    if STEP_LOG:
        print(
            f'[T-036 W0] {label}: {timer.perf_counter() - started:.1f}s',
            flush=True,
        )


MALE = Employee.Sex.MALE
FEMALE = Employee.Sex.FEMALE

# Должность -> (код, наименование, количество людей, из них мужчин).
POSITION_HEADCOUNT = [
    ('driver', 'Водитель', 70, 50),
    ('excavator', 'Машинист экскаватора', 50, 35),
    ('driller', 'Машинист буровой установки', 30, 25),
    ('electrician', 'Электромонтёр', 20, 15),
    ('locksmith', 'Слесарь', 20, 15),
    ('itr', 'ИТР', 20, 12),
    ('cook', 'Повар', 10, 3),
    ('medic', 'Медработник', 5, 1),
    ('security', 'Охранник', 15, 12),
    ('storekeeper', 'Кладовщик', 10, 5),
]

# Должность -> (якоря в мужских комнатах, якоря в женских комнатах).
POSITION_ANCHORS = {
    'driver': (25, 15),
    'excavator': (15, 15),
    'electrician': (15, 5),
    'cook': (3, 7),
    'medic': (1, 4),
}

# Постоянные привязки: берём первых людей должности (20 мужчин + 10 женщин).
BOUND_HEADCOUNT = {
    'itr': 5,
    'security': 15,
    'storekeeper': 10,
}

# Технический маршрут: первые N мужчин должности получают якорь техники.
EQUIPMENT_HEADCOUNT = {
    'excavator': 20,
}

# Люди с посторонним размещением на период -> hard_rule_conflict.
BLOCKING_OCCUPANCY_POSITION = 'driller'
BLOCKING_OCCUPANCY_COUNT = 3

# Свободные якоря без должности (места, которые resolver не может использовать).
FREE_UNASSIGNED_ANCHORS = 15

ROOM_LAYOUT = {
    'male_only': 55,
    'female_only': 40,
    'unknown': 5,
}
BEDS_PER_ROOM = 3


def _effective_positions():
    rows = []
    for key, name, count, male_count in POSITION_HEADCOUNT:
        total = _s(count)
        male = min(total, _s(male_count)) if male_count else 0
        rows.append((key, name, total, male))
    return rows


EFFECTIVE_POSITION_HEADCOUNT = _effective_positions()


def _people_split():
    return {
        key: (male, total - male)
        for key, _name, total, male in EFFECTIVE_POSITION_HEADCOUNT
    }


_PEOPLE_SPLIT = _people_split()

if BALANCED:
    EFFECTIVE_POSITION_ANCHORS = {
        key: _PEOPLE_SPLIT[key] for key in POSITION_ANCHORS
    }
else:
    EFFECTIVE_POSITION_ANCHORS = {
        key: (_s(male), _s(female))
        for key, (male, female) in POSITION_ANCHORS.items()
    }

EFFECTIVE_BOUND_HEADCOUNT = {
    key: _s(value) for key, value in BOUND_HEADCOUNT.items()
}
EFFECTIVE_EQUIPMENT_HEADCOUNT = {
    key: _s(value) for key, value in EQUIPMENT_HEADCOUNT.items()
}
EFFECTIVE_BLOCKING_COUNT = _s(BLOCKING_OCCUPANCY_COUNT)
EFFECTIVE_FREE_ANCHORS = _s(FREE_UNASSIGNED_ANCHORS)
EFFECTIVE_ROOM_LAYOUT = {
    key: _s(value) for key, value in ROOM_LAYOUT.items()
}


@skipUnless(
    RUN_SYNTHETIC_WATCH,
    'T-035: тяжёлый сценарий 250 человек; включить COPPER_SETTLEMENT_250=1',
)
class SyntheticWatch250AutoSettlementTests(TestCase):
    maxDiff = None

    # --- построение мира -------------------------------------------------

    def _aware(self, day, hour=0, minute=0):
        return timezone.make_aware(
            datetime.combine(day, dt_time(hour=hour, minute=minute)),
            timezone.get_current_timezone(),
        )

    def _build_reference_layer(self):
        self.now = timezone.now().replace(microsecond=0)
        self.period_starts = timezone.localdate(self.now) + timedelta(days=7)
        self.period_ends = self.period_starts + timedelta(days=29)
        self.arrival_at = self._aware(self.period_starts)
        self.departure_at = self._aware(self.period_ends + timedelta(days=1))

        self.composition = WatchComposition.objects.create(
            code='t035-watch-composition',
            name='T-035 синтетический нормативный состав',
        )
        self.period = WatchPeriod.objects.create(
            name='T-035 синтетическая вахта 250',
            watch_composition=self.composition,
            starts_on=self.period_starts,
            ends_on=self.period_ends,
            is_active=True,
        )
        self.actor = Employee.objects.create(
            full_name='ДЕМО T-035 Делопроизводитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=self.composition,
        )
        clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='T-035 делопроизводитель',
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.actor,
            role=clerk_role,
            access_code='T035-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='T-035 синтетическое основание',
            version='1',
            file_sha256='7' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=self.now,
            confirmed_by_label='T-035 e2e',
        )
        self.revision = SettlementRevision.objects.create(
            code='T035-REV-1',
            source=self.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=self.now,
            confirmed_at=self.now,
            confirmed_by_label='T-035 e2e',
            reason='Синтетическая вахта для измерения авторасселения.',
        )
        self.dormitory = Dormitory.objects.create(number='T035')
        self.equipment_type = EquipmentType.objects.create(name='T-035 техника')

        self.positions = {}
        self.work_roles = {}
        for key, name, _count, _male_count in EFFECTIVE_POSITION_HEADCOUNT:
            self.positions[key] = PersonnelPosition.objects.create(
                code=f't035-{key}',
                name=f'T-035 {name}',
                settlement_category=(
                    PersonnelPosition.SettlementCategory.ITR
                    if key == 'itr'
                    else PersonnelPosition.SettlementCategory.WORKER
                ),
            )
            self.work_roles[key] = Role.objects.create(
                code=f't035-work-{key}',
                name=f'T-035 работа {name}',
            )

    def _build_physical_fund(self):
        self.rooms = []
        self.beds_by_sex_class = defaultdict(list)
        number = 0
        for sex_restriction, room_count in EFFECTIVE_ROOM_LAYOUT.items():
            for _ in range(room_count):
                number += 1
                floor = 1 if number <= 50 else 2
                index_on_floor = number if floor == 1 else number - 50
                corridor_side = (
                    PhysicalRoom.CorridorSide.LEFT
                    if index_on_floor <= 25
                    else PhysicalRoom.CorridorSide.RIGHT
                )
                side_position = (
                    index_on_floor if index_on_floor <= 25
                    else index_on_floor - 25
                )
                room = PhysicalRoom.objects.create(
                    dormitory=self.dormitory,
                    floor=floor,
                    number=number,
                    transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                    # T-036 backend-14: пол комнате не присваивается; класс
                    # ниже — только раскладка тестовой фикстуры по группам.
                    capacity=BEDS_PER_ROOM,
                    corridor_side=corridor_side,
                    side_position=side_position,
                )
                self.rooms.append(room)
                sex_class = {
                    'male_only': 'male',
                    'female_only': 'female',
                    'unknown': 'unknown',
                }[sex_restriction]
                for position in range(1, BEDS_PER_ROOM + 1):
                    bed = PhysicalBed.objects.create(
                        room=room,
                        stable_id=f'T035-R{number:03d}-A{position}',
                        block=PhysicalBed.Block.A,
                        position=position,
                    )
                    self.beds_by_sex_class[sex_class].append(bed)

        # Три двухместные ИТР-комнаты вне пула авто (SET-R097). Койки не
        # попадают в beds_by_sex_class, поэтому авторасселение их не видит.
        self.two_bed_rooms = []
        for index in range(3):
            number += 1
            room = PhysicalRoom.objects.create(
                dormitory=self.dormitory,
                floor=2,
                number=number,
                room_type=PhysicalRoom.RoomType.ITR,
                transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                capacity=2,
                corridor_side=PhysicalRoom.CorridorSide.RIGHT,
                side_position=40 + index,
            )
            self.rooms.append(room)
            for position in (1, 2):
                PhysicalBed.objects.create(
                    room=room,
                    stable_id=f'T035-ITR-R{number:03d}-I{position}',
                    block=PhysicalBed.Block.ITR,
                    position=position,
                )
            self.two_bed_rooms.append(room)

    def _take_bed(self, sex_class):
        return self.beds_by_sex_class[sex_class].pop(0)

    def _anchor(self, *, seq, anchor_type, sex_class, position=None, equipment=None):
        bed = self._take_bed(sex_class)
        anchor = AccommodationAnchor.objects.create(
            code=f'T035-A-{seq:04d}',
            display_name=f'T-035 место {seq:04d}',
            anchor_type=anchor_type,
            equipment=equipment,
            personnel_position=position,
            group_key=f't035-room-{bed.room_id}',
            ordinal=seq,
            status=AccommodationAnchor.Status.ACTIVE,
            created_revision=self.revision,
        )
        AccommodationAnchorBedAssignment.objects.create(
            anchor=anchor,
            physical_bed=bed,
            valid_from=self.arrival_at,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            started_revision=self.revision,
        )
        slot = create_calendar_slot(
            anchor_id=anchor.pk,
            watch_period_id=self.period.pk,
            source_revision_id=self.revision.pk,
        )
        slot = confirm_calendar_slot(
            slot_id=slot.pk,
            approved_by_id=self.actor.pk,
            approved_at=self.now,
        )
        self._anchor_done = getattr(self, '_anchor_done', 0) + 1
        if STEP_LOG and self._anchor_done % 25 == 0:
            print(f'[T-036 W0] якорей готово: {self._anchor_done}', flush=True)
        return anchor, bed, slot

    def _build_anchors(self):
        self.anchor_seq = 0
        self.position_anchors = defaultdict(list)
        self.binding_slots = []
        self.equipment_anchors = []
        self.free_slots = []

        def next_seq():
            self.anchor_seq += 1
            return self.anchor_seq

        for key, (male_count, female_count) in EFFECTIVE_POSITION_ANCHORS.items():
            position = self.positions[key]
            for sex_class, count in (('male', male_count), ('female', female_count)):
                for _ in range(count):
                    self.position_anchors[key].append(self._anchor(
                        seq=next_seq(),
                        anchor_type=AccommodationAnchor.AnchorType.FUNCTION,
                        sex_class=sex_class,
                        position=position,
                    ))

        bound_total = sum(EFFECTIVE_BOUND_HEADCOUNT.values())
        bound_male = sum(
            min(_PEOPLE_SPLIT[key][0], EFFECTIVE_BOUND_HEADCOUNT.get(key, 0))
            for key in EFFECTIVE_BOUND_HEADCOUNT
        )
        bound_male = min(bound_total, bound_male)
        for index in range(bound_total):
            self.binding_slots.append(self._anchor(
                seq=next_seq(),
                anchor_type=AccommodationAnchor.AnchorType.GROUP,
                sex_class='male' if index < bound_male else 'female',
            ))

        for _ in range(EFFECTIVE_FREE_ANCHORS):
            self.free_slots.append(self._anchor(
                seq=next_seq(),
                anchor_type=AccommodationAnchor.AnchorType.RESERVE,
                sex_class='unknown',
            ))

        for key, count in EFFECTIVE_EQUIPMENT_HEADCOUNT.items():
            for _ in range(count):
                equipment = Equipment.objects.create(
                    equipment_type=self.equipment_type,
                    garage_number=f'T035-EQ-{len(self.equipment_anchors) + 1:03d}',
                    is_active=True,
                )
                anchor, bed, slot = self._anchor(
                    seq=next_seq(),
                    anchor_type=AccommodationAnchor.AnchorType.EQUIPMENT,
                    sex_class='male',
                    equipment=equipment,
                )
                self.equipment_anchors.append({
                    'key': key,
                    'equipment': equipment,
                    'anchor': anchor,
                    'bed': bed,
                    'slot': slot,
                })

    def _build_people(self):
        self.employee_rows = []
        for key, name, count, male_count in EFFECTIVE_POSITION_HEADCOUNT:
            for index in range(count):
                sex = MALE if index < male_count else FEMALE
                if STEP_LOG and index == 0:
                    print(
                        f'[T-036 W0] люди {key}: {count} '
                        f'(м {male_count} / ж {count - male_count})',
                        flush=True,
                    )
                employee = Employee.objects.create(
                    full_name=f'ДЕМО T-035 {name} {index + 1:03d}',
                    sex=sex,
                    personnel_position=self.positions[key],
                    status=Employee.Status.ACTIVE,
                    is_active=True,
                    watch_composition=self.composition,
                )
                resident, _created = get_or_create_employee_resident(
                    employee_id=employee.pk,
                )
                shift_type = (
                    WorkShiftType.SHIFT_1 if (index % 5) < 3
                    else WorkShiftType.SHIFT_2
                )
                self.employee_rows.append({
                    'key': key,
                    'position_name': name,
                    'index': index,
                    'employee': employee,
                    'resident': resident,
                    'shift_type': shift_type,
                    'bound': index < EFFECTIVE_BOUND_HEADCOUNT.get(key, 0),
                    'equipment_route': (
                        sex == MALE
                        and index < EFFECTIVE_EQUIPMENT_HEADCOUNT.get(key, 0)
                    ),
                    'blocking': (
                        key == BLOCKING_OCCUPANCY_POSITION
                        and index < EFFECTIVE_BLOCKING_COUNT
                    ),
                })

    def _build_official_assignments(self):
        plans = {
            key: CrewPlan.objects.create(
                work_date=self.period_starts,
                role=self.work_roles[key],
                revision=1,
                status=CrewPlanStatus.PUBLISHED,
                version=1,
                created_by=self.actor,
                updated_by=self.actor,
                published_by=self.actor,
                published_at=self.now,
            )
            for key in self.positions
        }
        route_rows = [row for row in self.employee_rows if row['equipment_route']]
        route_by_resident = {
            row['resident'].pk: route
            for row, route in zip(route_rows, self.equipment_anchors)
        }
        pending = []
        for row in self.employee_rows:
            route = route_by_resident.get(row['resident'].pk)
            equipment = (
                route['equipment'] if route is not None
                else Equipment.objects.create(
                    equipment_type=self.equipment_type,
                    garage_number=f'T035-W-{row["resident"].pk:05d}',
                    is_active=True,
                )
            )
            crew_slot = CrewPlanSlot.objects.create(
                plan=plans[row['key']],
                equipment=equipment,
                shift_type=row['shift_type'],
                employee=row['employee'],
                baseline_employee=row['employee'],
            )
            assignment = EquipmentAssignment(
                employee=row['employee'],
                role=self.work_roles[row['key']],
                equipment=equipment,
                shift_type=row['shift_type'],
                status=AssignmentStatus.ACCEPTED,
                accepted_at=self.now,
                source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
                source_crew_plan_slot=crew_slot,
            )
            assignment.full_clean()
            pending.append(assignment)
        EquipmentAssignment._base_manager.bulk_create(pending)
        created = {
            item.employee_id: item
            for item in EquipmentAssignment._base_manager.filter(
                employee_id__in=[row['employee'].pk for row in self.employee_rows],
            )
        }
        for row in self.employee_rows:
            row['assignment'] = created[row['employee'].pk]

    def _build_bindings(self):
        bound_rows = [row for row in self.employee_rows if row['bound']]
        bound_rows.sort(key=lambda item: (item['employee'].sex != MALE, item['index']))
        available = list(self.binding_slots)
        for row in bound_rows:
            # T-036 backend-14: пол комнаты определяется жильцами, поэтому
            # слоты заранее разложены по мужской/женской группам (пул выше).
            if not available:
                raise AssertionError(
                    f'Нет подходящего места для привязки: {row["employee"].pk}',
                )
            _anchor, _bed, slot = available.pop(0)
            binding = create_employee_accommodation_binding(
                resident_id=row['resident'].pk,
                calendar_slot_id=slot.pk,
                valid_from=self.period_starts,
                valid_to=self.period_ends,
                basis_type='t035_synthetic_binding',
                basis_id=f'T035-BINDING-{row["resident"].pk}',
                basis_snapshot={
                    'resident_id': row['resident'].pk,
                    'slot_id': slot.pk,
                },
                source_revision_id=self.revision.pk,
            )
            confirm_employee_accommodation_binding(
                binding_id=binding.pk,
                approved_by_id=self.actor.pk,
                approved_at=self.now,
            )
            row['binding'] = binding

    def _build_blocking_occupancies(self):
        blocking_rows = [row for row in self.employee_rows if row['blocking']]
        spare_beds = self.beds_by_sex_class['male'][-EFFECTIVE_BLOCKING_COUNT:]
        for row, bed in zip(blocking_rows, spare_beds):
            EmployeeBedOccupancy(
                resident=row['resident'],
                physical_bed=bed,
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                source_kind=EmployeeBedOccupancy.SourceKind.MANUAL,
                watch_period=self.period,
                starts_at=self.arrival_at - timedelta(days=5),
                settled_at=self.arrival_at - timedelta(days=5),
                settled_by=self.actor,
            ).save()

    # --- прогон ----------------------------------------------------------

    def _scenario_report(self):
        total_slots = (
            sum(len(items) for items in self.position_anchors.values())
            + len(self.binding_slots)
            + len(self.free_slots)
            + len(self.equipment_anchors)
        )
        return {
            'watch_period': {
                'starts_on': self.period_starts.isoformat(),
                'ends_on': self.period_ends.isoformat(),
            },
            'people': len(self.employee_rows),
            'positions': [
                {
                    'code': key,
                    'name': name,
                    'headcount': count,
                    'male': male_count,
                    'female': count - male_count,
                    'position_anchors': sum(
                        EFFECTIVE_POSITION_ANCHORS.get(key, (0, 0)),
                    ),
                    'bound': EFFECTIVE_BOUND_HEADCOUNT.get(key, 0),
                    'equipment_route': EFFECTIVE_EQUIPMENT_HEADCOUNT.get(key, 0),
                }
                for key, name, count, male_count in EFFECTIVE_POSITION_HEADCOUNT
            ],
            'scale': SCALE,
            'balanced_anchors': BALANCED,
            'rooms': len(self.rooms),
            'beds': sum(BEDS_PER_ROOM for _ in self.rooms),
            'confirmed_slots': total_slots,
            'position_anchors': {
                key: len(items) for key, items in self.position_anchors.items()
            },
            'binding_anchors': len(self.binding_slots),
            'equipment_anchors': len(self.equipment_anchors),
            'free_unassigned_anchors': len(self.free_slots),
            'blocking_occupancies': sum(
                1 for row in self.employee_rows if row['blocking']
            ),
        }

    def _run_scenario(self, setup_seconds):
        started_all = timer.perf_counter()
        report = {'scenario': self._scenario_report(), 'timing_seconds': {}}

        cohort_timer = timer.perf_counter()
        cohort = create_settlement_cohort(
            watch_period_id=self.period.pk,
            source_revision_id=self.revision.pk,
            source_type='t035_synthetic',
            source_id='T035-COHORT-1',
            source_snapshot={'period_id': self.period.pk, 'scenario': 'T-035'},
            input_fingerprint='a' * 64,
            created_by_id=self.actor.pk,
        )
        for number, row in enumerate(self.employee_rows, start=1):
            add_settlement_cohort_member(
                cohort_id=cohort.pk,
                resident_id=row['resident'].pk,
                arrival_at=self.arrival_at,
                departure_at=self.departure_at,
                participation_status=(
                    SettlementCohortMember.ParticipationStatus.PARTICIPATING
                ),
                reason='',
                expected_schedule_regime='',
                source_revision_id=self.revision.pk,
                basis_type='t035_synthetic',
                basis_id=f'T035-MEMBER-{row["resident"].pk}',
                basis_snapshot={'resident_id': row['resident'].pk},
                production_context_snapshot={'scenario': 'T-035'},
                official_equipment_assignment_id=row['assignment'].pk,
            )
            if STEP_LOG and number % 25 == 0:
                print(
                    f'[T-036 W0] членов когорты добавлено: {number} '
                    f'({timer.perf_counter() - cohort_timer:.1f}s)',
                    flush=True,
                )
        cohort = approve_settlement_cohort(
            cohort_id=cohort.pk,
            approved_by_id=self.actor.pk,
            approved_at=self.now,
        )
        report['timing_seconds']['cohort_create_and_approve_250'] = round(
            timer.perf_counter() - cohort_timer, 3,
        )
        _step('когорта создана и утверждена', started_all)
        report['cohort'] = {
            'id': cohort.pk,
            'status': cohort.status,
            'members': cohort.members.count(),
        }

        resolve_timer = timer.perf_counter()
        resolver_result = resolve_settlement_cohort(cohort_id=cohort.pk)
        report['timing_seconds']['resolver_only'] = round(
            timer.perf_counter() - resolve_timer, 3,
        )
        _step(
            f'resolver: размещено {len(resolver_result.placements)}, '
            f'не размещено {len(resolver_result.unresolved)}',
            started_all,
        )

        preview_timer = timer.perf_counter()
        run = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        report['timing_seconds']['preview_create'] = round(
            timer.perf_counter() - preview_timer, 3,
        )
        _step('preview создан', started_all)

        confirm_timer = timer.perf_counter()
        run = confirm_settlement_preview_run(
            run_id=run.pk,
            control_context=self.control_context,
        )
        report['timing_seconds']['preview_confirm'] = round(
            timer.perf_counter() - confirm_timer, 3,
        )
        _step('preview подтверждён', started_all)

        report['preview'] = {
            'run_id': run.pk,
            'status': run.status,
            'placements': run.placements.count(),
            'unresolved': run.unresolved_rows.count(),
            'resolver_placements': len(resolver_result.placements),
            'resolver_unresolved': len(resolver_result.unresolved),
            'resolver_reason_codes': list(resolver_result.reason_codes),
        }

        row_by_resident = {row['resident'].pk: row for row in self.employee_rows}
        by_source = Counter()
        by_shift_placed = Counter()
        by_position = defaultdict(
            lambda: {'placed': 0, 'unresolved': 0, 'reasons': Counter()},
        )
        for placement in run.placements.select_related('resident'):
            row = row_by_resident[placement.resident_id]
            by_source[placement.source_kind] += 1
            by_shift_placed[placement.work_shift] += 1
            by_position[row['key']]['placed'] += 1

        by_reason = Counter()
        by_shift_unresolved = Counter()
        for unresolved in run.unresolved_rows.select_related('resident'):
            row = row_by_resident[unresolved.resident_id]
            for code in unresolved.reason_codes:
                by_reason[code] += 1
            by_shift_unresolved[unresolved.work_shift] += 1
            bucket = by_position[row['key']]
            bucket['unresolved'] += 1
            for code in unresolved.reason_codes:
                bucket['reasons'][code] += 1

        placed_total = run.placements.count()
        unresolved_total = run.unresolved_rows.count()
        total_slots = report['scenario']['confirmed_slots']
        report['result'] = {
            'auto_placed': placed_total,
            'unresolved': unresolved_total,
            'manual_review_people': unresolved_total,
            'by_source_kind': dict(sorted(by_source.items())),
            'by_reason_code': dict(sorted(by_reason.items())),
            'by_shift': {
                'placed': dict(sorted(by_shift_placed.items())),
                'unresolved': dict(sorted(by_shift_unresolved.items())),
            },
            'by_position': {
                key: {
                    'placed': value['placed'],
                    'unresolved': value['unresolved'],
                    'reasons': dict(sorted(value['reasons'].items())),
                }
                for key, value in sorted(by_position.items())
            },
            'free_confirmed_slots_left': total_slots - placed_total,
            'beds_without_confirmed_slot': (
                report['scenario']['beds'] - total_slots
            ),
        }

        # T-036 W1: инварианты паросочетания — койка не выдана дважды,
        # пол комнаты соблюдён. Без полиномиального matching этот тест
        # на реалистичном фонде не доходит до этой точки вообще.
        placement_beds = list(
            run.placements.values_list('physical_bed_id', flat=True),
        )
        self.assertEqual(
            len(placement_beds),
            len(set(placement_beds)),
            'одна койка не может быть выдана двум жильцам',
        )
        # T-036 backend-14: пол комнаты вычисляется по поставленным жильцам.
        sex_by_resident = {
            row['resident'].pk: row['employee'].sex
            for row in self.employee_rows
        }
        sexes_by_room: dict[int, set[str]] = defaultdict(set)
        capacity_by_room = dict(
            PhysicalRoom.objects.values_list('pk', 'capacity'),
        )
        for placement in run.placements.all():
            room_id = placement.physical_room_id_snapshot
            sexes_by_room[room_id].add(
                sex_by_resident[placement.resident_id],
            )
            self.assertGreater(
                capacity_by_room[room_id],
                2,
                f'двухместная комната {room_id} попала в авторасселение',
            )
        for room_id, sexes in sexes_by_room.items():
            self.assertEqual(
                len(sexes),
                1,
                f'в комнате {room_id} оказались разные полы',
            )

        report['apply'] = {}
        for work_shift, allowed_day in (
            (SettlementCohortMember.WorkShift.DAY, self.period_starts),
            (
                SettlementCohortMember.WorkShift.NIGHT,
                self.period_starts - timedelta(days=1),
            ),
        ):
            apply_timer = timer.perf_counter()
            try:
                application = apply_confirmed_settlement_preview(
                    run_id=run.pk,
                    work_shift=work_shift,
                    control_context=self.control_context,
                    now=self._aware(allowed_day, hour=12),
                )
            except ValidationError as error:
                report['apply'][work_shift] = {
                    'ok': False,
                    'code': getattr(error, 'code', None),
                    'messages': list(error.messages),
                    'seconds': round(timer.perf_counter() - apply_timer, 3),
                }
                continue
            report['apply'][work_shift] = {
                'ok': True,
                'application_id': application.pk,
                'created_occupancies': application.created_occupancies.count(),
                'skipped_count': application.result_snapshot.get(
                    'skipped_count', 0,
                ),
                'skipped': [
                    {
                        'resident_id': row['resident_id'],
                        'source_placement_id': row.get('source_placement_id'),
                        'in_plan': row.get('in_plan', False),
                        'bed_stable_id': row.get('bed_stable_id'),
                        'reason_codes': row['reason_codes'],
                        'rule_ids': row['rule_ids'],
                    }
                    for row in application.result_snapshot.get('skipped_rows', [])
                ],
                'seconds': round(timer.perf_counter() - apply_timer, 3),
            }
            _step(f'apply {work_shift}', started_all)
        report['apply']['occupancies_total'] = EmployeeBedOccupancy.objects.count()
        report['apply']['applications_total'] = run.applications.count()

        # T-036 review-6 note 1: план должен быть применён полностью либо
        # каждая плановая строка должна быть видна в skipped с in_plan=True.
        created_total = sum(
            report['apply'][shift].get('created_occupancies', 0)
            for shift in (
                SettlementCohortMember.WorkShift.DAY,
                SettlementCohortMember.WorkShift.NIGHT,
            )
            if report['apply'][shift].get('ok')
        )
        self.assertEqual(
            created_total,
            placed_total,
            (
                'план разместил больше, чем создало применение: '
                f'apply={report["apply"].get("day", {})} / '
                f'{report["apply"].get("night", {})}'
            ),
        )
        plan_placement_ids = set(
            run.placements.values_list('pk', flat=True),
        )
        skipped_in_plan = [
            row
            for shift in (
                SettlementCohortMember.WorkShift.DAY,
                SettlementCohortMember.WorkShift.NIGHT,
            )
            for row in report['apply'][shift].get('skipped', [])
            if row.get('in_plan')
        ]
        for row in skipped_in_plan:
            self.assertIn(row.get('source_placement_id'), plan_placement_ids)
        self.assertEqual(
            created_total
            + len({row['source_placement_id'] for row in skipped_in_plan}),
            placed_total,
        )
        # T-036 QA-21/22: сценарий должен быть содержательным — кто-то реально
        # размещён, а смешанных по категориям комнат нет. Замеры QA-22 (PG и
        # SQLite совпали): 112/138 (created 66 день + 46 ночь, 4 конфликта
        # категории, 2 применения); повторные прогоны на другой нумерации PK —
        # 111/139 (65+46, 5) и 110/140. Базовая точка без правила — 117/133.
        # Разброс даёт порядок PK и разрешение равных приоритетов, поэтому
        # проверяются границы и инварианты, а не магия числа.
        self.assertGreater(report['result']['auto_placed'], 0)
        self.assertGreaterEqual(report['result']['auto_placed'], 100)
        self.assertLessEqual(report['result']['unresolved'], 150)
        from .personnel_categories import employee_settlement_category

        category_by_resident = {
            row['resident'].pk: employee_settlement_category(row['employee'])
            for row in self.employee_rows
        }
        categories_by_room: dict[int, set[str | None]] = defaultdict(set)
        for placement in run.placements.all():
            categories_by_room[placement.physical_room_id_snapshot].add(
                category_by_resident[placement.resident_id],
            )
        for room_id, categories in categories_by_room.items():
            self.assertEqual(
                len(categories),
                1,
                f'в комнате {room_id} смешаны категории рабочих и ИТР',
            )
        self.assertEqual(
            report['apply']['applications_total'],
            2,
        )
        report['timing_seconds']['data_setup'] = setup_seconds
        return report

    # --- тест ------------------------------------------------------------

    def test_synthetic_watch_250_auto_settlement(self):
        started = timer.perf_counter()
        self._build_reference_layer()
        _step('справочники', started)
        self._build_physical_fund()
        _step('фонд (комнаты/койки)', started)
        self._build_anchors()
        _step(f'якоря/слоты ({self.anchor_seq})', started)
        self._build_people()
        _step('люди/жильцы', started)
        self._build_official_assignments()
        _step('официальные назначения', started)
        self._build_bindings()
        _step('привязки', started)
        self._build_blocking_occupancies()
        _step('посторонние размещения', started)
        setup_seconds = round(timer.perf_counter() - started, 3)

        raw_session_key = 't035-synthetic-watch-250'
        grant = acquire_control_lease(
            owner_access_id=self.access.pk,
            raw_session_key=raw_session_key,
            source='t035-e2e',
        )
        self.control_context = SettlementControlWriteContext(
            owner_access_id=self.access.pk,
            raw_session_key=raw_session_key,
            lease_token=str(grant.lease_token),
            fencing_revision=grant.fencing_revision,
        )

        report = self._run_scenario(setup_seconds)
        report['timing_seconds']['total'] = round(
            timer.perf_counter() - started, 3,
        )

        if REPORT_PATH:
            Path(REPORT_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(REPORT_PATH).write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding='utf-8',
            )
        print('T-035 REPORT START')
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        print('T-035 REPORT END')

        self.assertEqual(
            report['result']['auto_placed'] + report['result']['unresolved'],
            len(self.employee_rows),
        )
        self.assertEqual(
            report['preview']['placements'],
            report['result']['auto_placed'],
        )
