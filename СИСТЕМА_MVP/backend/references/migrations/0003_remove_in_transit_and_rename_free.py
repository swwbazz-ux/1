from django.db import migrations


def apply_state_cleanup(apps, schema_editor):
    EquipmentState = apps.get_model('references', 'EquipmentState')
    EquipmentState.objects.filter(code='free').update(
        name='Свободен',
        short_label='Свободен',
    )
    EquipmentState.objects.filter(code='loaded_waiting_unload').update(
        name='На разгрузку',
        short_label='На разгрузку',
    )
    EquipmentState.objects.filter(code='in_transit').delete()


def reverse_state_cleanup(apps, schema_editor):
    EquipmentState = apps.get_model('references', 'EquipmentState')
    EquipmentState.objects.filter(code='free').update(
        name='Свободна',
        short_label='Свободна',
    )
    EquipmentState.objects.filter(code='loaded_waiting_unload').update(
        name='В пути на разгрузку',
        short_label='На разгрузку',
    )
    EquipmentState.objects.update_or_create(
        code='in_transit',
        defaults={
            'name': 'В пути',
            'short_label': 'В пути',
            'color_group': 'green',
            'semantic_group': 'operation',
            'priority': 60,
            'blocks_operation': True,
            'description': 'Устаревшее состояние открытого рейса.',
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('references', '0002_equipmentstate'),
    ]

    operations = [
        migrations.RunPython(apply_state_cleanup, reverse_state_cleanup),
    ]
