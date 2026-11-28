"""Deterministic, read-only M6 settlement resolver.

The resolver deliberately uses only already authoritative M4/M5 data.  Until a
versioned RoomUseProfile/SettlementResolverRule schema exists, an unbound
resident can use only a pool proven by existing structured anchors/bindings;
unsupported categories fail closed with ``resolver_not_configured``.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType

from .bed_availability import (
    active_prepared_bed_ids,
    active_reserved_bed_ids,
    planned_blocked_bed_ids,
)
from .intervals import occupancy_overlaps_period
from .cohorts import (
    _revalidated_member_shift_source,
    revalidate_routing_cohort_members,
)

from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    AccommodationAnchorCalendarSlot,
    EmployeeAccommodationBinding,
    EmployeeBedOccupancy,
    PhysicalRoom,
    SettlementCohort,
    SettlementCohortMember,
    SettlementPreviewCorrection,
    SettlementResident,
    SettlementRevision,
)
from .residents import authoritative_resident_sex
from .personnel_categories import (
    MISSING_CATEGORY,
    MISSING_CATEGORY_MESSAGE,
    ROOM_CATEGORY_CONFLICT,
    ROOM_CATEGORY_CONFLICT_MESSAGE,
    employee_settlement_category,
    room_allows_personnel_category,
)
from .room_sex import (
    FEMALE,
    MALE,
    ROOM_FIXED,
    ROOM_FREE,
    ROOM_MIXED,
    ROOM_UNKNOWN_OCCUPANT,
    classify_sexes,
    occupancy_sex,
    rejection_reason,
    resident_sex,
)
from .special_fund import is_manual_only_room


REASON_COHORT_NOT_APPROVED = 'cohort_not_approved'
REASON_RESOLVER_NOT_CONFIGURED = 'resolver_not_configured'
REASON_RESIDENT_INACTIVE = 'resident_inactive'
REASON_INCOMPLETE_CONTEXT = 'incomplete_authoritative_context'
REASON_STALE_CALENDAR = 'stale_calendar_relation'
REASON_INVALID_BINDING = 'invalid_existing_binding'
REASON_NO_PLACE = 'no_compatible_place'
REASON_EQUAL_PRIORITY = 'equal_priority_conflict'
REASON_HARD_CONFLICT = 'hard_rule_conflict'
# T-036 domain-2 §4: атомарные причины по фактической причине отказа. Их нельзя
# размазывать по агрегированным hard_rule_conflict/no_compatible_place.
REASON_BED_PREPARED_HOLD = 'bed_prepared_hold'
REASON_BED_RESERVE_HOLD = 'bed_reserve_hold'
REASON_ROOM_SEX_UNKNOWN = 'room_occupant_sex_unknown'
REASON_RESIDENT_SEX_UNKNOWN = 'resident_sex_unknown'
REASON_ROOM_SEX_MISMATCH = 'room_sex_mismatch'
REASON_ROOM_SEX_MIXED = 'room_sex_mixed'
REASON_ROOM_SEX_TAKEN = 'room_sex_taken'
# Решение владельца 22.11.2026: рабочие и ИТР не живут в одной комнате.
REASON_MISSING_CATEGORY = MISSING_CATEGORY
REASON_ROOM_CATEGORY_CONFLICT = ROOM_CATEGORY_CONFLICT


# SET-R078: каждый машинный код причины неразмещения явно связан с правилом
# реестра. Новая причина обязана получить строку здесь, иначе конфликт остаётся
# «безымянным» и не проходит проверку отчёта.
_REASON_RULE_IDS = {
    REASON_COHORT_NOT_APPROVED: ('SET-R006',),
    REASON_RESOLVER_NOT_CONFIGURED: ('SET-R013',),
    REASON_RESIDENT_INACTIVE: ('SET-R003',),
    REASON_INCOMPLETE_CONTEXT: ('SET-R004', 'SET-R044'),
    REASON_STALE_CALENDAR: ('SET-R010',),
    REASON_INVALID_BINDING: ('SET-R014', 'SET-R016'),
    # Нет совместимого места: блокируют официальный профиль/должность и
    # ограничение пола комнаты, а не R032 (непереданная комната отфильтрована
    # раньше и даёт resolver_not_configured).
    REASON_NO_PLACE: ('SET-R004', 'SET-R042', 'SET-R043'),
    REASON_EQUAL_PRIORITY: ('SET-R064',),
    # Жёсткий конфликт: пересечение интервалов и изоляция внешних комнат.
    # Пол и неизвестные данные вынесены в атомарные причины ниже.
    REASON_HARD_CONFLICT: ('SET-R033', 'SET-R034', 'SET-R062'),
    REASON_BED_PREPARED_HOLD: ('SET-R095',),
    REASON_BED_RESERVE_HOLD: ('SET-R096',),
    REASON_ROOM_SEX_UNKNOWN: ('SET-R081', 'SET-R043'),
    REASON_RESIDENT_SEX_UNKNOWN: ('SET-R081', 'SET-R042'),
    REASON_ROOM_SEX_MISMATCH: ('SET-R042', 'SET-R043'),
    REASON_ROOM_SEX_MIXED: ('SET-R042', 'SET-R043'),
    REASON_ROOM_SEX_TAKEN: ('SET-R042', 'SET-R043'),
    # SET-R102: решение владельца 22.11.2026 — рабочие и ИТР не живут в одной
    # комнате даже в разных блоках; без категории кадровой должности авто не
    # размещает.
    REASON_MISSING_CATEGORY: ('SET-R102',),
    REASON_ROOM_CATEGORY_CONFLICT: ('SET-R102',),
}


def _reason_rule_ids(reason_codes) -> tuple[str, ...]:
    return tuple(sorted({
        rule_id
        for reason in reason_codes
        for rule_id in _REASON_RULE_IDS.get(reason, ())
    }))


@dataclass(frozen=True, slots=True)
class ResolverPlacement:
    resident_id: int
    resident_stable_id: str
    member_id: int
    physical_bed_id: int
    bed_stable_id: str
    physical_room_id: int
    action: str
    source_kind: str
    work_shift: str
    shift_source_kind: str
    shift_source_fingerprint: str
    binding_id: int | None
    equipment_assignment_id: int | None
    calendar_slot_id: int
    anchor_id: int
    anchor_bed_assignment_id: int
    profile_identifiers: tuple[str, ...] = ()
    rule_identifiers: tuple[str, ...] = ()
    place_kind: str = ''


@dataclass(frozen=True, slots=True)
class ResolverUnresolved:
    resident_id: int
    resident_stable_id: str
    member_id: int
    reason_codes: tuple[str, ...]
    work_shift: str
    shift_source_kind: str
    shift_source_fingerprint: str
    # SET-R078: явная запись конфликта — правило реестра и период проживания.
    rule_ids: tuple[str, ...] = ()
    interval: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class SettlementResolverResult:
    cohort_id: int
    cohort_stable_id: str
    watch_period_id: int
    placements: tuple[ResolverPlacement, ...]
    unresolved: tuple[ResolverUnresolved, ...]
    reason_codes: tuple[str, ...]
    source_identifiers: tuple[str, ...]
    input_fingerprint: str

    def normalized_payload(self) -> dict:
        return {
            'cohort_id': self.cohort_id,
            'cohort_stable_id': self.cohort_stable_id,
            'watch_period_id': self.watch_period_id,
            'placements': [asdict(item) for item in self.placements],
            'unresolved': [asdict(item) for item in self.unresolved],
            'reason_codes': list(self.reason_codes),
            'source_identifiers': list(self.source_identifiers),
            'input_fingerprint': self.input_fingerprint,
        }

    def normalized_json(self) -> str:
        return json.dumps(
            self.normalized_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )


@dataclass(frozen=True, slots=True)
class _SlotCandidate:
    slot_id: int
    anchor_id: int
    anchor_type: str
    anchor_key: str
    personnel_position_id: int | None
    anchor_equipment_id: int | None
    anchor_bed_assignment_id: int
    bed_id: int
    bed_stable_id: str
    room_id: int
    room_type: str
    room_order: tuple[str, int, str, int, int]


def _candidate_place_kind(candidate: _SlotCandidate) -> str:
    """Тип места: пул (без должности и техники) или место с правом."""

    if (
        candidate.personnel_position_id is None
        and candidate.anchor_equipment_id is None
    ):
        return 'pool'
    return 'positional'


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _normalize_organization(value: str) -> str:
    return ' '.join(value.split()).casefold()


def _period_datetimes(cohort: SettlementCohort) -> tuple[datetime, datetime]:
    current_timezone = timezone.get_current_timezone()
    starts_at = timezone.make_aware(
        datetime.combine(cohort.watch_period.starts_on, time.min),
        current_timezone,
    )
    ends_at = timezone.make_aware(
        datetime.combine(cohort.watch_period.ends_on + timedelta(days=1), time.min),
        current_timezone,
    )
    return starts_at, ends_at


def _occupancy_overlaps_member(occupancy, member) -> bool:
    return occupancy_overlaps_period(
        occupancy=occupancy,
        starts_at=member.arrival_at,
        ends_at=member.departure_at,
    )


def _replaceable_occupancy_kind(*, occupancy, cohort, member) -> str | None:
    """Classify only fully proven current-cohort baseline rows as replaceable."""

    if (
        occupancy.assignment_type
        == EmployeeBedOccupancy.AssignmentType.TEMPORARY_UNAPPROVED
    ):
        # Временное до утверждения — не плановый baseline: такая строка
        # не заменяется молча, для неё нужен явный путь переселения.
        return None

    if (
        member is None
        or occupancy.resident_id != member.resident_id
        or occupancy.replaced_by_application_id is not None
        or occupancy.replaced_by_occupancy_id is not None
        or occupancy.ended_at is not None
        or occupancy.terminated_at is not None
        or not _occupancy_overlaps_member(occupancy, member)
        or occupancy.watch_period_id != cohort.watch_period_id
        or occupancy.cohort_member_id != member.pk
        or member.cohort_id != cohort.pk
        or occupancy.work_shift != member.work_shift
    ):
        return None

    if occupancy.source_kind == EmployeeBedOccupancy.SourceKind.MANUAL:
        if (
            occupancy.source_application_id is None
            and occupancy.source_preview_placement_id is None
            and occupancy.source_preview_correction_id is None
            and occupancy.shift_source_fingerprint == member.shift_source_fingerprint
            and occupancy.shift_source_kind in {
                EmployeeBedOccupancy.ShiftSourceKind.OFFICIAL_ASSIGNMENT,
                EmployeeBedOccupancy.ShiftSourceKind.CLERK_SELECTED,
            }
        ):
            return 'manual_current_cohort'
        return None

    if occupancy.source_kind != EmployeeBedOccupancy.SourceKind.AUTO:
        return None
    application = occupancy.source_application
    placement = occupancy.source_preview_placement
    correction = occupancy.source_preview_correction
    if application is None or (placement is None and correction is None):
        return None
    run = application.preview_run
    source_run_id = correction.preview_run_id if correction else placement.run_id
    source_resident_id = correction.resident_id if correction else placement.resident_id
    source_bed_id = (
        correction.target_physical_bed_id
        if correction and correction.action == SettlementPreviewCorrection.Action.MOVE
        else correction.source_placement.physical_bed_id
        if correction and correction.source_placement_id
        else placement.physical_bed_id
        if placement else None
    )
    source_member_id = (
        correction.source_placement.cohort_member_id_snapshot
        if correction and correction.source_placement_id
        else correction.source_unresolved.cohort_member_id_snapshot
        if correction else placement.cohort_member_id_snapshot
    )
    source_shift_fingerprint = (
        correction.source_snapshot.get('shift_source_fingerprint')
        if correction else placement.normalized_provenance.get('shift_source_fingerprint')
    )
    if (
        application.legacy_whole_run
        or application.work_shift != member.work_shift
        or occupancy.shift_source_kind
        != EmployeeBedOccupancy.ShiftSourceKind.AUTO_PREVIEW
        or (correction.work_shift if correction else placement.work_shift) != member.work_shift
        or occupancy.shift_source_fingerprint
        != source_shift_fingerprint
        or application.watch_period_id != cohort.watch_period_id
        or application.cohort_id != cohort.pk
        or run.pk != source_run_id
        or run.watch_period_id != cohort.watch_period_id
        or run.cohort_id != cohort.pk
        or source_resident_id != occupancy.resident_id
        or source_bed_id != occupancy.physical_bed_id
        or source_member_id != member.pk
        or occupancy.starts_at != member.arrival_at
        or occupancy.ends_at != member.departure_at
    ):
        return None
    return 'auto_current_cohort'


def validate_preview_correction_target(*, cohort, resident, calendar_slot_id, physical_bed_id):
    """Validate one exact M4 target without choosing any fallback row."""

    candidates, _snapshot = _slot_candidates(cohort)
    candidate = candidates.get(calendar_slot_id)
    if candidate is None or candidate.bed_id != physical_bed_id:
        raise ValidationError(
            'Целевой календарный слот не доказывает указанную койку.',
            code='settlement.preview_correction.target_invalid',
        )
    member = (
        SettlementCohortMember.objects.filter(
            cohort=cohort,
            resident_id=resident.pk,
        )
        .order_by('-pk')
        .first()
    )
    if member is not None:
        period_start, period_end = member.arrival_at, member.departure_at
    else:
        period_start, period_end = _period_datetimes(cohort)
    from .room_sex import room_sex_states

    states = room_sex_states(
        room_ids=(candidate.room_id,),
        starts_at=period_start,
        ends_at=period_end,
    )
    if _room_sex_rejection_reason(
        candidate=candidate,
        resident=resident,
        room_states=states,
    ) is not None:
        raise ValidationError(
            'Целевое место не соответствует полу жильцов комнаты.',
            code='settlement.preview_correction.target_conflict',
        )
    if candidate.bed_id in planned_blocked_bed_ids():
        raise ValidationError(
            'Целевое место подготовлено или стоит в резерве.',
            code='settlement.preview_correction.target_conflict',
        )
    # Решение владельца 22.11.2026: рабочие и ИТР не смешиваются в комнате.
    if not resident.is_external:
        category = employee_settlement_category(resident.employee)
        if category is None:
            raise ValidationError(
                MISSING_CATEGORY_MESSAGE,
                code='settlement.preview_correction.target_conflict',
            )
        room_slots = [
            row.slot_id for row in candidates.values()
            if row.room_id == candidate.room_id
        ]
        reservations = (
            EmployeeAccommodationBinding.objects.filter(
                anchor_calendar_slot_id__in=room_slots,
                status=EmployeeAccommodationBinding.Status.CONFIRMED,
                valid_from__lte=cohort.watch_period.ends_on,
                valid_to__gte=cohort.watch_period.starts_on,
                resident__employee__isnull=False,
            )
            .exclude(resident_id=resident.pk)
            .select_related('resident__employee__personnel_position')
        )
        if not room_allows_personnel_category(
            candidate.room_type,
            category,
            (
                employee_settlement_category(row.resident.employee)
                for row in reservations
            ),
        ):
            raise ValidationError(
                ROOM_CATEGORY_CONFLICT_MESSAGE,
                code='settlement.preview_correction.target_conflict',
            )
    return candidate


def _effective_equipment_assignments(*, employee_ids, effective_date):
    """Mirror the accepted production criterion used by legacy preview."""

    return list(
        EquipmentAssignment.objects.filter(
            employee_id__in=employee_ids,
            status=AssignmentStatus.ACCEPTED,
            role__isnull=False,
            shift__isnull=True,
            assigned_at__lte=effective_date,
            source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
            source_crew_plan_slot__isnull=False,
            source_crew_plan_slot__plan__work_date=effective_date.date(),
        )
        .filter(Q(accepted_at__isnull=True) | Q(accepted_at__lte=effective_date))
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=effective_date))
        .select_related(
            'equipment__equipment_type', 'role',
            'source_crew_plan_slot__plan',
        )
        .order_by('employee_id', 'equipment_id', 'shift_type', 'pk')
    )


def _slot_candidates(cohort: SettlementCohort) -> tuple[dict[int, _SlotCandidate], list[dict]]:
    slots = list(
        AccommodationAnchorCalendarSlot.objects.filter(
            watch_period_id=cohort.watch_period_id,
            status=AccommodationAnchorCalendarSlot.Status.CONFIRMED,
        )
        .select_related('anchor', 'watch_period', 'source_revision')
        .order_by('pk')
    )
    assignments = list(
        AccommodationAnchorBedAssignment.objects.filter(
            anchor_id__in=[slot.anchor_id for slot in slots],
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
        )
        .select_related('physical_bed__room__dormitory', 'started_revision')
        .order_by('anchor_id', 'pk')
    )
    period_start, period_end = _period_datetimes(cohort)
    by_anchor: dict[int, list[AccommodationAnchorBedAssignment]] = defaultdict(list)
    for assignment in assignments:
        if assignment.valid_from <= period_start and (
            assignment.valid_to is None or assignment.valid_to >= period_end
        ):
            by_anchor[assignment.anchor_id].append(assignment)

    candidates = {}
    snapshot = []
    for slot in slots:
        matching = by_anchor.get(slot.anchor_id, [])
        assignment_snapshot = [
            {
                'id': item.pk,
                'physical_bed_id': item.physical_bed_id,
                'status': item.status,
                'valid_from': item.valid_from.isoformat(),
                'valid_to': item.valid_to.isoformat() if item.valid_to else None,
                'started_revision_id': item.started_revision_id,
                'started_revision_status': item.started_revision.status,
                'ended_revision_id': item.ended_revision_id,
            }
            for item in matching
        ]
        snapshot.append({
            'slot_id': slot.pk,
            'slot_stable_id': str(slot.stable_id),
            'anchor_id': slot.anchor_id,
            'status': slot.status,
            'source_revision_id': slot.source_revision_id,
            'source_revision_status': slot.source_revision.status,
            'valid_from': slot.valid_from.isoformat(),
            'valid_to': slot.valid_to.isoformat(),
            'calendar_stale': slot.calendar_relation_is_stale,
            'assignments': assignment_snapshot,
        })
        if (
            slot.calendar_relation_is_stale
            or slot.source_revision.status
            != SettlementRevision.Status.CONFIRMED
            or slot.anchor.status != AccommodationAnchor.Status.ACTIVE
            or len(matching) != 1
        ):
            continue
        assignment = matching[0]
        if (
            assignment.started_revision.status
            != SettlementRevision.Status.CONFIRMED
        ):
            # SET-R010: происхождение статуса места — только подтверждённая
            # ревизия. Денормализованного transfer_status недостаточно.
            continue
        bed = assignment.physical_bed
        room = bed.room
        if room.transfer_status != PhysicalRoom.TransferStatus.TRANSFERRED:
            continue
        # SET-R097: двухместные комнаты (capacity <= 2) авто не использует
        # вовсе, даже если карта ошибочно объявила их место.
        if is_manual_only_room(room):
            continue
        # SET-R098/R099: защищённый и сервисный фонд — только ручной путь.
        if slot.anchor.anchor_type in {
            AccommodationAnchor.AnchorType.PROTECTED,
            AccommodationAnchor.AnchorType.SERVICE,
            AccommodationAnchor.AnchorType.RESERVE,
        }:
            continue
        anchor_key = (
            slot.anchor.function_key
            or slot.anchor.group_key
            or slot.anchor.code
        )
        candidates[slot.pk] = _SlotCandidate(
            slot_id=slot.pk,
            anchor_id=slot.anchor_id,
            anchor_type=slot.anchor.anchor_type,
            anchor_key=anchor_key,
            personnel_position_id=slot.anchor.personnel_position_id,
            anchor_equipment_id=slot.anchor.equipment_id,
            anchor_bed_assignment_id=assignment.pk,
            bed_id=bed.pk,
            bed_stable_id=bed.stable_id,
            room_id=room.pk,
            room_type=room.room_type,
            room_order=(
                str(room.dormitory.number),
                room.floor,
                room.corridor_side,
                room.side_position,
                room.number,
            ),
        )
    return candidates, snapshot


def _resident_authoritative_sex(resident):
    try:
        return authoritative_resident_sex(resident)
    except ValidationError:
        return None


def _combined_room_state(state, room_sex, owner_sex):
    """Соединить фактических жильцов комнаты с владельцем пола в плане."""

    if state == ROOM_UNKNOWN_OCCUPANT:
        return state, room_sex
    if state == ROOM_MIXED:
        return state, room_sex
    if owner_sex is None:
        return state, room_sex
    if room_sex is None:
        return ROOM_FIXED, owner_sex
    if room_sex == owner_sex:
        return ROOM_FIXED, owner_sex
    return ROOM_MIXED, None


def _room_sex_rejection_reason(
    *,
    candidate: _SlotCandidate,
    resident,
    room_states,
    owner_sex=None,
) -> str | None:
    """Фактическая причина отказа по полу или ``None``.

    Пол комнаты вычисляется по эффективным жильцам на период проживания
    (``room_sex.py``); атрибут ``PhysicalRoom.sex_restriction`` в решении не
    участвует. Атомарные причины нужны, чтобы ``rule_ids`` называл R081 тогда
    и только тогда, когда блокировка вызвана неизвестным обязательным
    значением (review-2 §2.2).
    """

    state, room_sex = room_states.get(candidate.room_id, (ROOM_FREE, None))
    state, room_sex = _combined_room_state(state, room_sex, owner_sex)
    return rejection_reason(
        sex=resident_sex(resident),
        room_state=state,
        room_sex=room_sex,
    )


def _blocked_bed_reasons(
    bed_id: int,
    prepared_bed_ids: set[int],
    reserved_bed_ids: set[int],
) -> tuple[str, ...]:
    """Атомарные причины удержания койки (подготовка и/или резерв)."""

    reasons = []
    if bed_id in prepared_bed_ids:
        reasons.append(REASON_BED_PREPARED_HOLD)
    if bed_id in reserved_bed_ids:
        reasons.append(REASON_BED_RESERVE_HOLD)
    return tuple(reasons)


def _deterministic_group_matching(
    ordered_resident_ids: list[int],
    resident_candidates: dict[int, tuple[_SlotCandidate, ...]],
) -> dict[int, _SlotCandidate]:
    """Максимальное паросочетание жильцов с койками за полиномиальное время.

    Т-036 W1: заменяет факториальный перебор с возвратом. Используется
    итеративный вариант алгоритма Куна (цепочки дополнения): жильцы
    обрабатываются в переданном порядке, кандидаты — в сохранённом порядке
    предпочтений, поэтому результат детерминирован. Возвращаются только
    реально сопоставленные пары; сравнение ``len(result)`` с числом жильцов
    группы сохраняет прежнее fail-closed правило «группа с дефицитом мест
    уходит в equal_priority_conflict целиком».
    """

    bed_owner: dict[int, int] = {}
    assignment: dict[int, _SlotCandidate] = {}

    def augment(start_resident_id: int) -> bool:
        stack = [start_resident_id]
        choice: list[_SlotCandidate] = []
        next_index = [0]
        visited_beds: set[int] = set()
        while stack:
            resident_id = stack[-1]
            candidates = resident_candidates[resident_id]
            index = next_index[-1]
            pushed = False
            while index < len(candidates):
                candidate = candidates[index]
                index += 1
                if candidate.bed_id in visited_beds:
                    continue
                visited_beds.add(candidate.bed_id)
                owner = bed_owner.get(candidate.bed_id)
                if owner is None:
                    bed_owner[candidate.bed_id] = resident_id
                    assignment[resident_id] = candidate
                    for level, path_candidate in enumerate(choice):
                        path_resident_id = stack[level]
                        bed_owner[path_candidate.bed_id] = path_resident_id
                        assignment[path_resident_id] = path_candidate
                    return True
                next_index[-1] = index
                stack.append(owner)
                choice.append(candidate)
                next_index.append(0)
                pushed = True
                break
            if pushed:
                continue
            stack.pop()
            next_index.pop()
            if choice:
                choice.pop()
        return False

    for resident_id in ordered_resident_ids:
        augment(resident_id)
    return assignment


def _connected_scarcity_groups(
    resident_candidates: dict[int, tuple[_SlotCandidate, ...]],
) -> list[tuple[set[int], set[int]]]:
    bed_to_residents: dict[int, set[int]] = defaultdict(set)
    for resident_id, candidates in resident_candidates.items():
        for candidate in candidates:
            bed_to_residents[candidate.bed_id].add(resident_id)

    remaining = set(resident_candidates)
    groups = []
    while remaining:
        first = next(iter(remaining))
        resident_ids = {first}
        bed_ids: set[int] = set()
        queue = deque([first])
        while queue:
            resident_id = queue.popleft()
            remaining.discard(resident_id)
            for candidate in resident_candidates[resident_id]:
                if candidate.bed_id in bed_ids:
                    continue
                bed_ids.add(candidate.bed_id)
                for neighbour in bed_to_residents[candidate.bed_id]:
                    if neighbour not in resident_ids:
                        resident_ids.add(neighbour)
                        queue.append(neighbour)
        groups.append((resident_ids, bed_ids))
    return groups


def resolve_settlement_cohort(
    *,
    cohort_id: int,
    materialized_preview_run_id: int | None = None,
) -> SettlementResolverResult:
    """Return a deterministic proposal without writing any database row."""

    cohort = (
        SettlementCohort.objects.select_related(
            'watch_period', 'watch_composition', 'source_revision',
            'routing_batch__arrival_roster_version',
        )
        .get(pk=cohort_id)
    )
    members = list(
        SettlementCohortMember.objects.filter(cohort_id=cohort.pk)
        .select_related(
            'cohort__routing_batch__arrival_roster_version',
            'resident__employee__work_schedule',
            'resident__employee__personnel_position',
            'source_revision',
            'watch_profile_work_schedule',
            'watch_profile_watch_composition',
            'employee_watch_profile_change',
            'routing_row',
            'routing_event__crew_plan_slot__plan__role',
            'brigade_phase_row__version',
            'official_equipment_assignment__equipment__equipment_type',
            'official_equipment_assignment__source_crew_plan_slot__plan__role',
            'shift_selected_by_access__employee', 'shift_selected_by_access__role',
        )
        .order_by('resident__stable_id', 'pk')
    )
    routing_context = None
    if cohort.routing_batch_id is not None:
        routing_context = revalidate_routing_cohort_members(
            cohort=cohort,
            members=members,
            allow_unverified_watch_profile=True,
        )
    return _resolve_members(
        cohort=cohort, members=members, routing_context=routing_context,
        materialized_preview_run_id=materialized_preview_run_id,
    )


def resolve_ready_arrivals(*, batch, readiness, rows):
    from .cohorts import build_provisional_members

    cohort, members, routing = build_provisional_members(batch=batch, readiness=readiness, rows=rows)
    return _resolve_members(cohort=cohort, members=members, routing_context=routing, provisional=True)


def _resolve_members(*, cohort, members, routing_context, materialized_preview_run_id=None, provisional=False):
    active_members = [item for item in members if item.participates_in_accommodation]
    candidates, slot_snapshot = _slot_candidates(cohort)
    # T-029: подготовленные и резервные койки в плановое распределение не идут.
    # Два множества нужны раздельно, чтобы назвать фактическое правило R095/R096.
    prepared_bed_ids = active_prepared_bed_ids()
    reserved_plan_bed_ids = active_reserved_bed_ids()
    blocked_bed_ids = prepared_bed_ids | reserved_plan_bed_ids
    starts_at, ends_at = _period_datetimes(cohort)
    internal_employee_ids = {
        item.resident.employee_id
        for item in active_members
        if item.resident.employee_id
    }
    if routing_context is None:
        effective_equipment_assignments = _effective_equipment_assignments(
            employee_ids=internal_employee_ids,
            effective_date=starts_at,
        )
    else:
        effective_equipment_assignments = sorted(
            {
                context['official_equipment_assignment'].pk:
                    context['official_equipment_assignment']
                for context in routing_context.values()
                if context['official_equipment_assignment'] is not None
            }.values(),
            key=lambda item: item.pk,
        )
    equipment_assignments_by_employee: dict[int, list[EquipmentAssignment]] = defaultdict(list)
    for equipment_assignment in effective_equipment_assignments:
        equipment_assignments_by_employee[equipment_assignment.employee_id].append(
            equipment_assignment,
        )

    equipment_ids = {
        item.equipment_id for item in effective_equipment_assignments
    }
    equipment_anchors = list(
        AccommodationAnchor.objects.filter(
            equipment_id__in=equipment_ids,
            anchor_type=AccommodationAnchor.AnchorType.EQUIPMENT,
            status=AccommodationAnchor.Status.ACTIVE,
        ).order_by('equipment_id', 'pk')
    )
    equipment_anchors_by_equipment: dict[int, list[AccommodationAnchor]] = defaultdict(list)
    for equipment_anchor in equipment_anchors:
        equipment_anchors_by_equipment[equipment_anchor.equipment_id].append(
            equipment_anchor,
        )

    equipment_anchor_assignments = list(
        AccommodationAnchorBedAssignment.objects.filter(
            anchor_id__in=[item.pk for item in equipment_anchors],
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            valid_from__lte=starts_at,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=starts_at))
        .select_related('physical_bed__room')
        .order_by('anchor_id', 'pk')
    )
    equipment_anchor_assignments_by_anchor: dict[
        int, list[AccommodationAnchorBedAssignment]
    ] = defaultdict(list)
    for anchor_assignment in equipment_anchor_assignments:
        equipment_anchor_assignments_by_anchor[anchor_assignment.anchor_id].append(
            anchor_assignment,
        )

    equipment_slots = list(
        AccommodationAnchorCalendarSlot.objects.filter(
            anchor_id__in=[item.pk for item in equipment_anchors],
            watch_period_id=cohort.watch_period_id,
            status=AccommodationAnchorCalendarSlot.Status.CONFIRMED,
        )
        .select_related('watch_period')
        .order_by('anchor_id', 'pk')
    )
    equipment_slots_by_anchor: dict[int, list[AccommodationAnchorCalendarSlot]] = defaultdict(list)
    for equipment_slot in equipment_slots:
        equipment_slots_by_anchor[equipment_slot.anchor_id].append(equipment_slot)

    all_period_bindings = list(
        EmployeeAccommodationBinding.objects.filter(
            status=EmployeeAccommodationBinding.Status.CONFIRMED,
            valid_from__lte=cohort.watch_period.ends_on,
            valid_to__gte=cohort.watch_period.starts_on,
        )
        .select_related('resident__employee__personnel_position', 'anchor_calendar_slot__anchor')
        .order_by('resident_id', 'pk')
    )
    cohort_resident_ids = {item.resident_id for item in active_members}
    bindings = [
        binding
        for binding in all_period_bindings
        if binding.resident_id in cohort_resident_ids
    ]
    bindings_by_resident: dict[int, list[EmployeeAccommodationBinding]] = defaultdict(list)
    for binding in bindings:
        bindings_by_resident[binding.resident_id].append(binding)

    member_by_resident = {item.resident_id: item for item in active_members}
    resident_categories = {
        item.resident_id: (
            None if item.resident.is_external
            else employee_settlement_category(item.resident.employee)
        )
        for item in active_members
    }
    # Решение владельца 22.11.2026: рабочие и ИТР не живут в одной комнате.
    # Окно чтения занятостей расширено по датам участников, чтобы «пересечение
    # дат» учитывалось даже за пределами периода вахты.
    occupancy_window_start = min(
        (starts_at, *(item.arrival_at for item in active_members)),
    )
    occupancy_window_end = max(
        (ends_at, *(item.departure_at for item in active_members)),
    )
    occupancy_rows = list(
        EmployeeBedOccupancy.objects.filter(
            starts_at__lt=occupancy_window_end,
            replaced_by_application__isnull=True,
            replaced_by_occupancy__isnull=True,
        )
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=occupancy_window_start))
        .filter(
            Q(terminated_at__isnull=True)
            | Q(terminated_at__gt=occupancy_window_start),
        )
        .select_related(
            'resident__employee__personnel_position',
            'physical_bed__room',
            'cohort_member',
            'source_application__preview_run',
            'source_preview_placement',
            'source_preview_correction__source_placement',
            'source_preview_correction__source_unresolved',
        )
        .order_by('pk')
    )
    occupancy_kinds = {
        item.pk: _replaceable_occupancy_kind(
            occupancy=item,
            cohort=cohort,
            member=member_by_resident.get(item.resident_id),
        )
        for item in occupancy_rows
    }
    materialized_current_run_ids = {
        item.pk
        for item in occupancy_rows
        if (
            materialized_preview_run_id is not None
            and occupancy_kinds[item.pk] == 'auto_current_cohort'
            and item.source_application.preview_run_id == materialized_preview_run_id
        )
    }
    blocking_occupancy_rows = [
        item
        for item in occupancy_rows
        if item.pk not in materialized_current_run_ids
        and occupancy_kinds[item.pk] is None
    ]
    blocking_resident_ids = {
        item.resident_id for item in blocking_occupancy_rows
    }
    occupied_bed_ids = {
        item.physical_bed_id for item in blocking_occupancy_rows
    }
    internal_room_ids = {
        item.physical_bed.room_id
        for item in blocking_occupancy_rows
        if not item.resident.is_external
    }
    room_categories: dict[int, set[str]] = defaultdict(set)
    for item in blocking_occupancy_rows:
        if not item.resident.is_external:
            room_categories[item.physical_bed.room_id].add(
                employee_settlement_category(item.resident.employee),
            )

    # T-036 backend-14: пол комнаты вычисляется по эффективным жильцам на
    # период проживания. `room_sex_owner` фиксирует пол комнаты внутри плана:
    # первый поставленный жилец закрывает комнату для другого пола до конца
    # прогона (решение владельца 20.11.2026).
    occupancy_rows_by_room: dict[int, list] = defaultdict(list)
    for item in blocking_occupancy_rows:
        occupancy_rows_by_room[item.physical_bed.room_id].append(item)
    room_sex_owner: dict[int, str] = {}
    occupant_sexes_cache: dict[tuple[int, object, object], tuple[str, ...]] = {}

    def occupant_sexes(room_id, member):
        key = (room_id, member.arrival_at, member.departure_at)
        cached = occupant_sexes_cache.get(key)
        if cached is not None:
            return cached
        values = tuple(
            occupancy_sex(row)
            for row in occupancy_rows_by_room.get(room_id, ())
            if occupancy_overlaps_period(
                occupancy=row,
                starts_at=member.arrival_at,
                ends_at=member.departure_at,
            )
        )
        occupant_sexes_cache[key] = values
        return values

    def room_state_for(room_id, member):
        sexes = list(occupant_sexes(room_id, member))
        owner = room_sex_owner.get(room_id)
        if owner is not None:
            sexes.append(owner)
        return classify_sexes(sexes)

    def sex_rejection(candidate, resident, member):
        state, room_sex_value = room_state_for(candidate.room_id, member)
        return rejection_reason(
            sex=resident_sex(resident),
            room_state=state,
            room_sex=room_sex_value,
        )

    def fix_room_sex(candidate, resident):
        sex = resident_sex(resident)
        if sex in {MALE, FEMALE}:
            room_sex_owner.setdefault(candidate.room_id, sex)

    external_room_organizations: dict[int, set[str]] = defaultdict(set)
    external_room_ids: set[int] = {
        item.physical_bed.room_id
        for item in blocking_occupancy_rows
        if item.resident.is_external
    }
    for occupancy in blocking_occupancy_rows:
        if occupancy.resident.is_external:
            external_room_organizations[occupancy.physical_bed.room_id].add(
                _normalize_organization(occupancy.resident.organization),
            )
    binding_slot_ids = {
        binding.anchor_calendar_slot_id for binding in all_period_bindings
    }
    for binding in all_period_bindings:
        candidate = candidates.get(binding.anchor_calendar_slot_id)
        if candidate is None:
            continue
        if binding.resident.is_external:
            external_room_ids.add(candidate.room_id)
            external_room_organizations[candidate.room_id].add(
                _normalize_organization(binding.resident.organization),
            )
        else:
            internal_room_ids.add(candidate.room_id)
            # Категории подтверждённых привязок учитываем заранее только для
            # жильцов вне текущего состава: своя привязка внутри состава
            # учитывается при фактической постановке (иначе двое из одного
            # состава блокировали бы друг друга ещё до матчинга).
            if binding.resident_id not in cohort_resident_ids:
                room_categories[candidate.room_id].add(
                    employee_settlement_category(binding.resident.employee),
                )

    def member_category_problem(candidate, member):
        """Причина отказа по категории для кандидата или ``None``."""
        if member.resident.is_external:
            return None
        category = resident_categories.get(member.resident_id)
        if category is None:
            return MISSING_CATEGORY
        roommate_categories = room_categories[candidate.room_id]
        # Review-12 N-3: пустая категория у соседа — это «уточните категорию»,
        # а не «смешение рабочих и ИТР».
        if None in roommate_categories:
            return MISSING_CATEGORY
        if not room_allows_personnel_category(
            candidate.room_type,
            category,
            roommate_categories,
        ):
            return ROOM_CATEGORY_CONFLICT
        return None

    routing_member_provenance = {}
    if routing_context is not None:
        routing_member_provenance = {
            item.pk: {
                'routing_row_id': item.routing_row_id,
                'routing_event_id': item.routing_event_id,
                'brigade_phase_row_id': item.brigade_phase_row_id,
                'brigade_phase_version_id': item.brigade_phase_row.version_id,
                'official_equipment_assignment_id': (
                    item.official_equipment_assignment_id
                ),
                'source_crew_plan_slot_id': (
                    item.official_equipment_assignment.source_crew_plan_slot_id
                    if item.official_equipment_assignment_id else None
                ),
                'watch_profile': routing_context[item.pk]['watch_profile'],
            }
            for item in members
        }

    snapshot = {
        'resolver_version': 'm6-personnel-category-v2',
        'cohort': {
            'id': cohort.pk,
            'stable_id': str(cohort.stable_id),
            'status': cohort.status,
            'watch_composition_id': cohort.watch_composition_id,
            'watch_period_id': cohort.watch_period_id,
            'period_composition_id': cohort.watch_period.watch_composition_id,
            'starts_on': cohort.watch_period.starts_on.isoformat(),
            'ends_on': cohort.watch_period.ends_on.isoformat(),
            'source_revision_id': cohort.source_revision_id,
            'input_fingerprint': cohort.input_fingerprint,
            **(
                {
                    'routing_batch_id': cohort.routing_batch_id,
                    'arrival_roster_version_id': (
                        cohort.routing_batch.arrival_roster_version_id
                    ),
                }
                if routing_context is not None else {}
            ),
        },
        'members': [
            {
                'id': item.pk,
                'stable_id': str(item.stable_id),
                'resident_id': item.resident_id,
                'resident_stable_id': str(item.resident.stable_id),
                'resident_type': item.resident.resident_type,
                'resident_status': item.resident.status,
                'resident_revision': item.resident.revision,
                'authoritative_sex': _resident_authoritative_sex(item.resident),
                'external_sex': item.resident.external_sex,
                'organization': (
                    _normalize_organization(item.resident.organization)
                    if item.resident.is_external else ''
                ),
                'employee_id': item.resident.employee_id,
                'employee_status': (
                    item.resident.employee.status if item.resident.employee_id else None
                ),
                'employee_active': (
                    item.resident.employee.is_active if item.resident.employee_id else None
                ),
                'employee_sex': (
                    item.resident.employee.sex if item.resident.employee_id else None
                ),
                'employee_position_id': (
                    item.resident.employee.personnel_position_id
                    if item.resident.employee_id else None
                ),
                'settlement_category': (
                    None if item.resident.is_external
                    else employee_settlement_category(item.resident.employee)
                ),
                'employee_composition_id': (
                    routing_context[item.pk]['watch_profile']['watch_composition_id']
                    if routing_context is not None and item.resident.employee_id
                    else (
                        item.resident.employee.watch_composition_id
                        if item.resident.employee_id else None
                    )
                ),
                'arrival_at': item.arrival_at.isoformat(),
                'departure_at': item.departure_at.isoformat(),
                'participation_status': item.participation_status,
                'source_revision_id': item.source_revision_id,
                'basis_type': item.basis_type,
                'basis_id': item.basis_id,
                'basis_snapshot': item.basis_snapshot,
                'production_context_snapshot': item.production_context_snapshot,
                'work_shift': item.work_shift,
                'shift_source_kind': item.shift_source_kind,
                'official_equipment_assignment_id': item.official_equipment_assignment_id,
                'shift_selected_by_access_id': item.shift_selected_by_access_id,
                'shift_selected_at': (
                    item.shift_selected_at.isoformat() if item.shift_selected_at else None
                ),
                'shift_selection_basis': item.shift_selection_basis,
                'shift_source_snapshot': item.shift_source_snapshot,
                'shift_source_fingerprint': item.shift_source_fingerprint,
                **(
                    {'routing_provenance': routing_member_provenance[item.pk]}
                    if routing_context is not None else {}
                ),
            }
            for item in members
        ],
        'slots': slot_snapshot,
        'candidate_room_types': [
            (slot_id, candidates[slot_id].room_type)
            for slot_id in sorted(candidates)
        ],
        'equipment_assignments': [
            {
                'id': item.pk,
                'employee_id': item.employee_id,
                'equipment_id': item.equipment_id,
                'role_id': item.role_id,
                'shift_id': item.shift_id,
                'shift_type': item.shift_type,
                'status': item.status,
                'assigned_at': item.assigned_at.isoformat(),
                'accepted_at': item.accepted_at.isoformat() if item.accepted_at else None,
                'ended_at': item.ended_at.isoformat() if item.ended_at else None,
                'equipment_active': item.equipment.is_active,
                'equipment_type_id': item.equipment.equipment_type_id,
                'equipment_type_active': item.equipment.equipment_type.is_active,
                'source_kind': item.source_kind,
                'source_crew_plan_slot_id': item.source_crew_plan_slot_id,
                'source_plan_id': item.source_crew_plan_slot.plan_id,
                'source_plan_status': item.source_crew_plan_slot.plan.status,
                'source_plan_work_date': item.source_crew_plan_slot.plan.work_date.isoformat(),
            }
            for item in sorted(effective_equipment_assignments, key=lambda row: row.pk)
        ],
        'equipment_anchors': [
            {
                'id': item.pk,
                'stable_id': str(item.stable_id),
                'equipment_id': item.equipment_id,
                'status': item.status,
                'anchor_type': item.anchor_type,
                'created_revision_id': item.created_revision_id,
            }
            for item in equipment_anchors
        ],
        'equipment_anchor_assignments': [
            {
                'id': item.pk,
                'anchor_id': item.anchor_id,
                'physical_bed_id': item.physical_bed_id,
                'status': item.status,
                'valid_from': item.valid_from.isoformat(),
                'valid_to': item.valid_to.isoformat() if item.valid_to else None,
                'started_revision_id': item.started_revision_id,
                'ended_revision_id': item.ended_revision_id,
            }
            for item in equipment_anchor_assignments
        ],
        'bindings': [
            {
                'id': item.pk,
                'stable_id': str(item.stable_id),
                'resident_id': item.resident_id,
                'slot_id': item.anchor_calendar_slot_id,
                'settlement_category': (
                    None if item.resident.is_external
                    else employee_settlement_category(item.resident.employee)
                ),
                'valid_from': item.valid_from.isoformat(),
                'valid_to': item.valid_to.isoformat(),
                'source_revision_id': item.source_revision_id,
                'basis_type': item.basis_type,
                'basis_id': item.basis_id,
            }
            for item in all_period_bindings
        ],
        'occupancies': [
            {
                'id': item.pk,
                'classification': occupancy_kinds[item.pk] or 'non_replaceable',
                'resident_id': item.resident_id,
                'resident_revision': item.resident.revision,
                'settlement_category': (
                    None if item.resident.is_external
                    else employee_settlement_category(item.resident.employee)
                ),
                'bed_id': item.physical_bed_id,
                'assignment_type': item.assignment_type,
                'source_kind': item.source_kind,
                'work_shift': item.work_shift,
                'shift_source_kind': item.shift_source_kind,
                'shift_source_fingerprint': item.shift_source_fingerprint,
                'shift_official_assignment_id': item.shift_official_assignment_id,
                'shift_selected_by_access_id': item.shift_selected_by_access_id,
                'shift_selected_at': (
                    item.shift_selected_at.isoformat() if item.shift_selected_at else None
                ),
                'shift_selection_basis': item.shift_selection_basis,
                'watch_period_id': item.watch_period_id,
                'cohort_member_id': item.cohort_member_id,
                'target_cohort_id': cohort.pk,
                'target_watch_period_id': cohort.watch_period_id,
                'source_application_id': item.source_application_id,
                'source_application_cohort_id': (
                    item.source_application.cohort_id
                    if item.source_application_id else None
                ),
                'source_application_work_shift': (
                    item.source_application.work_shift
                    if item.source_application_id else None
                ),
                'source_application_legacy_whole_run': (
                    item.source_application.legacy_whole_run
                    if item.source_application_id else None
                ),
                'source_application_watch_period_id': (
                    item.source_application.watch_period_id
                    if item.source_application_id else None
                ),
                'source_preview_run_id': (
                    item.source_application.preview_run_id
                    if item.source_application_id else None
                ),
                'source_preview_placement_id': item.source_preview_placement_id,
                'source_preview_correction_id': item.source_preview_correction_id,
                'source_preview_correction_fingerprint': (
                    item.source_preview_correction.fingerprint
                    if item.source_preview_correction_id else None
                ),
                'source_placement_run_id': (
                    item.source_preview_placement.run_id
                    if item.source_preview_placement_id else None
                ),
                'source_placement_work_shift': (
                    item.source_preview_placement.work_shift
                    if item.source_preview_placement_id else None
                ),
                'starts_at': item.starts_at.isoformat(),
                'ends_at': item.ends_at.isoformat() if item.ends_at else None,
                'terminated_at': (
                    item.terminated_at.isoformat() if item.terminated_at else None
                ),
                'ended_at': item.ended_at.isoformat() if item.ended_at else None,
                'replaced_by_application_id': item.replaced_by_application_id,
                'replaced_by_occupancy_id': item.replaced_by_occupancy_id,
            }
            for item in sorted(occupancy_rows, key=lambda row: row.pk)
            if item.pk not in materialized_current_run_ids
        ],
    }
    fingerprint = _canonical_hash(snapshot)
    routing_source_identifiers = []
    if routing_context is not None:
        routing_source_identifiers.extend((
            f'routing-batch:{cohort.routing_batch_id}',
            'arrival-roster-version:'
            f'{cohort.routing_batch.arrival_roster_version_id}',
        ))
        for item in members:
            profile = routing_context[item.pk]['watch_profile']
            routing_source_identifiers.extend((
                f'routing-row:{item.routing_row_id}',
                f'routing-event:{item.routing_event_id}',
                f'brigade-phase-row:{item.brigade_phase_row_id}',
                f'brigade-phase-version:{item.brigade_phase_row.version_id}',
                f"watch-profile:{profile['profile_fingerprint']}",
            ))
            if profile['employee_watch_profile_change_id'] is not None:
                routing_source_identifiers.append(
                    'employee-watch-profile-change:'
                    f"{profile['employee_watch_profile_change_id']}",
                )
    source_identifiers = tuple(sorted({
        'resolver:m6-personnel-category-v2',
        f'cohort:{cohort.stable_id}',
        f'cohort-source-revision:{cohort.source_revision_id}',
        *(
            f'member-source-revision:{item.source_revision_id}'
            for item in members
        ),
        *(
            f'member-shift-source:{item.shift_source_fingerprint}'
            for item in members
            if item.shift_source_fingerprint
        ),
        *(
            f'binding:{item.stable_id}'
            for item in all_period_bindings
        ),
        *(
            f'equipment-assignment:{item.pk}'
            for item in effective_equipment_assignments
        ),
        *routing_source_identifiers,
    }))

    unresolved: dict[int, ResolverUnresolved] = {}
    placements: list[ResolverPlacement] = []
    claimed_bed_ids = set(occupied_bed_ids)

    def reject(member, *reasons):
        reason_codes = tuple(sorted(set(reasons)))
        unresolved[member.resident_id] = ResolverUnresolved(
            resident_id=member.resident_id,
            resident_stable_id=str(member.resident.stable_id),
            member_id=member.pk,
            reason_codes=reason_codes,
            work_shift=member.work_shift,
            shift_source_kind=member.shift_source_kind,
            shift_source_fingerprint=member.shift_source_fingerprint,
            rule_ids=_reason_rule_ids(reason_codes),
            interval=(
                member.arrival_at.isoformat(),
                member.departure_at.isoformat(),
            ),
        )

    cohort_failure_reason = None
    if not provisional and cohort.status != SettlementCohort.Status.APPROVED:
        cohort_failure_reason = REASON_COHORT_NOT_APPROVED
    elif cohort.watch_period.watch_composition_id != cohort.watch_composition_id:
        cohort_failure_reason = REASON_STALE_CALENDAR
    elif not cohort.watch_period.is_active or not cohort.watch_composition.is_active:
        cohort_failure_reason = REASON_INCOMPLETE_CONTEXT
    if cohort_failure_reason:
        for member in active_members:
            reject(member, cohort_failure_reason)
        return SettlementResolverResult(
            cohort_id=cohort.pk,
            cohort_stable_id=str(cohort.stable_id),
            watch_period_id=cohort.watch_period_id,
            placements=(),
            unresolved=tuple(sorted(unresolved.values(), key=lambda item: item.resident_stable_id)),
            reason_codes=(cohort_failure_reason,),
            source_identifiers=source_identifiers,
            input_fingerprint=fingerprint,
        )

    # ADR-023: женские группы обрабатываются первыми; внутри группы порядок
    # детерминирован stable_id.
    def order_key(member):
        sex = resident_sex(member.resident)
        return (0 if sex == FEMALE else 1, str(member.resident.stable_id))

    ordered_members = sorted(active_members, key=order_key)
    unbound_members = []
    for member in ordered_members:
        resident = member.resident
        try:
            _revalidated_member_shift_source(
                member=member,
                period=cohort.watch_period,
                for_update=False,
                routing_context=routing_context,
            )
        except ValidationError:
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue
        if resident.status != SettlementResident.Status.ACTIVE:
            reject(member, REASON_RESIDENT_INACTIVE)
            continue
        if (
            resident.is_external
            and _resident_authoritative_sex(resident) not in {'male', 'female'}
        ):
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue
        if member.departure_at <= member.arrival_at:
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue
        if resident.pk in blocking_resident_ids:
            reject(member, REASON_HARD_CONFLICT)
            continue
        if resident.employee_id:
            employee = resident.employee
            if (
                not employee.is_active
                or employee.status != employee.Status.ACTIVE
            ):
                reject(member, REASON_RESIDENT_INACTIVE)
                continue
            resolved_composition_id = (
                routing_context[member.pk]['watch_profile']['watch_composition_id']
                if routing_context is not None
                else employee.watch_composition_id
            )
            if resolved_composition_id != cohort.watch_composition_id:
                reject(member, REASON_INCOMPLETE_CONTEXT)
                continue

        resident_bindings = bindings_by_resident.get(resident.pk, [])
        if len(resident_bindings) > 1:
            reject(member, REASON_INVALID_BINDING)
            continue
        if not resident_bindings:
            unbound_members.append(member)
            continue

        binding = resident_bindings[0]
        slot = binding.anchor_calendar_slot
        candidate = candidates.get(slot.pk)
        invalid_reasons: tuple[str, ...] = ()
        if slot.calendar_relation_is_stale:
            invalid_reasons = (REASON_STALE_CALENDAR,)
        elif (
            slot.watch_period_id != cohort.watch_period_id
            or slot.watch_composition_id != cohort.watch_composition_id
            or binding.valid_from > cohort.watch_period.starts_on
            or binding.valid_to < cohort.watch_period.ends_on
        ):
            invalid_reasons = (REASON_INVALID_BINDING,)
        elif candidate is None:
            invalid_reasons = (REASON_INVALID_BINDING,)
        elif candidate.bed_id in claimed_bed_ids:
            invalid_reasons = (REASON_HARD_CONFLICT,)
        elif candidate.bed_id in blocked_bed_ids:
            # Койка подготовлена или в резерве: планового сюда не селим.
            invalid_reasons = _blocked_bed_reasons(
                candidate.bed_id,
                prepared_bed_ids,
                reserved_plan_bed_ids,
            )
        elif resident.is_external and candidate.room_id in internal_room_ids:
            invalid_reasons = (REASON_HARD_CONFLICT,)
        elif not resident.is_external and candidate.room_id in external_room_ids:
            invalid_reasons = (REASON_HARD_CONFLICT,)
        else:
            rejection = sex_rejection(candidate, resident, member)
            if rejection is not None:
                invalid_reasons = (rejection,)
            else:
                category_problem = member_category_problem(candidate, member)
                if category_problem is not None:
                    invalid_reasons = (category_problem,)
        if invalid_reasons:
            reject(member, *invalid_reasons)
            continue

        claimed_bed_ids.add(candidate.bed_id)
        fix_room_sex(candidate, resident)
        if resident.is_external:
            external_room_ids.add(candidate.room_id)
            external_room_organizations[candidate.room_id].add(
                _normalize_organization(resident.organization),
            )
        else:
            internal_room_ids.add(candidate.room_id)
            room_categories[candidate.room_id].add(resident_categories[resident.pk])
        placements.append(ResolverPlacement(
            resident_id=resident.pk,
            resident_stable_id=str(resident.stable_id),
            member_id=member.pk,
            physical_bed_id=candidate.bed_id,
            bed_stable_id=candidate.bed_stable_id,
            physical_room_id=candidate.room_id,
            action='SETTLE',
            source_kind='confirmed_binding',
            work_shift=member.work_shift,
            shift_source_kind=member.shift_source_kind,
            shift_source_fingerprint=member.shift_source_fingerprint,
            binding_id=binding.pk,
            equipment_assignment_id=None,
            calendar_slot_id=candidate.slot_id,
            anchor_id=candidate.anchor_id,
            anchor_bed_assignment_id=candidate.anchor_bed_assignment_id,
            profile_identifiers=(),
            rule_identifiers=('m4.confirmed_binding',),
            place_kind=_candidate_place_kind(candidate),
        ))

    equipment_proposals: dict[int, list[tuple[SettlementCohortMember, EquipmentAssignment, _SlotCandidate]]] = defaultdict(list)
    remaining_unbound_members = []
    for member in unbound_members:
        resident = member.resident
        if resident.is_external:
            remaining_unbound_members.append(member)
            continue
        employee = resident.employee
        active_assignments = equipment_assignments_by_employee.get(employee.pk, [])
        if not active_assignments:
            remaining_unbound_members.append(member)
            continue
        if len(active_assignments) != 1:
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue

        equipment_assignment = active_assignments[0]
        if equipment_assignment.pk != member.official_equipment_assignment_id:
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue
        equipment = equipment_assignment.equipment
        if (
            equipment_assignment.shift_type not in WorkShiftType.values
            or equipment_assignment.role_id is None
            or equipment_assignment.shift_id is not None
            or not equipment.is_active
            or not equipment.equipment_type.is_active
        ):
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue

        matching_anchors = equipment_anchors_by_equipment.get(equipment.pk, [])
        if not matching_anchors:
            if employee.personnel_position_id:
                remaining_unbound_members.append(member)
            else:
                reject(member, REASON_RESOLVER_NOT_CONFIGURED)
            continue
        if len(matching_anchors) != 1:
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue
        equipment_anchor = matching_anchors[0]

        matching_anchor_assignments = equipment_anchor_assignments_by_anchor.get(
            equipment_anchor.pk,
            [],
        )
        if not matching_anchor_assignments:
            reject(member, REASON_RESOLVER_NOT_CONFIGURED)
            continue
        if len(matching_anchor_assignments) != 1:
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue
        anchor_assignment = matching_anchor_assignments[0]

        matching_slots = equipment_slots_by_anchor.get(equipment_anchor.pk, [])
        if not matching_slots:
            reject(member, REASON_RESOLVER_NOT_CONFIGURED)
            continue
        if len(matching_slots) != 1:
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue
        equipment_slot = matching_slots[0]
        if equipment_slot.calendar_relation_is_stale:
            reject(member, REASON_STALE_CALENDAR)
            continue

        candidate = candidates.get(equipment_slot.pk)
        if candidate is None:
            if anchor_assignment.physical_bed_id in blocked_bed_ids:
                reject(
                    member,
                    *_blocked_bed_reasons(
                        anchor_assignment.physical_bed_id,
                        prepared_bed_ids,
                        reserved_plan_bed_ids,
                    ),
                )
            elif not anchor_assignment.physical_bed.is_available:
                reject(member, REASON_HARD_CONFLICT)
            else:
                reject(member, REASON_RESOLVER_NOT_CONFIGURED)
            continue
        if candidate.anchor_bed_assignment_id != anchor_assignment.pk:
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue
        if candidate.room_id in external_room_ids:
            reject(member, REASON_HARD_CONFLICT)
            continue
        rejection = sex_rejection(candidate, resident, member)
        if rejection is not None:
            reject(member, rejection)
            continue
        if candidate.bed_id in claimed_bed_ids:
            reject(member, REASON_HARD_CONFLICT)
            continue
        if candidate.bed_id in blocked_bed_ids:
            reject(
                member,
                *_blocked_bed_reasons(
                    candidate.bed_id,
                    prepared_bed_ids,
                    reserved_plan_bed_ids,
                ),
            )
            continue
        category_problem = member_category_problem(candidate, member)
        if category_problem is not None:
            reject(member, category_problem)
            continue
        equipment_proposals[candidate.bed_id].append(
            (member, equipment_assignment, candidate),
        )

    equipment_categories_by_room: dict[int, set[str]] = defaultdict(set)
    for proposals in equipment_proposals.values():
        if len(proposals) == 1:
            proposal_member, _assignment, proposal_candidate = proposals[0]
            equipment_categories_by_room[proposal_candidate.room_id].add(
                resident_categories.get(proposal_member.resident_id),
            )

    pending_equipment = []
    for bed_id in sorted(equipment_proposals):
        proposals = equipment_proposals[bed_id]
        if len(proposals) != 1:
            for member, _equipment_assignment, _candidate in proposals:
                reject(member, REASON_HARD_CONFLICT)
            continue
        pending_equipment.append(proposals[0])
    # Женские группы — первыми (ADR-023); внутри группы порядок stable_id.
    pending_equipment.sort(key=lambda item: order_key(item[0]))
    for member, equipment_assignment, candidate in pending_equipment:
        rejection = sex_rejection(candidate, member.resident, member)
        if rejection is not None:
            reject(member, rejection)
            continue
        if len(equipment_categories_by_room.get(candidate.room_id, set())) > 1:
            reject(member, ROOM_CATEGORY_CONFLICT)
            continue
        claimed_bed_ids.add(candidate.bed_id)
        fix_room_sex(candidate, member.resident)
        internal_room_ids.add(candidate.room_id)
        room_categories[candidate.room_id].add(
            resident_categories.get(member.resident_id),
        )
        placements.append(ResolverPlacement(
            resident_id=member.resident_id,
            resident_stable_id=str(member.resident.stable_id),
            member_id=member.pk,
            physical_bed_id=candidate.bed_id,
            bed_stable_id=candidate.bed_stable_id,
            physical_room_id=candidate.room_id,
            action='SETTLE_NEW_BINDING',
            source_kind='official_equipment_assignment',
            work_shift=member.work_shift,
            shift_source_kind=member.shift_source_kind,
            shift_source_fingerprint=member.shift_source_fingerprint,
            binding_id=None,
            equipment_assignment_id=equipment_assignment.pk,
            calendar_slot_id=candidate.slot_id,
            anchor_id=candidate.anchor_id,
            anchor_bed_assignment_id=candidate.anchor_bed_assignment_id,
            profile_identifiers=(),
            rule_identifiers=('m6.official_equipment_assignment',),
            place_kind=_candidate_place_kind(candidate),
        ))

    candidate_map: dict[int, tuple[_SlotCandidate, ...]] = {}
    member_by_resident = {
        item.resident_id: item for item in remaining_unbound_members
    }
    for member in remaining_unbound_members:
        resident = member.resident
        base_available = [
            candidate
            for candidate in candidates.values()
            if candidate.bed_id not in claimed_bed_ids
            and candidate.slot_id not in binding_slot_ids
        ]
        if resident.is_external:
            if not external_room_ids:
                reject(member, REASON_RESOLVER_NOT_CONFIGURED)
                continue
            room_eligible = [
                candidate for candidate in base_available
                if candidate.room_id in external_room_ids
                and candidate.room_id not in internal_room_ids
            ]
            sex_eligible = [
                candidate for candidate in room_eligible
                if sex_rejection(candidate, resident, member) is None
            ]
            if room_eligible and not sex_eligible:
                reject(
                    member,
                    sex_rejection(room_eligible[0], resident, member),
                )
                continue
            organization = _normalize_organization(resident.organization)
            available = [
                candidate for candidate in sex_eligible
                if candidate.bed_id not in blocked_bed_ids
            ]
            available.sort(key=lambda candidate: (
                0 if organization in external_room_organizations[candidate.room_id] else 1,
                candidate.room_order,
                candidate.bed_stable_id,
            ))
        else:
            employee = resident.employee
            if not employee.personnel_position_id:
                reject(member, REASON_INCOMPLETE_CONTEXT)
                continue
            room_eligible = [
                candidate for candidate in base_available
                if candidate.personnel_position_id == employee.personnel_position_id
                and candidate.room_id not in external_room_ids
            ]
            sex_eligible = [
                candidate for candidate in room_eligible
                if sex_rejection(candidate, resident, member) is None
            ]
            if room_eligible and not sex_eligible:
                reject(
                    member,
                    sex_rejection(room_eligible[0], resident, member),
                )
                continue
            available = [
                candidate for candidate in sex_eligible
                if candidate.bed_id not in blocked_bed_ids
            ]
            available.sort(key=lambda candidate: (
                candidate.room_order,
                candidate.bed_stable_id,
            ))
        if not resident.is_external:
            category_eligible = []
            category_problem = None
            for candidate in available:
                problem = member_category_problem(candidate, member)
                if problem == MISSING_CATEGORY:
                    category_problem = MISSING_CATEGORY
                    break
                if problem is None:
                    category_eligible.append(candidate)
            if available and not category_eligible:
                reject(
                    member,
                    category_problem or ROOM_CATEGORY_CONFLICT,
                )
                continue
            available = category_eligible
        if not available:
            if sex_eligible:
                # Свободные по профилю/полу места есть, но все придержаны
                # подготовкой или резервом — называем фактическое правило.
                hold_reasons = {
                    reason
                    for candidate in sex_eligible
                    for reason in _blocked_bed_reasons(
                        candidate.bed_id,
                        prepared_bed_ids,
                        reserved_plan_bed_ids,
                    )
                }
                if hold_reasons:
                    reject(member, *sorted(hold_reasons))
                    continue
            reject(
                member,
                REASON_NO_PLACE if candidates else REASON_RESOLVER_NOT_CONFIGURED,
            )
            continue
        candidate_map[resident.pk] = tuple(available)

    scarce_resident_ids: set[int] = set()
    category_conflict_resident_ids: set[int] = set()
    assigned_candidates: dict[int, _SlotCandidate] = {}
    # Одна комната — один пол: группы делятся по полу и обрабатываются
    # раздельно, женская первой (ADR-023). Комнаты, занятые первым жильцом,
    # закрываются для другого пола до конца прогона (room_sex_owner).
    grouped_ids: dict[str, list[int]] = {FEMALE: [], MALE: []}
    for resident_id in candidate_map:
        sex = resident_sex(member_by_resident[resident_id].resident)
        if sex in grouped_ids:
            grouped_ids[sex].append(resident_id)
        else:
            reject(member_by_resident[resident_id], REASON_RESIDENT_SEX_UNKNOWN)
    for group_sex in (FEMALE, MALE):
        group_map: dict[int, tuple[_SlotCandidate, ...]] = {}
        for resident_id in grouped_ids[group_sex]:
            member = member_by_resident[resident_id]
            filtered = tuple(
                candidate
                for candidate in candidate_map[resident_id]
                if sex_rejection(candidate, member.resident, member) is None
            )
            if not filtered:
                owner_conflict = any(
                    room_sex_owner.get(candidate.room_id) not in (None, group_sex)
                    for candidate in candidate_map[resident_id]
                )
                if owner_conflict:
                    reject(member, REASON_ROOM_SEX_TAKEN)
                else:
                    reject(
                        member,
                        sex_rejection(
                            candidate_map[resident_id][0],
                            member.resident,
                            member,
                        ),
                    )
                continue
            group_map[resident_id] = filtered
        for resident_ids, _bed_ids in _connected_scarcity_groups(group_map):
            ordered_residents = sorted(
                resident_ids,
                key=lambda value: str(member_by_resident[value].resident.stable_id),
            )
            group_assignment = _deterministic_group_matching(
                ordered_residents,
                group_map,
            )
            if len(group_assignment) == len(ordered_residents):
                # Пост-проверка однородности категорий по комнатам: матчер
                # распределяет койки, поэтому двух разных категорий в одной
                # комнате он сам не заметит (решение владельца 22.11.2026).
                room_category_sets: dict[int, set[str]] = defaultdict(set)
                for resident_id, assigned in group_assignment.items():
                    if member_by_resident[resident_id].resident.is_external:
                        continue
                    room_category_sets[assigned.room_id].add(
                        resident_categories.get(resident_id),
                    )
                conflicting_resident_ids = {
                    resident_id
                    for resident_id, assigned in group_assignment.items()
                    if len(room_category_sets[assigned.room_id]) > 1
                }
                if conflicting_resident_ids:
                    for resident_id in conflicting_resident_ids:
                        group_assignment.pop(resident_id, None)
                    scarce_resident_ids.update(conflicting_resident_ids)
                    category_conflict_resident_ids.update(
                        conflicting_resident_ids,
                    )
                assigned_candidates.update(group_assignment)
                for assigned in group_assignment.values():
                    room_sex_owner.setdefault(assigned.room_id, group_sex)
            else:
                scarce_resident_ids.update(resident_ids)
    for resident_id in scarce_resident_ids:
        reject(
            member_by_resident[resident_id],
            ROOM_CATEGORY_CONFLICT
            if resident_id in category_conflict_resident_ids
            else REASON_EQUAL_PRIORITY,
        )

    for resident_id in sorted(
        set(candidate_map) - scarce_resident_ids,
        key=lambda value: str(member_by_resident[value].resident.stable_id),
    ):
        member = member_by_resident[resident_id]
        resident = member.resident
        chosen = assigned_candidates.get(resident_id)
        if chosen is None:
            reject(member, REASON_EQUAL_PRIORITY)
            continue
        claimed_bed_ids.add(chosen.bed_id)
        fix_room_sex(chosen, resident)
        if resident.is_external:
            external_room_ids.add(chosen.room_id)
            external_room_organizations[chosen.room_id].add(
                _normalize_organization(resident.organization),
            )
            source_kind = 'external_residual_pool'
            rules = ('m6.external_residual', 'm6.external_organization_soft_preference')
        else:
            internal_room_ids.add(chosen.room_id)
            source_kind = 'official_position_anchor'
            rules = ('m6.official_personnel_position',)
        placements.append(ResolverPlacement(
            resident_id=resident.pk,
            resident_stable_id=str(resident.stable_id),
            member_id=member.pk,
            physical_bed_id=chosen.bed_id,
            bed_stable_id=chosen.bed_stable_id,
            physical_room_id=chosen.room_id,
            action='SETTLE_NEW_BINDING',
            source_kind=source_kind,
            work_shift=member.work_shift,
            shift_source_kind=member.shift_source_kind,
            shift_source_fingerprint=member.shift_source_fingerprint,
            binding_id=None,
            equipment_assignment_id=None,
            calendar_slot_id=chosen.slot_id,
            anchor_id=chosen.anchor_id,
            anchor_bed_assignment_id=chosen.anchor_bed_assignment_id,
            profile_identifiers=(f'computed-external-room:{chosen.room_id}',)
            if resident.is_external else (),
            rule_identifiers=rules,
            place_kind=_candidate_place_kind(chosen),
        ))

    placements_tuple = tuple(sorted(placements, key=lambda item: item.resident_stable_id))
    unresolved_tuple = tuple(sorted(unresolved.values(), key=lambda item: item.resident_stable_id))
    reason_codes = tuple(sorted({
        reason
        for item in unresolved_tuple
        for reason in item.reason_codes
    }))
    return SettlementResolverResult(
        cohort_id=cohort.pk,
        cohort_stable_id=str(cohort.stable_id),
        watch_period_id=cohort.watch_period_id,
        placements=placements_tuple,
        unresolved=unresolved_tuple,
        reason_codes=reason_codes,
        source_identifiers=source_identifiers,
        input_fingerprint=fingerprint,
    )
