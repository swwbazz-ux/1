from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment, HaulAssignmentAction
from assignments.services import get_active_equipment_assignment
from downtimes.models import DowntimeEvent
from references.models import Equipment
from shifts.models import EmployeeShift
from trips.models import FreeBucketAcceptance, FreeBucketAcceptanceStatus, OPEN_TRIP_STATUSES, Trip, TripStatus
from users.models import EmployeeAccess

now = timezone.now()
access = EmployeeAccess.objects.select_related('employee').get(pk=3)
employee = access.employee
excavator = Equipment.objects.get(pk=54)

closed = EmployeeShift.objects.filter(employee=employee, closed_at__isnull=True).update(closed_at=now)
closed_eq = EmployeeShift.objects.filter(equipment=excavator, closed_at__isnull=True).update(closed_at=now)
trips = Trip.objects.filter(status__in=OPEN_TRIP_STATUSES).update(status=TripStatus.COMPLETED, completed_at=now)
fb = FreeBucketAcceptance.objects.exclude(
    status__in=(FreeBucketAcceptanceStatus.CLOSED, FreeBucketAcceptanceStatus.CANCELLED)
).update(status=FreeBucketAcceptanceStatus.CLOSED, closed_at=now)
dt = DowntimeEvent.objects.filter(equipment=excavator, ended_at__isnull=True).update(ended_at=now)

truck1 = Equipment.objects.get(pk=1)
own = HaulAssignment.objects.filter(truck=truck1, ended_at__isnull=True).exclude(excavator=excavator)
own.update(ended_at=now)
if not HaulAssignment.objects.filter(truck=truck1, excavator=excavator, status=AssignmentStatus.ACCEPTED, ended_at__isnull=True).exists():
    HaulAssignment.objects.create(
        truck=truck1, excavator=excavator, action=HaulAssignmentAction.ASSIGN,
        status=AssignmentStatus.ACCEPTED, accepted_at=now,
    )

work = get_active_equipment_assignment(employee, 'excavator_operator')
print('closed shifts', closed, closed_eq, 'trips', trips, 'free_bucket', fb, 'downtimes', dt)
print('work assignment ->', work.equipment_id if work else None)
print('truck1 open assignments', list(HaulAssignment.objects.filter(truck=truck1, ended_at__isnull=True).values_list('id', 'excavator_id', 'status')))
print('truck10 open assignments', list(HaulAssignment.objects.filter(truck_id=10, ended_at__isnull=True).values_list('id', 'excavator_id', 'status')))
