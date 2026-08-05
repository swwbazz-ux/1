from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction

from core.db_locks import lock_idempotency_key
from core.production_time import production_work_date
from users.models import AdminActionLog

from .models import RatingPeriod
from .rating_period_calendar import (
    RATING_PERIOD_CATALOG_LOCK_ACTION,
    RATING_PERIOD_CATALOG_LOCK_KEY,
    RATING_PERIOD_DEFAULT_MONTHS_AHEAD,
    RATING_PERIOD_DEFAULT_START_DAY,
    add_calendar_months,
    nominal_rating_period_end,
    nominal_rating_period_start,
    rating_period_display_name,
)


class RatingPeriodCatalogConflict(ValidationError):
    pass


@dataclass(frozen=True)
class RatingPeriodCalendarInspection:
    as_of: date
    current_nominal_start: date
    horizon_end: date
    period_count: int
    automatic_count: int
    manual_count: int
    override_count: int
    gap_ranges: tuple
    overlap_pairs: tuple
    prepared_through: date

    @property
    def is_ready(self):
        return not self.gap_ranges and not self.overlap_pairs


@dataclass(frozen=True)
class RatingPeriodGenerationResult:
    as_of: date
    bootstrap: bool
    created_ids: tuple
    preserved_nominal_starts: tuple
    skipped_overlap_nominal_starts: tuple
    inspection: RatingPeriodCalendarInspection

    @property
    def created_count(self):
        return len(self.created_ids)


def _periods_for_inspection(current_start, horizon_end):
    return list(
        RatingPeriod.objects
        .filter(
            starts_on__lt=horizon_end,
            ends_before__gt=current_start,
        )
        .order_by('starts_on', 'ends_before', 'id')
    )


def _active_overlap_pairs(periods):
    active_periods = sorted(
        (period for period in periods if period.is_active),
        key=lambda period: (period.starts_on, period.ends_before, period.id),
    )
    overlaps = []
    for index, period in enumerate(active_periods):
        for following in active_periods[index + 1:]:
            if following.starts_on >= period.ends_before:
                break
            if (
                period.starts_on < following.ends_before
                and period.ends_before > following.starts_on
            ):
                overlaps.append((period.id, following.id))
    return tuple(overlaps)


def _active_gap_ranges(periods, current_start, horizon_end):
    active_intervals = sorted(
        (
            (
                max(period.starts_on, current_start),
                min(period.ends_before, horizon_end),
            )
            for period in periods
            if (
                period.is_active
                and period.starts_on < horizon_end
                and period.ends_before > current_start
            )
        ),
        key=lambda interval: (interval[0], interval[1]),
    )
    gaps = []
    cursor = current_start
    for starts_on, ends_before in active_intervals:
        if ends_before <= cursor:
            continue
        if starts_on > cursor:
            gaps.append((cursor, starts_on))
        cursor = max(cursor, ends_before)
        if cursor >= horizon_end:
            break
    if cursor < horizon_end:
        gaps.append((cursor, horizon_end))
    return tuple(gaps), cursor


def inspect_rating_period_calendar(
    *,
    as_of=None,
    months_ahead=RATING_PERIOD_DEFAULT_MONTHS_AHEAD,
):
    as_of = as_of or production_work_date()
    if not isinstance(as_of, date):
        raise TypeError('as_of должен быть датой.')
    if not isinstance(months_ahead, int) or not 0 <= months_ahead <= 60:
        raise ValueError('months_ahead должен быть целым числом от 0 до 60.')

    current_start = nominal_rating_period_start(as_of)
    horizon_end = nominal_rating_period_end(
        add_calendar_months(current_start, months_ahead)
    )
    periods = _periods_for_inspection(current_start, horizon_end)
    gaps, _last_covered_boundary = _active_gap_ranges(
        periods,
        current_start,
        horizon_end,
    )
    prepared_through = gaps[0][0] if gaps else horizon_end
    return RatingPeriodCalendarInspection(
        as_of=as_of,
        current_nominal_start=current_start,
        horizon_end=horizon_end,
        period_count=len(periods),
        automatic_count=sum(
            period.nominal_starts_on is not None
            for period in periods
        ),
        manual_count=sum(
            period.nominal_starts_on is None
            for period in periods
        ),
        override_count=sum(
            period.has_manual_override
            for period in periods
        ),
        gap_ranges=gaps,
        overlap_pairs=_active_overlap_pairs(periods),
        prepared_through=prepared_through,
    )


def _validate_catalog_before_generation(periods):
    invalid_ids = [
        period.id
        for period in periods
        if period.ends_before <= period.starts_on
    ]
    if invalid_ids:
        raise RatingPeriodCatalogConflict(
            'Автоматическое формирование остановлено: найдены периоды '
            f'с неверными границами (ID: {", ".join(map(str, invalid_ids))}).'
        )

    overlaps = _active_overlap_pairs(periods)
    if overlaps:
        labels = ', '.join(
            f'{first_id}/{second_id}'
            for first_id, second_id in overlaps
        )
        raise RatingPeriodCatalogConflict(
            'Автоматическое формирование остановлено: найдены '
            f'пересекающиеся активные периоды (ID: {labels}).'
        )


def _period_audit_value(period):
    return period.audit_value()


def ensure_rating_periods(
    *,
    as_of=None,
    months_ahead=RATING_PERIOD_DEFAULT_MONTHS_AHEAD,
):
    as_of = as_of or production_work_date()
    if not isinstance(as_of, date):
        raise TypeError('as_of должен быть датой.')
    if not isinstance(months_ahead, int) or not 0 <= months_ahead <= 60:
        raise ValueError('months_ahead должен быть целым числом от 0 до 60.')

    current_start = nominal_rating_period_start(
        as_of,
        start_day=RATING_PERIOD_DEFAULT_START_DAY,
    )

    with transaction.atomic():
        lock_idempotency_key(
            RATING_PERIOD_CATALOG_LOCK_ACTION,
            RATING_PERIOD_CATALOG_LOCK_KEY,
        )
        existing_periods = list(
            RatingPeriod.objects
            .select_for_update()
            .order_by('starts_on', 'ends_before', 'id')
        )
        _validate_catalog_before_generation(existing_periods)

        bootstrap = not existing_periods
        first_offset = 0 if bootstrap else 1
        created = []
        preserved_nominal_starts = []
        skipped_overlap_nominal_starts = []

        for offset in range(first_offset, months_ahead + 1):
            nominal_starts_on = add_calendar_months(
                current_start,
                offset,
            )
            nominal_ends_before = nominal_rating_period_end(
                nominal_starts_on
            )

            same_nominal = next(
                (
                    period
                    for period in existing_periods
                    if period.nominal_starts_on == nominal_starts_on
                ),
                None,
            )
            if same_nominal is not None:
                preserved_nominal_starts.append(nominal_starts_on)
                continue

            exact_manual = next(
                (
                    period
                    for period in existing_periods
                    if (
                        period.nominal_starts_on is None
                        and period.starts_on == nominal_starts_on
                        and period.ends_before == nominal_ends_before
                    )
                ),
                None,
            )
            if exact_manual is not None:
                preserved_nominal_starts.append(nominal_starts_on)
                continue

            overlapping_active = [
                period
                for period in existing_periods
                if (
                    period.is_active
                    and period.starts_on < nominal_ends_before
                    and period.ends_before > nominal_starts_on
                )
            ]
            if overlapping_active:
                skipped_overlap_nominal_starts.append(
                    nominal_starts_on
                )
                continue

            period = RatingPeriod(
                name=rating_period_display_name(
                    nominal_starts_on,
                    nominal_ends_before,
                ),
                starts_on=nominal_starts_on,
                ends_before=nominal_ends_before,
                nominal_starts_on=nominal_starts_on,
                comment='',
                is_active=True,
            )
            period.save()
            existing_periods.append(period)
            created.append(period)
            AdminActionLog.objects.create(
                actor=None,
                action='Период рейтинга создан автоматически',
                action_code='rating_period_auto_created',
                object_type=period.__class__.__name__,
                object_id=str(period.pk),
                object_repr=str(period),
                new_value=_period_audit_value(period),
                comment='Ежедневное правило 14-е → 14-е.',
            )

        inspection = inspect_rating_period_calendar(
            as_of=as_of,
            months_ahead=months_ahead,
        )
        return RatingPeriodGenerationResult(
            as_of=as_of,
            bootstrap=bootstrap,
            created_ids=tuple(period.id for period in created),
            preserved_nominal_starts=tuple(preserved_nominal_starts),
            skipped_overlap_nominal_starts=tuple(
                skipped_overlap_nominal_starts
            ),
            inspection=inspection,
        )
