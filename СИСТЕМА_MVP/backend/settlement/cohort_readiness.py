"""Read-only readiness projections for arrival-roster routing batches."""

from collections import Counter
from dataclasses import dataclass
import hashlib
import json

from django.core.exceptions import ValidationError

from rotations.arrival_roster_routing import (
    BATCH_STATE_CURRENT,
    ERROR_BATCH_INCONSISTENT,
    ERROR_BATCH_NOT_FOUND,
    EVIDENCE_NOT_ARRIVING,
    EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED,
    EVIDENCE_SENT_TO_CLERK,
    resolve_arrival_roster_routing_evidence,
)
from rotations.employee_watch_profile_changes import (
    ERROR_BRIGADE_NOT_ALLOWED as PROFILE_ERROR_BRIGADE_NOT_ALLOWED,
    ERROR_BRIGADE_OUT_OF_RANGE as PROFILE_ERROR_BRIGADE_OUT_OF_RANGE,
    ERROR_BRIGADE_REQUIRED as PROFILE_ERROR_BRIGADE_REQUIRED,
    ERROR_EMPLOYEE_INACTIVE as PROFILE_ERROR_EMPLOYEE_INACTIVE,
    ERROR_EMPLOYEE_NOT_FOUND as PROFILE_ERROR_EMPLOYEE_NOT_FOUND,
    ERROR_PROFILE_INCONSISTENT as PROFILE_ERROR_PROFILE_INCONSISTENT,
    ERROR_SOURCE_FINGERPRINT_INVALID as PROFILE_ERROR_SOURCE_FINGERPRINT_INVALID,
    ERROR_SOURCE_INVALID as PROFILE_ERROR_SOURCE_INVALID,
    ERROR_WATCH_COMPOSITION_INACTIVE as PROFILE_ERROR_WATCH_COMPOSITION_INACTIVE,
    ERROR_WATCH_COMPOSITION_NOT_FOUND as PROFILE_ERROR_WATCH_COMPOSITION_NOT_FOUND,
    ERROR_WATCH_PERIOD_NOT_FOUND as PROFILE_ERROR_WATCH_PERIOD_NOT_FOUND,
    ERROR_WORK_SCHEDULE_INACTIVE as PROFILE_ERROR_WORK_SCHEDULE_INACTIVE,
    ERROR_WORK_SCHEDULE_NOT_FOUND as PROFILE_ERROR_WORK_SCHEDULE_NOT_FOUND,
    SOURCE_KIND_APPLIED_CHANGE,
    SOURCE_KIND_LEGACY_BASELINE,
    EmployeeWatchProfileChangeError,
    resolve_employee_watch_profile,
)
from rotations.models import ArrivalRosterRoutingBatch, ArrivalRosterVersion
from shifts.brigade_phase_calendar import (
    ERROR_BRIGADE_NOT_FOUND,
    ERROR_CONFIRMED_VERSION_INCONSISTENT,
    ERROR_CONFIRMED_VERSION_NOT_FOUND,
    ERROR_GRAPH_INCOMPLETE,
    ERROR_GRAPH_INCONSISTENT,
    ERROR_POLICY_MISMATCH,
    ERROR_POLICY_NOT_DEFINED,
    ERROR_SCHEDULE_DESIGNATION_MISMATCH,
    ERROR_SOURCE_FINGERPRINT_INVALID,
    ERROR_SOURCE_INVALID,
    ERROR_WATCH_PERIOD_NOT_FOUND,
    ERROR_WORK_SCHEDULE_NOT_FOUND,
    BrigadePhaseCalendarError,
    resolve_confirmed_brigade_phase,
)
from shifts.models import WatchPeriod

from .models import SettlementCohort, SettlementCohortMember


ERROR_EMPLOYEE_NOT_FOUND = 'employee_not_found'
ERROR_EMPLOYEE_INACTIVE = 'employee_inactive'
ERROR_EMPLOYEE_SCHEDULE_MISSING = 'employee_schedule_missing'
ERROR_EMPLOYEE_BRIGADE_MISSING = 'employee_brigade_missing'
ERROR_EMPLOYEE_WATCH_COMPOSITION_MISSING = 'employee_watch_composition_missing'
ERROR_EMPLOYEE_WATCH_COMPOSITION_MISMATCH = 'employee_watch_composition_mismatch'
ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT = 'employee_watch_profile_inconsistent'
ERROR_CALENDAR_NOT_CONFIRMED = 'calendar_not_confirmed'
ERROR_CALENDAR_INCONSISTENT = 'calendar_inconsistent'
ERROR_CALENDAR_POLICY_MISSING = 'calendar_policy_missing'
ERROR_BRIGADE_PHASE_MISSING = 'brigade_phase_missing'
ERROR_BRIGADE_OFF_BUT_ARRIVING = 'brigade_off_but_arriving'
ERROR_EXTERNAL_SHIFT_UNRESOLVED = 'external_shift_unresolved'
ERROR_ASSIGNMENT_PHASE_MISMATCH = 'assignment_phase_mismatch'
ERROR_DUPLICATE_RESIDENT = 'duplicate_resident'
ERROR_DUPLICATE_ROUTING_ROW = 'duplicate_routing_row'
ERROR_NO_ARRIVING_MEMBERS = 'no_arriving_members'
ERROR_ROUTING_INCONSISTENT = 'routing_inconsistent'

_ROUTING_SOURCE_TYPE = 'arrival_roster_routing'
_ROUTING_BASIS_TYPE = 'arrival_roster_routing_row'
_INCONSISTENT_COHORT_MESSAGE = (
    'Связанный состав расселения повреждён или имеет неожиданное состояние.'
)

_PHASE_DAY = 'day'
_PHASE_NIGHT = 'night'
_PHASE_OFF = 'off'
_WORK_SHIFTS = frozenset({_PHASE_DAY, _PHASE_NIGHT})

_SAFE_MESSAGES = {
    ERROR_BATCH_NOT_FOUND: 'Передача утверждённого реестра не найдена.',
    'batch_stale': 'Передача устарела: действует другая версия реестра.',
    ERROR_BATCH_INCONSISTENT: 'Связь передачи с утверждённым реестром нарушена.',
    'routing_pending': 'Передача строки ещё не завершена.',
    'routing_requires_review': 'Строка требует проверки табельщиком.',
    'routing_stale': 'Передача строки устарела.',
    ERROR_ROUTING_INCONSISTENT: 'История передачи строки несогласована.',
    'official_assignment_missing': 'Официальное назначение техники и смены ещё не опубликовано.',
    'official_assignment_inconsistent': 'Официальное назначение техники и смены несогласовано.',
    'unknown_route_state': 'Маршрут строки не определён.',
    ERROR_EMPLOYEE_NOT_FOUND: 'Карточка сотрудника не найдена.',
    ERROR_EMPLOYEE_INACTIVE: 'Сотрудник не является действующим.',
    ERROR_EMPLOYEE_SCHEDULE_MISSING: 'В карточке сотрудника не указан график работы.',
    ERROR_EMPLOYEE_BRIGADE_MISSING: 'В карточке сотрудника не указана действующая бригада.',
    ERROR_EMPLOYEE_WATCH_COMPOSITION_MISSING: 'Для сотрудника не определён состав вахты.',
    ERROR_EMPLOYEE_WATCH_COMPOSITION_MISMATCH: (
        'Сотрудник относится к другому составу вахты.'
    ),
    ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT: (
        'История графика, бригады и состава вахты сотрудника несогласована.'
    ),
    ERROR_CALENDAR_NOT_CONFIRMED: 'Для графика и периода нет утверждённого календаря фаз.',
    ERROR_CALENDAR_INCONSISTENT: 'Утверждённый календарь фаз повреждён или несогласован.',
    ERROR_CALENDAR_POLICY_MISSING: 'Для графика не определено правило календарных фаз.',
    ERROR_BRIGADE_PHASE_MISSING: 'Фаза указанной бригады не найдена.',
    ERROR_BRIGADE_OFF_BUT_ARRIVING: 'Сотрудник отмечен прибывающим, но его бригада находится на межвахте.',
    ERROR_EXTERNAL_SHIFT_UNRESOLVED: 'Для внешнего жильца смена не определяется автоматически.',
    ERROR_ASSIGNMENT_PHASE_MISMATCH: 'Производственное назначение не совпадает с календарной фазой бригады.',
    ERROR_DUPLICATE_RESIDENT: 'Один жилец присутствует в передаче более одного раза.',
    ERROR_DUPLICATE_ROUTING_ROW: 'Строка передачи повторяется в структурированном результате.',
    ERROR_NO_ARRIVING_MEMBERS: 'В передаче нет прибывающих участников для формирования состава.',
}

_CALENDAR_ERROR_CODES = {
    ERROR_CONFIRMED_VERSION_NOT_FOUND: ERROR_CALENDAR_NOT_CONFIRMED,
    ERROR_POLICY_NOT_DEFINED: ERROR_CALENDAR_POLICY_MISSING,
    ERROR_BRIGADE_NOT_FOUND: ERROR_BRIGADE_PHASE_MISSING,
    ERROR_CONFIRMED_VERSION_INCONSISTENT: ERROR_CALENDAR_INCONSISTENT,
    ERROR_SOURCE_INVALID: ERROR_CALENDAR_INCONSISTENT,
    ERROR_SOURCE_FINGERPRINT_INVALID: ERROR_CALENDAR_INCONSISTENT,
    ERROR_GRAPH_INCOMPLETE: ERROR_CALENDAR_INCONSISTENT,
    ERROR_GRAPH_INCONSISTENT: ERROR_CALENDAR_INCONSISTENT,
    ERROR_SCHEDULE_DESIGNATION_MISMATCH: ERROR_CALENDAR_INCONSISTENT,
    ERROR_POLICY_MISMATCH: ERROR_CALENDAR_INCONSISTENT,
    ERROR_WATCH_PERIOD_NOT_FOUND: ERROR_CALENDAR_INCONSISTENT,
    ERROR_WORK_SCHEDULE_NOT_FOUND: ERROR_CALENDAR_INCONSISTENT,
}

_PROFILE_ERROR_CODES = {
    PROFILE_ERROR_EMPLOYEE_NOT_FOUND: ERROR_EMPLOYEE_NOT_FOUND,
    PROFILE_ERROR_EMPLOYEE_INACTIVE: ERROR_EMPLOYEE_INACTIVE,
    PROFILE_ERROR_WORK_SCHEDULE_NOT_FOUND: ERROR_EMPLOYEE_SCHEDULE_MISSING,
    PROFILE_ERROR_WORK_SCHEDULE_INACTIVE: ERROR_EMPLOYEE_SCHEDULE_MISSING,
    PROFILE_ERROR_BRIGADE_REQUIRED: ERROR_EMPLOYEE_BRIGADE_MISSING,
    PROFILE_ERROR_BRIGADE_NOT_ALLOWED: ERROR_EMPLOYEE_SCHEDULE_MISSING,
    PROFILE_ERROR_BRIGADE_OUT_OF_RANGE: ERROR_EMPLOYEE_BRIGADE_MISSING,
    PROFILE_ERROR_WATCH_COMPOSITION_NOT_FOUND: (
        ERROR_EMPLOYEE_WATCH_COMPOSITION_MISSING
    ),
    PROFILE_ERROR_WATCH_COMPOSITION_INACTIVE: (
        ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT
    ),
    PROFILE_ERROR_WATCH_PERIOD_NOT_FOUND: ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT,
    PROFILE_ERROR_SOURCE_INVALID: ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT,
    PROFILE_ERROR_SOURCE_FINGERPRINT_INVALID: (
        ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT
    ),
    PROFILE_ERROR_PROFILE_INCONSISTENT: ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT,
}


class SettlementCohortReadinessError(ValidationError):
    """Controlled failure before a structured readiness result can be built."""


@dataclass(frozen=True, slots=True)
class CohortReadyMember:
    routing_row_id: int
    routing_event_id: int
    brigade_phase_row_id: int
    resident_id: int
    employee_id: int
    work_shift: str
    equipment_assignment_id: int | None
    crew_plan_slot_id: int | None
    watch_profile_source_kind: str
    employee_watch_profile_change_id: int | None
    watch_profile_work_schedule_id: int | None
    watch_profile_brigade_number: int | None
    watch_profile_watch_composition_id: int | None
    watch_profile_fingerprint: str


@dataclass(frozen=True, slots=True)
class CohortReadinessBlocker:
    routing_row_id: int | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SettlementCohortReadiness:
    batch_id: int
    watch_period_id: int
    ready_members: tuple[CohortReadyMember, ...]
    blockers: tuple[CohortReadinessBlocker, ...]
    excluded_not_arriving_row_ids: tuple[int, ...]
    is_ready: bool


@dataclass(frozen=True, slots=True)
class SettlementCohortBatchOverview:
    batch_id: int
    watch_period_name: str
    watch_period_dates: str
    ready_member_count: int
    excluded_not_arriving_count: int
    blocker_messages: tuple[str, ...]
    can_create: bool
    is_created: bool
    approved_at: object | None
    approved_member_count: int


@dataclass(frozen=True, slots=True)
class SettlementCohortOverview:
    batches: tuple[SettlementCohortBatchOverview, ...]


def _blocker(*, routing_row_id, code):
    return CohortReadinessBlocker(
        routing_row_id=routing_row_id,
        code=code,
        message=_SAFE_MESSAGES.get(code, _SAFE_MESSAGES[ERROR_ROUTING_INCONSISTENT]),
    )


def _calendar_blocker_code(error):
    return _CALENDAR_ERROR_CODES.get(
        getattr(error, 'code', None),
        ERROR_CALENDAR_INCONSISTENT,
    )


def _profile_blocker_code(error):
    return _PROFILE_ERROR_CODES.get(
        getattr(error, 'code', None),
        ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT,
    )


def _positive_identifier(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_profile_fingerprint(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in '0123456789abcdef' for character in value)
    )


def _profile_for_evidence(
    evidence,
    *,
    watch_period_id,
    watch_period_starts_on,
    watch_composition_id,
):
    if evidence.employee_id is None:
        return None, ERROR_EXTERNAL_SHIFT_UNRESOLVED
    try:
        profile = resolve_employee_watch_profile(
            employee_id=evidence.employee_id,
            watch_period_id=watch_period_id,
        )
    except EmployeeWatchProfileChangeError as error:
        return None, _profile_blocker_code(error)
    if (
        profile.employee_id != evidence.employee_id
        or profile.watch_period_id != watch_period_id
        or profile.effective_on != watch_period_starts_on
        or profile.source_kind not in {
            SOURCE_KIND_LEGACY_BASELINE,
            SOURCE_KIND_APPLIED_CHANGE,
        }
        or not _valid_profile_fingerprint(profile.profile_fingerprint)
        or (
            profile.source_kind == SOURCE_KIND_LEGACY_BASELINE
            and profile.change_id is not None
        )
        or (
            profile.source_kind == SOURCE_KIND_APPLIED_CHANGE
            and not _positive_identifier(profile.change_id)
        )
    ):
        return None, ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT
    if profile.work_schedule_id is None:
        return None, ERROR_EMPLOYEE_SCHEDULE_MISSING
    if not _positive_identifier(profile.work_schedule_id):
        return None, ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT
    if (
        isinstance(profile.brigade_number, bool)
        or not isinstance(profile.brigade_number, int)
        or profile.brigade_number < 1
    ):
        return None, ERROR_EMPLOYEE_BRIGADE_MISSING
    if profile.watch_composition_id is None:
        return None, ERROR_EMPLOYEE_WATCH_COMPOSITION_MISSING
    if not _positive_identifier(profile.watch_composition_id):
        return None, ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT
    if profile.watch_composition_id != watch_composition_id:
        return None, ERROR_EMPLOYEE_WATCH_COMPOSITION_MISMATCH
    return profile, None


def _phase_for_profile(*, profile, watch_period_id):
    try:
        return resolve_confirmed_brigade_phase(
            watch_period_id=watch_period_id,
            work_schedule_id=profile.work_schedule_id,
            brigade_number=profile.brigade_number,
        ), None
    except BrigadePhaseCalendarError as error:
        return None, _calendar_blocker_code(error)


def _ready_evidence_shape_error(evidence):
    if evidence.evidence_state == EVIDENCE_SENT_TO_CLERK:
        if (
            evidence.route_state != 'to_clerk'
            or evidence.latest_event_type != EVIDENCE_SENT_TO_CLERK
            or evidence.crew_plan_slot_id is not None
            or evidence.equipment_assignment_id is not None
            or evidence.assignment_shift_type is not None
        ):
            return ERROR_ROUTING_INCONSISTENT
        return None
    if evidence.evidence_state == EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED:
        if (
            evidence.route_state != 'to_deputy'
            or evidence.latest_event_type != EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED
            or evidence.crew_plan_slot_id is None
            or evidence.equipment_assignment_id is None
            or evidence.assignment_shift_type not in _WORK_SHIFTS
        ):
            return ERROR_ROUTING_INCONSISTENT
        return None
    return evidence.blocker_code or ERROR_ROUTING_INCONSISTENT


def _ready_member(*, evidence, phase, profile):
    if evidence.latest_event_id is None or not evidence.participating:
        return None, ERROR_ROUTING_INCONSISTENT
    if phase.phase == _PHASE_OFF:
        return None, ERROR_BRIGADE_OFF_BUT_ARRIVING
    if phase.phase not in _WORK_SHIFTS:
        return None, ERROR_CALENDAR_INCONSISTENT

    if evidence.evidence_state == EVIDENCE_SENT_TO_CLERK:
        if (
            evidence.route_state != 'to_clerk'
            or evidence.latest_event_type != EVIDENCE_SENT_TO_CLERK
            or evidence.crew_plan_slot_id is not None
            or evidence.equipment_assignment_id is not None
            or evidence.assignment_shift_type is not None
        ):
            return None, ERROR_ROUTING_INCONSISTENT
        return CohortReadyMember(
            routing_row_id=evidence.routing_row_id,
            routing_event_id=evidence.latest_event_id,
            brigade_phase_row_id=phase.row_id,
            resident_id=evidence.resident_id,
            employee_id=evidence.employee_id,
            work_shift=phase.phase,
            equipment_assignment_id=None,
            crew_plan_slot_id=None,
            watch_profile_source_kind=profile.source_kind,
            employee_watch_profile_change_id=profile.change_id,
            watch_profile_work_schedule_id=profile.work_schedule_id,
            watch_profile_brigade_number=profile.brigade_number,
            watch_profile_watch_composition_id=profile.watch_composition_id,
            watch_profile_fingerprint=profile.profile_fingerprint,
        ), None

    if evidence.evidence_state == EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED:
        if (
            evidence.route_state != 'to_deputy'
            or evidence.latest_event_type != EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED
            or evidence.crew_plan_slot_id is None
            or evidence.equipment_assignment_id is None
            or evidence.assignment_shift_type not in _WORK_SHIFTS
        ):
            return None, ERROR_ROUTING_INCONSISTENT
        if evidence.assignment_shift_type != phase.phase:
            return None, ERROR_ASSIGNMENT_PHASE_MISMATCH
        return CohortReadyMember(
            routing_row_id=evidence.routing_row_id,
            routing_event_id=evidence.latest_event_id,
            brigade_phase_row_id=phase.row_id,
            resident_id=evidence.resident_id,
            employee_id=evidence.employee_id,
            work_shift=phase.phase,
            equipment_assignment_id=evidence.equipment_assignment_id,
            crew_plan_slot_id=evidence.crew_plan_slot_id,
            watch_profile_source_kind=profile.source_kind,
            employee_watch_profile_change_id=profile.change_id,
            watch_profile_work_schedule_id=profile.work_schedule_id,
            watch_profile_brigade_number=profile.brigade_number,
            watch_profile_watch_composition_id=profile.watch_composition_id,
            watch_profile_fingerprint=profile.profile_fingerprint,
        ), None

    return None, evidence.blocker_code or ERROR_ROUTING_INCONSISTENT


def build_arrival_roster_cohort_readiness(*, batch_id):
    """Combine exact routing and confirmed calendar evidence without writes."""
    try:
        routing = resolve_arrival_roster_routing_evidence(batch_id=batch_id)
    except ValidationError as error:
        code = getattr(error, 'code', None)
        if code == ERROR_BATCH_NOT_FOUND:
            raise SettlementCohortReadinessError(
                _SAFE_MESSAGES[ERROR_BATCH_NOT_FOUND],
                code=ERROR_BATCH_NOT_FOUND,
            ) from error
        raise SettlementCohortReadinessError(
            _SAFE_MESSAGES[ERROR_BATCH_INCONSISTENT],
            code=ERROR_BATCH_INCONSISTENT,
        ) from error

    ready_members = []
    blockers = []
    excluded = []
    rows = routing.rows
    watch_period = WatchPeriod.objects.only(
        'pk',
        'starts_on',
        'watch_composition_id',
    ).filter(pk=routing.watch_period_id).first()
    if watch_period is None:
        raise SettlementCohortReadinessError(
            _SAFE_MESSAGES[ERROR_BATCH_INCONSISTENT],
            code=ERROR_BATCH_INCONSISTENT,
        )

    if routing.batch_state != BATCH_STATE_CURRENT:
        batch_code = routing.batch_blocker_code or ERROR_BATCH_INCONSISTENT
        blockers.append(_blocker(routing_row_id=None, code=batch_code))
        for evidence in rows:
            if evidence.evidence_state == EVIDENCE_NOT_ARRIVING:
                excluded.append(evidence.routing_row_id)
            else:
                blockers.append(_blocker(
                    routing_row_id=evidence.routing_row_id,
                    code=batch_code,
                ))
        return SettlementCohortReadiness(
            batch_id=routing.batch_id,
            watch_period_id=routing.watch_period_id,
            ready_members=(),
            blockers=tuple(blockers),
            excluded_not_arriving_row_ids=tuple(excluded),
            is_ready=False,
        )

    arriving_rows = [
        evidence
        for evidence in rows
        if evidence.evidence_state != EVIDENCE_NOT_ARRIVING
    ]
    row_counts = Counter(evidence.routing_row_id for evidence in rows)
    resident_counts = Counter(evidence.resident_id for evidence in arriving_rows)
    handled_row_ids = set()

    for evidence in rows:
        row_id = evidence.routing_row_id
        if row_id in handled_row_ids:
            continue
        handled_row_ids.add(row_id)
        if row_counts[row_id] > 1:
            blockers.append(_blocker(
                routing_row_id=row_id,
                code=ERROR_DUPLICATE_ROUTING_ROW,
            ))
            continue
        if evidence.evidence_state == EVIDENCE_NOT_ARRIVING:
            excluded.append(row_id)
            continue
        if resident_counts[evidence.resident_id] > 1:
            blockers.append(_blocker(
                routing_row_id=row_id,
                code=ERROR_DUPLICATE_RESIDENT,
            ))
            continue
        if evidence.evidence_state not in {
            EVIDENCE_SENT_TO_CLERK,
            EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED,
        }:
            blockers.append(_blocker(
                routing_row_id=row_id,
                code=evidence.blocker_code or ERROR_ROUTING_INCONSISTENT,
            ))
            continue
        evidence_shape_error = _ready_evidence_shape_error(evidence)
        if evidence_shape_error:
            blockers.append(_blocker(
                routing_row_id=row_id,
                code=evidence_shape_error,
            ))
            continue

        profile, profile_error = _profile_for_evidence(
            evidence,
            watch_period_id=routing.watch_period_id,
            watch_period_starts_on=watch_period.starts_on,
            watch_composition_id=watch_period.watch_composition_id,
        )
        if profile_error:
            blockers.append(_blocker(routing_row_id=row_id, code=profile_error))
            continue
        phase, calendar_error = _phase_for_profile(
            profile=profile,
            watch_period_id=routing.watch_period_id,
        )
        if calendar_error:
            blockers.append(_blocker(routing_row_id=row_id, code=calendar_error))
            continue
        member, member_error = _ready_member(
            evidence=evidence,
            phase=phase,
            profile=profile,
        )
        if member_error:
            blockers.append(_blocker(routing_row_id=row_id, code=member_error))
            continue
        ready_members.append(member)

    if not arriving_rows:
        blockers.insert(0, _blocker(
            routing_row_id=None,
            code=ERROR_NO_ARRIVING_MEMBERS,
        ))

    return SettlementCohortReadiness(
        batch_id=routing.batch_id,
        watch_period_id=routing.watch_period_id,
        ready_members=tuple(ready_members),
        blockers=tuple(blockers),
        excluded_not_arriving_row_ids=tuple(excluded),
        is_ready=bool(ready_members) and not blockers,
    )


def _canonical_sha256(value):
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload).hexdigest()


def _cohort_member_snapshot(member):
    if member.routing_row_id is None or member.routing_event_id is None:
        return None
    return {
        'routing_row_id': member.routing_row_id,
        'routing_event_id': member.routing_event_id,
        'brigade_phase_row_id': member.brigade_phase_row_id,
        'resident_id': member.resident_id,
        'employee_id': member.routing_row.employee_id,
        'work_shift': member.work_shift,
        'crew_plan_slot_id': member.routing_event.crew_plan_slot_id,
        'equipment_assignment_id': member.official_equipment_assignment_id,
    }


def _approved_cohort_is_consistent(*, cohort, batch, members, routing_rows):
    snapshot = cohort.source_snapshot
    try:
        expected_members = tuple(sorted(
            snapshot['members'],
            key=lambda item: item['routing_row_id'],
        ))
        excluded = tuple(snapshot['excluded_not_arriving_row_ids'])
    except (KeyError, TypeError, ValueError):
        return False
    member_snapshots = tuple(_cohort_member_snapshot(member) for member in members)
    if any(snapshot is None for snapshot in member_snapshots):
        return False
    actual_members = tuple(sorted(
        member_snapshots,
        key=lambda item: item['routing_row_id'],
    ))
    routing_by_id = {row.pk: row for row in routing_rows}
    expected_member_ids = {
        item.get('routing_row_id')
        for item in expected_members
        if isinstance(item, dict)
    }
    excluded_ids = set(excluded)
    return bool(
        cohort.status == SettlementCohort.Status.APPROVED
        and cohort.source_revision_id is None
        and cohort.routing_batch_id == batch.pk
        and cohort.watch_period_id == batch.watch_period_id
        and cohort.source_type == _ROUTING_SOURCE_TYPE
        and cohort.source_id == str(batch.pk)
        and cohort.created_by_id == cohort.approved_by_id
        and cohort.approved_at is not None
        and isinstance(snapshot, dict)
        and snapshot.get('source_kind') == _ROUTING_SOURCE_TYPE
        and snapshot.get('routing_batch_id') == batch.pk
        and snapshot.get('arrival_roster_version_id') == batch.arrival_roster_version_id
        and snapshot.get('watch_period_id') == batch.watch_period_id
        and list(expected_members) == snapshot.get('members')
        and list(excluded) == sorted(excluded_ids)
        and _canonical_sha256(snapshot) == cohort.input_fingerprint
        and actual_members == expected_members
        and len(members) == len(expected_members)
        and expected_member_ids.isdisjoint(excluded_ids)
        and expected_member_ids | excluded_ids == set(routing_by_id)
        and excluded_ids <= set(routing_by_id)
        and all(
            routing_by_id[row_id].route_state == 'not_participating'
            and (routing_by_id[row_id].participation_snapshot or {}).get(
                'participation_status',
            ) == 'not_arriving'
            for row_id in excluded_ids
        )
        and all(
            member.source_revision_id is None
            and member.routing_row.batch_id == batch.pk
            and member.routing_event.routing_row_id == member.routing_row_id
            and member.basis_type == _ROUTING_BASIS_TYPE
            and member.basis_id == str(member.routing_row_id)
            and _canonical_sha256(member.shift_source_snapshot)
            == member.shift_source_fingerprint
            and (
                (
                    member.shift_source_kind
                    == SettlementCohortMember.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE
                    and member.official_equipment_assignment_id is None
                )
                or (
                    member.shift_source_kind
                    == SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT
                    and member.official_equipment_assignment_id is not None
                )
            )
            for member in members
        )
    )


def build_arrival_roster_cohort_overview():
    """Return immutable, safe UI state for current confirmed routing batches."""
    batches = list(
        ArrivalRosterRoutingBatch.objects
        .select_related('arrival_roster_version', 'watch_period')
        .filter(
            arrival_roster_version__status=ArrivalRosterVersion.Status.CONFIRMED,
            arrival_roster_version__superseded_at__isnull=True,
        )
        .order_by('watch_period__starts_on', 'watch_period_id', 'pk')
    )
    batch_ids = [batch.pk for batch in batches]
    cohorts = {
        cohort.routing_batch_id: cohort
        for cohort in (
            SettlementCohort._base_manager
            .filter(routing_batch_id__in=batch_ids)
            .order_by('routing_batch_id', 'pk')
        )
    }
    members_by_cohort = {}
    for member in (
        SettlementCohortMember._base_manager
        .filter(cohort_id__in=[cohort.pk for cohort in cohorts.values()])
        .select_related('routing_row', 'routing_event')
        .order_by('cohort_id', 'routing_row_id', 'pk')
    ):
        members_by_cohort.setdefault(member.cohort_id, []).append(member)
    routing_rows_by_batch = {}
    for batch in batches:
        routing_rows_by_batch[batch.pk] = list(
            batch.rows.order_by('pk')
        )

    overview = []
    for batch in batches:
        try:
            readiness = build_arrival_roster_cohort_readiness(batch_id=batch.pk)
            readiness_error = None
        except SettlementCohortReadinessError as error:
            readiness = None
            readiness_error = error.messages[0]
        cohort = cohorts.get(batch.pk)
        is_created = bool(
            cohort
            and _approved_cohort_is_consistent(
                cohort=cohort,
                batch=batch,
                members=members_by_cohort.get(cohort.pk, []),
                routing_rows=routing_rows_by_batch[batch.pk],
            )
        )
        if cohort is not None and not is_created:
            blocker_messages = (_INCONSISTENT_COHORT_MESSAGE,)
        elif readiness_error is not None:
            blocker_messages = (readiness_error,)
        else:
            blocker_messages = tuple(dict.fromkeys(
                blocker.message for blocker in readiness.blockers
            ))
        overview.append(SettlementCohortBatchOverview(
            batch_id=batch.pk,
            watch_period_name=batch.watch_period.name,
            watch_period_dates=(
                f'{batch.watch_period.starts_on:%d.%m.%Y} — '
                f'{batch.watch_period.ends_on:%d.%m.%Y}'
            ),
            ready_member_count=(len(readiness.ready_members) if readiness else 0),
            excluded_not_arriving_count=(
                len(readiness.excluded_not_arriving_row_ids) if readiness else 0
            ),
            blocker_messages=blocker_messages,
            can_create=bool(cohort is None and readiness and readiness.is_ready),
            is_created=is_created,
            approved_at=cohort.approved_at if is_created else None,
            approved_member_count=(
                len(members_by_cohort.get(cohort.pk, [])) if is_created else 0
            ),
        ))
    return SettlementCohortOverview(batches=tuple(overview))
