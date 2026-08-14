from collections import defaultdict
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q, Subquery
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
        Q(starts_at__lte=moment)
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
            .select_related('employee', 'physical_bed__room')
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
            if occupancy.employee_id != assignment.employee_id
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
        employee_id=OuterRef('pk'),
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


def _validate_room_sex_restriction(*, employee, room):
    restriction = room.sex_restriction
    if restriction == PhysicalRoom.SexRestriction.UNKNOWN:
        return

    required_sex = {
        PhysicalRoom.SexRestriction.MALE_ONLY: Employee.Sex.MALE,
        PhysicalRoom.SexRestriction.FEMALE_ONLY: Employee.Sex.FEMALE,
    }.get(restriction)
    if employee.sex == Employee.Sex.UNKNOWN:
        raise _settlement_error(
            'Нельзя заселить сотрудника с неуказанным полом в комнату с ограничением по полу.',
            'settlement_employee_sex_unknown',
        )
    if employee.sex != required_sex:
        raise _settlement_error(
            'Пол сотрудника не соответствует ограничению выбранной комнаты.',
            'settlement_room_sex_mismatch',
        )


def _locked_employee(employee_id):
    try:
        return (
            Employee.objects
            .select_for_update(of=('self',))
            .select_related('personnel_position')
            .get(pk=employee_id)
        )
    except (Employee.DoesNotExist, TypeError, ValueError) as error:
        raise _settlement_error(
            'Сотрудник не найден.',
            'settlement_employee_not_found',
        ) from error


def _locked_beds(*bed_ids):
    beds = list(
        PhysicalBed.objects
        .select_for_update()
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


def _locked_rooms(*room_ids):
    rooms = list(
        PhysicalRoom.objects
        .select_for_update()
        .filter(pk__in=room_ids)
        .order_by('pk')
    )
    if len(rooms) != len(set(room_ids)):
        raise _settlement_error(
            'Комната не найдена.',
            'settlement_room_not_found',
        )
    return {room.pk: room for room in rooms}


def _validate_employee_and_destination(*, employee, room):
    if room.transfer_status != PhysicalRoom.TransferStatus.TRANSFERRED:
        raise _settlement_error(
            'Комната не передана для расселения.',
            'settlement_room_not_transferred',
        )
    if not employee.is_active or employee.status != Employee.Status.ACTIVE:
        raise _settlement_error(
            'Для заселения доступен только активный сотрудник.',
            'settlement_employee_inactive',
        )
    _validate_room_sex_restriction(employee=employee, room=room)


def _locked_related_occupancies(*, employee_id, bed_ids):
    return list(
        EmployeeBedOccupancy.objects
        .select_for_update()
        .filter(Q(employee_id=employee_id) | Q(physical_bed_id__in=bed_ids))
        .select_related('physical_bed')
        .order_by('pk')
    )


def _validate_occupancy_conflicts(
    *,
    employee_id,
    bed,
    starts_at,
    ends_at,
    persisted_occupancies,
    current_occupancy_id=None,
):
    effective_placement_intervals = tuple(
        EffectivePlacementInterval(
            occupancy_id=occupancy.pk,
            employee_id=occupancy.employee_id,
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
            employee_id=employee_id,
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


def _create_occupancy(*, employee, bed, assignment_type, starts_at, ends_at, settled_by):
    return EmployeeBedOccupancy.objects.create(
        employee=employee,
        physical_bed=bed,
        assignment_type=assignment_type,
        settled_at=starts_at,
        starts_at=starts_at,
        ends_at=ends_at,
        terminated_at=None,
        settled_by=settled_by,
        ended_at=None,
    )


def settle_employee_on_bed(
    *,
    bed_stable_id,
    employee_id,
    assignment_type,
    settled_by,
    ends_at=None,
):
    with transaction.atomic():
        employee = _locked_employee(employee_id)
        try:
            bed_id = (
                PhysicalBed.objects
                .filter(stable_id=bed_stable_id)
                .values_list('pk', flat=True)
                .get()
            )
        except PhysicalBed.DoesNotExist as error:
            raise _settlement_error(
                'Койко-место не найдено.',
                'settlement_bed_not_found',
            ) from error
        bed = _locked_beds(bed_id)[bed_id]
        room = _locked_rooms(bed.room_id)[bed.room_id]
        _validate_employee_and_destination(employee=employee, room=room)
        moment = timezone.now()
        _validate_assignment_interval(
            assignment_type=assignment_type,
            starts_at=moment,
            ends_at=ends_at,
        )
        persisted_occupancies = _locked_related_occupancies(
            employee_id=employee.pk,
            bed_ids=(bed.pk,),
        )
        _validate_occupancy_conflicts(
            employee_id=employee.pk,
            bed=bed,
            starts_at=moment,
            ends_at=ends_at,
            persisted_occupancies=persisted_occupancies,
        )

        try:
            with transaction.atomic():
                occupancy = _create_occupancy(
                    employee=employee,
                    bed=bed,
                    assignment_type=assignment_type,
                    starts_at=moment,
                    ends_at=ends_at,
                    settled_by=settled_by,
                )
        except IntegrityError as error:
            if EmployeeBedOccupancy.objects.filter(
                effective_occupancy_at_q(moment),
                physical_bed=bed,
            ).exists():
                raise _settlement_error(
                    'Койко-место уже занято.',
                    'settlement_bed_occupied',
                ) from error
            if EmployeeBedOccupancy.objects.filter(
                effective_occupancy_at_q(moment),
                employee=employee,
            ).exists():
                raise _settlement_error(
                    'Сотрудник уже занимает другое активное место.',
                    'settlement_employee_already_housed',
                ) from error
            raise

    return occupancy


def relocate_employee_to_bed(
    *,
    bed_stable_id,
    employee_id,
    assignment_type,
    settled_by,
    ends_at=None,
):
    with transaction.atomic():
        employee = _locked_employee(employee_id)
        moment = timezone.now()
        active_occupancies = list(
            EmployeeBedOccupancy.objects
            .select_for_update()
            .filter(effective_occupancy_at_q(moment), employee=employee)
            .select_related('physical_bed')
            .order_by('pk')
        )
        if not active_occupancies:
            raise _settlement_error(
                'У сотрудника нет действующего размещения для переселения.',
                'settlement_employee_not_housed',
            )
        if len(active_occupancies) != 1:
            raise _settlement_error(
                'У сотрудника найдено несколько действующих размещений.',
                'settlement_employee_multiple_active_occupancies',
            )
        current_occupancy = active_occupancies[0]
        try:
            target_bed_id = (
                PhysicalBed.objects
                .filter(stable_id=bed_stable_id)
                .values_list('pk', flat=True)
                .get()
            )
        except PhysicalBed.DoesNotExist as error:
            raise _settlement_error(
                'Койко-место не найдено.',
                'settlement_bed_not_found',
            ) from error
        if target_bed_id == current_occupancy.physical_bed_id:
            raise _settlement_error(
                'Нельзя переселить сотрудника на то же койко-место.',
                'settlement_relocation_same_bed',
            )

        beds = _locked_beds(current_occupancy.physical_bed_id, target_bed_id)
        target_bed = beds[target_bed_id]
        rooms = _locked_rooms(*(bed.room_id for bed in beds.values()))
        target_room = rooms[target_bed.room_id]
        _validate_employee_and_destination(employee=employee, room=target_room)
        _validate_assignment_interval(
            assignment_type=assignment_type,
            starts_at=moment,
            ends_at=ends_at,
        )
        if current_occupancy.starts_at >= moment:
            raise _settlement_error(
                'Переселение невозможно в момент начала текущего размещения.',
                'settlement_relocation_same_moment',
            )
        persisted_occupancies = _locked_related_occupancies(
            employee_id=employee.pk,
            bed_ids=(target_bed.pk,),
        )
        _validate_occupancy_conflicts(
            employee_id=employee.pk,
            bed=target_bed,
            starts_at=moment,
            ends_at=ends_at,
            persisted_occupancies=persisted_occupancies,
            current_occupancy_id=current_occupancy.pk,
        )

        current_occupancy.terminated_at = moment
        current_occupancy.save(update_fields=['terminated_at'])
        occupancy = _create_occupancy(
            employee=employee,
            bed=target_bed,
            assignment_type=assignment_type,
            starts_at=moment,
            ends_at=ends_at,
            settled_by=settled_by,
        )

    return occupancy


def release_employee_from_bed(*, bed_stable_id):
    with transaction.atomic():
        try:
            bed_id = (
                PhysicalBed.objects
                .filter(stable_id=bed_stable_id)
                .values_list('pk', flat=True)
                .get()
            )
        except PhysicalBed.DoesNotExist as error:
            raise _settlement_error(
                'Койко-место не найдено.',
                'settlement_bed_not_found',
            ) from error

        moment = timezone.now()
        current_occupancy = (
            EmployeeBedOccupancy.objects
            .filter(effective_occupancy_at_q(moment), physical_bed_id=bed_id)
            .order_by('pk')
            .first()
        )
        if current_occupancy is None:
            _locked_beds(bed_id)
            raise _settlement_error(
                'Койко-место уже свободно.',
                'settlement_bed_already_free',
            )

        employee = _locked_employee(current_occupancy.employee_id)
        bed = _locked_beds(bed_id)[bed_id]
        active_occupancies = list(
            EmployeeBedOccupancy.objects
            .select_for_update()
            .filter(effective_occupancy_at_q(moment), physical_bed=bed)
            .order_by('pk')
        )
        if not active_occupancies:
            raise _settlement_error(
                'Койко-место уже свободно.',
                'settlement_bed_already_free',
            )
        if len(active_occupancies) != 1:
            raise _settlement_error(
                'Для койко-места найдено несколько действующих размещений.',
                'settlement_bed_multiple_active_occupancies',
            )
        occupancy = active_occupancies[0]
        if occupancy.employee_id != employee.pk:
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        if occupancy.starts_at >= moment:
            raise _settlement_error(
                'Освобождение невозможно в момент начала размещения.',
                'settlement_release_same_moment',
            )
        occupancy.terminated_at = moment
        occupancy.save(update_fields=['terminated_at'])

    return occupancy
