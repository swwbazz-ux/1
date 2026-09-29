"""Full timeline of one offline run: every receipt of employee 3 since SINCE (UTC ISO), shifts, downtimes, trips."""
import os
import sys
from datetime import datetime, timezone as tz

from core.models import OfflineFieldEvent
from downtimes.models import DowntimeEvent
from shifts.models import EmployeeShift
from trips.models import Trip

since = datetime.fromisoformat(os.environ['SINCE']).replace(tzinfo=tz.utc)
label = os.environ.get('STAND_LABEL', 'stand')
out = []


def ts(value):
    return value.strftime('%H:%M:%S.%f')[:-3] if value else '—'


def delta(a, b):
    if not a or not b:
        return '—'
    return f'{(b - a).total_seconds():+.1f} с'


out.append(f'## {label} (время UTC, прогон с {ts(since)})\n')
out.append('### Квитанции очереди (OfflineFieldEvent, сотрудник 3)\n')
out.append('| # | событие | статус | код | смена | local_shift_id | время телефона | получено сервером | задержка |')
out.append('|---|---|---|---|---|---|---|---|---|')
for e in OfflineFieldEvent.objects.filter(actor_id=3, occurred_at__gte=since).order_by('occurred_at', 'id'):
    local = ''
    payload = getattr(e, 'payload', None) or {}
    if isinstance(payload, dict):
        local = (payload.get('local_shift_id') or '')[-6:]
    out.append(f'| {e.id} | {e.event_type} | {e.status} | {e.error_code or "—"} | {e.shift_id or "—"} | '
               f'{local or "—"} | {ts(e.occurred_at)} | {ts(e.received_at)} | {delta(e.occurred_at, e.received_at)} |')
out.append('\n### Смены сотрудника 3\n')
out.append('| # | открыта | закрыта | топливо/моточасы начала | конца |')
out.append('|---|---|---|---|---|')
for s in EmployeeShift.objects.filter(employee_id=3, opened_at__gte=since).order_by('id'):
    start = f'{getattr(s, "start_fuel_percent", "?")}/{getattr(s, "start_engine_hours", "?")}'
    end = f'{getattr(s, "end_fuel_percent", "?")}/{getattr(s, "end_engine_hours", "?")}'
    out.append(f'| {s.id} | {ts(s.opened_at)} | {ts(s.closed_at) if s.closed_at else "ОТКРЫТА"} | {start} | {end} |')
out.append('\n### Простои экскаватора 54\n')
out.append('| # | причина | начало | конец | длительность | создан |')
out.append('|---|---|---|---|---|---|')
for d in DowntimeEvent.objects.filter(equipment_id=54, started_at__gte=since).select_related('reason').order_by('id'):
    dur = f'{(d.ended_at - d.started_at).total_seconds():.1f} с' if d.ended_at else 'идёт'
    created = getattr(d, 'created_at', None)
    out.append(f'| {d.id} | {d.reason_id} {d.reason.name} | {ts(d.started_at)} | {ts(d.ended_at)} | {dur} | {ts(created)} |')
out.append('\n### Рейсы самосвала 1\n')
out.append('| # | статус | смена погрузки | погрузка | отмена |')
out.append('|---|---|---|---|---|')
for t in Trip.objects.filter(truck_id=1, loaded_at__gte=since).order_by('id'):
    out.append(f'| {t.id} | {t.status} | {t.loading_shift_id} | {ts(t.loaded_at)} | {ts(getattr(t, "cancelled_at", None))} |')
sys.stdout.buffer.write(('\n'.join(out) + '\n').encode('utf-8'))
