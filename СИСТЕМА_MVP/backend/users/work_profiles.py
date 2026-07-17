"""Rules for separating official positions from operational work specialization."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Employee, EmployeeAccess, ProductionSpecialization, TemporaryWorkTransfer


PRODUCTION_APP_ROLE_CODES = frozenset({'driver', 'excavator_operator'})


def legacy_work_category_for_specialization(specialization):
    """Keep legacy fields coherent while new records use the specialization catalogue."""
    if (
        specialization
        and specialization.access_role_id
        and specialization.access_role.code in PRODUCTION_APP_ROLE_CODES
    ):
        return specialization.access_role.code
    return Employee.WorkCategory.OTHER


def expire_due_temporary_work_transfers(*, as_of=None):
    """Close expired transfers lazily so a forgotten approval never stays effective."""
    as_of = as_of or timezone.localdate()
    due_transfers = list(
        TemporaryWorkTransfer.objects.select_related('employee')
        .filter(
            status=TemporaryWorkTransfer.Status.APPROVED,
            effective_to__lt=as_of,
        )
        .order_by('id')
    )
    if not due_transfers:
        return []

    now = timezone.now()
    expired = []
    with transaction.atomic():
        for transfer in due_transfers:
            locked_transfer = (
                TemporaryWorkTransfer.objects.select_for_update()
                .select_related('employee')
                .filter(
                    id=transfer.id,
                    status=TemporaryWorkTransfer.Status.APPROVED,
                    effective_to__lt=as_of,
                )
                .first()
            )
            if not locked_transfer:
                continue
            locked_transfer.status = TemporaryWorkTransfer.Status.EXPIRED
            locked_transfer.closed_at = now
            locked_transfer.save(update_fields=['status', 'closed_at'])
            employee = Employee.objects.select_for_update().get(pk=locked_transfer.employee_id)
            sync_employee_production_access(employee=employee)
            expired.append(locked_transfer)
    return expired


def active_temporary_work_transfer(employee, *, as_of=None):
    if not employee or not employee.pk:
        return None
    as_of = as_of or timezone.localdate()
    return (
        TemporaryWorkTransfer.objects.filter(
            employee=employee,
            status=TemporaryWorkTransfer.Status.APPROVED,
            effective_from__lte=as_of,
            effective_to__gte=as_of,
        )
        .select_related('target_specialization', 'target_specialization__access_role')
        .order_by('-reviewed_at', '-id')
        .first()
    )


def effective_specialization(employee, *, as_of=None):
    """Return the approved temporary specialization first, then the official base one."""
    transfer = active_temporary_work_transfer(employee, as_of=as_of)
    if transfer:
        return transfer.target_specialization
    return getattr(employee, 'base_specialization', None)


def employee_has_effective_access_role(
    employee,
    role_code,
    *,
    as_of=None,
    allow_pending_access=False,
):
    """Whether an employee may use a production app role at this date.

    Roles outside the two production applications remain governed by their own
    access records. Old employees without a personnel position retain the
    previous work-category behavior during the migration period.
    """
    if role_code not in PRODUCTION_APP_ROLE_CODES:
        return True

    if not getattr(employee, 'personnel_position_id', None):
        if employee.work_category == role_code:
            return True
        if employee.work_category != Employee.WorkCategory.OTHER:
            return False
        allowed_statuses = [EmployeeAccess.Status.ACTIVATED]
        if allow_pending_access:
            allowed_statuses.append(EmployeeAccess.Status.NOT_ACTIVATED)
        return EmployeeAccess.objects.filter(
            employee=employee,
            role__code=role_code,
            role__is_active=True,
            status__in=allowed_statuses,
            is_active=True,
        ).exists()

    specialization = effective_specialization(employee, as_of=as_of)
    return bool(
        specialization
        and specialization.is_active
        and specialization.access_role_id
        and specialization.access_role.code == role_code
    )


def eligible_employee_ids_for_work_role(role_code, *, as_of=None):
    """Evaluate eligibility in one place so deputy planning and admin agree."""
    as_of = as_of or timezone.localdate()
    employees = (
        Employee.objects.filter(is_active=True, status=Employee.Status.ACTIVE)
        .select_related(
            'personnel_position',
            'base_specialization',
            'base_specialization__access_role',
        )
        .prefetch_related('accesses__role')
        .order_by('id')
    )
    return {
        employee.id
        for employee in employees
        if employee_has_effective_access_role(employee, role_code, as_of=as_of)
    }


def _effective_production_role(employee, *, as_of=None):
    specialization = effective_specialization(employee, as_of=as_of)
    if (
        specialization
        and specialization.access_role_id
        and specialization.access_role.code in PRODUCTION_APP_ROLE_CODES
    ):
        return specialization.access_role
    return None


def sync_employee_production_access(*, employee, as_of=None):
    """Keep exactly one production app access aligned with effective specialization.

    A role change reuses an existing PIN/password record whenever possible. A
    new access without credentials is left in ``not_activated`` state for the
    normal PIN issuance flow; this avoids silently creating a credential.
    """
    as_of = as_of or timezone.localdate()
    target_role = _effective_production_role(employee, as_of=as_of)
    production_accesses = list(
        EmployeeAccess.objects.select_for_update()
        .select_related('role')
        .filter(employee=employee, role__code__in=PRODUCTION_APP_ROLE_CODES)
        .order_by('id')
    )
    active_accesses = [access for access in production_accesses if access.is_active]

    if not target_role:
        now = timezone.now()
        for access in active_accesses:
            access.status = EmployeeAccess.Status.DEACTIVATED
            access.is_active = False
            access.deactivated_at = now
            access.save(update_fields=['status', 'is_active', 'deactivated_at'])
        return None

    target_access = next((access for access in production_accesses if access.role_id == target_role.id), None)
    reusable_access = next((access for access in active_accesses if access.role_id != target_role.id), None)

    if target_access is None and reusable_access is not None:
        reusable_access.role = target_role
        reusable_access.save(update_fields=['role'])
        target_access = reusable_access

    if target_access is None:
        target_access = EmployeeAccess.objects.create(
            employee=employee,
            role=target_role,
            access_code='',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            is_active=True,
        )
    elif not target_access.is_active and target_access.status == EmployeeAccess.Status.DEACTIVATED:
        target_access.is_active = True
        target_access.status = (
            EmployeeAccess.Status.ACTIVATED
            if target_access.activated_at
            else EmployeeAccess.Status.NOT_ACTIVATED
        )
        target_access.deactivated_at = None
        target_access.save(update_fields=['status', 'is_active', 'deactivated_at'])

    now = timezone.now()
    for access in production_accesses:
        if access.id == target_access.id or not access.is_active:
            continue
        access.status = EmployeeAccess.Status.DEACTIVATED
        access.is_active = False
        access.deactivated_at = now
        access.save(update_fields=['status', 'is_active', 'deactivated_at'])
    return target_access


def validate_base_specialization(*, personnel_position, specialization):
    if not personnel_position:
        return
    if personnel_position.requires_specialization and not specialization:
        raise ValidationError('Для этой кадровой должности выберите производственную специализацию.')
    if not specialization:
        return
    if not specialization.is_active:
        raise ValidationError('Выбранная производственная специализация отключена.')
    if not personnel_position.allowed_specializations.filter(pk=specialization.pk).exists():
        raise ValidationError('Эта специализация не разрешена для выбранной кадровой должности.')


@transaction.atomic
def request_temporary_work_transfer(*, employee, target_specialization, watch_period, requested_by, reason=''):
    today = timezone.localdate()
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    if not employee.is_active or employee.status != Employee.Status.ACTIVE:
        raise ValidationError('Нельзя оформить перевод для неактивного сотрудника.')
    if not watch_period.is_active or not (watch_period.starts_on <= today <= watch_period.ends_on):
        raise ValidationError('Выберите действующую текущую вахту.')
    if not target_specialization.is_active:
        raise ValidationError('Целевая производственная специализация отключена.')
    if target_specialization.pk == getattr(effective_specialization(employee), 'pk', None):
        raise ValidationError('У сотрудника уже действует эта производственная специализация.')
    if TemporaryWorkTransfer.objects.filter(
        employee=employee,
        status__in=[
            TemporaryWorkTransfer.Status.REQUESTED,
            TemporaryWorkTransfer.Status.APPROVED,
        ],
        effective_to__gte=today,
    ).exists():
        raise ValidationError('У сотрудника уже есть незавершенный запрос или временный перевод.')
    return TemporaryWorkTransfer.objects.create(
        employee=employee,
        source_specialization=effective_specialization(employee),
        target_specialization=target_specialization,
        watch_period=watch_period,
        effective_from=today,
        effective_to=watch_period.ends_on,
        reason=(reason or '').strip(),
        requested_by=requested_by,
    )


@transaction.atomic
def approve_temporary_work_transfer(*, transfer, reviewed_by, review_comment=''):
    today = timezone.localdate()
    transfer = (
        TemporaryWorkTransfer.objects.select_for_update()
        .select_related('employee', 'watch_period', 'target_specialization')
        .get(pk=transfer.pk)
    )
    if transfer.status != TemporaryWorkTransfer.Status.REQUESTED:
        raise ValidationError('Этот запрос уже обработан.')
    if transfer.effective_to < today or transfer.watch_period.ends_on < today:
        raise ValidationError('Срок выбранной вахты уже закончился.')
    if not transfer.target_specialization.is_active:
        raise ValidationError('Целевая производственная специализация отключена.')
    from .oup_services import employee_work_category_blockers

    employee = Employee.objects.select_for_update().get(pk=transfer.employee_id)
    blockers = employee_work_category_blockers(employee)
    if blockers:
        raise ValidationError('Сначала освободите сотрудника от рабочих операций: ' + '; '.join(blockers) + '.')
    transfer.status = TemporaryWorkTransfer.Status.APPROVED
    transfer.reviewed_by = reviewed_by
    transfer.reviewed_at = timezone.now()
    transfer.review_comment = (review_comment or '').strip()
    transfer.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_comment'])
    access = sync_employee_production_access(employee=employee, as_of=today)
    return transfer, access


@transaction.atomic
def resolve_temporary_work_transfer(*, transfer, reviewed_by, approved, review_comment=''):
    if approved:
        return approve_temporary_work_transfer(
            transfer=transfer,
            reviewed_by=reviewed_by,
            review_comment=review_comment,
        )

    transfer = TemporaryWorkTransfer.objects.select_for_update().get(pk=transfer.pk)
    if transfer.status != TemporaryWorkTransfer.Status.REQUESTED:
        raise ValidationError('Этот запрос уже обработан.')
    transfer.status = TemporaryWorkTransfer.Status.REJECTED
    transfer.reviewed_by = reviewed_by
    transfer.reviewed_at = timezone.now()
    transfer.review_comment = (review_comment or '').strip()
    transfer.closed_at = transfer.reviewed_at
    transfer.save(
        update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_comment', 'closed_at'],
    )
    return transfer, None


@transaction.atomic
def cancel_temporary_work_transfer(*, transfer, cancelled_by, comment=''):
    """Cancel an approved transfer and immediately restore the base work profile."""
    transfer = (
        TemporaryWorkTransfer.objects.select_for_update()
        .select_related('employee')
        .get(pk=transfer.pk)
    )
    if transfer.status != TemporaryWorkTransfer.Status.APPROVED:
        raise ValidationError('Отменить можно только действующий одобренный временный перевод.')

    from .oup_services import employee_work_category_blockers

    employee = Employee.objects.select_for_update().get(pk=transfer.employee_id)
    blockers = employee_work_category_blockers(employee)
    if blockers:
        raise ValidationError('Сначала освободите сотрудника от рабочих операций: ' + '; '.join(blockers) + '.')

    now = timezone.now()
    cancellation_note = (comment or '').strip()
    transfer.status = TemporaryWorkTransfer.Status.CANCELLED
    transfer.closed_at = now
    transfer.review_comment = '\n'.join(
        part for part in [
            transfer.review_comment.strip(),
            f'Отменено администратором {cancelled_by.full_name}.',
            cancellation_note,
        ] if part
    )
    transfer.save(update_fields=['status', 'closed_at', 'review_comment'])
    access = sync_employee_production_access(employee=employee)
    return transfer, access
