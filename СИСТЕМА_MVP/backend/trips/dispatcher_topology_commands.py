"""Команды расстановки техники на Диспетчерском пульте."""

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from assignments.command_guards import (
    ClientActionPayloadConflict,
    ClientActionRequired,
    begin_client_action,
    complete_client_action,
)
from assignments.models import ExcavatorPlacement
from assignments.services import (
    HaulAssignmentStateConflict,
    projected_haul_assignments_for_excavator,
    schedule_haul_assignment,
    schedule_haul_release,
    validate_projected_excavator_state,
)
from core.models import lock_production_state
from references.models import Equipment

from .dispatcher_guards import (
    dispatcher_access_from_request,
    dispatcher_client_action_error,
    dispatcher_json_payload,
    dispatcher_shift_required_response,
)
from .dispatcher_header import get_active_dispatcher_shift
from .models import DispatcherActionType


def required_assignment_state_id(payload):
    if 'expected_assignment_state_id' not in payload:
        raise ClientActionRequired(
            'Экран открыт в старой версии. Обновите пульт перед изменением расстановки.'
        )
    try:
        value = int(payload.get('expected_assignment_state_id'))
    except (TypeError, ValueError):
        raise ClientActionRequired('Некорректная версия назначения. Обновите пульт.')
    if value < 0:
        raise ClientActionRequired('Некорректная версия назначения. Обновите пульт.')
    return value


def required_projected_assignment_states(payload):
    if 'expected_assignment_states' not in payload:
        raise ClientActionRequired(
            'Экран открыт в старой версии. Обновите пульт перед массовым действием.'
        )
    return payload.get('expected_assignment_states')


def execute_dispatcher_move_excavator(
    request,
    *,
    lock_mutation_access,
    action_logger,
    equipment_label,
):
    access = dispatcher_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к диспетчерскому пульту.'}, status=403)
    access = lock_mutation_access(request, access)
    if not access:
        return JsonResponse(
            {
                'ok': False,
                'error': 'Роль неактивна — доступен только просмотр',
                'code': 'inactive_role',
            },
            status=409,
        )
    shift_error = dispatcher_shift_required_response(access)
    if shift_error:
        return shift_error
    payload = dispatcher_json_payload(request)
    try:
        client_action_id, signature, repeated_response = begin_client_action(
            employee=access.employee,
            action_type='dispatcher_move_excavator',
            payload=payload,
        )
    except (ClientActionRequired, ClientActionPayloadConflict) as error:
        return dispatcher_client_action_error(payload, error)
    if repeated_response is not None:
        return JsonResponse(repeated_response)
    lock_production_state()
    excavator = get_object_or_404(
        Equipment.objects.select_for_update().select_related('equipment_type'),
        id=payload.get('excavator_id'),
        equipment_type__name__icontains='Экскаватор',
        is_active=True,
    )
    zone = payload.get('zone')
    if zone not in {ExcavatorPlacement.Zone.ACTIVE, ExcavatorPlacement.Zone.INACTIVE}:
        return JsonResponse({'ok': False, 'error': 'Некорректная зона экскаватора.'}, status=400)

    placement = (
        ExcavatorPlacement.objects.select_for_update()
        .filter(excavator=excavator)
        .first()
    )
    actual_zone = placement.zone if placement else ExcavatorPlacement.Zone.INACTIVE
    expected_zone = str(payload.get('expected_zone') or '').strip()
    if not expected_zone:
        return dispatcher_client_action_error(
            payload,
            ClientActionRequired('Экран открыт в старой версии. Обновите пульт.'),
        )
    if expected_zone != actual_zone:
        return dispatcher_client_action_error(
            payload,
            HaulAssignmentStateConflict(
                expected_state_id=expected_zone,
                actual_state_id=actual_zone,
            ),
            code='state_conflict',
        )
    if not placement:
        placement = ExcavatorPlacement.objects.create(excavator=excavator)

    scheduled_assignments = []
    if zone == ExcavatorPlacement.Zone.INACTIVE:
        try:
            expected_states = required_projected_assignment_states(payload)
            current_assignments = projected_haul_assignments_for_excavator(
                excavator,
                for_update=True,
            )
            validate_projected_excavator_state(current_assignments, expected_states)
            now = timezone.now()
            for current_assignment in current_assignments:
                assignment, _ = schedule_haul_release(
                    truck=current_assignment.truck,
                    assigned_by=access.employee,
                    now=now,
                    expected_state_id=current_assignment.id,
                )
                if assignment:
                    scheduled_assignments.append(assignment)
        except (ClientActionRequired, HaulAssignmentStateConflict) as error:
            return dispatcher_client_action_error(payload, error, code='state_conflict')

    placement.zone = zone
    placement.changed_by = access.employee
    placement.save(update_fields=['zone', 'changed_by', 'changed_at'])

    if zone == ExcavatorPlacement.Zone.INACTIVE:
        scheduled = len(scheduled_assignments)
        summary = f'{equipment_label(excavator)} возвращен в гараж, снятие назначений ожидает ({scheduled} самосв.)'
    else:
        scheduled = 0
        summary = f'{equipment_label(excavator)} переведен в активную смену'

    action_logger(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        target_summary=summary,
    )
    response_payload = {
        'ok': True,
        'scheduled': scheduled,
        'assignment_state_ids': {
            str(item.truck_id): item.id for item in scheduled_assignments
        },
        'client_action_id': client_action_id,
    }
    complete_client_action(
        employee=access.employee,
        shift=get_active_dispatcher_shift(access),
        action_type='dispatcher_move_excavator',
        client_action_id=client_action_id,
        signature=signature,
        response_payload=response_payload,
    )
    return JsonResponse(response_payload)


def execute_dispatcher_assign_truck(
    request,
    *,
    lock_mutation_access,
    action_logger,
    equipment_label,
):
    access = dispatcher_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к диспетчерскому пульту.'}, status=403)
    access = lock_mutation_access(request, access)
    if not access:
        return JsonResponse(
            {
                'ok': False,
                'error': 'Роль неактивна — доступен только просмотр',
                'code': 'inactive_role',
            },
            status=409,
        )
    shift_error = dispatcher_shift_required_response(access)
    if shift_error:
        return shift_error
    payload = dispatcher_json_payload(request)
    action = payload.get('action')
    now = timezone.now()
    try:
        client_action_id, signature, repeated_response = begin_client_action(
            employee=access.employee,
            action_type='dispatcher_assign_truck',
            payload=payload,
        )
    except (ClientActionRequired, ClientActionPayloadConflict) as error:
        return dispatcher_client_action_error(payload, error)
    if repeated_response is not None:
        return JsonResponse(repeated_response)
    lock_production_state()

    if action == 'release_complex':
        excavator = get_object_or_404(
            Equipment.objects.select_for_update().select_related('equipment_type'),
            id=payload.get('excavator_id'),
            equipment_type__name__icontains='Экскаватор',
            is_active=True,
        )
        try:
            expected_states = required_projected_assignment_states(payload)
            current_assignments = projected_haul_assignments_for_excavator(
                excavator,
                for_update=True,
            )
            validate_projected_excavator_state(current_assignments, expected_states)
            scheduled_assignments = []
            for current_assignment in current_assignments:
                assignment, _ = schedule_haul_release(
                    truck=current_assignment.truck,
                    assigned_by=access.employee,
                    now=now,
                    expected_state_id=current_assignment.id,
                )
                if assignment:
                    scheduled_assignments.append(assignment)
        except (ClientActionRequired, HaulAssignmentStateConflict) as error:
            return dispatcher_client_action_error(payload, error, code='state_conflict')
        scheduled = len(scheduled_assignments)
        action_logger(
            actor=access.employee,
            action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
            target_summary=f'{equipment_label(excavator)}: снятие назначений ожидает ({scheduled})',
        )
        response_payload = {
            'ok': True,
            'scheduled': scheduled,
            'assignment_state_ids': {
                str(item.truck_id): item.id for item in scheduled_assignments
            },
            'client_action_id': client_action_id,
        }
        complete_client_action(
            employee=access.employee,
            shift=get_active_dispatcher_shift(access),
            action_type='dispatcher_assign_truck',
            client_action_id=client_action_id,
            signature=signature,
            response_payload=response_payload,
        )
        return JsonResponse(response_payload)

    truck = get_object_or_404(
        Equipment.objects.select_for_update().select_related('equipment_type'),
        id=payload.get('truck_id'),
        equipment_type__name__icontains='Самосвал',
        is_active=True,
    )
    try:
        expected_state_id = required_assignment_state_id(payload)
    except ClientActionRequired as error:
        return dispatcher_client_action_error(payload, error)
    if action == 'release':
        try:
            assignment, created = schedule_haul_release(
                truck=truck,
                assigned_by=access.employee,
                now=now,
                expected_state_id=expected_state_id,
            )
        except HaulAssignmentStateConflict as error:
            return dispatcher_client_action_error(payload, error, code='state_conflict')
        action_logger(
            actor=access.employee,
            action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
            target_summary=f'{equipment_label(truck)} снят с комплекса и возвращен в гараж',
        )
        response_payload = {
            'ok': True,
            'assignment_id': assignment.id if assignment else None,
            'assignment_state_id': assignment.id if assignment else 0,
            'created': created,
            'client_action_id': client_action_id,
        }
        complete_client_action(
            employee=access.employee,
            shift=get_active_dispatcher_shift(access),
            action_type='dispatcher_assign_truck',
            client_action_id=client_action_id,
            signature=signature,
            response_payload=response_payload,
        )
        return JsonResponse(response_payload)

    if action != 'assign':
        return JsonResponse({'ok': False, 'error': 'Некорректное действие с самосвалом.'}, status=400)

    excavator = get_object_or_404(
        Equipment.objects.select_for_update().select_related('equipment_type'),
        id=payload.get('excavator_id'),
        equipment_type__name__icontains='Экскаватор',
        is_active=True,
    )
    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=excavator)
    if placement.zone != ExcavatorPlacement.Zone.ACTIVE:
        placement.zone = ExcavatorPlacement.Zone.ACTIVE
        placement.changed_by = access.employee
        placement.save(update_fields=['zone', 'changed_by', 'changed_at'])

    try:
        assignment, created = schedule_haul_assignment(
            truck=truck,
            excavator=excavator,
            assigned_by=access.employee,
            now=now,
            expected_state_id=expected_state_id,
        )
    except HaulAssignmentStateConflict as error:
        return dispatcher_client_action_error(payload, error, code='state_conflict')
    action_logger(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_ASSIGNMENT,
        target_summary=f'{equipment_label(truck)} назначен под {equipment_label(excavator)}',
        haul_assignment=assignment,
    )
    response_payload = {
        'ok': True,
        'assignment_id': assignment.id,
        'assignment_state_id': assignment.id,
        'created': created,
        'client_action_id': client_action_id,
    }
    complete_client_action(
        employee=access.employee,
        shift=get_active_dispatcher_shift(access),
        action_type='dispatcher_assign_truck',
        client_action_id=client_action_id,
        signature=signature,
        response_payload=response_payload,
    )
    return JsonResponse(response_payload)
