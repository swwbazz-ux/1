from collections import defaultdict

from django.db import migrations, transaction


DEFAULT_GROUPS = {
    'belaz_trucks': {
        'name': 'Самосвалы БелАЗ',
        'calculation_mode': 'trips',
        'comment': 'Стартовая группа для ежесменного плана самосвалов БелАЗ.',
    },
    'nhl_trucks': {
        'name': 'Самосвалы NHL',
        'calculation_mode': 'trips',
        'comment': 'Стартовая группа для ежесменного плана самосвалов NHL.',
    },
    'excavators_4000': {
        'name': 'Экскаваторы 4000',
        'calculation_mode': 'volume_m3',
        'comment': 'Стартовая группа для экскаваторов 1 и 8.',
    },
    'excavators_3000': {
        'name': 'Экскаваторы 3000',
        'calculation_mode': 'volume_m3',
        'comment': 'Стартовая группа для остальных экскаваторов.',
    },
}


def text(value):
    return str(value or '').casefold()


def equipment_type_name(equipment):
    return text(getattr(getattr(equipment, 'equipment_type', None), 'name', ''))


def equipment_model_name(equipment):
    return text(getattr(getattr(equipment, 'model', None), 'name', ''))


def garage_number_int(equipment):
    digits = ''.join(char for char in str(getattr(equipment, 'garage_number', '') or '') if char.isdigit())
    return int(digits) if digits else None


def is_truck(equipment):
    return 'самосвал' in equipment_type_name(equipment)


def is_excavator(equipment):
    return 'экскаватор' in equipment_type_name(equipment)


def expected_group_code(equipment):
    model_name = equipment_model_name(equipment)
    if is_truck(equipment) and ('белаз' in model_name or 'belaz' in model_name):
        return 'belaz_trucks'
    if is_truck(equipment) and ('nhl' in model_name or 'nte' in model_name):
        return 'nhl_trucks'
    if is_excavator(equipment) and garage_number_int(equipment) in {1, 8}:
        return 'excavators_4000'
    if is_excavator(equipment):
        return 'excavators_3000'
    return ''


def reconcile_equipment_plan_groups(apps, schema_editor):
    EquipmentPlanGroup = apps.get_model('shifts', 'EquipmentPlanGroup')
    Equipment = apps.get_model('references', 'Equipment')

    with transaction.atomic():
        equipment_by_group = defaultdict(set)
        for equipment in Equipment.objects.select_related('equipment_type', 'model').all():
            group_code = expected_group_code(equipment)
            if group_code:
                equipment_by_group[group_code].add(equipment.id)

        for group_code, defaults in DEFAULT_GROUPS.items():
            group, _ = EquipmentPlanGroup.objects.get_or_create(
                code=group_code,
                defaults={
                    'name': defaults['name'],
                    'calculation_mode': defaults['calculation_mode'],
                    'is_active': False,
                    'comment': defaults['comment'],
                },
            )
            update_fields = []
            if group.name != defaults['name']:
                group.name = defaults['name']
                update_fields.append('name')
            if group.calculation_mode != defaults['calculation_mode']:
                group.calculation_mode = defaults['calculation_mode']
                update_fields.append('calculation_mode')
            if not group.comment:
                group.comment = defaults['comment']
                update_fields.append('comment')
            if update_fields:
                group.save(update_fields=update_fields)
            group.equipment.set(equipment_by_group[group_code])


class Migration(migrations.Migration):
    dependencies = [
        ('shifts', '0005_employeeshift_plan_assigned_at_and_more'),
    ]

    operations = [
        migrations.RunPython(reconcile_equipment_plan_groups, migrations.RunPython.noop),
    ]
