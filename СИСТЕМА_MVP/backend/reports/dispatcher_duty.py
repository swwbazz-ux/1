"""Шкала дежурства и журнал действий для витрины руководства."""

from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import Q
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.production_time import business_localtime, production_shift_bounds, production_work_date
from shifts.models import ShiftType
from trips.dispatcher_duty import SESSION_LOGOUT, WINDOW_VALIDITY, WindowState
from trips.dispatcher_header import dispatcher_shift_queryset
from trips.models import DispatcherActionLog, DispatcherActionType, DispatcherDutyTracking
from users.active_role import role_session_state
from users.models import EmployeeAccess

from .views import parse_filter_date


STATE_LABELS = {'on': 'В пульте', 'out': 'Вне пульта', 'unknown': 'Нет данных', 'future': 'Предстоит'}


def clock(at):
    return business_localtime(at).strftime('%H:%M')


def duration(seconds, *, exact=False):
    if exact:
        hours, rest = divmod(int(seconds), 3600)
        minutes, secs = divmod(rest, 60)
        units = ((hours, 'ч'), (minutes, 'мин'), (secs, 'с'))
        return ' '.join(f'{n} {unit}' for n, unit in units if n) or '0 с'
    minutes = int(seconds // 60)
    if not minutes:
        return 'меньше минуты' if seconds else '0 мин'
    hours, mins = divmod(minutes, 60)
    return ' '.join(part for part in (f'{hours} ч' if hours else '', f'{mins} мин' if mins else '') if part)


def window_ranges(shift, stop):
    """Yield confirmed and missing observations per window; an explicit logout ends the entire session."""
    windows = defaultdict(list)
    logouts = {}
    for row in shift.duty_windows.all():
        if row.window_id == SESSION_LOGOUT:
            logouts[row.session_digest] = row.started_at
        else:
            windows[(row.session_digest, row.window_id)].append(row)
    for key, periods in windows.items():
        session_stop = min(logouts.get(key[0], stop), stop)
        periods.sort(key=lambda row: (row.started_at, row.pk))
        for index, row in enumerate(periods):
            until = min(periods[index + 1].started_at, session_stop) if index + 1 < len(periods) else session_stop
            if until <= row.started_at:
                continue
            expiry = row.last_seen_at + WINDOW_VALIDITY[row.state] if row.state in WINDOW_VALIDITY else until
            confirmed = min(until, expiry)
            yield key, row.started_at, confirmed, row.state
            if confirmed < until:
                yield key, confirmed, until, 'unknown'
    for digest, at in logouts.items():
        yield (digest, SESSION_LOGOUT), at, stop, WindowState.SIGNED_OUT


def classify(active, tracking, at, shifts):
    staff = {key[0]: value for key, value in active.items() if len(key) == 1}
    if not staff:
        if not tracking or at < tracking:
            return 'unknown', 'За это время наблюдений нет', ''
        previously_open = any(s.closed_at and s.closed_at <= at for s in shifts)
        return 'out', 'Смена завершена, пост свободен' if previously_open else 'Смена не открыта', ''
    names = ', '.join(staff.values())
    observed = defaultdict(set)
    for key, state in active.items():
        if len(key) > 1 and key[0] in staff:
            observed[key[0]].add(state)
    states = set().union(*observed.values())
    if WindowState.VISIBLE in states:
        visible_names = (name for pk, name in staff.items() if WindowState.VISIBLE in observed[pk])
        return 'on', 'Пульт открыт на экране', ', '.join(visible_names)
    if 'unknown' in states or any(not observed.get(pk) for pk in staff):
        return 'unknown', 'Состояние пульта не подтверждено', names
    if WindowState.HIDDEN in states:
        return 'out', 'Все открытые окна пульта свёрнуты', names
    if WindowState.SIGNED_OUT in states:
        return 'out', 'Выход из приложения', names
    return 'out', 'Окна пульта закрыты', names


def timeline(start, end, now, tracking, shifts):
    cutoff = min(max(now, start), end)
    # Process ends before starts at the same instant. All ranges are half-open.
    changes = defaultdict(lambda: ([], {}))
    changes[start]
    changes[cutoff]
    if tracking and start < tracking < cutoff:
        changes[tracking]

    def add(key, first, last, value):
        first, last = max(first, start), min(last, cutoff)
        if first < last:
            changes[first][1][key] = value
            changes[last][0].append(key)

    for shift in shifts:
        stop = min(shift.closed_at or cutoff, cutoff)
        add((shift.pk,), shift.opened_at, stop, shift.employee.full_name)
        for key, first, last, state in window_ranges(shift, stop):
            add((shift.pk, *key), first, last, state)

    raw = []
    active = {}
    times = sorted(changes)
    for at, following in zip(times, times[1:]):
        remove, additions = changes[at]
        for key in remove:
            active.pop(key, None)
        active.update(additions)
        kind, reason, who = classify(active, tracking, at, shifts)
        if raw and raw[-1][2:] == [kind, reason, who]:
            raw[-1][1] = following
        else:
            raw.append([at, following, kind, reason, who])
    if cutoff < end:
        raw.append([cutoff, end, 'future', 'После времени среза', ''])
    return raw


def journal_event(at, kind, title, person='', detail=''):
    local = business_localtime(at)
    return dict(at=local.isoformat(), time=clock(at), day=local.strftime('%d.%m.%Y'),
                kind=kind, title=title, person=person, detail=detail)


def operations(start, end, tracking, shifts):
    rows = DispatcherActionLog.objects.filter(created_at__gte=start, created_at__lt=end)
    eligible = Q(actor_role_code='dispatcher')
    # Old rows have no acting role. Only include a historical action if its
    # author held the post and has no alternative role that can issue the command.
    legacy_staff = {s.employee_id for s in shifts}
    if tracking and start < tracking and legacy_staff:
        other = EmployeeAccess.objects.filter(employee_id__in=legacy_staff,
            role__code__in=['admin', 'manager', 'mining_master']).values_list('employee_id', flat=True)
        eligible |= Q(actor_role_code='', created_at__lt=tracking, actor_id__in=legacy_staff.difference(other))
    result = []
    for row in rows.filter(eligible).select_related('actor').order_by('created_at', 'pk'):
        duty_id = row.duty_shift_id
        if not row.actor_role_code:
            duty_id = next((s.pk for s in shifts if s.employee_id == row.actor_id
                            and s.opened_at <= row.created_at and (s.closed_at is None or row.created_at < s.closed_at)), None)
            if duty_id is None:
                continue
        label = ('Изменение расстановки' if row.action_type == DispatcherActionType.CANCEL_ASSIGNMENT
                 else row.get_action_type_display())
        if row.action_type == 'report_source_correction' and row.duty_detail:
            summary = f'Рейс № {row.trip_id}' if row.trip_id else 'Рейс'
        else:
            summary = row.target_summary
        change = row.duty_detail
        if not row.actor_role_code and row.haul_assignment_id and not change:
            change = 'Предыдущий комплекс не записан'
        detail = ' · '.join(filter(None, (summary, change, row.reason)))
        result.append((row.created_at, journal_event(row.created_at, 'operation', label, row.actor.full_name, detail), duty_id))
    return result


def build_slot(selected_date, shift_type, now, tracking, all_shifts, day_operations):
    start, end = production_shift_bounds(selected_date, shift_type)
    shifts = [s for s in all_shifts if start <= now and s.opened_at < end and (not s.closed_at or s.closed_at > start)]
    slot_operations = [op for op in day_operations if start <= op[0] < end and op[0] <= now]
    by_shift = Counter(duty_id for _, _, duty_id in slot_operations)
    seconds = (end - start).total_seconds()

    def staff_time(at):
        local = business_localtime(at)
        return clock(at) if local.date() == selected_date else local.strftime('%d.%m %H:%M')

    def percent(at):
        return round(100 * (at - start).total_seconds() / seconds, 5)

    totals = Counter()
    parts, events, marks, staff = [], [], [], []
    for shift in shifts:
        who = shift.employee.full_name
        staff.append(dict(person=who, opened=staff_time(shift.opened_at), operations=by_shift[shift.pk],
                          closed=staff_time(shift.closed_at) if shift.closed_at and shift.closed_at <= now else None))
        owns_start = start <= shift.opened_at < end
        if owns_start:
            opener = shift.opened_by.full_name if shift.opened_by_id else who
            events.append(journal_event(shift.opened_at, 'duty', 'Смена открыта', opener, who if opener != who else ''))
        if shift.closed_at and start < shift.closed_at <= min(end, now):
            automatic = shift.service_close_kind == 'auto_expired'
            closer = 'Система' if automatic else shift.closed_by.full_name if shift.closed_by_id else ''
            events.append(journal_event(shift.closed_at, 'duty', 'Служебное закрытие' if shift.is_service_closed else 'Смена завершена',
                       closer or who, ' · '.join(filter(None, (who if closer and closer != who else '', shift.service_close_note)))))

    shift_edges = {value for s in shifts for value in (s.opened_at, s.closed_at) if value}
    for first, last, code, reason, who in timeline(start, end, now, tracking, shifts):
        length = (last - first).total_seconds()
        totals[code] += round((last - start).total_seconds()) - round((first - start).total_seconds())
        parts.append(dict(start=clock(first), end=clock(last), state=code, label=STATE_LABELS[code],
                          width=round(100 * length / seconds, 6), duration=duration(length), reason=reason, person=who))
        if who and first not in shift_edges and start < first < end:
            events.append(journal_event(first, 'connection', reason, who, 'Состояние рабочего окна'))
    for at, event, _ in slot_operations:
        events.append(event)
        marks.append(dict(left=percent(at), time=clock(at), title=event['title'], at=event['at']))
    events.sort(key=lambda e: e['at'])
    live = start <= now < end
    last_op = marks[-1]['time'] if marks else ''
    last_op_age = ''
    if live and marks:
        last_time = slot_operations[-1][0]
        last_op_age = duration((now - last_time).total_seconds())
    ticks = [dict(label=clock(start + timedelta(hours=h)), left=100 * h / 12) for h in range(0, 13, 2)]
    return dict(key=shift_type, label='Дневная смена' if shift_type == ShiftType.DAY else 'Ночная смена',
                start=clock(start), end=clock(end), staff=staff, parts=parts, events=events, marks=marks,
                ticks=ticks, count=len(marks), last_op=last_op, last_op_age=last_op_age, live=live,
                marker=percent(now) if live else None,
                totals=[dict(state=k, label=STATE_LABELS[k], seconds=totals[k], duration=duration(totals[k], exact=True))
                        for k in STATE_LABELS if totals[k] > 0])


def duty_report(selected_date, now=None):
    now = now or timezone.now()
    start, _ = production_shift_bounds(selected_date, ShiftType.DAY)
    _, end = production_shift_bounds(selected_date, ShiftType.NIGHT)
    shifts = list(dispatcher_shift_queryset(open_only=False).filter(opened_at__lt=end, opened_at__lte=now)
                  .filter(Q(closed_at__gt=start) | Q(closed_at__isnull=True))
                  .select_related('employee', 'opened_by', 'closed_by').prefetch_related('duty_windows').order_by('opened_at', 'pk'))
    tracking = DispatcherDutyTracking.objects.filter(pk=1).values_list('started_at', flat=True).first()
    actions = operations(start, min(end, now + timedelta(microseconds=1)), tracking, shifts)
    return dict(date=selected_date.isoformat(), as_of=business_localtime(now).strftime('%d.%m.%Y %H:%M'),
                now_clock=clock(now), live=start <= now < end,
                slots=[build_slot(selected_date, kind, now, tracking, shifts, actions) for kind in (ShiftType.DAY, ShiftType.NIGHT)])


@never_cache
@require_GET
def management_dispatcher_duty(request):
    if not request.session.get('employee_access_id'):
        login_url = reverse('login', query={'form': '1', 'next': request.get_full_path()})
        return redirect(login_url)
    auth = role_session_state(request)
    if (not auth['authenticated'] or not auth['is_active'] or auth['access'].role.code != 'manager'
            or getattr(request, 'observer_mode', False)):
        return HttpResponseForbidden('Для просмотра требуется роль руководства.')
    date_value = request.GET.get('date')
    selected = parse_filter_date(date_value) if date_value else production_work_date()
    if not selected or not 1 < selected.year < 9999:
        return HttpResponseBadRequest('Неверная дата смены.')
    report = duty_report(selected)
    if request.GET.get('format') == 'json':
        return JsonResponse(report)
    context = {'access': auth['access'], 'selected_date': selected, 'report': report}
    template = 'reports/includes/dispatcher_duty_slots.html' if request.GET.get('partial') == '1' else 'reports/dispatcher_duty.html'
    return render(request, template, context)
