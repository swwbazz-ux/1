"""Команды управления простоями с Диспетчерского пульта."""

from django.utils import timezone

from core.models import bump_operational_state, lock_production_state
from downtimes.models import DowntimeEvent

from .dispatcher_guards import dispatcher_access_from_request, dispatcher_json_payload
from .dispatcher_header import get_active_dispatcher_shift


def execute_dispatcher_close_downtime(
    request,
    event_id,
    *,
    lock_mutation_access,
    response_builder,
    event_payload,
):
    access = dispatcher_access_from_request(request)
    if not access:
        return response_builder(
            {'ok': False, 'error': 'forbidden'},
            status=403,
        )
    access = lock_mutation_access(request, access)
    if not access:
        return response_builder(
            {'ok': False, 'error': 'inactive_role'},
            status=409,
        )
    if not get_active_dispatcher_shift(access):
        return response_builder(
            {'ok': False, 'error': 'dispatcher_shift_required'},
            status=409,
        )

    payload = dispatcher_json_payload(request)
    try:
        requested_version = int(payload.get('state_version', -1))
    except (TypeError, ValueError):
        requested_version = -1
    if requested_version < 0:
        return response_builder(
            {'ok': False, 'error': 'invalid_state_version'},
            status=400,
        )

    state = lock_production_state()
    event = (
        DowntimeEvent.objects
        .select_for_update()
        .select_related('equipment', 'equipment__equipment_type', 'reason')
        .filter(
            pk=event_id,
            equipment__is_active=True,
            equipment__equipment_type__name__in={'Самосвал', 'Экскаватор'},
        )
        .first()
    )
    if not event:
        return response_builder(
            {'ok': False, 'error': 'downtime_not_found'},
            status=404,
        )
    if event.ended_at:
        response_payload = event_payload(
            event,
            action='dispatcher_downtime_already_closed',
        )
        response_payload.update({
            'closed': False,
            'already_closed': True,
            'version': state.version,
        })
        return response_builder(response_payload)
    if state.version != requested_version:
        return response_builder(
            {
                'ok': False,
                'error': 'stale_board',
                'version': state.version,
            },
            status=409,
        )

    event.ended_at = timezone.now()
    event.save(update_fields=['ended_at'])
    state = bump_operational_state(
        'Dispatcher:downtime_closed',
        event_type='downtime_changed',
        object_type='DowntimeEvent',
        object_id=event.id,
        payload={
            'action': 'dispatcher_downtime_closed',
            'actor_id': access.employee_id,
            'equipment_id': event.equipment_id,
            'equipment_type': event.equipment.equipment_type.name,
            'reason_id': event.reason_id,
            'source': 'dispatcher_override',
        },
    )
    response_payload = event_payload(
        event,
        action='dispatcher_downtime_closed',
        closed=True,
    )
    response_payload.update({
        'already_closed': False,
        'version': state.version,
    })
    return response_builder(response_payload)
