from calendar import monthrange
from datetime import date, timedelta


RATING_PERIOD_DEFAULT_START_DAY = 14
RATING_PERIOD_DEFAULT_MONTHS_AHEAD = 12
RATING_PERIOD_CATALOG_LOCK_ACTION = 'rating_period_catalog'
RATING_PERIOD_CATALOG_LOCK_KEY = 'global'


def add_calendar_months(value, months):
    if not isinstance(value, date):
        raise TypeError('value должен быть датой.')
    if not isinstance(months, int):
        raise TypeError('months должен быть целым числом.')

    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def nominal_rating_period_start(
    as_of,
    *,
    start_day=RATING_PERIOD_DEFAULT_START_DAY,
):
    if not isinstance(as_of, date):
        raise TypeError('as_of должен быть датой.')
    if not 1 <= start_day <= 28:
        raise ValueError('start_day должен быть от 1 до 28.')

    this_month_start = date(as_of.year, as_of.month, start_day)
    if as_of >= this_month_start:
        return this_month_start
    return add_calendar_months(this_month_start, -1)


def nominal_rating_period_end(nominal_starts_on):
    return add_calendar_months(nominal_starts_on, 1)


def rating_period_display_name(starts_on, ends_before):
    last_included_date = ends_before - timedelta(days=1)
    return (
        f'Рейтинг {starts_on:%d.%m.%Y}–'
        f'{last_included_date:%d.%m.%Y}'
    )
