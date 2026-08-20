import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from shifts.models import WatchPeriod
from users.models import (
    Employee,
    EmployeeAccess,
    WatchComposition,
    WorkSchedule,
)

from .models import EmployeeWatchProfileChange


class EmployeeWatchProfileChangeError(ValidationError):
    """Controlled failure of a closed employee watch-profile command."""


ERROR_ACCESS_NOT_FOUND = 'employee_watch_profile.access_not_found'
ERROR_ACCESS_INACTIVE = 'employee_watch_profile.access_inactive'
ERROR_ACCESS_BLOCKED = 'employee_watch_profile.access_blocked'
ERROR_ACCESS_WRONG_ROLE = 'employee_watch_profile.access_wrong_role'
ERROR_ACTOR_EMPLOYEE_INACTIVE = 'employee_watch_profile.actor_employee_inactive'
ERROR_EMPLOYEE_NOT_FOUND = 'employee_watch_profile.employee_not_found'
ERROR_EMPLOYEE_INACTIVE = 'employee_watch_profile.employee_inactive'
ERROR_WATCH_PERIOD_NOT_FOUND = 'employee_watch_profile.watch_period_not_found'
ERROR_WATCH_PERIOD_NOT_FUTURE = 'employee_watch_profile.watch_period_not_future'
ERROR_WORK_SCHEDULE_NOT_FOUND = 'employee_watch_profile.work_schedule_not_found'
ERROR_WORK_SCHEDULE_INACTIVE = 'employee_watch_profile.work_schedule_inactive'
ERROR_WATCH_COMPOSITION_NOT_FOUND = 'employee_watch_profile.watch_composition_not_found'
ERROR_WATCH_COMPOSITION_INACTIVE = 'employee_watch_profile.watch_composition_inactive'
ERROR_WATCH_COMPOSITION_MISMATCH = 'employee_watch_profile.watch_composition_mismatch'
ERROR_BRIGADE_REQUIRED = 'employee_watch_profile.brigade_required'
ERROR_BRIGADE_NOT_ALLOWED = 'employee_watch_profile.brigade_not_allowed'
ERROR_BRIGADE_OUT_OF_RANGE = 'employee_watch_profile.brigade_out_of_range'
ERROR_INVALID_BASIS = 'employee_watch_profile.invalid_basis'
ERROR_BASIS_DATE_IN_FUTURE = 'employee_watch_profile.basis_date_in_future'
ERROR_NO_CHANGE = 'employee_watch_profile.no_change'
ERROR_CHANGE_NOT_FOUND = 'employee_watch_profile.change_not_found'
ERROR_CHANGE_NOT_DRAFT = 'employee_watch_profile.change_not_draft'
ERROR_CHANGE_STALE = 'employee_watch_profile.change_stale'
ERROR_SOURCE_INVALID = 'employee_watch_profile.source_invalid'
ERROR_SOURCE_FINGERPRINT_INVALID = (
    'employee_watch_profile.source_fingerprint_invalid'
)
ERROR_WATCH_PERIOD_ALREADY_SETTLED = (
    'employee_watch_profile.watch_period_already_settled'
)
ERROR_PROFILE_INCONSISTENT = 'employee_watch_profile.profile_inconsistent'

_SNAPSHOT_SCHEMA = 'rotations.employee_watch_profile_change'
_SNAPSHOT_VERSION = 1
_PROFILE_SNAPSHOT_SCHEMA = 'rotations.resolved_employee_watch_profile'
_PROFILE_SNAPSHOT_VERSION = 1
_TIMEKEEPER_ROLE_CODE = 'timekeeper'

SOURCE_KIND_LEGACY_BASELINE = 'legacy_baseline'
SOURCE_KIND_APPLIED_CHANGE = 'applied_change'


@dataclass(frozen=True, slots=True)
class ResolvedEmployeeWatchProfile:
    employee_id: int
    watch_period_id: int
    effective_on: date
    work_schedule_id: int | None
    brigade_number: int | None
    watch_composition_id: int | None
    source_kind: str
    change_id: int | None
    change_version_number: int | None
    source_fingerprint: str | None
    profile_fingerprint: str


def _error(code, message):
    return EmployeeWatchProfileChangeError(message, code=code)


def _valid_identifier(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _normalize_required_text(value, *, label):
    if not isinstance(value, str):
        raise _error(ERROR_INVALID_BASIS, f'Поле «{label}» заполнено неверно.')
    normalized = ' '.join(value.split())
    if not normalized:
        raise _error(ERROR_INVALID_BASIS, f'Поле «{label}» обязательно.')
    return normalized


def _normalize_basis_date(value):
    if isinstance(value, datetime):
        raise _error(ERROR_INVALID_BASIS, 'Дата официального основания указана неверно.')
    if isinstance(value, date):
        normalized = value
    elif isinstance(value, str):
        try:
            normalized = date.fromisoformat(value.strip())
        except ValueError as error:
            raise _error(
                ERROR_INVALID_BASIS,
                'Дата официального основания указана неверно.',
            ) from error
    else:
        raise _error(ERROR_INVALID_BASIS, 'Дата официального основания обязательна.')
    if normalized > timezone.localdate():
        raise _error(
            ERROR_BASIS_DATE_IN_FUTURE,
            'Дата официального основания не может находиться в будущем.',
        )
    return normalized


def _normalize_basis(*, basis_kind, basis_number, basis_date, basis):
    if not isinstance(basis_kind, str):
        raise _error(ERROR_INVALID_BASIS, 'Вид официального основания указан неверно.')
    normalized_kind = basis_kind.strip()
    if normalized_kind not in EmployeeWatchProfileChange.BasisKind.values:
        raise _error(ERROR_INVALID_BASIS, 'Вид официального основания указан неверно.')
    normalized_number = _normalize_required_text(
        basis_number,
        label='Номер официального основания',
    )
    max_number_length = EmployeeWatchProfileChange._meta.get_field(
        'basis_number',
    ).max_length
    if len(normalized_number) > max_number_length:
        raise _error(
            ERROR_INVALID_BASIS,
            'Номер официального основания слишком длинный.',
        )
    return (
        normalized_kind,
        normalized_number,
        _normalize_basis_date(basis_date),
        _normalize_required_text(basis, label='Официальное основание'),
    )


def _access_plan(actor_access_id):
    if not _valid_identifier(actor_access_id):
        raise _error(ERROR_ACCESS_NOT_FOUND, 'Точный доступ табельщика не найден.')
    plan = (
        EmployeeAccess.objects.filter(pk=actor_access_id)
        .values('pk', 'employee_id', 'role_id')
        .first()
    )
    if plan is None:
        raise _error(ERROR_ACCESS_NOT_FOUND, 'Точный доступ табельщика не найден.')
    return plan


def _lock_actor_employee(plan):
    try:
        return (
            Employee.objects.select_for_update(of=('self',))
            .get(pk=plan['employee_id'])
        )
    except Employee.DoesNotExist as error:
        raise _error(ERROR_ACCESS_NOT_FOUND, 'Сотрудник доступа не найден.') from error


def _lock_timekeeper_access(plan, *, actor_employee):
    try:
        access = (
            EmployeeAccess.objects.select_for_update(of=('self',))
            .select_related('role')
            .get(pk=plan['pk'])
        )
    except EmployeeAccess.DoesNotExist as error:
        raise _error(ERROR_ACCESS_NOT_FOUND, 'Точный доступ табельщика не найден.') from error

    if (
        access.employee_id != actor_employee.pk
        or access.employee_id != plan['employee_id']
        or access.role_id != plan['role_id']
    ):
        raise _error(ERROR_ACCESS_NOT_FOUND, 'Точный доступ табельщика изменился.')
    if access.status == EmployeeAccess.Status.BLOCKED:
        raise _error(ERROR_ACCESS_BLOCKED, 'Доступ табельщика заблокирован.')
    if access.status != EmployeeAccess.Status.ACTIVATED or not access.is_active:
        raise _error(ERROR_ACCESS_INACTIVE, 'Доступ табельщика неактивен.')
    if actor_employee.status != Employee.Status.ACTIVE or not actor_employee.is_active:
        raise _error(
            ERROR_ACTOR_EMPLOYEE_INACTIVE,
            'Сотрудник табельщика неактивен.',
        )
    if not access.role.is_active or access.role.code != _TIMEKEEPER_ROLE_CODE:
        raise _error(
            ERROR_ACCESS_WRONG_ROLE,
            'Доступ не принадлежит активной роли табельщика.',
        )
    return access


def _lock_target_employee(employee_id, *, actor_employee, require_active=True):
    if not _valid_identifier(employee_id):
        raise _error(ERROR_EMPLOYEE_NOT_FOUND, 'Сотрудник не найден.')
    if employee_id == actor_employee.pk:
        employee = actor_employee
    else:
        try:
            employee = (
                Employee.objects.select_for_update(of=('self',))
                .get(pk=employee_id)
            )
        except Employee.DoesNotExist as error:
            raise _error(ERROR_EMPLOYEE_NOT_FOUND, 'Сотрудник не найден.') from error
    if (
        require_active
        and (employee.status != Employee.Status.ACTIVE or not employee.is_active)
    ):
        raise _error(ERROR_EMPLOYEE_INACTIVE, 'Сотрудник неактивен.')
    return employee


def _lock_watch_period(watch_period_id, *, require_future=True):
    if not _valid_identifier(watch_period_id):
        raise _error(ERROR_WATCH_PERIOD_NOT_FOUND, 'Период вахты не найден.')
    try:
        period = (
            WatchPeriod.objects.select_for_update(of=('self',))
            .get(pk=watch_period_id)
        )
    except WatchPeriod.DoesNotExist as error:
        raise _error(ERROR_WATCH_PERIOD_NOT_FOUND, 'Период вахты не найден.') from error
    if require_future and period.starts_on <= timezone.localdate():
        raise _error(
            ERROR_WATCH_PERIOD_NOT_FUTURE,
            'Изменение разрешено только для будущего периода вахты.',
        )
    return period


def _lock_work_schedule(work_schedule_id, *, require_active=True):
    if not _valid_identifier(work_schedule_id):
        raise _error(ERROR_WORK_SCHEDULE_NOT_FOUND, 'График работы не найден.')
    try:
        schedule = (
            WorkSchedule.objects.select_for_update(of=('self',))
            .get(pk=work_schedule_id)
        )
    except WorkSchedule.DoesNotExist as error:
        raise _error(ERROR_WORK_SCHEDULE_NOT_FOUND, 'График работы не найден.') from error
    if require_active and not schedule.is_active:
        raise _error(ERROR_WORK_SCHEDULE_INACTIVE, 'График работы неактивен.')
    return schedule


def _lock_watch_composition(
    watch_composition_id,
    *,
    watch_period,
    require_active=True,
    require_period_match=True,
):
    if not _valid_identifier(watch_composition_id):
        raise _error(
            ERROR_WATCH_COMPOSITION_NOT_FOUND,
            'Состав вахты не найден.',
        )
    try:
        composition = (
            WatchComposition.objects.select_for_update(of=('self',))
            .get(pk=watch_composition_id)
        )
    except WatchComposition.DoesNotExist as error:
        raise _error(
            ERROR_WATCH_COMPOSITION_NOT_FOUND,
            'Состав вахты не найден.',
        ) from error
    if require_active and not composition.is_active:
        raise _error(
            ERROR_WATCH_COMPOSITION_INACTIVE,
            'Состав вахты неактивен.',
        )
    if (
        require_period_match
        and watch_period.watch_composition_id != composition.pk
    ):
        raise _error(
            ERROR_WATCH_COMPOSITION_MISMATCH,
            'Состав вахты не соответствует выбранному периоду.',
        )
    return composition


def _normalize_brigade_number(value, *, work_schedule):
    if work_schedule.brigade_count == 0:
        if value is not None:
            raise _error(
                ERROR_BRIGADE_NOT_ALLOWED,
                'Для выбранного графика номер бригады не указывается.',
            )
        return None
    if value is None:
        raise _error(
            ERROR_BRIGADE_REQUIRED,
            'Для выбранного графика необходимо указать номер бригады.',
        )
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(
            ERROR_BRIGADE_OUT_OF_RANGE,
            'Номер бригады находится вне допустимого диапазона.',
        )
    if value < 1 or value > work_schedule.brigade_count:
        raise _error(
            ERROR_BRIGADE_OUT_OF_RANGE,
            'Номер бригады находится вне допустимого диапазона.',
        )
    return value


def _profile_tuple(*, work_schedule_id, brigade_number, watch_composition_id):
    return (work_schedule_id, brigade_number, watch_composition_id)


def _validate_existing_profile(*, work_schedule, brigade_number):
    if work_schedule is None:
        if brigade_number is not None:
            raise _error(
                ERROR_PROFILE_INCONSISTENT,
                'Существующий профиль сотрудника противоречив.',
            )
        return
    count = work_schedule.brigade_count
    if (
        (count == 0 and brigade_number is not None)
        or (count > 0 and (
            brigade_number is None
            or brigade_number < 1
            or brigade_number > count
        ))
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Существующий профиль сотрудника противоречив.',
        )


def _build_source_snapshot(
    *,
    employee,
    watch_period,
    old_profile,
    new_profile,
    basis_kind,
    basis_number,
    basis_date,
    basis,
):
    return {
        'schema': _SNAPSHOT_SCHEMA,
        'version': _SNAPSHOT_VERSION,
        'employee_id': employee.pk,
        'watch_period': {
            'id': watch_period.pk,
            'starts_on': watch_period.starts_on.isoformat(),
            'ends_on': watch_period.ends_on.isoformat(),
        },
        'old_profile': {
            'work_schedule_id': old_profile[0],
            'brigade_number': old_profile[1],
            'watch_composition_id': old_profile[2],
        },
        'new_profile': {
            'work_schedule_id': new_profile[0],
            'brigade_number': new_profile[1],
            'watch_composition_id': new_profile[2],
        },
        'basis': {
            'kind': basis_kind,
            'number': basis_number,
            'date': basis_date.isoformat(),
            'text': basis,
        },
    }


def _canonical_fingerprint(snapshot):
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def _snapshot_for_change(change):
    return _build_source_snapshot(
        employee=change.employee,
        watch_period=change.effective_watch_period,
        old_profile=_profile_tuple(
            work_schedule_id=change.old_work_schedule_id,
            brigade_number=change.old_brigade_number,
            watch_composition_id=change.old_watch_composition_id,
        ),
        new_profile=_profile_tuple(
            work_schedule_id=change.new_work_schedule_id,
            brigade_number=change.new_brigade_number,
            watch_composition_id=change.new_watch_composition_id,
        ),
        basis_kind=change.basis_kind,
        basis_number=change.basis_number,
        basis_date=change.basis_date,
        basis=change.basis,
    )


def _validate_applied_change(change):
    if (
        change.status != EmployeeWatchProfileChange.Status.APPLIED
        or change.effective_on != change.effective_watch_period.starts_on
        or change.superseded_at is not None
        or change.cancelled_at is not None
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'История профиля сотрудника противоречива.',
        )
    _validate_existing_profile(
        work_schedule=change.new_work_schedule,
        brigade_number=change.new_brigade_number,
    )
    _validate_existing_profile(
        work_schedule=change.old_work_schedule,
        brigade_number=change.old_brigade_number,
    )
    expected_snapshot = _snapshot_for_change(change)
    if (
        change.source_snapshot != expected_snapshot
        or change.source_fingerprint != _canonical_fingerprint(expected_snapshot)
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'История профиля сотрудника содержит повреждённый снимок.',
        )


def _validate_target_history(target_changes):
    version_numbers = [change.version_number for change in target_changes]
    if (
        len(version_numbers) != len(set(version_numbers))
        or any(number < 1 for number in version_numbers)
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Нумерация истории профиля сотрудника противоречива.',
        )
    for change in target_changes:
        expected_snapshot = _snapshot_for_change(change)
        if (
            change.effective_on != change.effective_watch_period.starts_on
            or change.source_snapshot != expected_snapshot
            or change.source_fingerprint != _canonical_fingerprint(expected_snapshot)
        ):
            raise _error(
                ERROR_PROFILE_INCONSISTENT,
                'История профиля сотрудника содержит повреждённый снимок.',
            )


def _lock_change_history(*, employee, watch_period):
    target_changes = list(
        EmployeeWatchProfileChange._base_manager.select_for_update(of=('self',))
        .filter(employee=employee, effective_watch_period=watch_period)
        .select_related(
            'employee',
            'effective_watch_period',
            'old_work_schedule',
            'new_work_schedule',
        )
        .order_by('version_number', 'pk')
    )
    earlier_applied = list(
        EmployeeWatchProfileChange._base_manager.select_for_update(of=('self',))
        .filter(
            employee=employee,
            effective_on__lt=watch_period.starts_on,
            status=EmployeeWatchProfileChange.Status.APPLIED,
        )
        .select_related(
            'employee',
            'effective_watch_period',
            'old_work_schedule',
            'new_work_schedule',
        )
        .order_by('effective_on', 'version_number', 'pk')
    )
    return target_changes, earlier_applied


def _effective_profiles(*, employee, target_changes, earlier_applied):
    applied_for_target = [
        change
        for change in target_changes
        if change.status == EmployeeWatchProfileChange.Status.APPLIED
    ]
    if len(applied_for_target) > 1:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Для периода найдено несколько действующих решений.',
        )
    if applied_for_target:
        applied = applied_for_target[0]
        _validate_applied_change(applied)
        old_profile = _profile_tuple(
            work_schedule_id=applied.old_work_schedule_id,
            brigade_number=applied.old_brigade_number,
            watch_composition_id=applied.old_watch_composition_id,
        )
        comparison_profile = _profile_tuple(
            work_schedule_id=applied.new_work_schedule_id,
            brigade_number=applied.new_brigade_number,
            watch_composition_id=applied.new_watch_composition_id,
        )
        return old_profile, comparison_profile

    if earlier_applied:
        applied = earlier_applied[-1]
        _validate_applied_change(applied)
        profile = _profile_tuple(
            work_schedule_id=applied.new_work_schedule_id,
            brigade_number=applied.new_brigade_number,
            watch_composition_id=applied.new_watch_composition_id,
        )
        return profile, profile

    _validate_existing_profile(
        work_schedule=employee.work_schedule,
        brigade_number=employee.brigade_number,
    )
    profile = _profile_tuple(
        work_schedule_id=employee.work_schedule_id,
        brigade_number=employee.brigade_number,
        watch_composition_id=employee.watch_composition_id,
    )
    return profile, profile


def _trusted_insert_change(change):
    change.full_clean()
    models.QuerySet.bulk_create(
        EmployeeWatchProfileChange._base_manager.all(),
        [change],
    )
    return change


def _matching_draft(
    *,
    target_changes,
    actor_access,
    source_snapshot,
    source_fingerprint,
):
    matches = [
        change
        for change in target_changes
        if (
            change.status == EmployeeWatchProfileChange.Status.DRAFT
            and change.created_by_access_id == actor_access.pk
            and change.source_snapshot == source_snapshot
            and change.source_fingerprint == source_fingerprint
            and change.supersedes_id is None
            and change.applied_by_access_id is None
            and change.superseded_by_access_id is None
            and change.cancelled_by_access_id is None
            and change.applied_at is None
            and change.superseded_at is None
            and change.cancelled_at is None
        )
    ]
    if len(matches) > 1:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Найдено несколько одинаковых черновиков изменения.',
        )
    return matches[0] if matches else None


def _create_employee_watch_profile_change_draft_once(
    *,
    access_plan,
    employee_id,
    effective_watch_period_id,
    new_work_schedule_id,
    new_brigade_number,
    new_watch_composition_id,
    basis_kind,
    basis_number,
    basis_date,
    basis,
):
    actor_employee = _lock_actor_employee(access_plan)
    actor_access = _lock_timekeeper_access(
        access_plan,
        actor_employee=actor_employee,
    )
    employee = _lock_target_employee(employee_id, actor_employee=actor_employee)
    watch_period = _lock_watch_period(effective_watch_period_id)
    work_schedule = _lock_work_schedule(new_work_schedule_id)
    watch_composition = _lock_watch_composition(
        new_watch_composition_id,
        watch_period=watch_period,
    )
    brigade_number = _normalize_brigade_number(
        new_brigade_number,
        work_schedule=work_schedule,
    )
    target_changes, earlier_applied = _lock_change_history(
        employee=employee,
        watch_period=watch_period,
    )
    _validate_target_history(target_changes)
    old_profile, comparison_profile = _effective_profiles(
        employee=employee,
        target_changes=target_changes,
        earlier_applied=earlier_applied,
    )
    new_profile = _profile_tuple(
        work_schedule_id=work_schedule.pk,
        brigade_number=brigade_number,
        watch_composition_id=watch_composition.pk,
    )
    if new_profile == comparison_profile:
        raise _error(ERROR_NO_CHANGE, 'Новый профиль совпадает с действующим.')

    source_snapshot = _build_source_snapshot(
        employee=employee,
        watch_period=watch_period,
        old_profile=old_profile,
        new_profile=new_profile,
        basis_kind=basis_kind,
        basis_number=basis_number,
        basis_date=basis_date,
        basis=basis,
    )
    source_fingerprint = _canonical_fingerprint(source_snapshot)
    existing = _matching_draft(
        target_changes=target_changes,
        actor_access=actor_access,
        source_snapshot=source_snapshot,
        source_fingerprint=source_fingerprint,
    )
    if existing is not None:
        return existing

    change = EmployeeWatchProfileChange(
        employee=employee,
        effective_watch_period=watch_period,
        effective_on=watch_period.starts_on,
        version_number=max(
            (item.version_number for item in target_changes),
            default=0,
        ) + 1,
        supersedes=None,
        old_work_schedule_id=old_profile[0],
        old_brigade_number=old_profile[1],
        old_watch_composition_id=old_profile[2],
        new_work_schedule=work_schedule,
        new_brigade_number=brigade_number,
        new_watch_composition=watch_composition,
        basis_kind=basis_kind,
        basis_number=basis_number,
        basis_date=basis_date,
        basis=basis,
        source_snapshot=source_snapshot,
        source_fingerprint=source_fingerprint,
        created_by_access=actor_access,
        applied_by_access=None,
        superseded_by_access=None,
        cancelled_by_access=None,
        applied_at=None,
        superseded_at=None,
        cancelled_at=None,
        status=EmployeeWatchProfileChange.Status.DRAFT,
    )
    return _trusted_insert_change(change)


def create_employee_watch_profile_change_draft(
    *,
    employee_id,
    effective_watch_period_id,
    new_work_schedule_id,
    new_brigade_number,
    new_watch_composition_id,
    basis_kind,
    basis_number,
    basis_date,
    basis,
    actor_access_id,
):
    """Create or return an exact immutable future watch-profile draft."""
    access_plan = _access_plan(actor_access_id)
    normalized_basis = _normalize_basis(
        basis_kind=basis_kind,
        basis_number=basis_number,
        basis_date=basis_date,
        basis=basis,
    )
    try:
        with transaction.atomic():
            return _create_employee_watch_profile_change_draft_once(
                access_plan=access_plan,
                employee_id=employee_id,
                effective_watch_period_id=effective_watch_period_id,
                new_work_schedule_id=new_work_schedule_id,
                new_brigade_number=new_brigade_number,
                new_watch_composition_id=new_watch_composition_id,
                basis_kind=normalized_basis[0],
                basis_number=normalized_basis[1],
                basis_date=normalized_basis[2],
                basis=normalized_basis[3],
            )
    except EmployeeWatchProfileChangeError:
        raise
    except (IntegrityError, ValidationError) as error:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Не удалось атомарно сохранить изменение профиля сотрудника.',
        ) from error


def _change_plan(change_id):
    if not _valid_identifier(change_id):
        raise _error(ERROR_CHANGE_NOT_FOUND, 'Изменение профиля не найдено.')
    plan = (
        EmployeeWatchProfileChange._base_manager.filter(pk=change_id)
        .values(
            'pk',
            'employee_id',
            'effective_watch_period_id',
            'new_work_schedule_id',
            'new_watch_composition_id',
        )
        .first()
    )
    if plan is None:
        raise _error(ERROR_CHANGE_NOT_FOUND, 'Изменение профиля не найдено.')
    return plan


def _locked_change_from_history(*, target_changes, change_plan):
    change = next(
        (row for row in target_changes if row.pk == change_plan['pk']),
        None,
    )
    if change is None:
        raise _error(ERROR_CHANGE_NOT_FOUND, 'Изменение профиля не найдено.')
    if (
        change.employee_id != change_plan['employee_id']
        or change.effective_watch_period_id
        != change_plan['effective_watch_period_id']
        or change.new_work_schedule_id != change_plan['new_work_schedule_id']
        or change.new_watch_composition_id
        != change_plan['new_watch_composition_id']
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Смысловые ссылки изменения профиля изменились.',
        )
    return change


def _validate_change_source(change):
    normalized_basis = _normalize_basis(
        basis_kind=change.basis_kind,
        basis_number=change.basis_number,
        basis_date=change.basis_date,
        basis=change.basis,
    )
    if normalized_basis != (
        change.basis_kind,
        change.basis_number,
        change.basis_date,
        change.basis,
    ):
        raise _error(
            ERROR_SOURCE_INVALID,
            'Официальное основание изменения не является каноническим.',
        )
    expected_snapshot = _snapshot_for_change(change)
    if change.source_snapshot != expected_snapshot:
        raise _error(
            ERROR_SOURCE_INVALID,
            'Снимок источника изменения не соответствует сохранённым данным.',
        )
    expected_fingerprint = _canonical_fingerprint(expected_snapshot)
    if change.source_fingerprint != expected_fingerprint:
        raise _error(
            ERROR_SOURCE_FINGERPRINT_INVALID,
            'Отпечаток источника изменения не соответствует снимку.',
        )


def _lock_settlement_applications(watch_period):
    from settlement.models import SettlementPreviewApplication

    return list(
        SettlementPreviewApplication._base_manager
        .select_for_update(of=('self',))
        .filter(watch_period=watch_period)
        .order_by('applied_at', 'pk')
    )


def _trusted_transition_change(change, *, expected_status, update_fields):
    change.full_clean()
    values = {}
    for field_name in update_fields:
        field = EmployeeWatchProfileChange._meta.get_field(field_name)
        values[field.attname] = getattr(change, field.attname)
    updated = models.QuerySet.update(
        EmployeeWatchProfileChange._base_manager.filter(
            pk=change.pk,
            status=expected_status,
        ),
        **values,
    )
    if updated != 1:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Состояние изменения профиля изменилось во время операции.',
        )
    return change


def _applied_for_target(target_changes):
    applied = [
        change
        for change in target_changes
        if change.status == EmployeeWatchProfileChange.Status.APPLIED
    ]
    if len(applied) > 1:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Для периода найдено несколько действующих решений.',
        )
    return applied[0] if applied else None


def _validate_draft_references(
    *,
    change,
    employee,
    watch_period,
    work_schedule,
    watch_composition,
):
    if employee.status != Employee.Status.ACTIVE or not employee.is_active:
        raise _error(ERROR_EMPLOYEE_INACTIVE, 'Сотрудник неактивен.')
    if watch_period.starts_on <= timezone.localdate():
        raise _error(
            ERROR_WATCH_PERIOD_NOT_FUTURE,
            'Изменение разрешено только для будущего периода вахты.',
        )
    if change.effective_on != watch_period.starts_on:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Дата вступления изменения не совпадает с началом периода.',
        )
    if not work_schedule.is_active:
        raise _error(ERROR_WORK_SCHEDULE_INACTIVE, 'График работы неактивен.')
    if not watch_composition.is_active:
        raise _error(
            ERROR_WATCH_COMPOSITION_INACTIVE,
            'Состав вахты неактивен.',
        )
    if watch_period.watch_composition_id != watch_composition.pk:
        raise _error(
            ERROR_WATCH_COMPOSITION_MISMATCH,
            'Состав вахты не соответствует выбранному периоду.',
        )
    normalized_brigade = _normalize_brigade_number(
        change.new_brigade_number,
        work_schedule=work_schedule,
    )
    if normalized_brigade != change.new_brigade_number:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Номер бригады изменения профиля повреждён.',
        )
    if (
        change.supersedes_id is not None
        or change.applied_by_access_id is not None
        or change.superseded_by_access_id is not None
        or change.cancelled_by_access_id is not None
        or change.applied_at is not None
        or change.superseded_at is not None
        or change.cancelled_at is not None
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Черновик содержит недопустимые реквизиты перехода.',
        )


def _draft_old_profile(change):
    return _profile_tuple(
        work_schedule_id=change.old_work_schedule_id,
        brigade_number=change.old_brigade_number,
        watch_composition_id=change.old_watch_composition_id,
    )


def _draft_new_profile(change):
    return _profile_tuple(
        work_schedule_id=change.new_work_schedule_id,
        brigade_number=change.new_brigade_number,
        watch_composition_id=change.new_watch_composition_id,
    )


def _profile_before_target(*, employee, earlier_applied):
    if earlier_applied:
        applied = earlier_applied[-1]
        _validate_applied_change(applied)
        return _profile_tuple(
            work_schedule_id=applied.new_work_schedule_id,
            brigade_number=applied.new_brigade_number,
            watch_composition_id=applied.new_watch_composition_id,
        )
    _validate_existing_profile(
        work_schedule=employee.work_schedule,
        brigade_number=employee.brigade_number,
    )
    return _profile_tuple(
        work_schedule_id=employee.work_schedule_id,
        brigade_number=employee.brigade_number,
        watch_composition_id=employee.watch_composition_id,
    )


def _validate_draft_freshness(
    *,
    change,
    employee,
    target_changes,
    earlier_applied,
):
    current_applied = _applied_for_target(target_changes)
    if current_applied is None and any(
        row.status == EmployeeWatchProfileChange.Status.SUPERSEDED
        for row in target_changes
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'История исправлений периода не содержит действующей версии.',
        )

    profile_before_target = _profile_before_target(
        employee=employee,
        earlier_applied=earlier_applied,
    )
    if current_applied is not None:
        _validate_applied_change(current_applied)
        if (
            current_applied.applied_at is None
            or change.created_at <= current_applied.applied_at
            or change.version_number <= current_applied.version_number
        ):
            raise _error(
                ERROR_CHANGE_STALE,
                'После создания черновика было применено другое решение.',
            )
        expected_old = _profile_tuple(
            work_schedule_id=current_applied.old_work_schedule_id,
            brigade_number=current_applied.old_brigade_number,
            watch_composition_id=current_applied.old_watch_composition_id,
        )
        if expected_old != profile_before_target:
            raise _error(
                ERROR_CHANGE_STALE,
                'Профиль перед периодом изменился после исходного решения.',
            )
        current_profile = _profile_tuple(
            work_schedule_id=current_applied.new_work_schedule_id,
            brigade_number=current_applied.new_brigade_number,
            watch_composition_id=current_applied.new_watch_composition_id,
        )
    else:
        expected_old = profile_before_target
        current_profile = profile_before_target
    if _draft_old_profile(change) != expected_old:
        raise _error(
            ERROR_CHANGE_STALE,
            'Исходный профиль сотрудника изменился после создания черновика.',
        )
    if _draft_new_profile(change) == current_profile:
        raise _error(ERROR_NO_CHANGE, 'Новый профиль совпадает с действующим.')
    return current_applied


def _apply_employee_watch_profile_change_once(
    *,
    access_plan,
    change_plan,
):
    actor_employee = _lock_actor_employee(access_plan)
    actor_access = _lock_timekeeper_access(
        access_plan,
        actor_employee=actor_employee,
    )
    employee = _lock_target_employee(
        change_plan['employee_id'],
        actor_employee=actor_employee,
        require_active=False,
    )
    watch_period = _lock_watch_period(
        change_plan['effective_watch_period_id'],
        require_future=False,
    )
    work_schedule = _lock_work_schedule(
        change_plan['new_work_schedule_id'],
        require_active=False,
    )
    watch_composition = _lock_watch_composition(
        change_plan['new_watch_composition_id'],
        watch_period=watch_period,
        require_active=False,
        require_period_match=False,
    )
    target_changes, earlier_applied = _lock_change_history(
        employee=employee,
        watch_period=watch_period,
    )
    settlement_applications = _lock_settlement_applications(watch_period)
    change = _locked_change_from_history(
        target_changes=target_changes,
        change_plan=change_plan,
    )
    current_applied = _applied_for_target(target_changes)

    if change.status == EmployeeWatchProfileChange.Status.APPLIED:
        if current_applied is None or current_applied.pk != change.pk:
            raise _error(
                ERROR_PROFILE_INCONSISTENT,
                'Применённая версия не является действующей для периода.',
            )
        _validate_change_source(change)
        _validate_applied_change(change)
        return change
    if change.status != EmployeeWatchProfileChange.Status.DRAFT:
        raise _error(
            ERROR_CHANGE_NOT_DRAFT,
            'Применить можно только черновик изменения.',
        )

    _validate_draft_references(
        change=change,
        employee=employee,
        watch_period=watch_period,
        work_schedule=work_schedule,
        watch_composition=watch_composition,
    )
    _validate_change_source(change)
    _validate_target_history(target_changes)
    current_applied = _validate_draft_freshness(
        change=change,
        employee=employee,
        target_changes=target_changes,
        earlier_applied=earlier_applied,
    )
    if settlement_applications:
        raise _error(
            ERROR_WATCH_PERIOD_ALREADY_SETTLED,
            'Для периода уже применено фактическое расселение.',
        )

    transition_at = timezone.now()
    if current_applied is not None:
        current_applied.status = EmployeeWatchProfileChange.Status.SUPERSEDED
        current_applied.superseded_by_access = actor_access
        current_applied.superseded_at = transition_at
        _trusted_transition_change(
            current_applied,
            expected_status=EmployeeWatchProfileChange.Status.APPLIED,
            update_fields=(
                'status',
                'superseded_by_access',
                'superseded_at',
            ),
        )
        change.supersedes = current_applied

    change.status = EmployeeWatchProfileChange.Status.APPLIED
    change.applied_by_access = actor_access
    change.applied_at = transition_at
    _trusted_transition_change(
        change,
        expected_status=EmployeeWatchProfileChange.Status.DRAFT,
        update_fields=(
            'status',
            'supersedes',
            'applied_by_access',
            'applied_at',
        ),
    )
    return change


def apply_employee_watch_profile_change(*, change_id, actor_access_id):
    """Apply one exact immutable future watch-profile draft atomically."""
    access_plan = _access_plan(actor_access_id)
    change_plan = _change_plan(change_id)
    try:
        with transaction.atomic():
            return _apply_employee_watch_profile_change_once(
                access_plan=access_plan,
                change_plan=change_plan,
            )
    except EmployeeWatchProfileChangeError:
        raise
    except (IntegrityError, ValidationError) as error:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Не удалось атомарно применить изменение профиля сотрудника.',
        ) from error


def _read_employee_for_profile(employee_id):
    if not _valid_identifier(employee_id):
        raise _error(ERROR_EMPLOYEE_NOT_FOUND, 'Сотрудник не найден.')
    employee = (
        Employee._base_manager
        .select_related('work_schedule', 'watch_composition')
        .filter(pk=employee_id)
        .first()
    )
    if employee is None:
        raise _error(ERROR_EMPLOYEE_NOT_FOUND, 'Сотрудник не найден.')
    if employee.status != Employee.Status.ACTIVE or not employee.is_active:
        raise _error(ERROR_EMPLOYEE_INACTIVE, 'Сотрудник неактивен.')
    return employee


def _read_watch_period_for_profile(watch_period_id):
    if not _valid_identifier(watch_period_id):
        raise _error(ERROR_WATCH_PERIOD_NOT_FOUND, 'Период вахты не найден.')
    watch_period = (
        WatchPeriod._base_manager
        .filter(pk=watch_period_id)
        .first()
    )
    if watch_period is None:
        raise _error(ERROR_WATCH_PERIOD_NOT_FOUND, 'Период вахты не найден.')
    return watch_period


def _resolved_profile_tuple(change):
    return _profile_tuple(
        work_schedule_id=change.new_work_schedule_id,
        brigade_number=change.new_brigade_number,
        watch_composition_id=change.new_watch_composition_id,
    )


def _old_profile_tuple(change):
    return _profile_tuple(
        work_schedule_id=change.old_work_schedule_id,
        brigade_number=change.old_brigade_number,
        watch_composition_id=change.old_watch_composition_id,
    )


def _validate_applied_audit_shape(change):
    if (
        change.status != EmployeeWatchProfileChange.Status.APPLIED
        or change.applied_by_access_id is None
        or change.applied_at is None
        or change.superseded_by_access_id is not None
        or change.superseded_at is not None
        or change.cancelled_by_access_id is not None
        or change.cancelled_at is not None
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'История профиля сотрудника содержит неверное состояние применения.',
        )


def _validate_superseded_lineage(change):
    if change.supersedes_id is None:
        return
    superseded = change.supersedes
    if (
        superseded.employee_id != change.employee_id
        or superseded.effective_watch_period_id
        != change.effective_watch_period_id
        or superseded.effective_on != change.effective_on
        or superseded.version_number >= change.version_number
        or superseded.status != EmployeeWatchProfileChange.Status.SUPERSEDED
        or superseded.applied_by_access_id is None
        or superseded.applied_at is None
        or superseded.superseded_by_access_id is None
        or superseded.superseded_at is None
        or superseded.cancelled_by_access_id is not None
        or superseded.cancelled_at is not None
        or _old_profile_tuple(superseded) != _old_profile_tuple(change)
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'История исправлений профиля сотрудника противоречива.',
        )
    _validate_change_source(superseded)


def _validate_replayed_change(change, *, employee_id, previous_profile):
    _validate_applied_audit_shape(change)
    if (
        change.employee_id != employee_id
        or change.version_number < 1
        or change.effective_on != change.effective_watch_period.starts_on
        or change.new_watch_composition_id
        != change.effective_watch_period.watch_composition_id
    ):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'История профиля сотрудника противоречива.',
        )
    _validate_change_source(change)
    _validate_superseded_lineage(change)
    _validate_existing_profile(
        work_schedule=change.old_work_schedule,
        brigade_number=change.old_brigade_number,
    )
    _validate_existing_profile(
        work_schedule=change.new_work_schedule,
        brigade_number=change.new_brigade_number,
    )
    if _old_profile_tuple(change) != previous_profile:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'История профиля сотрудника содержит разрыв прежних и новых значений.',
        )
    next_profile = _resolved_profile_tuple(change)
    if next_profile == previous_profile:
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Применённое изменение не изменяет структурированный профиль.',
        )
    return next_profile


def _validate_resolved_profile(
    *,
    work_schedule,
    brigade_number,
    watch_composition,
):
    if work_schedule is None:
        if brigade_number is not None:
            raise _error(
                ERROR_BRIGADE_NOT_ALLOWED,
                'Без графика работы номер бригады не указывается.',
            )
    else:
        if not work_schedule.is_active:
            raise _error(ERROR_WORK_SCHEDULE_INACTIVE, 'График работы неактивен.')
        _normalize_brigade_number(
            brigade_number,
            work_schedule=work_schedule,
        )
    if watch_composition is not None and not watch_composition.is_active:
        raise _error(
            ERROR_WATCH_COMPOSITION_INACTIVE,
            'Состав вахты неактивен.',
        )


def _build_profile_snapshot(
    *,
    employee_id,
    watch_period,
    profile,
    source_kind,
    change,
):
    return {
        'schema': _PROFILE_SNAPSHOT_SCHEMA,
        'version': _PROFILE_SNAPSHOT_VERSION,
        'employee_id': employee_id,
        'watch_period': {
            'id': watch_period.pk,
            'starts_on': watch_period.starts_on.isoformat(),
        },
        'resolved_profile': {
            'work_schedule_id': profile[0],
            'brigade_number': profile[1],
            'watch_composition_id': profile[2],
        },
        'source': {
            'kind': source_kind,
            'change_id': change.pk if change is not None else None,
            'change_version_number': (
                change.version_number if change is not None else None
            ),
            'source_fingerprint': (
                change.source_fingerprint if change is not None else None
            ),
        },
    }


def resolve_employee_watch_profile(*, employee_id, watch_period_id):
    """Resolve one employee's structured profile for one exact watch period."""
    employee = _read_employee_for_profile(employee_id)
    watch_period = _read_watch_period_for_profile(watch_period_id)
    applied_history = list(
        EmployeeWatchProfileChange._base_manager
        .filter(
            employee_id=employee.pk,
            status=EmployeeWatchProfileChange.Status.APPLIED,
            effective_on__lte=watch_period.starts_on,
        )
        .select_related(
            'effective_watch_period',
            'old_work_schedule',
            'old_watch_composition',
            'new_work_schedule',
            'new_watch_composition',
            'supersedes',
            'supersedes__effective_watch_period',
            'supersedes__old_work_schedule',
            'supersedes__old_watch_composition',
            'supersedes__new_work_schedule',
            'supersedes__new_watch_composition',
        )
        .order_by(
            'effective_on',
            'effective_watch_period_id',
            'version_number',
            'pk',
        )
    )

    effective_dates = [change.effective_on for change in applied_history]
    if len(effective_dates) != len(set(effective_dates)):
        raise _error(
            ERROR_PROFILE_INCONSISTENT,
            'Для одной даты найдено несколько действующих решений профиля.',
        )

    profile = _profile_tuple(
        work_schedule_id=employee.work_schedule_id,
        brigade_number=employee.brigade_number,
        watch_composition_id=employee.watch_composition_id,
    )
    last_change = None
    for change in applied_history:
        profile = _validate_replayed_change(
            change,
            employee_id=employee.pk,
            previous_profile=profile,
        )
        last_change = change

    if last_change is None:
        work_schedule = employee.work_schedule
        watch_composition = employee.watch_composition
        source_kind = SOURCE_KIND_LEGACY_BASELINE
    else:
        work_schedule = last_change.new_work_schedule
        watch_composition = last_change.new_watch_composition
        source_kind = SOURCE_KIND_APPLIED_CHANGE
    _validate_resolved_profile(
        work_schedule=work_schedule,
        brigade_number=profile[1],
        watch_composition=watch_composition,
    )

    profile_snapshot = _build_profile_snapshot(
        employee_id=employee.pk,
        watch_period=watch_period,
        profile=profile,
        source_kind=source_kind,
        change=last_change,
    )
    return ResolvedEmployeeWatchProfile(
        employee_id=employee.pk,
        watch_period_id=watch_period.pk,
        effective_on=watch_period.starts_on,
        work_schedule_id=profile[0],
        brigade_number=profile[1],
        watch_composition_id=profile[2],
        source_kind=source_kind,
        change_id=last_change.pk if last_change is not None else None,
        change_version_number=(
            last_change.version_number if last_change is not None else None
        ),
        source_fingerprint=(
            last_change.source_fingerprint if last_change is not None else None
        ),
        profile_fingerprint=_canonical_fingerprint(profile_snapshot),
    )
