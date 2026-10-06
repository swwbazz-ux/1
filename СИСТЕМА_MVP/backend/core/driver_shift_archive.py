"""Проверяемый архив собственных действий водителя в закрытой смене."""
import hashlib
import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from core.excavator_shift_archive import ArchiveUnavailable, MAX_ARCHIVE_EVENTS, PAGE_SIZE
from core.models import OfflineFieldEvent
from core.offline_replay import lock_stream, restore_input
from core.offline_sync import DEVICE_ID_RE, EVENT_ID_RE
from shifts.models import EmployeeShift
from reports.driver_shift_timeline import _carryover_loading_driver_shift
from trips.models import TripStatus
from users.active_role import role_session_state
from users.models import EmployeeAccess


def _iso(value):
    return value.isoformat() if value else None


def _trip_fact(receipt, shift):
    if not receipt.event_type.startswith('driver.trip.'):
        return None
    trip = receipt.trip
    if (not trip or not trip.loaded_at or trip.truck_id != shift.equipment_id
            or (receipt.result_payload.get('server_ids') or {}).get('trip_id') != trip.pk):
        raise ArchiveUnavailable('trip_projection_missing')
    point = trip.actual_dump_point or trip.dump_point or trip.assigned_dump_point
    # Participation, unloading and accrual are separate facts for D1 -> D2.
    original_driver_shift = _carryover_loading_driver_shift(trip)
    credited_shift_id = original_driver_shift.pk if original_driver_shift else trip.unloading_shift_id
    return {
        'trip_id': trip.pk, 'truck_id': trip.truck_id, 'status': trip.status,
        'loaded_at': _iso(trip.loaded_at), 'completed_at': _iso(trip.completed_at),
        'cancelled_at': _iso(trip.cancelled_at), 'is_carryover': trip.is_carryover,
        'driver_control_shift_id': trip.driver_control_shift_id,
        'unloading_shift_id': trip.unloading_shift_id, 'credited_shift_id': credited_shift_id,
        'excavator_id': trip.excavator_id, 'excavator': str(trip.excavator.garage_number or ''),
        'dump_point_id': point.pk if point else None, 'dump_point': str(point) if point else '—',
        'volume_m3': str(trip.volume_m3) if trip.volume_m3 is not None else None,
        'load_time_source': trip.load_time_source, 'unload_time_source': trip.unload_time_source,
    }


def _downtime_fact(receipt, shift):
    if not receipt.event_type.startswith('driver.downtime.'):
        return None
    event = receipt.downtime_event
    ids = receipt.result_payload.get('server_ids') or {}
    if (not event or event.equipment_id != shift.equipment_id
            or (ids.get('downtime_event_id') or ids.get('downtime_id')) != event.pk):
        raise ArchiveUnavailable('downtime_projection_missing')
    start = max(event.started_at, shift.opened_at)
    end = min(event.ended_at or shift.closed_at, shift.closed_at)
    return {
        'downtime_id': event.pk, 'equipment_id': event.equipment_id,
        'employee_id': event.employee_id, 'reason_id': event.reason_id,
        'reason': event.reason.button_label, 'started_at': _iso(event.started_at),
        'ended_at': _iso(event.ended_at), 'shift_seconds': max(0, int((end - start).total_seconds())),
    }


@transaction.atomic
def build_archive_page(access, *, device_id, close_event_id, local_shift_id='', server_shift_id=None,
                       offset=0, snapshot_id=''):
    lock_stream(access, {'role_code': 'driver', 'device_id': device_id})
    stream = OfflineFieldEvent.objects.filter(
        actor_id=access.employee_id, access_id=access.pk, role_code='driver', device_id=device_id,
    )
    closing = stream.filter(event_id=close_event_id, event_type='driver.shift.closed', status='accepted').first()
    if not closing or not closing.shift_id:
        raise ArchiveUnavailable('closing_not_covered')
    shift = EmployeeShift.objects.select_for_update(of=('self',)).filter(
        pk=closing.shift_id, employee_id=access.employee_id, workplace_code__in=['driver', ''], closed_at__isnull=False,
    ).first()
    if not shift:
        raise ArchiveUnavailable('closed_shift_required')
    if server_shift_id and server_shift_id != shift.pk:
        raise ArchiveUnavailable('archive_shift_mismatch')
    openings = stream.filter(event_type='driver.shift.opened', shift=shift, status='accepted')
    if local_shift_id:
        opening = openings.filter(event_id=local_shift_id).first()
        if not opening:
            raise ArchiveUnavailable('opening_not_covered')
    else:
        opening = openings.order_by('sequence', 'pk').first()
        local_shift_id = opening.event_id if opening else ''
    # After opening ACK later originals use the numeric shift ID. Pending
    # originals have no application FK yet and must not disappear from coverage.
    scope = Q(shift_id=shift.pk) | Q(input_envelope__normalized__shift_id=shift.pk)
    if local_shift_id:
        scope |= Q(input_envelope__normalized__payload__local_shift_id=local_shift_id)
    receipts = list(stream.filter(scope).select_related(
        'trip__excavator', 'trip__dump_point', 'trip__actual_dump_point', 'trip__assigned_dump_point',
        'trip__driver_control_shift', 'trip__unloading_shift',
        'downtime_event__reason',
    ).order_by('sequence', 'pk')[:MAX_ARCHIVE_EVENTS + 1])
    if not receipts or len(receipts) > MAX_ARCHIVE_EVENTS or any(item.status != 'accepted' for item in receipts):
        raise ArchiveUnavailable('archive_incomplete')
    if offset < 0 or offset >= len(receipts) or offset % PAGE_SIZE:
        raise ArchiveUnavailable('invalid_archive_offset')
    entries = []
    for receipt in receipts:
        try:
            original = restore_input(receipt)
        except (ValueError, TypeError, KeyError):
            raise ArchiveUnavailable('archive_source_invalid')
        original_local_id = (original.get('payload') or {}).get('local_shift_id')
        if (receipt.shift_id != shift.pk or not (
                original.get('shift_id') == shift.pk
                or (local_shift_id and original_local_id == local_shift_id))):
            raise ArchiveUnavailable('archive_shift_mismatch')
        if (not isinstance(receipt.result_payload, dict)
                or (receipt.result_payload.get('server_ids') or {}).get('event_receipt_id') != receipt.pk):
            raise ArchiveUnavailable('archive_receipt_invalid')
        entries.append({
            'event': receipt.input_envelope['raw_event'], 'receipt_id': receipt.pk,
            'fingerprint': receipt.fingerprint, 'status': 'accepted', 'result': receipt.result_payload,
            'trip_fact': _trip_fact(receipt, shift), 'downtime_fact': _downtime_fact(receipt, shift),
        })
    trips = {entry['trip_fact']['trip_id']: entry['trip_fact'] for entry in entries if entry['trip_fact']}
    downtimes = {entry['downtime_fact']['downtime_id']: entry['downtime_fact'] for entry in entries if entry['downtime_fact']}
    credited = [fact for fact in trips.values()
                if fact['status'] == TripStatus.COMPLETED and fact['credited_shift_id'] == shift.pk]
    shift_snapshot = {
        'local_shift_id': local_shift_id, 'open_event_id': opening.event_id if opening else '',
        'close_event_id': close_event_id, 'server_shift_id': shift.pk, 'equipment_id': shift.equipment_id,
        'opened_at': _iso(shift.opened_at), 'closed_at': _iso(shift.closed_at),
        'readings': {key: str(getattr(shift, key)) if getattr(shift, key) is not None else None
                     for key in ('start_fuel', 'end_fuel', 'start_mileage', 'end_mileage',
                                 'start_engine_hours', 'end_engine_hours')},
    }
    # Totals cover the domain facts referenced by this device's own originals,
    # not unrelated trips recorded by another device or another employee.
    projection = {
        'source_event_ids': [entry['event']['event_id'] for entry in entries],
        'source_trip_ids': sorted(trips), 'source_downtime_ids': sorted(downtimes),
        'completed_trip_count': sum(fact['status'] == TripStatus.COMPLETED for fact in trips.values()),
        'cancelled_trip_count': sum(fact['status'] == TripStatus.CANCELLED for fact in trips.values()),
        'credited_trip_count': len(credited),
        'credited_volume_m3': str(sum((Decimal(fact['volume_m3'] or '0') for fact in credited), Decimal('0'))),
        'unknown_volume_trip_count': sum(fact['volume_m3'] is None for fact in credited),
        'downtime_seconds': sum(fact['shift_seconds'] for fact in downtimes.values()),
    }
    token = hashlib.sha256(json.dumps([shift_snapshot, entries, projection], sort_keys=True,
                                      ensure_ascii=False, default=str).encode()).hexdigest()
    if snapshot_id and token != snapshot_id:
        raise ArchiveUnavailable('archive_snapshot_changed')
    if offset and not snapshot_id:
        raise ArchiveUnavailable('archive_snapshot_required')
    end = min(offset + PAGE_SIZE, len(entries))
    return {
        'ok': True, 'schema_version': 1, 'snapshot_id': token, 'generated_at': timezone.now().isoformat(),
        'identity': {'actor_id': access.employee_id, 'access_id': access.pk, 'role_code': 'driver', 'device_id': device_id},
        'shift': shift_snapshot, 'projection': projection, 'event_count': len(entries), 'offset': offset,
        'next_offset': end if end < len(entries) else None, 'entries': entries[offset:end],
    }


@require_GET
def driver_shift_archive_view(request):
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(
        pk=request.session.get('employee_access_id'), role__code='driver',
    ).first()
    state = role_session_state(request, access)
    if not access or not state.get('authenticated') or not state.get('is_active'):
        response = JsonResponse({'ok': False, 'code': 'authentication_required'}, status=403)
    else:
        device = request.GET.get('device_id', '')
        opening = request.GET.get('local_shift_id', '')
        closing = request.GET.get('close_event_id', '')
        try:
            offset = int(request.GET.get('offset', '0'))
            shift_id = int(request.GET.get('server_shift_id', '0'))
            if (not DEVICE_ID_RE.fullmatch(device) or not EVENT_ID_RE.fullmatch(closing)
                    or (opening and not EVENT_ID_RE.fullmatch(opening)) or shift_id < 0):
                raise ValueError
        except (ValueError, TypeError):
            response = JsonResponse({'ok': False, 'code': 'invalid_archive_request'}, status=400)
        else:
            try:
                response = JsonResponse(build_archive_page(
                    access, device_id=device, local_shift_id=opening, close_event_id=closing,
                    server_shift_id=shift_id or None, offset=offset, snapshot_id=request.GET.get('snapshot_id', ''),
                ))
            except ArchiveUnavailable as error:
                response = JsonResponse({'ok': False, 'code': str(error)}, status=409)
    response['Cache-Control'] = 'no-store'
    return response
