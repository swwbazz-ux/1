"""Отчёт руководства по личным дежурствам диспетчеров."""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from django import forms
from django.core.paginator import Paginator
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from core.production_time import production_shift_bounds, production_work_date
from shifts.models import ShiftType
from trips.dispatcher_duty import WindowState
from trips.models import DispatcherDutyTracking

from .dispatcher_duty import management_duty_view, period_records, timeline
from .dispatcher_duty_export import period_workbook_response


PRESENCE = {
    WindowState.VISIBLE: ('visible', 'Пульт на экране'),
    WindowState.HIDDEN: ('background', 'Пульт в фоне'),
    WindowState.CLOSED: ('away', 'Окна закрыты'),
    WindowState.SIGNED_OUT: ('away', 'Выполнен выход'),
    'unknown': ('unknown', 'Нет данных'),
}


class PeriodForm(forms.Form):
    date_from = forms.DateField(label='С', input_formats=['%Y-%m-%d'],
                                widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    date_to = forms.DateField(label='По', input_formats=['%Y-%m-%d'],
                              widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))

    def clean(self):
        values = super().clean()
        for key, value in list(values.items()):
            if not 1 < value.year < 9999:
                self.add_error(key, 'Проверьте год.')
        if self.errors:
            return values
        if values['date_from'] > values['date_to']:
            self.add_error('date_to', 'Конец периода должен быть не раньше его начала.')
        return values


@dataclass(frozen=True)
class Span:
    start: datetime
    end: datetime

    @property
    def seconds(self):
        return int((self.end.replace(microsecond=0) - self.start.replace(microsecond=0)).total_seconds())


def observed_totals(intervals):
    totals = Counter()
    for first, last, state, _, person in intervals:
        if person and state in PRESENCE:
            totals[PRESENCE[state][0]] += Span(first, last).seconds
    return totals


def background_percent(totals):
    total = sum(totals.values())
    if total > totals['unknown']:
        return round(100 * totals['background'] / total, 1)
    return None


def longest_visible_gap(intervals, times):
    """Only a continuously visible window can contribute to this indicator."""
    longest = None
    for first, last, state, _, _ in intervals:
        if state != WindowState.VISIBLE:
            continue
        inside = times[bisect_right(times, first):bisect_left(times, last)]
        for left, right in zip([first, *inside], [*inside, last]):
            if not longest or right - left > longest.end - longest.start:
                longest = Span(left, right)
    return longest


def post_gaps(shifts, start, stop):
    """Union of all recorded personal shifts, including relief and overlaps."""
    cursor = start
    gaps = []
    for shift in shifts:  # period_records orders by opening time
        if shift.opened_at >= stop:
            break
        if cursor < shift.opened_at:
            gaps.append(Span(cursor, shift.opened_at))
        cursor = max(cursor, min(shift.closed_at or stop, stop))
    if cursor < stop:
        gaps.append(Span(cursor, stop))
    return gaps


def personal_duty(shift, start, stop, now, tracking, commands):
    first = max(start, shift.opened_at)
    last = max(first, min(stop, shift.closed_at or stop))
    intervals = timeline(first, last, now, tracking, [shift])
    times = [event['when'] for event in commands]
    background = [Span(a, b) for a, b, state, _, _ in intervals if state == WindowState.HIDDEN]
    closed = shift.closed_at if shift.closed_at and shift.closed_at <= now else None
    closure = ''
    if closed:
        closure = shift.get_service_close_kind_display() if shift.is_service_closed else ''
        if not closure and shift.is_service_closed:
            closure = 'Служебное закрытие'
    totals = observed_totals(intervals)
    return dict(shift=shift, person=shift.employee.full_name, opened=shift.opened_at, closed=closed,
                closure=closure, kind={ShiftType.DAY: 'Дневная', ShiftType.NIGHT: 'Ночная'}.get(shift.shift_type, 'Личная'),
                span=Span(first, last), seconds=totals, background_percent=background_percent(totals),
                commands=commands, count=len(times),
                first_op=times[0] if times else None, last_op=times[-1] if times else None,
                longest_background=max(background, key=lambda span: span.end - span.start, default=None),
                visible_gap=longest_visible_gap(intervals, times),
                intervals=[(Span(a, b), PRESENCE[state][1]) for a, b, state, _, _ in intervals if state in PRESENCE])


def dispatcher_summary(rows, start, stop, now, tracking):
    totals = Counter()
    covered_until = start
    overlap = False
    for row in rows:
        overlap |= row['span'].start < covered_until
        covered_until = max(covered_until, row['span'].end)
        totals.update(row['seconds'])
    if overlap:
        # Legacy overlapping records for one person must not double their hours.
        totals = observed_totals(timeline(start, stop, now, tracking, [row['shift'] for row in rows]))
    return dict(person=rows[0]['person'], duties=len(rows), seconds=totals, background_percent=background_percent(totals),
                duration=sum(totals.values()), count=sum(row['count'] for row in rows))


def period_report(date_from, date_to, now=None):
    now = now or timezone.now()
    start, _ = production_shift_bounds(date_from, ShiftType.DAY)
    _, end = production_shift_bounds(date_to, ShiftType.NIGHT)
    stop = min(end, now)
    tracking = DispatcherDutyTracking.objects.filter(pk=1).values_list('started_at', flat=True).first()
    shifts, actions = period_records(start, end, now, tracking) if start <= stop else ([], [])
    commands = defaultdict(list)
    for at, event, owner in actions:
        commands[owner].append(dict(event, when=at))
    rows = [personal_duty(shift, start, stop, now, tracking, commands[shift.pk]) for shift in shifts]
    people = defaultdict(list)
    for row in rows:
        people[row['shift'].employee_id].append(row)
    summary = [dispatcher_summary(group, start, stop, now, tracking) for group in people.values()]
    observed_start = max(start, tracking) if tracking else None
    gaps = post_gaps(shifts, observed_start, stop) if observed_start and observed_start < stop else []
    return dict(date_from=date_from, date_to=date_to, start=start, end=end, stop=stop, now=now,
                tracking=tracking, future=start > now, rows=rows[::-1],
                people=sorted(summary, key=lambda x: x['person']), unassigned=commands[None],
                gaps=gaps, gap_seconds=sum(gap.seconds for gap in gaps), covers_post=bool(observed_start and observed_start < stop))


@management_duty_view
def management_dispatcher_period(request, access):
    now = timezone.now()
    today = production_work_date(now)
    defaults = {'date_from': (today - timedelta(days=6)).isoformat(), 'date_to': today.isoformat()}
    supplied = 'date_from' in request.GET or 'date_to' in request.GET
    form = PeriodForm(request.GET if supplied else defaults)
    context = {'access': access, 'selected_date': today, 'form': form}
    if form.is_valid():
        report = period_report(**form.cleaned_data, now=now)
        if request.GET.get('export') == 'xlsx':
            return period_workbook_response(report)
        query = {key: value.isoformat() for key, value in form.cleaned_data.items()}
        path = 'management_dispatcher_period'
        context.update(report=report, page=Paginator(report['rows'], 25).get_page(request.GET.get('page')),
                       period_url=reverse(path, query=query), export_url=reverse(path, query={**query, 'export': 'xlsx'}))
    return render(request, 'reports/dispatcher_duty_period.html', context, status=200 if form.is_valid() else 400)
