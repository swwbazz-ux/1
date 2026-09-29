from core.models import OfflineFieldEvent
from downtimes.models import DowntimeEvent
from shifts.models import EmployeeShift
from trips.models import Trip

print('--- shifts (employee 3, latest 4)')
for s in EmployeeShift.objects.filter(employee_id=3).order_by('-id')[:4]:
    print(s.id, 'opened', s.opened_at, 'closed', s.closed_at, 'equip', s.equipment_id)
print('--- offline events (latest 12)')
for e in OfflineFieldEvent.objects.order_by('-id')[:12]:
    print(e.id, e.event_type, e.status, e.error_code or '-', 'occ', e.occurred_at, 'rcv', e.received_at,
          'shift', getattr(e, 'shift_id', None), 'trip', e.trip_id)
print('--- downtimes excavator 54 (latest 4)')
for d in DowntimeEvent.objects.filter(equipment_id=54).order_by('-id')[:4]:
    print(d.id, d.reason_id, 'start', d.started_at, 'end', d.ended_at)
print('--- trips (latest 3)')
for t in Trip.objects.order_by('-id')[:3]:
    print(t.id, t.status, 'truck', t.truck_id, 'loaded', t.loaded_at, 'shift', t.loading_shift_id, 'cancelled', t.cancelled_at)
