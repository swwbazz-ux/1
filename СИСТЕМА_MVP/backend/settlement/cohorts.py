import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from assignments.models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    EquipmentAssignment,
    WorkShiftType,
)
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rotations.models import (
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterVersion,
)
from rotations.arrival_roster_routing import (
    ERROR_OFFICIAL_ASSIGNMENT_INCONSISTENT,
    ERROR_OFFICIAL_ASSIGNMENT_MISSING,
)
from shifts.models import (
    WatchPeriod,
    WatchPeriodBrigadePhaseRow,
    WatchPeriodBrigadePhaseVersion,
)
from users.models import Employee, EmployeeAccess, WorkSchedule

from .cohort_readiness import (
    ERROR_ASSIGNMENT_PHASE_MISMATCH,
    SettlementCohortReadinessError,
    _approved_cohort_is_consistent,
    build_arrival_roster_cohort_readiness,
)
from .models import (
    SettlementCohort,
    SettlementCohortMember,
    SettlementResident,
    SettlementRevision,
)
from .residents import build_settlement_resident_lock_plan, lock_settlement_resident_plan


def _confirmed_revision(revision):
    if revision.status != SettlementRevision.Status.CONFIRMED:
        raise ValidationError('Операция M5 требует подтверждённой ревизии.')


def _shift_review_required(message):
    return ValidationError(
        f'Требуется проверка: {message}',
        code='settlement.cohort.shift_review_required',
    )


def _canonical_shift_source(snapshot):
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    )
    return snapshot, hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _watch_period_start(period):
    return timezone.make_aware(
        datetime.combine(period.starts_on, time.min),
        timezone.get_current_timezone(),
    )


def _eligible_official_assignments(*, employee_id, period):
    effective_at = _watch_period_start(period)
    return list(
        EquipmentAssignment.objects.filter(
            employee_id=employee_id,
            status=AssignmentStatus.ACCEPTED,
            shift__isnull=True,
            role__isnull=False,
            shift_type__in=WorkShiftType.values,
            assigned_at__lte=effective_at,
            source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
            source_crew_plan_slot__isnull=False,
            source_crew_plan_slot__plan__work_date=period.starts_on,
        )
        .filter(Q(accepted_at__isnull=True) | Q(accepted_at__lte=effective_at))
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=effective_at))
        .select_related(
            'employee', 'equipment', 'role', 'source_crew_plan_slot__plan',
        )
        .order_by('pk')
    )


def resolve_internal_official_shift_source(
    *,
    resident,
    period,
    expected_assignment_id=None,
    require_expected_assignment=False,
):
    if not resident.employee_id:
        raise _shift_review_required('внутренний жилец не связан с Employee.')
    if require_expected_assignment and expected_assignment_id is None:
        raise _shift_review_required('не указано точное официальное назначение.')
    assignments = _eligible_official_assignments(
        employee_id=resident.employee_id,
        period=period,
    )
    if len(assignments) != 1:
        raise _shift_review_required(
            'на начало WatchPeriod требуется ровно одно официальное назначение.',
        )
    assignment = assignments[0]
    if (
        expected_assignment_id is not None
        and assignment.pk != expected_assignment_id
    ):
        raise _shift_review_required('переданное назначение не является единственным действующим.')
    slot = assignment.source_crew_plan_slot
    if (
        slot.employee_id != resident.employee_id
        or slot.equipment_id != assignment.equipment_id
        or slot.plan.role_id != assignment.role_id
        or slot.shift_type != assignment.shift_type
        or slot.plan.work_date != period.starts_on
        or assignment.shift_type not in WorkShiftType.values
    ):
        raise _shift_review_required('назначение не соответствует Employee, технике, роли, смене или периоду.')
    try:
        assignment.full_clean()
    except ValidationError as error:
        raise _shift_review_required('официальное назначение больше невалидно.') from error
    snapshot = {
        'kind': SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT,
        'work_shift': assignment.shift_type,
        'assignment_id': assignment.pk,
        'assignment_status': assignment.status,
        'assigned_at': assignment.assigned_at.isoformat(),
        'accepted_at': assignment.accepted_at.isoformat() if assignment.accepted_at else None,
        'ended_at': assignment.ended_at.isoformat() if assignment.ended_at else None,
        'employee_id': assignment.employee_id,
        'equipment_id': assignment.equipment_id,
        'role_id': assignment.role_id,
        'crew_plan_slot_id': slot.pk,
        'crew_plan_id': slot.plan_id,
        'crew_plan_status': slot.plan.status,
        'crew_plan_work_date': slot.plan.work_date.isoformat(),
    }
    snapshot, fingerprint = _canonical_shift_source(snapshot)
    return {
        'work_shift': assignment.shift_type,
        'shift_source_kind': SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT,
        'official_equipment_assignment': assignment,
        'shift_source_snapshot': snapshot,
        'shift_source_fingerprint': fingerprint,
        'shift_selected_by_access': None,
        'shift_selected_at': None,
        'shift_selection_basis': '',
    }


def _internal_shift_source(*, resident, period, assignment_id):
    return resolve_internal_official_shift_source(
        resident=resident,
        period=period,
        expected_assignment_id=assignment_id,
        require_expected_assignment=True,
    )


def _external_shift_source(*, work_shift, access_id, basis, selected_at=None, for_update=True):
    if work_shift not in SettlementCohortMember.WorkShift.values:
        raise _shift_review_required('для внешнего жильца не выбрана дневная или ночная смена.')
    if access_id is None or not str(basis or '').strip():
        raise _shift_review_required('внешняя смена требует точный доступ и непустое основание.')
    try:
        access_query = EmployeeAccess.objects.select_related('employee', 'role')
        if for_update:
            access_query = access_query.select_for_update(of=('self',))
        access = access_query.get(pk=access_id)
    except EmployeeAccess.DoesNotExist as error:
        raise _shift_review_required('доступ делопроизводителя не найден.') from error
    if (
        access.status != EmployeeAccess.Status.ACTIVATED
        or not access.is_active
        or access.role.code != 'clerk'
        or not access.employee.is_active
        or access.employee.status != access.employee.Status.ACTIVE
    ):
        raise _shift_review_required('доступ делопроизводителя неактивен или имеет другую роль.')
    selected_at = selected_at or timezone.now()
    basis = str(basis).strip()
    snapshot = {
        'kind': SettlementCohortMember.ShiftSourceKind.EXTERNAL_CLERK,
        'work_shift': work_shift,
        'access_id': access.pk,
        'actor_employee_id': access.employee_id,
        'role_id': access.role_id,
        'role_code': access.role.code,
        'selected_at': selected_at.isoformat(),
        'basis': basis,
    }
    snapshot, fingerprint = _canonical_shift_source(snapshot)
    return {
        'work_shift': work_shift,
        'shift_source_kind': SettlementCohortMember.ShiftSourceKind.EXTERNAL_CLERK,
        'official_equipment_assignment': None,
        'shift_source_snapshot': snapshot,
        'shift_source_fingerprint': fingerprint,
        'shift_selected_by_access': access,
        'shift_selected_at': selected_at,
        'shift_selection_basis': basis,
    }


def _routing_source_error(message):
    return _shift_review_required(f'источник передачи реестра изменился: {message}')


def _routing_member_context(*, cohort, member, ready, period):
    row = member.routing_row
    event = member.routing_event
    phase_row = member.brigade_phase_row
    assignment = member.official_equipment_assignment
    if (
        row is None
        or event is None
        or phase_row is None
        or member.cohort_id != cohort.pk
        or row.batch_id != cohort.routing_batch_id
        or row.pk != ready.routing_row_id
        or row.resident_id != member.resident_id
        or row.employee_id != member.resident.employee_id
        or ready.resident_id != member.resident_id
        or ready.employee_id != member.resident.employee_id
        or event.pk != ready.routing_event_id
        or event.routing_row_id != row.pk
        or phase_row.pk != ready.brigade_phase_row_id
        or phase_row.version.watch_period_id != period.pk
        or phase_row.version.work_schedule_id != member.resident.employee.work_schedule_id
        or phase_row.brigade_number != member.resident.employee.brigade_number
        or phase_row.phase != ready.work_shift
        or member.work_shift != ready.work_shift
        or member.participation_status != _participation_status(row)
        or member.arrival_at != _aware_day(_snapshot_date(row.dates_snapshot, 'arrival_on'))
        or member.departure_at != _aware_day(
            _snapshot_date(row.dates_snapshot, 'departure_on') + timedelta(days=1)
        )
    ):
        raise _routing_source_error('exact FK участника больше не согласованы.')

    if ready.equipment_assignment_id is None:
        if (
            member.shift_source_kind
            != SettlementCohortMember.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE
            or assignment is not None
            or ready.crew_plan_slot_id is not None
            or event.event_type != ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK
            or event.crew_plan_slot_id is not None
            or event.equipment_assignment_id is not None
        ):
            raise _routing_source_error('прямой маршрут участника повреждён.')
    else:
        effective_at = _watch_period_start(period)
        if (
            member.shift_source_kind
            != SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT
            or assignment is None
            or assignment.pk != ready.equipment_assignment_id
            or assignment.pk != event.equipment_assignment_id
            or assignment.source_crew_plan_slot_id != ready.crew_plan_slot_id
            or assignment.source_crew_plan_slot_id != event.crew_plan_slot_id
            or event.event_type
            != ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED
            or assignment.status != AssignmentStatus.ACCEPTED
            or assignment.ended_at is not None
            or assignment.shift_id is not None
            or assignment.role_id is None
            or assignment.assigned_at > effective_at
            or assignment.accepted_at is not None
            and assignment.accepted_at > effective_at
            or assignment.source_kind
            != EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN
            or assignment.source_crew_plan_slot_id is None
            or assignment.employee_id != member.resident.employee_id
            or assignment.shift_type != ready.work_shift
            or assignment.shift_type != phase_row.phase
        ):
            raise _routing_source_error('официальное назначение участника изменилось.')
        slot = assignment.source_crew_plan_slot
        if (
            slot.employee_id != assignment.employee_id
            or slot.equipment_id != assignment.equipment_id
            or slot.plan.role_id != assignment.role_id
            or slot.shift_type != assignment.shift_type
            or slot.plan.work_date
            != _snapshot_date(row.dates_snapshot, 'arrival_on')
        ):
            raise _routing_source_error('опубликованный слот назначения изменился.')
        try:
            assignment.full_clean()
        except ValidationError as error:
            raise _routing_source_error('официальное назначение больше невалидно.') from error

    basis_snapshot, shift_snapshot, shift_fingerprint, shift_source_kind = (
        _routing_member_snapshots(
            ready=ready,
            row=row,
            event=event,
            phase_row=phase_row,
            assignment=assignment,
        )
    )
    production_context_snapshot = {
        'role': row.role_snapshot,
        'role_basis': row.role_basis_snapshot,
        'crew_plan_slot_id': ready.crew_plan_slot_id,
        'equipment_assignment_id': ready.equipment_assignment_id,
    }
    if (
        member.basis_snapshot != basis_snapshot
        or member.shift_source_kind != shift_source_kind
        or member.shift_source_snapshot != shift_snapshot
        or member.shift_source_fingerprint != shift_fingerprint
        or member.production_context_snapshot != production_context_snapshot
        or member.shift_selected_by_access_id is not None
        or member.shift_selected_at is not None
        or member.shift_selection_basis
    ):
        raise _routing_source_error('неизменяемый snapshot участника не совпадает.')
    return {
        'work_shift': ready.work_shift,
        'shift_source_kind': shift_source_kind,
        'official_equipment_assignment': assignment,
        'shift_source_snapshot': shift_snapshot,
        'shift_source_fingerprint': shift_fingerprint,
        'shift_selected_by_access': None,
        'shift_selected_at': None,
        'shift_selection_basis': '',
    }


def revalidate_routing_cohort_members(*, cohort, members=None):
    """Revalidate one structurally linked routing cohort without selecting fallbacks."""

    if cohort.routing_batch_id is None:
        raise _routing_source_error('cohort не связан с exact routing batch.')
    batch = cohort.routing_batch
    version = batch.arrival_roster_version
    period = cohort.watch_period
    if (
        cohort.status != SettlementCohort.Status.APPROVED
        or batch.watch_period_id != period.pk
        or version.watch_period_id != period.pk
        or version.status != ArrivalRosterVersion.Status.CONFIRMED
        or version.superseded_at is not None
    ):
        raise _routing_source_error('версия реестра больше не является текущей.')
    if members is None:
        members = list(
            SettlementCohortMember._base_manager.filter(cohort_id=cohort.pk)
            .select_related(
                'cohort__routing_batch__arrival_roster_version',
                'cohort__watch_period',
                'resident__employee__work_schedule',
                'routing_row',
                'routing_event__crew_plan_slot__plan__role',
                'official_equipment_assignment__source_crew_plan_slot__plan__role',
                'brigade_phase_row__version',
            )
            .order_by('routing_row_id', 'pk')
        )
    else:
        members = list(members)
    routing_rows = list(
        ArrivalRosterRoutingRow._base_manager.filter(batch_id=batch.pk)
        .order_by('pk')
    )
    if not _approved_cohort_is_consistent(
        cohort=cohort,
        batch=batch,
        members=members,
        routing_rows=routing_rows,
    ):
        raise _routing_source_error('snapshot состава больше не воспроизводится.')
    try:
        readiness = build_arrival_roster_cohort_readiness(batch_id=batch.pk)
    except SettlementCohortReadinessError as error:
        raise _routing_source_error('готовность передачи не подтверждена.') from error
    if (
        readiness.batch_id != batch.pk
        or readiness.watch_period_id != period.pk
    ):
        raise _routing_source_error('готовность передачи изменилась.')

    assignment_blocker_codes = {
        ERROR_ASSIGNMENT_PHASE_MISMATCH,
        ERROR_OFFICIAL_ASSIGNMENT_INCONSISTENT,
        ERROR_OFFICIAL_ASSIGNMENT_MISSING,
    }
    assignment_blockers = {
        blocker.routing_row_id: blocker.code
        for blocker in readiness.blockers
        if (
            blocker.routing_row_id is not None
            and blocker.code in assignment_blocker_codes
        )
    }
    if any(
        blocker.routing_row_id is None
        or blocker.code not in assignment_blocker_codes
        for blocker in readiness.blockers
    ):
        raise _routing_source_error('готовность передачи изменилась.')
    ready_by_row = {
        item.routing_row_id: item for item in readiness.ready_members
    }
    members_by_row = {item.routing_row_id: item for item in members}
    if (
        None in members_by_row
        or len(members_by_row) != len(members)
        or set(members_by_row) != set(ready_by_row) | set(assignment_blockers)
        or set(members_by_row).intersection(readiness.excluded_not_arriving_row_ids)
    ):
        raise _routing_source_error('состав participants больше не совпадает с передачей.')
    contexts = {}
    for member in members:
        blocker_code = assignment_blockers.get(member.routing_row_id)
        if blocker_code is not None:
            contexts[member.pk] = {
                'validation_error': _routing_source_error(
                    'официальное назначение участника изменилось.',
                ),
                'official_equipment_assignment': None,
            }
            continue
        contexts[member.pk] = _routing_member_context(
            cohort=cohort,
            member=member,
            ready=ready_by_row[member.routing_row_id],
            period=period,
        )
    return contexts


def _revalidated_member_shift_source(
    *, member, period, for_update=True, routing_context=None,
):
    if member.cohort.routing_batch_id is not None:
        contexts = routing_context
        if contexts is None:
            contexts = revalidate_routing_cohort_members(cohort=member.cohort)
        try:
            context = contexts[member.pk]
        except KeyError as error:
            raise _routing_source_error('участник отсутствует в exact составе.') from error
        if 'validation_error' in context:
            raise context['validation_error']
        return context
    if member.shift_source_kind == SettlementCohortMember.ShiftSourceKind.UNVERIFIED_LEGACY:
        raise _shift_review_required('историческая membership не содержит проверенной смены.')
    if member.shift_source_kind == SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT:
        current = _internal_shift_source(
            resident=member.resident,
            period=period,
            assignment_id=member.official_equipment_assignment_id,
        )
    elif member.shift_source_kind == SettlementCohortMember.ShiftSourceKind.EXTERNAL_CLERK:
        current = _external_shift_source(
            work_shift=member.work_shift,
            access_id=member.shift_selected_by_access_id,
            basis=member.shift_selection_basis,
            selected_at=member.shift_selected_at,
            for_update=for_update,
        )
    else:
        raise _shift_review_required('тип источника смены неизвестен.')
    if (
        current['work_shift'] != member.work_shift
        or current['shift_source_snapshot'] != member.shift_source_snapshot
        or current['shift_source_fingerprint'] != member.shift_source_fingerprint
    ):
        raise _shift_review_required('источник официальной смены изменился.')
    return current


@transaction.atomic
def create_settlement_cohort(
    *,
    watch_period_id,
    source_revision_id,
    source_type,
    source_id,
    source_snapshot,
    input_fingerprint,
    created_by_id,
    supersedes_id=None,
):
    resident_plan = build_settlement_resident_lock_plan(
        resident_ids=(),
        employee_ids=(created_by_id,),
    )
    lock_settlement_resident_plan(resident_plan)
    period = (
        WatchPeriod.objects.select_for_update(of=('self',))
        .select_related('watch_composition')
        .get(pk=watch_period_id)
    )
    if period.watch_composition_id is None:
        raise ValidationError('WatchPeriod без WatchComposition нельзя использовать для M5.')

    previous = None
    if supersedes_id is not None:
        previous = SettlementCohort.objects.select_for_update(of=('self',)).get(pk=supersedes_id)
        if previous.status != SettlementCohort.Status.APPROVED:
            raise ValidationError('Новая версия cohort может заменять только APPROVED cohort.')
        if previous.watch_period_id != period.pk:
            raise ValidationError('Заменяемый cohort относится к другому WatchPeriod.')
        version = previous.version + 1
    else:
        if SettlementCohort.objects.filter(watch_period_id=period.pk).exists():
            raise ValidationError('Следующая версия cohort должна явно указывать supersedes.')
        version = 1

    cohort = SettlementCohort(
        watch_composition_id=period.watch_composition_id,
        watch_period=period,
        version=version,
        source_revision_id=source_revision_id,
        source_type=source_type,
        source_id=source_id,
        source_snapshot=source_snapshot,
        input_fingerprint=input_fingerprint,
        created_by_id=created_by_id,
        supersedes=previous,
    )
    cohort.save()
    return cohort


@transaction.atomic
def add_settlement_cohort_member(
    *,
    cohort_id,
    resident_id,
    arrival_at,
    departure_at,
    participation_status,
    source_revision_id,
    basis_type,
    basis_id,
    basis_snapshot,
    reason='',
    expected_schedule_regime='',
    production_context_snapshot=None,
    official_equipment_assignment_id=None,
    work_shift=None,
    shift_selected_by_access_id=None,
    shift_selection_basis='',
):
    snapshot = (
        SettlementCohort.objects.filter(pk=cohort_id)
        .values('watch_period_id')
        .first()
    )
    if snapshot is None:
        raise SettlementCohort.DoesNotExist
    resident_plan = build_settlement_resident_lock_plan(resident_ids=(resident_id,))
    locked_subjects = lock_settlement_resident_plan(resident_plan)
    locked_residents = {resident.pk: resident for resident in locked_subjects.residents}
    WatchPeriod.objects.select_for_update(of=('self',)).get(pk=snapshot['watch_period_id'])
    cohort = (
        SettlementCohort.objects.select_for_update(of=('self',))
        .select_related('watch_period', 'watch_composition')
        .get(pk=cohort_id)
    )
    if cohort.watch_period_id != snapshot['watch_period_id']:
        raise ValidationError('Cohort изменился после построения M5 lock plan.')
    if cohort.status != SettlementCohort.Status.DRAFT:
        raise ValidationError('Membership добавляется только в DRAFT cohort.')
    resident = locked_residents.get(resident_id)
    if resident is None:
        raise ValidationError('Resident исчез после построения M5 lock plan.')
    if resident.resident_type == resident.ResidentType.EMPLOYEE:
        if work_shift is not None or shift_selected_by_access_id is not None or shift_selection_basis:
            raise _shift_review_required('внутренний жилец не принимает параметры внешней смены.')
        shift_source = _internal_shift_source(
            resident=resident,
            period=cohort.watch_period,
            assignment_id=official_equipment_assignment_id,
        )
    else:
        if official_equipment_assignment_id is not None:
            raise _shift_review_required('внешний жилец не может иметь EquipmentAssignment.')
        shift_source = _external_shift_source(
            work_shift=work_shift,
            access_id=shift_selected_by_access_id,
            basis=shift_selection_basis,
        )
    member = SettlementCohortMember(
        cohort=cohort,
        resident_id=resident_id,
        arrival_at=arrival_at,
        departure_at=departure_at,
        participation_status=participation_status,
        reason=reason,
        expected_schedule_regime=expected_schedule_regime,
        source_revision_id=source_revision_id,
        basis_type=basis_type,
        basis_id=basis_id,
        basis_snapshot=basis_snapshot,
        production_context_snapshot=production_context_snapshot or {},
        **shift_source,
    )
    member.save()
    return member


@transaction.atomic
def approve_settlement_cohort(*, cohort_id, approved_by_id, approved_at=None):
    snapshot = SettlementCohort.objects.filter(pk=cohort_id).values(
        'watch_period_id', 'watch_composition_id', 'supersedes_id',
    ).first()
    if snapshot is None:
        raise SettlementCohort.DoesNotExist
    member_snapshot = tuple(
        SettlementCohortMember.objects.filter(cohort_id=cohort_id)
        .order_by('resident_id', 'pk')
        .values_list('pk', 'resident_id')
    )
    resident_ids = tuple(resident_id for _pk, resident_id in member_snapshot)
    resident_plan = build_settlement_resident_lock_plan(
        resident_ids=resident_ids,
        employee_ids=(approved_by_id,),
    )
    locked_subjects = lock_settlement_resident_plan(resident_plan)
    locked_residents = {resident.pk: resident for resident in locked_subjects.residents}

    period = WatchPeriod.objects.select_for_update(of=('self',)).get(pk=snapshot['watch_period_id'])
    locked_cohorts = list(
        SettlementCohort.objects.select_for_update(of=('self',))
        .filter(watch_period_id=period.pk)
        .order_by('pk')
    )
    cohort_by_id = {cohort.pk: cohort for cohort in locked_cohorts}
    cohort = cohort_by_id.get(cohort_id)
    if cohort is None:
        raise ValidationError('Cohort исчез после построения M5 lock plan.')
    if (
        cohort.watch_period_id != snapshot['watch_period_id']
        or cohort.watch_composition_id != snapshot['watch_composition_id']
        or cohort.supersedes_id != snapshot['supersedes_id']
    ):
        raise ValidationError('Cohort изменился после построения M5 lock plan.')

    members = list(
        SettlementCohortMember.objects.select_for_update(of=('self',))
        .filter(cohort_id=cohort.pk)
        .select_related(
            'resident__employee', 'source_revision',
            'official_equipment_assignment__source_crew_plan_slot__plan',
            'shift_selected_by_access__employee', 'shift_selected_by_access__role',
        )
        .order_by('resident_id', 'pk')
    )
    if tuple((member.pk, member.resident_id) for member in members) != member_snapshot:
        raise ValidationError('Membership изменился после построения M5 lock plan.')

    if cohort.status == SettlementCohort.Status.APPROVED:
        return cohort
    if cohort.status != SettlementCohort.Status.DRAFT:
        raise ValidationError('Утвердить можно только DRAFT cohort.')
    if period.watch_composition_id != cohort.watch_composition_id:
        raise ValidationError('WatchPeriod больше не соответствует WatchComposition cohort.')
    _confirmed_revision(cohort.source_revision)

    previous = None
    excluded_cohort_ids = {cohort.pk}
    if cohort.supersedes_id is not None:
        previous = cohort_by_id.get(cohort.supersedes_id)
        if previous is None or previous.status != SettlementCohort.Status.APPROVED:
            raise ValidationError('Заменяемая версия cohort больше не является APPROVED.')
        if (
            previous.watch_period_id != cohort.watch_period_id
            or previous.watch_composition_id != cohort.watch_composition_id
            or cohort.version != previous.version + 1
        ):
            raise ValidationError('Нарушена последовательность версий cohort.')
        excluded_cohort_ids.add(previous.pk)
    elif any(item.status == SettlementCohort.Status.APPROVED for item in locked_cohorts):
        raise ValidationError('Для WatchPeriod уже существует APPROVED cohort.')

    for member in members:
        resident = locked_residents[member.resident_id]
        if (
            resident.resident_type == resident.ResidentType.EMPLOYEE
            and resident.employee.watch_composition_id != cohort.watch_composition_id
        ):
            raise ValidationError({
                'resident': 'Внутренний Employee больше не принадлежит WatchComposition cohort.',
            })
        _confirmed_revision(member.source_revision)
        _revalidated_member_shift_source(member=member, period=period)
        member.full_clean()
        if not member.participates_in_accommodation:
            continue
        overlap_exists = (
            SettlementCohortMember.objects.filter(
                resident_id=member.resident_id,
                cohort__status=SettlementCohort.Status.APPROVED,
                participation_status__in=SettlementCohortMember.ACTIVE_PARTICIPATION_STATUSES,
                arrival_at__lt=member.departure_at,
                departure_at__gt=member.arrival_at,
            )
            .exclude(cohort_id__in=excluded_cohort_ids)
            .exists()
        )
        if overlap_exists:
            raise ValidationError({
                'resident': 'Жилец уже входит в APPROVED cohort пересекающегося периода.',
            })

    approval_time = approved_at or timezone.now()
    if previous is not None:
        previous.status = SettlementCohort.Status.SUPERSEDED
        previous.superseded_at = approval_time
        previous.save(update_fields=['status', 'superseded_at', 'updated_at'])

    cohort.status = SettlementCohort.Status.APPROVED
    cohort.approved_by_id = approved_by_id
    cohort.approved_at = approval_time
    cohort.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    return cohort


ERROR_ACCESS_NOT_FOUND = 'access_not_found'
ERROR_ACCESS_INACTIVE = 'access_inactive'
ERROR_ACCESS_BLOCKED = 'access_blocked'
ERROR_ACCESS_WRONG_ROLE = 'access_wrong_role'
ERROR_BATCH_NOT_FOUND = 'batch_not_found'
ERROR_COHORT_NOT_READY = 'cohort_not_ready'
ERROR_COHORT_ALREADY_INCONSISTENT = 'cohort_already_inconsistent'
ERROR_ROUTING_STALE = 'routing_stale'
ERROR_PROVENANCE_INCONSISTENT = 'provenance_inconsistent'
ERROR_COHORT_GRAPH_INCOMPLETE = 'cohort_graph_incomplete'
ERROR_COHORT_GRAPH_INCONSISTENT = 'cohort_graph_inconsistent'

_ROUTING_SOURCE_TYPE = 'arrival_roster_routing'
_ROUTING_BASIS_TYPE = 'arrival_roster_routing_row'


class ArrivalRosterCohortCreationError(ValidationError):
    """Controlled failure of the closed routing-to-cohort writer."""

    def __init__(self, message, *, code, blocker_codes=()):
        super().__init__(message, code=code)
        self.blocker_codes = tuple(blocker_codes)


def _creation_error(code, message, *, blocker_codes=()):
    return ArrivalRosterCohortCreationError(
        message,
        code=code,
        blocker_codes=blocker_codes,
    )


@dataclass(frozen=True, slots=True)
class _RoutingCohortLockPlan:
    access_id: int
    actor_employee_id: int
    actor_role_id: int
    batch_id: int
    version_id: int
    watch_period_id: int
    routing_row_ids: tuple[int, ...]
    routing_event_ids: tuple[int, ...]
    resident_ids: tuple[int, ...]
    employee_ids: tuple[int, ...]
    work_schedule_ids: tuple[int, ...]
    crew_plan_ids: tuple[int, ...]
    crew_plan_slot_ids: tuple[int, ...]
    equipment_assignment_ids: tuple[int, ...]
    phase_version_ids: tuple[int, ...]
    phase_row_ids: tuple[int, ...]
    cohort_ids: tuple[int, ...]
    cohort_member_ids: tuple[int, ...]


def _ordered_ids(queryset):
    return tuple(queryset.order_by('pk').values_list('pk', flat=True))


def _routing_cohort_lock_plan(*, batch_id, actor_access_id):
    access_plan = EmployeeAccess.objects.filter(pk=actor_access_id).values(
        'pk', 'employee_id', 'role_id',
    ).first()
    if access_plan is None:
        raise _creation_error(
            ERROR_ACCESS_NOT_FOUND,
            'Точный доступ делопроизводителя не найден.',
        )
    batch_plan = ArrivalRosterRoutingBatch._base_manager.filter(pk=batch_id).values(
        'pk', 'arrival_roster_version_id', 'watch_period_id',
    ).first()
    if batch_plan is None:
        raise _creation_error(ERROR_BATCH_NOT_FOUND, 'Передача реестра не найдена.')

    routing_rows = tuple(
        ArrivalRosterRoutingRow._base_manager.filter(batch_id=batch_id)
        .order_by('pk')
        .values('pk', 'resident_id', 'employee_id')
    )
    routing_row_ids = tuple(row['pk'] for row in routing_rows)
    routing_events = tuple(
        ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row_id__in=routing_row_ids,
        )
        .order_by('pk')
        .values('pk', 'crew_plan_slot_id', 'equipment_assignment_id')
    )
    crew_plan_slot_ids = tuple(sorted({
        event['crew_plan_slot_id']
        for event in routing_events
        if event['crew_plan_slot_id'] is not None
    }))
    crew_plan_ids = tuple(sorted(set(
        CrewPlanSlot.objects.filter(pk__in=crew_plan_slot_ids)
        .values_list('plan_id', flat=True)
    )))
    employee_ids = tuple(sorted({
        row['employee_id']
        for row in routing_rows
        if row['employee_id'] is not None
    }))
    work_schedule_ids = tuple(sorted(set(
        Employee.objects.filter(pk__in=employee_ids)
        .exclude(work_schedule_id__isnull=True)
        .values_list('work_schedule_id', flat=True)
    )))
    phase_version_ids = _ordered_ids(
        WatchPeriodBrigadePhaseVersion._base_manager.filter(
            watch_period_id=batch_plan['watch_period_id'],
            work_schedule_id__in=work_schedule_ids,
        )
    )
    cohort_ids = _ordered_ids(
        SettlementCohort._base_manager.filter(
            watch_period_id=batch_plan['watch_period_id'],
        )
    )
    return _RoutingCohortLockPlan(
        access_id=access_plan['pk'],
        actor_employee_id=access_plan['employee_id'],
        actor_role_id=access_plan['role_id'],
        batch_id=batch_plan['pk'],
        version_id=batch_plan['arrival_roster_version_id'],
        watch_period_id=batch_plan['watch_period_id'],
        routing_row_ids=routing_row_ids,
        routing_event_ids=tuple(event['pk'] for event in routing_events),
        resident_ids=tuple(sorted({row['resident_id'] for row in routing_rows})),
        employee_ids=employee_ids,
        work_schedule_ids=work_schedule_ids,
        crew_plan_ids=crew_plan_ids,
        crew_plan_slot_ids=crew_plan_slot_ids,
        equipment_assignment_ids=tuple(sorted({
            event['equipment_assignment_id']
            for event in routing_events
            if event['equipment_assignment_id'] is not None
        })),
        phase_version_ids=phase_version_ids,
        phase_row_ids=_ordered_ids(
            WatchPeriodBrigadePhaseRow._base_manager.filter(
                version_id__in=phase_version_ids,
            )
        ),
        cohort_ids=cohort_ids,
        cohort_member_ids=_ordered_ids(
            SettlementCohortMember._base_manager.filter(cohort_id__in=cohort_ids)
        ),
    )


def _lock_exact(queryset, expected_ids, *, code=ERROR_PROVENANCE_INCONSISTENT):
    rows = list(queryset.select_for_update(of=('self',)).filter(pk__in=expected_ids).order_by('pk'))
    if tuple(row.pk for row in rows) != tuple(expected_ids):
        raise _creation_error(code, 'Состав серверного lock plan изменился.')
    return rows


def _lock_settlement_clerk_access(plan):
    actor = _lock_exact(
        Employee.objects.all(),
        (plan.actor_employee_id,),
        code=ERROR_ACCESS_NOT_FOUND,
    )[0]
    access = _lock_exact(
        EmployeeAccess.objects.select_related('employee', 'role'),
        (plan.access_id,),
        code=ERROR_ACCESS_NOT_FOUND,
    )[0]
    if access.employee_id != actor.pk or access.role_id != plan.actor_role_id:
        raise _creation_error(ERROR_ACCESS_NOT_FOUND, 'Точный доступ изменился.')
    if access.status == EmployeeAccess.Status.BLOCKED:
        raise _creation_error(ERROR_ACCESS_BLOCKED, 'Доступ делопроизводителя заблокирован.')
    if access.status != EmployeeAccess.Status.ACTIVATED or not access.is_active:
        raise _creation_error(ERROR_ACCESS_INACTIVE, 'Доступ делопроизводителя неактивен.')
    if actor.status != Employee.Status.ACTIVE or not actor.is_active:
        raise _creation_error(ERROR_ACCESS_INACTIVE, 'Сотрудник делопроизводителя неактивен.')
    if not access.role.is_active:
        raise _creation_error(ERROR_ACCESS_INACTIVE, 'Роль доступа неактивна.')
    if access.role.code != 'settlement_clerk':
        raise _creation_error(
            ERROR_ACCESS_WRONG_ROLE,
            'Точный доступ не принадлежит роли делопроизводителя.',
        )
    return actor, access


def _lock_routing_cohort_graph(plan):
    """Lock the preflight graph in writer-compatible deterministic order."""
    actor, access = _lock_settlement_clerk_access(plan)
    crew_plans = _lock_exact(CrewPlan.objects.all(), plan.crew_plan_ids)
    crew_plan_slots = _lock_exact(CrewPlanSlot.objects.all(), plan.crew_plan_slot_ids)
    employees = _lock_exact(Employee.objects.all(), plan.employee_ids)
    assignments = _lock_exact(
        EquipmentAssignment._base_manager.all(),
        plan.equipment_assignment_ids,
    )
    period = _lock_exact(WatchPeriod.objects.all(), (plan.watch_period_id,))[0]
    version = _lock_exact(
        ArrivalRosterVersion._base_manager.all(),
        (plan.version_id,),
    )[0]
    batch = _lock_exact(
        ArrivalRosterRoutingBatch._base_manager.all(),
        (plan.batch_id,),
    )[0]
    routing_rows = _lock_exact(
        ArrivalRosterRoutingRow._base_manager.all(),
        plan.routing_row_ids,
    )
    routing_events = _lock_exact(
        ArrivalRosterRoutingEvent._base_manager.all(),
        plan.routing_event_ids,
    )
    residents = _lock_exact(SettlementResident._base_manager.all(), plan.resident_ids)
    schedules = _lock_exact(WorkSchedule.objects.all(), plan.work_schedule_ids)
    phase_versions = _lock_exact(
        WatchPeriodBrigadePhaseVersion._base_manager.all(),
        plan.phase_version_ids,
    )
    phase_rows = _lock_exact(
        WatchPeriodBrigadePhaseRow._base_manager.all(),
        plan.phase_row_ids,
    )
    cohorts = _lock_exact(SettlementCohort._base_manager.all(), plan.cohort_ids)
    cohort_members = _lock_exact(
        SettlementCohortMember._base_manager.all(),
        plan.cohort_member_ids,
    )
    current_row_ids = _ordered_ids(
        ArrivalRosterRoutingRow._base_manager.filter(batch_id=plan.batch_id)
    )
    current_event_ids = _ordered_ids(
        ArrivalRosterRoutingEvent._base_manager.filter(
            routing_row_id__in=current_row_ids,
        )
    )
    current_schedule_ids = tuple(sorted({
        employee.work_schedule_id
        for employee in employees
        if employee.work_schedule_id is not None
    }))
    current_phase_version_ids = _ordered_ids(
        WatchPeriodBrigadePhaseVersion._base_manager.filter(
            watch_period_id=plan.watch_period_id,
            work_schedule_id__in=current_schedule_ids,
        )
    )
    current_phase_row_ids = _ordered_ids(
        WatchPeriodBrigadePhaseRow._base_manager.filter(
            version_id__in=current_phase_version_ids,
        )
    )
    current_cohort_ids = _ordered_ids(
        SettlementCohort._base_manager.filter(watch_period_id=plan.watch_period_id)
    )
    current_member_ids = _ordered_ids(
        SettlementCohortMember._base_manager.filter(cohort_id__in=current_cohort_ids)
    )
    if (
        current_row_ids != plan.routing_row_ids
        or current_event_ids != plan.routing_event_ids
        or tuple(sorted({row.resident_id for row in routing_rows})) != plan.resident_ids
        or tuple(sorted({
            row.employee_id for row in routing_rows if row.employee_id is not None
        })) != plan.employee_ids
        or current_schedule_ids != plan.work_schedule_ids
        or current_phase_version_ids != plan.phase_version_ids
        or current_phase_row_ids != plan.phase_row_ids
        or current_cohort_ids != plan.cohort_ids
        or current_member_ids != plan.cohort_member_ids
    ):
        raise _creation_error(
            ERROR_PROVENANCE_INCONSISTENT,
            'Состав exact графа изменился после построения lock plan.',
        )
    if (
        batch.arrival_roster_version_id != version.pk
        or batch.watch_period_id != period.pk
        or version.watch_period_id != period.pk
    ):
        raise _creation_error(
            ERROR_PROVENANCE_INCONSISTENT,
            'Передача, версия реестра и период больше не согласованы.',
        )
    return {
        'actor': actor,
        'access': access,
        'crew_plans': crew_plans,
        'crew_plan_slots': crew_plan_slots,
        'employees': employees,
        'assignments': assignments,
        'period': period,
        'version': version,
        'batch': batch,
        'routing_rows': routing_rows,
        'routing_events': routing_events,
        'residents': residents,
        'schedules': schedules,
        'phase_versions': phase_versions,
        'phase_rows': phase_rows,
        'cohorts': cohorts,
        'cohort_members': cohort_members,
    }


def _canonical_snapshot(snapshot):
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return snapshot, hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _canonical_fingerprint_or_none(snapshot):
    try:
        return _canonical_snapshot(snapshot)[1]
    except (TypeError, ValueError):
        return None


def _cohort_member_source_row(member):
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


def _validate_existing_routing_cohort(*, cohort, graph):
    members = [member for member in graph['cohort_members'] if member.cohort_id == cohort.pk]
    routing_rows = {row.pk: row for row in graph['routing_rows']}
    snapshot = cohort.source_snapshot
    try:
        expected_members = tuple(sorted(
            snapshot['members'],
            key=lambda item: item['routing_row_id'],
        ))
        excluded = tuple(snapshot['excluded_not_arriving_row_ids'])
        _snapshot, fingerprint = _canonical_snapshot(snapshot)
    except (KeyError, TypeError, ValueError):
        fingerprint = None
        expected_members = ()
        excluded = ()
    actual_members = tuple(sorted(
        (_cohort_member_source_row(member) for member in members),
        key=lambda item: item['routing_row_id'],
    ))
    expected_member_row_ids = {
        item.get('routing_row_id') for item in expected_members
        if isinstance(item, dict)
    }
    excluded_row_ids = set(excluded)
    consistent = (
        cohort.status == SettlementCohort.Status.APPROVED
        and cohort.source_revision_id is None
        and cohort.routing_batch_id == graph['batch'].pk
        and cohort.watch_period_id == graph['period'].pk
        and cohort.source_type == _ROUTING_SOURCE_TYPE
        and cohort.source_id == str(graph['batch'].pk)
        and isinstance(snapshot, dict)
        and snapshot.get('source_kind') == _ROUTING_SOURCE_TYPE
        and snapshot.get('routing_batch_id') == graph['batch'].pk
        and snapshot.get('arrival_roster_version_id') == graph['version'].pk
        and snapshot.get('watch_period_id') == graph['period'].pk
        and list(expected_members) == snapshot.get('members')
        and list(excluded) == sorted(set(excluded))
        and fingerprint == cohort.input_fingerprint
        and actual_members == expected_members
        and len(members) == len(expected_members)
        and cohort.created_by_id == cohort.approved_by_id
        and cohort.approved_at is not None
        and expected_member_row_ids.isdisjoint(excluded_row_ids)
        and expected_member_row_ids | excluded_row_ids == set(routing_rows)
        and all(
            routing_rows[row_id].route_state
            == ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING
            and (routing_rows[row_id].participation_snapshot or {}).get(
                'participation_status',
            ) == 'not_arriving'
            for row_id in excluded_row_ids
            if row_id in routing_rows
        )
        and excluded_row_ids <= set(routing_rows)
        and all(
            member.source_revision_id is None
            and member.routing_row_id is not None
            and member.routing_event_id is not None
            and member.brigade_phase_row_id is not None
            and member.routing_row.batch_id == graph['batch'].pk
            and member.routing_event.routing_row_id == member.routing_row_id
            and member.basis_type == _ROUTING_BASIS_TYPE
            and member.basis_id == str(member.routing_row_id)
            and member.shift_source_fingerprint
            == _canonical_fingerprint_or_none(member.shift_source_snapshot)
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
    if not consistent:
        raise _creation_error(
            ERROR_COHORT_ALREADY_INCONSISTENT,
            'Существующий состав для этой передачи повреждён или неполон.',
        )
    return cohort


def _snapshot_date(snapshot, field_name):
    value = (snapshot or {}).get(field_name)
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise _creation_error(
            ERROR_PROVENANCE_INCONSISTENT,
            'Даты строки передачи повреждены.',
        ) from error


def _aware_day(value):
    return timezone.make_aware(
        datetime.combine(value, time.min),
        timezone.get_current_timezone(),
    )


def _participation_status(routing_row):
    status = (routing_row.participation_snapshot or {}).get('participation_status')
    mapping = {
        'arriving': SettlementCohortMember.ParticipationStatus.PARTICIPATING,
        'extended': SettlementCohortMember.ParticipationStatus.EXTENDED,
        'additional': SettlementCohortMember.ParticipationStatus.ADDITIONAL,
    }
    try:
        return mapping[status]
    except KeyError as error:
        raise _creation_error(
            ERROR_PROVENANCE_INCONSISTENT,
            'Статус участия строки передачи не согласован с readiness.',
        ) from error


def _routing_member_snapshots(*, ready, row, event, phase_row, assignment):
    basis_snapshot = {
        'source_kind': _ROUTING_SOURCE_TYPE,
        'routing_row_id': row.pk,
        'routing_event_id': event.pk,
        'brigade_phase_row_id': phase_row.pk,
        'resident_id': row.resident_id,
        'employee_id': row.employee_id,
        'participation': row.participation_snapshot,
        'dates': row.dates_snapshot,
        'role': row.role_snapshot,
        'role_basis': row.role_basis_snapshot,
    }
    if assignment is None:
        shift_snapshot = {
            'kind': SettlementCohortMember.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE,
            'work_shift': ready.work_shift,
            'brigade_phase_row_id': phase_row.pk,
            'brigade_phase_version_id': phase_row.version_id,
            'crew_plan_slot_id': None,
            'equipment_assignment_id': None,
            'routing_event_id': event.pk,
        }
        shift_source_kind = SettlementCohortMember.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE
    else:
        shift_snapshot = {
            'kind': SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT,
            'work_shift': ready.work_shift,
            'brigade_phase_row_id': phase_row.pk,
            'brigade_phase_version_id': phase_row.version_id,
            'crew_plan_slot_id': event.crew_plan_slot_id,
            'equipment_assignment_id': assignment.pk,
            'routing_event_id': event.pk,
        }
        shift_source_kind = SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT
    shift_snapshot, shift_fingerprint = _canonical_snapshot(shift_snapshot)
    return basis_snapshot, shift_snapshot, shift_fingerprint, shift_source_kind


def _trusted_save_routing_cohort(cohort):
    cohort.full_clean()
    cohort.save()
    return cohort


def _trusted_save_routing_member(member):
    member.full_clean()
    member.save()
    return member


def _new_routing_member(*, cohort, ready, graph):
    rows = {row.pk: row for row in graph['routing_rows']}
    events = {event.pk: event for event in graph['routing_events']}
    residents = {resident.pk: resident for resident in graph['residents']}
    employees = {employee.pk: employee for employee in graph['employees']}
    phases = {row.pk: row for row in graph['phase_rows']}
    assignments = {assignment.pk: assignment for assignment in graph['assignments']}
    row = rows.get(ready.routing_row_id)
    event = events.get(ready.routing_event_id)
    resident = residents.get(ready.resident_id)
    employee = employees.get(ready.employee_id)
    phase_row = phases.get(ready.brigade_phase_row_id)
    assignment = assignments.get(ready.equipment_assignment_id)
    if (
        row is None
        or event is None
        or resident is None
        or employee is None
        or phase_row is None
        or row.batch_id != graph['batch'].pk
        or row.resident_id != resident.pk
        or row.employee_id != employee.pk
        or event.routing_row_id != row.pk
        or phase_row.version.watch_period_id != graph['period'].pk
        or phase_row.version.work_schedule_id != employee.work_schedule_id
        or phase_row.brigade_number != employee.brigade_number
        or phase_row.phase != ready.work_shift
        or ready.crew_plan_slot_id != event.crew_plan_slot_id
        or ready.equipment_assignment_id != event.equipment_assignment_id
        or (ready.equipment_assignment_id is not None and assignment is None)
    ):
        raise _creation_error(
            ERROR_PROVENANCE_INCONSISTENT,
            'Exact provenance готовой строки больше не согласована.',
        )
    arrival_on = _snapshot_date(row.dates_snapshot, 'arrival_on')
    departure_on = _snapshot_date(row.dates_snapshot, 'departure_on')
    if departure_on < arrival_on:
        raise _creation_error(ERROR_PROVENANCE_INCONSISTENT, 'Даты участия идут в неверном порядке.')
    participation = _participation_status(row)
    basis_snapshot, shift_snapshot, shift_fingerprint, shift_source_kind = (
        _routing_member_snapshots(
            ready=ready,
            row=row,
            event=event,
            phase_row=phase_row,
            assignment=assignment,
        )
    )
    return SettlementCohortMember(
        cohort=cohort,
        resident=resident,
        arrival_at=_aware_day(arrival_on),
        departure_at=_aware_day(departure_on + timedelta(days=1)),
        participation_status=participation,
        reason=(
            ''
            if participation == SettlementCohortMember.ParticipationStatus.PARTICIPATING
            else 'Статус перенесён из утверждённой передачи реестра.'
        ),
        expected_schedule_regime=employee.work_schedule.code,
        work_shift=ready.work_shift,
        shift_source_kind=shift_source_kind,
        official_equipment_assignment=assignment,
        shift_source_snapshot=shift_snapshot,
        shift_source_fingerprint=shift_fingerprint,
        source_revision=None,
        routing_row=row,
        routing_event=event,
        brigade_phase_row=phase_row,
        basis_type=_ROUTING_BASIS_TYPE,
        basis_id=str(row.pk),
        basis_snapshot=basis_snapshot,
        production_context_snapshot={
            'role': row.role_snapshot,
            'role_basis': row.role_basis_snapshot,
            'crew_plan_slot_id': ready.crew_plan_slot_id,
            'equipment_assignment_id': ready.equipment_assignment_id,
        },
    )


def _source_snapshot(*, graph, readiness):
    members = [
        {
            'routing_row_id': item.routing_row_id,
            'routing_event_id': item.routing_event_id,
            'brigade_phase_row_id': item.brigade_phase_row_id,
            'resident_id': item.resident_id,
            'employee_id': item.employee_id,
            'work_shift': item.work_shift,
            'crew_plan_slot_id': item.crew_plan_slot_id,
            'equipment_assignment_id': item.equipment_assignment_id,
        }
        for item in sorted(readiness.ready_members, key=lambda member: member.routing_row_id)
    ]
    snapshot = {
        'source_kind': _ROUTING_SOURCE_TYPE,
        'routing_batch_id': graph['batch'].pk,
        'arrival_roster_version_id': graph['version'].pk,
        'watch_period_id': graph['period'].pk,
        'members': members,
        'excluded_not_arriving_row_ids': sorted(readiness.excluded_not_arriving_row_ids),
    }
    return _canonical_snapshot(snapshot)


def _assert_complete_new_graph(*, cohort, readiness):
    members = list(
        SettlementCohortMember._base_manager.filter(cohort=cohort)
        .select_related('routing_row', 'routing_event')
        .order_by('routing_row_id')
    )
    expected = tuple(sorted(
        (
            item.routing_row_id,
            item.routing_event_id,
            item.brigade_phase_row_id,
            item.equipment_assignment_id,
        )
        for item in readiness.ready_members
    ))
    actual = tuple(
        (
            member.routing_row_id,
            member.routing_event_id,
            member.brigade_phase_row_id,
            member.official_equipment_assignment_id,
        )
        for member in members
    )
    if actual != expected or len(members) != len(readiness.ready_members):
        raise _creation_error(
            ERROR_COHORT_GRAPH_INCOMPLETE,
            'Состав создан не полностью.',
        )
    if any(
        member.source_revision_id is not None
        or member.routing_row.batch_id != cohort.routing_batch_id
        or member.routing_event.routing_row_id != member.routing_row_id
        for member in members
    ):
        raise _creation_error(
            ERROR_COHORT_GRAPH_INCONSISTENT,
            'Созданный состав содержит несогласованную provenance.',
        )


def _create_approved_arrival_roster_cohort_once(*, plan):
    graph = _lock_routing_cohort_graph(plan)
    existing = next(
        (cohort for cohort in graph['cohorts'] if cohort.routing_batch_id == graph['batch'].pk),
        None,
    )
    if existing is not None:
        return _validate_existing_routing_cohort(cohort=existing, graph=graph)

    try:
        readiness = build_arrival_roster_cohort_readiness(batch_id=graph['batch'].pk)
    except SettlementCohortReadinessError as error:
        if getattr(error, 'code', None) == ERROR_BATCH_NOT_FOUND:
            code = ERROR_BATCH_NOT_FOUND
        else:
            code = ERROR_PROVENANCE_INCONSISTENT
        raise _creation_error(code, str(error)) from error
    if not readiness.is_ready:
        blocker_codes = tuple(blocker.code for blocker in readiness.blockers)
        code = ERROR_ROUTING_STALE if any(
            blocker in {'batch_stale', 'routing_stale'} for blocker in blocker_codes
        ) else ERROR_COHORT_NOT_READY
        raise _creation_error(
            code,
            'Состав нельзя сформировать: не все строки передачи готовы.',
            blocker_codes=blocker_codes,
        )
    if readiness.batch_id != graph['batch'].pk or readiness.watch_period_id != graph['period'].pk:
        raise _creation_error(
            ERROR_PROVENANCE_INCONSISTENT,
            'Readiness относится к другой передаче или периоду.',
        )
    covered_row_ids = {
        item.routing_row_id for item in readiness.ready_members
    } | set(readiness.excluded_not_arriving_row_ids)
    if covered_row_ids != set(plan.routing_row_ids):
        raise _creation_error(
            ERROR_COHORT_GRAPH_INCOMPLETE,
            'Readiness не покрывает все строки exact batch.',
        )

    approved = [
        cohort for cohort in graph['cohorts']
        if cohort.status == SettlementCohort.Status.APPROVED
    ]
    if len(approved) > 1:
        raise _creation_error(
            ERROR_COHORT_GRAPH_INCONSISTENT,
            'Для периода найдено несколько утверждённых составов.',
        )
    previous = approved[0] if approved else None
    if graph['cohorts']:
        latest = max(graph['cohorts'], key=lambda cohort: (cohort.version, cohort.pk))
        if previous is None or latest.pk != previous.pk:
            raise _creation_error(
                ERROR_COHORT_GRAPH_INCONSISTENT,
                'Последовательность версий составов повреждена.',
            )
        version_number = previous.version + 1
    else:
        version_number = 1

    snapshot, fingerprint = _source_snapshot(graph=graph, readiness=readiness)
    cohort = _trusted_save_routing_cohort(SettlementCohort(
        watch_composition_id=graph['period'].watch_composition_id,
        watch_period=graph['period'],
        version=version_number,
        source_revision=None,
        routing_batch=graph['batch'],
        source_type=_ROUTING_SOURCE_TYPE,
        source_id=str(graph['batch'].pk),
        source_snapshot=snapshot,
        input_fingerprint=fingerprint,
        created_by=graph['actor'],
        supersedes=previous,
    ))
    for ready in sorted(readiness.ready_members, key=lambda item: item.routing_row_id):
        _trusted_save_routing_member(_new_routing_member(
            cohort=cohort,
            ready=ready,
            graph=graph,
        ))
    _assert_complete_new_graph(cohort=cohort, readiness=readiness)

    transition_at = timezone.now()
    if previous is not None:
        previous.status = SettlementCohort.Status.SUPERSEDED
        previous.superseded_at = transition_at
        previous.save(update_fields=['status', 'superseded_at', 'updated_at'])
    cohort.status = SettlementCohort.Status.APPROVED
    cohort.approved_by = graph['actor']
    cohort.approved_at = transition_at
    cohort.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    _assert_complete_new_graph(cohort=cohort, readiness=readiness)
    if SettlementCohort._base_manager.filter(
        watch_period=graph['period'], status=SettlementCohort.Status.APPROVED,
    ).count() != 1:
        raise _creation_error(
            ERROR_COHORT_GRAPH_INCONSISTENT,
            'После утверждения нарушена уникальность действующего состава.',
        )
    return cohort


def create_approved_arrival_roster_cohort(*, batch_id, actor_access_id):
    """Create and approve one complete cohort from one exact ready routing batch."""
    plan = _routing_cohort_lock_plan(
        batch_id=batch_id,
        actor_access_id=actor_access_id,
    )
    try:
        with transaction.atomic():
            return _create_approved_arrival_roster_cohort_once(plan=plan)
    except ArrivalRosterCohortCreationError:
        raise
    except IntegrityError as error:
        raise _creation_error(
            ERROR_COHORT_GRAPH_INCONSISTENT,
            'Состав конфликтует с уже сохранённым графом.',
        ) from error
    except ValidationError as error:
        raise _creation_error(
            ERROR_PROVENANCE_INCONSISTENT,
            'Серверные данные состава не прошли проверку целостности.',
        ) from error
