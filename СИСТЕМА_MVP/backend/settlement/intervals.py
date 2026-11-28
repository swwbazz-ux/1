"""Эффективные интервалы проживания (SET-R076).

Правило ``SET-R076`` требует считать окончание фактического проживания как
``min(ends_at, terminated_at)``: досрочное прекращение сокращает плановый
интервал и не должно удерживать койку или жильца в проверках пересечений.

Модуль намеренно не импортирует Django: его используют чистый валидатор,
resolver и точка применения.
"""

from __future__ import annotations

from datetime import datetime


def effective_ends_at(
    ends_at: datetime | None,
    terminated_at: datetime | None,
) -> datetime | None:
    """Вернуть эффективное окончание ``min(ends_at, terminated_at)``.

    ``None`` означает открытый интервал. Если задано только одно окончание,
    возвращается оно.
    """

    endings = [
        value
        for value in (ends_at, terminated_at)
        if value is not None
    ]
    return min(endings) if endings else None


def intervals_overlap(
    starts_at: datetime,
    ends_at: datetime | None,
    other_starts_at: datetime,
    other_ends_at: datetime | None,
) -> bool:
    """Проверить пересечение двух полуоткрытых интервалов ``[start, end)``."""

    return (
        other_ends_at is None or starts_at < other_ends_at
    ) and (
        ends_at is None or other_starts_at < ends_at
    )


def occupancy_interval(occupancy) -> tuple[datetime, datetime | None]:
    """Эффективный интервал строки ``EmployeeBedOccupancy``."""

    return occupancy.starts_at, effective_ends_at(
        occupancy.ends_at,
        occupancy.terminated_at,
    )


def occupancy_overlaps_period(
    *,
    occupancy,
    starts_at: datetime,
    ends_at: datetime | None,
) -> bool:
    """Пересекается ли проживание с проверяемым периодом."""

    occupancy_start, occupancy_end = occupancy_interval(occupancy)
    return intervals_overlap(
        occupancy_start,
        occupancy_end,
        starts_at,
        ends_at,
    )


__all__ = [
    'effective_ends_at',
    'intervals_overlap',
    'occupancy_interval',
    'occupancy_overlaps_period',
]
