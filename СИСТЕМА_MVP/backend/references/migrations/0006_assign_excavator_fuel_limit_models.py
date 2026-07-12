from django.db import migrations


def assign_excavator_models(apps, schema_editor):
    EquipmentModel = apps.get_model('references', 'EquipmentModel')
    EquipmentPlanGroup = apps.get_model('shifts', 'EquipmentPlanGroup')

    series = (
        ('3000', 5000),
        ('4000', 7000),
    )
    for series_name, fuel_limit in series:
        groups = EquipmentPlanGroup.objects.filter(name__icontains=series_name).prefetch_related('equipment')
        for group in groups:
            equipment_items = list(group.equipment.select_related('equipment_type'))
            if not equipment_items:
                continue
            equipment_type = equipment_items[0].equipment_type
            equipment_model, _ = EquipmentModel.objects.get_or_create(
                equipment_type=equipment_type,
                name=f'Экскаватор {series_name}',
                defaults={'fuel_capacity_limit_l': fuel_limit},
            )
            if equipment_model.fuel_capacity_limit_l != fuel_limit:
                equipment_model.fuel_capacity_limit_l = fuel_limit
                equipment_model.save(update_fields=['fuel_capacity_limit_l'])
            group.equipment.filter(model__isnull=True).update(model=equipment_model)


class Migration(migrations.Migration):

    dependencies = [
        ('references', '0005_equipmentmodel_fuel_capacity_limit_l'),
        ('shifts', '0008_employeeshift_unique_open_shift_per_employee_and_more'),
    ]

    operations = [
        migrations.RunPython(assign_excavator_models, migrations.RunPython.noop),
    ]
