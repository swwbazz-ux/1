import re


DEFAULT_DOWNTIME_REASON_STATE_CODES = {
    'Ожидание погрузки': 'waiting',
    'Ожидание разгрузки': 'waiting',
    'Ожидание разгрузки ККД': 'waiting',
    'Ожидание разгрузки СКДР': 'waiting',
    'Ожидание фронта работ': 'waiting',
    'Заправка': 'waiting',
    'ТО': 'maintenance',
    'Ремонт': 'repair',
    'Поломка': 'breakdown',
    'БВР': 'waiting',
    'Обед': 'waiting',
    'Чистка кузова': 'waiting',
    'Ожидание самосвалов': 'waiting',
    'Зачистка забоя': 'waiting',
    'Подготовка забоя': 'waiting',
    'Перегон экскаватора': 'waiting',
    'Климатические условия': 'waiting',
    'Прочие': 'waiting',
    'Диагностика': 'maintenance',
    'Текущий ремонт': 'repair',
    'Электрика': 'repair',
    'Гидравлика': 'repair',
    'Двигатель': 'repair',
    'Ходовая часть': 'repair',
    'ТО и обслуживание': 'maintenance',
    'Сварочные работы': 'repair',
    'Система охлаждения': 'repair',
    'Шиномонтажные работы': 'repair',
    'Программное обеспечение': 'repair',
}

DOWNTIME_STATE_COLOR_GROUPS = {
    'waiting': 'yellow',
    'maintenance': 'orange',
    'repair': 'orange',
    'downtime': 'red',
    'breakdown': 'red',
    'conflict': 'red',
}

CRITICAL_REASON_PARTS = ('авар', 'полом', 'критич', 'отказ')
REPAIR_REASON_PARTS = ('ремонт', 'электрик', 'гидравлик', 'двигател', 'ходов', 'свар', 'охлажд', 'шин')
MAINTENANCE_REASON_PARTS = ('диагност', 'обслуж')
WAITING_REASON_PARTS = (
    'ожидан',
    'офр',
    'заправ',
    'бвр',
    'обед',
    'чистк',
    'зачист',
    'подготов',
    'перегон',
    'климат',
    'погод',
    'проч',
)


def normalize_reason_name(value):
    return re.sub(r'\s+', ' ', str(value or '').strip())


def infer_downtime_reason_state_code(name, *, is_critical=False):
    normalized = normalize_reason_name(name)
    if normalized in DEFAULT_DOWNTIME_REASON_STATE_CODES:
        return DEFAULT_DOWNTIME_REASON_STATE_CODES[normalized]

    lower_name = normalized.lower()
    tokens = set(re.findall(r'[a-zа-яё0-9]+', lower_name))
    if is_critical or any(part in lower_name for part in CRITICAL_REASON_PARTS):
        return 'breakdown'
    if 'то' in tokens or any(part in lower_name for part in MAINTENANCE_REASON_PARTS):
        return 'maintenance'
    if any(part in lower_name for part in REPAIR_REASON_PARTS):
        return 'repair'
    if any(part in lower_name for part in WAITING_REASON_PARTS):
        return 'waiting'
    return 'waiting'


def downtime_reason_color_group_for_state_code(state_code):
    return DOWNTIME_STATE_COLOR_GROUPS.get(state_code or '', 'yellow')
