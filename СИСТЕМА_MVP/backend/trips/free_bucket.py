"""Domain helpers for a one-load free-bucket acceptance.

The helpers deliberately never touch ``HaulAssignment``: it is dispatcher
history and remains the primary assignment while a different excavator records
the actual temporary load.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.utils import timezone


FREE_BUCKET_DUMP_CARD_VISIBILITY = timedelta(
    seconds=getattr(settings, 'EXCAVATOR_FREE_BUCKET_DUMP_CARD_SECONDS', 300)
)


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


def free_bucket_dump_card_expires_at(trip):
    """Return the visual queue deadline without changing the Trip lifecycle."""
    try:
        acceptance = trip.free_bucket_acceptance
    except (AttributeError, ObjectDoesNotExist):
        acceptance = None
    if acceptance is None:
        return None
    anchor = trip.loaded_at or trip.created_at
    return anchor + FREE_BUCKET_DUMP_CARD_VISIBILITY if anchor else None


def free_bucket_dump_card_is_visible(trip, *, now=None):
    expires_at = free_bucket_dump_card_expires_at(trip)
    if expires_at is None:
        return True
    return expires_at > (now or timezone.now())


def free_bucket_dump_card_visibility_filter(*, now):
    """SQL projection for the independent five-minute free-bucket badge."""
    cutoff = now - FREE_BUCKET_DUMP_CARD_VISIBILITY
    return Q(
        free_bucket_acceptance__isnull=False,
        loaded_at__gt=cutoff,
    ) | Q(
        free_bucket_acceptance__isnull=False,
        loaded_at__isnull=True,
        created_at__gt=cutoff,
    )
