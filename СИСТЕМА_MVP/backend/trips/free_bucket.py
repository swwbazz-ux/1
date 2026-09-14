"""Domain helpers for a one-load free-bucket acceptance.

The helpers deliberately never touch ``HaulAssignment``: it is dispatcher
history and remains the primary assignment while a different excavator records
the actual temporary load.
"""

from django.utils import timezone


def active_free_bucket_acceptance_for_truck(truck, *, for_update=False):
    """Return an unspent acceptance while the caller holds the truck lock."""
    from .models import FreeBucketAcceptance, FreeBucketAcceptanceStatus

    queryset = FreeBucketAcceptance.objects.select_related('excavator', 'operator', 'loading_shift').filter(
        truck=truck,
        status__in=[FreeBucketAcceptanceStatus.ACCEPTED, FreeBucketAcceptanceStatus.USED],
    ).order_by('-occurred_at', '-id')
    if for_update:
        queryset = queryset.select_for_update(of=('self',))
    return queryset.first()


def close_free_bucket_acceptance_for_trip(trip, *, closed_at=None):
    """Close the consumed acceptance without restoring a one-shot right."""
    from .models import FreeBucketAcceptance, FreeBucketAcceptanceStatus

    return FreeBucketAcceptance.objects.filter(
        used_trip=trip,
        status=FreeBucketAcceptanceStatus.USED,
    ).update(
        status=FreeBucketAcceptanceStatus.CLOSED,
        closed_at=closed_at or timezone.now(),
    )
