from django.db import migrations


NEW_EQUIPMENT_STATES = [
    {
        'code': 'off_shift',
        'name': 'Вне смены',
        'short_label': 'Вне смены',
        'color_group': 'gray',
        'semantic_group': 'availability',
        'priority': 35,
        'description': 'Техника не входит в текущий сменный контур и не должна сейчас работать.',
    },
    {
        'code': 'waiting_for_shift',
        'name': 'Ожидает начала смены',
        'short_label': 'Ожидает смену',
        'color_group': 'blue',
        'semantic_group': 'assignment',
        'priority': 45,
        'description': 'Техника и сотрудник назначены или запланированы, но плановое время смены еще не наступило.',
    },
    {
        'code': 'no_driver',
        'name': 'Нет водителя',
        'short_label': 'Нет водителя',
        'color_group': 'yellow',
        'semantic_group': 'availability',
        'priority': 75,
        'allows_assignment': True,
        'requires_attention': True,
        'description': 'Самосвал входит в текущий или целевой сменный контур, но водитель не назначен.',
    },
    {
        'code': 'no_operator',
        'name': 'Нет машиниста',
        'short_label': 'Нет машиниста',
        'color_group': 'yellow',
        'semantic_group': 'availability',
        'priority': 76,
        'allows_assignment': True,
        'requires_attention': True,
        'description': 'Экскаватор или другая техника с оператором входит в текущий или целевой сменный контур, но машинист не назначен.',
    },
]


def apply_color_semantics(apps, schema_editor):
    EquipmentState = apps.get_model('references', 'EquipmentState')
    EquipmentState.objects.filter(code='free').update(
        name='Свободен',
        short_label='Свободен',
        color_group='gray',
        semantic_group='availability',
        allows_assignment=True,
        allows_drag=True,
        requires_attention=False,
        description='Техника доступна и сейчас не выполняет производственную операцию. Серый цвет означает нормальный фон без отклонений.',
    )
    EquipmentState.objects.filter(code='garage').update(
        color_group='gray',
        semantic_group='availability',
    )
    EquipmentState.objects.filter(code='inactive').update(
        color_group='gray',
        semantic_group='terminal',
    )
    for state in NEW_EQUIPMENT_STATES:
        EquipmentState.objects.update_or_create(
            code=state['code'],
            defaults={
                'name': state['name'],
                'short_label': state.get('short_label', ''),
                'color_group': state['color_group'],
                'semantic_group': state['semantic_group'],
                'priority': state.get('priority', 100),
                'allows_assignment': state.get('allows_assignment', False),
                'allows_drag': state.get('allows_drag', False),
                'blocks_operation': state.get('blocks_operation', False),
                'requires_attention': state.get('requires_attention', False),
                'requires_reason': state.get('requires_reason', False),
                'is_terminal': state.get('is_terminal', False),
                'is_active': state.get('is_active', True),
                'description': state.get('description', ''),
            },
        )


def reverse_color_semantics(apps, schema_editor):
    EquipmentState = apps.get_model('references', 'EquipmentState')
    EquipmentState.objects.filter(code__in=[state['code'] for state in NEW_EQUIPMENT_STATES]).delete()
    EquipmentState.objects.filter(code='free').update(
        color_group='yellow',
        requires_attention=True,
        description='Техника доступна или ожидает назначения. В проекте желтый цвет означает ожидание действия, а не аварию.',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('references', '0003_remove_in_transit_and_rename_free'),
    ]

    operations = [
        migrations.RunPython(apply_color_semantics, reverse_color_semantics),
    ]
