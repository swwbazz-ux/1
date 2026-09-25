"""Чистые представления данных комплекса для Диспетчерского пульта."""

import re
from collections import defaultdict
from decimal import Decimal


def dispatcher_complex_truck_rows(card):
    return list(card.get('truck_rows') or [])


def dispatcher_tons_from_label(value):
    if not value:
        return Decimal('0')
    digits = ''.join(char for char in str(value) if char.isdigit())
    return Decimal(digits or '0')


def dispatcher_employee_for_equipment(
    equipment_id,
    open_shifts,
    work_assignments,
):
    open_shift = open_shifts.get(equipment_id)
    if open_shift:
        presence = getattr(open_shift, 'application_presence', None) or {}
        presence_label = presence.get('status_label') or 'Нет связи'
        return open_shift.employee, f'В смене · {presence_label}'
    work_assignment = work_assignments.get(equipment_id)
    if work_assignment:
        shift_label = work_assignment.get_shift_type_display().lower()
        return work_assignment.employee, f'Назначен на {shift_label}'
    return None, 'Сотрудник не назначен'


def dispatcher_equipment_presence_fields(equipment_id, open_shifts):
    shift = open_shifts.get(equipment_id)
    if not shift:
        return {
            'has_current_shift': False,
            'presence_status': '',
            'presence_label': '',
        }
    presence = getattr(shift, 'application_presence', None) or {}
    return {
        'has_current_shift': True,
        'presence_status': presence.get('status_code') or 'not_registered',
        'presence_label': presence.get('status_label') or 'Не подключался',
    }


def dispatcher_equipment_card_requested(requested_card_ids, card_id):
    return (
        requested_card_ids is None
        or str(card_id) in requested_card_ids
    )


def dispatcher_downtime_reason_label(downtime):
    reason = getattr(downtime, 'reason', None) if downtime else None
    if not reason:
        return ''
    return reason.button_label or reason.name or str(reason)


def dispatcher_shift_details(shift, *, format_datetime):
    if not shift:
        return []
    presence = getattr(shift, 'application_presence', None) or {}
    details = [
        {'label': 'Смена', 'value': shift.get_shift_type_display()},
        {
            'label': 'Смена открыта',
            'value': format_datetime(shift.opened_at),
        },
        {
            'label': 'Связь',
            'value': presence.get('status_label') or 'Не подключался',
        },
    ]
    if presence.get('last_seen_at'):
        details.append({
            'label': 'Последняя связь',
            'value': format_datetime(presence['last_seen_at']),
        })
    client_labels = [
        badge.get('label')
        for badge in presence.get('client_badges') or []
        if badge.get('label')
    ]
    if client_labels:
        details.append({
            'label': 'Приложение',
            'value': ', '.join(dict.fromkeys(client_labels)),
        })
    return details


def dispatcher_plan_details(plan):
    if not plan:
        return []
    rows = [
        {'label': 'Статус плана', 'value': plan.get('status_label')},
        {'label': 'Факт / план', 'value': plan.get('fact_plan_label')},
    ]
    if plan.get('has_plan'):
        rows.insert(
            1,
            {
                'label': 'Выполнение плана',
                'value': plan.get('percent_label'),
            },
        )
    if plan.get('group_name'):
        rows.append(
            {'label': 'Группа плана', 'value': plan.get('group_name')}
        )
    return rows


def dispatcher_status_label(status, label=''):
    return label or ''


def dispatcher_garage_number_int(equipment):
    match = re.search(
        r'\d+',
        str(getattr(equipment, 'garage_number', '') or ''),
    )
    return int(match.group(0)) if match else 9999


def dispatcher_complex_label(equipment):
    """Вернуть человекочитаемое и однозначное имя комплекса."""
    raw = str(
        getattr(equipment, 'garage_number', '') or ''
    ).strip().upper()
    ordinary = re.fullmatch(r'(?:ЭКГ|ЭКС|Э)?[\s\-№]*(\d+)', raw)
    if ordinary:
        return f'K-{int(ordinary.group(1))}'
    slug = re.sub(r'[^0-9A-ZА-ЯЁ]+', '-', raw).strip('-')
    return f'K-{slug}' if slug else f'K-ID-{equipment.id}'


def dispatcher_complex_number_int(card):
    match = re.search(r'\d+', str(card.get('id', '') or ''))
    return int(match.group(0)) if match else 9999


def dispatcher_complex_face_label(card):
    horizon = card.get('current_horizon') or ''
    block = card.get('current_block') or ''
    label = ' / '.join(
        part for part in [horizon, block] if part and '-' not in part
    )
    return label or 'Забой не указан'


def dispatcher_complex_location_parts(card):
    return (
        card.get('current_horizon') or 'Гор. -',
        card.get('current_block') or 'Блок -',
    )


def dispatcher_complex_shift_report(card, *, format_number, chart_percent):
    status_key = card.get('status_key') or 'green'
    assigned = int(card.get('assigned') or 0)
    need = int(card.get('need') or 0)
    balance = assigned - need
    percent = int(card.get('percent') or 0)
    truck_rows = dispatcher_complex_truck_rows(card)
    current_truck_rows = [
        row for row in truck_rows if row['state_key'] == 'current'
    ]
    removed_truck_rows = [
        row for row in truck_rows if row['state_key'] == 'removed'
    ]
    plan_context = card.get('plan') or {}
    plan_unit = plan_context.get('unit') or 'т'
    plan_value = (
        f'{plan_context.get("value_display")} {plan_unit}'.strip()
        if plan_context.get('value_display')
        else f'{card.get("plan_tons", "0")} т'
    )
    fact_value = (
        plan_context.get('fact_plan_label')
        or f'{card.get("fact_tons", "0")} т'
    )
    forecast_value = f'{card.get("forecast_tons", "0")} т'
    if status_key == 'red':
        problem = 'работа заблокирована'
        action = 'ремонт / перераспределить самосвалы'
    elif status_key == 'orange':
        problem = 'техническое ограничение'
        action = 'контроль ремонта или ТО'
    elif status_key == 'yellow':
        problem = 'ожидает действия'
        action = 'добавить транспорт'
    elif status_key == 'blue':
        problem = 'назначен'
        action = 'дождаться активной операции'
    else:
        problem = 'без отклонений'
        action = 'контроль нормы'

    def grouped_chart_rows(source_rows, field, meta_field):
        totals = defaultdict(Decimal)
        meta = defaultdict(set)
        for row in source_rows:
            label = row.get(field) or 'не указано'
            totals[label] += dispatcher_tons_from_label(row.get('value'))
            if row.get(meta_field):
                meta[label].add(row.get(meta_field))
        sorted_rows = sorted(
            totals.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        max_value = max(
            (value for _, value in sorted_rows),
            default=Decimal('0'),
        )
        accents = ('green', 'blue', 'yellow', 'red')
        return [
            {
                'label': label,
                'meta': ', '.join(sorted(meta[label])[:3]),
                'value': f'{format_number(value)} т',
                'percent': chart_percent(value, max_value) if max_value else 0,
                'accent': accents[index % len(accents)],
            }
            for index, (label, value) in enumerate(sorted_rows)
        ]

    material_rows = grouped_chart_rows(truck_rows, 'rock', 'target')
    unload_rows = grouped_chart_rows(truck_rows, 'target', 'rock')
    valid_statuses = {'green', 'yellow', 'blue', 'orange', 'red', 'gray'}
    return {
        'metrics': [
            {'label': 'План', 'value': plan_value},
            {'label': 'Факт', 'value': fact_value},
            {'label': 'Самосвалы', 'value': f'{assigned} / {need}'},
            {'label': 'Работали', 'value': str(len(truck_rows))},
            {'label': 'Выведены', 'value': str(len(removed_truck_rows))},
        ],
        'charts': [
            {
                'type': 'bar',
                'title': 'План / факт',
                'rows': [
                    {
                        'label': 'Факт / план',
                        'meta': (
                            plan_context.get('group_name') or 'snapshot смены'
                        ),
                        'value': fact_value,
                        'percent': max(4, percent),
                        'accent': (
                            status_key if status_key in valid_statuses else 'green'
                        ),
                    },
                    {
                        'label': 'Прогноз',
                        'meta': 'ожидаемый итог',
                        'value': forecast_value,
                        'percent': min(100, max(4, percent + 8)),
                        'accent': 'blue',
                    },
                    {
                        'label': 'План',
                        'meta': 'сменное задание',
                        'value': plan_value,
                        'percent': 100,
                        'accent': 'green',
                    },
                ],
            },
            {
                'type': 'donut-list',
                'title': 'Порода',
                'rows': material_rows,
            },
            {
                'type': 'donut-list',
                'title': 'Разгрузка',
                'rows': unload_rows,
            },
            {
                'type': 'truck-ledger',
                'title': 'Самосвалы',
                'rows': truck_rows,
            },
            {
                'type': 'bar',
                'title': 'Баланс',
                'rows': [
                    {
                        'label': 'Назначено',
                        'meta': 'самосвалы в комплексе',
                        'value': str(assigned),
                        'percent': chart_percent(
                            Decimal(assigned),
                            Decimal(max(need, assigned, 1)),
                        ),
                        'accent': 'green' if assigned >= need else 'yellow',
                    },
                    {
                        'label': 'Нужно',
                        'meta': 'расчетная потребность',
                        'value': str(need),
                        'percent': 100,
                        'accent': 'blue',
                    },
                    {
                        'label': 'Баланс',
                        'meta': action,
                        'value': f'+{balance}' if balance > 0 else str(balance),
                        'percent': chart_percent(
                            Decimal(abs(balance)),
                            Decimal(max(need, 1)),
                        ),
                        'accent': 'red' if balance < 0 else 'green',
                    },
                ],
            },
        ],
        'tables': [],
        'problem': problem,
        'truck_rows': truck_rows,
        'current_trucks': [row['truck'] for row in current_truck_rows],
        'removed_trucks': [row['truck'] for row in removed_truck_rows],
    }
