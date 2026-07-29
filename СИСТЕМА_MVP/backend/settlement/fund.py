from dataclasses import dataclass


@dataclass(frozen=True)
class RoomSpec:
    dormitory_number: str
    floor: int
    room_number: int
    room_type: str
    transfer_status: str
    capacity: int
    corridor_side: str
    side_position: int


def _room_specs(
    *,
    dormitory_number,
    floor,
    room_numbers,
    transferred_numbers,
    itr_numbers=(),
):
    transferred = set(transferred_numbers)
    itr = set(itr_numbers)
    side_positions = {'left': 0, 'right': 0}
    specs = []

    for room_number in room_numbers:
        corridor_side = 'left' if room_number % 2 else 'right'
        side_positions[corridor_side] += 1
        is_itr = room_number in itr
        specs.append(
            RoomSpec(
                dormitory_number=str(dormitory_number),
                floor=floor,
                room_number=room_number,
                room_type='itr' if is_itr else 'standard',
                transfer_status=(
                    'transferred'
                    if room_number in transferred
                    else 'not_transferred'
                ),
                capacity=2 if is_itr else 6,
                corridor_side=corridor_side,
                side_position=side_positions[corridor_side],
            )
        )
    return specs


PHYSICAL_FUND_SPECS = tuple(
    _room_specs(
        dormitory_number='5',
        floor=1,
        room_numbers=range(1, 20),
        transferred_numbers=(*range(1, 18), 19),
    )
    + _room_specs(
        dormitory_number='5',
        floor=2,
        room_numbers=range(20, 39),
        transferred_numbers=(*range(20, 29), 36, 37, 38),
        itr_numbers=(36, 37, 38),
    )
    + _room_specs(
        dormitory_number='6',
        floor=1,
        room_numbers=range(1, 12),
        transferred_numbers=(1, 2, 3, 5, 6, 8, 9, 10, 11),
    )
    + _room_specs(
        dormitory_number='6',
        floor=2,
        room_numbers=range(40, 51),
        transferred_numbers=range(40, 48),
    )
)


def bed_specs(room_spec):
    dormitory_code = f'KIS{room_spec.dormitory_number}'
    room_code = (
        f'{dormitory_code}-F{room_spec.floor}-'
        f'R{room_spec.room_number:02d}'
    )
    if room_spec.room_type == 'itr':
        return tuple(
            {
                'stable_id': f'{room_code}-ITR{position}',
                'block': 'ITR',
                'position': position,
            }
            for position in range(1, 3)
        )

    return tuple(
        {
            'stable_id': f'{room_code}-{block}{position}',
            'block': block,
            'position': position,
        }
        for block in ('A', 'B')
        for position in range(1, 4)
    )


def expected_fund_totals():
    room_count = len(PHYSICAL_FUND_SPECS)
    bed_count = sum(spec.capacity for spec in PHYSICAL_FUND_SPECS)
    transferred_rooms = sum(
        spec.transfer_status == 'transferred'
        for spec in PHYSICAL_FUND_SPECS
    )
    transferred_beds = sum(
        spec.capacity
        for spec in PHYSICAL_FUND_SPECS
        if spec.transfer_status == 'transferred'
    )
    return {
        'rooms': room_count,
        'beds': bed_count,
        'transferred_rooms': transferred_rooms,
        'transferred_beds': transferred_beds,
        'not_transferred_rooms': room_count - transferred_rooms,
        'not_transferred_beds': bed_count - transferred_beds,
    }
