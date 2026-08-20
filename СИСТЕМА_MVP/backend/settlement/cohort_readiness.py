"""Read-only readiness projection for one exact arrival-roster routing batch."""

from collections import Counter
from dataclasses import dataclass

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
from users.models import Employee


ERROR_EMPLOYEE_NOT_FOUND = 'employee_not_found'
ERROR_EMPLOYEE_INACTIVE = 'employee_inactive'
ERROR_EMPLOYEE_SCHEDULE_MISSING = 'employee_schedule_missing'
ERROR_EMPLOYEE_BRIGADE_MISSING = 'employee_brigade_missing'
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


def _employee_for_evidence(evidence):
    if evidence.employee_id is None:
        return None, ERROR_EXTERNAL_SHIFT_UNRESOLVED
    employee = Employee.objects.select_related('work_schedule').filter(
        pk=evidence.employee_id,
    ).first()
    if employee is None:
        return None, ERROR_EMPLOYEE_NOT_FOUND
    if not employee.is_active or employee.status != Employee.Status.ACTIVE:
        return None, ERROR_EMPLOYEE_INACTIVE
    if employee.work_schedule_id is None:
        return None, ERROR_EMPLOYEE_SCHEDULE_MISSING
    if (
        isinstance(employee.brigade_number, bool)
        or not isinstance(employee.brigade_number, int)
        or employee.brigade_number < 1
    ):
        return None, ERROR_EMPLOYEE_BRIGADE_MISSING
    return employee, None


def _phase_for_employee(*, employee, watch_period_id):
    try:
        return resolve_confirmed_brigade_phase(
            watch_period_id=watch_period_id,
            work_schedule_id=employee.work_schedule_id,
            brigade_number=employee.brigade_number,
        ), None
    except BrigadePhaseCalendarError as error:
        return None, _calendar_blocker_code(error)


def _ready_member(*, evidence, phase):
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

        employee, employee_error = _employee_for_evidence(evidence)
        if employee_error:
            blockers.append(_blocker(routing_row_id=row_id, code=employee_error))
            continue
        phase, calendar_error = _phase_for_employee(
            employee=employee,
            watch_period_id=routing.watch_period_id,
        )
        if calendar_error:
            blockers.append(_blocker(routing_row_id=row_id, code=calendar_error))
            continue
        member, member_error = _ready_member(evidence=evidence, phase=phase)
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
