import json
from collections import defaultdict
from urllib.parse import urlencode

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from assignments.models import AssignmentStatus, EquipmentAssignment
from core.production_time import production_day_bounds, production_work_date
from shifts.models import EmployeeShift
from users.active_role import role_session_state
from users.models import Employee, EmployeeAccess
from users.role_apps import (
    get_role_app,
    role_app_manifest_response,
    role_app_service_worker_response,
)

from .control import (
    SettlementControlWriteContext,
    acquire_control_lease,
    clear_control_session_credentials,
    control_session_credentials_from_session,
    heartbeat_control_lease,
    release_control_lease,
    store_control_session_credentials,
)
from .models import EmployeeBedOccupancy, PhysicalBed, PhysicalRoom
from .services import (
    current_roster_resolution,
    build_auto_settlement_preview,
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


class AutoSettlementPreviewForm(forms.Form):
    effective_date = forms.DateField(
        label='Расчётная дата',
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
        widget=forms.DateInput(
            format='%Y-%m-%d',
            attrs={'type': 'date'},
        ),
        error_messages={
            'required': 'Укажите дату расчёта.',
            'invalid': 'Укажите дату расчёта в корректном формате.',
        },
    )


_AUTO_SETTLEMENT_CONFLICT_LABELS = {
    'employee_multiple_effective_assignments': 'У сотрудника несколько несовместимых действующих назначений.',
    'assignment_equipment_missing': 'В действующем назначении не указана техника.',
    'assignment_shift_missing_or_invalid': 'В действующем назначении не указана дневная или ночная смена.',
    'equipment_anchor_missing': 'За техникой не закреплена жилая позиция.',
    'equipment_anchor_ambiguous_for_shift': 'Для техники и смены найдено несколько жилых позиций.',
    'anchor_bed_assignment_missing': 'Для жилой позиции не закреплена действующая койка.',
    'anchor_bed_assignment_ambiguous': 'Для жилой позиции нельзя однозначно определить койку.',
    'anchor_bed_unavailable': 'Закреплённая койка недоступна для расселения.',
    'bed_shift_capacity_conflict': 'Одна койка предварительно выбрана для нескольких сотрудников одной смены.',
    'bed_occupied_by_other_employee': 'Койка занята действующим размещением другого сотрудника.',
}


def _preview_employee_label(employee):
    return getattr(employee, 'full_name', '') or str(employee or '—')


def _preview_bed_label(bed):
    return getattr(bed, 'stable_id', '') or str(bed or '—')


def _auto_settlement_preview_context(preview):
    rows = []
    for row in preview['rows']:
        room = row['room']
        dormitory = getattr(room, 'dormitory', None)
        employee_label = _preview_employee_label(row['employee'])
        rows.append(
            {
                'employee': employee_label,
                'employee_id': row['employee'].pk,
                'employee_sort': employee_label.casefold(),
                'equipment': str(row['equipment']),
                'shift': DAY_NIGHT_LABELS.get(row['shift_type'], 'Не указана'),
                'shift_type': row['shift_type'],
                'dormitory': getattr(dormitory, 'number', '—'),
                'room': getattr(room, 'number', '—'),
                'bed': _preview_bed_label(row['bed']),
                'bed_id': row['bed'].pk,
            }
        )

    conflicts = []
    for conflict in preview['conflicts']:
        assignments = tuple(conflict.get('equipment_assignments') or ())
        assignment = assignments[0] if assignments else None
        employee = conflict.get('employee') or getattr(assignment, 'employee', None)
        equipment = conflict.get('equipment') or getattr(assignment, 'equipment', None)
        shift_type = conflict.get('shift_type') or getattr(assignment, 'shift_type', None)
        room = conflict.get('room') or getattr(conflict.get('bed'), 'room', None)
        conflicts.append(
            {
                'message': _AUTO_SETTLEMENT_CONFLICT_LABELS.get(
                    conflict['code'],
                    'Предварительный расчёт обнаружил конфликт.',
                ),
                'employee': _preview_employee_label(employee) if employee else '—',
                'equipment': str(equipment) if equipment else '—',
                'shift': DAY_NIGHT_LABELS.get(shift_type, '—'),
                'anchor': str(conflict['accommodation_anchor']) if conflict.get('accommodation_anchor') else '—',
                'room': getattr(room, 'number', '—'),
                'bed': _preview_bed_label(conflict.get('bed')) if conflict.get('bed') else '—',
            }
        )
    return {'summary': preview['summary'], 'rows': rows, 'conflicts': conflicts}


def _attach_auto_settlement_preview(rooms, preview):
    """Attach only successful, already-resolved preview rows to map bed objects."""
    beds_by_id = {
        bed.pk: bed
        for room in rooms
        for bed in room.beds.all()
    }
    for bed in beds_by_id.values():
        bed.preview_rows = []

    shift_order = {'day': 0, 'night': 1}
    for row in preview['rows']:
        bed = beds_by_id.get(row['bed_id'])
        if bed is None:
            raise ValueError('Предварительная строка не связана с физической койкой карты.')
        active_occupancy = bed.active_occupancy
        bed.preview_rows.append(
            {
                **row,
                'unchanged': bool(
                    active_occupancy
                    and active_occupancy.resident.employee_id == row['employee_id']
                ),
            }
        )

    for bed in beds_by_id.values():
        bed.preview_rows.sort(
            key=lambda row: (
                shift_order.get(row['shift_type'], 2),
                row['employee_sort'],
                row['employee_id'],
            )
        )


def _employee_photo_url(employee):
    if not employee.photo:
        return ''
    try:
        return employee.photo.url
    except ValueError:
        return ''


def _resident_photo_url(resident):
    if resident.employee_id:
        return _employee_photo_url(resident.employee)
    if not resident.photo:
        return ''
    try:
        return resident.photo.url
    except ValueError:
        return ''


def _person_initials(full_name):
    parts = [part for part in str(full_name or '').split() if part]
    return ''.join(part[0].upper() for part in parts[:2])


def _person_short_label(full_name):
    parts = [part for part in str(full_name or '').split() if part]
    if len(parts) < 2:
        return ' '.join(parts)
    return f"{parts[0]} {''.join(f'{part[0]}.' for part in parts[1:])}"


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
        occupancy.resident.employee
        for occupancy in occupancies
        if occupancy.resident.employee_id
    )

    for room in rooms:
        room.occupied_bed_count = 0
        for bed in room.beds.all():
            occupancy = bed.active_occupancies[0] if bed.active_occupancies else None
            bed.active_occupancy = occupancy
            bed.preview_rows = []
            if occupancy is None:
                bed.occupant_name = UNKNOWN_LABEL
                bed.occupant_short_name = UNKNOWN_LABEL
                bed.occupant_photo_url = ''
                bed.shift_label = UNKNOWN_LABEL
                bed.work_label = UNKNOWN_LABEL
                bed.position_label = UNKNOWN_LABEL
                bed.assignment_type_label = UNKNOWN_LABEL
                continue

            resident = occupancy.resident
            room.occupied_bed_count += 1
            bed.occupant_name = resident.display_name
            bed.occupant_short_name = _person_short_label(resident.display_name)
            if resident.employee_id:
                profile = profiles[resident.employee_id]
                bed.occupant_photo_url = profile['photo_url']
                bed.shift_label = profile['shift_label']
                bed.work_label = profile['work_label']
                bed.position_label = profile['position_label']
            else:
                bed.occupant_photo_url = _resident_photo_url(resident)
                bed.shift_label = UNKNOWN_LABEL
                bed.work_label = resident.organization or UNKNOWN_LABEL
                bed.position_label = resident.position_title or UNKNOWN_LABEL
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
    preview_requested = request.GET.get('preview') == '1'
    preview_form = AutoSettlementPreviewForm(
        request.GET if preview_requested else None,
        initial={'effective_date': production_work_date(moment)},
    )
    auto_settlement_preview = None
    if preview_requested and preview_form.is_valid():
        effective_date = preview_form.cleaned_data['effective_date']
        effective_moment, _ = production_day_bounds(effective_date)
        try:
            auto_settlement_preview = _auto_settlement_preview_context(
                build_auto_settlement_preview(effective_date=effective_moment)
            )
        except ValueError:
            preview_form.add_error(
                'effective_date',
                'Не удалось выполнить расчёт для указанной даты.',
            )
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
                                .select_related(
                                    'resident',
                                    'resident__employee',
                                    'resident__employee__personnel_position',
                                )
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
    if auto_settlement_preview is not None:
        _attach_auto_settlement_preview(rooms, auto_settlement_preview)
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
            'auto_settlement_preview_form': preview_form,
            'auto_settlement_preview': auto_settlement_preview,
            'auto_settlement_preview_requested': preview_requested,
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


@require_GET
def settlement_employee_detail_view(request, employee_id):
    """Read-only operational card opened from an occupied bed on the map."""
    if not settlement_clerk_access_from_request(request):
        return JsonResponse(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )

    employee = (
        Employee.objects
        .select_related(
            'personnel_position',
            'personnel_department',
            'work_schedule',
        )
        .filter(pk=employee_id)
        .first()
    )
    if employee is None:
        return JsonResponse(
            {'ok': False, 'error': 'Сотрудник не найден.'},
            status=404,
        )

    occupancy = (
        EmployeeBedOccupancy.objects
        .filter(effective_occupancy_at_q(timezone.now()), resident__employee=employee)
        .select_related('physical_bed__room__dormitory')
        .order_by('pk')
        .first()
    )
    residence = 'Фактическое место проживания не назначено.'
    if occupancy is not None:
        bed = occupancy.physical_bed
        room = bed.room
        residence = (
            f'КИС-{room.dormitory.number}, этаж {room.floor}, '
            f'комната {room.number}, блок {bed.get_block_display()}, '
            f'койка {bed.position} ({bed.stable_id})'
        )

    return JsonResponse({
        'ok': True,
        'employee': {
            'id': employee.pk,
            'full_name': employee.full_name,
            'personnel_number': employee.personnel_number or UNKNOWN_LABEL,
            'position': (
                employee.personnel_position.name
                if employee.personnel_position_id
                else employee.position or UNKNOWN_LABEL
            ),
            'department': employee.department_label or UNKNOWN_LABEL,
            'work_schedule': employee.work_schedule_label or UNKNOWN_LABEL,
            'brigade': employee.get_brigade_number_display() or UNKNOWN_LABEL,
            'sex': employee.get_sex_display(),
            'phone': employee.phone or UNKNOWN_LABEL,
            'residence': residence,
        },
    })


def _validation_error_details(error):
    if hasattr(error, 'error_list') and error.error_list:
        item = error.error_list[0]
        return item.message, item.code
    return str(error), 'settlement_validation_error'


def _control_access_denied_response(request):
    clear_control_session_credentials(request.session)
    return JsonResponse(
        {
            'ok': False,
            'status': 'free',
            'error': 'Нет доступа к управлению расселением.',
            'code': 'settlement.control.invalid_access',
        },
        status=403,
    )


def _control_error_response(request, error):
    message, code = _validation_error_details(error)
    short_code = code.rsplit('.', 1)[-1]
    clear_control_session_credentials(request.session)
    return JsonResponse(
        {
            'ok': False,
            'status': 'free' if short_code in {'not_held', 'expired'} else 'busy',
            'error': message,
            'code': code,
        },
        status=(
            403
            if short_code in {'invalid_access', 'inactive_access', 'invalid_role'}
            else 409
        ),
    )


def _control_credentials_for_access(request, access):
    credentials = control_session_credentials_from_session(request.session)
    if credentials.owner_access_id != access.pk:
        raise ValidationError(
            'Серверная сессия не владеет управлением расселением.',
            code='settlement.control.session_mismatch',
        )
    return credentials


def _settlement_write_control_context(request):
    credentials = control_session_credentials_from_session(request.session)
    owner_access_id = request.session.get('employee_access_id')
    raw_session_key = request.session.session_key
    if not raw_session_key:
        raise ValidationError(
            'Управление расселением свободно.',
            code='settlement.control.not_held',
        )
    if credentials.owner_access_id != owner_access_id:
        raise ValidationError(
            'Серверная сессия не владеет управлением расселением.',
            code='settlement.control.session_mismatch',
        )
    return SettlementControlWriteContext(
        owner_access_id=owner_access_id,
        raw_session_key=raw_session_key,
        lease_token=credentials.lease_token,
        fencing_revision=credentials.fencing_revision,
    )


@require_POST
def settlement_control_acquire_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _control_access_denied_response(request)

    try:
        grant = acquire_control_lease(
            owner_access_id=access.pk,
            raw_session_key=request.session.session_key,
            source='http_acquire',
            session_metadata={'session_kind': 'django'},
        )
    except ValidationError as error:
        return _control_error_response(request, error)

    store_control_session_credentials(
        request.session,
        owner_access_id=access.pk,
        grant=grant,
    )
    return JsonResponse({
        'ok': True,
        'status': 'held',
        'expires_at': grant.expires_at.isoformat(),
    })


@require_POST
def settlement_control_heartbeat_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _control_access_denied_response(request)

    try:
        credentials = _control_credentials_for_access(request, access)
        grant = heartbeat_control_lease(
            owner_access_id=credentials.owner_access_id,
            raw_session_key=request.session.session_key,
            lease_token=credentials.lease_token,
            fencing_revision=credentials.fencing_revision,
        )
    except ValidationError as error:
        return _control_error_response(request, error)

    store_control_session_credentials(
        request.session,
        owner_access_id=access.pk,
        grant=grant,
    )
    return JsonResponse({
        'ok': True,
        'status': 'held',
        'expires_at': grant.expires_at.isoformat(),
    })


@require_POST
def settlement_control_release_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _control_access_denied_response(request)

    try:
        credentials = _control_credentials_for_access(request, access)
        transition = release_control_lease(
            owner_access_id=credentials.owner_access_id,
            raw_session_key=request.session.session_key,
            lease_token=credentials.lease_token,
            fencing_revision=credentials.fencing_revision,
            source='http_release',
            session_metadata={'session_kind': 'django'},
        )
    except ValidationError as error:
        return _control_error_response(request, error)

    clear_control_session_credentials(request.session)
    return JsonResponse({
        'ok': True,
        'status': 'free',
        'occurred_at': transition.occurred_at.isoformat(),
    })


def _occupancy_response(occupancy):
    resident = occupancy.resident
    if resident.employee_id:
        employee = resident.employee
        profile = _employee_profiles([employee])[employee.pk]
        photo_url = profile['photo_url']
        shift_label = profile['shift_label']
        work_label = profile['work_label']
    else:
        photo_url = _resident_photo_url(resident)
        shift_label = UNKNOWN_LABEL
        work_label = resident.organization or UNKNOWN_LABEL
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
            'occupant_name': resident.display_name,
            'photo_url': photo_url,
            'shift_label': shift_label,
            'work_label': work_label,
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

    try:
        control_context = _settlement_write_control_context(request)
    except ValidationError as error:
        return _control_error_response(request, error)

    action = payload.get('action', 'settle')
    try:
        if action == 'settle':
            occupancy = settle_employee_on_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
                employee_id=payload.get('employee_id'),
                assignment_type=payload.get('assignment_type', ''),
                ends_at=_payload_ends_at(payload),
                control_context=control_context,
            )
        elif action == 'relocate':
            occupancy = relocate_employee_to_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
                employee_id=payload.get('employee_id'),
                assignment_type=payload.get('assignment_type', ''),
                ends_at=_payload_ends_at(payload),
                control_context=control_context,
            )
        elif action == 'release':
            occupancy = release_employee_from_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
                control_context=control_context,
            )
        else:
            raise ValidationError(
                'Неизвестное действие расселения.',
                code='settlement_action_invalid',
            )
    except ValidationError as error:
        message, code = _validation_error_details(error)
        if code.startswith('settlement.control.'):
            return _control_error_response(request, error)
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
