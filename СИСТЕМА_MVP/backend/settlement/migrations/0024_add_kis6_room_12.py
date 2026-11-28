from django.db import migrations


def add_kis6_room_12(apps, schema_editor):
    dormitories = apps.get_model('references', 'Dormitory').objects.using(schema_editor.connection.alias)
    rooms = apps.get_model('settlement', 'PhysicalRoom').objects.using(schema_editor.connection.alias)
    beds = apps.get_model('settlement', 'PhysicalBed').objects.using(schema_editor.connection.alias)
    dormitory = dormitories.filter(number='6', kind='own').first()
    if dormitory is None:
        return

    # Между блоками 11 и 40 в реестре расположен блок 12. Чётные номера
    # первого этажа идут по правой стороне коридора.
    room, created = rooms.get_or_create(
        dormitory=dormitory,
        floor=1,
        number=12,
        defaults={
            'room_type': 'standard',
            'transfer_status': 'transferred',
            'capacity': 6,
            'corridor_side': 'right',
            'side_position': 6,
        },
    )
    if created:
        bed_model = beds.model
        beds.bulk_create([
            bed_model(room=room, stable_id=f'KIS6-F1-R12-{block}{position}', block=block, position=position)
            for block in ('A', 'B') for position in range(1, 4)
        ])


class Migration(migrations.Migration):
    dependencies = [
        ('references', '0011_dormitory_contractor_organization_dormitory_kind_and_more'),
        ('settlement', '0023_alter_settlementplacementevent_event_type'),
    ]
    # При откате кода сохраняются добавленная комната и записи её проживания.
    operations = [migrations.RunPython(add_kis6_room_12, migrations.RunPython.noop)]
