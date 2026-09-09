from collections import defaultdict
from datetime import datetime, timedelta

from django.utils import timezone

from assignments.models import CrewPlan, CrewPlanSlot, CrewPlanStatus, WorkShiftType

from .models import EmployeeAccess, Role


REGISTRATION_PERIODS = (14, 30, 90)
REGISTRATION_STATES = {
    'no_access': 'Нет доступа',
    'awaiting_activation': 'Ждут активации',
    'activated': 'Активировали',
    'logged_in': 'Входили в приложение',
}


def _percent(value, total):
    if not total:
        return 0
    return round((value / total) * 100)


def _parse_period(raw_value):
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 30
    return value if value in REGISTRATION_PERIODS else 30


def _local_date(value):
    if not value:
        return None
    if timezone.is_aware(value):
        return timezone.localtime(value).date()
    return value.date()


def _access_status_for_employee(employee_id, role_ids, accesses_by_employee):
    matching = [
        item
        for item in accesses_by_employee.get(employee_id, ())
        if item.role_id in role_ids
        and item.is_active
        and item.status != EmployeeAccess.Status.DEACTIVATED
    ]
    activated = [item for item in matching if item.status == EmployeeAccess.Status.ACTIVATED]
    activated_at = min(
        (item.activated_at for item in activated if item.activated_at),
        default=None,
    )
    last_login_at = max(
        (item.last_login_at for item in activated if item.last_login_at),
        default=None,
    )

    if last_login_at:
        return {
            'code': 'logged_in',
            'label': 'Пользуется приложением',
            'tone': 'ok',
            'has_access': True,
            'is_activated': True,
            'activated_at': activated_at,
            'last_login_at': last_login_at,
        }
    if activated:
        return {
            'code': 'activated',
            'label': 'Активирован, входов нет',
            'tone': 'info',
            'has_access': True,
            'is_activated': True,
            'activated_at': activated_at,
            'last_login_at': None,
        }
    if any(item.status == EmployeeAccess.Status.BLOCKED for item in matching):
        return {
            'code': 'awaiting_activation',
            'label': 'Доступ заблокирован',
            'tone': 'danger',
            'has_access': True,
            'is_activated': False,
            'activated_at': None,
            'last_login_at': None,
        }
    if matching:
        return {
            'code': 'awaiting_activation',
            'label': 'Ждёт активации',
            'tone': 'warning',
            'has_access': True,
            'is_activated': False,
            'activated_at': None,
            'last_login_at': None,
        }
    return {
        'code': 'no_access',
        'label': 'Доступ не выдан',
        'tone': 'neutral',
        'has_access': False,
        'is_activated': False,
        'activated_at': None,
        'last_login_at': None,
    }


def _build_daily_chart(rows, period_days):
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=period_days - 1)
    activation_dates = [
        _local_date(row['activated_at'])
        for row in rows
        if row['is_activated']
    ]
    undated_activations = sum(1 for value in activation_dates if value is None)
    dated_activations = [value for value in activation_dates if value is not None]
    new_by_date = defaultdict(int)
    for value in dated_activations:
        new_by_date[value] += 1

    cumulative = undated_activations + sum(1 for value in dated_activations if value < start_date)
    daily_values = []
    current_date = start_date
    while current_date <= end_date:
        new_count = new_by_date[current_date]
        cumulative += new_count
        daily_values.append({
            'date': current_date,
            'new_count': new_count,
            'cumulative': cumulative,
        })
        current_date += timedelta(days=1)

    max_daily = max((item['new_count'] for item in daily_values), default=0) or 1
    max_cumulative = max((item['cumulative'] for item in daily_values), default=0) or 1
    label_step = max(1, period_days // 6)
    svg_points = []
    for index, item in enumerate(daily_values):
        x = 16 if len(daily_values) == 1 else 16 + (index / (len(daily_values) - 1)) * 968
        y = 224 - (item['cumulative'] / max_cumulative) * 190
        item['bar_height'] = round((item['new_count'] / max_daily) * 100, 2)
        item['show_label'] = index in {0, len(daily_values) - 1} or index % label_step == 0
        item['svg_x'] = round(x, 2)
        item['svg_y'] = round(y, 2)
        svg_points.append(f"{item['svg_x']},{item['svg_y']}")

    return {
        'days': daily_values,
        'polyline': ' '.join(svg_points),
        'start_date': start_date,
        'end_date': end_date,
        'max_daily': max_daily,
        'max_cumulative': max_cumulative,
    }


def _build_breakdown(rows, key, label_key):
    grouped = {}
    for row in rows:
        for code, label in zip(row[key], row[label_key]):
            item = grouped.setdefault(code, {'code': code, 'label': label, 'total': 0, 'activated': 0})
            item['total'] += 1
            if row['is_activated']:
                item['activated'] += 1
    result = []
    for item in grouped.values():
        item['percent'] = _percent(item['activated'], item['total'])
        result.append(item)
    return sorted(result, key=lambda item: item['label'])


def build_registration_dashboard(params):
    published_plans = CrewPlan.objects.filter(status=CrewPlanStatus.PUBLISHED)
    available_dates = list(
        published_plans.order_by('-work_date')
        .values_list('work_date', flat=True)
        .distinct()
    )

    requested_date = params.get('date', '')
    selected_date = None
    if requested_date:
        try:
            selected_date = datetime.strptime(requested_date, '%Y-%m-%d').date()
        except ValueError:
            selected_date = None
    if selected_date not in available_dates:
        selected_date = available_dates[0] if available_dates else timezone.localdate()

    role_options = list(
        Role.objects.filter(
            crew_plans__status=CrewPlanStatus.PUBLISHED,
            crew_plans__work_date=selected_date,
        )
        .distinct()
        .order_by('name')
    )
    valid_role_codes = {role.code for role in role_options}
    selected_role = params.get('role', '')
    if selected_role not in valid_role_codes:
        selected_role = ''

    valid_shift_codes = {value for value, _label in WorkShiftType.choices}
    selected_shift = params.get('shift', '')
    if selected_shift not in valid_shift_codes:
        selected_shift = ''

    selected_state = params.get('state', '')
    if selected_state not in REGISTRATION_STATES:
        selected_state = ''
    period_days = _parse_period(params.get('period'))

    slots = (
        CrewPlanSlot.objects.filter(
            plan__status=CrewPlanStatus.PUBLISHED,
            plan__work_date=selected_date,
            employee__isnull=False,
        )
        .select_related('employee', 'plan__role', 'equipment')
        .order_by('employee__full_name', 'plan__role__name', 'shift_type')
    )
    if selected_role:
        slots = slots.filter(plan__role__code=selected_role)
    if selected_shift:
        slots = slots.filter(shift_type=selected_shift)

    cohort = {}
    for slot in slots:
        item = cohort.setdefault(
            slot.employee_id,
            {
                'employee': slot.employee,
                'role_ids': set(),
                'role_codes': [],
                'role_labels': [],
                'shift_codes': [],
                'shift_labels': [],
                'equipment_labels': [],
            },
        )
        item['role_ids'].add(slot.plan.role_id)
        if slot.plan.role.code not in item['role_codes']:
            item['role_codes'].append(slot.plan.role.code)
            item['role_labels'].append(slot.plan.role.name)
        if slot.shift_type not in item['shift_codes']:
            item['shift_codes'].append(slot.shift_type)
            item['shift_labels'].append(slot.get_shift_type_display())
        equipment_label = slot.equipment.garage_number or str(slot.equipment)
        if equipment_label not in item['equipment_labels']:
            item['equipment_labels'].append(equipment_label)

    employee_ids = set(cohort)
    accesses_by_employee = defaultdict(list)
    relevant_role_ids = {role_id for item in cohort.values() for role_id in item['role_ids']}
    if employee_ids and relevant_role_ids:
        accesses = (
            EmployeeAccess.objects.filter(
                employee_id__in=employee_ids,
                role_id__in=relevant_role_ids,
            )
            .select_related('role')
            .order_by('employee_id', '-last_login_at', '-activated_at', '-created_at')
        )
        for access in accesses:
            accesses_by_employee[access.employee_id].append(access)

    rows = []
    for employee_id, item in cohort.items():
        status = _access_status_for_employee(employee_id, item['role_ids'], accesses_by_employee)
        employee = item['employee']
        rows.append({
            'employee': employee,
            'role_codes': item['role_codes'],
            'role_labels': item['role_labels'],
            'shift_codes': item['shift_codes'],
            'shift_labels': item['shift_labels'],
            'equipment_labels': item['equipment_labels'],
            **status,
        })

    status_order = {'no_access': 0, 'awaiting_activation': 1, 'activated': 2, 'logged_in': 3}
    rows.sort(key=lambda row: (status_order[row['code']], row['employee'].full_name.casefold()))

    total = len(rows)
    access_issued = sum(1 for row in rows if row['has_access'])
    activated = sum(1 for row in rows if row['is_activated'])
    logged_in = sum(1 for row in rows if row['last_login_at'])
    awaiting_activation = access_issued - activated
    no_access = total - access_issued

    chart = _build_daily_chart(rows, period_days)
    role_breakdown = _build_breakdown(rows, 'role_codes', 'role_labels')
    shift_breakdown = _build_breakdown(rows, 'shift_codes', 'shift_labels')
    if selected_state == 'activated':
        visible_rows = [row for row in rows if row['is_activated']]
    else:
        visible_rows = [row for row in rows if not selected_state or row['code'] == selected_state]

    activated_percent = _percent(activated, total)
    awaiting_percent = _percent(awaiting_activation, total)
    return {
        'available_dates': available_dates,
        'selected_date': selected_date,
        'role_options': role_options,
        'selected_role': selected_role,
        'shift_options': WorkShiftType.choices,
        'selected_shift': selected_shift,
        'state_options': REGISTRATION_STATES.items(),
        'selected_state': selected_state,
        'period_options': REGISTRATION_PERIODS,
        'period_days': period_days,
        'total': total,
        'access_issued': access_issued,
        'activated': activated,
        'logged_in': logged_in,
        'awaiting_activation': awaiting_activation,
        'no_access': no_access,
        'coverage_percent': activated_percent,
        'login_percent': _percent(logged_in, total),
        'issued_percent': _percent(access_issued, total),
        'donut_activated_stop': activated_percent,
        'donut_pending_stop': min(100, activated_percent + awaiting_percent),
        'chart': chart,
        'role_breakdown': role_breakdown,
        'shift_breakdown': shift_breakdown,
        'rows': visible_rows,
        'visible_total': len(visible_rows),
        'has_published_plans': bool(available_dates),
    }
