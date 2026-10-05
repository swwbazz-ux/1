"""Read-only, paged proof of a closed device shift and its loading projection."""
import hashlib
import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from core.models import OfflineFieldEvent
from core.offline_replay import lock_stream, restore_input
from core.offline_sync import DEVICE_ID_RE, EVENT_ID_RE
from shifts.models import EmployeeShift
from trips.excavator_hourly_report import fleet_code_for_truck
from trips.models import TripStatus
from users.active_role import role_session_state
from users.models import EmployeeAccess


PAGE_SIZE = 100
MAX_ARCHIVE_EVENTS = 10000
LOAD_TYPES = {'excavator.trip.loaded', 'excavator.free_bucket.loaded'}


class ArchiveUnavailable(Exception):
    pass


def _load_projection(receipt, shift):
    if receipt.event_type not in LOAD_TYPES:
        return None
    trip = receipt.trip
    # Loading receipts link equipment to the truck; the shift owns the excavator.
    if (not trip or not trip.loaded_at or trip.excavator_id != shift.equipment_id
            or (receipt.result_payload.get('server_ids') or {}).get('trip_id') != trip.pk):
        raise ArchiveUnavailable('loading_projection_missing')
    point = trip.assigned_dump_point
    return {
        'event_id': receipt.event_id, 'trip_id': trip.pk,
        'occurred_at': trip.loaded_at.isoformat(),
        'cancelled': trip.status == TripStatus.CANCELLED,
        'fleet_code': fleet_code_for_truck(trip.truck),
        'dump_point_id': point.pk if point else None,
        'dump_point': point.name if point else 'Точка не определена',
        'volume_m3': str(trip.volume_m3) if trip.volume_m3 is not None else None,
    }


@transaction.atomic
def build_archive_page(access, *, device_id, local_shift_id, close_event_id, offset=0, snapshot_id=''):
    lock_stream(access, {'role_code': 'excavator_operator', 'device_id': device_id})
    stream = OfflineFieldEvent.objects.filter(
        actor_id=access.employee_id, access_id=access.pk,
        role_code='excavator_operator', device_id=device_id,
    )
    opening = stream.filter(event_id=local_shift_id, event_type='excavator.shift.opened', status='accepted').first()
    if not opening or not opening.shift_id:
        raise ArchiveUnavailable('opening_not_covered')
    shift = EmployeeShift.objects.select_for_update(of=('self',)).filter(
        pk=opening.shift_id, employee_id=access.employee_id, workplace_code='excavator_operator',
        closed_at__isnull=False,
    ).first()
    if not shift:
        raise ArchiveUnavailable('closed_shift_required')
    # Include unresolved inputs too: their application FK is intentionally null.
    receipts = list(stream.filter(
        Q(shift_id=shift.pk) | Q(input_envelope__normalized__local_shift_id=local_shift_id),
    ).select_related('trip__truck__equipment_type', 'trip__truck__model', 'trip__assigned_dump_point')
        .order_by('sequence', 'pk')[:MAX_ARCHIVE_EVENTS + 1])
    if len(receipts) > MAX_ARCHIVE_EVENTS or not receipts or any(item.status != 'accepted' for item in receipts):
        raise ArchiveUnavailable('archive_incomplete')
    if any(not isinstance(item.input_envelope, dict) for item in receipts):
        raise ArchiveUnavailable('archive_source_invalid')
    if not any(item.event_id == close_event_id and item.event_type == 'excavator.shift.closed' for item in receipts):
        raise ArchiveUnavailable('closing_not_covered')
    if offset < 0 or offset >= len(receipts) or offset % PAGE_SIZE:
        raise ArchiveUnavailable('invalid_archive_offset')

    facts = {item.event_id: _load_projection(item, shift) for item in receipts if item.event_type in LOAD_TYPES}
    shift_snapshot = {
        'local_shift_id': local_shift_id, 'open_event_id': local_shift_id, 'close_event_id': close_event_id,
        'server_shift_id': shift.pk, 'equipment_id': shift.equipment_id,
        'opened_at': shift.opened_at.isoformat(), 'closed_at': shift.closed_at.isoformat(),
        'readings': {key: str(getattr(shift, key)) if getattr(shift, key) is not None else None
                     for key in ('start_fuel', 'end_fuel', 'start_engine_hours', 'end_engine_hours')},
    }
    manifest = [[item.event_id, item.sequence, item.fingerprint, item.input_envelope.get('checksum'),
                 item.shift_id, item.equipment_id, item.trip_id, item.downtime_event_id, item.result_payload]
                for item in receipts]
    token = hashlib.sha256(json.dumps([shift_snapshot, manifest, facts], sort_keys=True,
                                      ensure_ascii=False, default=str).encode()).hexdigest()
    if snapshot_id and token != snapshot_id:
        raise ArchiveUnavailable('archive_snapshot_changed')
    if offset and not snapshot_id:
        raise ArchiveUnavailable('archive_snapshot_required')

    entries = []
    for receipt in receipts[offset:offset + PAGE_SIZE]:
        try:
            original = restore_input(receipt)
        except (ValueError, TypeError, KeyError):
            raise ArchiveUnavailable('archive_source_invalid')
        if original.get('local_shift_id') != local_shift_id or receipt.shift_id != shift.pk:
            raise ArchiveUnavailable('archive_shift_mismatch')
        entries.append({
            'event': receipt.input_envelope['raw_event'], 'fingerprint': receipt.fingerprint,
            'receipt_id': receipt.pk, 'status': 'accepted', 'result': receipt.result_payload,
            'load_fact': facts.get(receipt.event_id),
        })
    # One trip can have multiple load action IDs; totals count the domain trip once.
    unique = {fact['trip_id']: fact for fact in facts.values()}
    active = [fact for fact in unique.values() if not fact['cancelled']]
    next_offset = offset + len(entries)
    return {
        'ok': True, 'schema_version': 1, 'snapshot_id': token,
        'generated_at': timezone.now().isoformat(),
        'identity': {'actor_id': access.employee_id, 'access_id': access.pk,
                     'role_code': 'excavator_operator', 'device_id': device_id},
        'shift': shift_snapshot, 'event_count': len(receipts), 'offset': offset,
        'next_offset': next_offset if next_offset < len(receipts) else None,
        'entries': entries,
        'projection': {
            'source_event_ids': list(facts), 'source_trip_ids': sorted(unique),
            'trip_count': len(active), 'cancelled_trip_count': len(unique) - len(active),
            'volume_m3': str(sum((Decimal(fact['volume_m3'] or '0') for fact in active), Decimal('0'))),
            'unknown_volume_trip_count': sum(fact['volume_m3'] is None for fact in active),
        },
    }


@require_GET
def excavator_shift_archive_view(request):
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(
        pk=request.session.get('employee_access_id'), role__code='excavator_operator',
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
            if not DEVICE_ID_RE.fullmatch(device) or not EVENT_ID_RE.fullmatch(opening) or not EVENT_ID_RE.fullmatch(closing):
                raise ValueError
        except (ValueError, TypeError):
            response = JsonResponse({'ok': False, 'code': 'invalid_archive_request'}, status=400)
        else:
            try:
                payload = build_archive_page(access, device_id=device, local_shift_id=opening,
                                             close_event_id=closing, offset=offset,
                                             snapshot_id=request.GET.get('snapshot_id', ''))
                response = JsonResponse(payload)
            except ArchiveUnavailable as error:
                response = JsonResponse({'ok': False, 'code': str(error)}, status=409)
    response['Cache-Control'] = 'no-store'
    return response
