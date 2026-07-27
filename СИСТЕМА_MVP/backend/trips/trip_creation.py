from .models import Trip, TripStatus


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
        volume_m3=None,
        tonnage=None,
        loading_horizon=str(loading_horizon or '')[:64],
        loading_block=str(loading_block or '')[:64],
        transport_distance_km=transport_distance_km,
        downtime_text=str(downtime_text or '')[:255],
        note=str(note or '')[:1000],
        status=TripStatus.LOADED_WAITING_UNLOAD,
    )
