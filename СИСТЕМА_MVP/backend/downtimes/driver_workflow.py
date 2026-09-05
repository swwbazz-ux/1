from django.utils import timezone

from .defaults import normalize_reason_name
from .models import DowntimeEvent


DRIVER_DOWNTIME_FLOW_WAITING_LOADING = 'waiting_loading'
DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD = 'waiting_unload'

TRUCK_WAITING_LOADING_REASON_NAME = 'Ожидание погрузки'
TRUCK_UNLOADING_WAIT_REASON_NAMES = (
    'Ожидание разгрузки',
    'Ожидание разгрузки ККД',
    'Ожидание разгрузки СКДР',
)


def _reason_name_key(value):
    if hasattr(value, 'name'):
        value = value.name
    return normalize_reason_name(value).casefold()


TRUCK_WAITING_LOADING_REASON_KEY = _reason_name_key(TRUCK_WAITING_LOADING_REASON_NAME)
TRUCK_UNLOADING_WAIT_REASON_KEYS = frozenset(
    _reason_name_key(name)
    for name in TRUCK_UNLOADING_WAIT_REASON_NAMES
)


def driver_downtime_flow(reason):
    reason_key = _reason_name_key(reason)
    if reason_key == TRUCK_WAITING_LOADING_REASON_KEY:
        return DRIVER_DOWNTIME_FLOW_WAITING_LOADING
    if reason_key in TRUCK_UNLOADING_WAIT_REASON_KEYS:
        return DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD
    return ''


def driver_downtime_requires_loaded_trip(reason):
    return driver_downtime_flow(reason) == DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD


def driver_downtime_requires_empty_truck(reason):
    return driver_downtime_flow(reason) == DRIVER_DOWNTIME_FLOW_WAITING_LOADING


def close_open_truck_downtimes_for_reasons(truck, reason_names, *, ended_at=None):
    """Close matching truck waits within the caller's database transaction."""
    if not truck:
        return 0
    reason_keys = frozenset(_reason_name_key(name) for name in reason_names)
    events = list(
        DowntimeEvent.objects
        .select_for_update(of=('self',))
        .select_related('reason')
        .filter(equipment=truck, ended_at__isnull=True)
        .order_by('id')
    )
    matching_events = [
        event
        for event in events
        if _reason_name_key(event.reason) in reason_keys
    ]
    if not matching_events:
        return 0
    closed_at = ended_at or timezone.now()
    for event in matching_events:
        event.ended_at = closed_at
        event.save(update_fields=['ended_at'])
    return len(matching_events)


def close_truck_waiting_loading_downtimes(truck, *, ended_at=None):
    return close_open_truck_downtimes_for_reasons(
        truck,
        (TRUCK_WAITING_LOADING_REASON_NAME,),
        ended_at=ended_at,
    )


def close_truck_unloading_wait_downtimes(truck, *, ended_at=None):
    return close_open_truck_downtimes_for_reasons(
        truck,
        TRUCK_UNLOADING_WAIT_REASON_NAMES,
        ended_at=ended_at,
    )
