"""Rules for separating official positions from operational work specialization."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .protected_cards import protected_writes_allowed
from .models import Employee, EmployeeAccess, TemporaryWorkTransfer


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


def _effective_work_transfers(as_of):
    return (
        TemporaryWorkTransfer.objects.filter(
            status=TemporaryWorkTransfer.Status.APPROVED,
            effective_from__lte=as_of,
            effective_to__gte=as_of,
        )
        .select_related('target_specialization', 'target_specialization__access_role')
    )


def active_temporary_work_transfer(employee, *, as_of=None):
    if not employee or not employee.pk:
        return None
    return (
        _effective_work_transfers(as_of or timezone.localdate()).filter(employee=employee)
        .order_by('-reviewed_at', '-id')
        .first()
    )


def effective_specialization(employee, *, as_of=None):
    """Return the approved temporary specialization first, then the official base one."""
    transfer = active_temporary_work_transfer(employee, as_of=as_of)
    if transfer:
        return transfer.target_specialization
    return getattr(employee, 'base_specialization', None)


def effective_specializations_by_employee_id(employees, *, as_of=None):
    specializations = {employee.pk: employee.base_specialization for employee in employees}
    if not specializations:
        return specializations
    transferred = {}
    for transfer in _effective_work_transfers(as_of or timezone.localdate()).filter(
        employee_id__in=specializations,
    ).order_by('employee_id', '-reviewed_at', '-id'):
        transferred.setdefault(transfer.employee_id, transfer.target_specialization)
    specializations.update(transferred)
    return specializations


def _specialization_matches_role(specialization, role_code):
    return bool(
        specialization
        and specialization.is_active
        and specialization.access_role_id
        and specialization.access_role.code == role_code
    )


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
    as_of = as_of or timezone.localdate()
    if not employee.contractor_access_is_valid(as_of):
        return False
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

    return _specialization_matches_role(effective_specialization(employee, as_of=as_of), role_code)


def eligible_employee_ids_for_work_role(role_code, *, as_of=None):
    """Evaluate eligibility in one place so deputy planning and admin agree."""
    as_of = as_of or timezone.localdate()
    eligible = Employee.objects.filter(
        Employee.work_eligibility_q(as_of),
        is_active=True,
        status=Employee.Status.ACTIVE,
    )
    if role_code not in PRODUCTION_APP_ROLE_CODES:
        return set(eligible.values_list('id', flat=True))

    legacy_access = EmployeeAccess.objects.filter(
        employee_id=OuterRef('pk'),
        role__code=role_code,
        role__is_active=True,
        status=EmployeeAccess.Status.ACTIVATED,
        is_active=True,
    )
    legacy_ids = set(
        eligible.filter(personnel_position__isnull=True).filter(
            Q(work_category=role_code)
            | (Q(work_category=Employee.WorkCategory.OTHER) & Exists(legacy_access)),
        ).values_list('id', flat=True)
    )
    specialized = eligible.filter(personnel_position__isnull=False).select_related(
        'base_specialization__access_role',
    )
    return legacy_ids | {
        employee_id
        for employee_id, specialization in effective_specializations_by_employee_id(specialized, as_of=as_of).items()
        if _specialization_matches_role(specialization, role_code)
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


def production_access_is_out_of_sync(employee, *, as_of=None):
    """Расходятся ли выданные доступы с тем, что положено по специализации.

    Карточку правят гораздо чаще, чем меняют специализацию, и раньше доступ
    подтягивался только в момент её смены. Если запись доступа по какой-то
    причине не появилась — карточку завели раньше, чем справочник должностей,
    или специализацию проставили в обход формы — человек оставался без входа
    навсегда: сколько ни сохраняй карточку, специализация не менялась, и
    синхронизация не запускалась. Со стороны это выглядело как «внёс телефон,
    сохранил, а система всё равно не узнаёт номер», хотя телефон был ни при чём.

    Проверка дешёвая и точечная: у кого доступы уже в порядке, ничего не
    трогаем — иначе пересборка задевала бы и тех, у кого всё настроено руками.
    """
    if employee.is_protected and not protected_writes_allowed():
        return False

    as_of = as_of or timezone.localdate()
    target_role = _effective_production_role(employee, as_of=as_of)
    active_accesses = [
        access
        for access in EmployeeAccess.objects
        .select_related('role')
        .filter(employee=employee, role__code__in=PRODUCTION_APP_ROLE_CODES)
        if access.is_active
    ]

    if not target_role:
        return bool(active_accesses)
    return not any(access.role_id == target_role.id for access in active_accesses)


def _revoke_sessions_for_changed_accesses(accesses):
    """Гасит вход для доступов, которые только что изменились.

    Сдвига поколения мало: `reclaim-session` его не сверяет и перевыдал бы
    сессию без PIN. Поэтому удаляем строки `django_session` и двигаем
    поколение через общий механизм отзыва. Импорт ленивый — чтобы не тянуть
    цепочку `live_monitor` при импорте профилей.
    """
    unique = {access.pk: access for access in accesses if access.pk}
    if not unique:
        return
    from .live_monitor import force_end_access_sessions

    for access in unique.values():
        force_end_access_sessions(access=access)


def sync_employee_production_access(*, employee, as_of=None):
    """Keep exactly one production app access aligned with effective specialization.

    A role change reuses an existing PIN/password record whenever possible. A
    new access without credentials is left in ``not_activated`` state for the
    normal PIN issuance flow; this avoids silently creating a credential.
    """
    # Защищённая карточка живёт по своим правилам: её доступы выставлены руками
    # и не должны подстраиваться под специальность. Молча пропускаем — иначе
    # запрет уронил бы весь пакетный проход по временным переводам.
    if employee.is_protected and not protected_writes_allowed():
        return None

    as_of = as_of or timezone.localdate()
    target_role = _effective_production_role(employee, as_of=as_of)
    production_accesses = list(
        EmployeeAccess.objects.select_for_update()
        .select_related('role')
        .filter(employee=employee, role__code__in=PRODUCTION_APP_ROLE_CODES)
        .order_by('id')
    )
    active_accesses = [access for access in production_accesses if access.is_active]
    changed_accesses = []

    if not target_role:
        now = timezone.now()
        for access in active_accesses:
            access.status = EmployeeAccess.Status.DEACTIVATED
            access.is_active = False
            access.deactivated_at = now
            access.save(update_fields=['status', 'is_active', 'deactivated_at'])
            changed_accesses.append(access)
        _revoke_sessions_for_changed_accesses(changed_accesses)
        return None

    target_access = next((access for access in production_accesses if access.role_id == target_role.id), None)
    reusable_access = next((access for access in active_accesses if access.role_id != target_role.id), None)

    if target_access is None and reusable_access is not None:
        reusable_access.role = target_role
        reusable_access.save(update_fields=['role'])
        target_access = reusable_access
        # Смена роли на месте — новый вход: старые сессии этого доступа мертвы.
        changed_accesses.append(reusable_access)

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
        # Реактивация не должна оживлять cookie, выданную до отключения.
        changed_accesses.append(target_access)

    now = timezone.now()
    for access in production_accesses:
        if access.id == target_access.id or not access.is_active:
            continue
        access.status = EmployeeAccess.Status.DEACTIVATED
        access.is_active = False
        access.deactivated_at = now
        access.save(update_fields=['status', 'is_active', 'deactivated_at'])
        changed_accesses.append(access)
    _revoke_sessions_for_changed_accesses(changed_accesses)
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


def resolve_base_specialization(*, personnel_position, selected=None, previous=None):
    """Use an explicit choice, a compatible saved value, or the only active option."""
    if selected:
        validate_base_specialization(personnel_position=personnel_position, specialization=selected)
        return selected
    allowed = list(personnel_position.allowed_specializations.all())
    active = [specialization for specialization in allowed if specialization.is_active]
    if previous in active:
        return previous
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise ValidationError('Выберите производственную специализацию для этой должности.')
    if allowed or personnel_position.default_specialization_id or personnel_position.requires_specialization:
        raise ValidationError('У должности нет действующей производственной специализации. Проверьте справочник должностей.')
    return None


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
