"""Immutable offline inputs and bounded replay, using the original authority.

No receipt lease is necessary: application is DB-only and the stream advisory
lock and each receipt/effect transaction serialize PostgreSQL workers.
"""

import copy
import hashlib
import json
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.db_locks import lock_idempotency_key
from core.models import OfflineFieldEvent, OfflineFieldEventStatus


MAX_REPLAY = 50
MAX_WAKE_SCAN = 250
DATE_KEYS = ('occurred_at', 'device_occurred_at', 'received_at')


def snapshot_input(normalized, raw_event):
    # Do this before processors bind local IDs or adjust execution context.
    normalized['_input_snapshot'] = copy.deepcopy(normalized)
    normalized['_raw_event'] = copy.deepcopy(raw_event)


def build_input_envelope(access, normalized, *, legacy=False):
    envelope = json.loads(json.dumps({
        'schema': 1,
        'source': 'legacy_resubmission' if legacy else 'first_receipt',
        'authority': {'actor_id': access.employee_id, 'access_id': access.pk,
                      'role_code': normalized['role_code'], 'device_id': normalized['device_id']},
        'raw_event': normalized['_raw_event'],
        'normalized': normalized['_input_snapshot'],
    }, ensure_ascii=False, default=lambda value: value.isoformat()))
    envelope['checksum'] = _envelope_checksum(envelope)
    return envelope


def _envelope_checksum(envelope):
    return hashlib.sha256(json.dumps(
        {key: value for key, value in envelope.items() if key != 'checksum'},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode()).hexdigest()


def same_identity(receipt, access, normalized):
    return (receipt.actor_id == access.employee_id and receipt.access_id == access.pk
            and receipt.role_code == normalized['role_code']
            and receipt.device_id == normalized['device_id']
            and receipt.fingerprint == normalized['fingerprint'])


def restore_input(receipt):
    envelope = receipt.input_envelope
    if (not isinstance(envelope, dict) or envelope.get('schema') != 1
            or envelope.get('checksum') != _envelope_checksum(envelope)
            or not isinstance(envelope.get('raw_event'), dict)
            or not isinstance(envelope.get('normalized'), dict)):
        raise ValueError('Offline receipt envelope format/checksum invalid')
    authority = envelope.get('authority', {})
    normalized = copy.deepcopy(envelope.get('normalized', {}))
    required = {
        'event_id', 'event_type', 'format_version', 'role_code', 'device_id',
        'sequence', 'depends_on', 'occurred_at', 'device_occurred_at', 'received_at',
        'shift_id', 'equipment_id', 'trip_id', 'local_trip_id', 'local_downtime_id',
        'payload', 'context_snapshot', 'fingerprint', 'clock_adjusted',
    }
    if (not required.issubset(normalized)
            or not isinstance(normalized['payload'], dict)
            or not isinstance(normalized['context_snapshot'], dict)
            or not isinstance(normalized['depends_on'], list)
            or not all(isinstance(item, str) for item in normalized['depends_on'])):
        raise ValueError('Offline receipt normalized input invalid')
    if (envelope.get('schema') != 1 or authority != {
            'actor_id': receipt.actor_id, 'access_id': receipt.access_id,
            'role_code': receipt.role_code, 'device_id': receipt.device_id,
        } or normalized.get('event_id') != receipt.event_id
            or normalized.get('fingerprint') != receipt.fingerprint
            or normalized.get('sequence') != receipt.sequence):
        raise ValueError('Offline receipt envelope identity mismatch')
    for key in DATE_KEYS:
        normalized[key] = parse_datetime(normalized[key])
        if normalized[key] is None or timezone.is_naive(normalized[key]):
            raise ValueError('Offline receipt envelope timestamp invalid')
    if (normalized['received_at'] != receipt.received_at
            or normalized['device_occurred_at'] != receipt.occurred_at):
        raise ValueError('Offline receipt first receipt time changed')
    snapshot_input(normalized, envelope['raw_event'])
    return normalized


def lock_stream(access, normalized):
    # Match the unique sequence constraint: an access rotation must not create a
    # second independent lock for the same employee/role/device sequence space.
    lock_idempotency_key('offline_device_stream', json.dumps([
        access.employee_id, normalized['role_code'], normalized['device_id'],
    ], separators=(',', ':')))


def schedule_retry(receipt):
    if receipt.status == OfflineFieldEventStatus.RETRY:
        receipt.retry_attempts += 1
        delay = min(3600, 5 * 2 ** min(receipt.retry_attempts, 10))
        receipt.next_retry_at = timezone.now() + timedelta(seconds=delay)
    else:
        receipt.next_retry_at = None
    receipt.save(update_fields=['retry_attempts', 'next_retry_at'])


def _original_access(receipt):
    from users.models import EmployeeAccess

    return EmployeeAccess.objects.select_related('employee', 'role').filter(
        pk=receipt.access_id, employee_id=receipt.actor_id, role__code=receipt.role_code,
        is_active=True, status=EmployeeAccess.Status.ACTIVATED, employee__is_active=True,
    ).first()


def _blocked_access(receipt):
    # The saved event survives revocation, but a worker cannot invent a new
    # access/session or apply it using a replacement employee's authority.
    receipt.error_code = 'original_access_unavailable'
    receipt.error_message = 'Исходный доступ отключён. Событие сохранено.'
    receipt.retry_attempts += 1
    receipt.next_retry_at = timezone.now() + timedelta(hours=1)
    receipt.save(update_fields=['error_code', 'error_message', 'retry_attempts', 'next_retry_at', 'updated_at'])


def mark_invalid_saved_input(receipt):
    receipt.status = OfflineFieldEventStatus.CONFLICT
    receipt.retryable = False
    receipt.error_code = 'saved_envelope_invalid'
    receipt.error_message = 'Сохранённый исходный конверт требует сверки.'
    receipt.next_retry_at = None
    receipt.save(update_fields=['status', 'retryable', 'error_code', 'error_message', 'next_retry_at', 'updated_at'])


def _references(normalized):
    payload = normalized.get('payload') or {}
    context = normalized.get('context_snapshot') or {}
    refs = set(normalized.get('depends_on') or [])
    for field in ('local_trip_id', 'local_downtime_id', 'local_shift_id',
                  'expected_open_trip_local_id', 'free_bucket_acceptance_local_id',
                  'free_bucket_acceptance_id'):
        for source in (normalized, payload, context):
            if source.get(field):
                refs.add(str(source[field]))
    return refs


def resume_dependents(access, parent, *, limit=MAX_REPLAY):
    """Wake only this parent's descendants; one bounded pass, no recursion.

    The caller already owns the stream lock. Candidates are read in sequence
    order; an accepted child can wake a later child during the same pass. Other
    retry reasons keep their backoff. A due worker drains beyond the scan cap.
    """
    from core.offline_sync import process_one_offline_event

    roots = {parent['event_id'], str(parent.get('local_trip_id') or ''),
             str(parent.get('local_downtime_id') or '')} - {''}
    processed = 0
    candidates = list(OfflineFieldEvent.objects.filter(
        actor_id=access.employee_id, access_id=access.pk, role_code=parent['role_code'],
        device_id=parent['device_id'], status=OfflineFieldEventStatus.RETRY,
        input_envelope__source='first_receipt',
    ).order_by('sequence', 'pk')[:MAX_WAKE_SCAN])
    for receipt in candidates:
        if processed >= min(limit, MAX_REPLAY):
            break
        try:
            normalized = restore_input(receipt)
        except (ValueError, KeyError, TypeError):
            mark_invalid_saved_input(receipt)
            continue
        if not (_references(normalized) & roots):
            continue
        original_access = _original_access(receipt)
        if original_access is None:
            _blocked_access(receipt)
            continue
        result = process_one_offline_event(original_access, normalized, _resume=False)
        processed += 1
        if result['status'] in {'accepted', 'deduplicated'}:
            roots.update({receipt.event_id, receipt.local_trip_id, receipt.local_downtime_id} - {''})
    return processed


def run_offline_replay(*, limit=MAX_REPLAY):
    """Resume durable due receipts after restart without a phone resubmission."""
    from core.offline_sync import process_one_offline_event

    now = timezone.now()
    # Each selected ID is visited once. Failed/missing parents move into backoff
    # rather than consuming the entire batch repeatedly.
    ids = list(OfflineFieldEvent.objects.filter(
        status=OfflineFieldEventStatus.RETRY, next_retry_at__lte=now,
        input_envelope__source='first_receipt',
    ).order_by('next_retry_at', 'sequence', 'pk')
               .values_list('pk', flat=True)[:max(0, min(int(limit), MAX_REPLAY))])
    result = {'processed': 0, 'accepted': 0}
    for receipt_id in ids:
        receipt = OfflineFieldEvent.objects.get(pk=receipt_id)
        # Never take a receipt lock before the stream lock (same ordering as HTTP).
        with transaction.atomic():
            lock_idempotency_key('offline_device_stream', json.dumps([
                receipt.actor_id, receipt.role_code, receipt.device_id,
            ], separators=(',', ':')))
            lock_idempotency_key('offline_field_event', receipt.event_id)
            receipt = OfflineFieldEvent.objects.select_for_update().get(pk=receipt_id)
            if (receipt.status != OfflineFieldEventStatus.RETRY or receipt.next_retry_at is None
                    or receipt.next_retry_at > now):
                continue
            result['processed'] += 1
            access = _original_access(receipt)
            if access is None:
                _blocked_access(receipt)
                continue
            try:
                normalized = restore_input(receipt)
            except (ValueError, KeyError, TypeError):
                mark_invalid_saved_input(receipt)
                continue
            outcome = process_one_offline_event(access, normalized, _resume=False)
            result['accepted'] += int(outcome['status'] in {'accepted', 'deduplicated'})
    return result
