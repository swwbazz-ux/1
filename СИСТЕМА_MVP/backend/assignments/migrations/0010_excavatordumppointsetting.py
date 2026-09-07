from django.db import migrations, models
import django.db.models.deletion


def copy_primary_dump_points(apps, schema_editor):
    ExcavatorPlacement = apps.get_model('assignments', 'ExcavatorPlacement')
    ExcavatorDumpPointSetting = apps.get_model('assignments', 'ExcavatorDumpPointSetting')
    rows = []
    for placement in ExcavatorPlacement.objects.exclude(work_dump_point_id__isnull=True).iterator():
        rows.append(ExcavatorDumpPointSetting(
            placement_id=placement.id,
            dump_point_id=placement.work_dump_point_id,
            transport_distance_km=placement.transport_distance_km,
            position=0,
            changed_by_id=placement.changed_by_id,
        ))
    ExcavatorDumpPointSetting.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ('assignments', '0009_excavatorplacement_transport_distance_km'),
        ('references', '0001_initial'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExcavatorDumpPointSetting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('transport_distance_km', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, verbose_name='Плечо до точки разгрузки, км')),
                ('position', models.PositiveSmallIntegerField(default=0, verbose_name='Порядок')),
                ('changed_at', models.DateTimeField(auto_now=True, verbose_name='Изменено')),
                ('changed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='users.employee', verbose_name='Кто изменил')),
                ('dump_point', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='excavator_work_settings', to='references.dumppoint', verbose_name='Точка разгрузки')),
                ('placement', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='dump_point_settings', to='assignments.excavatorplacement', verbose_name='Размещение экскаватора')),
            ],
            options={
                'verbose_name': 'Рабочая точка разгрузки экскаватора',
                'verbose_name_plural': 'Рабочие точки разгрузки экскаваторов',
                'ordering': ['position', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='excavatordumppointsetting',
            constraint=models.UniqueConstraint(fields=('placement', 'dump_point'), name='assignments_unique_excavator_dump_point'),
        ),
        migrations.RunPython(copy_primary_dump_points, migrations.RunPython.noop),
    ]
