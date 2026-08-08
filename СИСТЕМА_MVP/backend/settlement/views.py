import json
from collections import defaultdict
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from assignments.models import AssignmentStatus, EquipmentAssignment
from shifts.models import EmployeeShift
from users.active_role import role_session_state
from users.models import Employee, EmployeeAccess
from users.role_apps import (
    get_role_app,
    role_app_manifest_response,
    role_app_service_worker_response,
)

from .models import EmployeeBedOccupancy, PhysicalBed, PhysicalRoom
from .services import (
    current_roster_resolution,
    effective_occupancy_at_q,
    relocate_employee_to_bed,
    release_employee_from_bed,
    settle_employee_on_bed,
    unsettled_current_roster_employees,
)


UNKNOWN_LABEL = 'Не указано'
DAY_NIGHT_LABELS = {
    'day': 'День',
    'night': 'Ночь',
}
SHIFT_FILTER_VALUES = {
    'День': 'day',
    'Ночь': 'night',
    'Ранняя': 'early',
}


def _employee_photo_url(employee):
    if not employee.photo:
        return ''
    try:
        return employee.photo.url
    except ValueError:
        return ''


def _person_initials(full_name):
    parts = [part for part in str(full_name or '').split() if part]
    return ''.join(part[0].upper() for part in parts[:2])


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
            role__code__in=('settlement_clerk', 'admin'),
        )
        .first()
    )
    if not access:
        return None
    session_state = role_session_state(request, access)
    if not session_state['authenticated'] or not session_state['is_active']:
        return None
    return access


def _employee_profiles(employees):
    employees = list(employees)
    employee_ids = [employee.pk for employee in employees]
    equipment_by_employee = {}
    shift_by_employee = {}

    if employee_ids:
        equipment_assignments = (
            EquipmentAssignment.objects
            .filter(
                employee_id__in=employee_ids,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
                shift__isnull=True,
                role__isnull=False,
                shift_type__isnull=False,
            )
            .select_related(
                'equipment',
                'equipment__equipment_type',
                'equipment__model',
            )
            .order_by('employee_id', '-assigned_at', '-id')
        )
        for assignment in equipment_assignments:
            equipment_by_employee.setdefault(assignment.employee_id, assignment)

        open_shifts = (
            EmployeeShift.objects
            .filter(employee_id__in=employee_ids, closed_at__isnull=True)
            .select_related(
                'equipment',
                'equipment__equipment_type',
                'equipment__model',
            )
            .order_by('employee_id', '-opened_at', '-id')
        )
        for shift in open_shifts:
            shift_by_employee.setdefault(shift.employee_id, shift)

    profiles = {}
    for employee in employees:
        assignment = equipment_by_employee.get(employee.pk)
        shift = shift_by_employee.get(employee.pk)
        shift_type = (
            assignment.shift_type
            if assignment is not None
            else (shift.shift_type if shift is not None else '')
        )
        equipment = (
            assignment.equipment
            if assignment is not None
            else (shift.equipment if shift is not None else None)
        )
        position_label = (
            employee.personnel_position.name
            if employee.personnel_position_id
            else employee.position.strip()
        )
        profiles[employee.pk] = {
            'photo_url': _employee_photo_url(employee),
            'shift_label': DAY_NIGHT_LABELS.get(shift_type, UNKNOWN_LABEL),
            'position_label': position_label or UNKNOWN_LABEL,
            'work_label': str(equipment) if equipment is not None else (position_label or UNKNOWN_LABEL),
        }
    return profiles


def _unsettled_employee_cards(moment):
    employees = list(unsettled_current_roster_employees(moment))
    profiles = _employee_profiles(employees)
    return [
        {
            'id': employee.pk,
            'full_name': employee.full_name,
            'initials': _person_initials(employee.full_name),
            'personnel_number': employee.personnel_number,
            'position_label': profiles[employee.pk]['position_label'],
            'shift_label': profiles[employee.pk]['shift_label'],
            'has_shift': profiles[employee.pk]['shift_label'] != UNKNOWN_LABEL,
            'shift_filter': SHIFT_FILTER_VALUES.get(
                profiles[employee.pk]['shift_label'],
                '',
            ),
            'photo_url': profiles[employee.pk]['photo_url'],
            'watch_composition_label': employee.watch_composition.name,
            'search_text': ' '.join(
                filter(
                    None,
                    (employee.full_name, employee.personnel_number),
                )
            ).casefold(),
        }
        for employee in employees
    ]


def _attach_occupancy_view(rooms):
    beds = [bed for room in rooms for bed in room.beds.all()]
    occupancies = [
        bed.active_occupancies[0]
        for bed in beds
        if bed.active_occupancies
    ]
    profiles = _employee_profiles(
        occupancy.employee
        for occupancy in occupancies
    )

    for room in rooms:
        room.occupied_bed_count = 0
        for bed in room.beds.all():
            occupancy = bed.active_occupancies[0] if bed.active_occupancies else None
            bed.active_occupancy = occupancy
            if occupancy is None:
                bed.occupant_name = UNKNOWN_LABEL
                bed.occupant_photo_url = ''
                bed.shift_label = UNKNOWN_LABEL
                bed.work_label = UNKNOWN_LABEL
                bed.assignment_type_label = UNKNOWN_LABEL
                continue

            profile = profiles[occupancy.employee_id]
            room.occupied_bed_count += 1
            bed.occupant_name = occupancy.employee.full_name
            bed.occupant_photo_url = profile['photo_url']
            bed.shift_label = profile['shift_label']
            bed.work_label = profile['work_label']
            bed.assignment_type_label = occupancy.get_assignment_type_display()
        room.free_bed_count = len(room.beds.all()) - room.occupied_bed_count


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
        'occupied_beds': sum(room.occupied_bed_count for room in rooms),
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
            'occupied_beds': sum(
                room.occupied_bed_count
                for room in dormitory_rooms
                if room.is_transferred
            ),
            'free_beds': sum(
                room.free_bed_count
                for room in dormitory_rooms
                if room.is_transferred
            ),
        })
    return result


def settlement_login_view(request):
    from users.views import login_view

    return login_view(
        request,
        allowed_role_codes=('settlement_clerk', 'admin'),
        target_role_app=get_role_app('settlement_clerk'),
        forced_next_url=reverse('clerk_home'),
    )


def settlement_manifest_view(request):
    return role_app_manifest_response(request, 'settlement_clerk')


def settlement_service_worker_view(request):
    return role_app_service_worker_response(request, 'settlement_clerk')


@require_GET
def legacy_settlement_entry_view(request):
    return redirect('clerk_home')


@require_http_methods(['GET', 'POST'])
def legacy_settlement_login_view(request):
    if request.method == 'POST':
        return settlement_login_view(request)
    next_url = urlencode({'next': reverse('clerk_home')})
    return redirect(f'{reverse("clerk_login")}?{next_url}')


@require_GET
def legacy_settlement_service_worker_view(request):
    script = r'''
const LEGACY_CACHE_PREFIX = "settlement-clerk-shell-";
const CLERK_START_URL = "/clerk/";

self.addEventListener("install", event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key.startsWith(LEGACY_CACHE_PREFIX))
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.matchAll({ type: "window", includeUncontrolled: true }))
      .then(clients => Promise.all(
        clients.map(client => {
          const clientUrl = new URL(client.url);
          return clientUrl.pathname.startsWith("/settlement/")
            ? client.navigate(CLERK_START_URL)
            : undefined;
        })
      ))
      .then(() => self.registration.unregister())
  );
});

self.addEventListener("fetch", event => {
  if (event.request.mode !== "navigate") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith("/settlement/")) return;
  event.respondWith(Response.redirect(new URL(CLERK_START_URL, self.location.origin), 302));
});
'''.strip()
    response = HttpResponse(
        script,
        content_type='application/javascript; charset=utf-8',
    )
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = '/settlement/'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_GET
def settlement_map_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        next_url = urlencode({'next': reverse('clerk_home')})
        return redirect(f'{reverse("clerk_login")}?{next_url}')

    moment = timezone.now()
    rooms = list(
        PhysicalRoom.objects
        .select_related('dormitory')
        .prefetch_related(
            Prefetch(
                'beds',
                queryset=(
                    PhysicalBed.objects
                    .prefetch_related(
                        Prefetch(
                            'occupancies',
                            queryset=(
                                EmployeeBedOccupancy.objects
                                .filter(effective_occupancy_at_q(moment))
                                .select_related('employee', 'employee__personnel_position')
                            ),
                            to_attr='active_occupancies',
                        )
                    )
                    .order_by('block', 'position')
                ),
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
    _attach_occupancy_view(rooms)
    dormitories = _dormitory_views(rooms)
    total_beds = sum(room.capacity for room in rooms)
    transferred_rooms = sum(room.is_transferred for room in rooms)
    transferred_beds = sum(
        room.capacity
        for room in rooms
        if room.is_transferred
    )
    occupied_beds = sum(
        room.occupied_bed_count
        for room in rooms
        if room.is_transferred
    )
    roster_resolution = current_roster_resolution(moment)
    unsettled_employees = _unsettled_employee_cards(moment)

    return render(
        request,
        'settlement/clerk_map.html',
        {
            'access': access,
            'dormitories': dormitories,
            'current_date': timezone.localdate(moment),
            'summary': {
                'rooms': len(rooms),
                'beds': total_beds,
                'transferred_rooms': transferred_rooms,
                'transferred_beds': transferred_beds,
                'occupied_beds': occupied_beds,
                'free_beds': transferred_beds - occupied_beds,
                'not_transferred_rooms': len(rooms) - transferred_rooms,
                'not_transferred_beds': total_beds - transferred_beds,
            },
            'assignment_type_choices': EmployeeBedOccupancy.AssignmentType.choices,
            'unsettled_employees': unsettled_employees,
            'unsettled_employee_count': len(unsettled_employees),
            'unsettled_roster_available': roster_resolution['has_unambiguous'],
            'unsettled_roster_ambiguous': roster_resolution['has_ambiguous'],
            'clerk_active_section': 'settlement',
        },
    )


@require_GET
def settlement_employee_search_view(request):
    if not settlement_clerk_access_from_request(request):
        return JsonResponse(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )

    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'ok': True, 'results': []})

    moment = timezone.now()
    employees = list(
        unsettled_current_roster_employees(moment)
        .filter(
            Q(full_name__icontains=query)
            | Q(personnel_number__icontains=query)
            | Q(position__icontains=query)
            | Q(personnel_position__name__icontains=query)
        )
        .select_related('personnel_position')
        .order_by('full_name', 'pk')[:12]
    )
    profiles = _employee_profiles(employees)
    return JsonResponse({
        'ok': True,
        'results': [
            {
                'id': employee.pk,
                'full_name': employee.full_name,
                'personnel_number': employee.personnel_number or UNKNOWN_LABEL,
                'shift_label': profiles[employee.pk]['shift_label'],
                'work_label': profiles[employee.pk]['work_label'],
            }
            for employee in employees
        ],
    })


def _validation_error_details(error):
    if hasattr(error, 'error_list') and error.error_list:
        item = error.error_list[0]
        return item.message, item.code
    return str(error), 'settlement_validation_error'


def _occupancy_response(occupancy):
    employee = occupancy.employee
    profile = _employee_profiles([employee])[employee.pk]
    bed = occupancy.physical_bed
    room = bed.room
    moment = timezone.now()
    active_in_room = EmployeeBedOccupancy.objects.filter(
        effective_occupancy_at_q(moment),
        physical_bed__room=room,
    ).count()
    transferred_beds = PhysicalBed.objects.filter(
        room__dormitory__number__in=['5', '6'],
        room__transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
    )
    occupied_beds = EmployeeBedOccupancy.objects.filter(
        effective_occupancy_at_q(moment),
        physical_bed__in=transferred_beds,
    ).count()

    return {
        'ok': True,
        'occupancy': {
            'id': occupancy.pk,
            'bed_stable_id': bed.stable_id,
            'occupant_name': employee.full_name,
            'photo_url': profile['photo_url'],
            'shift_label': profile['shift_label'],
            'work_label': profile['work_label'],
            'assignment_type': occupancy.assignment_type,
            'assignment_type_label': occupancy.get_assignment_type_display(),
        },
        'room': {
            'id': room.pk,
            'occupied_beds': active_in_room,
            'free_beds': room.beds.count() - active_in_room,
        },
        'summary': {
            'occupied_beds': occupied_beds,
            'free_beds': transferred_beds.count() - occupied_beds,
        },
    }


def _payload_ends_at(payload):
    value = payload.get('ends_at')
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ValidationError(
            'Плановое окончание размещения указано некорректно.',
            code='settlement_ends_at_invalid',
        )
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise ValidationError(
            'Плановое окончание размещения указано некорректно.',
            code='settlement_ends_at_invalid',
        )
    return parsed


@require_POST
def settlement_occupancy_create_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return JsonResponse(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )

    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError):
        return JsonResponse(
            {'ok': False, 'error': 'Некорректные данные запроса.'},
            status=400,
        )
    if not isinstance(payload, dict):
        return JsonResponse(
            {'ok': False, 'error': 'Некорректные данные запроса.'},
            status=400,
        )

    action = payload.get('action', 'settle')
    try:
        if action == 'settle':
            occupancy = settle_employee_on_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
                employee_id=payload.get('employee_id'),
                assignment_type=payload.get('assignment_type', ''),
                ends_at=_payload_ends_at(payload),
                settled_by=access.employee,
            )
        elif action == 'relocate':
            occupancy = relocate_employee_to_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
                employee_id=payload.get('employee_id'),
                assignment_type=payload.get('assignment_type', ''),
                ends_at=_payload_ends_at(payload),
                settled_by=access.employee,
            )
        elif action == 'release':
            occupancy = release_employee_from_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
            )
        else:
            raise ValidationError(
                'Неизвестное действие расселения.',
                code='settlement_action_invalid',
            )
    except ValidationError as error:
        message, code = _validation_error_details(error)
        status = 409 if code in {
            'settlement.bed.interval_overlap',
            'settlement.employee.interval_overlap',
            'settlement_bed_occupied',
            'settlement_employee_already_housed',
            'settlement_room_not_transferred',
            'settlement_employee_not_housed',
            'settlement_employee_multiple_active_occupancies',
            'settlement_bed_multiple_active_occupancies',
            'settlement_bed_already_free',
            'settlement_relocation_same_bed',
            'settlement_relocation_same_moment',
            'settlement_release_same_moment',
            'settlement_employee_sex_unknown',
            'settlement_room_sex_mismatch',
        } else 400
        if code in {'settlement_bed_not_found', 'settlement_employee_not_found'}:
            status = 404
        return JsonResponse(
            {'ok': False, 'error': message, 'code': code},
            status=status,
        )

    if action == 'release':
        return JsonResponse({
            'ok': True,
            'action': 'release',
            'occupancy_id': occupancy.pk,
        })
    return JsonResponse(_occupancy_response(occupancy), status=201)
