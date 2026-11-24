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
    EmployeeAccommodationBinding,
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementBedPreparation,
    SettlementCohortMember,
    SettlementPlacementEvent,
    SettlementPreviewApplication,
    SettlementPreviewRun,
    SettlementResident,
)
from .cohorts import (
    eligible_official_assignments,
    official_assignment_matches_period,
    resolve_internal_official_shift_source,
)
from .personnel_categories import (
    MISSING_CATEGORY,
    MISSING_CATEGORY_MESSAGE,
    ROOM_CATEGORY_CONFLICT,
    ROOM_CATEGORY_CONFLICT_MESSAGE,
    employee_settlement_category,
    room_allows_personnel_category,
)
from .control import (
    lock_settlement_write_access,
    lock_settlement_write_lease,
)
from .residents import (
    build_settlement_resident_lock_plan,
    lock_settlement_residents_after_access,
)
from .validator import (
    ActualPlacementType,
    EffectivePlacementInterval,
    PlacementConflictValidationRequest,
    PlacementStructureValidationRequest,
    ValidationPhase,
    validate_placement_conflicts,
    validate_placement_structure,
)


# Версия исполняемого набора правил для структурного контракта записи
# (SET-R094). Значение фиксирует отфильтрованный набор согласованных правил
# реестра от 21.11.2026 (101 правило, R001–R101).
SETTLEMENT_RULE_SET_VERSION = 'SET-R001-R101/2026-11-21'


def _settlement_error(message, code):
    return ValidationError(message, code=code)


def validate_placement_input_contract(
    *,
    resident,
    bed_stable_id,
    assignment_type,
    starts_at,
    ends_at=None,
    checked_at=None,
    phase=ValidationPhase.COMMIT,
):
    """Структурный контракт точки записи (SET-R092-R094).

    Уже написанные проверки ``validator.py`` обязательны для любой записи
    проживания: фаза, момент проверки, версия набора правил, идентификатор
    сотрудника и койки, границы интервала. При нарушении запись отклоняется.
    """

    try:
        placement_type = ActualPlacementType(assignment_type)
    except ValueError:
        placement_type = assignment_type
    findings = validate_placement_structure(
        PlacementStructureValidationRequest(
            phase=phase,
            employee_id=getattr(resident, 'employee_id', None),
            bed_stable_id=bed_stable_id,
            placement_type=placement_type,
            starts_at=starts_at,
            ends_at=ends_at,
            checked_at=checked_at or timezone.now(),
            rule_set_version=SETTLEMENT_RULE_SET_VERSION,
        )
    )
    if findings:
        first = findings[0]
        raise _settlement_error(first.message, first.code)


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

    candidate_room_ids = {
        row['room'].pk
        for row in provisional_rows
        if row['equipment_assignment'].pk not in conflicted_assignment_ids
    }
    occupancies_by_room = defaultdict(list)
    if candidate_room_ids:
        for occupancy in (
            EmployeeBedOccupancy.objects
            .filter(
                effective_occupancy_at_q(effective_date),
                physical_bed__room_id__in=candidate_room_ids,
            )
            .select_related(
                'resident__employee__personnel_position',
                'physical_bed',
            )
            .order_by('pk')
        ):
            occupancies_by_room[occupancy.physical_bed.room_id].append(occupancy)

    candidate_rows_by_room = defaultdict(list)
    for row in provisional_rows:
        assignment = row['equipment_assignment']
        if assignment.pk in conflicted_assignment_ids:
            continue
        incompatible_occupancies = [
            occupancy
            for occupancy in occupancies_by_room[row['room'].pk]
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
        # Решение владельца 22.11.2026: рабочие и ИТР не смешиваются в комнате,
        # даже в разных блоках. Правило считается по комнате, не по койке.
        category = employee_settlement_category(assignment.employee)
        roommate_categories = (
            employee_settlement_category(occupancy.resident.employee)
            for occupancy in occupancies_by_room[row['room'].pk]
            if occupancy.resident.employee_id != assignment.employee_id
        )
        if not room_allows_personnel_category(
            row['room'].room_type, category, roommate_categories,
        ):
            code = MISSING_CATEGORY if category is None else ROOM_CATEGORY_CONFLICT
            conflicts.append(
                _auto_settlement_preview_conflict(
                    code,
                    assignments=(assignment,),
                    employee=assignment.employee,
                    equipment=assignment.equipment,
                    shift_type=assignment.shift_type,
                    accommodation_anchor=row['accommodation_anchor'],
                    room=row['room'],
                    bed=row['bed'],
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)
            continue
        candidate_rows_by_room[row['room'].pk].append(row)

    # Второй проход: разные категории внутри самого расчёта в одной комнате.
    for room_rows in candidate_rows_by_room.values():
        categories = {
            employee_settlement_category(row['equipment_assignment'].employee)
            for row in room_rows
        }
        if len(categories) <= 1:
            continue
        for row in room_rows:
            assignment = row['equipment_assignment']
            conflicts.append(
                _auto_settlement_preview_conflict(
                    ROOM_CATEGORY_CONFLICT,
                    assignments=(assignment,),
                    employee=assignment.employee,
                    equipment=assignment.equipment,
                    shift_type=assignment.shift_type,
                    accommodation_anchor=row['accommodation_anchor'],
                    room=row['room'],
                    bed=row['bed'],
                ),
            )
            conflicted_assignment_ids.add(assignment.pk)

    successful_rows = [
        row for row in provisional_rows
        if row['equipment_assignment'].pk not in conflicted_assignment_ids
    ]

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
    if (
        assignment_type
        == EmployeeBedOccupancy.AssignmentType.TEMPORARY_UNAPPROVED
        and ends_at is not None
    ):
        raise _settlement_error(
            'Для временного до утверждения плановое окончание не указывается.',
            'settlement_temporary_unapproved_end_forbidden',
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


def _validate_room_sex(
    *,
    resident,
    room,
    starts_at,
    ends_at,
    exclude_occupancy_ids=(),
    using=None,
):
    """T-036 backend-14: пол комнаты — по фактическим жильцам, не по атрибуту.

    Решение владельца 20.11.2026: пустая комната принимает любую группу и
    фиксирует её пол; комната с жильцами принимает только их пол; неизвестный
    пол жильца или оба пола — отказ.
    """

    from .room_sex import (
        MALE,
        REASON_OCCUPANT_SEX_UNKNOWN,
        REASON_ROOM_SEX_MIXED,
        REASON_ROOM_SEX_MISMATCH,
        REASON_RESIDENT_SEX_UNKNOWN,
        resident_sex,
        room_sex_states,
        rejection_reason,
    )

    sex = resident_sex(resident)
    if sex not in {MALE, Employee.Sex.FEMALE}:
        raise _settlement_error(
            'Нельзя заселить жильца с неуказанным полом: пол обязателен.',
            'settlement_employee_sex_unknown',
        )
    states = room_sex_states(
        room_ids=(room.pk,),
        starts_at=starts_at,
        ends_at=ends_at,
        using=using,
        exclude_occupancy_ids=exclude_occupancy_ids,
    )
    state, room_sex = states.get(room.pk, ('free', None))
    reason = rejection_reason(sex=sex, room_state=state, room_sex=room_sex)
    if reason is None:
        return
    codes = {
        REASON_RESIDENT_SEX_UNKNOWN: 'settlement_employee_sex_unknown',
        REASON_OCCUPANT_SEX_UNKNOWN: 'settlement_room_occupant_sex_unknown',
        REASON_ROOM_SEX_MIXED: 'settlement_room_sex_mixed',
        REASON_ROOM_SEX_MISMATCH: 'settlement_room_sex_mismatch',
    }
    messages = {
        'settlement_employee_sex_unknown': (
            'Нельзя заселить жильца с неуказанным полом: пол обязателен.'
        ),
        'settlement_room_occupant_sex_unknown': (
            'В комнате живёт жилец с неуказанным полом; заселение запрещено.'
        ),
        'settlement_room_sex_mixed': (
            'В комнате на этот период живут и мужчины, и женщины.'
        ),
        'settlement_room_sex_mismatch': (
            'Пол жильца не совпадает с полом комнаты на этот период.'
        ),
    }
    code = codes[reason]
    raise _settlement_error(messages[code], code)


def _validate_room_capacity(
    *,
    resident,
    room,
    starts_at=None,
    ends_at=None,
    confirm_itr=False,
    placement_reason='',
    using=None,
):
    """Специальный фонд: двухместные, protected-держатели, резерв.

    SET-R097 (двухместные только ИТР), SET-R098 (держатели защищённого места),
    SET-R099 (резерв только командированным). Тип фонда определяется якорями
    комнаты, а не номером комнаты.
    """

    from .special_fund import manual_room_rejection

    if starts_at is None:
        starts_at = timezone.now()
    problem = manual_room_rejection(
        resident=resident,
        room=room,
        starts_at=starts_at,
        ends_at=ends_at,
        confirm_itr=confirm_itr,
        placement_reason=placement_reason,
        using=using,
    )
    if problem is not None:
        code, message = problem
        raise _settlement_error(message, code)


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


def current_housing_import_occupancy_ids(placements, *, using=None):
    """Identify factual Excel imports by their placement event, including older rows."""
    if not placements:
        return set()
    events = SettlementPlacementEvent.objects.using(using).filter(
        event_type=SettlementPlacementEvent.EventType.TEMPORARY_CREATED,
        resident_id__in={resident_id for resident_id, _ in placements.values()},
        payload__occupancy_id__in=placements,
        payload__has_keys=['workbook_sha256', 'source_row'],
    ).values_list('payload', 'resident_id', 'physical_bed_id')
    return {
        payload['occupancy_id'] for payload, resident_id, bed_id in events
        if placements.get(payload.get('occupancy_id')) == (resident_id, bed_id)
    }


def _current_import_watch_periods(composition_ids, moment, *, using=None):
    as_of = production_work_date(moment)
    return WatchPeriod.objects.using(using).filter(
        is_active=True, watch_composition__is_active=True, watch_composition_id__in=composition_ids,
        starts_on__lte=as_of, ends_on__gte=as_of,
    ).order_by('pk')


def current_housing_official_assignments(occupancies, moment):
    """Read the unambiguous published assignments for imported residents in bulk."""
    placements = {
        occupancy.pk: (occupancy.resident_id, occupancy.physical_bed_id)
        for occupancy in occupancies if occupancy.watch_period_id is None and occupancy.resident.employee_id
    }
    imported_ids = current_housing_import_occupancy_ids(placements)
    employees_by_composition = defaultdict(set)
    for occupancy in occupancies:
        if occupancy.pk in imported_ids:
            employee = occupancy.resident.employee
            employees_by_composition[employee.watch_composition_id].add(employee.pk)
    periods_by_composition = defaultdict(list)
    for period in _current_import_watch_periods(employees_by_composition, moment):
        periods_by_composition[period.watch_composition_id].append(period)
    assignments_by_employee = defaultdict(list)
    for composition_id, periods in periods_by_composition.items():
        if len(periods) != 1:
            continue
        period = periods[0]
        for assignment in eligible_official_assignments(
            employee_ids=employees_by_composition[composition_id], period=period,
        ):
            assignments_by_employee[assignment.employee_id].append((assignment, period))
    verified = {
        employee_id: entries[0][0]
        for employee_id, entries in assignments_by_employee.items()
        if len(entries) == 1 and official_assignment_matches_period(*entries[0])
    }
    return imported_ids, verified


def _lock_import_relocation_shift(resident, occupancy_snapshot, moment, *, using):
    occupancy_id, resident_id, bed_id, period_id = occupancy_snapshot[:4]
    if period_id or not resident.employee_id or not current_housing_import_occupancy_ids(
        {occupancy_id: (resident_id, bed_id)}, using=using,
    ):
        return None, None

    periods = list(_current_import_watch_periods(
        [resident.employee.watch_composition_id], moment, using=using,
    ).select_for_update(of=('self',))[:2])
    if len(periods) != 1:
        raise _settlement_error(
            'Для сотрудника нужна одна действующая вахта. Проверьте состав вахты.',
            'settlement.manual.period_ambiguous',
        )
    period = periods[0]
    try:
        source = resolve_internal_official_shift_source(resident=resident, period=period)
    except ValidationError as error:
        raise _settlement_error(
            'Проверьте технику и смену сотрудника в опубликованной расстановке на эту вахту.',
            'settlement.cohort.shift_review_required',
        ) from error
    return period, source


def _official_manual_source_for_occupancy(*, occupancy, resident):
    # T-036 backend-16: у командированного размещения нет проверенной смены,
    # поэтому сначала называем настоящую причину, а не «историческое» placement.
    if not resident.employee_id:
        raise _settlement_error(
            'Ручное переселение внешнего жильца ещё не подключено.',
            'settlement.manual.external_unavailable',
        )
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


def _validate_room_personnel_category(
    *,
    resident,
    room,
    starts_at,
    ends_at,
    exclude_occupancy_ids=(),
    exclude_resident_ids=(),
    using=None,
):
    """Решение владельца 22.11.2026: рабочие и ИТР не смешиваются в комнате.

    Правило считается по физической комнате (не по блоку/койке): рабочий и ИТР
    не живут вместе даже в разных блоках. Пустая категория кадровой должности —
    отказ, а не догадка. Внешние жильцы не классифицируются и правило к ним не
    применяется.
    """

    if resident.is_external:
        return
    category = employee_settlement_category(resident.employee)
    if category is None:
        raise _settlement_error(
            MISSING_CATEGORY_MESSAGE,
            MISSING_CATEGORY,
        )
    exclude_ids = tuple(exclude_occupancy_ids)
    interval_q = Q(
        replaced_by_application__isnull=True,
        replaced_by_occupancy__isnull=True,
    )
    if ends_at is not None:
        interval_q &= Q(starts_at__lt=ends_at)
    if starts_at is not None:
        interval_q &= Q(ends_at__isnull=True) | Q(ends_at__gt=starts_at)
        interval_q &= (
            Q(terminated_at__isnull=True) | Q(terminated_at__gt=starts_at)
        )
    occupancies = (
        EmployeeBedOccupancy.objects.using(using)
        .filter(
            interval_q,
            physical_bed__room_id=room.pk,
        )
        .exclude(resident_id=resident.pk)
        .select_related('resident__employee__personnel_position')
    )
    if exclude_ids:
        occupancies = occupancies.exclude(pk__in=exclude_ids)
    roommate_categories = [
        employee_settlement_category(occupancy.resident.employee)
        for occupancy in occupancies
        if not occupancy.resident.is_external
    ]
    # T-036 D1 (QA-21): ручной путь обязан видеть и подтверждённые плановые
    # привязки другой категории, пересекающиеся по датам, — не только фактические
    # проживания. Привязка относится к комнате через якорь и его подтверждённое
    # закрепление койки.
    period_start = starts_at.date() if starts_at is not None else None
    period_end = ends_at.date() if ends_at is not None else None
    binding_q = Q(
        status=EmployeeAccommodationBinding.Status.CONFIRMED,
        resident__employee__isnull=False,
        anchor_calendar_slot__anchor__bed_assignments__status=(
            AccommodationAnchorBedAssignment.Status.CONFIRMED
        ),
        anchor_calendar_slot__anchor__bed_assignments__physical_bed__room_id=(
            room.pk
        ),
    )
    # N1 (QA-22): закрепление койки за якорем действует только в своём периоде.
    # Без этого после переноса якоря в другую комнату старая привязка ложно
    # считалась бы соседом этой комнаты.
    if ends_at is not None:
        binding_q &= Q(
            anchor_calendar_slot__anchor__bed_assignments__valid_from__lt=ends_at,
        )
    if starts_at is not None:
        binding_q &= (
            Q(
                anchor_calendar_slot__anchor__bed_assignments__valid_to__isnull=True,
            )
            | Q(
                anchor_calendar_slot__anchor__bed_assignments__valid_to__gt=(
                    starts_at
                ),
            )
        )
    bindings = (
        EmployeeAccommodationBinding.objects.using(using)
        .filter(binding_q)
        .exclude(resident_id=resident.pk)
        .select_related('resident__employee__personnel_position')
        .distinct()
    )
    if exclude_resident_ids:
        bindings = bindings.exclude(resident_id__in=tuple(exclude_resident_ids))
    for binding in bindings:
        if period_end is not None and binding.valid_from > period_end:
            continue
        if period_start is not None and binding.valid_to < period_start:
            continue
        if binding.resident.is_external:
            continue
        roommate_categories.append(
            employee_settlement_category(binding.resident.employee),
        )
    if not room_allows_personnel_category(
        room.room_type, category, roommate_categories,
    ):
        # Review-12 N-3: пустая категория у соседа — «уточните категорию»,
        # а не «смешение рабочих и ИТР».
        if None in roommate_categories:
            raise _settlement_error(
                MISSING_CATEGORY_MESSAGE,
                MISSING_CATEGORY,
            )
        raise _settlement_error(
            ROOM_CATEGORY_CONFLICT_MESSAGE,
            ROOM_CATEGORY_CONFLICT,
        )


def _validate_resident_and_destination(
    *,
    resident,
    room,
    starts_at,
    ends_at,
    confirm_itr=False,
    placement_reason='',
    exclude_occupancy_ids=(),
    exclude_resident_ids=(),
    using=None,
):
    if room.transfer_status != PhysicalRoom.TransferStatus.TRANSFERRED:
        raise _settlement_error(
            'Комната не передана для расселения.',
            'settlement_room_not_transferred',
        )
    _validate_resident_state(resident=resident)
    _validate_room_capacity(
        resident=resident,
        room=room,
        starts_at=starts_at,
        ends_at=ends_at,
        confirm_itr=confirm_itr,
        placement_reason=placement_reason,
        using=using,
    )
    _validate_room_sex(
        resident=resident,
        room=room,
        starts_at=starts_at,
        ends_at=ends_at,
        exclude_occupancy_ids=exclude_occupancy_ids,
        using=using,
    )
    _validate_room_personnel_category(
        resident=resident,
        room=room,
        starts_at=starts_at,
        ends_at=ends_at,
        exclude_occupancy_ids=exclude_occupancy_ids,
        exclude_resident_ids=exclude_resident_ids,
        using=using,
    )


def _ensure_bed_has_no_active_preparation(
    *,
    bed,
    resident_id,
    moment,
    access,
    using,
):
    """T-029: подготовленная койка придержана за жильцом до приезда.

    Подготовка (``SettlementBedPreparation.status='prepared'``) не является
    проживанием, но другого жильца на эту койку пускать нельзя. Просроченные
    подготовки этой койки (и этого жильца) сначала снимаются тем же ленивым
    правилом, что и при подготовке: место держится до конца дня приезда, затем
    освобождается без отдельного job'а. Оставшаяся подготовка — действующая,
    поэтому заселение отклоняется.
    """
    from .temporary_placement import _close_expired_preparations

    _close_expired_preparations(
        bed_id=bed.pk,
        resident_id=resident_id,
        moment=moment,
        access=access,
        using=using,
    )
    active = (
        SettlementBedPreparation.objects.using(using)
        .select_for_update(of=('self',))
        .filter(
            physical_bed_id=bed.pk,
            status=SettlementBedPreparation.Status.PREPARED,
        )
        .only('pk', 'resident_id')
        .first()
    )
    if active is None:
        return
    if resident_id is not None and active.resident_id == resident_id:
        message = (
            'Койко-место подготовлено для этого жильца. '
            'Подтвердите приезд или снимите подготовку.'
        )
    else:
        message = (
            'Койко-место подготовлено для другого жильца. '
            'Сначала снимите подготовку.'
        )
    raise _settlement_error(message, 'settlement.bed.preparation_hold')


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
    placement_reason='',
):
    return EmployeeBedOccupancy.objects.using(using).create(
        resident=resident,
        physical_bed=bed,
        assignment_type=assignment_type,
        placement_reason=placement_reason,
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
    confirm_itr=False,
    placement_reason='',
):
    using = router.db_for_write(EmployeeBedOccupancy)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        if assignment_type == EmployeeBedOccupancy.AssignmentType.PROPOSED:
            raise _settlement_error(
                'Тип закрепления «Предложенное» больше не используется '
                'для новых заселений.',
                'settlement.proposed.disabled',
            )
        if (
            assignment_type
            == EmployeeBedOccupancy.AssignmentType.TEMPORARY_UNAPPROVED
        ):
            raise _settlement_error(
                'Временное до утверждения оформляется через срочное размещение.',
                'settlement.temporary_unapproved.use_service',
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
        if not resident.employee_id:
            # T-036 backend-22: внешний жилец допускается только в ВИП-резерв
            # (RESERVE) и только командированный из центрального офиса;
            # EXTERNAL_OTHER, подрядчик и прочие внешние — отказ.
            from .special_fund import is_business_trip_resident, room_anchor_types
            from .models import AccommodationAnchor as _Anchor

            anchor_types = room_anchor_types(room=room, using=using)
            if not (
                _Anchor.AnchorType.RESERVE in anchor_types
                and is_business_trip_resident(resident)
            ):
                raise _settlement_error(
                    'Ручное заселение внешнего жильца ещё не подключено.',
                    'settlement.manual.external_unavailable',
                )
        _validate_resident_and_destination(
            resident=resident,
            room=room,
            starts_at=moment,
            ends_at=ends_at,
            confirm_itr=confirm_itr,
            placement_reason=placement_reason,
            using=using,
        )
        if resident.employee_id:
            _application, period, member, shift_source = (
                _lock_manual_application_context(
                    resident=resident,
                    application_snapshot=application_snapshot,
                    moment=moment,
                    using=using,
                )
            )
            shift_source_kind = (
                EmployeeBedOccupancy.ShiftSourceKind.OFFICIAL_ASSIGNMENT
            )
        else:
            period = None
            member = None
            shift_source = {
                'work_shift': None,
                'shift_source_fingerprint': '',
                'official_equipment_assignment': None,
            }
            shift_source_kind = (
                EmployeeBedOccupancy.ShiftSourceKind.UNVERIFIED_LEGACY
            )
        _ensure_bed_has_no_active_preparation(
            bed=bed,
            resident_id=resident.pk,
            moment=moment,
            access=owner_access,
            using=using,
        )
        _validate_assignment_interval(
            assignment_type=assignment_type,
            starts_at=moment,
            ends_at=ends_at,
        )
        validate_placement_input_contract(
            resident=resident,
            bed_stable_id=bed_snapshot['stable_id'],
            assignment_type=assignment_type,
            starts_at=moment,
            ends_at=ends_at,
            checked_at=moment,
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
                    placement_reason=placement_reason,
                    watch_period=period,
                    cohort_member=member,
                    work_shift=shift_source['work_shift'],
                    shift_source_kind=shift_source_kind,
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
    confirm_itr=False,
    placement_reason='',
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
        import_period, shift_source = _lock_import_relocation_shift(
            resident, occupancy_snapshot, moment, using=using,
        )
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
        _ensure_bed_has_no_active_preparation(
            bed=target_bed,
            resident_id=resident.pk,
            moment=moment,
            access=owner_access,
            using=using,
        )
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
        _validate_resident_and_destination(
            resident=resident,
            room=target_room,
            starts_at=moment,
            ends_at=current_occupancy.ends_at,
            confirm_itr=confirm_itr,
            placement_reason=placement_reason,
            exclude_occupancy_ids=(current_occupancy.pk,),
            using=using,
        )
        if (
            current_occupancy.assignment_type
            == EmployeeBedOccupancy.AssignmentType.PROPOSED
        ):
            raise _settlement_error(
                'Историческое «Предложенное» нельзя переселять: '
                'освободите койко-место.',
                'settlement.proposed.relocation_disabled',
            )
        if current_occupancy.starts_at >= moment:
            raise _settlement_error(
                'Переселение невозможно в момент начала текущего размещения.',
                'settlement_relocation_same_moment',
            )
        if shift_source is None:
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
            placement_reason=(
                placement_reason or current_occupancy.placement_reason
            ),
            watch_period=import_period or current_occupancy.watch_period,
            cohort_member=current_occupancy.cohort_member,
            work_shift=shift_source['work_shift'],
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
    reason='',
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
        reason_text = ''
        if (
            occupancy.assignment_type
            == EmployeeBedOccupancy.AssignmentType.TEMPORARY_UNAPPROVED
        ):
            from .temporary_placement import _normalized_text

            reason_text = _normalized_text(
                reason,
                field_label='Причина освобождения временного размещения',
                required=True,
            )
        occupancy.terminated_at = moment
        occupancy.save(update_fields=['terminated_at'])
        if reason_text:
            from .temporary_placement import _placement_event

            _placement_event(
                event_type=(
                    SettlementPlacementEvent.EventType.TEMPORARY_RELEASED
                ),
                using=using,
                resident=occupancy.resident,
                physical_bed=bed,
                actor_access=locked_rows.access_by_id(
                    control_context.owner_access_id,
                ),
                reason=reason_text,
                payload={
                    'occupancy_id': occupancy.pk,
                    'bed_stable_id': bed.stable_id,
                    'terminated_at': occupancy.terminated_at.isoformat(),
                },
            )

    return occupancy


def settle_employee_on_bed(
    *,
    bed_stable_id,
    employee_id,
    assignment_type,
    control_context=None,
    ends_at=None,
    confirm_itr=False,
    placement_reason='',
):
    using = router.db_for_write(EmployeeBedOccupancy)
    resident_id = _resident_id_for_employee(employee_id=employee_id, using=using)
    return settle_resident_on_bed(
        bed_stable_id=bed_stable_id,
        resident_id=resident_id,
        assignment_type=assignment_type,
        control_context=control_context,
        ends_at=ends_at,
        confirm_itr=confirm_itr,
        placement_reason=placement_reason,
    )


def relocate_employee_to_bed(
    *,
    bed_stable_id,
    occupancy_id,
    control_context=None,
    confirm_itr=False,
    placement_reason='',
):
    return relocate_resident_to_bed(
        bed_stable_id=bed_stable_id,
        occupancy_id=occupancy_id,
        control_context=control_context,
        confirm_itr=confirm_itr,
        placement_reason=placement_reason,
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
