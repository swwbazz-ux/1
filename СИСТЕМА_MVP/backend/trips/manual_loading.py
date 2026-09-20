"""Переходное участие водителя. Связь не является состоянием техники."""
import hashlib
import os
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files import locks
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from shifts.models import EmployeeShift
from users.live_monitor import application_presence_by_access_ids
from users.models import EmployeeAccess


MANUAL_DUMP_CARD_VISIBILITY = timedelta(
    seconds=getattr(settings, 'EXCAVATOR_MANUAL_DUMP_CARD_SECONDS', 300)
)


def manual_loading_enabled():
    return getattr(settings, 'EXCAVATOR_MANUAL_LOADING_ENABLED', False)


def passive_manual_trip(trip):
    """Признак фиксируется при отправке и не зависит от последующего входа водителя."""
    return bool(
        trip
        and trip.driver_participation_recorded
        and not trip.driver_control_shift_id
    )


def manual_dump_card_expires_at(trip):
    if not passive_manual_trip(trip) or not trip.created_at:
        return None
    anchor = (
        trip.loaded_at
        if trip.load_time_source == 'excavator_device' and trip.loaded_at
        else trip.created_at
    )
    return anchor + MANUAL_DUMP_CARD_VISIBILITY


def manual_dump_card_is_visible(trip, *, now=None):
    expires_at = manual_dump_card_expires_at(trip)
    if expires_at is None:
        return True
    return expires_at > (now or timezone.now())


def manual_dump_card_visibility_filter(*, now):
    """SQL-проекция очереди: скрываем только истёкшие пассивные ручные рейсы."""
    cutoff = now - MANUAL_DUMP_CARD_VISIBILITY
    active_device_time = Q(
        load_time_source='excavator_device',
        loaded_at__gt=cutoff,
    )
    active_created_time = (
        ~Q(load_time_source='excavator_device') | Q(loaded_at__isnull=True)
    ) & Q(created_at__gt=cutoff)
    return (
        Q(driver_participation_recorded=False)
        | Q(driver_control_shift_id__isnull=False)
        | active_device_time
        | active_created_time
    )


def reconcile_expired_manual_trips(*, now=None):
    """Remove expired passive loads from operational control without inventing unloading."""
    from core.models import bump_operational_state, lock_production_state
    from .models import Trip, TripStatus

    now = now or timezone.now()
    cutoff = now - MANUAL_DUMP_CARD_VISIBILITY
    expired_by_device_time = Q(
        load_time_source='excavator_device',
        loaded_at__isnull=False,
        loaded_at__lte=cutoff,
    )
    expired_by_created_time = (
        ~Q(load_time_source='excavator_device') | Q(loaded_at__isnull=True)
    ) & Q(created_at__lte=cutoff)

    with transaction.atomic():
        lock_production_state()
        trips = list(
            Trip.objects.select_for_update().filter(
                status=TripStatus.LOADED_WAITING_UNLOAD,
                driver_participation_recorded=True,
                driver_control_shift_id__isnull=True,
            ).filter(expired_by_device_time | expired_by_created_time)
        )
        if not trips:
            return []

        for trip in trips:
            trip.status = TripStatus.UNCONTROLLED
            trip.operationally_closed_at = manual_dump_card_expires_at(trip) or now
            trip.closure_recorded_by = None
            trip.save(update_fields=['status', 'operationally_closed_at', 'closure_recorded_by'])

        trip_ids = [trip.pk for trip in trips]
        bump_operational_state(
            'Trip:passive_manual_expired',
            event_type='trip_changed',
            object_type='Trip',
            payload={
                'action': 'passive_manual_trip_expired',
                'trip_ids': trip_ids,
                'truck_ids': sorted({trip.truck_id for trip in trips}),
                'excavator_ids': sorted({trip.excavator_id for trip in trips}),
            },
        )
        return trip_ids


# Пассивный ручной рейс (боевой случай 20.09.2026): экскаваторщик подобрал
# самосвал вручную, пока телефон водителя не был «активно на связи», —
# reconcile_expired_manual_trips() выше гасит такую запись через 5 минут, но
# сама функция вызывалась только при ПОЛНОЙ перезагрузке страницы пульта и
# экскаваторщика (dispatcher_control_view / excavator_work_view). Пока
# сотрудник держит вкладку открытой и живёт на фоновом обновлении раз в
# несколько секунд, запись годами не гаснет — «на разгрузку» висит вечно, а
# перезагружать вручную должен был каждый, кто эту технику видит. Экран
# водителя эту запись не показывает никогда (см. trip_driver_control_filter) —
# несовпадение видно только между пультом/экскаваторщиком и водителем.
#
# Лечится тем же приёмом, что и HaulAssignment (reconcile_due_haul_assignments_throttled
# в assignments/services.py): вызывается из того же опроса, который читают
# все роли каждые несколько секунд (operational_state_version_view), но не
# чаще чем раз в MANUAL_TRIP_RECONCILE_INTERVAL_SECONDS — межпроцессный
# файловый замок делает это безопасным при нескольких воркерах Gunicorn.
MANUAL_TRIP_RECONCILE_INTERVAL_SECONDS = 30


def _manual_trip_reconcile_lock_path():
    database = connection.settings_dict
    identity = '|'.join(
        str(database.get(field) or '')
        for field in ('ENGINE', 'HOST', 'PORT', 'NAME')
    )
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f'accounting-mvp-manual-trip-reconcile-v1-{digest}.lock'


_MANUAL_TRIP_RECONCILE_LOCK_PATH = _manual_trip_reconcile_lock_path()
_MANUAL_TRIP_RECONCILE_PROCESS_ONLY = object()
_manual_trip_reconcile_gate = threading.Lock()
_manual_trip_reconcile_next_check = 0.0
_manual_trip_reconcile_running = False


def _acquire_manual_trip_reconcile_process_gate():
    global _manual_trip_reconcile_next_check, _manual_trip_reconcile_running

    now = time.monotonic()
    with _manual_trip_reconcile_gate:
        if _manual_trip_reconcile_running or now < _manual_trip_reconcile_next_check:
            return False
        _manual_trip_reconcile_running = True
        _manual_trip_reconcile_next_check = now + MANUAL_TRIP_RECONCILE_INTERVAL_SECONDS
    return True


def _release_manual_trip_reconcile_process_gate():
    global _manual_trip_reconcile_running

    with _manual_trip_reconcile_gate:
        _manual_trip_reconcile_running = False


def _acquire_manual_trip_reconcile_server_gate():
    try:
        _MANUAL_TRIP_RECONCILE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_file = _MANUAL_TRIP_RECONCILE_LOCK_PATH.open('a+b')
    except OSError:
        return _MANUAL_TRIP_RECONCILE_PROCESS_ONLY
    locked = False
    try:
        if not locks.lock(lock_file, locks.LOCK_EX | locks.LOCK_NB):
            lock_file.close()
            return None
        locked = True
        lock_file.seek(0)
        try:
            next_check = float(lock_file.read().decode('ascii').strip() or 0)
        except (UnicodeDecodeError, ValueError):
            next_check = 0
        if time.time() < next_check:
            locks.unlock(lock_file)
            locked = False
            lock_file.close()
            return None
        return lock_file
    except OSError:
        if locked:
            try:
                locks.unlock(lock_file)
            except OSError:
                pass
        try:
            lock_file.close()
        except OSError:
            pass
        return _MANUAL_TRIP_RECONCILE_PROCESS_ONLY


def _release_manual_trip_reconcile_server_gate(lock_file):
    try:
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(
                f'{time.time() + MANUAL_TRIP_RECONCILE_INTERVAL_SECONDS:.6f}'.encode('ascii')
            )
            lock_file.flush()
            os.fsync(lock_file.fileno())
        except OSError:
            pass
    finally:
        try:
            locks.unlock(lock_file)
        except OSError:
            pass
        try:
            lock_file.close()
        except OSError:
            pass


def reconcile_expired_manual_trips_throttled():
    if not manual_loading_enabled():
        return []
    if not _acquire_manual_trip_reconcile_process_gate():
        return []
    try:
        lock_file = _acquire_manual_trip_reconcile_server_gate()
        if lock_file is None:
            return []
        try:
            return reconcile_expired_manual_trips()
        finally:
            if lock_file is not _MANUAL_TRIP_RECONCILE_PROCESS_ONLY:
                _release_manual_trip_reconcile_server_gate(lock_file)
    finally:
        _release_manual_trip_reconcile_process_gate()


def truck_driver_participation(truck_ids):
    """Одинаковые пороги с «Смена онлайн», только доступ к приложению Водителя."""
    truck_ids = set(truck_ids)
    shifts = {}
    for shift in EmployeeShift.objects.filter(
        equipment_id__in=truck_ids, closed_at__isnull=True,
    ).filter(
        Q(workplace_code='driver')
        | Q(workplace_code='', equipment__equipment_type__name='Самосвал')
    ).order_by('-opened_at', '-pk'):
        shifts.setdefault(shift.equipment_id, shift)
    accesses = {
        access.employee_id: access
        for access in EmployeeAccess.objects.filter(
            employee_id__in=[shift.employee_id for shift in shifts.values()],
            role__code='driver', is_active=True,
        ).exclude(status=EmployeeAccess.Status.DEACTIVATED)
    }
    presence = application_presence_by_access_ids([access.pk for access in accesses.values()])
    result = {}
    for truck_id in truck_ids:
        shift = shifts.get(truck_id)
        access = accesses.get(shift.employee_id) if shift else None
        state = presence.get(access.pk, {}) if access else {}
        code = state.get('status_code', 'not_registered') if shift else 'no_shift'
        passive = code in {'no_shift', 'not_registered', 'offline'}
        result[truck_id] = {
            'shift': shift,
            'control_shift': shift if not passive else None,
            'passive': passive,
            'code': code,
            'label': state.get('status_label', 'Не подключался') if shift else 'Смена не открыта',
        }
    return result


def trip_driver_control_filter(shift):
    # Старые рейсы сохраняют прежний контракт; новые привязаны к смене при отправке.
    return Q(driver_participation_recorded=False) | Q(driver_control_shift=shift)


def may_replace_open_trip(trip, participation):
    return bool(manual_loading_enabled() and trip and (
        participation['passive']
        or (trip.driver_participation_recorded and not trip.driver_control_shift_id)
    ))
