"""Read-model и подписи простоев для Диспетчерского пульта."""

from django.db.models import Q
from django.utils import timezone

from downtimes.models import DowntimeEvent


EQUIPMENT_STATUS_COLOR_GROUPS = {
    'gray',
    'yellow',
    'green',
    'blue',
    'orange',
    'red',
}
DOWNTIME_CARD_MAX_ROWS = 6
# Ремонт и прочие критические состояния всегда красные — это их смысл на доске.
DOWNTIME_CARD_CRITICAL_GROUPS = {'red', 'orange'}
# Остальные причины красим по очереди: ожидания все жёлтые по состоянию техники,
# и подряд идущие доли сливались в одну ленту.
DOWNTIME_CARD_PALETTE = ('yellow', 'blue', 'green')


def normalize_status_color_group(color_group, *, fallback='yellow'):
    if color_group in EQUIPMENT_STATUS_COLOR_GROUPS:
        return color_group
    return fallback


def downtime_reason_color_group(reason):
    if not reason:
        return 'yellow'
    return normalize_status_color_group(
        reason.effective_color_group,
        fallback='yellow',
    )


def dispatcher_alert_status_for_color_group(color_group):
    color_group = normalize_status_color_group(color_group, fallback='yellow')
    if color_group == 'red':
        return 'danger'
    if color_group in {'yellow', 'orange'}:
        return 'warning'
    if color_group == 'blue':
        return 'info'
    return 'ok'


def dispatcher_alert_status_for_downtime(downtime):
    return dispatcher_alert_status_for_color_group(
        downtime_reason_color_group(getattr(downtime, 'reason', None))
    )


def format_duration_label(seconds):
    seconds = max(0, int(seconds or 0))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f'{hours:02d}:{minutes:02d}:{seconds % 60:02d}'


def format_dispatcher_datetime(value):
    if not value:
        return ''
    return timezone.localtime(value).strftime('%d.%m %H:%M')


def dispatcher_downtime_card_payload(event, *, calculated_at=None):
    if not event:
        return {'active': False}
    calculated_at = calculated_at or timezone.now()
    started_at = event.started_at or calculated_at
    elapsed_seconds = max(0, int((calculated_at - started_at).total_seconds()))
    reason = getattr(event, 'reason', None)
    return {
        'active': True,
        'event_id': event.id,
        'equipment_id': event.equipment_id,
        'reason': reason.button_label if reason else 'Простой',
        'started_at': started_at.isoformat(),
        'started_at_label': format_dispatcher_datetime(started_at),
        'elapsed_seconds': elapsed_seconds,
        'elapsed_label': format_duration_label(elapsed_seconds),
    }


def format_dispatcher_downtime_duration(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f'{seconds} с'
    minutes, rest_seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if not hours and minutes < 10:
        return f'{minutes} мин {rest_seconds} с' if rest_seconds else f'{minutes} мин'
    if not hours:
        return f'{minutes} мин'
    if not minutes:
        return f'{hours} ч'
    return f'{hours} ч {minutes} мин'


def dispatcher_downtime_count_label(count):
    count = int(count or 0)
    remainder_100 = count % 100
    remainder_10 = count % 10
    if 11 <= remainder_100 <= 14:
        word = 'событий'
    elif remainder_10 == 1:
        word = 'событие'
    elif 2 <= remainder_10 <= 4:
        word = 'события'
    else:
        word = 'событий'
    return f'{count} {word}'


def dispatcher_shift_downtime_rows(equipment, shift, *, now=None):
    """Простои техники за её текущую смену, сгруппированные по причине.

    Событие, начавшееся внутри смены, считается до его завершения, закрытия смены
    или текущего момента. Событие, открытое раньше смены, считается с границы смены
    и помечается как переданное.
    """
    now = now or timezone.now()
    if not equipment or not shift or not shift.opened_at:
        return []
    period_end = min(shift.closed_at or now, now)
    if period_end <= shift.opened_at:
        return []
    events = (
        DowntimeEvent.objects
        .filter(equipment=equipment, started_at__lt=period_end)
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=shift.opened_at))
        .select_related('reason', 'reason__equipment_state')
        .order_by('-started_at')[:400]
    )
    grouped = {}
    for event in events:
        is_inherited = event.started_at < shift.opened_at
        count_from = max(event.started_at, shift.opened_at)
        event_end = min(event.ended_at or period_end, period_end)
        seconds = max(0, int((event_end - count_from).total_seconds()))
        reason = getattr(event, 'reason', None)
        row = grouped.setdefault(event.reason_id or 0, {
            'label': reason.button_label if reason else 'Простой',
            'color_group': downtime_reason_color_group(reason),
            'accent': 'yellow',
            'seconds': 0,
            'count': 0,
            'is_open': False,
            'is_inherited': False,
            'last_started_at': None,
        })
        row['seconds'] += seconds
        row['count'] += 1
        if event.ended_at is None and not shift.closed_at:
            row['is_open'] = True
        if is_inherited:
            row['is_inherited'] = True
        if row['last_started_at'] is None or event.started_at > row['last_started_at']:
            row['last_started_at'] = event.started_at
    rows = sorted(grouped.values(), key=lambda row: row['seconds'], reverse=True)
    palette_index = 0
    for row in rows:
        if row.get('color_group') in DOWNTIME_CARD_CRITICAL_GROUPS:
            row['accent'] = 'red'
            continue
        row['accent'] = DOWNTIME_CARD_PALETTE[
            palette_index % len(DOWNTIME_CARD_PALETTE)
        ]
        palette_index += 1
    return rows


def dispatcher_downtime_row_meta(row):
    parts = [dispatcher_downtime_count_label(row.get('count'))]
    if row.get('is_inherited'):
        parts.append('передан со смены')
    if row.get('is_open'):
        parts.append('идёт сейчас')
    elif row.get('last_started_at'):
        parts.append(
            f"последний {format_dispatcher_datetime(row['last_started_at'])}"
        )
    return ' · '.join(parts)


def dispatcher_downtime_report_extras(equipment, shift, *, now=None):
    """Метрики и вкладка простоев текущей смены для карточки техники."""
    now = now or timezone.now()
    metrics = []
    charts = []
    if not equipment or not shift or not shift.opened_at:
        return metrics, charts
    period_end = min(shift.closed_at or now, now)
    shift_elapsed_seconds = max(0, int((period_end - shift.opened_at).total_seconds()))
    rows = dispatcher_shift_downtime_rows(equipment, shift, now=now)
    total_seconds = sum(row['seconds'] for row in rows)
    if not total_seconds or not shift_elapsed_seconds:
        return metrics, charts
    share_percent = min(100, round(total_seconds * 100 / shift_elapsed_seconds))
    metrics.append({
        'label': 'Простои',
        'value': format_dispatcher_downtime_duration(total_seconds),
    })
    metrics.append({'label': 'Доля смены', 'value': f'{share_percent}%'})
    charts.append({
        'type': 'donut-list',
        'title': 'Простои смены',
        'summary': ' · '.join([
            f'Всего простоев {format_dispatcher_downtime_duration(total_seconds)}',
            f'{share_percent}% смены',
            dispatcher_downtime_count_label(sum(row['count'] for row in rows)),
        ]),
        'rows': [
            {
                'label': row['label'],
                'value': format_dispatcher_downtime_duration(row['seconds']),
                'percent': min(
                    100,
                    round(row['seconds'] * 100 / shift_elapsed_seconds),
                ),
                'accent': row['accent'],
                'meta': dispatcher_downtime_row_meta(row),
            }
            for row in rows[:DOWNTIME_CARD_MAX_ROWS]
        ],
    })
    return metrics, charts


def dispatcher_report_with_downtimes(report, equipment, shift, *, now=None):
    metrics, charts = dispatcher_downtime_report_extras(
        equipment,
        shift,
        now=now,
    )
    if not metrics and not charts:
        return report
    merged = dict(report or {})
    merged['metrics'] = list(merged.get('metrics') or []) + metrics
    merged['charts'] = list(merged.get('charts') or []) + charts
    return merged
