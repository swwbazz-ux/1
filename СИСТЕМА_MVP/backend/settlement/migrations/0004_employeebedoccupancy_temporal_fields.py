import django.utils.timezone
from django.db import migrations, models


def copy_legacy_occupancy_dates(apps, schema_editor):
    EmployeeBedOccupancy = apps.get_model(
        'settlement',
        'EmployeeBedOccupancy',
    )
    EmployeeBedOccupancy.objects.using(schema_editor.connection.alias).update(
        starts_at=models.F('settled_at'),
        ends_at=None,
        terminated_at=models.F('ended_at'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('settlement', '0003_employeebedoccupancy'),
    ]

    operations = [
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='starts_at',
            field=models.DateTimeField(
                null=True,
                verbose_name='Начало размещения',
            ),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='ends_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='Плановое окончание размещения',
            ),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='terminated_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='Досрочное прекращение размещения',
            ),
        ),
        migrations.RunPython(
            copy_legacy_occupancy_dates,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='employeebedoccupancy',
            name='starts_at',
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                verbose_name='Начало размещения',
            ),
        ),
        migrations.AddConstraint(
            model_name='employeebedoccupancy',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(ends_at__isnull=True)
                    | models.Q(ends_at__gt=models.F('starts_at'))
                ),
                name='occupancy_ends_after_start',
            ),
        ),
        migrations.AddConstraint(
            model_name='employeebedoccupancy',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(terminated_at__isnull=True)
                    | models.Q(terminated_at__gt=models.F('starts_at'))
                ),
                name='occupancy_term_after_start',
            ),
        ),
        migrations.AddConstraint(
            model_name='employeebedoccupancy',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(ends_at__isnull=True)
                    | models.Q(terminated_at__isnull=True)
                    | models.Q(terminated_at__lt=models.F('ends_at'))
                ),
                name='occupancy_term_before_end',
            ),
        ),
    ]
