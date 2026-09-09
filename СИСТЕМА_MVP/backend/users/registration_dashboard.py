from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from urllib.parse import urlencode

from django.db.models import Max, Q
from django.utils import timezone

from assignments.models import CrewPlan, CrewPlanSlot, CrewPlanStatus, WorkShiftType

from .models import Employee, EmployeeAccess, Role


REGISTRATION_PERIODS = (7, 14, 30, 90)
REGISTRATION_STATES = {
    'needs_attention': 'Требуют внимания',
    'prepared': 'Доступ подготовлен',
    'ready': 'Готовы к работе',
    'awaiting_activation': 'Ждут активации',
    'missing_access': 'Нет рабочего доступа',
    'blocked': 'Доступ заблокирован',
}
REGISTRATION_INTERNAL_STATES = {
    'deactivated': 'Доступ отключён',
    'inactive_employee': 'Сотрудник неактивен',
}
REGISTRATION_STATE_ALIASES = {
    'activated': 'ready',
    'logged_in': 'ready',
    'no_access': 'missing_access',
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


def _parse_date(raw_value):
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _local_date(value):
    if not value:
        return None
    if timezone.is_aware(value):
        return timezone.localtime(value).date()
    return value.date()


def _access_recency(access):
    moments = [
        value
        for value in (
            access.activated_at,
            access.primary_code_issued_at,
            access.blocked_at,
            access.deactivated_at,
            access.created_at,
        )
        if value
    ]
    value = max(moments) if moments else None
    return (value.timestamp() if value else float('-inf'), access.pk or 0)


def _role_access_status(accesses):
    """Return the current readiness of one exact employee/role pair."""
    accesses = list(accesses)
    access = max(accesses, key=_access_recency) if accesses else None
    if (
        access
        and access.is_active
        and access.status == EmployeeAccess.Status.ACTIVATED
    ):
        return {
            'code': 'ready',
            'reason_code': 'ready',
            'label': 'Готов к работе',
            'tone': 'ok',
            'is_prepared': True,
            'is_ready': True,
            'access_id': access.pk,
            'primary_code_issued_at': access.primary_code_issued_at,
            'activated_at': access.activated_at,
        }

    if (
        access
        and access.is_active
        and access.status == EmployeeAccess.Status.NOT_ACTIVATED
    ):
        pin_is_prepared = bool((access.access_code or '').strip()) or any((
            access.primary_code_issued_at,
            access.activated_at,
        ))
        if pin_is_prepared:
            return {
                'code': 'awaiting_activation',
                'reason_code': 'awaiting_activation',
                'label': 'Ждёт активации',
                'tone': 'warning',
                'is_prepared': True,
                'is_ready': False,
                'access_id': access.pk,
                'primary_code_issued_at': access.primary_code_issued_at,
                'activated_at': None,
            }
        return {
            'code': 'missing_access',
            'reason_code': 'missing_pin',
            'label': 'PIN не сформирован',
            'tone': 'neutral',
            'is_prepared': False,
            'is_ready': False,
            'access_id': access.pk,
            'primary_code_issued_at': None,
            'activated_at': None,
        }

    if access and access.status == EmployeeAccess.Status.BLOCKED:
        return {
            'code': 'blocked',
            'reason_code': 'blocked',
            'label': 'Доступ заблокирован',
            'tone': 'danger',
            'is_prepared': False,
            'is_ready': False,
            'access_id': access.pk,
            'primary_code_issued_at': access.primary_code_issued_at,
            'activated_at': None,
        }

    if access:
        return {
            'code': 'deactivated',
            'reason_code': 'deactivated',
            'label': 'Доступ отключён',
            'tone': 'danger',
            'is_prepared': False,
            'is_ready': False,
            'access_id': access.pk,
            'primary_code_issued_at': access.primary_code_issued_at,
            'activated_at': None,
        }

    return {
        'code': 'missing_access',
        'reason_code': 'missing_access',
        'label': 'Нет доступа',
        'tone': 'neutral',
        'is_prepared': False,
        'is_ready': False,
        'access_id': None,
        'primary_code_issued_at': None,
        'activated_at': None,
    }


def _build_issue_summary(role_statuses):
    issue_labels = {
        'inactive_employee': 'Сотрудник неактивен',
        'blocked': 'Заблокирован',
        'deactivated': 'Отключён',
        'missing_access': 'Нет доступа',
        'missing_pin': 'PIN не сформирован',
        'awaiting_activation': 'Ожидает активации',
    }
    grouped = defaultdict(list)
    for item in role_statuses:
        if item['reason_code'] in issue_labels:
            grouped[item['reason_code']].append(item['role_label'])
    return '; '.join(
        f"{issue_labels[code]}: {', '.join(labels)}"
        for code, labels in grouped.items()
    )


def _person_status(role_statuses):
    is_ready = bool(role_statuses) and all(item['is_ready'] for item in role_statuses)
    is_prepared = bool(role_statuses) and all(item['is_prepared'] for item in role_statuses)
    ready_at = None
    if is_ready:
        activation_moments = [item['activated_at'] for item in role_statuses]
        if all(activation_moments):
            ready_at = max(activation_moments)

    if is_ready:
        code = 'ready'
        label = 'Готов к работе'
        tone = 'ok'
        issue_summary = 'Все требуемые роли активированы'
    elif any(item['code'] == 'blocked' for item in role_statuses):
        code = 'blocked'
        label = 'Доступ заблокирован'
        tone = 'danger'
        issue_summary = _build_issue_summary(role_statuses)
    elif any(item['code'] == 'deactivated' for item in role_statuses):
        code = 'deactivated'
        label = 'Доступ отключён'
        tone = 'danger'
        issue_summary = _build_issue_summary(role_statuses)
    elif any(item['code'] == 'missing_access' for item in role_statuses):
        code = 'missing_access'
        label = 'Нет готового доступа'
        tone = 'neutral'
        issue_summary = _build_issue_summary(role_statuses)
    else:
        code = 'awaiting_activation'
        label = (
            'Часть ролей активирована'
            if any(item['is_ready'] for item in role_statuses)
            else 'Ждёт активации'
        )
        tone = 'warning'
        issue_summary = _build_issue_summary(role_statuses)

    waiting_moments = [
        item['primary_code_issued_at']
        for item in role_statuses
        if item['code'] == 'awaiting_activation'
        and item['primary_code_issued_at']
    ]
    waiting_since = min(waiting_moments) if waiting_moments else None
    waiting_hours = None
    waiting_label = None
    if waiting_since:
        waiting_hours = max(
            0,
            int((timezone.now() - waiting_since).total_seconds() // 3600),
        )
        waiting_label = (
            f'Ждёт {waiting_hours} ч'
            if waiting_hours < 24
            else f'Ждёт {waiting_hours // 24} д'
        )

    return {
        'code': code,
        'label': label,
        'tone': tone,
        'is_prepared': is_prepared,
        'is_ready': is_ready,
        'requires_action': not is_ready,
        'issue_summary': issue_summary,
        'ready_at': ready_at,
        'ready_on': _local_date(ready_at),
        'waiting_since': waiting_since,
        'waiting_hours': waiting_hours,
        'waiting_label': waiting_label,
    }


def _build_daily_chart(rows, period_days):
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=period_days - 1)
    ready_dates = [row['ready_on'] for row in rows if row['is_ready']]
    undated_ready = sum(1 for value in ready_dates if value is None)
    future_ready = sum(1 for value in ready_dates if value and value > end_date)
    dated_ready = [value for value in ready_dates if value and value <= end_date]
    new_by_date = defaultdict(int)
    for value in dated_ready:
        new_by_date[value] += 1

    cumulative = undated_ready + sum(1 for value in dated_ready if value < start_date)
    daily_values = []
    current_date = start_date
    while current_date <= end_date:
        new_count = new_by_date[current_date]
        cumulative += new_count
        daily_values.append({
            'date': current_date,
            'date_iso': current_date.isoformat(),
            'label': current_date.strftime('%d.%m'),
            'long_label': current_date.strftime('%d.%m.%Y'),
            'new_count': new_count,
            'cumulative': cumulative,
        })
        current_date += timedelta(days=1)

    max_daily = max((item['new_count'] for item in daily_values), default=0) or 1
    # The line uses the whole selected cohort as its scale.  Normalising by
    # the highest observed value would make 12 of 20 look like 100% coverage.
    max_cumulative = len(rows)
    cumulative_denominator = max_cumulative or 1
    label_step = max(1, period_days // 6)
    svg_points = []
    for index, item in enumerate(daily_values):
        x = ((index + 0.5) / len(daily_values)) * 1000
        y = 224 - (item['cumulative'] / cumulative_denominator) * 190
        item['bar_height'] = round((item['new_count'] / max_daily) * 100, 2)
        item['cumulative_percent'] = round(
            (item['cumulative'] / cumulative_denominator) * 100,
            2,
        )
        item['show_label'] = index in {0, len(daily_values) - 1} or index % label_step == 0
        item['show_value'] = item['new_count'] > 0
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
        'scale_total': max_cumulative,
        'undated_ready': undated_ready,
        'future_ready': future_ready,
    }


def _finalize_breakdown(grouped):
    result = []
    for item in grouped.values():
        item['not_ready'] = item['total'] - item['ready']
        item['percent'] = _percent(item['ready'], item['total'])
        item['ready_percent'] = item['percent']
        item['prepared_percent'] = _percent(item['prepared'], item['total'])
        item['activated'] = item['ready']
        result.append(item)
    return sorted(
        result,
        key=lambda item: (-item['not_ready'], item['percent'], item['label'].casefold()),
    )


def _build_breakdowns(rows, shift_labels):
    roles = {}
    shifts = {}
    for row in rows:
        role_status_by_id = {
            item['role_id']: item
            for item in row['role_statuses']
        }
        for status in row['role_statuses']:
            item = roles.setdefault(
                status['role_code'],
                {
                    'code': status['role_code'],
                    'label': status['role_label'],
                    'total': 0,
                    'prepared': 0,
                    'ready': 0,
                },
            )
            item['total'] += 1
            item['prepared'] += int(status['is_prepared'])
            item['ready'] += int(status['is_ready'])

        for shift_code, role_ids in row['_shift_role_ids'].items():
            statuses = [role_status_by_id[role_id] for role_id in role_ids]
            item = shifts.setdefault(
                shift_code,
                {
                    'code': shift_code,
                    'label': shift_labels[shift_code],
                    'total': 0,
                    'prepared': 0,
                    'ready': 0,
                },
            )
            item['total'] += 1
            item['prepared'] += int(all(status['is_prepared'] for status in statuses))
            item['ready'] += int(all(status['is_ready'] for status in statuses))

    return _finalize_breakdown(roles), _finalize_breakdown(shifts)


def _query_url(base_query, **updates):
    query = dict(base_query)
    for key, value in updates.items():
        if value in (None, ''):
            query.pop(key, None)
        else:
            query[key] = value
    encoded = urlencode(query)
    return f'?{encoded}' if encoded else ''


def _state_matches(row, selected_state):
    if not selected_state:
        return True
    if selected_state == 'needs_attention':
        return row['requires_action']
    if selected_state == 'prepared':
        return row['is_prepared']
    if selected_state == 'ready':
        return row['is_ready']
    if selected_state == 'awaiting_activation':
        return row['code'] == 'awaiting_activation'
    if selected_state == 'missing_access':
        return row['code'] == 'missing_access'
    if selected_state == 'blocked':
        return row['code'] == 'blocked'
    if selected_state == 'deactivated':
        return row['code'] == 'deactivated'
    if selected_state == 'inactive_employee':
        return row['code'] == 'inactive_employee'
    return True


def build_registration_dashboard(params):
    published_plans = CrewPlan.objects.filter(status=CrewPlanStatus.PUBLISHED)
    available_dates = list(
        published_plans.order_by('-work_date')
        .values_list('work_date', flat=True)
        .distinct()
    )

    selected_date = _parse_date(params.get('date', ''))
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

    selected_plan_scope = published_plans.filter(work_date=selected_date)
    if selected_role:
        selected_plan_scope = selected_plan_scope.filter(role__code=selected_role)
    plan_published_at = selected_plan_scope.aggregate(
        latest=Max('published_at'),
    )['latest']

    shift_labels = dict(WorkShiftType.choices)
    selected_shift = params.get('shift', '')
    if selected_shift not in shift_labels:
        selected_shift = ''

    raw_state = params.get('state', '')
    selected_state = REGISTRATION_STATE_ALIASES.get(raw_state, raw_state)
    valid_states = {*REGISTRATION_STATES, *REGISTRATION_INTERNAL_STATES}
    if selected_state not in valid_states:
        selected_state = ''
    period_days = _parse_period(params.get('period'))
    selected_ready_on = _parse_date(params.get('ready_on', ''))

    slots = (
        CrewPlanSlot.objects.filter(
            Q(employee__isnull=False) | Q(secondary_employee__isnull=False),
            plan__status=CrewPlanStatus.PUBLISHED,
            plan__work_date=selected_date,
        )
        .select_related(
            'employee',
            'secondary_employee',
            'plan__role',
            'equipment',
        )
        .order_by('plan__role__name', 'shift_type', 'equipment__garage_number')
    )
    if selected_role:
        slots = slots.filter(plan__role__code=selected_role)
    if selected_shift:
        slots = slots.filter(shift_type=selected_shift)

    cohort = {}
    for slot in slots:
        equipment_label = slot.equipment.garage_number or str(slot.equipment)
        for employee in (slot.employee, slot.secondary_employee):
            if employee is None:
                continue
            item = cohort.setdefault(
                employee.pk,
                {
                    'employee': employee,
                    'roles': {},
                    'shift_role_ids': defaultdict(set),
                    'equipment_labels': set(),
                },
            )
            item['roles'].setdefault(
                slot.plan.role_id,
                {
                    'role_id': slot.plan.role_id,
                    'role_code': slot.plan.role.code,
                    'role_label': slot.plan.role.name,
                },
            )
            item['shift_role_ids'][slot.shift_type].add(slot.plan.role_id)
            item['equipment_labels'].add(equipment_label)

    employee_ids = set(cohort)
    relevant_role_ids = {
        role_id
        for item in cohort.values()
        for role_id in item['roles']
    }
    accesses_by_pair = defaultdict(list)
    if employee_ids and relevant_role_ids:
        accesses = (
            EmployeeAccess.objects.filter(
                employee_id__in=employee_ids,
                role_id__in=relevant_role_ids,
            )
            .select_related('role')
            .order_by('employee_id', 'role_id', '-activated_at', '-created_at')
        )
        for access in accesses:
            accesses_by_pair[(access.employee_id, access.role_id)].append(access)

    rows = []
    activation_delays = []
    for employee_id, item in cohort.items():
        role_statuses = []
        for role in sorted(
            item['roles'].values(),
            key=lambda value: value['role_label'].casefold(),
        ):
            status = _role_access_status(
                accesses_by_pair.get((employee_id, role['role_id']), ()),
            )
            role_status = {**role, **status}
            if (
                not item['employee'].is_active
                or item['employee'].status in {
                    Employee.Status.DEACTIVATED,
                    Employee.Status.ARCHIVED,
                    Employee.Status.DISMISSED,
                    Employee.Status.DELETED,
                }
            ):
                role_status.update({
                    'code': 'missing_access',
                    'reason_code': 'inactive_employee',
                    'label': 'Сотрудник неактивен',
                    'tone': 'danger',
                    'is_prepared': False,
                    'is_ready': False,
                    'activated_at': None,
                })
            role_statuses.append(role_status)
            issued_at = role_status['primary_code_issued_at']
            activated_at = role_status['activated_at']
            if issued_at and activated_at and activated_at >= issued_at:
                activation_delays.append(
                    (activated_at - issued_at).total_seconds() / 3600,
                )

        person_status = _person_status(role_statuses)
        if any(
            status['reason_code'] == 'inactive_employee'
            for status in role_statuses
        ):
            person_status.update({
                'code': 'inactive_employee',
                'label': 'Неактивен в расстановке',
                'tone': 'danger',
                'is_prepared': False,
                'is_ready': False,
                'requires_action': True,
                'issue_summary': _build_issue_summary(role_statuses),
                'ready_at': None,
                'ready_on': None,
            })
        shift_codes = sorted(
            item['shift_role_ids'],
            key=lambda code: list(shift_labels).index(code),
        )
        row = {
            'employee': item['employee'],
            'role_codes': [status['role_code'] for status in role_statuses],
            'role_labels': [status['role_label'] for status in role_statuses],
            'shift_codes': shift_codes,
            'shift_labels': [shift_labels[code] for code in shift_codes],
            'equipment_labels': sorted(item['equipment_labels']),
            'role_statuses': role_statuses,
            '_shift_role_ids': dict(item['shift_role_ids']),
            **person_status,
        }
        row['has_access'] = row['is_prepared']
        row['is_activated'] = row['is_ready']
        row['activated_at'] = row['ready_at']
        row['last_login_at'] = None
        rows.append(row)

    status_order = {
        'blocked': 0,
        'inactive_employee': 1,
        'deactivated': 2,
        'missing_access': 3,
        'awaiting_activation': 4,
        'ready': 5,
    }
    rows.sort(key=lambda row: (
        status_order[row['code']],
        -(row['waiting_hours'] if row['waiting_hours'] is not None else -1),
        row['employee'].full_name.casefold(),
    ))

    total = len(rows)
    prepared = sum(1 for row in rows if row['is_prepared'])
    ready = sum(1 for row in rows if row['is_ready'])
    requires_action = total - ready
    blocked = sum(1 for row in rows if row['code'] == 'blocked')
    inactive_employee = sum(
        1 for row in rows if row['code'] == 'inactive_employee'
    )
    deactivated = sum(1 for row in rows if row['code'] == 'deactivated')
    missing_access = sum(1 for row in rows if row['code'] == 'missing_access')
    awaiting_activation = sum(
        1 for row in rows if row['code'] == 'awaiting_activation'
    )

    chart = _build_daily_chart(rows, period_days)
    role_breakdown, shift_breakdown = _build_breakdowns(rows, shift_labels)

    today = timezone.localdate()
    current_period_start = today - timedelta(days=6)
    previous_period_start = today - timedelta(days=13)
    previous_period_end = today - timedelta(days=7)
    new_ready_today = sum(1 for row in rows if row['ready_on'] == today)
    new_ready_7 = sum(
        1
        for row in rows
        if row['ready_on']
        and current_period_start <= row['ready_on'] <= today
    )
    previous_ready_7 = sum(
        1
        for row in rows
        if row['ready_on']
        and previous_period_start <= row['ready_on'] <= previous_period_end
    )
    delta_abs = new_ready_7 - previous_ready_7
    delta_percent = (
        round((delta_abs / previous_ready_7) * 100)
        if previous_ready_7
        else None
    )

    base_query = {
        'date': selected_date.isoformat(),
        'period': period_days,
    }
    if selected_role:
        base_query['role'] = selected_role
    if selected_shift:
        base_query['shift'] = selected_shift
    if selected_state:
        base_query['state'] = selected_state
    if selected_ready_on:
        base_query['ready_on'] = selected_ready_on.isoformat()

    state_counts = {
        'all': total,
        'needs_attention': requires_action,
        'prepared': prepared,
        'ready': ready,
        'awaiting_activation': awaiting_activation,
        'missing_access': missing_access,
        'inactive_employee': inactive_employee,
        'deactivated': deactivated,
        'blocked': blocked,
    }
    state_labels = {
        'all': 'Все сотрудники',
        **REGISTRATION_STATES,
        **REGISTRATION_INTERNAL_STATES,
    }
    state_tabs = []
    for code in (
        'all',
        'needs_attention',
        'prepared',
        'ready',
        'awaiting_activation',
        'missing_access',
        'inactive_employee',
        'deactivated',
        'blocked',
    ):
        state_value = '' if code == 'all' else code
        url = _query_url(base_query, state=state_value, ready_on=None)
        state_tabs.append({
            'code': code,
            'label': state_labels[code],
            'count': state_counts[code],
            'url': url,
            'filter_url': url,
            'is_active': selected_state == state_value,
        })

    period_links = []
    for value in REGISTRATION_PERIODS:
        url = _query_url(base_query, period=value, ready_on=None)
        period_links.append({
            'days': value,
            'label': f'{value} дней',
            'url': url,
            'filter_url': url,
            'is_active': value == period_days,
        })

    for item in role_breakdown:
        item['url'] = _query_url(base_query, role=item['code'], ready_on=None)
        item['filter_url'] = item['url']
    for item in shift_breakdown:
        item['url'] = _query_url(base_query, shift=item['code'], ready_on=None)
        item['filter_url'] = item['url']
    for item in chart['days']:
        item['url'] = _query_url(
            base_query,
            state='ready',
            ready_on=item['date_iso'],
        )
        item['filter_url'] = item['url']
        item['is_selected'] = item['date'] == selected_ready_on

    funnel_steps = [
        {
            'code': 'total',
            'label': 'В расстановке',
            'count': total,
            'percent': 100 if total else 0,
            'loss_count': total - prepared,
            'conversion_percent': _percent(prepared, total),
            'url': _query_url(base_query, state='', ready_on=None),
        },
        {
            'code': 'prepared',
            'label': 'Доступ подготовлен',
            'count': prepared,
            'percent': _percent(prepared, total),
            'loss_count': prepared - ready,
            'conversion_percent': _percent(ready, prepared),
            'url': _query_url(base_query, state='prepared', ready_on=None),
        },
        {
            'code': 'ready',
            'label': 'Готовы к работе',
            'count': ready,
            'percent': _percent(ready, total),
            'loss_count': 0,
            'conversion_percent': 100 if ready else 0,
            'url': _query_url(base_query, state='ready', ready_on=None),
        },
    ]

    attention_items = [
        {
            'code': 'blocked',
            'label': 'Доступ заблокирован',
            'detail': 'Нужна разблокировка требуемой роли.',
            'count': blocked,
            'tone': 'danger',
            'url': _query_url(base_query, state='blocked', ready_on=None),
        },
        {
            'code': 'deactivated',
            'label': 'Доступ отключён',
            'detail': 'Требуемый доступ сейчас не действует.',
            'count': deactivated,
            'tone': 'danger',
            'url': _query_url(base_query, state='deactivated', ready_on=None),
        },
        {
            'code': 'missing_access',
            'label': 'Нет доступа или PIN',
            'detail': 'Для требуемой роли нет рабочего доступа или PIN.',
            'count': missing_access,
            'tone': 'neutral',
            'url': _query_url(base_query, state='missing_access', ready_on=None),
        },
        {
            'code': 'inactive_employee',
            'label': 'Сотрудник неактивен',
            'detail': 'Неактивный сотрудник остался в опубликованной расстановке.',
            'count': inactive_employee,
            'tone': 'danger',
            'url': _query_url(base_query, state='inactive_employee', ready_on=None),
        },
        {
            'code': 'awaiting_activation',
            'label': 'Ждут активации',
            'detail': 'Доступ ещё не активирован.',
            'count': awaiting_activation,
            'tone': 'warning',
            'url': _query_url(base_query, state='awaiting_activation', ready_on=None),
        },
    ]
    for item in attention_items:
        item['filter_url'] = item['url']
    for item in funnel_steps:
        item['filter_url'] = item['url']

    visible_rows = [row for row in rows if _state_matches(row, selected_state)]
    if selected_ready_on:
        visible_rows = [
            row for row in visible_rows if row['ready_on'] == selected_ready_on
        ]

    ready_percent = _percent(ready, total)
    prepared_percent = _percent(prepared, total)
    unavailable = blocked + inactive_employee + deactivated + missing_access
    unavailable_percent = _percent(unavailable, total)
    awaiting_percent = _percent(awaiting_activation, total)
    median_activation_hours = (
        round(median(activation_delays), 1)
        if len(activation_delays) >= 3
        else None
    )

    return {
        'available_dates': available_dates,
        'selected_date': selected_date,
        'plan_published_at': plan_published_at,
        'dashboard_generated_at': timezone.now(),
        'role_options': role_options,
        'selected_role': selected_role,
        'shift_options': WorkShiftType.choices,
        'selected_shift': selected_shift,
        'state_options': (('', 'Все сотрудники'), *REGISTRATION_STATES.items()),
        'selected_state': selected_state,
        'selected_ready_on': selected_ready_on,
        'ready_on_clear_url': _query_url(base_query, ready_on=None),
        'period_options': REGISTRATION_PERIODS,
        'period_days': period_days,
        'period_links': period_links,
        'state_tabs': state_tabs,
        'funnel_steps': funnel_steps,
        'attention_items': attention_items,
        'total': total,
        'prepared': prepared,
        'ready': ready,
        'requires_action': requires_action,
        'prepared_percent': prepared_percent,
        'ready_percent': ready_percent,
        'activation_conversion_percent': _percent(ready, prepared),
        'lost_before_prepared': total - prepared,
        'lost_after_prepared': prepared - ready,
        'blocked': blocked,
        'inactive_employee': inactive_employee,
        'deactivated': deactivated,
        'missing_access': missing_access,
        'awaiting_activation': awaiting_activation,
        'new_ready_today': new_ready_today,
        'new_ready_7': new_ready_7,
        'previous_ready_7': previous_ready_7,
        'delta_abs': delta_abs,
        'delta_percent': delta_percent,
        'median_activation_hours': median_activation_hours,
        'median_activation_sample': len(activation_delays),
        'chart': chart,
        'role_breakdown': role_breakdown,
        'shift_breakdown': shift_breakdown,
        'rows': visible_rows,
        'all_rows': rows,
        'visible_total': len(visible_rows),
        'has_published_plans': bool(available_dates),
        'access_issued': prepared,
        'activated': ready,
        'logged_in': 0,
        'no_access': unavailable,
        'coverage_percent': ready_percent,
        'login_percent': 0,
        'issued_percent': prepared_percent,
        'donut_activated_stop': ready_percent,
        'donut_pending_stop': min(100, ready_percent + awaiting_percent),
        'donut_unavailable_percent': unavailable_percent,
    }
