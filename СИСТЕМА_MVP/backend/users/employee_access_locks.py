from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.db import DEFAULT_DB_ALIAS, connections

from .models import Employee, EmployeeAccess


class EmployeeAccessLockPlanError(RuntimeError):
    INVALID_IDENTIFIER = 'invalid_identifier'
    EMPTY_PLAN = 'empty_plan'
    ACCESS_MISSING = 'access_missing'
    EMPLOYEE_MISSING = 'employee_missing'
    INCOMPLETE_PLAN = 'incomplete_plan'
    ACCESS_MAPPING_CHANGED = 'access_mapping_changed'
    ACCESS_EMPLOYEE_OUTSIDE_PLAN = 'access_employee_outside_plan'
    OUTSIDE_ATOMIC = 'outside_atomic'

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class EmployeeAccessLockPlan:
    using: str
    employee_ids: tuple[int, ...]
    access_ids: tuple[int, ...]
    expected_access_employee_ids: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class LockedEmployeeAccessRows:
    employees: tuple[Employee, ...]
    accesses: tuple[EmployeeAccess, ...]

    def employee_by_id(self, employee_id: int) -> Employee:
        for employee in self.employees:
            if employee.pk == employee_id:
                return employee
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.EMPLOYEE_MISSING,
        )

    def access_by_id(self, access_id: int) -> EmployeeAccess:
        for access in self.accesses:
            if access.pk == access_id:
                return access
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.ACCESS_MISSING,
        )


def _normalized_ids(values: Iterable[int]) -> tuple[int, ...]:
    normalized: set[int] = set()
    try:
        iterator = iter(values)
    except TypeError:
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.INVALID_IDENTIFIER,
        ) from None

    for value in iterator:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EmployeeAccessLockPlanError(
                EmployeeAccessLockPlanError.INVALID_IDENTIFIER,
            )
        normalized.add(value)
    return tuple(sorted(normalized))


def _validate_plan_structure(plan: EmployeeAccessLockPlan) -> None:
    if not isinstance(plan, EmployeeAccessLockPlan):
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.INCOMPLETE_PLAN,
        )
    if (
        plan.employee_ids != tuple(sorted(set(plan.employee_ids)))
        or plan.access_ids != tuple(sorted(set(plan.access_ids)))
        or not plan.employee_ids
    ):
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.INCOMPLETE_PLAN,
        )

    expected_mapping = tuple(sorted(plan.expected_access_employee_ids))
    if (
        plan.expected_access_employee_ids != expected_mapping
        or tuple(access_id for access_id, _ in expected_mapping) != plan.access_ids
        or any(
            employee_id not in plan.employee_ids
            for _, employee_id in expected_mapping
        )
    ):
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.INCOMPLETE_PLAN,
        )


def build_employee_access_lock_plan(
    *,
    access_ids: Iterable[int],
    employee_ids: Iterable[int] = (),
    using: str = DEFAULT_DB_ALIAS,
) -> EmployeeAccessLockPlan:
    normalized_access_ids = _normalized_ids(access_ids)
    normalized_employee_ids = _normalized_ids(employee_ids)
    if not normalized_access_ids and not normalized_employee_ids:
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.EMPTY_PLAN,
        )

    expected_mapping = tuple(
        EmployeeAccess.objects.using(using)
        .filter(pk__in=normalized_access_ids)
        .order_by('pk')
        .values_list('pk', 'employee_id')
    )
    if tuple(access_id for access_id, _ in expected_mapping) != normalized_access_ids:
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.ACCESS_MISSING,
        )

    all_employee_ids = tuple(sorted({
        *normalized_employee_ids,
        *(employee_id for _, employee_id in expected_mapping),
    }))
    plan = EmployeeAccessLockPlan(
        using=using,
        employee_ids=all_employee_ids,
        access_ids=normalized_access_ids,
        expected_access_employee_ids=expected_mapping,
    )
    _validate_plan_structure(plan)
    return plan


def _lock_employees(plan: EmployeeAccessLockPlan) -> tuple[Employee, ...]:
    return tuple(
        Employee.objects.using(plan.using)
        .select_for_update(of=('self',))
        .filter(pk__in=plan.employee_ids)
        .order_by('pk')
    )


def _lock_accesses(plan: EmployeeAccessLockPlan) -> tuple[EmployeeAccess, ...]:
    return tuple(
        EmployeeAccess.objects.using(plan.using)
        .select_related('role')
        .select_for_update(of=('self',))
        .filter(pk__in=plan.access_ids)
        .order_by('pk')
    )


def lock_employee_access_plan(
    plan: EmployeeAccessLockPlan,
) -> LockedEmployeeAccessRows:
    _validate_plan_structure(plan)
    if not connections[plan.using].in_atomic_block:
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.OUTSIDE_ATOMIC,
        )

    employees = _lock_employees(plan)
    if tuple(employee.pk for employee in employees) != plan.employee_ids:
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.EMPLOYEE_MISSING,
        )

    accesses = _lock_accesses(plan)
    if tuple(access.pk for access in accesses) != plan.access_ids:
        raise EmployeeAccessLockPlanError(
            EmployeeAccessLockPlanError.ACCESS_MISSING,
        )

    expected_mapping = dict(plan.expected_access_employee_ids)
    planned_employee_ids = set(plan.employee_ids)
    for access in accesses:
        if access.employee_id not in planned_employee_ids:
            raise EmployeeAccessLockPlanError(
                EmployeeAccessLockPlanError.ACCESS_EMPLOYEE_OUTSIDE_PLAN,
            )
        if access.employee_id != expected_mapping[access.pk]:
            raise EmployeeAccessLockPlanError(
                EmployeeAccessLockPlanError.ACCESS_MAPPING_CHANGED,
            )

    return LockedEmployeeAccessRows(
        employees=employees,
        accesses=accesses,
    )
