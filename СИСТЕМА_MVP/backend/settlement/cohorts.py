import hashlib
import json
from datetime import datetime, time

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from shifts.models import WatchPeriod
from users.models import EmployeeAccess

from .models import SettlementCohort, SettlementCohortMember, SettlementRevision
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


def _internal_shift_source(*, resident, period, assignment_id):
    if not resident.employee_id:
        raise _shift_review_required('внутренний жилец не связан с Employee.')
    if assignment_id is None:
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
    if assignment.pk != assignment_id:
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


def _revalidated_member_shift_source(*, member, period, for_update=True):
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
