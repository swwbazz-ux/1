from collections import defaultdict
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery
from django.utils import timezone

from core.production_time import production_work_date
from shifts.models import WatchPeriod
from users.models import Employee
from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType

from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementCohortMember,
    SettlementPreviewApplication,
    SettlementPreviewRun,
    SettlementResident,
)
from .cohorts import resolve_internal_official_shift_source
from .control import (
    SettlementControlWriteContext,
    lock_settlement_write_access,
    lock_settlement_write_lease,
)
from .residents import (
    authoritative_resident_sex,
    build_settlement_resident_lock_plan,
    lock_settlement_residents_after_access,
)
from .validator import (
    ActualPlacementType,
    EffectivePlacementInterval,
    PlacementConflictValidationRequest,
    validate_placement_conflicts,
)


def _settlement_error(message, code):
    return ValidationError(message, code=code)


def effective_occupancy_at_q(moment):
    return (
        Q(replaced_by_application__isnull=True)
        & Q(replaced_by_occupancy__isnull=True)
        & Q(starts_at__lte=moment)
        & (Q(ends_at__isnull=True) | Q(ends_at__gt=moment))
        & (
            Q(terminated_at__isnull=True)
            | Q(terminated_at__gt=moment)
        )
    )


def _auto_settlement_preview_conflict(code, *, assignments=(), **details):
    return {
        'code': code,
        'equipment_assignments': tuple(assignments),
        **details,
    }


def _effective_auto_settlement_equipment_assignments(effective_date):
    return list(
        EquipmentAssignment.objects.filter(
            status=AssignmentStatus.ACCEPTED,
            role__isnull=False,
            shift__isnull=True,
            assigned_at__lte=effective_date,
        )
        .filter(Q(accepted_at__isnull=True) | Q(accepted_at__lte=effective_date))
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=effective_date))
        .select_related('employee', 'equipment', 'role')
        .order_by('employee_id', 'equipment_id', 'shift_type', 'pk')
    )


def _effective_anchor_bed_assignments(*, anchor_ids, effective_date):
    return list(
        AccommodationAnchorBedAssignment.objects
        .filter(
            anchor_id__in=anchor_ids,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            valid_from__lte=effective_date,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=effective_date))
        .select_related('anchor', 'physical_bed__room')
        .order_by('pk')
    )


def build_auto_settlement_preview(*, effective_date):
    """Calculate a non-persistent accommodation proposal from active work assignments."""
    if not isinstance(effective_date, datetime) or timezone.is_naive(effective_date):
        raise ValueError('effective_date must be an aware datetime.')

    effective_assignments = _effective_auto_settlement_equipment_assignments(
        effective_date,
    )
    conflicts = []
    conflicted_assignment_ids = set()

    assignments_by_employee = defaultdict(list)
    for assignment in effective_assignments:
        assignments_by_employee[assignment.employee_id].append(assignment)
    for employee_id in sorted(assignments_by_employee):
        assignments = assignments_by_employee[employee_id]
        if len(assignments) > 1:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'employee_multiple_effective_assignments',
                    assignments=assignments,
                    employee=assignments[0].employee,
                ),
            )
            conflicted_assignment_ids.update(item.pk for item in assignments)

    candidates = []
    for assignment in effective_assignments:
        if assignment.pk in conflicted_assignment_ids:
            continue
        if assignment.equipment_id is None:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'assignment_equipment_missing',
                    assignments=(assignment,),
                    employee=assignment.employee,
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)
            continue
        if assignment.shift_type not in WorkShiftType.values:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'assignment_shift_missing_or_invalid',
                    assignments=(assignment,),
                    employee=assignment.employee,
                    equipment=assignment.equipment,
                    shift_type=assignment.shift_type,
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)
            continue
        candidates.append(assignment)

    anchors_by_equipment = defaultdict(list)
    equipment_ids = {assignment.equipment_id for assignment in candidates}
    if equipment_ids:
        for anchor in AccommodationAnchor.objects.filter(
            equipment_id__in=equipment_ids,
            anchor_type=AccommodationAnchor.AnchorType.EQUIPMENT,
            status=AccommodationAnchor.Status.ACTIVE,
        ).order_by('pk'):
            anchors_by_equipment[anchor.equipment_id].append(anchor)

    assignments_by_anchor = defaultdict(list)
    anchor_ids = {
        anchor.pk
        for anchors in anchors_by_equipment.values()
        for anchor in anchors
    }
    if anchor_ids:
        for anchor_assignment in _effective_anchor_bed_assignments(
            anchor_ids=anchor_ids,
            effective_date=effective_date,
        ):
            assignments_by_anchor[anchor_assignment.anchor_id].append(anchor_assignment)

    provisional_rows = []
    for assignment in candidates:
        anchors = anchors_by_equipment[assignment.equipment_id]
        if not anchors:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'equipment_anchor_missing',
                    assignments=(assignment,),
                    employee=assignment.employee,
                    equipment=assignment.equipment,
                    shift_type=assignment.shift_type,
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)
            continue
        if len(anchors) != 1:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'equipment_anchor_ambiguous_for_shift',
                    assignments=(assignment,),
                    employee=assignment.employee,
                    equipment=assignment.equipment,
                    shift_type=assignment.shift_type,
                    accommodation_anchors=tuple(anchors),
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)
            continue

        anchor = anchors[0]
        anchor_bed_assignments = assignments_by_anchor[anchor.pk]
        if not anchor_bed_assignments:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'anchor_bed_assignment_missing',
                    assignments=(assignment,),
                    employee=assignment.employee,
                    equipment=assignment.equipment,
                    shift_type=assignment.shift_type,
                    accommodation_anchor=anchor,
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)
            continue
        if len(anchor_bed_assignments) != 1:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'anchor_bed_assignment_ambiguous',
                    assignments=(assignment,),
                    employee=assignment.employee,
                    equipment=assignment.equipment,
                    shift_type=assignment.shift_type,
                    accommodation_anchor=anchor,
                    anchor_bed_assignments=tuple(anchor_bed_assignments),
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)
            continue

        anchor_bed_assignment = anchor_bed_assignments[0]
        bed = anchor_bed_assignment.physical_bed
        room = bed.room
        if not bed.is_available:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'anchor_bed_unavailable',
                    assignments=(assignment,),
                    employee=assignment.employee,
                    equipment=assignment.equipment,
                    shift_type=assignment.shift_type,
                    accommodation_anchor=anchor,
                    room=room,
                    bed=bed,
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)
            continue
        provisional_rows.append({
            'employee': assignment.employee,
            'equipment_assignment': assignment,
            'equipment': assignment.equipment,
            'shift_type': assignment.shift_type,
            'accommodation_anchor': anchor,
            'anchor_bed_assignment': anchor_bed_assignment,
            'room': room,
            'bed': bed,
        })

    rows_by_bed_shift = defaultdict(list)
    for row in provisional_rows:
        rows_by_bed_shift[(row['bed'].pk, row['shift_type'])].append(row)
    for bed_id, shift_type in sorted(rows_by_bed_shift):
        rows = rows_by_bed_shift[(bed_id, shift_type)]
        if len(rows) > 1:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'bed_shift_capacity_conflict',
                    assignments=tuple(row['equipment_assignment'] for row in rows),
                    bed=rows[0]['bed'],
                    room=rows[0]['room'],
                    shift_type=shift_type,
                ),
            )
            conflicted_assignment_ids.update({
                row['equipment_assignment'].pk
                for row in rows
            })

    candidate_bed_ids = {
        row['bed'].pk
        for row in provisional_rows
        if row['equipment_assignment'].pk not in conflicted_assignment_ids
    }
    occupancies_by_bed = defaultdict(list)
    if candidate_bed_ids:
        for occupancy in (
            EmployeeBedOccupancy.objects
            .filter(effective_occupancy_at_q(effective_date), physical_bed_id__in=candidate_bed_ids)
            .select_related('resident__employee', 'physical_bed__room')
            .order_by('pk')
        ):
            occupancies_by_bed[occupancy.physical_bed_id].append(occupancy)

    successful_rows = []
    for row in provisional_rows:
        assignment = row['equipment_assignment']
        if assignment.pk in conflicted_assignment_ids:
            continue
        incompatible_occupancies = [
            occupancy
            for occupancy in occupancies_by_bed[row['bed'].pk]
            if occupancy.resident.employee_id != assignment.employee_id
        ]
        if incompatible_occupancies:
            conflicts.append(
                _auto_settlement_preview_conflict(
                    'bed_occupied_by_other_employee',
                    assignments=(assignment,),
                    employee=assignment.employee,
                    equipment=assignment.equipment,
                    shift_type=assignment.shift_type,
                    accommodation_anchor=row['accommodation_anchor'],
                    room=row['room'],
                    bed=row['bed'],
                    occupancies=tuple(incompatible_occupancies),
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)
            continue
        successful_rows.append(row)

    return {
        'effective_date': effective_date,
        'rows': tuple(successful_rows),
        'conflicts': tuple(conflicts),
        'summary': {
            'effective_assignment_count': len(effective_assignments),
            'success_count': len(successful_rows),
            'conflict_count': len(conflicts),
            'conflicted_assignment_count': len(conflicted_assignment_ids),
        },
    }


def _current_watch_period_groups(moment):
    """Group applicable periods by their canonical watch composition."""
    as_of = production_work_date(moment)
    return (
        WatchPeriod.objects
        .filter(
            is_active=True,
            watch_composition_id__isnull=False,
            watch_composition__is_active=True,
            starts_on__lte=as_of,
            ends_on__gte=as_of,
        )
        .order_by()
        .values('watch_composition_id')
        .annotate(period_count=Count('pk'))
    )


def current_roster_resolution(moment=None):
    """Describe whether today's canonical roster is usable or ambiguous."""
    moment = moment or timezone.now()
    groups = _current_watch_period_groups(moment)
    return {
        'has_unambiguous': groups.filter(period_count=1).exists(),
        'has_ambiguous': groups.filter(period_count__gt=1).exists(),
    }


def unsettled_current_roster_employees(moment=None):
    """Return active employees from unambiguous current watch rosters."""
    moment = moment or timezone.now()
    unambiguous_composition_ids = (
        _current_watch_period_groups(moment)
        .filter(period_count=1)
        .values('watch_composition_id')
    )
    effective_occupancy = EmployeeBedOccupancy.objects.filter(
        effective_occupancy_at_q(moment),
        resident__employee_id=OuterRef('pk'),
    )
    return (
        Employee.objects
        .filter(
            is_active=True,
            status=Employee.Status.ACTIVE,
            watch_composition__is_active=True,
            watch_composition_id__in=Subquery(
                unambiguous_composition_ids,
            ),
        )
        .annotate(
            has_effective_occupancy=Exists(effective_occupancy),
        )
        .filter(has_effective_occupancy=False)
        .select_related('personnel_position', 'watch_composition')
        .order_by('full_name', 'pk')
    )


def _validate_assignment_interval(*, assignment_type, starts_at, ends_at):
    if assignment_type not in EmployeeBedOccupancy.AssignmentType.values:
        raise _settlement_error(
            'Выберите тип закрепления.',
            'settlement_assignment_type_required',
        )
    if assignment_type == EmployeeBedOccupancy.AssignmentType.TEMPORARY:
        if ends_at is None:
            raise _settlement_error(
                'Для временного размещения укажите плановое окончание.',
                'settlement_temporary_end_required',
            )
    if ends_at is not None:
        if not isinstance(ends_at, datetime) or timezone.is_naive(ends_at):
            raise _settlement_error(
                'Плановое окончание размещения указано некорректно.',
                'settlement_ends_at_invalid',
            )
        if ends_at <= starts_at:
            raise _settlement_error(
                'Плановое окончание должно быть позже начала размещения.',
                'settlement_ends_at_not_after_start',
            )


def _validate_room_sex_restriction(*, resident, room):
    restriction = room.sex_restriction
    if restriction == PhysicalRoom.SexRestriction.UNKNOWN:
        return

    required_sex = {
        PhysicalRoom.SexRestriction.MALE_ONLY: Employee.Sex.MALE,
        PhysicalRoom.SexRestriction.FEMALE_ONLY: Employee.Sex.FEMALE,
    }.get(restriction)
    sex = authoritative_resident_sex(resident)
    if sex == Employee.Sex.UNKNOWN:
        raise _settlement_error(
            'Нельзя заселить сотрудника с неуказанным полом в комнату с ограничением по полу.',
            'settlement_employee_sex_unknown',
        )
    if sex != required_sex:
        raise _settlement_error(
            'Пол сотрудника не соответствует ограничению выбранной комнаты.',
            'settlement_room_sex_mismatch',
        )


def _normalized_employee_id(employee_id):
    if (
        isinstance(employee_id, bool)
        or not isinstance(employee_id, int)
        or employee_id <= 0
    ):
        raise _settlement_error(
            'Сотрудник не найден.',
            'settlement_employee_not_found',
        )
    return employee_id


def _employee_exists(*, employee_id, using):
    if not Employee.objects.using(using).filter(pk=employee_id).exists():
        raise _settlement_error(
            'Сотрудник не найден.',
            'settlement_employee_not_found',
        )


def _normalized_resident_id(resident_id):
    if (
        isinstance(resident_id, bool)
        or not isinstance(resident_id, int)
        or resident_id <= 0
    ):
        raise _settlement_error(
            'Жилец не найден.',
            'settlement_resident_not_found',
        )
    return resident_id


def _normalized_occupancy_id(occupancy_id):
    if (
        isinstance(occupancy_id, bool)
        or not isinstance(occupancy_id, int)
        or occupancy_id <= 0
    ):
        raise _settlement_error(
            'Размещение не найдено.',
            'settlement.manual.occupancy_not_found',
        )
    return occupancy_id


def _manual_application_snapshot(*, resident_id, moment, using):
    today = timezone.localdate(moment)
    return tuple(
        SettlementPreviewApplication._base_manager.using(using)
        .filter(
            legacy_whole_run=False,
            preview_run__status=SettlementPreviewRun.Status.CONFIRMED,
            watch_period__is_active=True,
            cohort__members__resident_id=resident_id,
            cohort__members__participation_status__in=(
                SettlementCohortMember.ACTIVE_PARTICIPATION_STATUSES
            ),
            cohort__members__work_shift=F('work_shift'),
        )
        .filter(
            Q(
                work_shift=SettlementCohortMember.WorkShift.DAY,
                watch_period__starts_on__lte=today,
                watch_period__ends_on__gte=today,
            )
            | Q(
                work_shift=SettlementCohortMember.WorkShift.NIGHT,
                watch_period__starts_on__lte=today + timedelta(days=1),
                watch_period__ends_on__gte=today,
            )
        )
        .values_list(
            'pk', 'watch_period_id', 'cohort_id', 'preview_run_id',
            'work_shift', 'cohort__members__pk',
        )
        .order_by('pk')
    )


def _lock_manual_application_context(
    *,
    resident,
    application_snapshot,
    moment,
    using,
):
    if not resident.employee_id:
        raise _settlement_error(
            'Ручное заселение внешнего жильца ещё не подключено.',
            'settlement.manual.external_unavailable',
        )
    if not application_snapshot:
        raise _settlement_error(
            'Соответствующая смена ещё не применена.',
            'settlement.manual.shift_not_applied',
        )
    if len(application_snapshot) != 1:
        raise _settlement_error(
            'Требуется проверка периода вахты.',
            'settlement.manual.period_ambiguous',
        )

    application_id, period_id, _cohort_id, _run_id, work_shift, member_id = (
        application_snapshot[0]
    )
    period = (
        WatchPeriod.objects.using(using)
        .select_for_update(of=('self',))
        .get(pk=period_id)
    )
    application = (
        SettlementPreviewApplication._base_manager.using(using)
        .select_for_update(of=('self',))
        .get(pk=application_id)
    )
    if _manual_application_snapshot(
        resident_id=resident.pk,
        moment=moment,
        using=using,
    ) != application_snapshot:
        raise _settlement_error(
            'Состояние применённой смены изменилось. Повторите действие.',
            'settlement.manual.period_stale',
        )
    member = (
        SettlementCohortMember._base_manager.using(using)
        .select_related('resident')
        .get(pk=member_id)
    )
    source = resolve_internal_official_shift_source(
        resident=resident,
        period=period,
        expected_assignment_id=member.official_equipment_assignment_id,
        require_expected_assignment=True,
    )
    if (
        source['work_shift'] != work_shift
        or member.resident_id != resident.pk
        or member.cohort_id != application.cohort_id
        or member.work_shift != application.work_shift
        or member.shift_source_fingerprint != source['shift_source_fingerprint']
    ):
        raise _settlement_error(
            'Требуется проверка официальной смены сотрудника.',
            'settlement.cohort.shift_review_required',
        )
    return application, period, member, source


def _occupancy_snapshot_by_id(*, occupancy_id, using):
    snapshot = (
        EmployeeBedOccupancy._base_manager.using(using)
        .filter(pk=occupancy_id)
        .values_list(
            'pk', 'resident_id', 'physical_bed_id', 'watch_period_id',
            'cohort_member_id', 'work_shift', 'shift_source_kind',
            'shift_source_fingerprint', 'shift_official_assignment_id',
            'source_application_id', 'starts_at', 'ends_at', 'terminated_at',
            'ended_at', 'replaced_by_application_id', 'replaced_by_occupancy_id',
        )
        .first()
    )
    if snapshot is None:
        raise _settlement_error(
            'Размещение не найдено.',
            'settlement.manual.occupancy_not_found',
        )
    return snapshot


def _lock_occupancy_context_rows(*, snapshot, using):
    period_id = snapshot[3]
    application_id = snapshot[9]
    if period_id is not None:
        WatchPeriod.objects.using(using).select_for_update(of=('self',)).get(pk=period_id)
    if application_id is not None:
        SettlementPreviewApplication._base_manager.using(using).select_for_update(
            of=('self',),
        ).get(pk=application_id)


def _official_manual_source_for_occupancy(*, occupancy, resident):
    if (
        occupancy.work_shift not in SettlementCohortMember.WorkShift.values
        or occupancy.watch_period_id is None
        or occupancy.shift_source_kind
        == EmployeeBedOccupancy.ShiftSourceKind.UNVERIFIED_LEGACY
    ):
        raise _settlement_error(
            'Историческое размещение без проверенной смены нельзя переселить.',
            'settlement.manual.legacy_relocation_forbidden',
        )
    if not resident.employee_id:
        raise _settlement_error(
            'Ручное переселение внешнего жильца ещё не подключено.',
            'settlement.manual.external_unavailable',
        )
    expected_assignment_id = occupancy.shift_official_assignment_id
    if expected_assignment_id is None and occupancy.cohort_member_id is not None:
        expected_assignment_id = occupancy.cohort_member.official_equipment_assignment_id
    source = resolve_internal_official_shift_source(
        resident=resident,
        period=occupancy.watch_period,
        expected_assignment_id=expected_assignment_id,
        require_expected_assignment=True,
    )
    if (
        source['work_shift'] != occupancy.work_shift
        or source['shift_source_fingerprint'] != occupancy.shift_source_fingerprint
    ):
        raise _settlement_error(
            'Требуется проверка официальной смены сотрудника.',
            'settlement.cohort.shift_review_required',
        )
    return source


def _resident_id_for_employee(*, employee_id, using):
    employee_id = _normalized_employee_id(employee_id)
    _employee_exists(employee_id=employee_id, using=using)
    subject = tuple(
        SettlementResident.objects.using(using)
        .filter(employee_id=employee_id)
        .values_list('pk', 'resident_type')
    )
    if len(subject) != 1 or subject[0][1] != SettlementResident.ResidentType.EMPLOYEE:
        raise _settlement_error(
            'Карточка жильца для сотрудника не найдена.',
            'settlement_resident_not_found',
        )
    return subject[0][0]


def _bed_snapshot(*, bed_stable_id, using):
    try:
        return (
            PhysicalBed.objects.using(using)
            .filter(stable_id=bed_stable_id)
            .values('pk', 'room_id', 'stable_id')
            .get()
        )
    except PhysicalBed.DoesNotExist as error:
        raise _settlement_error(
            'Койко-место не найдено.',
            'settlement_bed_not_found',
        ) from error


def _locked_beds(*bed_ids, using):
    beds = list(
        PhysicalBed.objects.using(using)
        .select_for_update(of=('self',))
        .select_related('room', 'room__dormitory')
        .filter(pk__in=bed_ids)
        .order_by('pk')
    )
    if len(beds) != len(set(bed_ids)):
        raise _settlement_error(
            'Койко-место не найдено.',
            'settlement_bed_not_found',
        )
    return {bed.pk: bed for bed in beds}


def _locked_rooms(*room_ids, using):
    rooms = list(
        PhysicalRoom.objects.using(using)
        .select_for_update(of=('self',))
        .filter(pk__in=room_ids)
        .order_by('pk')
    )
    if len(rooms) != len(set(room_ids)):
        raise _settlement_error(
            'Комната не найдена.',
            'settlement_room_not_found',
        )
    return {room.pk: room for room in rooms}


def _validate_resident_state(*, resident):
    if resident.status != SettlementResident.Status.ACTIVE:
        raise _settlement_error(
            'Для заселения доступен только активный жилец.',
            'settlement_resident_inactive',
        )
    if resident.employee_id:
        employee = resident.employee
        if not employee.is_active or employee.status != Employee.Status.ACTIVE:
            raise _settlement_error(
                'Для заселения доступен только активный сотрудник.',
                'settlement_employee_inactive',
            )


def _validate_resident_and_destination(*, resident, room):
    if room.transfer_status != PhysicalRoom.TransferStatus.TRANSFERRED:
        raise _settlement_error(
            'Комната не передана для расселения.',
            'settlement_room_not_transferred',
        )
    _validate_resident_state(resident=resident)
    _validate_room_sex_restriction(resident=resident, room=room)


def _locked_related_occupancies(*, resident_id, bed_ids, using):
    return list(
        EmployeeBedOccupancy.objects.using(using)
        .select_for_update(of=('self',))
        .filter(Q(resident_id=resident_id) | Q(physical_bed_id__in=bed_ids))
        .select_related('physical_bed')
        .order_by('pk')
    )


def _related_occupancy_snapshot(*, resident_id, bed_ids, using):
    return tuple(
        EmployeeBedOccupancy.objects.using(using)
        .filter(Q(resident_id=resident_id) | Q(physical_bed_id__in=bed_ids))
        .values_list('pk', 'resident_id', 'physical_bed_id')
        .order_by('pk')
    )


def _occupancy_identity(rows):
    return tuple(
        (row.pk, row.resident_id, row.physical_bed_id)
        for row in rows
    )


def _validate_occupancy_conflicts(
    *,
    resident_id,
    bed,
    starts_at,
    ends_at,
    persisted_occupancies,
    current_occupancy_id=None,
):
    effective_placement_intervals = tuple(
        EffectivePlacementInterval(
            occupancy_id=occupancy.pk,
            employee_id=occupancy.resident_id,
            bed_id=occupancy.physical_bed_id,
            placement_type=ActualPlacementType(occupancy.assignment_type),
            starts_at=occupancy.starts_at,
            ends_at=occupancy.ends_at,
            bed_stable_id=occupancy.physical_bed.stable_id,
            terminated_at=occupancy.terminated_at,
        )
        for occupancy in persisted_occupancies
    )
    conflict_result = validate_placement_conflicts(
        PlacementConflictValidationRequest(
            employee_id=resident_id,
            bed_stable_id=bed.stable_id,
            starts_at=starts_at,
            ends_at=ends_at,
            terminated_at=None,
            current_occupancy_id=current_occupancy_id,
            effective_placement_intervals=effective_placement_intervals,
        )
    )
    if conflict_result.blocks:
        raise ValidationError([
            ValidationError(block.message, code=block.code)
            for block in conflict_result.blocks
        ])


def _create_occupancy(
    *,
    resident,
    bed,
    assignment_type,
    starts_at,
    ends_at,
    settled_by,
    using,
    source_kind=EmployeeBedOccupancy.SourceKind.MANUAL,
    source_application=None,
    source_preview_placement=None,
    watch_period=None,
    cohort_member=None,
    work_shift=None,
    shift_source_kind=EmployeeBedOccupancy.ShiftSourceKind.UNVERIFIED_LEGACY,
    shift_source_fingerprint='',
    shift_official_assignment=None,
    shift_selected_by_access=None,
    shift_selected_at=None,
    shift_selection_basis='',
):
    return EmployeeBedOccupancy.objects.using(using).create(
        resident=resident,
        physical_bed=bed,
        assignment_type=assignment_type,
        source_kind=source_kind,
        source_application=source_application,
        source_preview_placement=source_preview_placement,
        watch_period=watch_period,
        cohort_member=cohort_member,
        work_shift=work_shift,
        shift_source_kind=shift_source_kind,
        shift_source_fingerprint=shift_source_fingerprint,
        shift_official_assignment=shift_official_assignment,
        shift_selected_by_access=shift_selected_by_access,
        shift_selected_at=shift_selected_at,
        shift_selection_basis=shift_selection_basis,
        settled_at=starts_at,
        starts_at=starts_at,
        ends_at=ends_at,
        terminated_at=None,
        settled_by=settled_by,
        ended_at=None,
    )


def settle_resident_on_bed(
    *,
    bed_stable_id,
    resident_id,
    assignment_type,
    control_context=None,
    ends_at=None,
):
    using = router.db_for_write(EmployeeBedOccupancy)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        resident_id = _normalized_resident_id(resident_id)
        resident_plan = build_settlement_resident_lock_plan(
            resident_ids=(resident_id,),
            require_active=False,
            using=using,
        )
        moment = timezone.now()
        application_snapshot = _manual_application_snapshot(
            resident_id=resident_id,
            moment=moment,
            using=using,
        )
        bed_snapshot = _bed_snapshot(
            bed_stable_id=bed_stable_id,
            using=using,
        )
        occupancy_snapshot = _related_occupancy_snapshot(
            resident_id=resident_id,
            bed_ids=(bed_snapshot['pk'],),
            using=using,
        )
        locked_rows = lock_settlement_write_access(
            lease=lease,
            control_context=control_context,
            employee_ids=resident_plan.employee_ids,
            using=using,
        )
        owner_access = locked_rows.access_by_id(control_context.owner_access_id)
        settled_by = locked_rows.employee_by_id(owner_access.employee_id)
        resident_rows = lock_settlement_residents_after_access(
            resident_plan,
            locked_employees=locked_rows.employees,
        )
        resident = resident_rows.resident_by_id(resident_id)
        _validate_resident_state(resident=resident)
        _application, period, member, shift_source = _lock_manual_application_context(
            resident=resident,
            application_snapshot=application_snapshot,
            moment=moment,
            using=using,
        )

        bed_id = bed_snapshot['pk']
        bed = _locked_beds(bed_id, using=using)[bed_id]
        if (
            bed.stable_id != bed_snapshot['stable_id']
            or bed.room_id != bed_snapshot['room_id']
        ):
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        room = _locked_rooms(bed.room_id, using=using)[bed.room_id]
        _validate_resident_and_destination(resident=resident, room=room)
        _validate_assignment_interval(
            assignment_type=assignment_type,
            starts_at=moment,
            ends_at=ends_at,
        )
        persisted_occupancies = _locked_related_occupancies(
            resident_id=resident.pk,
            bed_ids=(bed.pk,),
            using=using,
        )
        if _occupancy_identity(persisted_occupancies) != occupancy_snapshot:
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        _validate_occupancy_conflicts(
            resident_id=resident.pk,
            bed=bed,
            starts_at=moment,
            ends_at=ends_at,
            persisted_occupancies=persisted_occupancies,
        )

        try:
            with transaction.atomic(using=using):
                occupancy = _create_occupancy(
                    resident=resident,
                    bed=bed,
                    assignment_type=assignment_type,
                    starts_at=moment,
                    ends_at=ends_at,
                    settled_by=settled_by,
                    using=using,
                    watch_period=period,
                    cohort_member=member,
                    work_shift=shift_source['work_shift'],
                    shift_source_kind=(
                        EmployeeBedOccupancy.ShiftSourceKind.OFFICIAL_ASSIGNMENT
                    ),
                    shift_source_fingerprint=(
                        shift_source['shift_source_fingerprint']
                    ),
                    shift_official_assignment=(
                        shift_source['official_equipment_assignment']
                    ),
                )
        except IntegrityError as error:
            if EmployeeBedOccupancy.objects.using(using).filter(
                effective_occupancy_at_q(moment),
                physical_bed=bed,
            ).exists():
                raise _settlement_error(
                    'Койко-место уже занято.',
                    'settlement_bed_occupied',
                ) from error
            if EmployeeBedOccupancy.objects.using(using).filter(
                effective_occupancy_at_q(moment),
                resident=resident,
            ).exists():
                raise _settlement_error(
                    'Жилец уже занимает другое активное место.',
                    'settlement_employee_already_housed',
                ) from error
            raise

    return occupancy


def relocate_resident_to_bed(
    *,
    bed_stable_id,
    occupancy_id,
    control_context=None,
):
    using = router.db_for_write(EmployeeBedOccupancy)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        occupancy_id = _normalized_occupancy_id(occupancy_id)
        occupancy_snapshot = _occupancy_snapshot_by_id(
            occupancy_id=occupancy_id,
            using=using,
        )
        resident_id = occupancy_snapshot[1]
        current_bed_id = occupancy_snapshot[2]
        resident_plan = build_settlement_resident_lock_plan(
            resident_ids=(resident_id,),
            require_active=False,
            using=using,
        )
        moment = timezone.now()
        target_bed_snapshot = _bed_snapshot(
            bed_stable_id=bed_stable_id,
            using=using,
        )
        target_bed_id = target_bed_snapshot['pk']
        related_snapshot = _related_occupancy_snapshot(
            resident_id=resident_id,
            bed_ids=(target_bed_id,),
            using=using,
        )
        locked_rows = lock_settlement_write_access(
            lease=lease,
            control_context=control_context,
            employee_ids=resident_plan.employee_ids,
            using=using,
        )
        owner_access = locked_rows.access_by_id(control_context.owner_access_id)
        settled_by = locked_rows.employee_by_id(owner_access.employee_id)
        resident_rows = lock_settlement_residents_after_access(
            resident_plan,
            locked_employees=locked_rows.employees,
        )
        resident = resident_rows.resident_by_id(resident_id)
        _lock_occupancy_context_rows(snapshot=occupancy_snapshot, using=using)
        if target_bed_id == current_bed_id:
            raise _settlement_error(
                'Нельзя переселить сотрудника на то же койко-место.',
                'settlement_relocation_same_bed',
            )
        beds = _locked_beds(current_bed_id, target_bed_id, using=using)
        target_bed = beds[target_bed_id]
        if target_bed.stable_id != target_bed_snapshot['stable_id']:
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        rooms = _locked_rooms(
            *(bed.room_id for bed in beds.values()),
            using=using,
        )
        target_room = rooms[target_bed.room_id]
        _validate_resident_and_destination(resident=resident, room=target_room)
        persisted_occupancies = _locked_related_occupancies(
            resident_id=resident.pk,
            bed_ids=(target_bed.pk,),
            using=using,
        )
        if _occupancy_identity(persisted_occupancies) != related_snapshot:
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        current_occupancy = next(
            (row for row in persisted_occupancies if row.pk == occupancy_id),
            None,
        )
        if (
            current_occupancy is None
            or _occupancy_snapshot_by_id(occupancy_id=occupancy_id, using=using)
            != occupancy_snapshot
        ):
            raise _settlement_error(
                'Размещение изменилось или уже закрыто.',
                'settlement.manual.occupancy_stale',
            )
        if not current_occupancy.is_active_at(moment):
            raise _settlement_error(
                'Размещение изменилось или уже закрыто.',
                'settlement.manual.occupancy_stale',
            )
        if current_occupancy.starts_at >= moment:
            raise _settlement_error(
                'Переселение невозможно в момент начала текущего размещения.',
                'settlement_relocation_same_moment',
            )
        shift_source = _official_manual_source_for_occupancy(
            occupancy=current_occupancy,
            resident=resident,
        )
        _validate_assignment_interval(
            assignment_type=current_occupancy.assignment_type,
            starts_at=moment,
            ends_at=current_occupancy.ends_at,
        )
        _validate_occupancy_conflicts(
            resident_id=resident.pk,
            bed=target_bed,
            starts_at=moment,
            ends_at=current_occupancy.ends_at,
            persisted_occupancies=persisted_occupancies,
            current_occupancy_id=current_occupancy.pk,
        )

        current_occupancy.terminated_at = moment
        current_occupancy.save(update_fields=['terminated_at'])
        occupancy = _create_occupancy(
            resident=resident,
            bed=target_bed,
            assignment_type=current_occupancy.assignment_type,
            starts_at=moment,
            ends_at=current_occupancy.ends_at,
            settled_by=settled_by,
            using=using,
            watch_period=current_occupancy.watch_period,
            cohort_member=current_occupancy.cohort_member,
            work_shift=current_occupancy.work_shift,
            shift_source_kind=EmployeeBedOccupancy.ShiftSourceKind.OFFICIAL_ASSIGNMENT,
            shift_source_fingerprint=shift_source['shift_source_fingerprint'],
            shift_official_assignment=shift_source['official_equipment_assignment'],
        )
        current_occupancy.replaced_by_occupancy = occupancy
        current_occupancy.save(update_fields=['replaced_by_occupancy'])

    return occupancy


def release_resident_from_bed(
    *,
    occupancy_id,
    bed_stable_id=None,
    control_context=None,
):
    using = router.db_for_write(EmployeeBedOccupancy)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        occupancy_id = _normalized_occupancy_id(occupancy_id)
        occupancy_snapshot = _occupancy_snapshot_by_id(
            occupancy_id=occupancy_id,
            using=using,
        )
        resident_id = occupancy_snapshot[1]
        bed_id = occupancy_snapshot[2]
        resident_plan = build_settlement_resident_lock_plan(
            resident_ids=(resident_id,),
            require_active=False,
            using=using,
        )
        moment = timezone.now()
        locked_rows = lock_settlement_write_access(
            lease=lease,
            control_context=control_context,
            employee_ids=resident_plan.employee_ids,
            using=using,
        )
        lock_settlement_residents_after_access(
            resident_plan,
            locked_employees=locked_rows.employees,
        )
        _lock_occupancy_context_rows(snapshot=occupancy_snapshot, using=using)

        bed = _locked_beds(bed_id, using=using)[bed_id]
        if bed_stable_id not in (None, '', str(bed.stable_id), bed.stable_id):
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        _locked_rooms(bed.room_id, using=using)
        occupancy = (
            EmployeeBedOccupancy._base_manager.using(using)
            .select_for_update(of=('self',))
            .filter(pk=occupancy_id)
            .first()
        )
        if (
            occupancy is None
            or _occupancy_snapshot_by_id(occupancy_id=occupancy_id, using=using)
            != occupancy_snapshot
            or not occupancy.is_active_at(moment)
        ):
            raise _settlement_error(
                'Размещение изменилось или уже закрыто.',
                'settlement.manual.occupancy_stale',
            )
        if occupancy.starts_at >= moment:
            raise _settlement_error(
                'Освобождение невозможно в момент начала размещения.',
                'settlement_release_same_moment',
            )
        occupancy.terminated_at = moment
        occupancy.save(update_fields=['terminated_at'])

    return occupancy


def settle_employee_on_bed(
    *,
    bed_stable_id,
    employee_id,
    assignment_type,
    control_context=None,
    ends_at=None,
):
    using = router.db_for_write(EmployeeBedOccupancy)
    resident_id = _resident_id_for_employee(employee_id=employee_id, using=using)
    return settle_resident_on_bed(
        bed_stable_id=bed_stable_id,
        resident_id=resident_id,
        assignment_type=assignment_type,
        control_context=control_context,
        ends_at=ends_at,
    )


def relocate_employee_to_bed(
    *,
    bed_stable_id,
    occupancy_id,
    control_context=None,
):
    return relocate_resident_to_bed(
        bed_stable_id=bed_stable_id,
        occupancy_id=occupancy_id,
        control_context=control_context,
    )


def release_employee_from_bed(
    *,
    occupancy_id,
    bed_stable_id=None,
    control_context=None,
):
    return release_resident_from_bed(
        occupancy_id=occupancy_id,
        bed_stable_id=bed_stable_id,
        control_context=control_context,
    )
