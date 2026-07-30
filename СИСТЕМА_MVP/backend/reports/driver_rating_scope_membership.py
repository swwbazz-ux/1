from __future__ import annotations

from dataclasses import dataclass

from core.production_time import (
    business_localtime,
    production_day_bounds,
    production_work_date,
)
from django.db.models import F, OuterRef, Q, Subquery
from django.utils.dateparse import parse_datetime

from shifts.models import EmployeeShift, ShiftType
from users.models import Employee

from .models import DriverShiftPassportSnapshot


@dataclass(frozen=True)
class DriverRatingGroupScope:
    allowed_employee_ids: tuple[int, ...]
    expected_employee_ids: tuple[int, ...]
    historical_employee_ids: tuple[int, ...]
    latest_closed_at: dict[int, object]


@dataclass(frozen=True)
class DriverRatingCurrentGroupScope:
    allowed_employee_ids: tuple[int, ...]
    expected_employee_ids: tuple[int, ...]
    latest_closed_at: dict[int, object]


def _active_site_driver_rows():
    from portal.services import active_employees

    return tuple(
        active_employees()
        .filter(work_category=Employee.WorkCategory.DRIVER)
        .values_list('id', 'watch_composition_id')
    )


def _closed_driver_shift_rows(
    rating_period,
    *,
    employee_ids,
    shift_types,
):
    employee_ids = tuple(sorted({
        int(employee_id)
        for employee_id in employee_ids
    }))
    shift_types = tuple(dict.fromkeys(shift_types))
    if not employee_ids or not shift_types:
        return ()
    if any(
        shift_type not in {ShiftType.DAY, ShiftType.NIGHT}
        for shift_type in shift_types
    ):
        raise ValueError(
            'Для текущего состава рейтинга допустимы только '
            'shift_type day и night.'
        )
    period_start = production_day_bounds(
        rating_period.starts_on
    )[0]
    period_end = production_day_bounds(
        rating_period.ends_before
    )[0]
    return tuple(
        EmployeeShift.objects
        .filter(
            employee_id__in=employee_ids,
            shift_type__in=shift_types,
            closed_at__isnull=False,
            opened_at__gte=period_start,
            opened_at__lt=period_end,
        )
        .filter(
            Q(workplace_code='driver')
            | Q(
                workplace_code='',
                equipment__equipment_type__name__contains='Самосвал',
            )
        )
        .values_list(
            'employee_id',
            'watch_period__watch_composition_id',
            'shift_type',
            'closed_at',
        )
        .order_by('employee_id', 'closed_at', 'id')
    )


def linked_driver_closed_shift_groups(
    rating_period,
    *,
    employee_ids,
    shift_types,
):
    """Discover immutable linked groups without requiring a passport."""

    return tuple(sorted({
        (int(composition_id), shift_type)
        for (
            _employee_id,
            composition_id,
            shift_type,
            _closed_at,
        ) in _closed_driver_shift_rows(
            rating_period,
            employee_ids=employee_ids,
            shift_types=shift_types,
        )
        if composition_id is not None
    }))


def _current_group_scope_from_driver_rows(
    rating_period,
    watch_composition,
    *,
    shift_type,
    driver_rows,
):
    if shift_type not in {ShiftType.DAY, ShiftType.NIGHT}:
        raise ValueError(
            'Для текущего состава рейтинга требуется shift_type: '
            'day или night.'
        )
    if rating_period.ends_before <= rating_period.starts_on:
        raise ValueError(
            'Конец периода рейтинга должен быть позже даты начала.'
        )

    current_composition_by_employee = {
        int(employee_id): composition_id
        for employee_id, composition_id in driver_rows
    }
    site_employee_ids = tuple(sorted(
        current_composition_by_employee
    ))
    if not site_employee_ids:
        return DriverRatingCurrentGroupScope(
            allowed_employee_ids=(),
            expected_employee_ids=(),
            latest_closed_at={},
        )

    closed_shift_rows = _closed_driver_shift_rows(
        rating_period,
        employee_ids=site_employee_ids,
        shift_types=(shift_type,),
    )
    expected_employee_ids = tuple(sorted({
        int(employee_id)
        for (
            employee_id,
            shift_composition_id,
            _shift_type,
            _closed_at,
        ) in closed_shift_rows
        if (
            shift_composition_id == watch_composition.id
            or (
                shift_composition_id is None
                and current_composition_by_employee[int(employee_id)]
                == watch_composition.id
            )
        )
    }))
    latest_closed_at = {}
    for (
        employee_id,
        shift_composition_id,
        _shift_type,
        closed_at,
    ) in closed_shift_rows:
        employee_id = int(employee_id)
        if shift_composition_id != watch_composition.id:
            continue
        previous = latest_closed_at.get(employee_id)
        if previous is None or closed_at > previous:
            latest_closed_at[employee_id] = closed_at
    return DriverRatingCurrentGroupScope(
        allowed_employee_ids=site_employee_ids,
        expected_employee_ids=expected_employee_ids,
        latest_closed_at=latest_closed_at,
    )


def discover_driver_rating_current_scope(
    rating_period,
    watch_composition,
    *,
    shift_type,
):
    """Return current active/site scope without reading passports."""

    return _current_group_scope_from_driver_rows(
        rating_period,
        watch_composition,
        shift_type=shift_type,
        driver_rows=_active_site_driver_rows(),
    )


def linked_driver_snapshot_scopes(rating_period, *, employee_ids):
    """Возвращает исторические группы по последним паспортам смен.

    Границы периода, сотрудник, состав и тип смены читаются из неизменяемого
    source_manifest. Текущая карточка Employee здесь намеренно не участвует.
    """

    employee_ids = tuple(sorted({int(value) for value in employee_ids}))
    if not employee_ids:
        return ()
    latest_snapshot_id = (
        DriverShiftPassportSnapshot.objects
        .filter(shift_id=OuterRef('shift_id'))
        .order_by('-revision', '-id')
        .values('id')[:1]
    )
    snapshots = (
        DriverShiftPassportSnapshot.objects
        .filter(
            id=Subquery(latest_snapshot_id),
            payload__source_manifest__shift__employee_id__in=employee_ids,
        )
        .annotate(
            manifest_employee_id=F(
                'payload__source_manifest__shift__employee_id',
            ),
            manifest_opened_at=F(
                'payload__source_manifest__shift__opened_at',
            ),
            manifest_closed_at=F(
                'payload__source_manifest__shift__closed_at',
            ),
            manifest_shift_type=F(
                'payload__source_manifest__shift__shift_type',
            ),
            manifest_watch_composition_id=F(
                'payload__source_manifest__shift__watch_period'
                '__watch_composition__id',
            ),
        )
        .values(
            'id',
            'shift_id',
            'manifest_employee_id',
            'manifest_opened_at',
            'manifest_closed_at',
            'manifest_shift_type',
            'manifest_watch_composition_id',
        )
        .order_by('shift_id')
    )

    result = []
    for snapshot in snapshots.iterator(chunk_size=1000):
        employee_id = snapshot['manifest_employee_id']
        try:
            employee_id = int(employee_id)
        except (TypeError, ValueError):
            continue
        if employee_id not in employee_ids:
            continue
        opened_at = parse_datetime(
            str(snapshot['manifest_opened_at'] or ''),
        )
        closed_at = parse_datetime(
            str(snapshot['manifest_closed_at'] or ''),
        )
        if opened_at is None or closed_at is None:
            continue
        work_date = production_work_date(opened_at)
        if not (
            rating_period.starts_on
            <= work_date
            < rating_period.ends_before
        ):
            continue
        shift_type = snapshot['manifest_shift_type']
        if shift_type not in {ShiftType.DAY, ShiftType.NIGHT}:
            continue
        try:
            watch_composition_id = int(
                snapshot['manifest_watch_composition_id'],
            )
        except (TypeError, ValueError):
            continue
        result.append({
            'snapshot_id': snapshot['id'],
            'shift_id': snapshot['shift_id'],
            'employee_id': employee_id,
            'watch_composition_id': watch_composition_id,
            'shift_type': shift_type,
            'opened_at': business_localtime(opened_at),
            'closed_at': business_localtime(closed_at),
        })
    return tuple(result)


def driver_rating_group_membership(
    rating_period,
    *,
    employee_ids,
    watch_composition_id,
    shift_type,
):
    """Собирает участников одной группы из того же снимка БД, что и KPI."""

    members = set()
    latest_closed_at = {}
    for item in linked_driver_snapshot_scopes(
        rating_period,
        employee_ids=employee_ids,
    ):
        if (
            item['watch_composition_id'] != watch_composition_id
            or item['shift_type'] != shift_type
        ):
            continue
        employee_id = int(item['employee_id'])
        members.add(employee_id)
        previous = latest_closed_at.get(employee_id)
        if previous is None or item['closed_at'] > previous:
            latest_closed_at[employee_id] = item['closed_at']
    return {
        'employee_ids': tuple(sorted(members)),
        'latest_closed_at': latest_closed_at,
    }


def discover_driver_rating_group_scope(
    rating_period,
    watch_composition,
    *,
    shift_type,
):
    """Читает полный состав расчёта внутри транзакции материализации."""

    driver_rows = _active_site_driver_rows()
    current = _current_group_scope_from_driver_rows(
        rating_period,
        watch_composition,
        shift_type=shift_type,
        driver_rows=driver_rows,
    )
    historical = driver_rating_group_membership(
        rating_period,
        employee_ids=tuple(
            sorted(employee_id for employee_id, _ in driver_rows)
        ),
        watch_composition_id=watch_composition.id,
        shift_type=shift_type,
    )
    latest_closed_at = dict(current.latest_closed_at)
    for employee_id, closed_at in historical[
        'latest_closed_at'
    ].items():
        previous = latest_closed_at.get(employee_id)
        if previous is None or closed_at > previous:
            latest_closed_at[employee_id] = closed_at
    return DriverRatingGroupScope(
        allowed_employee_ids=current.allowed_employee_ids,
        expected_employee_ids=current.expected_employee_ids,
        historical_employee_ids=historical['employee_ids'],
        latest_closed_at=latest_closed_at,
    )
