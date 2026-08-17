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

from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    AccommodationAnchorCalendarSlot,
    EmployeeAccommodationBinding,
    EmployeeBedOccupancy,
    PhysicalRoom,
    SettlementCohort,
    SettlementCohortMember,
    SettlementResident,
)
from .residents import authoritative_resident_sex


REASON_COHORT_NOT_APPROVED = 'cohort_not_approved'
REASON_RESOLVER_NOT_CONFIGURED = 'resolver_not_configured'
REASON_RESIDENT_INACTIVE = 'resident_inactive'
REASON_INCOMPLETE_CONTEXT = 'incomplete_authoritative_context'
REASON_STALE_CALENDAR = 'stale_calendar_relation'
REASON_INVALID_BINDING = 'invalid_existing_binding'
REASON_NO_PLACE = 'no_compatible_place'
REASON_EQUAL_PRIORITY = 'equal_priority_conflict'
REASON_HARD_CONFLICT = 'hard_rule_conflict'


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
    binding_id: int | None
    equipment_assignment_id: int | None
    calendar_slot_id: int
    anchor_id: int
    anchor_bed_assignment_id: int
    profile_identifiers: tuple[str, ...] = ()
    rule_identifiers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolverUnresolved:
    resident_id: int
    resident_stable_id: str
    member_id: int
    reason_codes: tuple[str, ...]


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
    anchor_bed_assignment_id: int
    bed_id: int
    bed_stable_id: str
    room_id: int
    room_order: tuple[str, int, str, int, int]
    room_sex_restriction: str


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


def _effective_equipment_assignments(*, employee_ids, effective_date):
    """Mirror the accepted production criterion used by legacy preview."""

    return list(
        EquipmentAssignment.objects.filter(
            employee_id__in=employee_ids,
            status=AssignmentStatus.ACCEPTED,
            role__isnull=False,
            shift__isnull=True,
            assigned_at__lte=effective_date,
        )
        .filter(Q(accepted_at__isnull=True) | Q(accepted_at__lte=effective_date))
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=effective_date))
        .select_related('equipment__equipment_type', 'role')
        .order_by('employee_id', 'equipment_id', 'shift_type', 'pk')
    )


def _overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a


def _slot_candidates(cohort: SettlementCohort) -> tuple[dict[int, _SlotCandidate], list[dict]]:
    slots = list(
        AccommodationAnchorCalendarSlot.objects.filter(
            watch_period_id=cohort.watch_period_id,
            status=AccommodationAnchorCalendarSlot.Status.CONFIRMED,
        )
        .select_related('anchor', 'watch_period')
        .order_by('pk')
    )
    assignments = list(
        AccommodationAnchorBedAssignment.objects.filter(
            anchor_id__in=[slot.anchor_id for slot in slots],
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
        )
        .select_related('physical_bed__room__dormitory')
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
                'ended_revision_id': item.ended_revision_id,
            }
            for item in matching
        ]
        snapshot.append({
            'slot_id': slot.pk,
            'slot_stable_id': str(slot.stable_id),
            'anchor_id': slot.anchor_id,
            'status': slot.status,
            'valid_from': slot.valid_from.isoformat(),
            'valid_to': slot.valid_to.isoformat(),
            'calendar_stale': slot.calendar_relation_is_stale,
            'assignments': assignment_snapshot,
        })
        if (
            slot.calendar_relation_is_stale
            or slot.anchor.status != AccommodationAnchor.Status.ACTIVE
            or len(matching) != 1
        ):
            continue
        assignment = matching[0]
        bed = assignment.physical_bed
        room = bed.room
        if room.transfer_status != PhysicalRoom.TransferStatus.TRANSFERRED:
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
            anchor_bed_assignment_id=assignment.pk,
            bed_id=bed.pk,
            bed_stable_id=bed.stable_id,
            room_id=room.pk,
            room_order=(
                str(room.dormitory.number),
                room.floor,
                room.corridor_side,
                room.side_position,
                room.number,
            ),
            room_sex_restriction=room.sex_restriction,
        )
    return candidates, snapshot


def _resident_authoritative_sex(resident):
    try:
        return authoritative_resident_sex(resident)
    except ValidationError:
        return None


def _room_accepts_resident(candidate: _SlotCandidate, resident) -> bool:
    restriction = candidate.room_sex_restriction
    if restriction == PhysicalRoom.SexRestriction.UNKNOWN:
        return True
    sex = _resident_authoritative_sex(resident)
    if sex not in {'male', 'female'}:
        return False
    return (
        restriction == PhysicalRoom.SexRestriction.MALE_ONLY
        and sex == 'male'
    ) or (
        restriction == PhysicalRoom.SexRestriction.FEMALE_ONLY
        and sex == 'female'
    )


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


def resolve_settlement_cohort(*, cohort_id: int) -> SettlementResolverResult:
    """Return a deterministic proposal without writing any database row."""

    cohort = (
        SettlementCohort.objects.select_related(
            'watch_period', 'watch_composition', 'source_revision',
        )
        .get(pk=cohort_id)
    )
    members = list(
        SettlementCohortMember.objects.filter(cohort_id=cohort.pk)
        .select_related('resident__employee', 'source_revision')
        .order_by('resident__stable_id', 'pk')
    )
    active_members = [item for item in members if item.participates_in_accommodation]
    candidates, slot_snapshot = _slot_candidates(cohort)
    starts_at, ends_at = _period_datetimes(cohort)
    internal_employee_ids = {
        item.resident.employee_id
        for item in active_members
        if item.resident.employee_id
    }
    effective_equipment_assignments = _effective_equipment_assignments(
        employee_ids=internal_employee_ids,
        effective_date=starts_at,
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
        .select_related('resident', 'anchor_calendar_slot__anchor')
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

    occupancy_rows = list(
        EmployeeBedOccupancy.objects.filter(
            starts_at__lt=ends_at,
        )
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=starts_at))
        .filter(Q(terminated_at__isnull=True) | Q(terminated_at__gt=starts_at))
        .select_related('resident__employee', 'physical_bed__room')
        .order_by('pk')
    )
    occupied_bed_ids = {item.physical_bed_id for item in occupancy_rows}
    internal_room_ids = {
        item.physical_bed.room_id
        for item in occupancy_rows
        if not item.resident.is_external
    }

    external_room_organizations: dict[int, set[str]] = defaultdict(set)
    external_room_ids: set[int] = {
        item.physical_bed.room_id
        for item in occupancy_rows
        if item.resident.is_external
    }
    for occupancy in occupancy_rows:
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

    snapshot = {
        'resolver_version': 'm6-equipment-routing-v1',
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
                'employee_composition_id': (
                    item.resident.employee.watch_composition_id
                    if item.resident.employee_id else None
                ),
                'arrival_at': item.arrival_at.isoformat(),
                'departure_at': item.departure_at.isoformat(),
                'participation_status': item.participation_status,
                'source_revision_id': item.source_revision_id,
                'basis_type': item.basis_type,
                'basis_id': item.basis_id,
                'basis_snapshot': item.basis_snapshot,
                'production_context_snapshot': item.production_context_snapshot,
            }
            for item in members
        ],
        'slots': slot_snapshot,
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
                'resident_id': item.resident_id,
                'resident_revision': item.resident.revision,
                'bed_id': item.physical_bed_id,
                'starts_at': item.starts_at.isoformat(),
                'ends_at': item.ends_at.isoformat() if item.ends_at else None,
                'terminated_at': (
                    item.terminated_at.isoformat() if item.terminated_at else None
                ),
            }
            for item in occupancy_rows
        ],
    }
    fingerprint = _canonical_hash(snapshot)
    source_identifiers = tuple(sorted({
        'resolver:m6-equipment-routing-v1',
        f'cohort:{cohort.stable_id}',
        f'cohort-source-revision:{cohort.source_revision_id}',
        *(
            f'member-source-revision:{item.source_revision_id}'
            for item in members
        ),
        *(
            f'binding:{item.stable_id}'
            for item in all_period_bindings
        ),
        *(
            f'equipment-assignment:{item.pk}'
            for item in effective_equipment_assignments
        ),
    }))

    unresolved: dict[int, ResolverUnresolved] = {}
    placements: list[ResolverPlacement] = []
    reserved_bed_ids = set(occupied_bed_ids)

    def reject(member, *reasons):
        unresolved[member.resident_id] = ResolverUnresolved(
            resident_id=member.resident_id,
            resident_stable_id=str(member.resident.stable_id),
            member_id=member.pk,
            reason_codes=tuple(sorted(set(reasons))),
        )

    cohort_failure_reason = None
    if cohort.status != SettlementCohort.Status.APPROVED:
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

    unbound_members = []
    for member in active_members:
        resident = member.resident
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
        if resident.employee_id:
            employee = resident.employee
            if (
                not employee.is_active
                or employee.status != employee.Status.ACTIVE
            ):
                reject(member, REASON_RESIDENT_INACTIVE)
                continue
            if employee.watch_composition_id != cohort.watch_composition_id:
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
        invalid_reason = None
        if slot.calendar_relation_is_stale:
            invalid_reason = REASON_STALE_CALENDAR
        elif (
            slot.watch_period_id != cohort.watch_period_id
            or slot.watch_composition_id != cohort.watch_composition_id
            or binding.valid_from > cohort.watch_period.starts_on
            or binding.valid_to < cohort.watch_period.ends_on
        ):
            invalid_reason = REASON_INVALID_BINDING
        elif candidate is None:
            invalid_reason = REASON_INVALID_BINDING
        elif candidate.bed_id in reserved_bed_ids:
            invalid_reason = REASON_HARD_CONFLICT
        elif resident.is_external and candidate.room_id in internal_room_ids:
            invalid_reason = REASON_HARD_CONFLICT
        elif not resident.is_external and candidate.room_id in external_room_ids:
            invalid_reason = REASON_HARD_CONFLICT
        elif not _room_accepts_resident(candidate, resident):
            invalid_reason = REASON_HARD_CONFLICT
        if invalid_reason:
            reject(member, invalid_reason)
            continue

        reserved_bed_ids.add(candidate.bed_id)
        if resident.is_external:
            external_room_ids.add(candidate.room_id)
            external_room_organizations[candidate.room_id].add(
                _normalize_organization(resident.organization),
            )
        else:
            internal_room_ids.add(candidate.room_id)
        placements.append(ResolverPlacement(
            resident_id=resident.pk,
            resident_stable_id=str(resident.stable_id),
            member_id=member.pk,
            physical_bed_id=candidate.bed_id,
            bed_stable_id=candidate.bed_stable_id,
            physical_room_id=candidate.room_id,
            action='SETTLE',
            source_kind='confirmed_binding',
            binding_id=binding.pk,
            equipment_assignment_id=None,
            calendar_slot_id=candidate.slot_id,
            anchor_id=candidate.anchor_id,
            anchor_bed_assignment_id=candidate.anchor_bed_assignment_id,
            profile_identifiers=(),
            rule_identifiers=('m4.confirmed_binding',),
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
            if not anchor_assignment.physical_bed.is_available:
                reject(member, REASON_HARD_CONFLICT)
            else:
                reject(member, REASON_RESOLVER_NOT_CONFIGURED)
            continue
        if candidate.anchor_bed_assignment_id != anchor_assignment.pk:
            reject(member, REASON_INCOMPLETE_CONTEXT)
            continue
        if (
            candidate.room_id in external_room_ids
            or not _room_accepts_resident(candidate, resident)
        ):
            reject(member, REASON_HARD_CONFLICT)
            continue
        if candidate.bed_id in reserved_bed_ids:
            reject(member, REASON_HARD_CONFLICT)
            continue
        equipment_proposals[candidate.bed_id].append(
            (member, equipment_assignment, candidate),
        )

    for bed_id in sorted(equipment_proposals):
        proposals = equipment_proposals[bed_id]
        if len(proposals) != 1:
            for member, _equipment_assignment, _candidate in proposals:
                reject(member, REASON_HARD_CONFLICT)
            continue
        member, equipment_assignment, candidate = proposals[0]
        reserved_bed_ids.add(candidate.bed_id)
        internal_room_ids.add(candidate.room_id)
        placements.append(ResolverPlacement(
            resident_id=member.resident_id,
            resident_stable_id=str(member.resident.stable_id),
            member_id=member.pk,
            physical_bed_id=candidate.bed_id,
            bed_stable_id=candidate.bed_stable_id,
            physical_room_id=candidate.room_id,
            action='SETTLE_NEW_BINDING',
            source_kind='official_equipment_assignment',
            binding_id=None,
            equipment_assignment_id=equipment_assignment.pk,
            calendar_slot_id=candidate.slot_id,
            anchor_id=candidate.anchor_id,
            anchor_bed_assignment_id=candidate.anchor_bed_assignment_id,
            profile_identifiers=(),
            rule_identifiers=('m6.official_equipment_assignment',),
        ))

    candidate_map: dict[int, tuple[_SlotCandidate, ...]] = {}
    member_by_resident = {
        item.resident_id: item for item in remaining_unbound_members
    }
    for member in remaining_unbound_members:
        resident = member.resident
        available = [
            candidate
            for candidate in candidates.values()
            if candidate.bed_id not in reserved_bed_ids
            and candidate.slot_id not in binding_slot_ids
        ]
        if resident.is_external:
            available = [
                candidate for candidate in available
                if candidate.room_id in external_room_ids
                and candidate.room_id not in internal_room_ids
                and _room_accepts_resident(candidate, resident)
            ]
            if not external_room_ids:
                reject(member, REASON_RESOLVER_NOT_CONFIGURED)
                continue
            organization = _normalize_organization(resident.organization)
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
            available = [
                candidate for candidate in available
                if candidate.personnel_position_id == employee.personnel_position_id
                and candidate.room_id not in external_room_ids
                and _room_accepts_resident(candidate, resident)
            ]
            available.sort(key=lambda candidate: (
                candidate.room_order,
                candidate.bed_stable_id,
            ))
        if not available:
            reject(
                member,
                REASON_NO_PLACE if candidates else REASON_RESOLVER_NOT_CONFIGURED,
            )
            continue
        candidate_map[resident.pk] = tuple(available)

    scarce_resident_ids: set[int] = set()
    assigned_candidates: dict[int, _SlotCandidate] = {}
    for resident_ids, _bed_ids in _connected_scarcity_groups(candidate_map):
        ordered_residents = sorted(
            resident_ids,
            key=lambda value: str(member_by_resident[value].resident.stable_id),
        )
        group_assignment: dict[int, _SlotCandidate] = {}
        used_beds: set[int] = set()

        def assign(index):
            if index == len(ordered_residents):
                return True
            resident_id = ordered_residents[index]
            for candidate in candidate_map[resident_id]:
                if candidate.bed_id in used_beds:
                    continue
                used_beds.add(candidate.bed_id)
                group_assignment[resident_id] = candidate
                if assign(index + 1):
                    return True
                group_assignment.pop(resident_id, None)
                used_beds.remove(candidate.bed_id)
            return False

        if assign(0):
            assigned_candidates.update(group_assignment)
        else:
            scarce_resident_ids.update(resident_ids)
    for resident_id in scarce_resident_ids:
        reject(member_by_resident[resident_id], REASON_EQUAL_PRIORITY)

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
        reserved_bed_ids.add(chosen.bed_id)
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
            binding_id=None,
            equipment_assignment_id=None,
            calendar_slot_id=chosen.slot_id,
            anchor_id=chosen.anchor_id,
            anchor_bed_assignment_id=chosen.anchor_bed_assignment_id,
            profile_identifiers=(f'computed-external-room:{chosen.room_id}',)
            if resident.is_external else (),
            rule_identifiers=rules,
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
