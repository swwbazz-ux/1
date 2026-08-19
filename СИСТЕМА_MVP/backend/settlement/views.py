import json
from collections import Counter, defaultdict
from datetime import timedelta
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
from rotations.arrival_roster_routing import settlement_clerk_arrival_roster_routing_queue

from .control import (
    SettlementControlWriteContext,
    acquire_control_lease,
    clear_control_session_credentials,
    control_session_credentials_from_session,
    heartbeat_control_lease,
    release_control_lease,
    store_control_session_credentials,
)
from .apply import apply_confirmed_settlement_preview
from .models import (
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementCohort,
    SettlementCohortMember,
    SettlementPreviewCorrection,
    SettlementPreviewRun,
)
from .preview_corrections import (
    exclude_settlement_preview_resident,
    get_effective_settlement_preview_plan,
    move_settlement_preview_resident,
    restore_settlement_preview_resident,
)
from .resolver import _slot_candidates as _resolver_slot_candidates
from .saved_previews import (
    confirm_settlement_preview_run,
    create_settlement_preview_run,
    settlement_preview_is_stale,
)
from .services import (
    current_roster_resolution,
    build_auto_settlement_preview,
    effective_occupancy_at_q,
    relocate_resident_to_bed,
    release_resident_from_bed,
    settle_resident_on_bed,
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

AUTO_SETTLEMENT_REASON_LABELS = {
    'cohort_not_approved': 'Состав заезда ещё не утверждён.',
    'resolver_not_configured': 'Для этого случая ещё не настроено правило размещения.',
    'resident_inactive': 'Карточка жильца неактивна.',
    'incomplete_authoritative_context': 'Не хватает подтверждённых исходных данных.',
    'stale_calendar_relation': 'Календарное основание устарело.',
    'invalid_existing_binding': 'Существующее закрепление противоречит текущим данным.',
    'no_compatible_place': 'Подходящее свободное место не найдено.',
    'equal_priority_conflict': 'Найдено несколько равноприоритетных вариантов.',
    'hard_rule_conflict': 'Размещение нарушает обязательное правило.',
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


def settlement_clerk_access_from_request(request, *, allow_admin=True):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    role_codes = ('settlement_clerk', 'admin') if allow_admin else ('settlement_clerk',)
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
            role__code__in=role_codes,
        )
        .first()
    )
    if not access:
        return None
    session_state = role_session_state(request, access)
    if not session_state['authenticated'] or not session_state['is_active']:
        return None
    return access


@require_GET
def settlement_arrival_roster_routing_view(request):
    """Read-only ready-for-settlement hand-off queue for the exact clerk access."""
    access = settlement_clerk_access_from_request(request, allow_admin=False)
    if not access:
        next_url = urlencode({'next': reverse('settlement_arrival_roster_routing')})
        return redirect(f'{reverse("clerk_login")}?{next_url}')
    return render(
        request,
        'settlement/arrival_roster_routing_queue.html',
        {
            'access': access,
            'routing_queue': settlement_clerk_arrival_roster_routing_queue(),
            'clerk_active_section': 'routing',
        },
    )


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
            Q(
                Q(full_name__icontains=query)
                | Q(personnel_number__icontains=query)
                | Q(position__icontains=query)
                | Q(personnel_position__name__icontains=query)
            ),
            settlement_resident__resident_type='EMPLOYEE',
            settlement_resident__status='ACTIVE',
        )
        .select_related('personnel_position', 'settlement_resident')
        .order_by('full_name', 'pk')[:12]
    )
    profiles = _employee_profiles(employees)
    return JsonResponse({
        'ok': True,
        'results': [
            {
                'id': employee.settlement_resident.pk,
                'resident_id': employee.settlement_resident.pk,
                'employee_id': employee.pk,
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


def _auto_settlement_json_payload(request):
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError) as error:
        raise ValidationError(
            'Некорректные данные запроса.',
            code='settlement.auto.invalid_payload',
        ) from error
    if not isinstance(payload, dict):
        raise ValidationError(
            'Некорректные данные запроса.',
            code='settlement.auto.invalid_payload',
        )
    return payload


def _auto_settlement_positive_id(payload, key):
    value = payload.get(key)
    if isinstance(value, bool):
        value = None
    try:
        value = int(value)
    except (TypeError, ValueError) as error:
        raise ValidationError(
            'Не указан объект авторасселения.',
            code='settlement.auto.invalid_identifier',
        ) from error
    if value < 1:
        raise ValidationError(
            'Не указан объект авторасселения.',
            code='settlement.auto.invalid_identifier',
        )
    return value


def _auto_settlement_error_payload(error):
    message, code = _validation_error_details(error)
    params = {}
    if hasattr(error, 'error_list') and error.error_list:
        params = error.error_list[0].params or {}
    return {
        'ok': False,
        'error': message,
        'code': code,
        'details': {
            key: value
            for key, value in params.items()
            if key in {'manual_occupancy_count'}
        },
    }


def _auto_settlement_control_context(request, access):
    context = _settlement_write_control_context(request)
    if context.owner_access_id != access.pk:
        raise ValidationError(
            'Серверная сессия не владеет управлением расселением.',
            code='settlement.control.session_mismatch',
        )
    return context


def _auto_settlement_cohort_payload(cohort):
    period = cohort.watch_period
    return {
        'id': cohort.pk,
        'version': cohort.version,
        'member_count': cohort.members.count(),
        'watch_period': {
            'id': period.pk,
            'name': period.name,
            'starts_on': period.starts_on.isoformat(),
            'ends_on': period.ends_on.isoformat(),
        },
    }


def _auto_settlement_resident_payload(resident):
    return {
        'id': resident.pk,
        'name': resident.display_name,
        'kind': 'internal' if resident.employee_id else 'external',
        'kind_label': 'Внутренний' if resident.employee_id else 'Внешний',
        'organization': '' if resident.employee_id else (resident.organization or ''),
    }


def _auto_settlement_application_payload(application, unresolved_count):
    action_counts = Counter(
        application.occupancy_items.values_list('action', flat=True)
    )
    return {
        'applied_at': application.applied_at.isoformat(),
        'actor': application.applied_by_access.employee.full_name,
        'created': action_counts.get('created', 0),
        'reused': action_counts.get('reused', 0),
        'replaced_auto': action_counts.get('replaced_auto', 0),
        'replaced_manual': action_counts.get('replaced_manual', 0),
        'unresolved': unresolved_count,
    }


def _auto_settlement_work_shift_label(work_shift):
    return (
        'Дневная смена'
        if work_shift == SettlementCohortMember.WorkShift.DAY
        else 'Ночная смена'
    )


def _auto_settlement_reason_label(reason_code):
    short_code = str(reason_code or '').rsplit('.', 1)[-1]
    return AUTO_SETTLEMENT_REASON_LABELS.get(
        short_code,
        'Требуется уточнение данных.',
    )


def _auto_settlement_bed_place(bed):
    if bed is None:
        return 'Без назначенного места'
    room = bed.room
    return (
        f'КИС-{room.dormitory.number}, комната {room.number}, '
        f'койка {bed.get_block_display()}-{bed.position}'
    )


def _auto_settlement_plan_map(cohort):
    candidates, _snapshot = _resolver_slot_candidates(cohort)
    candidates_by_bed = defaultdict(list)
    for candidate in candidates.values():
        candidates_by_bed[candidate.bed_id].append(candidate)
    rooms = (
        PhysicalRoom.objects
        .select_related('dormitory')
        .prefetch_related('beds')
        .order_by(
            'dormitory__number', 'floor', 'corridor_side',
            'side_position', 'number', 'pk',
        )
    )
    payload = []
    for room in rooms:
        beds = []
        for bed in room.beds.all().order_by('block', 'position', 'pk'):
            exact_candidates = candidates_by_bed.get(bed.pk, [])
            target = exact_candidates[0] if len(exact_candidates) == 1 else None
            beds.append({
                'stable_id': bed.stable_id,
                'display': f'{bed.get_block_display()}-{bed.position}',
                'target_calendar_slot_id': target.slot_id if target else None,
                'target_physical_bed_id': bed.pk if target else None,
            })
        payload.append({
            'stable_id': (
                f'KIS-{room.dormitory.number}-F{room.floor}-R{room.number}'
            ),
            'display': f'КИС-{room.dormitory.number}, комната {room.number}',
            'dormitory': str(room.dormitory.number),
            'floor': room.floor,
            'corridor_side': room.corridor_side,
            'side_position': room.side_position,
            'transferred': room.is_transferred,
            'beds': beds,
        })
    return payload


def _auto_settlement_effective_plan_payload(run, *, stale, shift_apply):
    if run.status != SettlementPreviewRun.Status.CONFIRMED:
        return None
    placements = list(run.placements.all())
    unresolved_rows = list(run.unresolved_rows.all())
    corrections = list(run.corrections.all())
    effective = get_effective_settlement_preview_plan(run_id=run.pk)
    base_by_key = {
        (row.resident_id, row.work_shift): row
        for row in (*placements, *unresolved_rows)
    }
    residents = {
        row.resident_id: row.resident
        for row in (*placements, *unresolved_rows)
    }
    corrections_by_id = {row.pk: row for row in corrections}
    bed_ids = {
        *(row.physical_bed_id for row in placements),
        *(row.target_physical_bed_id for row in corrections if row.target_physical_bed_id),
        *(row.physical_bed_id for row in effective.decisions if row.physical_bed_id),
    }
    beds_by_id = {
        row.pk: row
        for row in PhysicalBed.objects.filter(pk__in=bed_ids)
        .select_related('room__dormitory')
    }
    placed = []
    unresolved = []
    excluded = []
    for decision in effective.decisions:
        key = (decision.resident_id, decision.work_shift)
        source = base_by_key[key]
        resident = residents[decision.resident_id]
        correction = corrections_by_id.get(decision.effective_correction_id)
        applied = shift_apply[decision.work_shift]['status'] == 'applied'
        correction_action = correction.action if correction else ''
        common = {
            'resident': _auto_settlement_resident_payload(resident),
            'work_shift': decision.work_shift,
            'work_shift_label': _auto_settlement_work_shift_label(decision.work_shift),
            'manually_changed': correction_action in {
                SettlementPreviewCorrection.Action.MOVE,
                SettlementPreviewCorrection.Action.EXCLUDE,
            },
            'action_description': {
                SettlementPreviewCorrection.Action.MOVE: 'Перемещён в плане',
                SettlementPreviewCorrection.Action.EXCLUDE: 'Исключён из плана',
                SettlementPreviewCorrection.Action.RESTORE: 'Исходное решение восстановлено',
            }.get(correction_action, ''),
            'shift_applied': applied,
            'editable': not stale and not applied,
            'can_restore': (
                not applied
                and correction_action in {
                    SettlementPreviewCorrection.Action.MOVE,
                    SettlementPreviewCorrection.Action.EXCLUDE,
                }
            ),
        }
        if decision.state == 'placement':
            bed = beds_by_id.get(decision.physical_bed_id)
            base_bed = beds_by_id.get(getattr(source, 'physical_bed_id', None))
            placed.append({
                **common,
                'room_stable_id': (
                    f'KIS-{bed.room.dormitory.number}-F{bed.room.floor}-R{bed.room.number}'
                    if bed else ''
                ),
                'room': (
                    f'КИС-{bed.room.dormitory.number}, комната {bed.room.number}'
                    if bed else ''
                ),
                'bed_stable_id': bed.stable_id if bed else '',
                'bed': f'{bed.get_block_display()}-{bed.position}' if bed else '',
                'target_calendar_slot_id': decision.calendar_slot_id,
                'target_physical_bed_id': decision.physical_bed_id,
                'original_place': (
                    _auto_settlement_bed_place(base_bed)
                    if correction_action == SettlementPreviewCorrection.Action.MOVE
                    else ''
                ),
            })
        else:
            row = {
                **common,
                'excluded': decision.state == 'excluded',
                'reason': (
                    correction.reason
                    if decision.state == 'excluded' and correction
                    else _auto_settlement_reason_label(getattr(source, 'reason_code', ''))
                ),
            }
            (excluded if row['excluded'] else unresolved).append(row)

    history = []
    current_places = {
        key: _auto_settlement_bed_place(
            beds_by_id.get(getattr(source, 'physical_bed_id', None)),
        )
        for key, source in base_by_key.items()
    }
    for correction in sorted(
        corrections,
        key=lambda row: (row.resident_id, row.work_shift, row.created_at, row.pk),
    ):
        key = (correction.resident_id, correction.work_shift)
        source = base_by_key[key]
        previous_place = current_places[key]
        if correction.action == SettlementPreviewCorrection.Action.MOVE:
            new_place = _auto_settlement_bed_place(
                beds_by_id.get(correction.target_physical_bed_id),
            )
        elif correction.action == SettlementPreviewCorrection.Action.EXCLUDE:
            new_place = 'Исключён из плана'
        else:
            new_place = _auto_settlement_bed_place(
                beds_by_id.get(getattr(source, 'physical_bed_id', None)),
            )
        current_places[key] = new_place
        history.append({
            'action': {
                SettlementPreviewCorrection.Action.MOVE: 'Перемещение в плане',
                SettlementPreviewCorrection.Action.EXCLUDE: 'Исключение из плана',
                SettlementPreviewCorrection.Action.RESTORE: 'Возврат исходного решения',
            }[correction.action],
            'resident': residents[correction.resident_id].display_name,
            'work_shift': _auto_settlement_work_shift_label(correction.work_shift),
            'previous_place': previous_place,
            'new_place': new_place,
            'actor': correction.actor_access.employee.full_name,
            'created_at': correction.created_at.isoformat(),
            'reason': correction.reason,
        })
    return {
        'placements': placed,
        'unresolved': unresolved,
        'excluded': excluded,
        'history': history,
        'rooms': _auto_settlement_plan_map(run.cohort),
    }


def _auto_settlement_shift_apply_payload(run, work_shift, *, stale):
    allowed_date = (
        run.watch_period.starts_on
        if work_shift == SettlementCohortMember.WorkShift.DAY
        else run.watch_period.starts_on - timedelta(days=1)
    )
    applications = list(run.applications.all())
    application = next(
        (
            row for row in applications
            if row.legacy_whole_run or row.work_shift == work_shift
        ),
        None,
    )
    if application is not None:
        action_counts = Counter(
            application.occupancy_items.values_list('action', flat=True)
        )
        return {
            'status': 'applied',
            'allowed_date': allowed_date.isoformat(),
            'applied_at': application.applied_at.isoformat(),
            'counts': {
                'created': action_counts.get('created', 0),
                'reused': action_counts.get('reused', 0),
                'replaced_auto': action_counts.get('replaced_auto', 0),
                'replaced_manual': action_counts.get('replaced_manual', 0),
                'unresolved': run.unresolved_rows.filter(work_shift=work_shift).count(),
            },
        }
    if run.status != SettlementPreviewRun.Status.CONFIRMED or stale:
        status = 'not_ready'
    elif timezone.localdate() < allowed_date:
        status = 'too_early'
    else:
        status = 'ready'
    return {
        'status': status,
        'allowed_date': allowed_date.isoformat(),
        'applied_at': None,
        'counts': None,
    }


def _auto_settlement_run_payload(run):
    source_labels = {
        'confirmed_binding': 'binding',
        'official_equipment_assignment': 'equipment',
        'official_position_anchor': 'position',
        'external_residual_pool': 'residual',
    }
    member_ids = {
        row.cohort_member_id_snapshot for row in run.placements.all()
    }
    member_ids.update(
        row.cohort_member_id_snapshot for row in run.unresolved_rows.all()
    )
    members = {
        row.pk: row
        for row in SettlementCohortMember.objects.filter(pk__in=member_ids)
    }
    placements = []
    for row in run.placements.all():
        member = members.get(row.cohort_member_id_snapshot)
        bed = row.physical_bed
        room = bed.room
        placements.append({
            'resident': _auto_settlement_resident_payload(row.resident),
            'room': f'КИС-{room.dormitory.number}, комната {room.number}',
            'bed': bed.stable_id,
            'arrival_at': member.arrival_at.isoformat() if member else '',
            'departure_at': member.departure_at.isoformat() if member else '',
            'source': source_labels.get(row.source_kind, row.source_kind),
        })
    unresolved = []
    for row in run.unresolved_rows.all():
        unresolved.append({
            'resident': _auto_settlement_resident_payload(row.resident),
            'reason': _auto_settlement_reason_label(row.reason_code),
        })
    applications = list(run.applications.all())
    application = next(iter(applications), None)
    applied_shifts = {
        row.work_shift for row in applications if not row.legacy_whole_run
    }
    has_legacy_application = any(row.legacy_whole_run for row in applications)
    stale = (
        settlement_preview_is_stale(run_id=run.pk)
        if (
            run.status == SettlementPreviewRun.Status.CONFIRMED
            and not has_legacy_application
            and applied_shifts != {
                SettlementCohortMember.WorkShift.DAY,
                SettlementCohortMember.WorkShift.NIGHT,
            }
        )
        else False
    )
    shift_apply = {
        'night': _auto_settlement_shift_apply_payload(
            run,
            SettlementCohortMember.WorkShift.NIGHT,
            stale=stale,
        ),
        'day': _auto_settlement_shift_apply_payload(
            run,
            SettlementCohortMember.WorkShift.DAY,
            stale=stale,
        ),
    }
    return {
        'id': run.pk,
        'version': run.version,
        'status': run.status,
        'created_at': run.created_at.isoformat(),
        'created_by': run.created_by_access.employee.full_name,
        'member_count': run.cohort.members.count(),
        'placement_count': len(placements),
        'unresolved_count': len(unresolved),
        'stale': stale,
        'placements': placements,
        'unresolved': unresolved,
        'application': (
            _auto_settlement_application_payload(application, len(unresolved))
            if application else None
        ),
        'shift_apply': shift_apply,
        'effective_plan': _auto_settlement_effective_plan_payload(
            run,
            stale=stale,
            shift_apply=shift_apply,
        ),
    }


def _auto_settlement_run_queryset():
    return (
        SettlementPreviewRun.objects
        .select_related(
            'cohort', 'watch_period',
            'created_by_access__employee',
        )
        .prefetch_related(
            'applications__applied_by_access__employee',
            'applications__occupancy_items',
            'placements__resident__employee',
            'placements__physical_bed__room__dormitory',
            'unresolved_rows__resident__employee',
            'corrections__resident__employee',
            'corrections__actor_access__employee',
            'corrections__source_placement__physical_bed__room__dormitory',
            'corrections__source_unresolved',
            'corrections__target_physical_bed__room__dormitory',
        )
    )


@require_GET
def settlement_auto_state_view(request):
    if not settlement_clerk_access_from_request(request):
        return JsonResponse({'ok': False, 'error': 'Нет доступа к расселению.'}, status=403)
    cohorts = list(
        SettlementCohort.objects
        .filter(status=SettlementCohort.Status.APPROVED)
        .select_related('watch_period', 'watch_composition')
        .prefetch_related('members')
        .order_by('-watch_period__starts_on', '-version', '-pk')
    )
    if not cohorts:
        return JsonResponse({'ok': True, 'state': 'no_cohort', 'cohorts': [], 'preview': None})
    try:
        cohort_id = int(request.GET.get('cohort_id') or cohorts[0].pk)
    except (TypeError, ValueError):
        cohort_id = cohorts[0].pk
    selected = next((row for row in cohorts if row.pk == cohort_id), cohorts[0])
    run = (
        _auto_settlement_run_queryset()
        .filter(cohort=selected)
        .exclude(status=SettlementPreviewRun.Status.SUPERSEDED)
        .order_by('-version', '-pk')
        .first()
    )
    return JsonResponse({
        'ok': True,
        'state': 'ready',
        'cohorts': [_auto_settlement_cohort_payload(row) for row in cohorts],
        'selected_cohort_id': selected.pk,
        'preview': _auto_settlement_run_payload(run) if run else None,
    })


def _auto_settlement_mutation(request, callback):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _control_access_denied_response(request)
    try:
        payload = _auto_settlement_json_payload(request)
        control_context = _auto_settlement_control_context(request, access)
        result = callback(payload, control_context)
    except ValidationError as error:
        _message, code = _validation_error_details(error)
        if code.startswith('settlement.control.'):
            return _control_error_response(request, error)
        return JsonResponse(_auto_settlement_error_payload(error), status=409)
    return JsonResponse({'ok': True, 'preview': _auto_settlement_run_payload(result)})


@require_POST
def settlement_auto_preview_create_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: create_settlement_preview_run(
            cohort_id=_auto_settlement_positive_id(payload, 'cohort_id'),
            control_context=context,
        ),
    )


@require_POST
def settlement_auto_preview_confirm_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: confirm_settlement_preview_run(
            run_id=_auto_settlement_positive_id(payload, 'run_id'),
            control_context=context,
        ),
    )


def _auto_settlement_correction_result(callback):
    correction = callback()
    return _auto_settlement_run_queryset().get(pk=correction.preview_run_id)


def _auto_settlement_reason(payload):
    reason = payload.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError(
            'Укажите причину изменения.',
            code='settlement.preview_correction.target_invalid',
        )
    return reason.strip()


@require_POST
def settlement_auto_preview_move_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: _auto_settlement_correction_result(
            lambda: move_settlement_preview_resident(
                run_id=_auto_settlement_positive_id(payload, 'run_id'),
                resident_id=_auto_settlement_positive_id(payload, 'resident_id'),
                target_calendar_slot_id=_auto_settlement_positive_id(
                    payload,
                    'target_calendar_slot_id',
                ),
                target_physical_bed_id=_auto_settlement_positive_id(
                    payload,
                    'target_physical_bed_id',
                ),
                reason=_auto_settlement_reason(payload),
                control_context=context,
            ),
        ),
    )


@require_POST
def settlement_auto_preview_exclude_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: _auto_settlement_correction_result(
            lambda: exclude_settlement_preview_resident(
                run_id=_auto_settlement_positive_id(payload, 'run_id'),
                resident_id=_auto_settlement_positive_id(payload, 'resident_id'),
                reason=_auto_settlement_reason(payload),
                control_context=context,
            ),
        ),
    )


@require_POST
def settlement_auto_preview_restore_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: _auto_settlement_correction_result(
            lambda: restore_settlement_preview_resident(
                run_id=_auto_settlement_positive_id(payload, 'run_id'),
                resident_id=_auto_settlement_positive_id(payload, 'resident_id'),
                reason=_auto_settlement_reason(payload),
                control_context=context,
            ),
        ),
    )


@require_POST
def settlement_auto_preview_apply_view(request):
    def apply(payload, context):
        raise ValidationError(
            'План необходимо применить отдельно для ночной и дневной смены.',
            code='settlement.apply.shift_split_required',
        )

    return _auto_settlement_mutation(request, apply)


def _auto_settlement_shift_apply_view(request, work_shift):
    def apply(payload, context):
        application = apply_confirmed_settlement_preview(
            run_id=_auto_settlement_positive_id(payload, 'run_id'),
            work_shift=work_shift,
            control_context=context,
            confirm_replace_manual=payload.get('confirm_replace_manual') is True,
        )
        return _auto_settlement_run_queryset().get(pk=application.preview_run_id)

    return _auto_settlement_mutation(request, apply)


@require_POST
def settlement_auto_preview_apply_night_view(request):
    return _auto_settlement_shift_apply_view(
        request,
        SettlementCohortMember.WorkShift.NIGHT,
    )


@require_POST
def settlement_auto_preview_apply_day_view(request):
    return _auto_settlement_shift_apply_view(
        request,
        SettlementCohortMember.WorkShift.DAY,
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
            'resident_id': resident.pk,
            'employee_id': resident.employee_id,
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
            occupancy = settle_resident_on_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
                resident_id=payload.get('resident_id'),
                assignment_type=payload.get('assignment_type', ''),
                ends_at=_payload_ends_at(payload),
                control_context=control_context,
            )
        elif action == 'relocate':
            occupancy = relocate_resident_to_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
                occupancy_id=payload.get('occupancy_id'),
                control_context=control_context,
            )
        elif action == 'release':
            occupancy = release_resident_from_bed(
                occupancy_id=payload.get('occupancy_id'),
                bed_stable_id=payload.get('bed_stable_id'),
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
            'settlement.manual.shift_not_applied',
            'settlement.manual.period_ambiguous',
            'settlement.manual.period_stale',
            'settlement.manual.legacy_relocation_forbidden',
            'settlement.manual.occupancy_stale',
            'settlement.manual.external_unavailable',
            'settlement.cohort.shift_review_required',
            'settlement_employee_sex_unknown',
            'settlement_room_sex_mismatch',
        } else 400
        if code in {
            'settlement_bed_not_found',
            'settlement_employee_not_found',
            'settlement_resident_not_found',
            'settlement.manual.occupancy_not_found',
        }:
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
