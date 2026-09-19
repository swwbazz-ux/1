"""Domain helpers for a one-load free-bucket acceptance.

The helpers deliberately never touch ``HaulAssignment``: it is dispatcher
history and remains the primary assignment while a different excavator records
the actual temporary load.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone


FREE_BUCKET_DUMP_CARD_VISIBILITY = timedelta(
    seconds=getattr(settings, 'EXCAVATOR_FREE_BUCKET_DUMP_CARD_SECONDS', 300)
)


def free_bucket_acceptance_expires_at(acceptance):
    """Return the server deadline only for the already-used visual card."""
    trip = getattr(acceptance, 'used_trip', None)
    anchor = (
        getattr(trip, 'loaded_at', None)
        or acceptance.used_at
        or getattr(trip, 'created_at', None)
    )
    return anchor + FREE_BUCKET_DUMP_CARD_VISIBILITY if anchor else None


def active_free_bucket_acceptance_filter(*, now=None):
    """Keep requests/acceptances until action; keep USED only for its card window.

    The five-minute window never cancels a pending driver request or an accepted
    one-load right.  It only retires the already-consumed Excavator card.
    """
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
    return Q(status__in=(
        FreeBucketAcceptanceStatus.REQUESTED,
        FreeBucketAcceptanceStatus.ACCEPTED,
    )) | (
        Q(status=FreeBucketAcceptanceStatus.USED)
        & used_time_is_recent
    )


def active_free_bucket_acceptance_for_truck(truck, *, for_update=False, now=None):
    """Return an unspent request/acceptance while the caller holds the truck lock."""
    from .models import FreeBucketAcceptance

    queryset = FreeBucketAcceptance.objects.select_related(
        'excavator', 'operator', 'loading_shift', 'requested_by', 'requesting_shift',
    ).filter(
        truck=truck,
    ).filter(
        active_free_bucket_acceptance_filter(now=now),
    ).order_by('-occurred_at', '-id')
    if for_update:
        queryset = queryset.select_for_update(of=('self',))
    return queryset.first()


@transaction.atomic
def reconcile_expired_free_bucket_acceptances(*, now=None):
    """Close only an already-used visual card; never cancel a request."""
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


def canonical_free_bucket_work_context_snapshot(excavator):
    """Freeze the target excavator's persisted work context for one load.

    Session/form fallbacks are deliberately excluded. A driver request may be
    delivered later, so only persisted active reference data is authoritative.
    """
    from assignments.models import ExcavatorDumpPointSetting, ExcavatorPlacement

    placement = (
        ExcavatorPlacement.objects.select_for_update(of=('self',))
        .select_related('work_rock_type', 'work_dump_point')
        .filter(excavator=excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
        .first()
    )
    if not placement:
        raise ValidationError('У выбранного экскаватора нет активного размещения.')
    rock = placement.work_rock_type
    if not rock or not rock.is_active:
        raise ValidationError('У выбранного экскаватора не задана действующая порода.')

    destinations = list(
        ExcavatorDumpPointSetting.objects.select_for_update(of=('self',))
        .select_related('dump_point')
        .filter(placement=placement, dump_point__is_active=True)
        .order_by('position', 'id')
    )
    dump_points = [
        {
            'id': item.dump_point_id,
            'name': str(item.dump_point),
            'transport_distance_km': (
                format(item.transport_distance_km, 'f')
                if item.transport_distance_km is not None else None
            ),
        }
        for item in destinations
    ]
    if not dump_points and placement.work_dump_point_id and placement.work_dump_point.is_active:
        dump_points.append({
            'id': placement.work_dump_point_id,
            'name': str(placement.work_dump_point),
            'transport_distance_km': (
                format(placement.transport_distance_km, 'f')
                if placement.transport_distance_km is not None else None
            ),
        })
    if not dump_points:
        raise ValidationError('У выбранного экскаватора нет действующей точки разгрузки.')

    return {
        'format_version': 1,
        'captured_at': timezone.now().isoformat(),
        'excavator_id': excavator.id,
        'placement_id': placement.id,
        'placement_updated_at': (
            placement.work_context_updated_at.isoformat()
            if placement.work_context_updated_at else None
        ),
        'rock_type_id': rock.id,
        'rock_type_name': str(rock),
        'loading_horizon': str(placement.loading_horizon or '')[:64],
        'loading_block': str(placement.loading_block or '')[:64],
        'dump_points': dump_points,
    }


def resolve_free_bucket_load_context(acceptance, payload):
    """Resolve a load strictly from the immutable acceptance snapshot."""
    from references.models import DumpPoint, RockType

    snapshot = acceptance.work_context_snapshot or {}
    try:
        snapshot_rock_id = int(snapshot.get('rock_type_id'))
        requested_rock_id = int(payload.get('rock_type_id') or payload.get('rock_type'))
        requested_dump_id = int(payload.get('dump_point_id'))
    except (TypeError, ValueError):
        raise ValidationError('Контекст погрузки свободного ковша заполнен не полностью.')
    if requested_rock_id != snapshot_rock_id:
        raise ValidationError('Порода погрузки отличается от сохранённого контекста свободного ковша.')

    dump_rows = snapshot.get('dump_points')
    if not isinstance(dump_rows, list):
        raise ValidationError('Сохранённый контекст свободного ковша повреждён.')
    selected_dump = next(
        (
            item for item in dump_rows
            if isinstance(item, dict) and str(item.get('id')) == str(requested_dump_id)
        ),
        None,
    )
    if not selected_dump:
        raise ValidationError('Точка разгрузки не входила в сохранённый контекст свободного ковша.')

    rock = RockType.objects.filter(pk=snapshot_rock_id).first()
    dump_point = DumpPoint.objects.select_for_update().filter(pk=requested_dump_id).first()
    if not rock or not dump_point:
        raise ValidationError('Справочные данные сохранённого контекста больше недоступны.')
    return {
        'rock_type': rock,
        'dump_point': dump_point,
        'loading_horizon': str(snapshot.get('loading_horizon') or '')[:64],
        'loading_block': str(snapshot.get('loading_block') or '')[:64],
        'transport_distance_km': selected_dump.get('transport_distance_km'),
    }


def free_bucket_snapshot_dump_points_for_trip(trip):
    """Return the immutable unload-point list for a free-bucket trip.

    ``None`` means that the trip is ordinary. An empty list means that the
    free-bucket snapshot is missing or damaged; callers must not fall back to
    the global directory in that case because it would silently broaden the
    choices saved for this particular load.
    """
    try:
        acceptance = trip.free_bucket_acceptance
    except (AttributeError, ObjectDoesNotExist):
        return None

    rows = (acceptance.work_context_snapshot or {}).get('dump_points')
    if not isinstance(rows, list):
        return []

    result = []
    seen_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            point_id = int(row.get('id'))
        except (TypeError, ValueError):
            continue
        if point_id <= 0 or point_id in seen_ids:
            continue
        seen_ids.add(point_id)
        result.append({
            'id': point_id,
            'name': str(row.get('name') or 'Точка разгрузки'),
            'transport_distance_km': row.get('transport_distance_km'),
        })
    return result


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


def cancel_free_bucket_acceptances_for_shift(shift, *, cancelled_at=None):
    """Cancel unspent temporary rights when either participating shift closes.

    ``USED`` rows belong to an already-created Trip and are deliberately left
    alone.  This helper deliberately never changes ``HaulAssignment``.
    """
    from .models import FreeBucketAcceptance, FreeBucketAcceptanceStatus

    with transaction.atomic():
        acceptance_ids = list(
            FreeBucketAcceptance.objects.select_for_update(of=('self',))
            .filter(
                Q(requesting_shift=shift) | Q(loading_shift=shift),
                status__in=(
                    FreeBucketAcceptanceStatus.REQUESTED,
                    FreeBucketAcceptanceStatus.ACCEPTED,
                ),
            )
            .order_by('id')
            .values_list('id', flat=True)
        )
        if not acceptance_ids:
            return 0
        return FreeBucketAcceptance.objects.filter(id__in=acceptance_ids).update(
            status=FreeBucketAcceptanceStatus.CANCELLED,
            cancelled_at=cancelled_at or timezone.now(),
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
