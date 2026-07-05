from django.db import migrations


REASON_STATE_CODES = {
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

CRITICAL_REASONS = {'Поломка'}


def apply_reason_state_semantics(apps, schema_editor):
    DowntimeReason = apps.get_model('downtimes', 'DowntimeReason')
    EquipmentState = apps.get_model('references', 'EquipmentState')
    states = {state.code: state for state in EquipmentState.objects.filter(code__in=set(REASON_STATE_CODES.values()))}
    for reason_name, state_code in REASON_STATE_CODES.items():
        state = states.get(state_code)
        if not state:
            continue
        update_fields = {'equipment_state': state}
        if reason_name in CRITICAL_REASONS:
            update_fields['is_critical'] = True
        DowntimeReason.objects.filter(name=reason_name).update(**update_fields)


def rollback_reason_state_semantics(apps, schema_editor):
    # Не откатываем данные: причины могут быть уже отредактированы администратором.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('downtimes', '0003_alter_downtimereason_options_and_more'),
    ]

    operations = [
        migrations.RunPython(apply_reason_state_semantics, rollback_reason_state_semantics),
    ]
