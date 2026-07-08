from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from trips.models import OPEN_TRIP_STATUSES, Trip
from users.models import EmployeeAccess

from .forms import MechanicDowntimeCreateForm
from .models import DowntimeEvent


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

    open_event = DowntimeEvent.objects.filter(
        equipment=trip.excavator,
        ended_at__isnull=True,
    ).select_related('reason').first()
    if open_event:
        messages.error(request, f'По технике {trip.excavator} уже есть открытый простой: {open_event.reason}.')
        return redirect('mechanic_dashboard')

    DowntimeEvent.objects.create(
        equipment=trip.excavator,
        employee=access.employee,
        reason=form.cleaned_data['reason'],
        started_at=timezone.now(),
        comment=form.cleaned_data['comment'],
    )
    messages.success(request, f'Механический простой по технике {trip.excavator} открыт.')
    return redirect('mechanic_dashboard')


def mechanic_close_downtime_view(request, event_id):
    access = get_mechanic_access(request)
    if not access:
        return redirect('login')
    if access.role.code not in {'mechanic', 'admin'}:
        return redirect('role_home')
    if request.method != 'POST':
        return redirect('mechanic_dashboard')

    event = get_object_or_404(
        DowntimeEvent.objects.select_related('equipment', 'reason'),
        id=event_id,
        ended_at__isnull=True,
    )
    event.ended_at = timezone.now()
    event.save(update_fields=['ended_at'])
    messages.success(request, f'Простой по технике {event.equipment} закрыт.')
    return redirect('mechanic_dashboard')
