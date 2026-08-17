from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from shifts.models import WatchPeriod
from users.models import Employee

from .models import SettlementCohort, SettlementCohortMember, SettlementRevision


def _confirmed_revision(revision):
    if revision.status != SettlementRevision.Status.CONFIRMED:
        raise ValidationError('Операция M5 требует подтверждённой ревизии.')


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
    employee_id,
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
):
    Employee.objects.select_for_update(of=('self',)).get(pk=employee_id)
    cohort = (
        SettlementCohort.objects.select_for_update(of=('self',))
        .select_related('watch_period', 'watch_composition')
        .get(pk=cohort_id)
    )
    if cohort.status != SettlementCohort.Status.DRAFT:
        raise ValidationError('Membership добавляется только в DRAFT cohort.')
    member = SettlementCohortMember(
        cohort=cohort,
        employee_id=employee_id,
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
        .order_by('employee_id', 'pk')
        .values_list('pk', 'employee_id')
    )
    employee_ids = sorted({approved_by_id, *(employee_id for _pk, employee_id in member_snapshot)})
    locked_employees = {
        employee.pk: employee
        for employee in Employee.objects.select_for_update(of=('self',))
        .filter(pk__in=employee_ids)
        .order_by('pk')
    }
    if set(locked_employees) != set(employee_ids):
        raise ValidationError('M5 lock plan содержит отсутствующего Employee.')

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
        .select_related('employee', 'source_revision')
        .order_by('employee_id', 'pk')
    )
    if tuple((member.pk, member.employee_id) for member in members) != member_snapshot:
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
        employee = locked_employees[member.employee_id]
        if employee.watch_composition_id != cohort.watch_composition_id:
            raise ValidationError({
                'employee': 'Сотрудник больше не принадлежит WatchComposition cohort.',
            })
        _confirmed_revision(member.source_revision)
        member.full_clean()
        if not member.participates_in_accommodation:
            continue
        overlap_exists = (
            SettlementCohortMember.objects.filter(
                employee_id=member.employee_id,
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
                'employee': 'Сотрудник уже входит в APPROVED cohort пересекающегося периода.',
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
