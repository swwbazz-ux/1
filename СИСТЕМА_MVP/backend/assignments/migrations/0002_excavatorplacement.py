from django.db import migrations, models
import django.db.models.deletion


def seed_active_excavator_placements(apps, schema_editor):
    Equipment = apps.get_model('references', 'Equipment')
    ExcavatorPlacement = apps.get_model('assignments', 'ExcavatorPlacement')

    active_excavators = Equipment.objects.filter(
        equipment_type__name='Экскаватор',
        is_active=True,
    )
    for excavator in active_excavators:
        ExcavatorPlacement.objects.get_or_create(
            excavator=excavator,
            defaults={'zone': 'active'},
        )


def remove_seeded_excavator_placements(apps, schema_editor):
    ExcavatorPlacement = apps.get_model('assignments', 'ExcavatorPlacement')
    ExcavatorPlacement.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('references', '0001_initial'),
        ('users', '0008_remove_employeeaccess_unique_employee_role_access'),
        ('assignments', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExcavatorPlacement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('zone', models.CharField(choices=[('active', 'Активная смена'), ('inactive', 'Неактивная смена')], default='inactive', max_length=16, verbose_name='Зона')),
                ('changed_at', models.DateTimeField(auto_now=True, verbose_name='Изменено')),
                ('changed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='users.employee', verbose_name='Кто изменил')),
                ('excavator', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='excavator_placement', to='references.equipment', verbose_name='Экскаватор')),
            ],
            options={
                'verbose_name': 'Размещение экскаватора',
                'verbose_name_plural': 'Размещения экскаваторов',
                'ordering': ['excavator__garage_number'],
            },
        ),
        migrations.RunPython(seed_active_excavator_placements, remove_seeded_excavator_placements),
    ]
