"""Команды управления рейсами с Диспетчерского пульта."""

from datetime import datetime, timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    ExcavatorDumpPointSetting,
    ExcavatorPlacement,
    HaulAssignment,
)
from core.models import bump_operational_state, lock_production_state
from core.production_time import BUSINESS_TIME_ZONE
from references.models import DumpPoint, Equipment, RockType
from shifts.models import EmployeeShift
from shifts.services import equipment_is_truck
from users.models import Employee, EmployeeAccess

from .dispatcher_guards import (
    dispatcher_shift_required_redirect,
    get_dispatcher_control_url,
)
from .free_bucket import close_free_bucket_acceptance_for_trip
from .models import DispatcherActionType, OPEN_TRIP_STATUSES, Trip, TripStatus
from .trip_creation import resolve_required_trip_measurements


DISPATCHER_MANUAL_TRIP_MAX_COUNT = 10


def parse_dispatcher_manual_trip_time(raw_value, *, now):
    """datetime-local из формы (часы предприятия) -> aware datetime; пусто -> сейчас."""
    raw = str(raw_value or '').strip()
    if not raw:
        return now
    parsed = None
    for pattern in ('%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S', '%d.%m.%Y %H:%M'):
        try:
            parsed = datetime.strptime(raw, pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError(
            'Время рейса: укажите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.'
        )
    return parsed.replace(tzinfo=BUSINESS_TIME_ZONE)


def execute_dispatcher_cancel_trip(
    request,
    trip_id,
    *,
    lock_mutation_access,
    reconcile_excavator,
    action_logger,
):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True)
        .first()
    )
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, 'Укажите причину отмены рейса.')
        return redirect(redirect_url)

    access = lock_mutation_access(request, access)
    if not access:
        messages.error(request, 'Роль неактивна — доступен только просмотр.')
        return redirect(redirect_url)
    # Driver unload locks the production state before the trip. Keep the same
    # order here so concurrent terminal actions cannot deadlock each other.
    lock_production_state()
    trip = (
        Trip.objects
        .select_for_update(of=('self',))
        .select_related('truck', 'excavator')
        .filter(id=trip_id, status__in=OPEN_TRIP_STATUSES)
        .first()
    )
    if not trip:
        messages.error(request, 'Активный рейс для отмены не найден.')
        return redirect(redirect_url)

    trip.status = TripStatus.CANCELLED
    trip.cancelled_at = timezone.now()
    trip.save(update_fields=['status', 'cancelled_at'])
    close_free_bucket_acceptance_for_trip(trip, closed_at=trip.cancelled_at)
    reconcile_excavator(trip.excavator)
    action_logger(
        actor=access.employee,
        action_type=DispatcherActionType.CANCEL_TRIP,
        trip=trip,
        target_summary=f'{trip.truck} -> {trip.dump_point}',
        reason=reason,
    )
    bump_operational_state(
        'Trip:dispatcher_cancel_trip',
        event_type='trip_changed',
        object_type='Trip',
        object_id=trip.id,
        payload={
            'action': 'dispatcher_cancel_trip',
            'trip_id': trip.id,
            'truck_id': trip.truck_id,
            'excavator_id': trip.excavator_id,
            'status': TripStatus.CANCELLED,
        },
    )
    messages.success(request, f'Рейс {trip.truck} -> {trip.dump_point} отменен.')
    return redirect(redirect_url)


def execute_dispatcher_complete_trip(
    request,
    trip_id,
    *,
    lock_mutation_access,
    finalize_trip,
    action_logger,
):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True)
        .first()
    )
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)

    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error
    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(
            request,
            'Укажите причину служебного завершения рейса.',
        )
        return redirect(redirect_url)

    trip_reference = (
        Trip.objects
        .select_related('truck', 'excavator', 'loading_shift')
        .filter(id=trip_id, status__in=OPEN_TRIP_STATUSES)
        .first()
    )
    if not trip_reference:
        messages.error(request, 'Активный рейс для служебного завершения не найден.')
        return redirect(redirect_url)

    unloading_employee_id = (
        EmployeeShift.objects
        .filter(equipment=trip_reference.truck, closed_at__isnull=True)
        .order_by('-opened_at')
        .values_list('employee_id', flat=True)
        .first()
    )
    if not unloading_employee_id:
        messages.error(
            request,
            'Нельзя служебно завершить рейс: не найдена открытая смена по этому самосвалу.',
        )
        return redirect(redirect_url)
    locked_employee_ids = list(
        Employee.objects
        .select_for_update()
        .filter(pk__in={access.employee_id, unloading_employee_id})
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    access = lock_mutation_access(request, access)
    if not access:
        messages.error(request, 'Роль неактивна — доступен только просмотр.')
        return redirect(redirect_url)

    unloading_shift = (
        EmployeeShift.objects
        .select_for_update()
        .filter(equipment=trip_reference.truck, closed_at__isnull=True)
        .order_by('-opened_at')
        .first()
    )
    if not unloading_shift:
        messages.error(
            request,
            'Нельзя служебно завершить рейс: не найдена открытая смена по этому самосвалу.',
        )
        return redirect(redirect_url)
    if unloading_shift.employee_id not in locked_employee_ids:
        messages.error(
            request,
            'Смена по самосвалу изменилась. Повторите служебное завершение.',
        )
        return redirect(redirect_url)

    # Driver unload uses production state -> equipment/shift -> trip. Acquire
    # the shared state before the trip here as well to preserve lock order.
    lock_production_state()
    trip = (
        Trip.objects
        .select_for_update(of=('self',))
        .select_related('truck', 'excavator', 'loading_shift')
        .filter(
            id=trip_id,
            truck_id=unloading_shift.equipment_id,
            status__in=OPEN_TRIP_STATUSES,
        )
        .first()
    )
    if not trip:
        messages.error(request, 'Активный рейс уже завершен другим действием.')
        return redirect(redirect_url)

    finalize_trip(
        trip,
        driver=unloading_shift.employee,
        unloading_shift=unloading_shift,
    )
    action_logger(
        actor=access.employee,
        action_type=DispatcherActionType.COMPLETE_TRIP,
        trip=trip,
        target_summary=f'{trip.truck} -> {trip.dump_point}',
        reason=reason,
    )
    bump_operational_state(
        'Trip:dispatcher_complete_trip',
        event_type='trip_changed',
        object_type='Trip',
        object_id=trip.id,
        payload={
            'action': 'dispatcher_complete_trip',
            'trip_id': trip.id,
            'truck_id': trip.truck_id,
            'excavator_id': trip.excavator_id,
            'assigned_dump_point_id': (
                trip.assigned_dump_point_id or trip.dump_point_id
            ),
            'actual_dump_point_id': trip.actual_dump_point_id or trip.dump_point_id,
            'status': TripStatus.COMPLETED,
        },
    )
    messages.success(request, f'Рейс {trip.truck} завершен служебно.')
    return redirect(redirect_url)


def execute_dispatcher_manual_trip(
    request,
    equipment_id,
    *,
    lock_mutation_access,
    format_datetime,
    action_logger,
):
    """Создать сразу выполненные рейсы водителю открытой смены самосвала."""
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True)
        .first()
    )
    if not access or access.role.code not in {'dispatcher', 'admin'}:
        return redirect('role_home')
    redirect_url = get_dispatcher_control_url(request)
    if request.method != 'POST':
        return redirect(redirect_url)
    shift_error = dispatcher_shift_required_redirect(request, access, redirect_url)
    if shift_error:
        return shift_error

    reason = request.POST.get('reason', '').strip()
    if not reason:
        messages.error(request, 'Укажите причину ручного рейса.')
        return redirect(redirect_url)
    try:
        trips_count = int(request.POST.get('trips_count', '1') or 1)
    except (TypeError, ValueError):
        trips_count = 0
    if not 1 <= trips_count <= DISPATCHER_MANUAL_TRIP_MAX_COUNT:
        messages.error(
            request,
            f'Количество рейсов: от 1 до {DISPATCHER_MANUAL_TRIP_MAX_COUNT}.',
        )
        return redirect(redirect_url)
    now = timezone.now()
    try:
        completed_at = parse_dispatcher_manual_trip_time(
            request.POST.get('completed_at'),
            now=now,
        )
    except ValueError as error:
        messages.error(request, str(error))
        return redirect(redirect_url)
    if completed_at > now + timedelta(minutes=5):
        messages.error(request, 'Время рейса не может быть в будущем.')
        return redirect(redirect_url)
    try:
        excavator_id = int(request.POST.get('excavator_id', '') or 0)
    except (TypeError, ValueError):
        excavator_id = 0

    access = lock_mutation_access(request, access)
    if not access:
        messages.error(request, 'Роль неактивна — доступен только просмотр.')
        return redirect(redirect_url)

    truck = (
        Equipment.objects
        .select_for_update(of=('self',))
        .select_related('equipment_type', 'model')
        .filter(pk=equipment_id, is_active=True)
        .first()
    )
    if not truck or not equipment_is_truck(truck):
        messages.error(request, 'Самосвал для ручного рейса не найден.')
        return redirect(redirect_url)
    truck_shift = (
        EmployeeShift.objects
        .select_for_update(of=('self',))
        .select_related('employee')
        .filter(equipment=truck, closed_at__isnull=True)
        .order_by('-opened_at')
        .first()
    )
    if not truck_shift:
        messages.error(
            request,
            f'{truck}: нет открытой смены водителя — рейс некому записать.',
        )
        return redirect(redirect_url)
    if truck_shift.opened_at and completed_at < truck_shift.opened_at:
        messages.error(
            request,
            f'Время рейса раньше начала смены водителя '
            f'({format_datetime(truck_shift.opened_at)}).',
        )
        return redirect(redirect_url)
    assignment = (
        HaulAssignment.objects
        .select_related('excavator')
        .filter(
            truck=truck,
            excavator_id=excavator_id,
            status__in=[AssignmentStatus.ACCEPTED, AssignmentStatus.PENDING],
            ended_at__isnull=True,
        )
        .order_by('-assigned_at')
        .first()
    )
    if not assignment:
        messages.error(
            request,
            f'{truck} больше не назначен на выбранный экскаватор — обновите пульт.',
        )
        return redirect(redirect_url)
    excavator = assignment.excavator
    rock_type = RockType.objects.filter(
        id=request.POST.get('rock_type_id'),
        is_active=True,
    ).first()
    dump_point = DumpPoint.objects.filter(
        id=request.POST.get('dump_point_id'),
        is_active=True,
    ).first()
    if not rock_type or not dump_point:
        messages.error(
            request,
            'Выберите породу и точку разгрузки для ручного рейса.',
        )
        return redirect(redirect_url)
    try:
        volume_m3, tonnage = resolve_required_trip_measurements(truck, rock_type)
    except ValidationError as error:
        messages.error(
            request,
            '; '.join(getattr(error, 'messages', None) or [str(error)]),
        )
        return redirect(redirect_url)

    placement = (
        ExcavatorPlacement.objects
        .select_related('work_dump_point')
        .filter(excavator=excavator)
        .first()
    )
    transport_distance_km = None
    if placement:
        setting = (
            ExcavatorDumpPointSetting.objects
            .filter(placement=placement, dump_point=dump_point)
            .first()
        )
        if setting and setting.transport_distance_km is not None:
            transport_distance_km = setting.transport_distance_km
        elif placement.work_dump_point_id == dump_point.id:
            transport_distance_km = placement.transport_distance_km
    loading_shift = (
        EmployeeShift.objects
        .select_related('employee')
        .filter(equipment=excavator, closed_at__isnull=True)
        .order_by('-opened_at')
        .first()
    )
    note = f'Добавлен диспетчером вручную: {reason}'[:1000]
    created = []
    for index in range(trips_count):
        trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            excavator_operator=getattr(loading_shift, 'employee', None),
            driver=truck_shift.employee,
            loading_shift=loading_shift,
            unloading_shift=truck_shift,
            rock_type=rock_type,
            dump_point=dump_point,
            assigned_dump_point=dump_point,
            actual_dump_point=dump_point,
            volume_m3=volume_m3,
            tonnage=tonnage,
            loading_horizon=(
                str(getattr(placement, 'loading_horizon', '') or '')[:64]
            ),
            loading_block=str(getattr(placement, 'loading_block', '') or '')[:64],
            transport_distance_km=transport_distance_km,
            note=note,
            status=TripStatus.COMPLETED,
            completed_at=(
                completed_at - timedelta(seconds=trips_count - 1 - index)
            ),
            is_carryover=bool(
                loading_shift and loading_shift.shift_type != truck_shift.shift_type
            ),
        )
        action_logger(
            actor=access.employee,
            action_type=DispatcherActionType.MANUAL_TRIP,
            trip=trip,
            target_summary=f'{truck} -> {dump_point}',
            reason=reason,
        )
        created.append(trip)
    bump_operational_state(
        'Trip:dispatcher_manual_trip',
        event_type='trip_changed',
        object_type='Trip',
        object_id=created[-1].id,
        payload={
            'action': 'dispatcher_manual_trip',
            'trip_ids': [trip.id for trip in created],
            'truck_id': truck.id,
            'excavator_id': excavator.id,
            'assigned_dump_point_id': dump_point.id,
            'actual_dump_point_id': dump_point.id,
            'status': TripStatus.COMPLETED,
        },
    )
    count_label = (
        'рейс' if trips_count == 1 else 'рейса' if trips_count < 5 else 'рейсов'
    )
    messages.success(
        request,
        f'{truck}: добавлено {trips_count} {count_label} вручную — '
        f'{dump_point}, {rock_type}, водитель {truck_shift.employee}.',
    )
    return redirect(redirect_url)
