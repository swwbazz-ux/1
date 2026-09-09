from django.db import migrations


ROCK_CAPACITY_ALIASES = {
    'Окисленная': 'Окисленная руда',
    'Рыхлая': 'Рыхлая порода',
}


def link_legacy_rock_capacity_rules(apps, schema_editor):
    RockType = apps.get_model('references', 'RockType')
    TruckCapacityRule = apps.get_model('references', 'TruckCapacityRule')

    for alias_name, configured_name in ROCK_CAPACITY_ALIASES.items():
        alias = RockType.objects.filter(name=alias_name).first()
        configured = RockType.objects.filter(name=configured_name).first()
        if not alias or not configured:
            continue
        for source_rule in TruckCapacityRule.objects.filter(rock_type=configured):
            TruckCapacityRule.objects.get_or_create(
                equipment_model_id=source_rule.equipment_model_id,
                rock_type=alias,
                defaults={'volume_m3': source_rule.volume_m3},
            )


class Migration(migrations.Migration):
    dependencies = [
        ('references', '0008_equipment_contractor_organization'),
    ]

    operations = [
        migrations.RunPython(
            link_legacy_rock_capacity_rules,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
