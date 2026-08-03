from collections import defaultdict

from django.db.models import Prefetch
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from users.active_role import role_session_state
from users.models import Employee, EmployeeAccess

from .models import PhysicalBed, PhysicalRoom


def settlement_clerk_access_from_request(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            id=access_id,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
            role__is_active=True,
            role__code='settlement_clerk',
        )
        .first()
    )
    if not access:
        return None
    session_state = role_session_state(request, access)
    if not session_state['authenticated'] or not session_state['is_active']:
        return None
    return access


def _room_view(room):
    blocks = defaultdict(list)
    for bed in room.beds.all():
        blocks[bed.block].append(bed)
    block_order = (
        (PhysicalBed.Block.ITR,)
        if room.room_type == PhysicalRoom.RoomType.ITR
        else (PhysicalBed.Block.A, PhysicalBed.Block.B)
    )
    room.view_blocks = [
        {
            'code': block,
            'label': dict(PhysicalBed.Block.choices)[block],
            'beds': blocks[block],
        }
        for block in block_order
    ]
    room.search_text = ' '.join(
        [
            f'КИС-{room.dormitory.number}',
            f'этаж {room.floor}',
            f'комната {room.number}',
            *(bed.stable_id for bed in room.beds.all()),
        ]
    ).lower()
    return room


def _floor_view(floor_number, rooms):
    left = {
        room.side_position: _room_view(room)
        for room in rooms
        if room.corridor_side == PhysicalRoom.CorridorSide.LEFT
    }
    right = {
        room.side_position: _room_view(room)
        for room in rooms
        if room.corridor_side == PhysicalRoom.CorridorSide.RIGHT
    }
    row_count = max([*left.keys(), *right.keys()], default=0)
    return {
        'number': floor_number,
        'rooms': rooms,
        'rows': [
            {
                'left': left.get(position),
                'right': right.get(position),
            }
            for position in range(1, row_count + 1)
        ],
        'transferred_count': sum(room.is_transferred for room in rooms),
        'room_count': len(rooms),
    }


def _dormitory_views(rooms):
    by_dormitory = defaultdict(lambda: defaultdict(list))
    for room in rooms:
        by_dormitory[room.dormitory][room.floor].append(room)

    result = []
    for dormitory in sorted(
        by_dormitory,
        key=lambda item: (len(item.number), item.number),
    ):
        floors = by_dormitory[dormitory]
        dormitory_rooms = [
            room
            for floor_rooms in floors.values()
            for room in floor_rooms
        ]
        result.append({
            'model': dormitory,
            'label': f'КИС-{dormitory.number}',
            'floors': [
                _floor_view(floor_number, floors[floor_number])
                for floor_number in sorted(floors)
            ],
            'room_count': len(dormitory_rooms),
            'bed_count': sum(room.capacity for room in dormitory_rooms),
            'transferred_rooms': sum(
                room.is_transferred
                for room in dormitory_rooms
            ),
            'transferred_beds': sum(
                room.capacity
                for room in dormitory_rooms
                if room.is_transferred
            ),
        })
    return result


@require_GET
def settlement_map_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        if request.session.get('employee_access_id'):
            return redirect('role_home')
        return redirect('login')

    rooms = list(
        PhysicalRoom.objects
        .select_related('dormitory')
        .prefetch_related(
            Prefetch(
                'beds',
                queryset=PhysicalBed.objects.order_by('block', 'position'),
            )
        )
        .filter(dormitory__number__in=['5', '6'])
        .order_by(
            'dormitory__number',
            'floor',
            'corridor_side',
            'side_position',
            'number',
        )
    )
    dormitories = _dormitory_views(rooms)
    total_beds = sum(room.capacity for room in rooms)
    transferred_rooms = sum(room.is_transferred for room in rooms)
    transferred_beds = sum(
        room.capacity
        for room in rooms
        if room.is_transferred
    )

    return render(
        request,
        'settlement/clerk_map.html',
        {
            'access': access,
            'dormitories': dormitories,
            'current_date': timezone.localdate(),
            'summary': {
                'rooms': len(rooms),
                'beds': total_beds,
                'transferred_rooms': transferred_rooms,
                'transferred_beds': transferred_beds,
                'not_transferred_rooms': len(rooms) - transferred_rooms,
                'not_transferred_beds': total_beds - transferred_beds,
            },
        },
    )
