from __future__ import annotations

from dataclasses import dataclass

from core.production_time import (
    business_localtime,
    production_work_date,
)
from django.db.models import F, OuterRef, Subquery
from django.utils.dateparse import parse_datetime

from shifts.models import ShiftType
from users.models import Employee

from .models import DriverShiftPassportSnapshot


@dataclass(frozen=True)
class DriverRatingGroupScope:
    allowed_employee_ids: tuple[int, ...]
    expected_employee_ids: tuple[int, ...]
    historical_employee_ids: tuple[int, ...]
    latest_closed_at: dict[int, object]


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

    from portal.services import active_employees

    driver_rows = tuple(
        active_employees()
        .filter(work_category=Employee.WorkCategory.DRIVER)
        .values_list('id', 'watch_composition_id')
    )
    allowed_employee_ids = tuple(
        sorted(employee_id for employee_id, _ in driver_rows)
    )
    expected_employee_ids = tuple(sorted(
        employee_id
        for employee_id, composition_id in driver_rows
        if composition_id == watch_composition.id
    ))
    historical = driver_rating_group_membership(
        rating_period,
        employee_ids=allowed_employee_ids,
        watch_composition_id=watch_composition.id,
        shift_type=shift_type,
    )
    return DriverRatingGroupScope(
        allowed_employee_ids=allowed_employee_ids,
        expected_employee_ids=expected_employee_ids,
        historical_employee_ids=historical['employee_ids'],
        latest_closed_at=historical['latest_closed_at'],
    )
