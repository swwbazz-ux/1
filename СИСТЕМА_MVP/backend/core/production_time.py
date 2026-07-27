from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from shifts.models import ShiftType


BUSINESS_TIME_ZONE_NAME = 'Asia/Vladivostok'
BUSINESS_TIME_ZONE = ZoneInfo(BUSINESS_TIME_ZONE_NAME)
DAY_SHIFT_START = time(7, 0)
NIGHT_SHIFT_START = time(19, 0)


@dataclass(frozen=True)
class ProductionShiftContext:
    local_datetime: datetime
    shift_type: str
    production_date: object
    time_range: str


def business_localtime(value=None):
    current = value or timezone.now()
    if timezone.is_naive(current):
        current = timezone.make_aware(current, BUSINESS_TIME_ZONE)
    return current.astimezone(BUSINESS_TIME_ZONE)


def production_shift_context(value=None):
    local_datetime = business_localtime(value)
    local_time = local_datetime.time().replace(tzinfo=None)
    if DAY_SHIFT_START <= local_time < NIGHT_SHIFT_START:
        shift_type = ShiftType.DAY
        production_date = local_datetime.date()
        time_range = '07:00-19:00'
    else:
        shift_type = ShiftType.NIGHT
        production_date = (
            local_datetime.date()
            if local_time >= NIGHT_SHIFT_START
            else local_datetime.date() - timedelta(days=1)
        )
        time_range = '19:00-07:00'
    return ProductionShiftContext(
        local_datetime=local_datetime,
        shift_type=shift_type,
        production_date=production_date,
        time_range=time_range,
    )


def production_shift_type(value=None):
    return production_shift_context(value).shift_type


def production_work_date(value=None):
    return production_shift_context(value).production_date


def production_day_bounds(production_date):
    if not isinstance(production_date, date):
        raise TypeError('production_date must be a date')
    start = datetime.combine(production_date, DAY_SHIFT_START, tzinfo=BUSINESS_TIME_ZONE)
    return start, start + timedelta(days=1)


def production_shift_bounds(production_date, shift_type):
    day_start, production_end = production_day_bounds(production_date)
    if shift_type == ShiftType.DAY:
        return day_start, datetime.combine(
            production_date,
            NIGHT_SHIFT_START,
            tzinfo=BUSINESS_TIME_ZONE,
        )
    if shift_type == ShiftType.NIGHT:
        night_start = datetime.combine(
            production_date,
            NIGHT_SHIFT_START,
            tzinfo=BUSINESS_TIME_ZONE,
        )
        return night_start, production_end
    raise ValueError('shift_type must be day or night')
