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
# Срок жизни включённого свободного ковша: с момента включения водителем и до
# первой погрузки или отмены, но не дольше этого окна. Приём машинистом окно
# не продлевает — отсчёт идёт от включения (решение пользователя 20.09.2026).
FREE_BUCKET_REQUEST_TTL = timedelta(
    seconds=getattr(settings, 'FREE_BUCKET_REQUEST_TTL_SECONDS', 600)
)


def free_bucket_reservation_expires_at(acceptance):
    """Return the immutable deadline of the original ten-minute reservation."""
    if acceptance is None or acceptance.occurred_at is None:
        return None
    return acceptance.occurred_at + FREE_BUCKET_REQUEST_TTL


def free_bucket_acceptance_expires_at(acceptance):
    """Return the operational deadline for a reservation or used visual card."""
    from .models import FreeBucketAcceptanceStatus

    if acceptance.status in {
        FreeBucketAcceptanceStatus.REQUESTED,
        FreeBucketAcceptanceStatus.ACCEPTED,
    }:
        return free_bucket_reservation_expires_at(acceptance)
    trip = getattr(acceptance, 'used_trip', None)
    anchor = (
        getattr(trip, 'loaded_at', None)
        or acceptance.used_at
        or getattr(trip, 'created_at', None)
    )
    return anchor + FREE_BUCKET_DUMP_CARD_VISIBILITY if anchor else None


def active_free_bucket_acceptance_filter(*, now=None):
    """Keep a ten-minute reservation or a used five-minute visual card.

    An unspent right also dies with its own shift.  Normally
    ``cancel_free_bucket_acceptances_for_shift`` does that at shift close, but a
    row whose shift ended without that cleanup (crash, lost request, a shift
    closed by another path) used to stay "active" forever: the truck kept
    showing as taken under a free bucket on the dispatcher board and in the
    Excavator app, and neither side could clear it.  The right belongs to the
    shift it was requested in, so an ended or missing shift means it is spent.
    ``USED`` rows are deliberately left alone here — they belong to a real Trip.
    """
    from .models import FreeBucketAcceptanceStatus

    now = now or timezone.now()
    cutoff = now - FREE_BUCKET_DUMP_CARD_VISIBILITY
    # Включённый, но не использованный ковш живёт не дольше FREE_BUCKET_REQUEST_TTL
    # с момента включения (occurred_at — момент выбора на устройстве водителя).
    request_is_fresh = Q(
        occurred_at__lte=now,
        occurred_at__gt=now - FREE_BUCKET_REQUEST_TTL,
    )
    used_time_is_recent = Q(
        used_trip__loaded_at__lte=now,
        used_trip__loaded_at__gt=cutoff,
    ) | Q(
        used_trip__loaded_at__isnull=True,
        used_at__lte=now,
        used_at__gt=cutoff,
    ) | Q(
        used_trip__loaded_at__isnull=True,
        used_at__isnull=True,
        used_trip__created_at__lte=now,
        used_trip__created_at__gt=cutoff,
    )
    # Заявку может завести и водитель (requesting_shift), и машинист
    # экскаватора (loading_shift). Право живо, пока жива хотя бы одна из своих
    # смен; без единой привязки к смене не трогаем — такие записи заводят
    # другие пути, и гадать за них здесь нельзя.
    # Every participant shift bound to the right must still be open. Closing
    # either the requesting Driver shift or the accepting Excavator shift ends
    # an unused one-load reservation; null means that side never participated.
    shift_is_alive = (
        (
            Q(requesting_shift__isnull=True)
            | Q(
                requesting_shift__opened_at__lte=now,
                requesting_shift__closed_at__isnull=True,
            )
            | Q(
                requesting_shift__opened_at__lte=now,
                requesting_shift__closed_at__gt=now,
            )
        )
        & (
            Q(loading_shift__isnull=True)
            | Q(
                loading_shift__opened_at__lte=now,
                loading_shift__closed_at__isnull=True,
            )
            | Q(
                loading_shift__opened_at__lte=now,
                loading_shift__closed_at__gt=now,
            )
        )
    )
    return (
        Q(status__in=(
            FreeBucketAcceptanceStatus.REQUESTED,
            FreeBucketAcceptanceStatus.ACCEPTED,
        )) & shift_is_alive & request_is_fresh
    ) | (
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


def finish_free_bucket_hint_for_factual_load(acceptance, *, loaded_at):
    """Retire an older free-bucket hint without vetoing a factual load.

    A REQUESTED/ACCEPTED row is only an operational hint.  If the loader taps
    after that hint, the tap wins and the unused row is cancelled.  A USED row
    belongs to the preceding trip and is closed when a later load supersedes
    that trip.  A hint created after the tap is historical future state and is
    deliberately left untouched.
    """
    from .models import FreeBucketAcceptanceStatus

    if acceptance.status == FreeBucketAcceptanceStatus.USED:
        used_trip = getattr(acceptance, 'used_trip', None)
        anchor = acceptance.used_at or getattr(used_trip, 'loaded_at', None) or acceptance.occurred_at
    else:
        anchor = acceptance.accepted_at or acceptance.occurred_at
    if anchor and anchor > loaded_at:
        return False
    if acceptance.status in {
        FreeBucketAcceptanceStatus.REQUESTED,
        FreeBucketAcceptanceStatus.ACCEPTED,
    }:
        acceptance.status = FreeBucketAcceptanceStatus.CANCELLED
        acceptance.cancelled_at = loaded_at
        acceptance.save(update_fields=['status', 'cancelled_at'])
        return True
    if acceptance.status == FreeBucketAcceptanceStatus.USED and acceptance.used_trip_id:
        close_free_bucket_acceptance_for_trip(acceptance.used_trip, closed_at=loaded_at)
        return True
    return False


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


def expire_stale_free_bucket_requests(truck, *, now=None):
    """Persist the exact TTL boundary for stale unaccepted Driver requests.

    The active-state filter already hides a REQUESTED reservation once its
    ten-minute window ends.  Keeping that row non-terminal, however, leaves a
    partial-unique slot occupied and breaks the release recovery path in which
    a delayed factual Driver load revives the same request.  Only REQUESTED
    rows are retired here: ACCEPTED and USED rows have their own lifecycle in
    the current worker-truth workflow.  The caller already holds the truck
    transaction/lock in the online path; the explicit row lock also keeps the
    helper safe for the isolated reconciliation/test path.
    """
    from core.models import bump_operational_state
    from .models import FreeBucketAcceptance, FreeBucketAcceptanceStatus

    now = now or timezone.now()
    expired = list(
        FreeBucketAcceptance.objects
        .select_for_update(of=('self',))
        .filter(
            truck=truck,
            status=FreeBucketAcceptanceStatus.REQUESTED,
            occurred_at__lte=now - FREE_BUCKET_REQUEST_TTL,
        )
        .order_by('id')
    )
    if not expired:
        return []
    for acceptance in expired:
        acceptance.status = FreeBucketAcceptanceStatus.CANCELLED
        acceptance.cancelled_at = acceptance.occurred_at + FREE_BUCKET_REQUEST_TTL
    FreeBucketAcceptance.objects.bulk_update(expired, ['status', 'cancelled_at'])
    bump_operational_state(
        'FreeBucketAcceptance:expired',
        event_type='trip_changed',
        object_type='FreeBucketAcceptance',
        payload={
            'action': 'free_bucket_expired',
            'acceptance_ids': [item.id for item in expired],
            'truck_ids': [truck.id],
            'excavator_ids': sorted({item.excavator_id for item in expired}),
        },
    )
    return expired


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


def _factual_free_bucket_work_context_snapshot(acceptance, payload, load_context):
    """Build the immutable context that was visible for the factual load.

    New clients send the complete dump-point drum. Older clients only send the
    selected point; for them we preserve the acceptance list but replace/add
    the selected point so a later Driver action can never be constrained to an
    obsolete route.
    """
    previous = acceptance.work_context_snapshot or {}
    raw_rows = payload.get('dump_points_snapshot')
    rows = []
    if isinstance(raw_rows, list):
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            try:
                point_id = int(raw.get('id'))
            except (TypeError, ValueError):
                continue
            if point_id <= 0 or any(item['id'] == point_id for item in rows):
                continue
            rows.append({
                'id': point_id,
                'name': str(raw.get('name') or 'Точка разгрузки')[:255],
                'transport_distance_km': raw.get('transport_distance_km'),
            })
    else:
        rows = [
            dict(raw)
            for raw in (previous.get('dump_points') or [])
            if isinstance(raw, dict)
        ]

    selected = {
        'id': load_context['dump_point'].id,
        'name': str(load_context['dump_point']),
        'transport_distance_km': load_context['transport_distance_km'],
    }
    selected_index = next(
        (index for index, row in enumerate(rows) if str(row.get('id')) == str(selected['id'])),
        None,
    )
    if selected_index is None:
        rows.append(selected)
    else:
        rows[selected_index] = selected

    return {
        **previous,
        'format_version': 2,
        'source': 'load_tap',
        'excavator_id': acceptance.excavator_id,
        'rock_type_id': load_context['rock_type'].id,
        'rock_type_name': str(load_context['rock_type']),
        'loading_horizon': load_context['loading_horizon'],
        'loading_block': load_context['loading_block'],
        'selected_dump_point_id': load_context['dump_point'].id,
        'dump_points': rows,
    }


def resolve_free_bucket_load_context(acceptance, payload):
    """Resolve the factual load from the worker's immutable tap payload.

    The acceptance snapshot remains audit/fallback data for older clients. It
    must never veto a later load whose screen showed a different valid route or
    face context: the tap snapshot is the production fact.
    """
    from references.models import DumpPoint, RockType

    snapshot = acceptance.work_context_snapshot or {}
    try:
        requested_rock_id = int(payload.get('rock_type_id') or payload.get('rock_type'))
        requested_dump_id = int(payload.get('dump_point_id'))
    except (TypeError, ValueError):
        raise ValidationError('Контекст погрузки свободного ковша заполнен не полностью.')

    dump_rows = snapshot.get('dump_points')
    if not isinstance(dump_rows, list):
        dump_rows = []
    selected_dump = next(
        (
            item for item in dump_rows
            if isinstance(item, dict) and str(item.get('id')) == str(requested_dump_id)
        ),
        None,
    )

    rock = RockType.objects.filter(pk=requested_rock_id).first()
    dump_point = DumpPoint.objects.select_for_update().filter(pk=requested_dump_id).first()
    if not rock or not dump_point:
        raise ValidationError('Справочные данные из отметки погрузки больше недоступны.')
    transport_distance = (
        payload.get('transport_distance_km')
        if 'transport_distance_km' in payload
        else (selected_dump or {}).get('transport_distance_km')
    )
    load_context = {
        'rock_type': rock,
        'dump_point': dump_point,
        'loading_horizon': str(
            payload.get('loading_horizon')
            if 'loading_horizon' in payload else snapshot.get('loading_horizon') or ''
        )[:64],
        'loading_block': str(
            payload.get('loading_block')
            if 'loading_block' in payload else snapshot.get('loading_block') or ''
        )[:64],
        'transport_distance_km': transport_distance,
    }
    acceptance.work_context_snapshot = _factual_free_bucket_work_context_snapshot(
        acceptance,
        payload,
        load_context,
    )
    return load_context


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


def restore_free_bucket_acceptance_after_trip_cancel(
    trip,
    *,
    cancelled_at=None,
    as_of=None,
):
    """Return a cancelled temporary load to its original reservation window.

    The reservation keeps the deadline that started at ``occurred_at``.  A
    swipe-back before that deadline reopens the same acceptance; it never starts
    another ten-minute window.  If the deadline/participant shift already ended
    (or another cycle is active), the historical acceptance stays terminal.
    """
    from .models import FreeBucketAcceptance, FreeBucketAcceptanceStatus

    cancelled_at = cancelled_at or timezone.now()
    as_of = as_of or timezone.now()
    acceptance = (
        FreeBucketAcceptance.objects.select_for_update(of=('self',))
        .select_related('requesting_shift', 'loading_shift')
        .filter(
            used_trip=trip,
            status__in=(
                FreeBucketAcceptanceStatus.USED,
                FreeBucketAcceptanceStatus.CLOSED,
            ),
        )
        .first()
    )
    if acceptance is None:
        return None, False, None

    deadline = free_bucket_reservation_expires_at(acceptance)
    shift_boundaries = [
        bound_shift.closed_at
        for bound_shift in (acceptance.requesting_shift, acceptance.loading_shift)
        if bound_shift and bound_shift.closed_at
    ]
    later_cycles = list(
        FreeBucketAcceptance.objects.select_for_update(of=('self',))
        .filter(truck=acceptance.truck)
        .exclude(pk=acceptance.pk)
        .filter(
            Q(occurred_at__gt=acceptance.occurred_at)
            | Q(occurred_at=acceptance.occurred_at, pk__gt=acceptance.pk)
        )
        .order_by('occurred_at', 'id')
    )
    later_cycle_at = later_cycles[0].occurred_at if later_cycles else None
    terminal_at = min([
        deadline,
        *shift_boundaries,
        *([later_cycle_at] if later_cycle_at else []),
    ])
    can_restore = bool(
        cancelled_at < terminal_at
        and as_of < terminal_at
        and not later_cycles
    )
    if can_restore:
        acceptance.status = FreeBucketAcceptanceStatus.ACCEPTED
        acceptance.cancelled_at = None
        acceptance.used_at = None
        acceptance.used_trip = None
        acceptance.closed_at = None
        acceptance.save(update_fields=[
            'status', 'cancelled_at', 'used_at', 'used_trip', 'closed_at',
        ])
        return acceptance, True, terminal_at

    if acceptance.status == FreeBucketAcceptanceStatus.USED:
        acceptance.status = FreeBucketAcceptanceStatus.CLOSED
        acceptance.closed_at = terminal_at
        acceptance.save(update_fields=['status', 'closed_at'])
    elif acceptance.closed_at is None or terminal_at < acceptance.closed_at:
        acceptance.closed_at = terminal_at
        acceptance.save(update_fields=['closed_at'])
    return acceptance, False, terminal_at


def cancel_free_bucket_acceptances_for_shift(shift, *, cancelled_at=None):
    """Cancel unspent temporary rights when either participating shift closes.

    ``USED`` rows belong to an already-created Trip and are deliberately left
    alone.  This helper deliberately never changes ``HaulAssignment``.
    """
    from .models import FreeBucketAcceptance, FreeBucketAcceptanceStatus

    with transaction.atomic():
        acceptances = list(
            FreeBucketAcceptance.objects.select_for_update(of=('self',))
            .filter(
                Q(requesting_shift=shift) | Q(loading_shift=shift),
                status__in=(
                    FreeBucketAcceptanceStatus.REQUESTED,
                    FreeBucketAcceptanceStatus.ACCEPTED,
                ),
            )
            .order_by('id')
        )
        if not acceptances:
            return 0
        shift_boundary = cancelled_at or timezone.now()
        for acceptance in acceptances:
            acceptance.status = FreeBucketAcceptanceStatus.CANCELLED
            acceptance.cancelled_at = min(
                shift_boundary,
                acceptance.occurred_at + FREE_BUCKET_REQUEST_TTL,
            )
            acceptance.save(update_fields=['status', 'cancelled_at'])
        return len(acceptances)


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
