"""Print a markdown timeline (phone time vs server time) for the test shift of employee 3."""
import os
import sys

from core.models import OfflineFieldEvent
from downtimes.models import DowntimeEvent
from shifts.models import EmployeeShift
from trips.models import Trip

label = os.environ.get('STAND_LABEL', 'stand')
out = []
shift = EmployeeShift.objects.filter(employee_id=3).order_by('-id').first()


def ts(value):
    return value.strftime('%H:%M:%S.%f')[:-3] if value else '—'


def delta(a, b):
    if not a or not b:
        return '—'
    return f'{(b - a).total_seconds():+.1f} с'


out.append(f'## {label}\n')
out.append(f'Смена #{shift.id}: открыта {shift.opened_at:%Y-%m-%d} {ts(shift.opened_at)} UTC, '
           f'закрыта {ts(shift.closed_at) if shift.closed_at else "НЕТ (на сервере осталась открытой)"}\n')
out.append('### Квитанции очереди (OfflineFieldEvent)\n')
out.append('| # | событие | статус | код | время телефона (occurred_at) | принято сервером | задержка |')
out.append('|---|---|---|---|---|---|---|')
events = OfflineFieldEvent.objects.filter(shift_id=shift.id).order_by('id')
extra = OfflineFieldEvent.objects.filter(shift_id__isnull=True, received_at__gte=shift.opened_at).order_by('id')
for e in sorted(list(events) + [x for x in extra if x.actor_id == 3], key=lambda x: x.id):
    out.append(f'| {e.id} | {e.event_type} | {e.status} | {e.error_code or "—"} | {ts(e.occurred_at)} | '
               f'{ts(e.received_at)} | {delta(e.occurred_at, e.received_at)} |')
out.append('\n### Простои экскаватора в базе (DowntimeEvent, equipment 54)\n')
out.append('| # | причина | начало в базе | конец в базе |')
out.append('|---|---|---|---|')
for d in DowntimeEvent.objects.filter(equipment_id=54, started_at__gte=shift.opened_at).select_related('reason').order_by('id'):
    out.append(f'| {d.id} | {d.reason_id} {d.reason.name} | {ts(d.started_at)} | {ts(d.ended_at)} |')
out.append('\n### Рейсы смены\n')
out.append('| # | статус | самосвал | погрузка | отмена |')
out.append('|---|---|---|---|---|')
for t in Trip.objects.filter(loading_shift_id=shift.id).order_by('id'):
    out.append(f'| {t.id} | {t.status} | {t.truck_id} | {ts(t.loaded_at)} | {ts(t.cancelled_at)} |')
sys.stdout.buffer.write(('\n'.join(out) + '\n').encode('utf-8'))
