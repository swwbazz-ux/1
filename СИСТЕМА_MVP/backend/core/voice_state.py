from collections import defaultdict

from assignments.models import (
    AssignmentStatus,
    HaulAssignment,
    HaulAssignmentAction,
)
from references.models import Equipment
from shifts.services import driver_open_shift_queryset
from trips.manual_loading import trip_driver_control_filter
from trips.models import Trip, TripStatus

from .realtime import selected_equipment_ids_for_access


def _iso(value):
    return value.isoformat() if value else ''


def _assignment_payload(assignment, state):
    return {
        'assignment_id': assignment.id,
        'action': assignment.action,
        'state': state,
        'truck_id': assignment.truck_id,
        'truck_number': str(assignment.truck.garage_number or ''),
        'excavator_id': assignment.excavator_id,
        'excavator_number': str(assignment.excavator.garage_number or ''),
        'effective_at': _iso(assignment.effective_at),
        'accepted_at': _iso(assignment.accepted_at),
    }


def _loaded_trip_payload(trip):
    point = trip.dump_point if trip.dump_point_id else trip.assigned_dump_point
    if point is None:
        return None
    assigned_point = trip.assigned_dump_point
    return {
        'trip_id': trip.id,
        'status': trip.status,
        'truck_id': trip.truck_id,
        'truck_number': str(trip.truck.garage_number or ''),
        'excavator_id': trip.excavator_id,
        'excavator_number': str(trip.excavator.garage_number or ''),
        'dump_point_id': point.id,
        'dump_point_name': str(point.name or ''),
        'assigned_dump_point_id': assigned_point.id if assigned_point else None,
        'assigned_dump_point_name': str(assigned_point.name or '') if assigned_point else '',
        'loaded_at': _iso(trip.loaded_at),
    }


def _driver_voice_state(access, selected_ids, observed_at_version):
    assignments_by_truck = defaultdict(list)
    assignments = (
        HaulAssignment.objects
        .filter(truck_id__in=selected_ids, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .select_related('truck', 'excavator')
        .order_by('assigned_at', 'id')
    )
    for assignment in assignments:
        assignments_by_truck[assignment.truck_id].append(assignment)

    loaded_by_truck = {}
    open_shift = (
        driver_open_shift_queryset(access.employee)
        .filter(equipment_id__in=selected_ids)
        .order_by('-opened_at', '-id')
        .first()
    )
    loaded_trips = Trip.objects.none()
    if open_shift is not None:
        # Keep authoritative voice truth on exactly the same ownership rule as
        # the driver's work screen. A current truck alone is not sufficient:
        # a loaded trip can belong to the previous driver's control shift.
        loaded_trips = (
            Trip.objects
            .filter(
                truck_id__in=selected_ids,
                status=TripStatus.LOADED_WAITING_UNLOAD,
            )
            .filter(trip_driver_control_filter(open_shift))
            .select_related(
                'truck',
                'excavator',
                'dump_point',
                'assigned_dump_point',
            )
            .order_by('created_at', 'id')
        )
    for trip in loaded_trips:
        loaded_by_truck[trip.truck_id] = trip

    truck_ids = set(selected_ids) | set(assignments_by_truck) | set(loaded_by_truck)
    equipment = {
        item.id: item
        for item in Equipment.objects.filter(id__in=truck_ids)
    }
    truck_states = []
    voice_operations = []
    for truck_id in sorted(truck_ids):
        open_assignments = assignments_by_truck.get(truck_id, [])
        active = next(
            (
                item for item in reversed(open_assignments)
                if item.status == AssignmentStatus.ACCEPTED
                and item.action == HaulAssignmentAction.ASSIGN
            ),
            None,
        )
        pending_assignment = next(
            (
                item for item in reversed(open_assignments)
                if item.status == AssignmentStatus.PENDING
                and item.action == HaulAssignmentAction.ASSIGN
            ),
            None,
        )
        pending_release = next(
            (
                item for item in reversed(open_assignments)
                if item.status == AssignmentStatus.PENDING
                and item.action == HaulAssignmentAction.RELEASE
            ),
            None,
        )
        if pending_release:
            # release_pending owns the same arbitration slot as the earlier
            # assignment event and must suppress replaying the old assignment.
            effective = None
            effective_state = 'release_pending'
        else:
            effective = pending_assignment or active
            effective_state = (
                'pending' if pending_assignment
                else ('active' if active else 'unassigned')
            )
        loaded_trip = loaded_by_truck.get(truck_id)
        loaded_trip_payload = _loaded_trip_payload(loaded_trip) if loaded_trip else None
        state = {
            'truck_id': truck_id,
            'truck_number': str(getattr(equipment.get(truck_id), 'garage_number', '') or ''),
            'assignment_state': effective_state,
            'current_assignment': (
                _assignment_payload(effective, effective_state) if effective else None
            ),
            'active_assignment': (
                _assignment_payload(active, 'active') if active else None
            ),
            'pending_assignment': (
                _assignment_payload(pending_assignment, 'pending')
                if pending_assignment else None
            ),
            'pending_release': (
                _assignment_payload(pending_release, 'release_pending')
                if pending_release else None
            ),
            'loaded_trip': loaded_trip_payload,
        }
        state['state_key'] = (
            f'driver-truck:{truck_id}:'
            f'assignment:{effective.id if effective else "none"}:'
            f'trip:{loaded_trip.id if loaded_trip else "none"}'
        )
        truck_states.append(state)

        if effective:
            voice_operations.append({
                'operation_key': f'assign:{effective.id}',
                'kind': 'driver_assignment',
                'observed_at_version': observed_at_version,
                'assignment_id': effective.id,
                'truck_id': effective.truck_id,
                'truck_number': str(effective.truck.garage_number or ''),
                'excavator_id': effective.excavator_id,
                'excavator_number': str(effective.excavator.garage_number or ''),
                'assignment_state': effective_state,
            })
        if loaded_trip and loaded_trip_payload:
            voice_operations.append({
                'operation_key': (
                    f'dump:{loaded_trip.id}:id-{loaded_trip.dump_point_id}'
                ),
                'kind': 'driver_dump_point',
                'observed_at_version': observed_at_version,
                'trip_id': loaded_trip.id,
                'truck_id': loaded_trip.truck_id,
                'dump_point_id': loaded_trip.dump_point_id,
                'dump_point_name': loaded_trip_payload['dump_point_name'],
            })
    return {'truck_states': truck_states}, voice_operations


def _excavator_voice_state(selected_ids, observed_at_version):
    assignments_by_excavator = defaultdict(list)
    assignments = (
        HaulAssignment.objects
        .filter(
            excavator_id__in=selected_ids,
            status=AssignmentStatus.ACCEPTED,
            action=HaulAssignmentAction.ASSIGN,
            ended_at__isnull=True,
        )
        .select_related('truck', 'excavator')
        .order_by('assigned_at', 'id')
    )
    voice_operations = []
    for assignment in assignments:
        assignments_by_excavator[assignment.excavator_id].append(assignment)
        voice_operations.append({
            'operation_key': (
                f'excavator-assignment:assign:{assignment.id}:'
                f'{assignment.excavator_id}'
            ),
            'kind': 'excavator_truck_assigned',
            'observed_at_version': observed_at_version,
            'assignment_id': assignment.id,
            'excavator_id': assignment.excavator_id,
            'truck_id': assignment.truck_id,
            'truck_number': str(assignment.truck.garage_number or ''),
        })

    equipment = {
        item.id: item
        for item in Equipment.objects.filter(id__in=selected_ids)
    }
    equipment_states = []
    for excavator_id in sorted(selected_ids):
        current = assignments_by_excavator.get(excavator_id, [])
        equipment_states.append({
            'excavator_id': excavator_id,
            'excavator_number': str(
                getattr(equipment.get(excavator_id), 'garage_number', '') or ''
            ),
            'state_key': (
                f'excavator:{excavator_id}:assignments:'
                + ','.join(str(item.id) for item in current)
            ),
            'assignments': [
                _assignment_payload(item, 'active') for item in current
            ],
        })
    return {'equipment_states': equipment_states}, voice_operations


def build_current_voice_state(access, role_app_code, *, observed_at_version):
    """Return authoritative current voice truth without consulting event history."""
    selected_ids = sorted(selected_equipment_ids_for_access(access))
    driver = None
    excavator = None
    voice_operations = []
    if role_app_code == 'driver':
        driver, voice_operations = _driver_voice_state(
            access,
            selected_ids,
            observed_at_version,
        )
    elif role_app_code == 'excavator_operator':
        excavator, voice_operations = _excavator_voice_state(
            selected_ids,
            observed_at_version,
        )
    return {
        'schema_version': 1,
        'authoritative': True,
        'source': 'domain_tables',
        'observed_at_version': observed_at_version,
        'role_app_code': role_app_code,
        'equipment_selection_ids': selected_ids,
        'voice_operations': voice_operations,
        'driver': driver,
        'excavator': excavator,
    }
