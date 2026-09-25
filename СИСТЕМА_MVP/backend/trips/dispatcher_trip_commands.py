"""Команды управления рейсами с Диспетчерского пульта."""

from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone

from core.models import bump_operational_state, lock_production_state
from users.models import EmployeeAccess

from .dispatcher_guards import (
    dispatcher_shift_required_redirect,
    get_dispatcher_control_url,
)
from .free_bucket import close_free_bucket_acceptance_for_trip
from .models import DispatcherActionType, OPEN_TRIP_STATUSES, Trip, TripStatus


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
