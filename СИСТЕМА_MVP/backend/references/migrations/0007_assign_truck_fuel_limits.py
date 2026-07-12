from django.db import migrations


def assign_truck_fuel_limits(apps, schema_editor):
    EquipmentModel = apps.get_model('references', 'EquipmentModel')
    EquipmentModel.objects.filter(name__icontains='БелАЗ').update(fuel_capacity_limit_l=2000)
    EquipmentModel.objects.filter(name__icontains='BelAZ').update(fuel_capacity_limit_l=2000)
    EquipmentModel.objects.filter(name__icontains='NHL').update(fuel_capacity_limit_l=3000)


class Migration(migrations.Migration):

    dependencies = [
        ('references', '0006_assign_excavator_fuel_limit_models'),
    ]

    operations = [
        migrations.RunPython(assign_truck_fuel_limits, migrations.RunPython.noop),
    ]
