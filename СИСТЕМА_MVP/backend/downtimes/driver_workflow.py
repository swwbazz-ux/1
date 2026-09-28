from django.db.models import Q
from django.utils import timezone

from .defaults import normalize_reason_name
from .models import DowntimeEvent


DRIVER_DOWNTIME_FLOW_WAITING_LOADING = 'waiting_loading'
DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD = 'waiting_unload'
DRIVER_DOWNTIME_WORK_FLOWS = frozenset({
    DRIVER_DOWNTIME_FLOW_WAITING_LOADING,
    DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD,
})

TRUCK_WAITING_LOADING_REASON_NAME = 'Ожидание погрузки'
TRUCK_UNLOADING_WAIT_REASON_NAMES = (
    'Ожидание разгрузки',
    'Ожидание разгрузки ККД',
    'Ожидание разгрузки СКДР',
)


EXCAVATOR_WORKFLOW_REASON_NAMES = (
    'Ожидание самосвалов',
    'Перегон экскаватора',
)

DOWNTIME_CLOSURE_WORK_RESUMED_BY_LOAD = 'work_resumed_by_load'


def _reason_name_key(value):
    if hasattr(value, 'name'):
        value = value.name
    return normalize_reason_name(value).casefold()


TRUCK_WAITING_LOADING_REASON_KEY = _reason_name_key(TRUCK_WAITING_LOADING_REASON_NAME)
TRUCK_UNLOADING_WAIT_REASON_KEYS = frozenset(
    _reason_name_key(name)
    for name in TRUCK_UNLOADING_WAIT_REASON_NAMES
)


WORKFLOW_REASON_KEYS = frozenset(
    _reason_name_key(name)
    for name in (
        TRUCK_WAITING_LOADING_REASON_NAME,
        *TRUCK_UNLOADING_WAIT_REASON_NAMES,
        *EXCAVATOR_WORKFLOW_REASON_NAMES,
    )
)


def is_workflow_downtime_reason(reason):
    """Простой рабочего процесса (ожидание, перегон), а не состояние техники."""
    return _reason_name_key(reason) in WORKFLOW_REASON_KEYS


def close_workflow_downtimes(equipment, *, ended_at=None):
    """Закрыть простои рабочего процесса у техники — например, при конце смены.

    Ремонт и другие состояния техники остаются открытыми: они переживают смену
    и передаются сменщику вместе с техникой.
    """
    return close_open_truck_downtimes_for_reasons(
        equipment,
        (
            TRUCK_WAITING_LOADING_REASON_NAME,
            *TRUCK_UNLOADING_WAIT_REASON_NAMES,
            *EXCAVATOR_WORKFLOW_REASON_NAMES,
        ),
        ended_at=ended_at,
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


# Когда водитель может включить простой (правила карьера, 26.09.2026):
# - только на разгруженном самосвале — ожидание погрузки, чистка кузова, ТО, заправка;
# - только на гружёном — ожидание разгрузки; ожидание разгрузки ККД/СКДР — только
#   если рейс идёт именно на эту точку;
# - в любом состоянии — поломка, ремонт, погода, БВР, прочие и остальные причины.
DRIVER_EMPTY_TRUCK_ONLY_REASON_NAMES = (
    TRUCK_WAITING_LOADING_REASON_NAME,
    'Чистка кузова',
    'ТО',
    'Заправка',
)
DRIVER_EMPTY_TRUCK_ONLY_REASON_KEYS = frozenset(
    _reason_name_key(name) for name in DRIVER_EMPTY_TRUCK_ONLY_REASON_NAMES
)
DRIVER_UNLOAD_WAIT_POINT_KEYS = {
    _reason_name_key('Ожидание разгрузки ККД'): _reason_name_key('ККД'),
    _reason_name_key('Ожидание разгрузки СКДР'): _reason_name_key('СКДР'),
}

DRIVER_EMPTY_TRUCK_REQUIRED_MESSAGE = 'Самосвал уже загружен'
DRIVER_LOADED_TRIP_REQUIRED_MESSAGE = 'Доступно только после погрузки'


def driver_downtime_requires_empty_truck(reason):
    return _reason_name_key(reason) in DRIVER_EMPTY_TRUCK_ONLY_REASON_KEYS


def driver_downtime_required_dump_point_key(reason):
    """Точка, на которую должен идти рейс для этого ожидания разгрузки ('' — любая)."""
    return DRIVER_UNLOAD_WAIT_POINT_KEYS.get(_reason_name_key(reason), '')


def driver_dump_point_matches(dump_point, point_key):
    if not point_key:
        return True
    if not dump_point:
        return False
    return _reason_name_key(str(dump_point)) == point_key


def driver_downtime_unavailable_message(reason, *, truck_loaded, dump_point=None):
    """Почему водитель не может начать этот простой сейчас ('' — может)."""
    if driver_downtime_requires_empty_truck(reason) and truck_loaded:
        return DRIVER_EMPTY_TRUCK_REQUIRED_MESSAGE
    if driver_downtime_requires_loaded_trip(reason):
        if not truck_loaded:
            return DRIVER_LOADED_TRIP_REQUIRED_MESSAGE
        point_key = driver_downtime_required_dump_point_key(reason)
        if not driver_dump_point_matches(dump_point, point_key):
            return 'Только при рейсе на ' + normalize_reason_name(reason.name).split()[-1]
    return ''


def driver_downtime_start_conflict(reason, truck):
    """Проверка сервера при старте простоя водителем: (код, текст) или None.

    Вызывать внутри транзакции — открытый рейс блокируется select_for_update.
    """
    if not (
        driver_downtime_requires_empty_truck(reason)
        or driver_downtime_requires_loaded_trip(reason)
    ):
        return None
    from trips.models import OPEN_TRIP_STATUSES, Trip, TripStatus

    open_trip = (
        Trip.objects.select_for_update(of=('self',))
        .filter(truck=truck, status__in=OPEN_TRIP_STATUSES)
        .select_related('dump_point', 'actual_dump_point')
        .order_by('-created_at')
        .first()
    )
    loaded = bool(open_trip and open_trip.status == TripStatus.LOADED_WAITING_UNLOAD)
    if driver_downtime_requires_empty_truck(reason) and open_trip:
        return (
            'empty_truck_required',
            f'{normalize_reason_name(reason.name)} нельзя начать: самосвал уже загружен.',
        )
    message = driver_downtime_unavailable_message(
        reason,
        truck_loaded=loaded,
        dump_point=(open_trip.actual_dump_point or open_trip.dump_point) if open_trip else None,
    )
    if message == DRIVER_LOADED_TRIP_REQUIRED_MESSAGE:
        return ('loaded_trip_required', 'Этот простой доступен только после погрузки самосвала.')
    if message:
        return ('dump_point_mismatch', message + '.')
    return None


def driver_downtime_opens_work(reason):
    return driver_downtime_flow(reason) in DRIVER_DOWNTIME_WORK_FLOWS


def close_open_truck_downtimes_for_reasons(truck, reason_names, *, ended_at=None):
    """Close matching waits at their factual boundary.

    When ``ended_at`` is historical, also shorten an interval that was closed
    later but still overlapped that boundary. Intervals starting after the
    worker's action are never touched.
    """
    if not truck:
        return 0
    reason_keys = frozenset(_reason_name_key(name) for name in reason_names)
    events_queryset = (
        DowntimeEvent.objects
        .select_for_update(of=('self',))
        .select_related('reason')
        .filter(equipment=truck)
    )
    if ended_at is None:
        events_queryset = events_queryset.filter(ended_at__isnull=True)
    else:
        events_queryset = events_queryset.filter(
            started_at__lte=ended_at,
        ).filter(Q(ended_at__isnull=True) | Q(ended_at__gt=ended_at))
    events = list(events_queryset.order_by('id'))
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


def close_downtimes_resumed_by_load(*, truck, excavator, loaded_at):
    """Close only downtime intervals that overlap the factual load time.

    The load itself proves that both participating machines resumed work at
    ``loaded_at``.  Earlier closed intervals and later-started intervals are
    historical facts and must not be rewritten.
    """
    equipment_ids = {
        equipment_id
        for equipment_id in (
            getattr(truck, 'pk', truck),
            getattr(excavator, 'pk', excavator),
        )
        if equipment_id
    }
    if not equipment_ids:
        return 0
    events = list(
        DowntimeEvent.objects
        .select_for_update(of=('self',))
        .filter(
            equipment_id__in=equipment_ids,
            started_at__lte=loaded_at,
        )
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=loaded_at))
        .order_by('id')
    )
    for event in events:
        event.ended_at = loaded_at
        event.closure_reason = DOWNTIME_CLOSURE_WORK_RESUMED_BY_LOAD
        event.save(update_fields=['ended_at', 'closure_reason'])
    return len(events)
