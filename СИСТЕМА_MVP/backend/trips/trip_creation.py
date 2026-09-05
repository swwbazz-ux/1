from decimal import Decimal

from django.core.exceptions import ValidationError

from references.models import Equipment, TruckCapacityRule

from .models import Trip, TripStatus


TRIP_CAPACITY_UNRESOLVED_MESSAGE = (
    'Для выбранных модели самосвала и породы не настроена кубатура.'
)
TRIP_DENSITY_UNRESOLVED_MESSAGE = (
    'Для выбранной породы не настроена плотность.'
)


def lock_trip_participant_equipment(*, excavator_id, truck_id):
    """Lock both trip participants in one deterministic order.

    Callers must already be inside ``transaction.atomic()``. The truck row is
    the shared serialization point with driver downtime actions, so a loaded
    trip and an incompatible waiting-for-loading event cannot be committed in
    opposite transactions.
    """
    participant_ids = tuple(sorted({excavator_id, truck_id}))
    locked_by_id = {
        equipment.pk: equipment
        for equipment in (
            Equipment.objects
            .select_for_update(of=('self',))
            .select_related('equipment_type', 'model')
            .filter(pk__in=participant_ids)
            .order_by('pk')
        )
    }
    if any(participant_id not in locked_by_id for participant_id in participant_ids):
        raise ValidationError('Техника для рейса больше недоступна.')
    return locked_by_id[excavator_id], locked_by_id[truck_id]


def calculate_trip_volume_and_tonnage(truck, rock_type):
    volume = None
    model = getattr(truck, 'model', None)
    if model and rock_type:
        rule = TruckCapacityRule.objects.filter(
            equipment_model=model,
            rock_type=rock_type,
        ).first()
        if rule:
            volume = rule.volume_m3
        elif model.body_volume_m3:
            volume = model.body_volume_m3
    if not volume or not rock_type or not rock_type.density:
        return volume, None
    tonnage = (Decimal(volume) * Decimal(rock_type.density)).quantize(
        Decimal('0.01')
    )
    return volume, tonnage


def resolve_required_trip_measurements(truck, rock_type):
    volume, tonnage = calculate_trip_volume_and_tonnage(truck, rock_type)
    if not volume:
        raise ValidationError(TRIP_CAPACITY_UNRESOLVED_MESSAGE)
    if tonnage is None:
        raise ValidationError(TRIP_DENSITY_UNRESOLVED_MESSAGE)
    return volume, tonnage


def create_loaded_waiting_unload_trip(
    *,
    assignment,
    excavator_operator,
    loading_shift,
    rock_type,
    dump_point,
    planned_volume_m3=None,
    loading_horizon='',
    loading_block='',
    transport_distance_km=None,
    downtime_text='',
    note='',
):
    """Create the single server-side state used after an excavator loads a truck."""
    volume_m3, tonnage = resolve_required_trip_measurements(
        assignment.truck,
        rock_type,
    )
    return Trip.objects.create(
        excavator=assignment.excavator,
        truck=assignment.truck,
        excavator_operator=excavator_operator,
        loading_shift=loading_shift,
        rock_type=rock_type,
        dump_point=dump_point,
        assigned_dump_point=dump_point,
        actual_dump_point=dump_point,
        planned_volume_m3=planned_volume_m3,
        volume_m3=volume_m3,
        tonnage=tonnage,
        loading_horizon=str(loading_horizon or '')[:64],
        loading_block=str(loading_block or '')[:64],
        transport_distance_km=transport_distance_km,
        downtime_text=str(downtime_text or '')[:255],
        note=str(note or '')[:1000],
        status=TripStatus.LOADED_WAITING_UNLOAD,
    )
