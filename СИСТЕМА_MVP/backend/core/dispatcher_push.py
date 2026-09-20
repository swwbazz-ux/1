"""Системные Web Push-оповещения закрытого Пульта диспетчера."""

from __future__ import annotations

import logging

from users.webpush import notify_role_web_subscribers


logger = logging.getLogger(__name__)

DISPATCHER_ROLE_CODE = 'dispatcher'
DISPATCHER_START_URL = '/dispatcher/control/'

# Сигналы models.signals с action=save нужны realtime-экранам, но не должны
# порождать второе системное уведомление рядом с богатым доменным событием.
IGNORED_ACTIONS = {'', 'save'}

TRIP_ACTIONS = {
    'truck_loaded': ('Самосвал загружен', 'На Пульте появился новый гружёный рейс.'),
    'truck_loaded_cancel': ('Погрузка отменена', 'Состояние рейса изменилось.'),
    'trip_unloaded': ('Самосвал разгружен', 'Рейс завершён. Пульт обновлён.'),
    'change_actual_unload_point': ('Изменена точка разгрузки', 'Маршрут текущего рейса изменился.'),
    'dispatcher_cancel_trip': ('Рейс отменён', 'Диспетчер отменил рейс.'),
    'dispatcher_complete_trip': ('Рейс завершён', 'Диспетчер завершил рейс.'),
    'dispatcher_manual_trip': ('Добавлен ручной рейс', 'Пульт и отчёты обновлены.'),
    'driver_free_bucket_selected': ('Свободный ковш', 'Водитель выбрал временный экскаватор.'),
    'driver_free_bucket_cancelled': ('Свободный ковш отменён', 'Временный выбор водителя отменён.'),
    'free_bucket_accepted': ('Свободный ковш принят', 'Машинист принял самосвал на одну погрузку.'),
    'free_bucket_cancelled': ('Свободный ковш отменён', 'Временный приём самосвала отменён.'),
    'free_bucket_loaded': ('Погрузка под свободный ковш', 'Создан рейс от временного экскаватора.'),
    'free_bucket_expired': ('Свободный ковш закрыт', 'Незавершённый временный приём закрыт.'),
    'passive_manual_trip_expired': ('Ручная погрузка закрыта', 'Незавершённая отметка закрыта.'),
}

DOWNTIME_ACTIONS = {
    'downtime_started': ('Начат простой', 'На Пульте появился новый простой.'),
    'downtime_switched': ('Изменён простой', 'Вид текущего простоя изменился.'),
    'downtime_closed': ('Простой завершён', 'Техника вернулась из простоя.'),
    'dispatcher_downtime_closed': ('Простой завершён', 'Диспетчер завершил простой.'),
}

SHIFT_EVENTS = {
    'driver_shift_opened': ('Смена водителя открыта', 'Пульт обновлён.'),
    'driver_shift_closed': ('Смена водителя закрыта', 'Пульт обновлён.'),
    'excavator_shift_opened': ('Смена экскаватора открыта', 'Пульт обновлён.'),
    'excavator_shift_closed': ('Смена эксаватора закрыта', 'Пульт обновлён.'),
    'shift_handover_closed': ('Пересменка завершена', 'Смена передана следующему сотруднику.'),
}


def notification_for_event(event) -> dict | None:
    """Возвращает только операционно важные события без signal-дублей."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    action = str(payload.get('action') or '').strip()
    event_type = str(event.event_type or '').strip()

    if action in IGNORED_ACTIONS and event_type not in SHIFT_EVENTS:
        return None

    category = ''
    message = None
    if event_type in {'trip_changed', 'driver.trip.dump_point_changed', 'excavator.trip.loaded'}:
        category = 'trip'
        message = TRIP_ACTIONS.get(action)
    elif event_type == 'downtime_changed':
        category = 'downtime'
        message = DOWNTIME_ACTIONS.get(action)
    elif event_type in SHIFT_EVENTS:
        category = 'shift'
        message = SHIFT_EVENTS[event_type]
    elif event_type == 'assignment_changed':
        category = 'assignment'
        message = ('Изменено назначение', 'Состав комплекса на Пульте изменился.')
    elif event_type == 'equipment_changed':
        category = 'equipment'
        message = ('Изменено состояние техники', 'Откройте Пульт для актуального состояния.')

    if not message:
        return None
    title, body = message
    return {
        'title': title,
        'body': body,
        'url': DISPATCHER_START_URL,
        'tag': f'dispatcher-{category}',
        'kind': f'dispatcher_{category}',
    }


def send_dispatcher_push_for_event(event_id: int) -> int:
    """После commit создаёт ролевое уведомление; сбой push не ломает доменную операцию."""
    from .models import OperationalStateEvent

    try:
        event = OperationalStateEvent.objects.get(pk=event_id)
        notification = notification_for_event(event)
        if not notification:
            return 0
        return notify_role_web_subscribers(DISPATCHER_ROLE_CODE, **notification)
    except Exception:
        logger.exception('Не удалось отправить системное уведомление Диспетчера для event=%s.', event_id)
        return 0
