from django.db import migrations, models


def assert_single_open_oup_period(apps, schema_editor):
    EmployeeShift = apps.get_model('shifts', 'EmployeeShift')
    open_period_count = EmployeeShift.objects.filter(
        workplace_code='oup',
        closed_at__isnull=True,
    ).count()
    if open_period_count > 1:
        raise RuntimeError(
            'Cannot add unique_open_oup_period: '
            f'found {open_period_count} open OUP periods.'
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('shifts', '0012_watchperiod_watch_composition'),
    ]

    operations = [
        migrations.RunPython(
            assert_single_open_oup_period,
            noop_reverse,
        ),
        migrations.AddConstraint(
            model_name='employeeshift',
            constraint=models.UniqueConstraint(
                fields=('workplace_code',),
                condition=models.Q(
                    workplace_code='oup',
                    closed_at__isnull=True,
                ),
                name='unique_open_oup_period',
            ),
        ),
    ]
