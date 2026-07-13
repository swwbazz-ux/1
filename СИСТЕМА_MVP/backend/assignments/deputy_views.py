import json
from datetime import date
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from references.models import Equipment
from users.models import Employee, EmployeeAccess, Role

from .models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from .services import (
    WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES,
    get_or_create_crew_draft,
    production_work_date,
    publish_crew_plan,
    update_crew_draft_slot,
)


DEPUTY_ROLE_CODE = 'deputy_mining_manager'
TARGET_ROLE_LABELS = {
    'driver': 'Самосвалы',
    'excavator_operator': 'Экскаваторы',
}


def deputy_access_from_request(request):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    return (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            id=access_id,
            is_active=True,
            employee__is_active=True,
            role__is_active=True,
            role__code=DEPUTY_ROLE_CODE,
        )
        .exclude(status__in=[EmployeeAccess.Status.BLOCKED, EmployeeAccess.Status.DEACTIVATED])
        .first()
    )


def _employee_short_name(employee):
    parts = (employee.full_name or '').split()
    if not parts:
        return ''
    initials = ''.join(f'{part[0].upper()}.' for part in parts[1:3] if part)
    return f'{parts[0]} {initials}'.strip()


def _employee_payload(employee):
    if not employee:
        return None
    photo_url = ''
    if employee.photo:
        try:
            photo_url = employee.photo.url
        except ValueError:
            photo_url = ''
    initials = ''.join(part[0] for part in (employee.full_name or '').split()[:2]).upper() or '—'
    return {
        'id': employee.id,
        'full_name': employee.full_name or '',
        'short_name': _employee_short_name(employee),
        'position_label': employee.position or '',
        'personnel_number': employee.personnel_number or '',
        'photo_url': photo_url,
        'initials': initials,
        'search': f'{employee.full_name} {employee.personnel_number}'.strip().lower(),
    }


def _role_from_request(request):
    role_code = request.GET.get('role') or request.POST.get('role') or 'driver'
    if role_code not in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES:
        role_code = 'driver'
    return get_object_or_404(Role, code=role_code, is_active=True)


def _work_date_from_request(request):
    current_date = production_work_date()
    raw_value = request.GET.get('date', '').strip()
    if not raw_value:
        return current_date
    try:
        selected_date = date.fromisoformat(raw_value)
    except ValueError:
        return current_date
    return min(selected_date, current_date)


def _slot_issue(slot, activated_employee_ids, other_role_assignment_employee_ids):
    employee = slot.employee
    if not employee:
        return ''
    if not employee.is_active or employee.status != Employee.Status.ACTIVE:
        return 'Сотрудник неактивен'
    if employee.id not in activated_employee_ids:
        return 'Нет активного доступа'
    if employee.id in other_role_assignment_employee_ids:
        return 'Назначен по другой роли'
    if not slot.equipment.is_active:
        return 'Техника недоступна'
    return ''


def build_crew_plan_payload(plan, *, request=None):
    if not plan:
        return {
            'plan': None,
            'role': None,
            'categories': [],
            'endpoints': {},
            'summary': {
                'equipment_total': 0,
                'assigned_count': 0,
                'unfilled_count': 0,
                'conflict_count': 0,
                'changed_count': 0,
            },
            'employees': [],
            'rows': [],
        }

    slots = list(
        plan.slots
        .select_related('equipment', 'equipment__equipment_type', 'equipment__model', 'employee', 'baseline_employee')
        .order_by('equipment__garage_number', 'shift_type')
    )
    assigned_employee_ids = {slot.employee_id for slot in slots if slot.employee_id}
    activated_employee_ids = set(
        EmployeeAccess.objects.filter(
            role=plan.role,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
        ).values_list('employee_id', flat=True)
    )
    other_role_assignment_employee_ids = set(
        EquipmentAssignment.objects.filter(
            employee_id__in=activated_employee_ids,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
            shift_type__in=WorkShiftType.values,
        )
        .exclude(role=plan.role)
        .values_list('employee_id', flat=True)
    )
    editable = plan.status == CrewPlanStatus.DRAFT and plan.work_date == production_work_date()
    eligible_employees = []
    if editable:
        eligible_employees = list(
            Employee.objects.filter(id__in=activated_employee_ids)
            .exclude(id__in=assigned_employee_ids)
            .exclude(id__in=other_role_assignment_employee_ids)
            .order_by('full_name')
        )

    slot_map = {}
    for slot in slots:
        slot_map[(slot.equipment_id, slot.shift_type)] = slot

    equipment_items = []
    seen_equipment_ids = set()
    for slot in slots:
        if slot.equipment_id in seen_equipment_ids:
            continue
        seen_equipment_ids.add(slot.equipment_id)
        equipment_items.append(slot.equipment)

    rows = []
    assigned_count = 0
    unfilled_count = 0
    conflict_count = 0
    changed_count = 0
    icon_prefix = 'truck' if plan.role.code == 'driver' else 'excavator'
    for equipment in equipment_items:
        row_slots = []
        row_attention = False
        row_conflict = False
        row_changed = False
        for shift_type, shift_label in WorkShiftType.choices:
            slot = slot_map.get((equipment.id, shift_type))
            if not slot:
                continue
            issue = _slot_issue(
                slot,
                activated_employee_ids,
                other_role_assignment_employee_ids,
            )
            changed = slot.employee_id != slot.baseline_employee_id
            if slot.employee_id:
                assigned_count += 1
            else:
                unfilled_count += 1
                row_attention = True
            if issue:
                conflict_count += 1
                row_attention = True
                row_conflict = True
            if changed:
                changed_count += 1
                row_changed = True
            row_slots.append({
                'shift_type': shift_type,
                'label': 'День' if shift_type == WorkShiftType.SHIFT_1 else 'Ночь',
                'time_label': '07:00–19:00' if shift_type == WorkShiftType.SHIFT_1 else '19:00–07:00',
                'employee': _employee_payload(slot.employee),
                'changed': changed,
                'conflict': bool(issue),
                'issue': issue,
            })
        status_label = '' if equipment.is_active else 'Недоступна'
        rows.append({
            'equipment': {
                'id': equipment.id,
                'label': equipment.garage_number or str(equipment),
                'model_label': equipment.model.name if equipment.model_id else '',
                'icon_url': static(f'img/equipment/{icon_prefix}-{"green" if equipment.is_active else "gray"}.png'),
                'is_active': equipment.is_active,
                'status_label': status_label,
            },
            'attention': row_attention,
            'conflict': row_conflict,
            'changed': row_changed,
            'search': ' '.join([
                equipment.garage_number or '',
                equipment.model.name if equipment.model_id else '',
                *[item['employee']['full_name'] for item in row_slots if item['employee']],
            ]).lower(),
            'slots': row_slots,
        })

    categories = []
    selected_date = plan.work_date.isoformat()
    for role_code, equipment_type_name in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES.items():
        query = urlencode({'role': role_code, 'date': selected_date})
        categories.append({
            'code': role_code,
            'label': TARGET_ROLE_LABELS.get(role_code, equipment_type_name),
            'url': f'{reverse("deputy_mining_manager_placement")}?{query}',
        })

    return {
        'plan': {
            'id': plan.id,
            'version': plan.version,
            'editable': editable,
            'status': plan.status,
            'work_date': plan.work_date.isoformat(),
            'work_date_label': plan.work_date.strftime('%d.%m.%Y'),
            'updated_at_label': timezone.localtime(plan.updated_at).strftime('%H:%M'),
        },
        'role': {
            'code': plan.role.code,
            'label': plan.role.name,
            'category_label': TARGET_ROLE_LABELS.get(plan.role.code, plan.role.name),
        },
        'categories': categories,
        'endpoints': {
            'slot': reverse('deputy_mining_manager_slot'),
            'publish': reverse('deputy_mining_manager_publish'),
        },
        'summary': {
            'equipment_total': len(rows),
            'assigned_count': assigned_count,
            'unfilled_count': unfilled_count,
            'conflict_count': conflict_count,
            'changed_count': changed_count,
        },
        'employees': [_employee_payload(employee) for employee in eligible_employees],
        'rows': rows,
    }


def deputy_mining_manager_placement_view(request):
    access = deputy_access_from_request(request)
    if not access:
        return redirect('role_home')
    role = _role_from_request(request)
    selected_date = _work_date_from_request(request)
    requested_plan_id = request.GET.get('plan', '').strip()
    if requested_plan_id:
        plan = get_object_or_404(
            CrewPlan.objects.filter(
                role=role,
                status__in=[CrewPlanStatus.PUBLISHED, CrewPlanStatus.SUPERSEDED],
            ),
            id=requested_plan_id,
        )
        selected_date = plan.work_date
    elif selected_date == production_work_date():
        plan, _ = get_or_create_crew_draft(work_date=selected_date, role=role, actor=access.employee)
    else:
        plan = (
            CrewPlan.objects
            .filter(work_date=selected_date, role=role, status=CrewPlanStatus.PUBLISHED)
            .order_by('-revision')
            .first()
        )
    payload = build_crew_plan_payload(plan, request=request)
    return render(
        request,
        'assignments/deputy_mining_manager_placement.html',
        {
            'access': access,
            'planning_payload': payload,
            'selected_role': role,
            'selected_date': selected_date,
            'current_production_date': production_work_date(),
        },
    )


def _json_payload(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValidationError('Переданы некорректные данные.')


def _validation_response(error):
    code = getattr(error, 'code', '') or ''
    status = 409 if code in {'stale_version', 'stale_baseline', 'plan_work_date_closed'} else 400
    messages = getattr(error, 'messages', None) or [str(error)]
    return JsonResponse({'ok': False, 'error': messages[0], 'code': code}, status=status)


@require_POST
def deputy_mining_manager_slot_view(request):
    access = deputy_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Недостаточно прав.'}, status=403)
    try:
        payload = _json_payload(request)
        plan = get_object_or_404(CrewPlan, id=payload.get('plan_id'))
        equipment = get_object_or_404(Equipment, id=payload.get('equipment_id'))
        employee_id = payload.get('employee_id')
        employee = get_object_or_404(Employee, id=employee_id) if employee_id else None
        plan = update_crew_draft_slot(
            plan=plan,
            equipment=equipment,
            shift_type=payload.get('shift_type'),
            employee=employee,
            expected_version=int(payload.get('expected_version')),
            actor=access.employee,
        )
    except (TypeError, ValueError, ValidationError) as error:
        if isinstance(error, ValidationError):
            return _validation_response(error)
        return JsonResponse({'ok': False, 'error': 'Обновите страницу и повторите действие.'}, status=400)
    return JsonResponse({'ok': True, 'payload': build_crew_plan_payload(plan, request=request)})


@require_POST
def deputy_mining_manager_publish_view(request):
    access = deputy_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Недостаточно прав.'}, status=403)
    try:
        payload = _json_payload(request)
        plan = get_object_or_404(CrewPlan, id=payload.get('plan_id'))
        published_plan = publish_crew_plan(
            plan=plan,
            expected_version=int(payload.get('expected_version')),
            actor=access.employee,
        )
        next_draft, _ = get_or_create_crew_draft(
            work_date=published_plan.work_date,
            role=published_plan.role,
            actor=access.employee,
        )
    except (TypeError, ValueError, ValidationError) as error:
        if isinstance(error, ValidationError):
            return _validation_response(error)
        return JsonResponse({'ok': False, 'error': 'Обновите страницу и повторите действие.'}, status=400)
    return JsonResponse({
        'ok': True,
        'published': True,
        'payload': build_crew_plan_payload(next_draft, request=request),
    })


def deputy_mining_manager_reports_view(request):
    access = deputy_access_from_request(request)
    if not access:
        return redirect('role_home')
    publications = list(
        CrewPlan.objects
        .filter(status__in=[CrewPlanStatus.PUBLISHED, CrewPlanStatus.SUPERSEDED])
        .select_related('role', 'published_by')
        .annotate(
            slot_count=Count('slots'),
            assigned_count=Count('slots', filter=Q(slots__employee__isnull=False)),
        )
        .order_by('-work_date', '-revision')[:30]
    )
    for publication in publications:
        publication.work_date_label = publication.work_date.strftime('%d.%m.%Y')
        publication.published_at_label = (
            timezone.localtime(publication.published_at).strftime('%d.%m.%Y %H:%M')
            if publication.published_at
            else '—'
        )
        publication.published_by_label = (
            publication.published_by.full_name
            if publication.published_by
            else 'Система'
        )
        publication.status_label = publication.get_status_display()
        publication.url = (
            f'{reverse("deputy_mining_manager_placement")}?' +
            urlencode({
                'role': publication.role.code,
                'date': publication.work_date.isoformat(),
                'plan': publication.id,
            })
        )
    return render(
        request,
        'reports/deputy_mining_manager_reports.html',
        {'access': access, 'publications': publications},
    )
