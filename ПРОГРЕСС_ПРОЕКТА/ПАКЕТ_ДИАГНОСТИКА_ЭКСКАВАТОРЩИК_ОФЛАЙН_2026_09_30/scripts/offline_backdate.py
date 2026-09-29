from datetime import timedelta
from django.utils import timezone
from trips.models import Trip
past = timezone.now() - timedelta(hours=3)
n = Trip.objects.filter(truck_id__in=[1, 10], completed_at__gt=past).update(completed_at=past)
print('backdated', n)
