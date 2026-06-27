from collections import defaultdict
import re
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from downtimes.models import DowntimeEvent
from references.models import Equipment
from shifts.models import EmployeeShift, ShiftType
from trips.models import Trip, TripStatus
from trips.views import dispatcher_control_view as render_dispatcher_control_view
from users.access_auth import find_employee_access_by_credentials
from users.models import EmployeeAccess
from users.session_device import get_session_device_kind, set_session_device_kind

from .forms import HaulAssignmentForm
from .models import AssignmentStatus, EquipmentAssignment, ExcavatorPlacement, HaulAssignment


TRUCK_ICON_BY_STATUS = {
    'green': 'img/equipment/truck-green.png',
    'yellow': 'img/equipment/truck-yellow.png',
    'blue': 'img/equipment/truck-blue.png',
    'red': 'img/equipment/truck-red.png',
    'gray': 'img/equipment/truck-gray.png',
}

EXCAVATOR_ICON_BY_STATUS = {
    'green': 'img/equipment/excavator-green.png',
    'yellow': 'img/equipment/excavator-yellow.png',
    'blue': 'img/equipment/excavator-blue.png',
    'red': 'img/equipment/excavator-red.png',
    'gray': 'img/equipment/excavator-gray.png',
}


def get_equipment_number(equipment):
    garage_number = (equipment.garage_number or '').strip()
    if not garage_number:
        return ''
    return garage_number


def get_truck_label(equipment):
    model_name = equipment.model.name if equipment.model else ''
    prefix = 'NHL' if 'NHL' in model_name.upper() or 'NHL' in equipment.garage_number.upper() else 'Б'
    return f'{prefix}-{get_equipment_number(equipment)}'


def get_excavator_label(equipment):
    return f'Экс {get_equipment_number(equipment)}'


def get_shift_state(employee):
    current_shift = (
        EmployeeShift.objects
        .select_related('employee')
        .filter(closed_at__isnull=True, employee=employee)
        .order_by('-opened_at')
        .first()
    )
    open_master_shifts = (
        EmployeeShift.objects
        .select_related('employee')
        .filter(
            closed_at__isnull=True,
            employee__accesses__role__code='mining_master',
            employee__accesses__is_active=True,
        )
        .distinct()
    )
    blocking_shift = open_master_shifts.exclude(employee=employee).first()
    return current_shift, blocking_shift


def get_shift_state_for_access(access):
    if access.role.code == 'mining_master':
        return get_shift_state(access.employee)
    blocking_shift = (
        EmployeeShift.objects
        .select_related('employee')
        .filter(
            closed_at__isnull=True,
            employee__accesses__role__code='mining_master',
            employee__accesses__is_active=True,
        )
        .distinct()
        .order_by('-opened_at')
        .first()
    )
    return None, blocking_shift


def get_shift_type_for_now(now):
    return ShiftType.DAY if 8 <= timezone.localtime(now).hour < 20 else ShiftType.NIGHT


def build_truck_tile(equipment, status_key, status_label, assignment=None, active_trip=None):
    label = get_truck_label(equipment)
    number = get_equipment_number(equipment)
    return {
        'id': equipment.id,
        'assignment_id': assignment.id if assignment else '',
        'number': number,
        'label': label,
        'garage_number': equipment.garage_number,
        'status_key': status_key,
        'status_label': status_label,
        'icon': TRUCK_ICON_BY_STATUS[status_key],
        'model': equipment.model.name if equipment.model else 'модель не указана',
        'search': f'{number} {label} {equipment.garage_number}'.lower(),
        'is_active_trip': bool(active_trip),
    }


def build_excavator_tile(equipment, status_key, status_label):
    number = get_equipment_number(equipment)
    label = get_excavator_label(equipment)
    return {
        'id': equipment.id,
        'number': number,
        'label': label,
        'garage_number': equipment.garage_number,
        'status_key': status_key,
        'status_label': status_label,
        'percent': 100,
        'icon': EXCAVATOR_ICON_BY_STATUS[status_key],
        'model': equipment.model.name if equipment.model else 'модель не указана',
        'search': f'{number} {label} {equipment.garage_number}'.lower(),
    }


def format_datetime_short(value):
    if not value:
        return ''
    return timezone.localtime(value).strftime('%d.%m %H:%M')


def format_decimal_short(value):
    if value is None:
        return ''
    return f'{value:g}'


def build_employee_badge(employee):
    if not employee:
        return None
    photo_url = ''
    if employee.photo:
        try:
            photo_url = employee.photo.url
        except ValueError:
            photo_url = ''
    initials = ''.join(part[0] for part in (employee.full_name or '').split()[:2]).upper()
    return {
        'name': employee.full_name or '',
        'phone': employee.phone or '',
        'position': employee.position or '',
        'photo': photo_url,
        'initials': initials or '??',
    }


def build_employee_initials(employee):
    if not employee:
        return '--'
    initials = ''.join(part[0] for part in (employee.full_name or '').split()[:2]).upper()
    return initials or '--'


def get_complex_truck_scale_class(truck_count):
    if truck_count <= 6:
        return 'truck-fill-1'
    if truck_count <= 12:
        return 'truck-fill-2'
    if truck_count <= 18:
        return 'truck-fill-3'
    return 'truck-fill-4'


def build_equipment_card_data(
    equipment,
    tile,
    zone_label,
    status_label,
    active_assignment=None,
    active_trip=None,
    downtime=None,
    shift_stats=None,
    truck_count=None,
    latest_trip=None,
    current_employee=None,
):
    model = equipment.model
    details = []
    details.append({'label': 'Гаражный N', 'value': equipment.garage_number})
    if equipment.vin:
        details.append({'label': 'VIN/серийный N', 'value': equipment.vin})
    if model:
        details.append({'label': 'Модель', 'value': model.name})
        if model.payload_tons:
            details.append({'label': 'ГП, т', 'value': format_decimal_short(model.payload_tons)})
        if model.body_volume_m3:
            details.append({'label': 'Кузов/ковш, м3', 'value': format_decimal_short(model.body_volume_m3)})
    else:
        details.append({'label': 'Модель', 'value': 'не указана'})
    if equipment.equipment_type.name == 'Самосвал':
        if active_assignment:
            details.append({'label': 'Назначен', 'value': format_datetime_short(active_assignment.assigned_at)})
        if active_trip:
            details.append({'label': 'Рейс', 'value': 'активный'})
            details.append({'label': 'Экскаватор рейса', 'value': get_excavator_label(active_trip.excavator)})
            details.append({'label': 'Разгрузка', 'value': str(active_trip.dump_point)})
            details.append({'label': 'Порода', 'value': str(active_trip.rock_type)})
            if active_trip.volume_m3:
                details.append({'label': 'Объем, м3', 'value': format_decimal_short(active_trip.volume_m3)})
        elif not active_assignment:
            details.append({'label': 'Назначение', 'value': 'ожидает'})
    else:
        stats = shift_stats or {}
        details.append({'label': 'Самосвалы', 'value': str(truck_count or 0)})
        details.append({'label': 'Рейсы', 'value': str(stats.get('total') or 0)})
        details.append({'label': 'Объем, м3', 'value': format_decimal_short(stats.get('volume') or Decimal('0'))})
        source_trip = active_trip or latest_trip
        details.append({'label': 'Горизонт', 'value': (source_trip.loading_horizon if source_trip else '') or 'не указан'})
        details.append({'label': 'Блок', 'value': (source_trip.loading_block if source_trip else '') or 'не указан'})

    if downtime:
        details.append({'label': 'Простой', 'value': str(downtime.reason)})
        details.append({'label': 'С начала', 'value': format_datetime_short(downtime.started_at)})

    return {
        'id': equipment.id,
        'type': equipment.equipment_type.name,
        'label': tile['label'],
        'number': tile['number'],
        'icon': tile['icon'],
        'status_key': tile['status_key'],
        'status_label': status_label,
        'zone': zone_label,
        'employee': build_employee_badge(current_employee),
        'details': details,
    }


def close_active_assignment(assignment, now):
    assignment.status = AssignmentStatus.CANCELLED
    assignment.ended_at = now
    assignment.save(update_fields=['status', 'ended_at'])


def close_active_assignments(assignments, now):
    for assignment in assignments:
        close_active_assignment(assignment, now)


def handle_shift_action(request, action, access, current_shift, blocking_shift):
    now = timezone.now()
    if action == 'start_shift':
        if current_shift:
            messages.info(request, 'Ваша смена уже открыта.')
            return
        if blocking_shift:
            messages.error(request, 'Предыдущий горный мастер еще не закрыл смену.')
            return
        EmployeeShift.objects.create(
            employee=access.employee,
            shift_type=get_shift_type_for_now(now),
            opened_at=now,
            opened_by=access.employee,
        )
        messages.success(request, 'Смена горного мастера открыта. Расстановка унаследована.')
        return

    if action == 'end_shift':
        if not current_shift:
            messages.error(request, 'Нет открытой смены для завершения.')
            return
        current_shift.closed_at = now
        current_shift.closed_by = access.employee
        current_shift.save(update_fields=['closed_at', 'closed_by'])
        messages.success(request, 'Смена горного мастера завершена.')


def authenticate_shared_shift_start(request):
    phone = request.POST.get('reauth_phone', '').strip()
    access_code = re.sub(r'\D', '', request.POST.get('reauth_access_code', ''))
    device_kind = request.POST.get('device_kind', '').strip()
    if not phone or not access_code:
        return None, 'Для начала смены на общем компьютере введите телефон и код горного мастера.'
    if phone and not phone.startswith(('+', '7', '8')):
        phone = f'+7 {phone}'

    access = find_employee_access_by_credentials(phone, access_code, role_code='mining_master')
    if not access:
        return None, 'Телефон или код горного мастера указаны неверно.'

    request.session['employee_access_id'] = access.id
    set_session_device_kind(request, device_kind)
    access.last_login_at = timezone.now()
    access.save(update_fields=['last_login_at'])
    return access, ''


def handle_assignment_action(request, action, access, current_shift):
    if not current_shift:
        messages.error(request, 'Сначала откройте смену горного мастера.')
        return

    now = timezone.now()
    truck_id = request.POST.get('truck')
    if not truck_id:
        messages.error(request, 'Самосвал не выбран.')
        return

    truck = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=truck_id,
        equipment_type__name='Самосвал',
        is_active=True,
    )
    active_assignments = list(
        HaulAssignment.objects
        .filter(truck=truck, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .select_related('excavator')
        .order_by('-assigned_at')
    )
    active_assignment = active_assignments[0] if active_assignments else None

    if action == 'release':
        if not active_assignment:
            messages.info(request, f'{get_truck_label(truck)} уже находится в гараже.')
            return
        close_active_assignments(active_assignments, now)
        messages.success(request, f'{get_truck_label(truck)} снят из комплекса и отправлен в гараж.')
        return

    excavator_id = request.POST.get('excavator')
    if not excavator_id:
        messages.error(request, 'Экскаватор не выбран.')
        return
    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=excavator_id,
        equipment_type__name='Экскаватор',
        is_active=True,
    )

    if active_assignment and active_assignment.excavator_id == excavator.id:
        close_active_assignments(active_assignments[1:], now)
        messages.info(request, f'{get_truck_label(truck)} уже назначен на {get_excavator_label(excavator)}.')
        return
    close_active_assignments(active_assignments, now)

    HaulAssignment.objects.create(
        truck=truck,
        excavator=excavator,
        assigned_by=access.employee,
        status=AssignmentStatus.PENDING,
    )
    messages.success(request, f'{get_truck_label(truck)} назначен на {get_excavator_label(excavator)}.')


def get_active_assignments_queryset():
    return (
        HaulAssignment.objects
        .filter(ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .select_related('truck', 'excavator')
    )


def mining_master_access_from_request(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(id=access_id, is_active=True)
        .first()
    )
    if not access or access.role.code != 'mining_master':
        return None
    return access


def mining_master_json_payload(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return {}


def mining_master_open_shift_or_error(access):
    if access.role.code != 'mining_master':
        return None, 'Изменять пульт Горного мастера может только Горный мастер.'
    current_shift, blocking_shift = get_shift_state_for_access(access)
    if not current_shift:
        if blocking_shift:
            return None, 'Предыдущий горный мастер еще не закрыл смену.'
        return None, 'Сначала откройте смену горного мастера.'
    return current_shift, ''


def build_mining_master_dispatcher_header(request, access, current_shift, blocking_shift):
    active_person = access.employee if current_shift else (blocking_shift.employee if blocking_shift else None)
    active_shift_opened_at = ''
    active_shift_date = ''
    if current_shift:
        active_shift_date = timezone.localtime(current_shift.opened_at).strftime('%d.%m.%Y')
        active_shift_opened_at = timezone.localtime(current_shift.opened_at).strftime('%H:%M')
    elif blocking_shift:
        active_shift_date = timezone.localtime(blocking_shift.opened_at).strftime('%d.%m.%Y')
        active_shift_opened_at = timezone.localtime(blocking_shift.opened_at).strftime('%H:%M')

    photo_url = ''
    if active_person and getattr(active_person, 'photo', None):
        try:
            photo_url = active_person.photo.url
        except ValueError:
            photo_url = ''

    can_start_shift = not current_shift and not blocking_shift
    requires_shift_reauth = can_start_shift
    current_time = timezone.localtime().strftime('%H:%M')
    current_date = timezone.localdate().strftime('%d.%m.%Y')
    if current_shift:
        shift_label = 'Смена открыта'
        time_range = f'с {active_shift_opened_at}'
        clock_caption = 'в работе'
    elif blocking_shift:
        shift_label = 'Режим наблюдателя'
        time_range = f'с {active_shift_opened_at}' if active_shift_opened_at else 'ожидание закрытия'
        clock_caption = 'наблюдение'
    else:
        shift_label = ''
        time_range = ''
        clock_caption = ''

    return {
        'header': {
            'active_shift': current_shift,
            'own_shift': current_shift,
            'active_dispatcher': active_person,
            'active_dispatcher_name': active_person.full_name if active_person else '',
            'active_dispatcher_photo': photo_url,
            'active_dispatcher_initials': build_employee_initials(active_person),
            'active_shift_date': active_shift_date,
            'active_shift_opened_at': active_shift_opened_at,
            'can_toggle_shift': bool(current_shift or can_start_shift),
            'shift_is_open': bool(current_shift),
            'active_role_label': 'горный мастер',
            'active_shift_title': 'Активная смена горного мастера',
            'inactive_shift_title': 'Смена горного мастера не открыта',
            'inactive_name': 'смена не открыта',
            'shift_form_action': request.get_full_path(),
            'shift_action_field_name': 'action',
            'shift_start_value': 'start_shift',
            'shift_end_value': 'end_shift',
            'shift_start_label': 'Начать смену',
            'shift_end_label': 'Завершить смену',
            'shift_start_confirm': 'Начать смену горного мастера?',
            'shift_end_confirm': 'Завершить смену горного мастера?',
            'shift_end_confirm_title': 'Завершение смены',
            'shift_end_confirm_description': 'Вы уверены, что хотите завершить текущую смену? После завершения смены будут сохранены результаты работы.',
            'shift_end_confirm_role': 'Горный мастер',
            'shift_button_marker': True,
            'requires_shift_reauth': requires_shift_reauth,
            'session_device_kind': get_session_device_kind(request),
        },
        'context': {
            'dispatcher_page_title': 'Горный мастер',
            'dispatcher_compat_title': 'Пульт горного мастера',
            'dispatcher_board_label': 'Горный мастер',
            'dispatcher_identity_label': 'Активный горный мастер',
            'dispatcher_main_label': 'Пульт горного мастера',
            'dispatcher_actions_label': 'Действия смены горного мастера',
            'dispatcher_clock_label': 'Текущее время смены горного мастера',
            'dispatcher_nav_label': 'Навигация горного мастера',
            'dispatcher_header_screen': 'ГОРНЫЙ МАСТЕР',
            'dispatcher_header_shift_label': shift_label,
            'dispatcher_header_time_range': time_range,
            'dispatcher_clock_caption': clock_caption,
            'current_time': current_time,
            'current_date': current_date,
            'mining_master_mobile_enabled': True,
            'dispatcher_move_excavator_url': reverse('mining_master_move_excavator'),
            'dispatcher_assign_truck_url': reverse('mining_master_assign_truck'),
            'dispatcher_nav_items': [
                {'label': 'ПУЛЬТ', 'href': '#', 'active': True},
                {'label': 'ТЕХНИКА', 'href': '#', 'active': False},
                {'label': 'ОТЧЕТЫ', 'href': '#', 'active': False},
                {'label': 'ЖУРНАЛ', 'href': '#', 'active': False},
            ],
        },
    }


@require_POST
def mining_master_move_excavator_view(request):
    access = mining_master_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану горного мастера.'}, status=403)
    current_shift, error = mining_master_open_shift_or_error(access)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=400)

    payload = mining_master_json_payload(request)
    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=payload.get('excavator_id'),
        equipment_type__name='Экскаватор',
        is_active=True,
    )
    zone = payload.get('zone')
    if zone not in {ExcavatorPlacement.Zone.ACTIVE, ExcavatorPlacement.Zone.INACTIVE}:
        return JsonResponse({'ok': False, 'error': 'Некорректная зона экскаватора.'}, status=400)

    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=excavator)
    placement.zone = zone
    placement.changed_by = access.employee
    placement.save(update_fields=['zone', 'changed_by', 'changed_at'])

    closed_count = 0
    if zone == ExcavatorPlacement.Zone.INACTIVE:
        active_assignments = list(get_active_assignments_queryset().filter(excavator=excavator))
        closed_count = len(active_assignments)
        close_active_assignments(active_assignments, timezone.now())

    return JsonResponse({'ok': True, 'closed': closed_count})


@require_POST
def mining_master_assign_truck_view(request):
    access = mining_master_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Нет доступа к экрану горного мастера.'}, status=403)
    current_shift, error = mining_master_open_shift_or_error(access)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=400)

    payload = mining_master_json_payload(request)
    action = payload.get('action')
    now = timezone.now()

    if action == 'release_complex':
        excavator = get_object_or_404(
            Equipment.objects.select_related('equipment_type', 'model'),
            id=payload.get('excavator_id'),
            equipment_type__name='Экскаватор',
            is_active=True,
        )
        assignments = list(get_active_assignments_queryset().filter(excavator=excavator))
        close_active_assignments(assignments, now)
        return JsonResponse({'ok': True, 'closed': len(assignments)})

    truck = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=payload.get('truck_id'),
        equipment_type__name='Самосвал',
        is_active=True,
    )
    active_assignments = list(
        HaulAssignment.objects
        .filter(truck=truck, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .select_related('excavator')
        .order_by('-assigned_at')
    )

    if action == 'release':
        close_active_assignments(active_assignments, now)
        return JsonResponse({'ok': True, 'closed': len(active_assignments)})

    if action != 'assign':
        return JsonResponse({'ok': False, 'error': 'Неизвестное действие.'}, status=400)

    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=payload.get('excavator_id'),
        equipment_type__name='Экскаватор',
        is_active=True,
    )
    if not ExcavatorPlacement.objects.filter(excavator=excavator, zone=ExcavatorPlacement.Zone.ACTIVE).exists():
        return JsonResponse({'ok': False, 'error': 'Самосвал можно назначить только в активный комплекс.'}, status=400)

    active_assignment = active_assignments[0] if active_assignments else None
    if active_assignment and active_assignment.excavator_id == excavator.id:
        close_active_assignments(active_assignments[1:], now)
        return JsonResponse({'ok': True, 'already_assigned': True})

    close_active_assignments(active_assignments, now)
    HaulAssignment.objects.create(
        truck=truck,
        excavator=excavator,
        assigned_by=access.employee,
        status=AssignmentStatus.PENDING,
    )
    return JsonResponse({'ok': True})


def handle_bulk_release_action(request, action, current_shift, access):
    if not current_shift:
        messages.error(request, 'Сначала откройте смену горного мастера.')
        return

    now = timezone.now()
    assignments = get_active_assignments_queryset()

    if action == 'release_excavator':
        excavator_id = request.POST.get('excavator')
        if not excavator_id:
            messages.error(request, 'Экскаватор не выбран.')
            return
        excavator = get_object_or_404(
            Equipment.objects.select_related('equipment_type', 'model'),
            id=excavator_id,
            equipment_type__name='Экскаватор',
            is_active=True,
        )
        assignments = list(assignments.filter(excavator=excavator))
        if not assignments:
            messages.info(request, f'{get_excavator_label(excavator)} уже пустой.')
            return
        close_active_assignments(assignments, now)
        messages.success(request, f'{get_excavator_label(excavator)} расформирован. Самосвалы возвращены в гараж.')
        return

    if action == 'release_all':
        assignments = list(assignments)
        active_excavator_placements = list(
            ExcavatorPlacement.objects
            .filter(zone=ExcavatorPlacement.Zone.ACTIVE, excavator__is_active=True)
            .select_related('excavator')
        )
        if not assignments and not active_excavator_placements:
            messages.info(request, 'Вся техника уже находится в неактивной смене.')
            return
        close_active_assignments(assignments, now)
        for placement in active_excavator_placements:
            placement.zone = ExcavatorPlacement.Zone.INACTIVE
            placement.changed_by = access.employee
            placement.save(update_fields=['zone', 'changed_by', 'changed_at'])
        messages.success(request, 'Все комплексы расформированы. Вся техника возвращена в неактивную смену.')


def handle_excavator_placement_action(request, action, access, current_shift):
    if not current_shift:
        messages.error(request, 'Сначала откройте смену горного мастера.')
        return

    excavator_id = request.POST.get('excavator')
    if not excavator_id:
        messages.error(request, 'Экскаватор не выбран.')
        return

    excavator = get_object_or_404(
        Equipment.objects.select_related('equipment_type', 'model'),
        id=excavator_id,
        equipment_type__name='Экскаватор',
        is_active=True,
    )
    placement, _ = ExcavatorPlacement.objects.get_or_create(excavator=excavator)
    now = timezone.now()

    if action == 'activate_excavator':
        placement.zone = ExcavatorPlacement.Zone.ACTIVE
        placement.changed_by = access.employee
        placement.save(update_fields=['zone', 'changed_by', 'changed_at'])
        messages.success(request, f'{get_excavator_label(excavator)} добавлен в активную смену.')
        return

    if action == 'deactivate_excavator':
        active_assignments = list(get_active_assignments_queryset().filter(excavator=excavator))
        close_active_assignments(active_assignments, now)
        placement.zone = ExcavatorPlacement.Zone.INACTIVE
        placement.changed_by = access.employee
        placement.save(update_fields=['zone', 'changed_by', 'changed_at'])
        messages.success(request, f'{get_excavator_label(excavator)} перенесен в неактивную смену.')


def mining_master_assignments_view(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return redirect('login')
    access = EmployeeAccess.objects.select_related('employee', 'role').filter(id=access_id, is_active=True).first()
    if not access or access.role.code != 'mining_master':
        return redirect('role_home')

    current_shift, blocking_shift = get_shift_state_for_access(access)

    if request.method == 'POST':
        action = request.POST.get('action') or 'assign'
        if action in {'start_shift', 'end_shift'}:
            if action == 'start_shift':
                reauth_access, reauth_error = authenticate_shared_shift_start(request)
                if reauth_error:
                    messages.error(request, reauth_error)
                    return redirect('mining_master_assignments')
                access = reauth_access
                current_shift, blocking_shift = get_shift_state_for_access(access)
            handle_shift_action(request, action, access, current_shift, blocking_shift)
        elif action in {'assign', 'release'}:
            handle_assignment_action(request, action, access, current_shift)
        elif action in {'release_excavator', 'release_all'}:
            handle_bulk_release_action(request, action, current_shift, access)
        elif action in {'activate_excavator', 'deactivate_excavator'}:
            handle_excavator_placement_action(request, action, access, current_shift)
        else:
            messages.error(request, 'Неизвестное действие.')
        return redirect('mining_master_assignments')

    master_dispatcher = build_mining_master_dispatcher_header(request, access, current_shift, blocking_shift)
    return render_dispatcher_control_view(
        request,
        access_override=access,
        enforce_dispatcher_access=False,
        dispatcher_header_override=master_dispatcher['header'],
        context_overrides=master_dispatcher['context'],
    )

    form = HaulAssignmentForm()
    now = timezone.now()
    shift_window_start = current_shift.opened_at if current_shift else now - timedelta(hours=12)

    excavators = list(
        Equipment.objects
        .filter(equipment_type__name='Экскаватор', is_active=True)
        .select_related('equipment_type', 'model')
        .order_by('garage_number')
    )
    active_excavator_ids = set(
        ExcavatorPlacement.objects
        .filter(excavator__in=excavators, zone=ExcavatorPlacement.Zone.ACTIVE)
        .values_list('excavator_id', flat=True)
    )
    trucks = list(
        Equipment.objects
        .filter(equipment_type__name='Самосвал', is_active=True)
        .select_related('equipment_type', 'model')
        .order_by('garage_number')
    )
    active_assignment_rows = list(
        HaulAssignment.objects
        .select_related('truck', 'truck__model', 'excavator', 'excavator__model', 'assigned_by')
        .filter(ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .order_by('truck_id', '-assigned_at')
    )
    active_assignments_by_truck = {}
    for assignment in active_assignment_rows:
        active_assignments_by_truck.setdefault(assignment.truck_id, assignment)
    active_assignments = list(active_assignments_by_truck.values())

    current_employee_by_equipment = {}
    for equipment_assignment in (
        EquipmentAssignment.objects
        .filter(ended_at__isnull=True, equipment__is_active=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .select_related('employee', 'equipment')
        .order_by('equipment_id', '-assigned_at')
    ):
        current_employee_by_equipment.setdefault(equipment_assignment.equipment_id, equipment_assignment.employee)

    assignments_by_excavator = defaultdict(list)
    for assignment in active_assignments:
        assignments_by_excavator[assignment.excavator_id].append(assignment)

    active_trip_by_truck = {
        trip.truck_id: trip
        for trip in (
            Trip.objects
            .filter(status=TripStatus.ACTIVE)
            .select_related('truck', 'excavator', 'rock_type', 'dump_point')
        )
    }
    open_downtime_by_equipment = {
        event.equipment_id: event
        for event in DowntimeEvent.objects.filter(ended_at__isnull=True).select_related('reason', 'equipment')
    }
    trips_by_excavator = {
        item['excavator_id']: item
        for item in (
            Trip.objects
            .filter(created_at__gte=shift_window_start)
            .exclude(status=TripStatus.CANCELLED)
            .values('excavator_id')
            .annotate(total=Count('id'), volume=Sum('volume_m3'))
        )
    }
    latest_trip_by_excavator = {}
    for trip in (
        Trip.objects
        .filter(created_at__gte=shift_window_start)
        .exclude(status=TripStatus.CANCELLED)
        .select_related('excavator', 'truck', 'rock_type', 'dump_point')
        .order_by('-created_at')
    ):
        latest_trip_by_excavator.setdefault(trip.excavator_id, trip)

    complexes = []
    equipment_tiles_by_status = {'green': [], 'yellow': [], 'blue': [], 'red': [], 'gray': []}
    equipment_cards = {}
    total_volume = Decimal('0')
    total_trips = 0
    not_work_count = 0

    inactive_excavator_tiles = []

    for excavator in excavators:
        if excavator.id not in active_excavator_ids:
            downtime = open_downtime_by_equipment.get(excavator.id)
            if downtime and downtime.reason.is_critical:
                excavator_status = 'red'
                excavator_label = 'Ремонт'
            elif downtime:
                excavator_status = 'yellow'
                excavator_label = 'ОФР'
            else:
                excavator_status = 'gray'
                excavator_label = 'Неактивная'
            tile = build_excavator_tile(excavator, excavator_status, excavator_label)
            inactive_excavator_tiles.append(tile)
            equipment_tiles_by_status[excavator_status].append(tile)
            equipment_cards[str(excavator.id)] = build_equipment_card_data(
                excavator,
                tile,
                'Неактивная смена',
                excavator_label,
                downtime=downtime,
                shift_stats={},
                truck_count=0,
                latest_trip=latest_trip_by_excavator.get(excavator.id),
                current_employee=current_employee_by_equipment.get(excavator.id),
            )
            continue

        downtime = open_downtime_by_equipment.get(excavator.id)
        trip_stats = trips_by_excavator.get(excavator.id, {})
        trips_count = trip_stats.get('total') or 0
        volume = trip_stats.get('volume') or Decimal('0')
        total_volume += volume
        total_trips += trips_count

        if downtime and downtime.reason.is_critical:
            excavator_status = 'red'
            excavator_label = 'Ремонт'
            complex_state = 'state-danger'
            not_work_count += 1
        elif downtime:
            excavator_status = 'yellow'
            excavator_label = 'ОФР'
            complex_state = 'state-warning'
            not_work_count += 1
        elif assignments_by_excavator.get(excavator.id) or trips_count:
            excavator_status = 'green'
            excavator_label = 'Работает'
            complex_state = 'state-normal'
        else:
            excavator_status = 'gray'
            excavator_label = 'Без сам.'
            complex_state = 'state-neutral'
            not_work_count += 1

        truck_tiles = []
        for assignment in assignments_by_excavator.get(excavator.id, []):
            active_trip = active_trip_by_truck.get(assignment.truck_id)
            if active_trip:
                truck_status = 'blue'
                truck_status_label = 'В рейсе'
            elif assignment.status == AssignmentStatus.PENDING:
                truck_status = 'yellow'
                truck_status_label = 'Ожидает'
            else:
                truck_status = 'green'
                truck_status_label = 'Работает'
            tile = build_truck_tile(assignment.truck, truck_status, truck_status_label, assignment, active_trip)
            truck_tiles.append(tile)
            equipment_tiles_by_status[truck_status].append(tile)
            equipment_cards[str(assignment.truck_id)] = build_equipment_card_data(
                assignment.truck,
                tile,
                f'Комплекс {get_excavator_label(excavator)}',
                truck_status_label,
                active_assignment=assignment,
                active_trip=active_trip,
                downtime=open_downtime_by_equipment.get(assignment.truck_id),
                current_employee=current_employee_by_equipment.get(assignment.truck_id),
            )

        excavator_tile = build_excavator_tile(excavator, excavator_status, excavator_label)
        latest_trip = latest_trip_by_excavator.get(excavator.id)
        current_horizon = f'Гор. {latest_trip.loading_horizon}' if latest_trip and latest_trip.loading_horizon else 'Гор. -'
        current_block = f'Блок {latest_trip.loading_block}' if latest_trip and latest_trip.loading_block else 'Блок -'
        current_rock = latest_trip.rock_type.name if latest_trip and latest_trip.rock_type else 'порода не указана'
        equipment_tiles_by_status[excavator_status].append(excavator_tile)
        equipment_cards[str(excavator.id)] = build_equipment_card_data(
            excavator,
            excavator_tile,
            'Активная смена',
            excavator_label,
            downtime=downtime,
            shift_stats=trip_stats,
            truck_count=len(truck_tiles),
            latest_trip=latest_trip_by_excavator.get(excavator.id),
            current_employee=current_employee_by_equipment.get(excavator.id),
        )
        complexes.append({
            'excavator': excavator,
            'tile': excavator_tile,
            'state': complex_state,
            'trucks': truck_tiles,
            'truck_count': len(truck_tiles),
            'volume': volume,
            'trips_count': trips_count,
            'downtime': downtime,
            'truck_scale_class': get_complex_truck_scale_class(len(truck_tiles)),
            'truck_column_count': 6,
            'current_horizon': current_horizon,
            'current_block': current_block,
            'current_rock': current_rock,
        })

    garage_tiles = []
    for truck in trucks:
        if truck.id in active_assignments_by_truck:
            continue
        active_trip = active_trip_by_truck.get(truck.id)
        status_key = 'blue' if active_trip else 'gray'
        status_label = 'В рейсе' if active_trip else 'Гараж'
        tile = build_truck_tile(truck, status_key, status_label, active_trip=active_trip)
        garage_tiles.append(tile)
        equipment_tiles_by_status[status_key].append(tile)
        equipment_cards[str(truck.id)] = build_equipment_card_data(
            truck,
            tile,
            'Неактивная смена',
            status_label,
            active_trip=active_trip,
            downtime=open_downtime_by_equipment.get(truck.id),
            current_employee=current_employee_by_equipment.get(truck.id),
        )

    pending_count = sum(1 for assignment in active_assignments if assignment.status == AssignmentStatus.PENDING)
    accepted_count = sum(1 for assignment in active_assignments if assignment.status == AssignmentStatus.ACCEPTED)
    selected_tile = garage_tiles[0] if garage_tiles else (complexes[0]['trucks'][0] if complexes and complexes[0]['trucks'] else None)
    can_edit = bool(current_shift)
    can_start_shift = not current_shift and not blocking_shift
    current_time = timezone.localtime().strftime('%H:%M')
    current_date = timezone.localdate().strftime('%d.%m.%Y')
    shift_opened_at = timezone.localtime(current_shift.opened_at).strftime('%H:%M') if current_shift else ''
    if current_shift:
        dispatcher_header_shift_label = 'Смена открыта'
        dispatcher_header_time_range = f'с {shift_opened_at}'
        dispatcher_clock_caption = 'в работе'
    elif blocking_shift:
        dispatcher_header_shift_label = 'Режим наблюдателя'
        dispatcher_header_time_range = 'ожидание закрытия'
        dispatcher_clock_caption = 'наблюдение'
    else:
        dispatcher_header_shift_label = ''
        dispatcher_header_time_range = ''
        dispatcher_clock_caption = 'готово'
    employee_photo = ''
    if getattr(access.employee, 'photo', None):
        try:
            employee_photo = access.employee.photo.url
        except ValueError:
            employee_photo = ''
    dispatcher_header = {
        'active_shift': current_shift,
        'active_dispatcher': access.employee if current_shift else None,
        'active_dispatcher_name': access.employee.full_name or 'Горный мастер',
        'active_dispatcher_photo': employee_photo,
        'active_dispatcher_initials': build_employee_initials(access.employee),
        'active_shift_opened_at': shift_opened_at,
        'can_toggle_shift': bool(current_shift or can_start_shift),
        'shift_is_open': bool(current_shift),
        'active_role_label': 'горный мастер',
        'active_shift_title': 'Активная смена горного мастера',
        'inactive_shift_title': 'Активная смена горного мастера не открыта',
        'inactive_name': 'смена не открыта',
        'shift_form_action': request.get_full_path(),
        'shift_action_field_name': 'action',
        'shift_start_value': 'start_shift',
        'shift_end_value': 'end_shift',
        'shift_start_label': 'Начать смену',
        'shift_end_label': 'Завершить смену',
        'shift_start_confirm': 'Начать смену горного мастера?',
        'shift_end_confirm': 'Завершить смену горного мастера?',
        'shift_button_marker': True,
    }
    dispatcher_nav_items = [
        {'label': 'Пульт', 'href': '#', 'active': True, 'data_tab': 'complexes'},
        {'label': 'Техника', 'href': '#', 'active': False, 'data_tab': 'equipment'},
        {'label': 'Отчеты', 'href': '#', 'active': False, 'data_tab': 'dashboards'},
        {'label': 'Журнал', 'href': '#', 'active': False, 'data_tab': 'shift'},
    ]

    dashboard_rows = sorted(complexes, key=lambda item: item['volume'], reverse=True)
    max_volume = max([item['volume'] for item in dashboard_rows] or [Decimal('1')]) or Decimal('1')
    for item in dashboard_rows:
        item['volume_percent'] = int((item['volume'] / max_volume) * 100) if max_volume else 0
    desktop_complex_empty_slots = range(max(0, 9 - len(complexes)))

    return render(
        request,
        'assignments/mining_master_assignments.html',
        {
            'access': access,
            'form': form,
            'current_shift': current_shift,
            'blocking_shift': blocking_shift,
            'can_edit': can_edit,
            'can_start_shift': can_start_shift,
            'master_initials': build_employee_initials(access.employee),
            'current_time': current_time,
            'current_date': current_date,
            'dispatcher_header': dispatcher_header,
            'dispatcher_nav_items': dispatcher_nav_items,
            'dispatcher_header_shift_label': dispatcher_header_shift_label,
            'dispatcher_header_time_range': dispatcher_header_time_range,
            'dispatcher_clock_caption': dispatcher_clock_caption,
            'complexes': complexes,
            'desktop_complex_empty_slots': desktop_complex_empty_slots,
            'garage_tiles': garage_tiles,
            'inactive_excavator_tiles': inactive_excavator_tiles,
            'selected_tile': selected_tile,
            'pending_assignments_count': pending_count,
            'accepted_assignments_count': accepted_count,
            'active_assignments_count': len(active_assignments),
            'active_excavators_count': len([item for item in complexes if item['tile']['status_key'] == 'green']),
            'free_trucks_count': len(garage_tiles),
            'not_work_count': not_work_count,
            'total_volume': total_volume,
            'total_trips': total_trips,
            'equipment_tiles_by_status': equipment_tiles_by_status,
            'equipment_cards': equipment_cards,
            'dashboard_rows': dashboard_rows,
            'shift_window_start': shift_window_start,
        },
    )
