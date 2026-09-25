"""Команды изменения настроек техники с Диспетчерского пульта."""

from django.db import transaction
from django.http import JsonResponse

from core.models import bump_operational_state, lock_production_state
from references.models import DumpPoint, Equipment, RockType


def execute_dispatcher_equipment_settings(
    request,
    access,
    equipment,
    *,
    active_role_state,
    active_shift_getter,
    json_payload,
    parse_destinations,
    normalize_numeric_setting,
    lock_mutation_access,
    error_response,
    save_work_context,
    build_settings,
    protect_response,
):
    if equipment.equipment_type.name != 'Экскаватор':
        return error_response('settings_not_available', status=400)
    if not active_role_state(request, access)['is_active']:
        return error_response('inactive_role', status=409)
    if not active_shift_getter(access):
        return error_response('dispatcher_shift_required', status=409)

    payload = json_payload(request)
    try:
        requested_version = int(payload.get('state_version', -1))
    except (TypeError, ValueError):
        requested_version = -1
    if requested_version < 0:
        return error_response('invalid_state_version', status=400)

    rock_type = RockType.objects.filter(
        id=payload.get('rock_type_id'),
        is_active=True,
    ).first()
    try:
        destinations = parse_destinations(
            payload,
            list(DumpPoint.objects.filter(is_active=True).order_by('name')),
        )
    except ValueError:
        return error_response('invalid_transport_distance', status=400)
    if not rock_type or not destinations:
        return error_response('invalid_work_settings', status=400)
    dump_points = [row['dump_point'] for row in destinations]
    loading_horizon = normalize_numeric_setting(payload.get('loading_horizon'))
    loading_block = normalize_numeric_setting(payload.get('loading_block'))

    with transaction.atomic():
        access = lock_mutation_access(request, access)
        if not access:
            return error_response('inactive_role', status=409)
        if not active_shift_getter(access):
            return error_response('dispatcher_shift_required', status=409)
        state = lock_production_state()
        if state.version != requested_version:
            return error_response('stale_board', status=409)
        equipment = (
            Equipment.objects
            .select_for_update()
            .select_related('equipment_type')
            .get(pk=equipment.pk)
        )
        placement = save_work_context(
            current_excavator=equipment,
            actor=access.employee,
            rock_type=rock_type,
            dump_points=dump_points,
            loading_horizon=loading_horizon,
            loading_block=loading_block,
            destination_settings=destinations,
        )
        state = bump_operational_state(
            'Dispatcher:excavator_work_settings',
            event_type='equipment_changed',
            object_type='Equipment',
            object_id=equipment.id,
            payload={
                'action': 'dispatcher_excavator_work_settings',
                'actor_id': access.employee_id,
                'excavator_id': equipment.id,
                'rock_type_id': rock_type.id,
                'dump_point_ids': [point.id for point in dump_points],
                'destinations': [
                    {
                        'dump_point_id': row['dump_point'].id,
                        'transport_distance_km': (
                            str(row['transport_distance_km'])
                            if row['transport_distance_km'] is not None
                            else ''
                        ),
                    }
                    for row in destinations
                ],
                'loading_horizon': loading_horizon,
                'loading_block': loading_block,
            },
        )

    return protect_response(JsonResponse({
        'ok': True,
        'contract': 'dispatcher-equipment-settings-v2',
        'equipment_id': equipment.id,
        'version': state.version,
        'settings': build_settings(
            equipment,
            placement,
            rock_types=RockType.objects.filter(is_active=True).order_by('name'),
            dump_points=DumpPoint.objects.filter(is_active=True).order_by('name'),
        ),
    }))
