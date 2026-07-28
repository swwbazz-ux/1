import json

from assignments.models import AssignmentStatus, EquipmentAssignment, HaulAssignment
from shifts.models import EmployeeShift


PRODUCTION_EVENT_TYPES = {
    'equipment_changed',
    'assignment_changed',
    'personnel_assignment_changed',
    'shift_changed',
    'reference_changed',
    'downtime_changed',
    'trip_changed',
    'driver_shift_opened',
    'driver_shift_closed',
    'shift_readings_corrected',
    'excavator_shift_opened',
    'excavator_shift_closed',
    'excavator_shift_readings_corrected',
    'test_shift_data_reset',
}
ACCESS_EVENT_TYPES = {
    'active_role_changed',
    'access_changed',
    'employee_changed',
}
GLOBAL_PRODUCTION_ROLES = {'dispatcher', 'mining_master'}
MANAGEMENT_EVENT_TYPES = PRODUCTION_EVENT_TYPES | {'personnel_changed'}
ADMIN_EVENT_TYPES = {
    'equipment_changed',
    'assignment_changed',
    'personnel_assignment_changed',
    'reference_changed',
    'employee_changed',
    'access_changed',
    'personnel_changed',
    'work_assignment_changed',
    'test_shift_data_reset',
}
MAX_EVENT_SCAN = 1000


def _as_int_set(value):
    if value is None or value == '':
        return set()
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    result = set()
    for item in value:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _payload_ids(payload, singular, plural):
    return _as_int_set(payload.get(singular)) | _as_int_set(payload.get(plural))


def _event_equipment_ids(payload):
    return (
        _payload_ids(payload, 'equipment_id', 'equipment_ids')
        | _payload_ids(payload, 'truck_id', 'truck_ids')
        | _payload_ids(payload, 'excavator_id', 'excavator_ids')
    )


def _event_employee_ids(payload):
    return _payload_ids(payload, 'employee_id', 'employee_ids')


def _event_access_ids(payload):
    return (
        _payload_ids(payload, 'access_id', 'access_ids')
        | _payload_ids(payload, 'active_access_id', 'active_access_ids')
    )


def _event_role_codes(payload):
    values = []
    for key in ('role_code', 'active_role_code'):
        value = str(payload.get(key) or '').strip()
        if value:
            values.append(value)
    role_codes = payload.get('role_codes')
    if isinstance(role_codes, (list, tuple, set)):
        values.extend(str(value).strip() for value in role_codes if str(value).strip())
    return set(values)


def _worker_equipment_ids(access):
    role_code = access.role.code
    equipment_ids = set(
        EmployeeShift.objects.filter(
            employee_id=access.employee_id,
            closed_at__isnull=True,
            equipment_id__isnull=False,
            workplace_code=role_code,
        ).values_list('equipment_id', flat=True)
    )
    equipment_ids.update(
        EquipmentAssignment.objects.filter(
            employee_id=access.employee_id,
            role_id=access.role_id,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        ).values_list('equipment_id', flat=True)
    )
    if role_code == 'driver':
        equipment_ids.update(
            HaulAssignment.objects.filter(
                truck_id__in=equipment_ids,
                ended_at__isnull=True,
            )
            .exclude(status=AssignmentStatus.CANCELLED)
            .values_list('excavator_id', flat=True)
        )
    elif role_code == 'excavator_operator':
        equipment_ids.update(
            HaulAssignment.objects.filter(
                excavator_id__in=equipment_ids,
                ended_at__isnull=True,
            )
            .exclude(status=AssignmentStatus.CANCELLED)
            .values_list('truck_id', flat=True)
        )
    return equipment_ids


def _personal_access_event(event_type, payload, access):
    employee_ids = _event_employee_ids(payload)
    access_ids = _event_access_ids(payload)
    role_codes = _event_role_codes(payload)
    if access.employee_id in employee_ids or access.id in access_ids:
        if (
            event_type == 'personnel_assignment_changed'
            and role_codes
            and access.role.code not in role_codes
        ):
            return False
        return True
    if (
        event_type == 'access_changed'
        and not employee_ids
        and not access_ids
        and role_codes
        and access.role.code in role_codes
    ):
        return True
    return False


def event_is_relevant(event, access, *, equipment_ids=None):
    payload = event.payload if isinstance(event.payload, dict) else {}
    event_type = event.event_type
    role_code = access.role.code

    if _personal_access_event(event_type, payload, access):
        return True
    if event_type == 'active_role_changed':
        return False
    if role_code in GLOBAL_PRODUCTION_ROLES:
        return event_type in PRODUCTION_EVENT_TYPES | ACCESS_EVENT_TYPES
    if role_code == 'manager':
        return event_type in MANAGEMENT_EVENT_TYPES
    if role_code == 'admin':
        return event_type in ADMIN_EVENT_TYPES
    if role_code not in {'driver', 'excavator_operator'}:
        return False
    if event_type == 'reference_changed':
        return True

    event_role_codes = _event_role_codes(payload)
    event_employee_ids = _event_employee_ids(payload)
    event_access_ids = _event_access_ids(payload)
    event_equipment_ids = _event_equipment_ids(payload)
    has_declared_entity_scope = any(
        key in payload
        for key in (
            'employee_id',
            'employee_ids',
            'access_id',
            'access_ids',
            'active_access_id',
            'active_access_ids',
            'equipment_id',
            'equipment_ids',
            'truck_id',
            'truck_ids',
            'excavator_id',
            'excavator_ids',
        )
    )
    if (
        event_type == 'personnel_assignment_changed'
        and event_role_codes
        and role_code not in event_role_codes
    ):
        return False
    if (
        event_type == 'personnel_assignment_changed'
        and payload.get('action') == 'crew_plan_published'
        and any(key in payload for key in ('employee_id', 'employee_ids'))
    ):
        return access.employee_id in event_employee_ids
    if access.employee_id in event_employee_ids:
        return True
    equipment_ids = equipment_ids if equipment_ids is not None else _worker_equipment_ids(access)
    if equipment_ids & event_equipment_ids:
        return True
    has_narrow_scope = bool(event_employee_ids or event_access_ids or event_equipment_ids)
    if (
        not has_narrow_scope
        and not has_declared_entity_scope
        and event_role_codes
        and role_code in event_role_codes
    ):
        return True
    has_explicit_scope = bool(
        event_equipment_ids
        or event_employee_ids
        or event_access_ids
        or event_role_codes
        or has_declared_entity_scope
    )
    # Events written before scoped payloads were introduced are conservative:
    # one related screen refresh is safer than silently advancing past state.
    return event_type in PRODUCTION_EVENT_TYPES and not has_explicit_scope


def collapse_duplicate_business_events(events):
    enriched_keys = {
        (event.event_type, event.object_type, event.object_id)
        for event in events
        if isinstance(event.payload, dict)
        and event.payload.get('action') not in {None, '', 'save'}
    }
    collapsed = []
    signatures = set()
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        key = (event.event_type, event.object_type, event.object_id)
        if payload.get('action') == 'save' and key in enriched_keys:
            continue
        signature = (
            key,
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str),
        )
        if signature in signatures:
            continue
        signatures.add(signature)
        collapsed.append(event)
    return collapsed


def serialize_event(event):
    return {
        'version': event.version,
        'type': event.event_type,
        'object_type': event.object_type,
        'object_id': event.object_id,
        'reason': event.reason,
        'payload': event.payload,
        'created_at': event.created_at.isoformat(),
    }


def relevant_event_delta(events_queryset, access, *, limit):
    scanned_events = list(events_queryset[:MAX_EVENT_SCAN + 1])
    scan_truncated = len(scanned_events) > MAX_EVENT_SCAN
    events = collapse_duplicate_business_events(scanned_events[:MAX_EVENT_SCAN])
    equipment_ids = None
    if access.role.code in {'driver', 'excavator_operator'}:
        equipment_ids = _worker_equipment_ids(access)
    relevant = [
        event
        for event in events
        if event_is_relevant(event, access, equipment_ids=equipment_ids)
    ]
    return (
        [serialize_event(event) for event in relevant[:limit]],
        scan_truncated or len(relevant) > limit,
    )
