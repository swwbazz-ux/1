"""Domain helpers for a one-load free-bucket acceptance.

The helpers deliberately never touch ``HaulAssignment``: it is dispatcher
history and remains the primary assignment while a different excavator records
the actual temporary load.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone


FREE_BUCKET_DUMP_CARD_VISIBILITY = timedelta(
    seconds=getattr(settings, 'EXCAVATOR_FREE_BUCKET_DUMP_CARD_SECONDS', 300)
)


def free_bucket_acceptance_expires_at(acceptance):
    """Return the server deadline for the temporary post-load state."""
    trip = getattr(acceptance, 'used_trip', None)
    anchor = (
        getattr(trip, 'loaded_at', None)
        or acceptance.used_at
        or getattr(trip, 'created_at', None)
    )
    return anchor + FREE_BUCKET_DUMP_CARD_VISIBILITY if anchor else None


def active_free_bucket_acceptance_filter(*, now=None):
    """Keep ACCEPTED active; keep USED active for only five minutes."""
    from .models import FreeBucketAcceptanceStatus

    cutoff = (now or timezone.now()) - FREE_BUCKET_DUMP_CARD_VISIBILITY
    used_time_is_recent = Q(used_trip__loaded_at__gt=cutoff) | Q(
        used_trip__loaded_at__isnull=True,
        used_at__gt=cutoff,
    ) | Q(
        used_trip__loaded_at__isnull=True,
        used_at__isnull=True,
        used_trip__created_at__gt=cutoff,
    )
    return Q(status=FreeBucketAcceptanceStatus.ACCEPTED) | (
        Q(status=FreeBucketAcceptanceStatus.USED)
        & used_time_is_recent
    )


def active_free_bucket_acceptance_for_truck(truck, *, for_update=False, now=None):
    """Return temporary authority only while its server deadline is current."""
    from .models import FreeBucketAcceptance

    queryset = FreeBucketAcceptance.objects.select_related('excavator', 'operator', 'loading_shift').filter(
        truck=truck,
    ).filter(
        active_free_bucket_acceptance_filter(now=now),
    ).order_by('-occurred_at', '-id')
    if for_update:
        queryset = queryset.select_for_update(of=('self',))
    return queryset.first()


@transaction.atomic
def reconcile_expired_free_bucket_acceptances(*, now=None):
    """Close only expired temporary state; never mutate its Trip or assignment."""
    from core.models import bump_operational_state, lock_production_state
    from .models import FreeBucketAcceptance, FreeBucketAcceptanceStatus

    now = now or timezone.now()
    lock_production_state()
    expired = []
    for acceptance in (
        FreeBucketAcceptance.objects
        .select_for_update(of=('self',))
        .select_related('used_trip')
        .filter(status=FreeBucketAcceptanceStatus.USED)
        .order_by('id')
    ):
        expires_at = free_bucket_acceptance_expires_at(acceptance)
        if expires_at is None or expires_at > now:
            continue
        acceptance.status = FreeBucketAcceptanceStatus.CLOSED
        acceptance.closed_at = expires_at
        expired.append(acceptance)
    if not expired:
        return 0
    FreeBucketAcceptance.objects.bulk_update(expired, ['status', 'closed_at'])
    bump_operational_state(
        'FreeBucketAcceptance:expired',
        event_type='trip_changed',
        object_type='FreeBucketAcceptance',
        payload={
            'action': 'free_bucket_expired',
            'acceptance_ids': [item.id for item in expired],
            'truck_ids': sorted({item.truck_id for item in expired}),
            'excavator_ids': sorted({item.excavator_id for item in expired}),
        },
    )
    return len(expired)


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
    return free_bucket_acceptance_expires_at(acceptance)


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
