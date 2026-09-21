import hashlib
import json
import logging
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.db_locks import lock_idempotency_key
from core.models import (
    OfflineFieldEvent,
    OfflineFieldEventConflict,
    OfflineFieldEventStatus,
    bump_operational_state,
    lock_production_state,
)


SYNC_FORMAT_VERSION = 1
MAX_BATCH_SIZE = 100
MAX_DEPENDENCIES = 32
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
EVENT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
DEVICE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$')
logger = logging.getLogger(__name__)

SUPPORTED_EVENT_ROLES = {
    'excavator.free_bucket.accepted': 'excavator_operator',
    'excavator.free_bucket.cancelled': 'excavator_operator',
    'excavator.free_bucket.loaded': 'excavator_operator',
    'excavator.trip.loaded': 'excavator_operator',
    'excavator.trip.loaded.cancelled': 'excavator_operator',
    'excavator.downtime.started': 'excavator_operator',
    'excavator.downtime.ended': 'excavator_operator',
    'excavator.shift.closed': 'excavator_operator',
    'driver.trip.unloaded': 'driver',
    'driver.trip.dump_point_changed': 'driver',
    'driver.trip.loaded': 'driver',
    'driver.trip.loaded.cancelled': 'driver',
    'driver.free_bucket.selected': 'driver',
    'driver.free_bucket.cancelled': 'driver',
    'driver.downtime.started': 'driver',
    'driver.downtime.ended': 'driver',
    'driver.shift.closed': 'driver',
}


def _resolve_free_bucket_acceptance(access, normalized):
    from trips.models import FreeBucketAcceptance

    reference = str(
        normalized['payload'].get('free_bucket_acceptance_id')
        or normalized['payload'].get('free_bucket_acceptance_local_id')
        or ''
    ).strip()
    if not reference:
        _invalid('free_bucket_acceptance_required', 'Не передан временный приём свободного ковша.')
    acceptance_filter = Q(client_acceptance_id=reference)
    if reference.isdigit():
        acceptance_filter |= Q(pk=int(reference))
    # Блокируем только саму строку приёма: связи loading_shift, primary_assignment,
    # requested_by, requesting_shift необязательны, и PostgreSQL отвергает
    # FOR UPDATE поверх их внешнего соединения («FOR UPDATE cannot be applied to
    # the nullable side of an outer join»). На SQLite select_for_update — пустышка,
    # поэтому локально ошибка не воспроизводилась; на бою она 5 часов держала
    # отмену свободного ковша в очереди телефона (20.09.2026).
    acceptance = (
        FreeBucketAcceptance.objects.select_for_update(of=('self',))
        .select_related(
            'truck', 'excavator', 'loading_shift', 'primary_assignment',
            'requested_by', 'requesting_shift',
        )
        .filter(acceptance_filter)
        .first()
    )
    if not acceptance:
        source = (
            OfflineFieldEvent.objects.select_for_update(of=('self',))
            .filter(
                actor=access.employee,
                access=access,
                device_id=normalized['device_id'],
                event_id=reference,
                event_type__in=[
                    'excavator.free_bucket.accepted',
                    'driver.free_bucket.selected',
                ],
                status=OfflineFieldEventStatus.ACCEPTED,
            )
            .first()
        )
        acceptance_id = (source.result_payload or {}).get('server_ids', {}).get('free_bucket_acceptance_id') if source else None
        acceptance = (
            FreeBucketAcceptance.objects.select_for_update(of=('self',))
            .select_related(
                'truck', 'excavator', 'loading_shift', 'primary_assignment',
                'requested_by', 'requesting_shift',
            )
            .filter(pk=acceptance_id)
            .first()
        ) if acceptance_id else None
    if not acceptance:
        _retry('free_bucket_acceptance_pending', 'Временный приём ещё не подтверждён сервером.')
    return acceptance


def _validate_free_bucket_participants(excavator, truck):
    if not truck.is_active or truck.equipment_type.name != 'Самосвал':
        _conflict('free_bucket_truck_unavailable', 'Самосвал больше недоступен для свободного ковша.')
    if not excavator.is_active or excavator.equipment_type.name != 'Экскаватор':
        _conflict('free_bucket_excavator_unavailable', 'Экскаватор больше недоступен для свободного ковша.')


def _free_bucket_work_context_snapshot(excavator):
    from trips.free_bucket import canonical_free_bucket_work_context_snapshot

    try:
        return canonical_free_bucket_work_context_snapshot(excavator)
    except ValidationError as error:
        _conflict('free_bucket_work_context_unavailable', '; '.join(error.messages))


def _free_bucket_primary_assignment(truck):
    from assignments.models import AssignmentStatus, HaulAssignment

    return (
        HaulAssignment.objects.select_for_update()
        .filter(truck=truck, ended_at__isnull=True, status=AssignmentStatus.ACCEPTED)
        .order_by('-assigned_at', '-id')
        .first()
    )


def _free_bucket_trip_changed_between(truck, *, occurred_at, received_at):
    from trips.models import Trip

    return Trip.objects.select_for_update().filter(truck=truck).filter(
        Q(created_at__gt=occurred_at, created_at__lte=received_at)
        | Q(loaded_at__gt=occurred_at, loaded_at__lte=received_at)
        | Q(completed_at__gt=occurred_at, completed_at__lte=received_at)
        | Q(cancelled_at__gt=occurred_at, cancelled_at__lte=received_at)
        | Q(operationally_closed_at__gt=occurred_at, operationally_closed_at__lte=received_at)
    ).exists()


def _process_driver_free_bucket_selected(access, normalized):
    from trips.models import FreeBucketAcceptance, FreeBucketAcceptanceStatus, OPEN_TRIP_STATUSES, Trip
    from trips.trip_creation import lock_trip_participant_equipment

    shift = _locked_shift(access, normalized, role_code='driver')
    if shift.closed_at:
        _conflict('driver_shift_closed', 'Смена водителя уже закрыта.')
    excavator_id = _positive_int(normalized['payload'].get('excavator_id'), field='excavator_id')
    production_state = lock_production_state()
    try:
        excavator, truck = lock_trip_participant_equipment(
            excavator_id=excavator_id,
            truck_id=shift.equipment_id,
        )
    except ValidationError as error:
        _conflict('free_bucket_equipment_changed', '; '.join(error.messages))
    _validate_free_bucket_participants(excavator, truck)
    if Trip.objects.select_for_update().filter(truck=truck, status__in=OPEN_TRIP_STATUSES).exists():
        _conflict('open_trip_exists', 'Самосвал уже находится в незавершённом рейсе.')
    if _free_bucket_trip_changed_between(
        truck,
        occurred_at=normalized['occurred_at'],
        received_at=normalized['received_at'],
    ):
        _conflict('free_bucket_request_stale', 'После выбора состояние рейса уже изменилось.')
    primary_assignment = _free_bucket_primary_assignment(truck)
    if primary_assignment and primary_assignment.excavator_id == excavator.id:
        _conflict(
            'free_bucket_primary_target',
            'Основной экскаватор не требует отдельного запроса свободного ковша.',
        )
    existing = (
        FreeBucketAcceptance.objects.select_for_update()
        .filter(
            truck=truck,
            status__in=(
                FreeBucketAcceptanceStatus.REQUESTED,
                FreeBucketAcceptanceStatus.ACCEPTED,
                FreeBucketAcceptanceStatus.USED,
            ),
        )
        .first()
    )
    if existing:
        if existing.excavator_id != excavator.id:
            _conflict('free_bucket_target_changed', 'Для самосвала уже выбран другой экскаватор.')
        if existing.status == FreeBucketAcceptanceStatus.USED:
            _conflict('free_bucket_already_loaded', 'Погрузка по свободному ковшу уже выполнена.')
        if existing.requested_by_id and (
            existing.requested_by_id != access.employee_id
            or existing.requesting_shift_id != shift.id
        ):
            _conflict('free_bucket_request_owner_changed', 'Запрос принадлежит другой смене водителя.')
        if existing.requested_by_id and normalized['occurred_at'] < existing.occurred_at:
            _conflict('free_bucket_request_stale', 'Запоздалый выбор не может изменить текущий запрос.')
        changed_fields = []
        if not existing.requested_by_id:
            existing.requested_by = access.employee
            existing.requesting_shift = shift
            changed_fields.extend(['requested_by', 'requesting_shift'])
        if changed_fields:
            existing.save(update_fields=changed_fields)
            state = bump_operational_state(
                'OfflineFieldEvent:driver_free_bucket_selected', event_type='trip_changed',
                object_type='FreeBucketAcceptance', object_id=existing.id,
                payload={'action': 'driver_free_bucket_selected', 'truck_id': truck.id,
                         'excavator_id': excavator.id, 'status': existing.status},
            )
            version = state.version
        else:
            version = production_state.version
        return {
            'server_ids': {'free_bucket_acceptance_id': existing.id, 'shift_id': shift.id},
            'free_bucket_status': existing.status,
            'mapped_existing': True,
            'version': version,
        }, {'free_bucket_acceptance': existing, 'shift': shift, 'equipment': truck}

    latest = (
        FreeBucketAcceptance.objects.select_for_update()
        .filter(truck=truck)
        .order_by('-received_at', '-id')
        .first()
    )
    if latest:
        terminal_at = latest.closed_at or latest.cancelled_at or latest.used_at or latest.accepted_at or latest.occurred_at
        if normalized['occurred_at'] <= terminal_at:
            _conflict('free_bucket_request_stale', 'Запоздалый выбор относится к уже завершённому состоянию.')

    snapshot = _free_bucket_work_context_snapshot(excavator)
    acceptance = FreeBucketAcceptance.objects.create(
        client_acceptance_id=normalized['event_id'],
        truck=truck,
        excavator=excavator,
        requested_by=access.employee,
        requesting_shift=shift,
        primary_assignment=primary_assignment,
        status=FreeBucketAcceptanceStatus.REQUESTED,
        occurred_at=normalized['occurred_at'],
        received_at=normalized['received_at'],
        work_context_snapshot=snapshot,
    )
    state = bump_operational_state(
        'OfflineFieldEvent:driver_free_bucket_selected', event_type='trip_changed',
        object_type='FreeBucketAcceptance', object_id=acceptance.id,
        payload={'action': 'driver_free_bucket_selected', 'truck_id': truck.id,
                 'excavator_id': excavator.id, 'status': acceptance.status},
    )
    return {
        'server_ids': {'free_bucket_acceptance_id': acceptance.id, 'shift_id': shift.id},
        'free_bucket_status': acceptance.status,
        'version': state.version,
    }, {'free_bucket_acceptance': acceptance, 'shift': shift, 'equipment': truck}


def _process_free_bucket_accepted(access, normalized):
    from trips.models import FreeBucketAcceptance, FreeBucketAcceptanceStatus, OPEN_TRIP_STATUSES, Trip
    from trips.trip_creation import lock_trip_participant_equipment

    shift = _locked_shift(access, normalized, role_code='excavator_operator')
    truck_id = _positive_int(normalized['payload'].get('truck_id'), field='truck_id')
    lock_production_state()
    try:
        excavator, truck = lock_trip_participant_equipment(
            excavator_id=shift.equipment_id,
            truck_id=truck_id,
        )
    except ValidationError as error:
        _conflict('free_bucket_equipment_changed', '; '.join(error.messages))
    _validate_free_bucket_participants(excavator, truck)
    if Trip.objects.select_for_update().filter(truck=truck, status__in=OPEN_TRIP_STATUSES).exists():
        _conflict('open_trip_exists', 'Самосвал уже находится в незавершённом рейсе.')
    existing = (
        FreeBucketAcceptance.objects.select_for_update()
        .filter(
            truck=truck,
            status__in=(
                FreeBucketAcceptanceStatus.REQUESTED,
                FreeBucketAcceptanceStatus.ACCEPTED,
                FreeBucketAcceptanceStatus.USED,
            ),
        )
        .first()
    )
    if existing:
        if _free_bucket_trip_changed_between(
            truck,
            occurred_at=(
                existing.occurred_at
                if existing.status == FreeBucketAcceptanceStatus.REQUESTED
                else normalized['occurred_at']
            ),
            received_at=normalized['received_at'],
        ):
            _conflict('free_bucket_request_stale', 'После выбора состояние рейса уже изменилось.')
        if existing.excavator_id != excavator.id:
            if existing.status == FreeBucketAcceptanceStatus.REQUESTED:
                _conflict('free_bucket_target_changed', 'Водитель уже выбрал другой экскаватор.')
            _conflict(
                'free_bucket_already_accepted',
                'Самосвал уже принят другим экскаватором или ожидает разгрузки.',
            )
        if existing.status != FreeBucketAcceptanceStatus.REQUESTED:
            _conflict(
                'free_bucket_already_accepted',
                'Самосвал уже принят под свободный ковш или ожидает разгрузки после такой погрузки.',
            )
        existing.operator = access.employee
        existing.loading_shift = shift
        existing.status = FreeBucketAcceptanceStatus.ACCEPTED
        existing.accepted_at = max(existing.occurred_at, normalized['occurred_at'])
        existing.save(update_fields=['operator', 'loading_shift', 'status', 'accepted_at'])
        acceptance = existing
    else:
        if _free_bucket_trip_changed_between(
            truck,
            occurred_at=normalized['occurred_at'],
            received_at=normalized['received_at'],
        ):
            _conflict('free_bucket_request_stale', 'После выбора состояние рейса уже изменилось.')
        acceptance = FreeBucketAcceptance.objects.create(
            client_acceptance_id=normalized['event_id'],
            truck=truck,
            excavator=excavator,
            operator=access.employee,
            loading_shift=shift,
            primary_assignment=_free_bucket_primary_assignment(truck),
            status=FreeBucketAcceptanceStatus.ACCEPTED,
            occurred_at=normalized['occurred_at'],
            received_at=normalized['received_at'],
            accepted_at=normalized['occurred_at'],
            work_context_snapshot=_free_bucket_work_context_snapshot(excavator),
        )
    state = bump_operational_state(
        'OfflineFieldEvent:free_bucket_accepted', event_type='trip_changed',
        object_type='FreeBucketAcceptance', object_id=acceptance.id,
        payload={'action': 'free_bucket_accepted', 'truck_id': truck.id,
                 'excavator_id': excavator.id, 'status': acceptance.status},
    )
    return {
        'server_ids': {'free_bucket_acceptance_id': acceptance.id, 'shift_id': shift.id},
        'free_bucket_status': acceptance.status,
        'version': state.version,
    }, {'free_bucket_acceptance': acceptance, 'shift': shift, 'equipment': truck}


def _process_free_bucket_cancelled(access, normalized):
    from trips.models import FreeBucketAcceptanceStatus

    shift = _locked_shift(access, normalized, role_code='excavator_operator')
    lock_production_state()
    acceptance = _resolve_free_bucket_acceptance(access, normalized)
    if (
        acceptance.excavator_id != shift.equipment_id
        or acceptance.operator_id != access.employee_id
        or acceptance.loading_shift_id != shift.id
    ):
        _conflict('free_bucket_owner_changed', 'Временный приём принадлежит другой смене машиниста.')
    if acceptance.status != FreeBucketAcceptanceStatus.ACCEPTED:
        _conflict('free_bucket_not_cancellable', 'Временный приём уже использован или отменён.')
    if normalized['occurred_at'] < (acceptance.accepted_at or acceptance.occurred_at):
        _conflict(
            'free_bucket_cancel_stale',
            'Запоздалая отмена не может предшествовать приёму под свободный ковш.',
        )
    acceptance.status = FreeBucketAcceptanceStatus.CANCELLED
    acceptance.cancelled_at = normalized['occurred_at']
    acceptance.save(update_fields=['status', 'cancelled_at'])
    state = bump_operational_state(
        'OfflineFieldEvent:free_bucket_cancelled', event_type='trip_changed',
        object_type='FreeBucketAcceptance', object_id=acceptance.id,
        payload={'action': 'free_bucket_cancelled', 'truck_id': acceptance.truck_id, 'excavator_id': acceptance.excavator_id},
    )
    return {
        'server_ids': {'free_bucket_acceptance_id': acceptance.id, 'shift_id': shift.id},
        'version': state.version,
    }, {'free_bucket_acceptance': acceptance, 'shift': shift, 'equipment': acceptance.truck}


def _process_driver_free_bucket_cancelled(access, normalized):
    from trips.models import FreeBucketAcceptanceStatus

    shift = _locked_shift(access, normalized, role_code='driver')
    lock_production_state()
    acceptance = _resolve_free_bucket_acceptance(access, normalized)
    if (
        acceptance.truck_id != shift.equipment_id
        or acceptance.requested_by_id != access.employee_id
        or acceptance.requesting_shift_id != shift.id
    ):
        _conflict('free_bucket_request_owner_changed', 'Запрос принадлежит другой смене водителя.')
    if acceptance.status not in (
        FreeBucketAcceptanceStatus.REQUESTED,
        FreeBucketAcceptanceStatus.ACCEPTED,
    ):
        _conflict('free_bucket_not_cancellable', 'Запрос уже использован или отменён.')
    current_at = acceptance.accepted_at or acceptance.occurred_at
    if normalized['occurred_at'] < current_at:
        _conflict('free_bucket_cancel_stale', 'Запоздалая отмена не может изменить более новое состояние.')
    acceptance.status = FreeBucketAcceptanceStatus.CANCELLED
    acceptance.cancelled_at = normalized['occurred_at']
    acceptance.save(update_fields=['status', 'cancelled_at'])
    state = bump_operational_state(
        'OfflineFieldEvent:driver_free_bucket_cancelled', event_type='trip_changed',
        object_type='FreeBucketAcceptance', object_id=acceptance.id,
        payload={'action': 'driver_free_bucket_cancelled', 'truck_id': acceptance.truck_id,
                 'excavator_id': acceptance.excavator_id},
    )
    return {
        'server_ids': {'free_bucket_acceptance_id': acceptance.id, 'shift_id': shift.id},
        'free_bucket_status': acceptance.status,
        'version': state.version,
    }, {'free_bucket_acceptance': acceptance, 'shift': shift, 'equipment': acceptance.truck}


def _process_free_bucket_loaded(access, normalized):
    """Consume exactly one confirmed free-bucket acceptance into an ordinary Trip."""
    from downtimes.driver_workflow import close_truck_waiting_loading_downtimes
    from downtimes.models import DowntimeEvent
    from shifts.models import EmployeeShift
    from trips.free_bucket import resolve_free_bucket_load_context
    from trips.models import FreeBucketAcceptanceStatus, OPEN_TRIP_STATUSES, Trip, TripClientAction
    from trips.trip_creation import create_loaded_waiting_unload_trip, lock_trip_participant_equipment
    from trips.views import notify_driver_truck_loaded, reconcile_excavator_waiting_for_trucks

    payload = normalized['payload']
    truck_id = _positive_int(payload.get('truck_id'), field='truck_id')
    excavator_id = _positive_int(normalized.get('equipment_id'), field='equipment_id')
    lock_idempotency_key('trip_load_pair', f'{excavator_id}:{truck_id}')
    shift = _locked_shift(access, normalized, role_code='excavator_operator')
    lock_production_state()
    excavator, truck = lock_trip_participant_equipment(excavator_id=shift.equipment_id, truck_id=truck_id)
    acceptance = _resolve_free_bucket_acceptance(access, normalized)
    if (
        acceptance.truck_id != truck.id
        or acceptance.excavator_id != excavator.id
        or acceptance.operator_id != access.employee_id
        or acceptance.loading_shift_id != shift.id
    ):
        _conflict('free_bucket_context_changed', 'Временный приём не соответствует этой погрузке.')
    merge_driver_trip = False
    if acceptance.status == FreeBucketAcceptanceStatus.USED and acceptance.used_trip_id:
        used_trip = Trip.objects.select_for_update().filter(
            pk=acceptance.used_trip_id,
            status__in=OPEN_TRIP_STATUSES,
        ).first()
        merge_driver_trip = bool(
            used_trip
            and TripClientAction.objects.select_for_update(of=('self',)).filter(
                trip=used_trip,
                action_type='driver_manual_loaded',
            ).exists()
            and not TripClientAction.objects.select_for_update(of=('self',)).filter(
                trip=used_trip,
                action_type='free_bucket_loaded',
            ).exists()
            and _manual_load_matches_trip(
                used_trip,
                {**payload, 'excavator_id': excavator.id},
                acceptance=acceptance,
            )
        )
        if not merge_driver_trip:
            _conflict('free_bucket_already_loaded', 'Погрузка по свободному ковшу уже выполнена другим событием.')
    if acceptance.status != FreeBucketAcceptanceStatus.ACCEPTED and not merge_driver_trip:
        _conflict('free_bucket_not_available', 'Временный приём уже использован или отменён.')
    if not acceptance.accepted_at:
        _conflict('free_bucket_not_accepted', 'Временный запрос ещё не согласован машинистом.')
    if normalized['occurred_at'] < acceptance.accepted_at:
        _conflict('free_bucket_load_before_accept', 'Время погрузки раньше времени приёма под свободный ковш.')
    if not merge_driver_trip and Trip.objects.select_for_update().filter(
        truck=truck,
        status__in=OPEN_TRIP_STATUSES,
    ).exists():
        _conflict('open_trip_exists', 'Самосвал уже находится в незавершённом рейсе.')
    truck_downtime = (
        DowntimeEvent.objects.select_for_update(of=('self',)).select_related('reason')
        .filter(equipment=truck, ended_at__isnull=True).order_by('-started_at', '-id').first()
    )
    excavator_downtimes = list(
        DowntimeEvent.objects.select_for_update(of=('self',)).select_related('reason')
        .filter(equipment=excavator, ended_at__isnull=True).order_by('id')
    )
    if (
        (truck_downtime and truck_downtime.started_at <= normalized['occurred_at'])
        or any(
            item.reason.is_critical and item.started_at <= normalized['occurred_at']
            for item in excavator_downtimes
        )
    ):
        _conflict('equipment_downtime_active', 'Погрузка невозможна: на технике открыт блокирующий простой.')
    try:
        load_context = resolve_free_bucket_load_context(acceptance, payload)
    except ValidationError as error:
        _conflict('free_bucket_work_context_changed', '; '.join(error.messages))
    dump_point = load_context['dump_point']
    rock_type = load_context['rock_type']
    driver_shift = (
        EmployeeShift.objects.select_for_update(of=('self',))
        .filter(equipment_id=truck.id, opened_at__lte=normalized['occurred_at'])
        .filter(Q(closed_at__isnull=True) | Q(closed_at__gte=normalized['occurred_at']))
        .filter(Q(workplace_code='driver') | Q(workplace_code='', equipment__equipment_type__name='Самосвал'))
        .order_by('-opened_at', '-id').first()
    )
    manual_control = payload.get('manual_control') is True or not bool(driver_shift)
    participation = {
        'shift': driver_shift,
        'control_shift': None if manual_control else driver_shift,
        'passive': manual_control,
        'code': 'free_bucket_manual' if manual_control else 'free_bucket_driver_shift',
        'label': '',
    }
    try:
        if merge_driver_trip:
            trip = used_trip
            trip.excavator_operator = access.employee
            trip.loading_shift = shift
            trip.loaded_at = normalized['occurred_at']
            trip.load_received_at = timezone.now()
            trip.load_time_source = 'excavator_device'
            trip.save(update_fields=[
                'excavator_operator', 'loading_shift', 'loaded_at',
                'load_received_at', 'load_time_source',
            ])
        else:
            trip = create_loaded_waiting_unload_trip(
                assignment=None, truck=truck, excavator=excavator, free_bucket_acceptance=acceptance,
                excavator_operator=access.employee, loading_shift=shift, rock_type=rock_type, dump_point=dump_point,
                planned_volume_m3=payload.get('planned_volume_m3') or None,
                loading_horizon=load_context['loading_horizon'],
                loading_block=load_context['loading_block'],
                transport_distance_km=load_context['transport_distance_km'],
                downtime_text=payload.get('downtime_text'),
                note=str(payload.get('note') or 'Свободный ковш')[:1000], participation=participation,
                occurred_at=normalized['occurred_at'], resolve_assignment_transition=False,
            )
    except ValidationError as error:
        _conflict('trip_validation_failed', '; '.join(error.messages))
    if not merge_driver_trip:
        acceptance.status = FreeBucketAcceptanceStatus.USED
        acceptance.used_at = normalized['occurred_at']
        acceptance.used_trip = trip
        acceptance.save(update_fields=['status', 'used_at', 'used_trip'])
    TripClientAction.objects.create(action_type='free_bucket_loaded', client_action_id=normalized['event_id'], trip=trip, actor=access.employee)
    close_truck_waiting_loading_downtimes(truck, ended_at=normalized['occurred_at'])
    reconcile_excavator_waiting_for_trucks(excavator, access.employee, start_when_empty=True)
    state = bump_operational_state(
        'OfflineFieldEvent:free_bucket_loaded', event_type='trip_changed', object_type='Trip', object_id=trip.id,
        payload={'action': 'free_bucket_loaded', 'trip_id': trip.id, 'truck_id': trip.truck_id,
                 'excavator_id': trip.excavator_id, 'free_bucket_acceptance_id': acceptance.id},
    )
    transaction.on_commit(lambda: notify_driver_truck_loaded(trip))
    return {
        'server_ids': {'trip_id': trip.id, 'free_bucket_acceptance_id': acceptance.id, 'shift_id': shift.id},
        'version': state.version,
    }, {'trip': trip, 'shift': shift, 'equipment': truck}


class OfflineEventProblem(Exception):
    def __init__(self, status, code, message, *, retryable=False, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def _invalid(code, message):
    raise OfflineEventProblem(OfflineFieldEventStatus.INVALID, code, message)


def _conflict(code, message, *, details=None):
    raise OfflineEventProblem(
        OfflineFieldEventStatus.CONFLICT,
        code,
        message,
        details=details,
    )


def _retry(code, message):
    raise OfflineEventProblem(OfflineFieldEventStatus.RETRY, code, message, retryable=True)


def _clean_identifier(value, *, field, pattern=EVENT_ID_RE):
    value = str(value or '').strip()
    if not pattern.fullmatch(value):
        _invalid(f'invalid_{field}', f'Некорректное поле {field}.')
    return value


def _positive_int(value, *, field, allow_zero=False, required=True):
    if value in (None, ''):
        if required:
            _invalid(f'{field}_required', f'Не передано поле {field}.')
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        _invalid(f'invalid_{field}', f'Некорректное поле {field}.')
    if value < (0 if allow_zero else 1):
        _invalid(f'invalid_{field}', f'Некорректное поле {field}.')
    return value


def _optional_decimal(value, *, field):
    if value in (None, ''):
        return None
    try:
        parsed = Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, TypeError, ValueError):
        _invalid(f'invalid_{field}', f'Некорректное поле {field}.')
    if not parsed.is_finite():
        _invalid(f'invalid_{field}', f'Некорректное поле {field}.')
    return parsed


def normalize_offline_event(raw_event, *, role_code, device_id, received_at=None):
    if not isinstance(raw_event, dict):
        _invalid('invalid_event', 'Событие должно быть JSON-объектом.')
    received_at = received_at or timezone.now()
    event_id = _clean_identifier(raw_event.get('event_id'), field='event_id')
    event_type = str(raw_event.get('event_type') or '').strip()
    if event_type not in SUPPORTED_EVENT_ROLES:
        _invalid('unsupported_event_type', 'Это действие не поддерживает offline-синхронизацию.')
    if SUPPORTED_EVENT_ROLES[event_type] != role_code:
        _invalid('event_role_mismatch', 'Тип события не соответствует роли.')
    try:
        format_version = int(raw_event.get('format_version', 0))
    except (TypeError, ValueError):
        format_version = 0
    if format_version != SYNC_FORMAT_VERSION:
        _invalid('unsupported_format_version', 'Версия offline-события не поддерживается.')
    sequence = _positive_int(raw_event.get('sequence'), field='sequence', allow_zero=True)
    depends_on = raw_event.get('depends_on') or []
    if not isinstance(depends_on, list) or len(depends_on) > MAX_DEPENDENCIES:
        _invalid('invalid_dependencies', 'Некорректный список зависимостей.')
    depends_on = [_clean_identifier(item, field='dependency') for item in depends_on]
    if event_id in depends_on or len(depends_on) != len(set(depends_on)):
        _invalid('invalid_dependencies', 'Событие не может зависеть от себя или повторять зависимость.')
    raw_occurred_at = str(raw_event.get('occurred_at') or '').strip()
    occurred_at = parse_datetime(raw_occurred_at) if raw_occurred_at else None
    if occurred_at is None or timezone.is_naive(occurred_at):
        _invalid('invalid_occurred_at', 'Время события должно содержать часовой пояс.')
    payload = raw_event.get('payload') or {}
    context_snapshot = raw_event.get('context_snapshot') or raw_event.get('context') or {}
    if not isinstance(payload, dict) or not isinstance(context_snapshot, dict):
        _invalid('invalid_event_payload', 'Параметры и контекст должны быть JSON-объектами.')
    local_downtime_id = str(
        raw_event.get('local_downtime_id') or payload.get('local_downtime_id') or ''
    ).strip()[:128]
    # Older field shells did not include a separate local downtime identifier.
    # The immutable event id is a safe, deterministic fallback and lets a later
    # offline "ended" event reference the accepted start after an app update.
    if not local_downtime_id and event_type.endswith('.downtime.started'):
        local_downtime_id = event_id
    # v234 excavator shells briefly emitted the active downtime reference only
    # inside payload.active_downtime_id. Keep those already-durable events
    # replayable after an application update without weakening normal identity
    # checks: the same legacy wire form is normalized deterministically.
    if not local_downtime_id and event_type.endswith('.downtime.ended'):
        legacy_downtime_ref = str(payload.get('active_downtime_id') or '').strip()
        if legacy_downtime_ref:
            if legacy_downtime_ref.isdecimal():
                payload = dict(payload)
                payload['downtime_id'] = int(legacy_downtime_ref)
            else:
                local_downtime_id = legacy_downtime_ref[:128]
    normalized = {
        'event_id': event_id,
        'event_type': event_type,
        'format_version': format_version,
        'role_code': role_code,
        'device_id': device_id,
        'claimed_actor_id': raw_event.get('actor_id'),
        'claimed_access_id': raw_event.get('access_id'),
        'claimed_role_code': raw_event.get('role_code'),
        'claimed_device_id': raw_event.get('device_id'),
        'sequence': sequence,
        'depends_on': depends_on,
        'occurred_at': occurred_at,
        'shift_id': raw_event.get('shift_id') or context_snapshot.get('shift_id'),
        'equipment_id': raw_event.get('equipment_id') or context_snapshot.get('equipment_id'),
        'trip_id': raw_event.get('trip_id') or payload.get('trip_id'),
        'local_trip_id': str(raw_event.get('local_trip_id') or payload.get('local_trip_id') or '').strip()[:128],
        'local_downtime_id': local_downtime_id,
        'payload': payload,
        'context_snapshot': context_snapshot,
        'received_at': received_at,
    }
    canonical = {
        **normalized,
        'occurred_at': occurred_at.isoformat(),
        'received_at': None,
    }
    normalized['fingerprint'] = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')
    ).hexdigest()
    return normalized


def _result(event_id, status, *, retryable=False, code='', message='', payload=None):
    payload = dict(payload or {})
    return {
        'event_id': event_id,
        'status': status,
        'retryable': bool(retryable),
        'code': code,
        'message': message,
        'server_ids': payload.pop('server_ids', {}),
        'server_received_at': payload.pop('server_received_at', timezone.now().isoformat()),
        'version': payload.pop('version', None),
        **payload,
    }


def _stored_result(event, *, deduplicated=False):
    status = 'deduplicated' if deduplicated and event.status == OfflineFieldEventStatus.ACCEPTED else event.status
    payload = dict(event.result_payload or {})
    payload.setdefault('server_received_at', event.received_at.isoformat())
    return _result(
        event.event_id, status, retryable=event.retryable,
        code=event.error_code, message=event.error_message, payload=payload,
    )


def _record_conflict_attempt(*, existing, access, normalized, code):
    OfflineFieldEventConflict.objects.create(
        existing_event=existing,
        attempted_event_id=normalized['event_id'],
        actor=access.employee,
        access=access,
        role_code=normalized['role_code'],
        device_id=normalized['device_id'],
        fingerprint=normalized['fingerprint'],
        code=code,
        submitted_event={
            key: (value.isoformat() if hasattr(value, 'isoformat') else value)
            for key, value in normalized.items()
            if key not in {'received_at', 'fingerprint'}
        },
    )


def _record_invalid_attempt(*, access, role_code, device_id, raw_event, code):
    submitted = raw_event if isinstance(raw_event, dict) else {'raw_event': raw_event}
    serialized = json.dumps(submitted, ensure_ascii=False, sort_keys=True, default=str)
    OfflineFieldEventConflict.objects.create(
        existing_event=None,
        attempted_event_id=str(submitted.get('event_id') or '')[:128],
        actor=access.employee,
        access=access,
        role_code=role_code,
        device_id=device_id,
        fingerprint=hashlib.sha256(serialized.encode('utf-8')).hexdigest(),
        code=code,
        submitted_event=submitted,
    )


def _validate_claimed_context(access, normalized):
    actor_claims = (
        normalized.get('claimed_actor_id'),
        normalized['context_snapshot'].get('actor_id'),
    )
    access_claims = (
        normalized.get('claimed_access_id'),
        normalized['context_snapshot'].get('access_id'),
    )
    role_claims = (
        normalized.get('claimed_role_code'),
        normalized['context_snapshot'].get('role_code'),
    )
    device_claim = normalized.get('claimed_device_id')
    if any(item not in (None, '', access.employee_id, str(access.employee_id)) for item in actor_claims):
        _conflict('actor_context_changed', 'Событие сохранено для другого сотрудника.')
    if any(item not in (None, '', access.id, str(access.id)) for item in access_claims):
        _conflict('access_context_changed', 'Доступ события не совпадает с текущим.')
    if any(item not in (None, '', access.role.code) for item in role_claims):
        _conflict('role_context_changed', 'Роль события не совпадает с текущей.')
    if device_claim not in (None, '', normalized['device_id']):
        _conflict('device_context_changed', 'Устройство события не совпадает с пакетом.')


def _locked_shift(access, normalized, *, role_code):
    from shifts.models import EmployeeShift

    shift_id = _positive_int(normalized['shift_id'], field='shift_id')
    shift = (
        EmployeeShift.objects.select_for_update(of=('self',))
        .select_related('equipment', 'equipment__equipment_type')
        .filter(pk=shift_id, employee=access.employee)
        .first()
    )
    if not shift:
        _conflict('shift_context_changed', 'Смена из события не найдена или не принадлежит сотруднику.')
    allowed_workplaces = {
        'driver': {'driver', ''},
        'excavator_operator': {'excavator_operator', ''},
    }
    expected_equipment_type = 'Самосвал' if role_code == 'driver' else 'Экскаватор'
    if shift.workplace_code not in allowed_workplaces[role_code] or (
        shift.workplace_code == '' and shift.equipment.equipment_type.name != expected_equipment_type
    ):
        _conflict('shift_role_mismatch', 'Смена не соответствует роли события.')
    if not shift.equipment_id:
        _conflict('shift_equipment_missing', 'В смене не зафиксирована техника.')
    equipment_id = _positive_int(normalized['equipment_id'], field='equipment_id')
    if equipment_id != shift.equipment_id:
        _conflict('equipment_context_changed', 'Техника в событии не совпадает со сменой.')
    occurred_at = normalized['occurred_at']
    if occurred_at < shift.opened_at:
        _conflict('event_before_shift', 'Время события раньше начала смены.')
    if shift.closed_at and occurred_at > shift.closed_at:
        _conflict('event_after_shift', 'Время события позже закрытия смены.')
    return shift


def _resolve_trip_reference(access, normalized):
    from trips.models import Trip

    trip_id = normalized['trip_id']
    local_trip_id = normalized['local_trip_id']
    if trip_id:
        trip = Trip.objects.select_for_update().filter(pk=_positive_int(trip_id, field='trip_id')).first()
    elif local_trip_id:
        source = (
            OfflineFieldEvent.objects.select_for_update(of=('self',))
            .filter(
                actor=access.employee,
                device_id=normalized['device_id'],
                local_trip_id=local_trip_id,
                event_type__in=[
                    'excavator.trip.loaded',
                    'excavator.free_bucket.loaded',
                    'driver.trip.loaded',
                ],
                status=OfflineFieldEventStatus.ACCEPTED,
                trip__isnull=False,
            )
            .exclude(event_id=normalized['event_id'])
            .order_by('sequence', 'id')
            .first()
        )
        trip = Trip.objects.select_for_update().filter(pk=source.trip_id).first() if source else None
    else:
        _invalid('trip_reference_required', 'Не передан рейс или его локальный ID.')
    if not trip:
        _retry('trip_reference_pending', 'Связанный рейс ещё не подтверждён сервером.')
    return trip


def _historical_excavator_assignment(*, shift, truck_id, requested_assignment_id, occurred_at):
    from assignments.models import HaulAssignment, HaulAssignmentAction

    assignment = (
        HaulAssignment.objects.select_for_update(of=('self',))
        .select_related('truck', 'excavator')
        .filter(pk=requested_assignment_id, truck_id=truck_id, excavator_id=shift.equipment_id)
        .first()
    )
    if not assignment or assignment.action != HaulAssignmentAction.ASSIGN:
        _conflict('assignment_context_changed', 'Назначение из события не найдено.')
    if occurred_at < assignment.assigned_at or (assignment.ended_at and occurred_at > assignment.ended_at):
        _conflict('assignment_time_mismatch', 'В указанное время это назначение не действовало.')
    return assignment


def _manual_load_matches_trip(trip, payload, *, acceptance=None):
    """Return whether two role events describe the same physical load."""
    requested_dump_id = payload.get('dump_point_id')
    requested_rock_id = payload.get('rock_type_id') or payload.get('rock_type')
    try:
        requested_dump_id = int(requested_dump_id)
        requested_rock_id = int(requested_rock_id)
    except (TypeError, ValueError):
        return False
    if (
        trip.excavator_id != int(payload.get('excavator_id') or trip.excavator_id)
        or trip.truck_id != int(payload.get('truck_id') or trip.truck_id)
        or trip.dump_point_id != requested_dump_id
        or trip.rock_type_id != requested_rock_id
        or str(trip.loading_horizon or '') != str(payload.get('loading_horizon') or '')[:64]
        or str(trip.loading_block or '') != str(payload.get('loading_block') or '')[:64]
    ):
        return False
    try:
        trip_acceptance_id = trip.free_bucket_acceptance.id
    except (AttributeError, ObjectDoesNotExist):
        trip_acceptance_id = None
    if acceptance is not None:
        return trip_acceptance_id == acceptance.id
    return trip_acceptance_id is None


def _trip_terminal_changed_between(truck_id, *, after, through):
    """Return whether another trip cycle ended inside the supplied interval."""
    from trips.models import Trip

    if not after or not through or through <= after:
        return False
    return Trip.objects.select_for_update(of=('self',)).filter(truck_id=truck_id).filter(
        Q(completed_at__gt=after, completed_at__lte=through)
        | Q(cancelled_at__gt=after, cancelled_at__lte=through)
        | Q(operationally_closed_at__gt=after, operationally_closed_at__lte=through)
    ).exists()


def _automatic_load_receipt(trip, *, acceptance=None):
    expected_type = (
        'excavator.free_bucket.loaded'
        if acceptance is not None
        else 'excavator.trip.loaded'
    )
    return (
        OfflineFieldEvent.objects.select_for_update(of=('self',))
        .filter(
            trip=trip,
            event_type=expected_type,
            status=OfflineFieldEventStatus.ACCEPTED,
        )
        .order_by('sequence', 'id')
        .first()
    )


def _automatic_load_authority_matches(receipt, *, assignment=None, acceptance=None):
    if acceptance is not None:
        return bool(receipt)
    if not receipt or assignment is None:
        return False
    return str((receipt.payload or {}).get('assignment_id') or '') == str(assignment.id)


def _claim_trip_for_driver_manual_event(trip, *, access, shift, event_id):
    from trips.models import TripClientAction

    if trip.driver_id not in (None, access.employee_id):
        _conflict(
            'driver_manual_owner_changed',
            'Рейс уже относится к другому водителю.',
        )
    if trip.driver_control_shift_id not in (None, shift.id):
        _conflict(
            'driver_manual_shift_changed',
            'Рейс уже относится к другой смене водителя.',
        )
    driver_fields = []
    if trip.driver_id is None:
        trip.driver = access.employee
        driver_fields.append('driver')
    if trip.driver_control_shift_id is None:
        trip.driver_control_shift = shift
        driver_fields.append('driver_control_shift')
    if not trip.driver_participation_recorded:
        trip.driver_participation_recorded = True
        driver_fields.append('driver_participation_recorded')
    if driver_fields:
        trip.save(update_fields=driver_fields)
    TripClientAction.objects.create(
        action_type='driver_manual_loaded',
        client_action_id=event_id,
        trip=trip,
        actor=access.employee,
    )


def _driver_manual_primary_context(*, excavator, payload, context_snapshot):
    from references.models import DumpPoint
    from trips.free_bucket import canonical_free_bucket_work_context_snapshot

    try:
        authoritative = canonical_free_bucket_work_context_snapshot(excavator)
    except ValidationError as error:
        _conflict('manual_work_context_unavailable', '; '.join(error.messages))
    checks = (
        ('placement_id', authoritative.get('placement_id'), payload.get('placement_id')),
        ('placement_updated_at', authoritative.get('placement_updated_at'), payload.get('placement_updated_at')),
        ('rock_type_id', authoritative.get('rock_type_id'), payload.get('rock_type_id')),
        ('loading_horizon', authoritative.get('loading_horizon'), payload.get('loading_horizon')),
        ('loading_block', authoritative.get('loading_block'), payload.get('loading_block')),
    )
    for field, actual, expected in checks:
        if str(actual or '') != str(expected or ''):
            _conflict(
                'manual_work_context_changed',
                'Настройки забоя изменились после сохранения отметки на телефоне.',
            )
    client_points = context_snapshot.get('dump_points')
    if not isinstance(client_points, list):
        _invalid('manual_dump_points_required', 'Не сохранён список точек ручного рейса.')
    authoritative_by_id = {str(item['id']): item for item in authoritative['dump_points']}
    client_by_id = {
        str(item.get('id')): item for item in client_points if isinstance(item, dict) and item.get('id')
    }
    dump_id = str(_positive_int(payload.get('dump_point_id'), field='dump_point_id'))
    selected_one_off = context_snapshot.get('selected_one_off') is True
    allowed_client_ids = set(client_by_id)
    if selected_one_off:
        allowed_client_ids.discard(dump_id)
    if set(authoritative_by_id) != allowed_client_ids:
        _conflict(
            'manual_work_context_changed',
            'Список точек разгрузки изменился после сохранения отметки на телефоне.',
        )
    selected = authoritative_by_id.get(dump_id)
    if not selected and selected_one_off:
        client_selected = client_by_id.get(dump_id)
        one_off_point = DumpPoint.objects.select_for_update().filter(
            pk=_positive_int(dump_id, field='dump_point_id'),
            is_active=True,
        ).first()
        if client_selected and one_off_point:
            selected = {
                'id': one_off_point.id,
                'name': str(one_off_point),
                'transport_distance_km': client_selected.get('transport_distance_km'),
            }
    if not selected:
        _conflict('manual_dump_point_not_allowed', 'Точка не входит в настройки выбранного экскаватора.')
    return authoritative, selected


def _process_driver_loaded(access, normalized):
    """Create or attach one Driver manual-load event to the common Trip row."""
    from assignments.models import HaulAssignment, HaulAssignmentAction
    from downtimes.models import DowntimeEvent
    from references.models import DumpPoint, RockType
    from trips.free_bucket import resolve_free_bucket_load_context
    from trips.models import FreeBucketAcceptanceStatus, OPEN_TRIP_STATUSES, Trip, TripClientAction
    from trips.trip_creation import create_loaded_waiting_unload_trip, lock_trip_participant_equipment
    from trips.views import finalize_trip_unloaded

    payload = normalized['payload']
    truck_id = _positive_int(payload.get('truck_id'), field='truck_id')
    excavator_id = _positive_int(payload.get('excavator_id'), field='excavator_id')
    lock_idempotency_key('trip_load_pair', f'{excavator_id}:{truck_id}')
    shift = _locked_shift(access, normalized, role_code='driver')
    if truck_id != shift.equipment_id:
        _conflict('driver_manual_truck_changed', 'Самосвал не принадлежит текущей смене водителя.')
    if payload.get('manual_control') is not True:
        _invalid('driver_manual_flag_required', 'Событие не помечено как ручная отправка водителя.')
    if normalized['local_trip_id'] != normalized['event_id']:
        _invalid('driver_manual_local_trip_invalid', 'Локальный ID ручного рейса должен совпадать с ID события.')

    lock_production_state()
    excavator, truck = lock_trip_participant_equipment(
        excavator_id=excavator_id,
        truck_id=truck_id,
    )
    _validate_free_bucket_participants(excavator, truck)
    shift.equipment = truck

    assignment_id = payload.get('assignment_id')
    acceptance_reference = (
        payload.get('free_bucket_acceptance_id')
        or payload.get('free_bucket_acceptance_local_id')
    )
    if bool(assignment_id) == bool(acceptance_reference):
        _invalid(
            'driver_manual_authority_ambiguous',
            'Нужно передать ровно одно основание: назначение или свободный ковш.',
        )

    assignment = None
    acceptance = None
    context_snapshot = normalized.get('context_snapshot') or {}
    if assignment_id:
        assignment = (
            HaulAssignment.objects.select_for_update(of=('self',))
            .select_related('truck', 'excavator')
            .filter(pk=_positive_int(assignment_id, field='assignment_id'), truck_id=truck.id, excavator_id=excavator.id)
            .first()
        )
        if not assignment or assignment.action != HaulAssignmentAction.ASSIGN:
            _conflict('assignment_context_changed', 'Назначение ручного рейса не найдено.')
        if normalized['occurred_at'] < assignment.assigned_at or (
            assignment.ended_at and normalized['occurred_at'] > assignment.ended_at
        ):
            _conflict('assignment_time_mismatch', 'В момент отметки это назначение не действовало.')
        assignment.truck = truck
        assignment.excavator = excavator
        authoritative, selected = _driver_manual_primary_context(
            excavator=excavator,
            payload=payload,
            context_snapshot=context_snapshot,
        )
        rock_type = RockType.objects.filter(pk=authoritative['rock_type_id']).first()
        dump_point = DumpPoint.objects.select_for_update().filter(pk=selected['id']).first()
        load_context = {
            'loading_horizon': authoritative['loading_horizon'],
            'loading_block': authoritative['loading_block'],
            'transport_distance_km': selected.get('transport_distance_km'),
        }
    else:
        acceptance = _resolve_free_bucket_acceptance(access, normalized)
        if acceptance.truck_id != truck.id or acceptance.excavator_id != excavator.id:
            _conflict('free_bucket_context_changed', 'Свободный ковш относится к другой технике.')
        owner_fields_mixed = bool(acceptance.requested_by_id) != bool(acceptance.requesting_shift_id)
        if owner_fields_mixed:
            _conflict(
                'free_bucket_request_owner_changed',
                'В запросе свободного ковша нарушена связь со сменой водителя.',
            )
        if acceptance.requested_by_id and (
            acceptance.requested_by_id != access.employee_id
            or acceptance.requesting_shift_id != shift.id
        ):
            _conflict(
                'free_bucket_request_owner_changed',
                'Запрос свободного ковша принадлежит другой смене водителя.',
            )
        if not acceptance.accepted_at:
            _retry('free_bucket_acceptance_pending', 'Машинист ещё не подтвердил свободный ковш.')
        if normalized['occurred_at'] < acceptance.accepted_at:
            _conflict(
                'free_bucket_load_before_accept',
                'Время ручной отправки раньше согласования свободного ковша.',
            )
        if not acceptance.requested_by_id:
            acceptance.requested_by = access.employee
            acceptance.requesting_shift = shift
            acceptance.save(update_fields=['requested_by', 'requesting_shift'])
        if acceptance.status == FreeBucketAcceptanceStatus.REQUESTED:
            _retry('free_bucket_acceptance_pending', 'Машинист ещё не подтвердил свободный ковш.')
        if acceptance.status not in (
            FreeBucketAcceptanceStatus.ACCEPTED,
            FreeBucketAcceptanceStatus.USED,
        ):
            _conflict('free_bucket_not_available', 'Свободный ковш уже отменён или закрыт.')
        try:
            load_context = resolve_free_bucket_load_context(acceptance, payload)
        except ValidationError as error:
            _conflict('free_bucket_work_context_changed', '; '.join(error.messages))
        rock_type = load_context['rock_type']
        dump_point = load_context['dump_point']

    if not rock_type or not dump_point:
        _conflict('reference_data_changed', 'Справочные данные ручного рейса больше недоступны.')

    truck_downtime = (
        DowntimeEvent.objects.select_for_update(of=('self',)).select_related('reason')
        .filter(equipment=truck, ended_at__isnull=True).order_by('-started_at', '-id').first()
    )
    excavator_downtimes = list(
        DowntimeEvent.objects.select_for_update(of=('self',)).select_related('reason')
        .filter(equipment=excavator, ended_at__isnull=True).order_by('id')
    )
    if truck_downtime or any(item.reason.is_critical for item in excavator_downtimes):
        _conflict('equipment_downtime_active', 'Ручная отправка невозможна: открыт блокирующий простой.')

    open_trip = (
        Trip.objects.select_for_update(of=('self',))
        .filter(truck=truck, status__in=OPEN_TRIP_STATUSES)
        .select_related('free_bucket_acceptance')
        .first()
    )
    if open_trip:
        existing_manual = TripClientAction.objects.select_for_update(of=('self',)).filter(
            trip=open_trip,
            action_type='driver_manual_loaded',
        ).first()
        automatic = TripClientAction.objects.select_for_update(of=('self',)).filter(
            trip=open_trip,
            action_type__in=['truck_loaded', 'free_bucket_loaded'],
        ).exists()
        automatic_receipt = _automatic_load_receipt(open_trip, acceptance=acceptance)
        same_cycle = bool(
            automatic_receipt
            and not _trip_terminal_changed_between(
                truck.id,
                after=min(normalized['occurred_at'], automatic_receipt.occurred_at),
                through=max(normalized['occurred_at'], automatic_receipt.occurred_at),
            )
        )
        authority_matches = _automatic_load_authority_matches(
            automatic_receipt,
            assignment=assignment,
            acceptance=acceptance,
        )
        if existing_manual and not automatic:
            previous_loaded_at = open_trip.loaded_at or open_trip.created_at
            if normalized['occurred_at'] <= previous_loaded_at:
                _conflict(
                    'stale_driver_manual_load',
                    'После этой отметки уже сохранён более новый ручной рейс.',
                )
            if not finalize_trip_unloaded(
                open_trip,
                driver=access.employee,
                unloading_shift=shift,
                occurred_at=normalized['occurred_at'],
            ):
                _conflict('driver_manual_cycle_changed', 'Предыдущий ручной рейс уже изменился.')
            TripClientAction.objects.create(
                action_type='driver_manual_cycle_advanced',
                client_action_id=normalized['event_id'],
                trip=open_trip,
                actor=access.employee,
            )
            open_trip = None
        elif (
            not automatic
            or not same_cycle
            or not authority_matches
            or not _manual_load_matches_trip(open_trip, payload, acceptance=acceptance)
        ):
            _conflict('open_trip_changed', 'У самосвала уже есть другой незавершённый рейс.')
        if open_trip:
            _claim_trip_for_driver_manual_event(
                open_trip,
                access=access,
                shift=shift,
                event_id=normalized['event_id'],
            )
            state = bump_operational_state(
                'OfflineFieldEvent:driver_manual_linked', event_type='trip_changed',
                object_type='Trip', object_id=open_trip.id,
                payload={'action': 'driver_manual_linked', 'trip_id': open_trip.id,
                         'truck_id': open_trip.truck_id, 'excavator_id': open_trip.excavator_id},
            )
            return {
                'server_ids': {'trip_id': open_trip.id, 'shift_id': shift.id},
                'trip_origin': 'excavator',
                'version': state.version,
            }, {'trip': open_trip, 'shift': shift, 'equipment': truck}

    terminal_candidates = list(
        Trip.objects.select_for_update(of=('self',))
        .filter(truck=truck, excavator=excavator, created_at__lte=normalized['received_at'])
        .exclude(status__in=OPEN_TRIP_STATUSES)
        .filter(
            Q(completed_at__gte=normalized['occurred_at'], completed_at__lte=normalized['received_at'])
            | Q(cancelled_at__gte=normalized['occurred_at'], cancelled_at__lte=normalized['received_at'])
            | Q(
                operationally_closed_at__gte=normalized['occurred_at'],
                operationally_closed_at__lte=normalized['received_at'],
            )
        )
        .select_related('free_bucket_acceptance')
        .order_by('id')
    )
    terminal_matches = []
    for candidate in terminal_candidates:
        automatic_receipt = _automatic_load_receipt(candidate, acceptance=acceptance)
        if (
            _automatic_load_authority_matches(
                automatic_receipt,
                assignment=assignment,
                acceptance=acceptance,
            )
            and _manual_load_matches_trip(candidate, payload, acceptance=acceptance)
        ):
            terminal_matches.append(candidate)
    if len(terminal_matches) == 1:
        terminal_trip = terminal_matches[0]
        _claim_trip_for_driver_manual_event(
            terminal_trip,
            access=access,
            shift=shift,
            event_id=normalized['event_id'],
        )
        state = bump_operational_state(
            'OfflineFieldEvent:driver_manual_linked_terminal',
            event_type='trip_changed',
            object_type='Trip',
            object_id=terminal_trip.id,
            payload={
                'action': 'driver_manual_linked_terminal',
                'trip_id': terminal_trip.id,
                'truck_id': terminal_trip.truck_id,
                'excavator_id': terminal_trip.excavator_id,
                'status': terminal_trip.status,
            },
        )
        return {
            'server_ids': {'trip_id': terminal_trip.id, 'shift_id': shift.id},
            'trip_origin': 'excavator',
            'version': state.version,
        }, {'trip': terminal_trip, 'shift': shift, 'equipment': truck}
    if terminal_candidates or _trip_terminal_changed_between(
        truck.id,
        after=normalized['occurred_at'],
        through=normalized['received_at'],
    ):
        _conflict(
            'stale_driver_manual_load',
            'После этой отметки состояние рейса уже изменилось; новый рейс не создан.',
        )

    if acceptance and acceptance.status != FreeBucketAcceptanceStatus.ACCEPTED:
        _conflict('free_bucket_already_loaded', 'Погрузка по свободному ковшу уже выполнена.')
    participation = {
        'shift': shift,
        'control_shift': shift,
        'passive': False,
        'code': 'driver_manual',
        'label': '',
    }
    try:
        trip = create_loaded_waiting_unload_trip(
            assignment=assignment,
            truck=truck if acceptance else None,
            excavator=excavator if acceptance else None,
            free_bucket_acceptance=acceptance,
            excavator_operator=acceptance.operator if acceptance else None,
            loading_shift=acceptance.loading_shift if acceptance else None,
            rock_type=rock_type,
            dump_point=dump_point,
            loading_horizon=load_context['loading_horizon'],
            loading_block=load_context['loading_block'],
            transport_distance_km=load_context['transport_distance_km'],
            note=str(payload.get('note') or 'Ручная отправка водителем')[:1000],
            participation=participation,
            occurred_at=normalized['occurred_at'],
            resolve_assignment_transition=False,
            driver=access.employee,
            driver_participation_recorded=True,
            load_time_source='driver_device',
        )
    except ValidationError as error:
        _conflict('trip_validation_failed', '; '.join(error.messages))
    TripClientAction.objects.create(
        action_type='driver_manual_loaded',
        client_action_id=normalized['event_id'],
        trip=trip,
        actor=access.employee,
    )
    if acceptance:
        acceptance.status = FreeBucketAcceptanceStatus.USED
        acceptance.used_at = normalized['occurred_at']
        acceptance.used_trip = trip
        acceptance.save(update_fields=['status', 'used_at', 'used_trip'])
    state = bump_operational_state(
        'OfflineFieldEvent:driver_trip_loaded', event_type='trip_changed',
        object_type='Trip', object_id=trip.id,
        payload={'action': 'driver_manual_loaded', 'trip_id': trip.id,
                 'truck_id': trip.truck_id, 'excavator_id': trip.excavator_id,
                 'dump_point_id': trip.dump_point_id, 'status': trip.status},
    )
    return {
        'server_ids': {'trip_id': trip.id, 'shift_id': shift.id},
        'trip_origin': 'driver_manual',
        'version': state.version,
    }, {'trip': trip, 'shift': shift, 'equipment': truck,
        'free_bucket_acceptance': acceptance}


def _process_excavator_loaded(access, normalized):
    from assignments.models import AssignmentStatus
    from downtimes.driver_workflow import close_truck_waiting_loading_downtimes
    from downtimes.models import DowntimeEvent
    from references.models import DumpPoint, RockType
    from trips.manual_loading import may_replace_open_trip
    from trips.models import OPEN_TRIP_STATUSES, Trip, TripClientAction
    from trips.trip_creation import create_loaded_waiting_unload_trip, lock_trip_participant_equipment
    from trips.views import (
        excavator_truck_load_block,
        notify_driver_truck_loaded,
        reconcile_excavator_waiting_for_trucks,
    )

    payload = normalized['payload']
    truck_id = _positive_int(payload.get('truck_id'), field='truck_id')
    assignment_id = _positive_int(payload.get('assignment_id'), field='assignment_id')
    excavator_id = _positive_int(normalized.get('equipment_id'), field='equipment_id')
    lock_idempotency_key('trip_load_pair', f'{excavator_id}:{truck_id}')
    shift = _locked_shift(access, normalized, role_code='excavator_operator')
    lock_production_state()
    excavator, truck = lock_trip_participant_equipment(excavator_id=shift.equipment_id, truck_id=truck_id)
    shift.equipment = excavator
    from trips.free_bucket import active_free_bucket_acceptance_for_truck
    pending_acceptance = active_free_bucket_acceptance_for_truck(truck, for_update=True)
    if pending_acceptance:
        _conflict(
            'free_bucket_acceptance_required',
            'Самосвал принят под свободный ковш; погрузка возможна только через этот временный приём.',
        )
    assignment = _historical_excavator_assignment(
        shift=shift, truck_id=truck_id, requested_assignment_id=assignment_id,
        occurred_at=normalized['occurred_at'],
    )
    assignment.truck = truck
    assignment.excavator = excavator
    from shifts.models import EmployeeShift

    historical_driver_shift = (
        EmployeeShift.objects.select_for_update(of=('self',))
        .filter(
            equipment_id=truck_id,
            opened_at__lte=normalized['occurred_at'],
        )
        .filter(Q(closed_at__isnull=True) | Q(closed_at__gte=normalized['occurred_at']))
        .filter(
            Q(workplace_code='driver')
            | Q(workplace_code='', equipment__equipment_type__name='Самосвал')
        )
        .order_by('-opened_at', '-id')
        .first()
    )
    manual_control = payload.get('manual_control') is True
    participation = {
        'shift': historical_driver_shift,
        'control_shift': None if manual_control else historical_driver_shift,
        'passive': manual_control,
        'code': 'offline_manual' if manual_control else 'historical_driver_shift',
        'label': '',
    }
    open_trip = (
        Trip.objects.select_for_update().filter(truck_id=truck_id, status__in=OPEN_TRIP_STATUSES).first()
    )
    merge_driver_trip = False
    expected_local = str(payload.get('expected_open_trip_local_id') or '').strip()
    expected_id = payload.get('expected_open_trip_id')
    linked_previous = None
    if expected_local:
        previous_event = (
            OfflineFieldEvent.objects.select_for_update(of=('self',))
            .filter(
                actor=access.employee, device_id=normalized['device_id'],
                local_trip_id=expected_local, event_type='excavator.trip.loaded',
                status=OfflineFieldEventStatus.ACCEPTED, trip__isnull=False,
            )
            .order_by('sequence', 'id').first()
        )
        if not previous_event:
            _retry('previous_trip_pending', 'Предыдущая offline-погрузка ещё не принята.')
        linked_previous = previous_event.trip
        expected_id = linked_previous.id
    if open_trip:
        manual_receipt = (
            OfflineFieldEvent.objects.select_for_update(of=('self',))
            .filter(
                trip=open_trip,
                event_type='driver.trip.loaded',
                status=OfflineFieldEventStatus.ACCEPTED,
            )
            .order_by('sequence', 'id')
            .first()
        )
        merge_driver_trip = bool(
            not expected_id
            and manual_receipt
            and str((manual_receipt.payload or {}).get('assignment_id') or '') == str(assignment.id)
            and not _trip_terminal_changed_between(
                truck.id,
                after=min(normalized['occurred_at'], manual_receipt.occurred_at),
                through=max(normalized['occurred_at'], manual_receipt.occurred_at),
            )
            and _manual_load_matches_trip(
                open_trip,
                {**payload, 'excavator_id': excavator.id},
            )
            and not TripClientAction.objects.select_for_update(of=('self',)).filter(
                trip=open_trip,
                action_type='truck_loaded',
            ).exists()
        )
        if not merge_driver_trip:
            if str(expected_id or '') != str(open_trip.id):
                _conflict('open_trip_changed', 'Незакрытый рейс самосвала уже изменился.')
            prior_is_own_offline_event = bool(linked_previous and linked_previous.id == open_trip.id)
            if not prior_is_own_offline_event and not may_replace_open_trip(open_trip, participation):
                _conflict('open_trip_cannot_be_replaced', 'Действующий рейс нельзя заменить этой погрузкой.')
    truck_downtime = (
        DowntimeEvent.objects.select_for_update(of=('self',))
        .select_related('reason')
        .filter(equipment=truck, ended_at__isnull=True)
        .order_by('-started_at', '-id')
        .first()
    )
    excavator_downtimes = list(
        DowntimeEvent.objects.select_for_update(of=('self',))
        .select_related('reason')
        .filter(equipment=excavator, ended_at__isnull=True)
        .order_by('id')
    )
    if (
        (truck_downtime and truck_downtime.started_at > normalized['occurred_at'])
        or any(item.started_at > normalized['occurred_at'] for item in excavator_downtimes)
    ):
        _conflict(
            'newer_downtime_exists',
            'После сохранённой погрузки состояние простоя техники уже изменилось.',
        )
    load_block = excavator_truck_load_block(
        assignment,
        current_excavator=excavator,
        active_trip=False if merge_driver_trip else (open_trip or False),
        active_downtime=truck_downtime or False,
        manual_control=manual_control,
        participation=participation,
        has_open_truck_shift=bool(historical_driver_shift),
    )
    if load_block:
        _conflict(load_block['code'], load_block['label'])
    if manual_control and (
        not excavator.is_active
        or any(item.reason.is_critical for item in excavator_downtimes)
    ):
        _conflict('excavator_unavailable', 'Экскаватор недоступен для работы.')
    dump_point_id = _positive_int(payload.get('dump_point_id'), field='dump_point_id')
    rock_type_id = _positive_int(payload.get('rock_type_id') or payload.get('rock_type'), field='rock_type_id')
    dump_point = DumpPoint.objects.select_for_update().filter(pk=dump_point_id).first()
    rock_type = RockType.objects.filter(pk=rock_type_id).first()
    if not dump_point or not rock_type:
        _conflict('reference_data_changed', 'Справочные данные погрузки больше недоступны.')
    try:
        if merge_driver_trip:
            trip = open_trip
            trip.excavator_operator = access.employee
            trip.loading_shift = shift
            trip.loaded_at = normalized['occurred_at']
            trip.load_received_at = timezone.now()
            trip.load_time_source = 'excavator_device'
            trip.save(update_fields=[
                'excavator_operator', 'loading_shift', 'loaded_at',
                'load_received_at', 'load_time_source',
            ])
        else:
            trip = create_loaded_waiting_unload_trip(
            assignment=assignment,
            excavator_operator=access.employee,
            loading_shift=shift,
            rock_type=rock_type,
            dump_point=dump_point,
            planned_volume_m3=payload.get('planned_volume_m3') or None,
            loading_horizon=str(payload.get('loading_horizon') or '')[:64],
            loading_block=str(payload.get('loading_block') or '')[:64],
            transport_distance_km=payload.get('transport_distance_km') or None,
            downtime_text=payload.get('downtime_text'),
            note=payload.get('note'),
            participation=participation,
            supersede_trip=open_trip,
            occurred_at=normalized['occurred_at'],
            resolve_assignment_transition=(
                assignment.ended_at is None
                and assignment.status in {AssignmentStatus.ACCEPTED, AssignmentStatus.PENDING}
            ),
        )
    except ValidationError as error:
        _conflict('trip_validation_failed', '; '.join(error.messages))
    TripClientAction.objects.create(
        action_type='truck_loaded', client_action_id=normalized['event_id'],
        trip=trip, actor=access.employee,
    )
    if open_trip and not merge_driver_trip:
        TripClientAction.objects.create(
            action_type='truck_load_supersede', client_action_id=normalized['event_id'],
            trip=open_trip, actor=access.employee,
        )
    close_truck_waiting_loading_downtimes(truck, ended_at=normalized['occurred_at'])
    for downtime in excavator_downtimes:
        downtime.ended_at = normalized['occurred_at']
        downtime.save(update_fields=['ended_at'])
    reconcile_excavator_waiting_for_trucks(excavator, access.employee, start_when_empty=True)
    state = bump_operational_state(
        'OfflineFieldEvent:excavator_trip_loaded', event_type='trip_changed',
        object_type='Trip', object_id=trip.id,
        payload={
            'action': 'truck_loaded',
            'trip_id': trip.id,
            'truck_id': trip.truck_id,
            'excavator_id': trip.excavator_id,
            'excavator_ids': [trip.excavator_id],
            'driver_participation_recorded': trip.driver_participation_recorded,
            'driver_control_shift_id': trip.driver_control_shift_id,
            'dump_point_id': trip.dump_point_id,
            'assigned_dump_point_id': trip.assigned_dump_point_id,
            'actual_dump_point_id': trip.actual_dump_point_id,
            'dump_point_name': str(trip.assigned_dump_point or trip.dump_point),
            'status': trip.status,
        },
    )
    transaction.on_commit(lambda: notify_driver_truck_loaded(trip))
    return {
        'server_ids': {'trip_id': trip.id, 'shift_id': shift.id},
        'version': state.version,
    }, {'trip': trip, 'shift': shift, 'equipment': truck}


def _create_free_bucket_load_review(*, access, normalized, receipt, problem):
    """Place an irreconcilable actual free-bucket load in existing review UI.

    The immutable offline receipt remains the evidence; the linked
    administrative conflict gives a responsible employee the existing,
    audited status-resolution workflow.
    """
    if (
        problem.status != OfflineFieldEventStatus.CONFLICT
        or normalized.get('event_type') != 'excavator.free_bucket.loaded'
    ):
        return
    from users.models import AdminConflict

    payload = normalized.get('payload') or {}
    truck = payload.get('truck_number') or payload.get('truck_id') or 'не указан'
    dump_point = payload.get('dump_point_name') or payload.get('dump_point_id') or 'не указана'
    description = (
        f'Offline-событие {receipt.event_id}: погрузка самосвала {truck} '
        f'под свободным ковшом на точку {dump_point}. Причина: {problem.message}'
    )
    AdminConflict.objects.get_or_create(
        employee=access.employee,
        role=access.role,
        conflict_type='Спорная погрузка под свободным ковшом',
        process='Свободный ковш',
        description=description,
    )


def _process_excavator_loaded_cancelled(access, normalized):
    from trips.models import Trip, TripClientAction, TripStatus
    from trips.views import reconcile_excavator_waiting_for_trucks

    shift = _locked_shift(access, normalized, role_code='excavator_operator')
    lock_production_state()
    trip = _resolve_trip_reference(access, normalized)
    if trip.loading_shift_id != shift.id or trip.excavator_operator_id != access.employee_id:
        _conflict('trip_owner_changed', 'Рейс не принадлежит этой смене машиниста.')
    if normalized['occurred_at'] < (trip.loaded_at or trip.created_at):
        _conflict('cancel_before_load', 'Время отмены раньше времени погрузки.')
    if trip.status != TripStatus.LOADED_WAITING_UNLOAD:
        _conflict('trip_not_cancellable', 'Рейс уже завершён, отменён или заменён.')
    trip.status = TripStatus.CANCELLED
    trip.cancelled_at = normalized['occurred_at']
    trip.save(update_fields=['status', 'cancelled_at'])
    from trips.free_bucket import close_free_bucket_acceptance_for_trip
    close_free_bucket_acceptance_for_trip(trip, closed_at=trip.cancelled_at)
    previous = Trip.objects.select_for_update().filter(
        superseded_by=trip, status=TripStatus.UNCONTROLLED,
    ).first()
    if previous:
        previous.status = TripStatus.LOADED_WAITING_UNLOAD
        previous.operationally_closed_at = None
        previous.closure_recorded_by = None
        previous.superseded_by = None
        previous.save(update_fields=['status', 'operationally_closed_at', 'closure_recorded_by', 'superseded_by'])
    TripClientAction.objects.create(
        action_type='truck_loaded_cancel', client_action_id=normalized['event_id'],
        trip=trip, actor=access.employee,
    )
    reconcile_excavator_waiting_for_trucks(trip.excavator)
    state = bump_operational_state(
        'OfflineFieldEvent:excavator_trip_loaded_cancelled', event_type='trip_changed',
        object_type='Trip', object_id=trip.id,
        payload={'action': 'truck_loaded_cancel', 'trip_id': trip.id, 'truck_id': trip.truck_id,
                 'excavator_id': trip.excavator_id, 'status': trip.status},
    )
    return {'server_ids': {'trip_id': trip.id, 'shift_id': shift.id}, 'version': state.version}, {
        'trip': trip, 'shift': shift, 'equipment': trip.truck,
    }


def _process_driver_loaded_cancelled(access, normalized):
    """Cancel only the Driver's latest open manual-load mark."""
    from trips.free_bucket import close_free_bucket_acceptance_for_trip
    from trips.models import OPEN_TRIP_STATUSES, Trip, TripClientAction, TripStatus
    from trips.trip_creation import lock_trip_participant_equipment
    from trips.views import reconcile_excavator_waiting_for_trucks

    payload = normalized['payload']
    truck_id = _positive_int(payload.get('truck_id'), field='truck_id')
    excavator_id = _positive_int(payload.get('excavator_id'), field='excavator_id')
    dump_point_id = _positive_int(payload.get('dump_point_id'), field='dump_point_id')
    if payload.get('manual_control') is not True:
        _invalid(
            'driver_manual_flag_required',
            'Событие не помечено как отмена ручной отправки водителя.',
        )

    lock_idempotency_key('trip_load_pair', f'{excavator_id}:{truck_id}')
    shift = _locked_shift(access, normalized, role_code='driver')
    if truck_id != shift.equipment_id:
        _conflict(
            'driver_manual_truck_changed',
            'Самосвал не принадлежит текущей смене водителя.',
        )
    lock_production_state()
    excavator, truck = lock_trip_participant_equipment(
        excavator_id=excavator_id,
        truck_id=truck_id,
    )
    trip = _resolve_trip_reference(access, normalized)
    if trip.truck_id != truck.id or trip.excavator_id != excavator.id:
        _conflict(
            'driver_manual_trip_changed',
            'Последний ручной рейс относится к другой технике.',
        )
    if (
        trip.driver_id != access.employee_id
        or trip.driver_control_shift_id != shift.id
        or not trip.driver_participation_recorded
    ):
        _conflict(
            'trip_owner_changed',
            'Ручной рейс не принадлежит текущей смене водителя.',
        )
    if normalized['occurred_at'] < (trip.loaded_at or trip.created_at):
        _conflict(
            'cancel_before_load',
            'Время отмены раньше времени ручной погрузки.',
        )
    if trip.status != TripStatus.LOADED_WAITING_UNLOAD:
        _conflict(
            'trip_not_cancellable',
            'Последний ручной рейс уже завершён, отменён или заменён следующим.',
        )
    current_open_trip = Trip.objects.select_for_update(of=('self',)).filter(
        truck=truck,
        status__in=OPEN_TRIP_STATUSES,
    ).first()
    if not current_open_trip or current_open_trip.id != trip.id:
        _conflict(
            'driver_manual_trip_changed',
            'После этого ручного рейса состояние самосвала уже изменилось.',
        )
    actual_dump_point_id = trip.actual_dump_point_id or trip.dump_point_id
    if actual_dump_point_id != dump_point_id:
        _conflict(
            'driver_manual_dump_point_changed',
            'Точка последнего ручного рейса уже изменилась.',
        )
    if not TripClientAction.objects.select_for_update(of=('self',)).filter(
        trip=trip,
        action_type='driver_manual_loaded',
        actor=access.employee,
    ).exists():
        _conflict(
            'driver_manual_trip_required',
            'Отменять свайпом можно только ручной рейс этого водителя.',
        )
    if TripClientAction.objects.select_for_update(of=('self',)).filter(
        trip=trip,
        action_type__in=['truck_loaded', 'free_bucket_loaded'],
    ).exists():
        _conflict(
            'automatic_load_cannot_be_cancelled_by_driver',
            'Фактическую погрузку машиниста нельзя отменить из ручного режима водителя.',
        )

    trip.status = TripStatus.CANCELLED
    trip.cancelled_at = normalized['occurred_at']
    trip.save(update_fields=['status', 'cancelled_at'])
    close_free_bucket_acceptance_for_trip(trip, closed_at=trip.cancelled_at)
    TripClientAction.objects.create(
        action_type='driver_manual_loaded_cancel',
        client_action_id=normalized['event_id'],
        trip=trip,
        actor=access.employee,
    )
    reconcile_excavator_waiting_for_trucks(excavator)
    state = bump_operational_state(
        'OfflineFieldEvent:driver_manual_loaded_cancelled',
        event_type='trip_changed',
        object_type='Trip',
        object_id=trip.id,
        payload={
            'action': 'driver_manual_loaded_cancel',
            'trip_id': trip.id,
            'truck_id': trip.truck_id,
            'excavator_id': trip.excavator_id,
            'status': trip.status,
        },
    )
    return {
        'server_ids': {'trip_id': trip.id, 'shift_id': shift.id},
        'trip_origin': 'driver_manual',
        'version': state.version,
    }, {'trip': trip, 'shift': shift, 'equipment': truck}


def _process_driver_unloaded(access, normalized):
    from trips.models import OPEN_TRIP_STATUSES, TripClientAction, TripStatus
    from trips.views import finalize_trip_unloaded

    shift = _locked_shift(access, normalized, role_code='driver')
    lock_production_state()
    trip = _resolve_trip_reference(access, normalized)
    if trip.truck_id != shift.equipment_id:
        _conflict('trip_truck_changed', 'Рейс не принадлежит самосвалу этой смены.')
    if trip.driver_participation_recorded and trip.driver_control_shift_id != shift.id:
        _conflict('trip_driver_shift_changed', 'Рейс закреплён за другой сменой водителя.')
    manual_load = TripClientAction.objects.select_for_update(of=('self',)).filter(
        trip=trip,
        action_type='driver_manual_loaded',
    ).exists()
    automatic_load = TripClientAction.objects.select_for_update(of=('self',)).filter(
        trip=trip,
        action_type__in=['truck_loaded', 'free_bucket_loaded'],
    ).exists()
    if manual_load and not automatic_load:
        _conflict(
            'driver_manual_unload_not_required',
            'Ручной рейс завершается следующей ручной погрузкой и не требует отдельной разгрузки.',
        )
    if trip.status not in (*OPEN_TRIP_STATUSES, TripStatus.UNCONTROLLED):
        _conflict('trip_already_terminal', 'Рейс уже завершён или отменён другим действием.')
    loaded_at = trip.loaded_at or trip.created_at
    if normalized['occurred_at'] < loaded_at:
        _conflict('unload_before_load', 'Время разгрузки раньше погрузки.')
    late_confirmation = trip.status == TripStatus.UNCONTROLLED
    if late_confirmation and trip.operationally_closed_at and normalized['occurred_at'] > trip.operationally_closed_at:
        _conflict('late_unload_after_supersede', 'Разгрузка относится к более позднему рейсу.')
    finalize_trip_unloaded(
        trip, driver=access.employee, unloading_shift=shift,
        occurred_at=normalized['occurred_at'], late_confirmation=late_confirmation,
    )
    TripClientAction.objects.create(
        action_type='trip_unloaded', client_action_id=normalized['event_id'],
        trip=trip, actor=access.employee,
    )
    state = bump_operational_state(
        'OfflineFieldEvent:driver_trip_unloaded', event_type='trip_changed',
        object_type='Trip', object_id=trip.id,
        payload={'action': 'trip_unloaded', 'trip_id': trip.id, 'truck_id': trip.truck_id,
                 'excavator_id': trip.excavator_id, 'status': trip.status},
    )
    return {'server_ids': {'trip_id': trip.id, 'shift_id': shift.id}, 'version': state.version}, {
        'trip': trip, 'shift': shift, 'equipment': trip.truck,
    }


def _process_driver_dump_point_changed(access, normalized):
    from references.models import DumpPoint
    from trips.free_bucket import free_bucket_snapshot_dump_points_for_trip
    from trips.models import OPEN_TRIP_STATUSES, TripClientAction

    shift = _locked_shift(access, normalized, role_code='driver')
    production_state = lock_production_state()
    trip = _resolve_trip_reference(access, normalized)
    if trip.truck_id != shift.equipment_id or trip.status not in OPEN_TRIP_STATUSES:
        _conflict('trip_not_editable', 'Рейс изменился или уже завершён.')
    if trip.driver_participation_recorded and trip.driver_control_shift_id != shift.id:
        _conflict('trip_driver_shift_changed', 'Рейс закреплён за другой сменой водителя.')
    if normalized['occurred_at'] < (trip.loaded_at or trip.created_at):
        _conflict('dump_point_change_before_load', 'Время изменения точки раньше погрузки.')
    latest_offline_change = OfflineFieldEvent.objects.select_for_update(of=('self',)).filter(
        trip=trip,
        event_type='driver.trip.dump_point_changed',
        status=OfflineFieldEventStatus.ACCEPTED,
        occurred_at__gte=normalized['occurred_at'],
    ).exclude(event_id=normalized['event_id']).order_by(
        '-occurred_at', '-updated_at', '-id',
    ).first()
    if latest_offline_change and (
        latest_offline_change.occurred_at > normalized['occurred_at']
        or latest_offline_change.event_id not in normalized['depends_on']
    ):
        _conflict('stale_dump_point_change', 'После этого действия точка разгрузки уже менялась.')
    offline_change_ids = OfflineFieldEvent.objects.filter(
        trip=trip,
        event_type='driver.trip.dump_point_changed',
    ).values_list('event_id', flat=True)
    newer_legacy_change = (
        TripClientAction.objects.select_for_update(of=('self',))
        .filter(
            trip=trip,
            action_type='change_actual_unload_point',
            created_at__gt=normalized['occurred_at'],
        )
        .exclude(client_action_id=normalized['event_id'])
        .exclude(client_action_id__in=offline_change_ids)
        .exists()
    )
    if newer_legacy_change:
        _conflict('stale_dump_point_change', 'После этого действия точка разгрузки уже менялась.')
    expected_dump_point_id = (
        normalized['payload'].get('expected_actual_dump_point_id')
        or normalized['payload'].get('expected_dump_point_id')
    )
    current_dump_point_id = trip.actual_dump_point_id or trip.dump_point_id
    if expected_dump_point_id not in (None, '') and (
        _positive_int(expected_dump_point_id, field='expected_dump_point_id') != current_dump_point_id
    ):
        _conflict('dump_point_state_changed', 'Текущая точка разгрузки уже отличается от сохранённого состояния.')
    dump_point_id = _positive_int(normalized['payload'].get('dump_point_id'), field='dump_point_id')
    free_bucket_dump_points = free_bucket_snapshot_dump_points_for_trip(trip)
    if free_bucket_dump_points is None:
        dump_point = DumpPoint.objects.select_for_update().filter(
            pk=dump_point_id,
            is_active=True,
        ).first()
    else:
        allowed_ids = {item['id'] for item in free_bucket_dump_points}
        if dump_point_id not in allowed_ids:
            _conflict(
                'free_bucket_dump_point_changed',
                'Точка разгрузки не входила в сохранённые настройки свободного ковша.',
            )
        # The immutable snapshot remains authoritative even if a directory row
        # is later deactivated. The historical load must keep its saved route.
        dump_point = DumpPoint.objects.select_for_update().filter(pk=dump_point_id).first()
    if not dump_point:
        _conflict('dump_point_changed', 'Точка разгрузки больше недоступна.')
    if current_dump_point_id == dump_point.id:
        return {
            'server_ids': {'trip_id': trip.id, 'shift_id': shift.id, 'dump_point_id': dump_point.id},
            'version': production_state.version,
            'no_change': True,
        }, {'trip': trip, 'shift': shift, 'equipment': trip.truck}
    if trip.assigned_dump_point_id is None:
        trip.assigned_dump_point = trip.dump_point
    trip.actual_dump_point = dump_point
    trip.dump_point = dump_point
    trip.save(update_fields=['assigned_dump_point', 'actual_dump_point', 'dump_point'])
    TripClientAction.objects.create(
        action_type='change_actual_unload_point', client_action_id=normalized['event_id'],
        trip=trip, actor=access.employee,
    )
    state = bump_operational_state(
        'OfflineFieldEvent:driver_dump_point_changed', event_type='trip_changed',
        object_type='Trip', object_id=trip.id,
        payload={'action': 'change_actual_unload_point', 'trip_id': trip.id,
                 'truck_id': trip.truck_id, 'excavator_id': trip.excavator_id,
                 'previous_dump_point_id': current_dump_point_id,
                 'actual_dump_point_id': dump_point.id, 'actor_id': access.employee_id,
                 'occurred_at': normalized['occurred_at'].isoformat(), 'status': trip.status},
    )
    return {
        'server_ids': {'trip_id': trip.id, 'shift_id': shift.id, 'dump_point_id': dump_point.id},
        'version': state.version,
    }, {'trip': trip, 'shift': shift, 'equipment': trip.truck}


def _process_downtime(access, normalized, *, role_code, close):
    from downtimes.driver_workflow import (
        driver_downtime_requires_empty_truck,
        driver_downtime_requires_loaded_trip,
    )
    from downtimes.models import DowntimeEvent, DowntimeReason
    from trips.models import OPEN_TRIP_STATUSES, Trip, TripStatus

    shift = _locked_shift(access, normalized, role_code=role_code)
    lock_production_state()
    equipment = shift.equipment.__class__.objects.select_for_update().get(pk=shift.equipment_id)
    if close:
        downtime_id = (
            normalized['payload'].get('downtime_event_id')
            or normalized['payload'].get('downtime_id')
        )
        event = None
        if downtime_id:
            event = DowntimeEvent.objects.select_for_update(of=('self',)).filter(pk=downtime_id).first()
        elif normalized['local_downtime_id']:
            source = (
                OfflineFieldEvent.objects.select_for_update(of=('self',))
                .filter(
                    actor=access.employee, device_id=normalized['device_id'],
                    local_downtime_id=normalized['local_downtime_id'],
                    event_type=f'{"driver" if role_code == "driver" else "excavator"}.downtime.started',
                    status=OfflineFieldEventStatus.ACCEPTED,
                    downtime_event__isnull=False,
                ).order_by('sequence', 'id').first()
            )
            event = source.downtime_event if source else None
        if not event:
            _retry('downtime_reference_pending', 'Связанный простой ещё не подтверждён.')
        if event.equipment_id != equipment.id:
            _conflict('downtime_equipment_changed', 'Простой относится к другой технике.')
        if normalized['occurred_at'] < event.started_at:
            _conflict('downtime_end_before_start', 'Время окончания простоя раньше его начала.')
        if event.employee_id not in (None, access.employee_id):
            _conflict('downtime_owner_changed', 'Простой относится к другому сотруднику.')
        if event.ended_at:
            if normalized['occurred_at'] > event.ended_at:
                _conflict('downtime_already_closed', 'Простой уже завершён более ранним серверным действием.')
            if normalized['occurred_at'] < event.ended_at:
                event.ended_at = normalized['occurred_at']
                event.save(update_fields=['ended_at'])
        else:
            event.ended_at = normalized['occurred_at']
            event.save(update_fields=['ended_at'])
        action = 'downtime_closed'
    else:
        reason_id = _positive_int(normalized['payload'].get('reason_id'), field='reason_id')
        workplace = 'truck_driver' if role_code == 'driver' else 'excavator_operator'
        reason = DowntimeReason.for_workplace(workplace, equipment.equipment_type).filter(pk=reason_id).first()
        if not reason:
            _conflict('downtime_reason_changed', 'Причина простоя больше недоступна.')
        if role_code == 'driver' and driver_downtime_requires_empty_truck(reason):
            if Trip.objects.select_for_update().filter(truck=equipment, status__in=OPEN_TRIP_STATUSES).exists():
                _conflict('empty_truck_required', 'Ожидание погрузки нельзя начать: самосвал уже загружен.')
        if role_code == 'driver' and driver_downtime_requires_loaded_trip(reason):
            if not Trip.objects.select_for_update().filter(
                truck=equipment, status=TripStatus.LOADED_WAITING_UNLOAD,
            ).exists():
                _conflict('loaded_trip_required', 'Этот простой доступен только после погрузки.')
        open_event = DowntimeEvent.objects.select_for_update(of=('self',)).filter(
            equipment=equipment, ended_at__isnull=True,
        ).order_by('-started_at', '-id').first()
        if open_event and open_event.employee_id != access.employee_id:
            _conflict(
                'downtime_owner_changed',
                'Активный простой начат другим сотрудником: его можно завершить, но нельзя подменить его причину.',
            )
        if open_event and normalized['occurred_at'] < open_event.started_at:
            _conflict(
                'downtime_switch_before_start',
                'Время переключения причины раньше начала текущего простоя.',
            )
        if open_event and open_event.reason_id == reason.id:
            # Repeated delivery or a repeated tap on the active reason is a no-op:
            # it must not reset the interval's start time.
            event = open_event
            action = 'downtime_unchanged'
        else:
            if open_event:
                # One timestamp closes the previous category and opens the next one,
                # so the total downtime has neither a gap nor an overlap.
                open_event.ended_at = normalized['occurred_at']
                open_event.save(update_fields=['ended_at'])
            event = DowntimeEvent.objects.create(
                equipment=equipment,
                employee=access.employee,
                subject_employee=access.employee,
                recorded_by=access.employee,
                reason=reason,
                started_at=normalized['occurred_at'],
                comment=str(normalized['payload'].get('comment') or '')[:255],
                recorded_at=normalized['received_at'],
                ended_at=(
                    shift.closed_at
                    if shift.closed_at and shift.closed_at >= normalized['occurred_at']
                    else None
                ),
            )
            action = 'downtime_switched' if open_event else 'downtime_started'
    state = bump_operational_state(
        f'OfflineFieldEvent:{action}', event_type='downtime_changed',
        object_type='DowntimeEvent', object_id=event.id,
        payload={'action': action, 'event_id': event.id, 'equipment_id': equipment.id,
                 'employee_id': access.employee_id},
    )
    return {
        'server_ids': {'downtime_event_id': event.id, 'shift_id': shift.id},
        'version': state.version,
    }, {'downtime_event': event, 'shift': shift, 'equipment': equipment}


def _process_shift_closed(access, normalized, *, role_code):
    from shifts.services import (
        DriverShiftCloseConfirmationRequired,
        ExcavatorShiftError,
        ExcavatorShiftCloseConfirmationRequired,
        close_driver_shift,
        close_excavator_shift,
    )

    shift = _locked_shift(access, normalized, role_code=role_code)
    if shift.closed_at:
        _conflict('shift_already_closed', 'Смена уже закрыта другим действием.')
    payload = normalized['payload']
    try:
        if role_code == 'driver':
            shift, _ = close_driver_shift(
                shift=shift, employee=access.employee,
                readings={
                    'end_fuel': _optional_decimal(payload.get('end_fuel'), field='end_fuel'),
                    'end_mileage': _optional_decimal(payload.get('end_mileage'), field='end_mileage'),
                    'end_engine_hours': _optional_decimal(
                        payload.get('end_engine_hours'), field='end_engine_hours',
                    ),
                },
                client_action_id=normalized['event_id'],
                confirmation_token=str(payload.get('confirmation_token') or ''),
                occurred_at=normalized['occurred_at'],
                actor_access_id=access.pk,
            )
            result = {'ok': True, 'shift_id': shift.id}
        else:
            result = close_excavator_shift(
                employee=access.employee,
                fuel_value=payload.get('fuel') if payload.get('fuel') is not None else payload.get('end_fuel'),
                engine_hours_value=(payload.get('engine_hours') if payload.get('engine_hours') is not None
                                    else payload.get('end_engine_hours')),
                client_action_id=normalized['event_id'],
                submitted_fuel_percent=payload.get('fuel_percent'),
                confirmation_token=str(payload.get('confirmation_token') or ''),
                expected_shift_id=shift.id,
                occurred_at=normalized['occurred_at'],
            )
    except (DriverShiftCloseConfirmationRequired, ExcavatorShiftCloseConfirmationRequired) as error:
        _conflict(
            'confirmation_required',
            str(error),
            details={
                'confirmation_token': error.confirmation_token,
                'warnings': error.warnings,
            },
        )
    except ExcavatorShiftError as error:
        _conflict(
            getattr(error, 'code', 'shift_close_failed'),
            str(error),
            details={
                'field_errors': getattr(error, 'field_errors', {}),
                **getattr(error, 'extra', {}),
            },
        )
    except ValidationError as error:
        _conflict('shift_close_failed', '; '.join(error.messages))
    shift.refresh_from_db()
    return {
        'server_ids': {'shift_id': shift.id},
        'version': result.get('version') if isinstance(result, dict) else None,
    }, {'shift': shift, 'equipment': shift.equipment}


PROCESSORS = {
    'excavator.free_bucket.accepted': _process_free_bucket_accepted,
    'excavator.free_bucket.cancelled': _process_free_bucket_cancelled,
    'excavator.free_bucket.loaded': _process_free_bucket_loaded,
    'excavator.trip.loaded': _process_excavator_loaded,
    'excavator.trip.loaded.cancelled': _process_excavator_loaded_cancelled,
    'driver.trip.unloaded': _process_driver_unloaded,
    'driver.trip.dump_point_changed': _process_driver_dump_point_changed,
    'driver.trip.loaded': _process_driver_loaded,
    'driver.trip.loaded.cancelled': _process_driver_loaded_cancelled,
    'driver.free_bucket.selected': _process_driver_free_bucket_selected,
    'driver.free_bucket.cancelled': _process_driver_free_bucket_cancelled,
    'excavator.downtime.started': lambda access, event: _process_downtime(access, event, role_code='excavator_operator', close=False),
    'excavator.downtime.ended': lambda access, event: _process_downtime(access, event, role_code='excavator_operator', close=True),
    'driver.downtime.started': lambda access, event: _process_downtime(access, event, role_code='driver', close=False),
    'driver.downtime.ended': lambda access, event: _process_downtime(access, event, role_code='driver', close=True),
    'excavator.shift.closed': lambda access, event: _process_shift_closed(access, event, role_code='excavator_operator'),
    'driver.shift.closed': lambda access, event: _process_shift_closed(access, event, role_code='driver'),
}


def _legacy_action_result(access, normalized):
    """Import an already acknowledged legacy unload into the receipt table."""
    if normalized['event_type'] != 'driver.trip.unloaded':
        return None
    from trips.models import TripClientAction

    action = (
        TripClientAction.objects.select_for_update(of=('self',))
        .select_related('trip')
        .filter(action_type='trip_unloaded', client_action_id=normalized['event_id'])
        .first()
    )
    if not action:
        return None
    trip = action.trip
    requested_trip_id = _positive_int(normalized['trip_id'], field='trip_id')
    requested_shift_id = _positive_int(normalized['shift_id'], field='shift_id')
    if action.actor_id != access.employee_id or trip.id != requested_trip_id:
        _conflict(
            'legacy_action_mismatch',
            'Ранее принятое действие с этим ID относится к другому сотруднику или рейсу.',
        )
    if trip.unloading_shift_id and trip.unloading_shift_id != requested_shift_id:
        _conflict(
            'legacy_action_mismatch',
            'Ранее принятое действие с этим ID относится к другой смене.',
        )
    if trip.completed_at and abs((trip.completed_at - normalized['occurred_at']).total_seconds()) > 1:
        _conflict(
            'legacy_action_time_mismatch',
            'Время ранее принятой разгрузки не совпадает с сохранённым событием.',
        )
    return {
        'server_ids': {'trip_id': trip.id, 'shift_id': trip.unloading_shift_id},
        'legacy_action_imported': True,
    }, {'trip': trip, 'shift': trip.unloading_shift, 'equipment': trip.truck}


def _dependency_state(access, normalized):
    if not normalized['depends_on']:
        return
    dependencies = {
        item.event_id: item
        for item in OfflineFieldEvent.objects.select_for_update(of=('self',)).filter(
            event_id__in=normalized['depends_on']
        )
    }
    for dependency_id in normalized['depends_on']:
        dependency = dependencies.get(dependency_id)
        if not dependency:
            _retry('dependency_pending', 'Предыдущее событие ещё не получено сервером.')
        if (
            dependency.actor_id != access.employee_id
            or dependency.access_id != access.id
            or dependency.role_code != normalized['role_code']
            or dependency.device_id != normalized['device_id']
        ):
            _conflict('dependency_owner_mismatch', 'Зависимость принадлежит другому сотруднику или устройству.')
        if dependency.sequence >= normalized['sequence']:
            _conflict('dependency_order_invalid', 'Зависимость должна иметь меньший номер порядка.')
        if dependency.status != OfflineFieldEventStatus.ACCEPTED:
            if dependency.status == OfflineFieldEventStatus.RETRY:
                _retry('dependency_pending', 'Предыдущее событие ещё не принято.')
            _conflict('dependency_rejected', 'Предыдущее событие требует сверки или отклонено.')


def process_one_offline_event(access, normalized):
    try:
        with transaction.atomic():
            lock_idempotency_key('offline_field_event', normalized['event_id'])
            existing = OfflineFieldEvent.objects.select_for_update().filter(
                event_id=normalized['event_id']
            ).first()
            if existing:
                same_identity = (
                    existing.actor_id == access.employee_id
                    and existing.access_id == access.id
                    and existing.role_code == normalized['role_code']
                    and existing.device_id == normalized['device_id']
                    and existing.fingerprint == normalized['fingerprint']
                )
                if not same_identity:
                    _record_conflict_attempt(
                        existing=existing, access=access, normalized=normalized,
                        code='event_id_reused',
                    )
                    return _result(
                        normalized['event_id'], 'conflict', code='event_id_reused',
                        message='Идентификатор уже использован для другого события.',
                    )
                if existing.status != OfflineFieldEventStatus.RETRY:
                    return _stored_result(existing, deduplicated=True)
                receipt = existing
                receipt.status = OfflineFieldEventStatus.PROCESSING
                receipt.retryable = False
                receipt.error_code = ''
                receipt.error_message = ''
                receipt.save(update_fields=['status', 'retryable', 'error_code', 'error_message', 'updated_at'])
            else:
                sequence_collision = OfflineFieldEvent.objects.select_for_update().filter(
                    actor=access.employee,
                    role_code=normalized['role_code'],
                    device_id=normalized['device_id'],
                    sequence=normalized['sequence'],
                ).first()
                if sequence_collision:
                    _record_conflict_attempt(
                        existing=sequence_collision, access=access, normalized=normalized,
                        code='sequence_reused',
                    )
                    return _result(
                        normalized['event_id'], 'conflict', code='sequence_reused',
                        message='Номер порядка уже занят другим событием.',
                    )
                receipt = OfflineFieldEvent.objects.create(
                    event_id=normalized['event_id'],
                    event_type=normalized['event_type'],
                    format_version=normalized['format_version'],
                    actor=access.employee,
                    access=access,
                    role_code=normalized['role_code'],
                    device_id=normalized['device_id'],
                    sequence=normalized['sequence'],
                    depends_on=normalized['depends_on'],
                    occurred_at=normalized['occurred_at'],
                    received_at=normalized['received_at'],
                    shift_id=_positive_int(normalized['shift_id'], field='shift_id', required=False),
                    equipment_id=_positive_int(normalized['equipment_id'], field='equipment_id', required=False),
                    local_trip_id=normalized['local_trip_id'],
                    local_downtime_id=normalized['local_downtime_id'],
                    context_snapshot=normalized['context_snapshot'],
                    payload=normalized['payload'],
                    fingerprint=normalized['fingerprint'],
                )
            try:
                with transaction.atomic():
                    _validate_claimed_context(access, normalized)
                    if normalized['occurred_at'] > normalized['received_at'] + MAX_FUTURE_CLOCK_SKEW:
                        _conflict('device_clock_ahead', 'Часы устройства заметно опережают сервер. Требуется сверка.')
                    _dependency_state(access, normalized)
                    legacy_result = _legacy_action_result(access, normalized)
                    if legacy_result is not None:
                        result_payload, links = legacy_result
                    else:
                        result_payload, links = PROCESSORS[normalized['event_type']](access, normalized)
            except OfflineEventProblem as problem:
                receipt.status = problem.status
                receipt.retryable = problem.retryable
                receipt.error_code = problem.code
                receipt.error_message = problem.message
                receipt.result_payload = {
                    'server_received_at': receipt.received_at.isoformat(),
                    **problem.details,
                }
                receipt.save(update_fields=[
                    'status', 'retryable', 'error_code', 'error_message',
                    'result_payload', 'updated_at',
                ])
                _create_free_bucket_load_review(
                    access=access, normalized=normalized, receipt=receipt, problem=problem,
                )
                return _stored_result(receipt)
            except (IntegrityError, TimeoutError):
                receipt.status = OfflineFieldEventStatus.RETRY
                receipt.retryable = True
                receipt.error_code = 'concurrent_state_retry'
                receipt.error_message = 'Состояние изменилось одновременно. Событие будет повторено.'
                receipt.result_payload = {'server_received_at': receipt.received_at.isoformat()}
                receipt.save(update_fields=[
                    'status', 'retryable', 'error_code', 'error_message',
                    'result_payload', 'updated_at',
                ])
                return _stored_result(receipt)
            except Exception as error:
                logger.exception(
                    'Unexpected offline event processing failure: %s',
                    normalized['event_id'],
                )
                receipt.status = OfflineFieldEventStatus.RETRY
                receipt.retryable = True
                receipt.error_code = 'temporary_server_error'
                # Класс и первые символы ошибки уходят в ответ: с недебажного телефона
                # это единственный способ узнать, обо что споткнулся сервер
                # (боевой случай 20.09.2026 — 60 повторов одной отмены свободного
                # ковша без единого следа причины на стороне телефона).
                receipt.error_message = 'Временная ошибка сервера ({}: {}). Событие сохранено и будет повторено.'.format(
                    type(error).__name__, str(error)[:120].replace(chr(10), ' '),
                )
                receipt.result_payload = {'server_received_at': receipt.received_at.isoformat()}
                receipt.save(update_fields=[
                    'status', 'retryable', 'error_code', 'error_message',
                    'result_payload', 'updated_at',
                ])
                return _stored_result(receipt)
            receipt.status = OfflineFieldEventStatus.ACCEPTED
            receipt.retryable = False
            receipt.error_code = ''
            receipt.error_message = ''
            result_payload = dict(result_payload or {})
            result_payload.setdefault('server_received_at', receipt.received_at.isoformat())
            result_payload.setdefault('server_ids', {})['event_receipt_id'] = receipt.id
            receipt.result_payload = result_payload
            for field in ('trip', 'shift', 'equipment', 'downtime_event'):
                if links.get(field) is not None:
                    setattr(receipt, field, links[field])
            receipt.save(update_fields=[
                'status', 'retryable', 'error_code', 'error_message', 'result_payload',
                'trip', 'shift', 'equipment', 'downtime_event', 'updated_at',
            ])
            return _stored_result(receipt)
    except IntegrityError:
        existing = OfflineFieldEvent.objects.filter(event_id=normalized['event_id']).first()
        if existing:
            return _stored_result(existing, deduplicated=(existing.status == OfflineFieldEventStatus.ACCEPTED))
        return _result(
            normalized['event_id'], 'retry', retryable=True,
            code='concurrent_receipt_retry',
            message='Параллельная синхронизация. Повторите позже.',
        )


def process_offline_batch(access, *, role_code, device_id, events):
    if role_code != access.role.code or role_code not in {'driver', 'excavator_operator'}:
        return [_result('', 'auth_required', code='role_session_changed', message='Активная роль изменилась.')]
    device_id = _clean_identifier(device_id, field='device_id', pattern=DEVICE_ID_RE)
    if not isinstance(events, list) or not events or len(events) > MAX_BATCH_SIZE:
        _invalid('invalid_batch_size', f'В пакете должно быть от 1 до {MAX_BATCH_SIZE} событий.')
    received_at = timezone.now()
    normalized_events = []
    immediate_results = []
    for index, raw_event in enumerate(events):
        try:
            normalized_events.append((index, normalize_offline_event(
                raw_event, role_code=role_code, device_id=device_id, received_at=received_at,
            )))
        except OfflineEventProblem as problem:
            event_id = str(raw_event.get('event_id') or '') if isinstance(raw_event, dict) else ''
            _record_invalid_attempt(
                access=access,
                role_code=role_code,
                device_id=device_id,
                raw_event=raw_event,
                code=problem.code,
            )
            immediate_results.append((index, _result(
                event_id, 'invalid', code=problem.code, message=problem.message,
            )))
    processed_by_index = {}
    for index, normalized in sorted(
        normalized_events,
        key=lambda item: (item[1]['sequence'], item[1]['event_id'], item[0]),
    ):
        processed_by_index[index] = process_one_offline_event(access, normalized)
    results = []
    immediate_by_index = dict(immediate_results)
    for index, raw_event in enumerate(events):
        if index in immediate_by_index:
            results.append(immediate_by_index[index])
        else:
            results.append(processed_by_index[index])
    return results
