from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from references.models import Equipment
from trips.models import OPEN_TRIP_STATUSES, Trip
from users.models import EmployeeAccess

from .forms import MechanicDowntimeCreateForm
from .models import DowntimeEvent


def equipment_is_truck(equipment):
    equipment_type_name = (
        getattr(getattr(equipment, 'equipment_type', None), 'name', '') or ''
    ).casefold()
    return 'самосвал' in equipment_type_name


def format_duration(started_at, ended_at=None):
    end_time = ended_at or timezone.now()
    total_minutes = max(int((end_time - started_at).total_seconds() // 60), 0)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f'{hours} ч {minutes} мин'
    if hours:
        return f'{hours} ч'
    return f'{minutes} мин'


def get_mechanic_access(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    return (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True)
        .first()
    )


def mechanic_dashboard_view(request):
    access = get_mechanic_access(request)
    if not access:
        return redirect('login')
    if access.role.code not in {'mechanic', 'admin', 'manager'}:
        return redirect('role_home')

    open_events = list(
        DowntimeEvent.objects
        .filter(ended_at__isnull=True)
        .select_related('equipment', 'equipment__equipment_type', 'reason', 'employee')
        .order_by('-started_at')[:40]
    )
    recent_closed_events = list(
        DowntimeEvent.objects
        .filter(ended_at__isnull=False)
        .select_related('equipment', 'equipment__equipment_type', 'reason', 'employee')
        .order_by('-ended_at')[:20]
    )
    open_event_by_equipment_id = {event.equipment_id: event for event in open_events}

    source_trips = (
        Trip.objects
        .filter(status__in=OPEN_TRIP_STATUSES)
        .exclude(downtime_text='')
        .select_related('excavator', 'excavator__equipment_type', 'rock_type', 'dump_point', 'excavator_operator')
        .order_by('-created_at')
    )

    pending_downtimes = []
    seen_equipment_ids = set()
    for trip in source_trips:
        if not trip.excavator_id or trip.excavator_id in seen_equipment_ids:
            continue
        if trip.excavator_id in open_event_by_equipment_id:
            continue
        seen_equipment_ids.add(trip.excavator_id)
        pending_downtimes.append(
            {
                'trip': trip,
                'equipment': trip.excavator,
                'source_text': trip.downtime_text,
                'form': MechanicDowntimeCreateForm(
                    equipment=trip.excavator,
                    source_text=trip.downtime_text,
                    prefix=f'trip_{trip.id}',
                ),
            }
        )
        if len(pending_downtimes) >= 20:
            break

    for event in open_events:
        event.duration_text = format_duration(event.started_at)
        event.mechanic_can_close = not equipment_is_truck(event.equipment)
    for event in recent_closed_events:
        event.duration_text = format_duration(event.started_at, event.ended_at)

    return render(
        request,
        'downtimes/mechanic_dashboard.html',
        {
            'access': access,
            'pending_downtimes': pending_downtimes,
            'open_events': open_events,
            'recent_closed_events': recent_closed_events,
            'pending_count': len(pending_downtimes),
            'open_count': len(open_events),
            'critical_open_count': sum(1 for event in open_events if event.reason.is_critical),
            'closed_count': len(recent_closed_events),
        },
    )


@transaction.atomic
def mechanic_create_downtime_view(request, trip_id):
    access = get_mechanic_access(request)
    if not access:
        return redirect('login')
    if access.role.code not in {'mechanic', 'admin'}:
        return redirect('role_home')
    if request.method != 'POST':
        return redirect('mechanic_dashboard')

    trip = get_object_or_404(
        Trip.objects.select_related('excavator', 'excavator__equipment_type'),
        id=trip_id,
        status__in=OPEN_TRIP_STATUSES,
    )
    form = MechanicDowntimeCreateForm(
        request.POST,
        equipment=trip.excavator,
        source_text=trip.downtime_text,
        prefix=f'trip_{trip.id}',
    )
    if not form.is_valid():
        messages.error(request, 'Не удалось открыть простой механической службы. Проверьте заполнение формы.')
        return redirect('mechanic_dashboard')

    equipment = Equipment.objects.select_for_update().get(pk=trip.excavator_id)
    open_event = DowntimeEvent.objects.select_for_update().filter(
        equipment=equipment,
        ended_at__isnull=True,
    ).select_related('reason').first()
    if open_event:
        messages.error(request, f'По технике {trip.excavator} уже есть открытый простой: {open_event.reason}.')
        return redirect('mechanic_dashboard')

    DowntimeEvent.objects.create(
        equipment=equipment,
        employee=access.employee,
        reason=form.cleaned_data['reason'],
        started_at=timezone.now(),
        comment=form.cleaned_data['comment'],
    )
    messages.success(request, f'Механический простой по технике {trip.excavator} открыт.')
    return redirect('mechanic_dashboard')


@transaction.atomic
def mechanic_close_downtime_view(request, event_id):
    access = get_mechanic_access(request)
    if not access:
        return redirect('login')
    if access.role.code not in {'mechanic', 'admin'}:
        return redirect('role_home')
    if request.method != 'POST':
        return redirect('mechanic_dashboard')

    event_reference = get_object_or_404(
        DowntimeEvent.objects.only('equipment_id'),
        id=event_id,
    )
    equipment = get_object_or_404(
        Equipment.objects.select_for_update(),
        id=event_reference.equipment_id,
    )
    event = get_object_or_404(
        DowntimeEvent.objects
        .select_for_update()
        .select_related('equipment', 'equipment__equipment_type', 'reason'),
        id=event_id,
        equipment=equipment,
    )
    if equipment_is_truck(event.equipment):
        return HttpResponseForbidden(
            'Простой самосвала закрывает только водитель.',
            content_type='text/plain; charset=utf-8',
        )
    if event.ended_at is not None:
        messages.info(request, f'Простой по технике {event.equipment} уже закрыт.')
        return redirect('mechanic_dashboard')

    event.ended_at = timezone.now()
    event.save(update_fields=['ended_at'])
    messages.success(request, f'Простой по технике {event.equipment} закрыт.')
    return redirect('mechanic_dashboard')
