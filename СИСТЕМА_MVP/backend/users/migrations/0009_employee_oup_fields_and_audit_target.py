from django.db import migrations, models


def prepare_oup_role_and_work_categories(apps, schema_editor):
    Employee = apps.get_model('users', 'Employee')
    Role = apps.get_model('users', 'Role')

    Role.objects.update_or_create(
        code='oup',
        defaults={'name': 'Специалист ОУП', 'is_active': True},
    )

    for employee in Employee.objects.all().iterator():
        role_codes = set(
            employee.accesses.filter(
                role__code__in=['driver', 'excavator_operator'],
                is_active=True,
            ).values_list('role__code', flat=True)
        )
        if len(role_codes) == 1:
            employee.work_category = role_codes.pop()
            employee.save(update_fields=['work_category'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0008_remove_employeeaccess_unique_employee_role_access'),
    ]

    operations = [
        migrations.AddField(
            model_name='employee',
            name='birth_date',
            field=models.DateField(blank=True, null=True, verbose_name='Дата рождения'),
        ),
        migrations.AddField(
            model_name='employee',
            name='department',
            field=models.CharField(blank=True, max_length=160, verbose_name='Подразделение'),
        ),
        migrations.AddField(
            model_name='employee',
            name='work_category',
            field=models.CharField(
                choices=[
                    ('driver', 'Водитель самосвала'),
                    ('excavator_operator', 'Машинист экскаватора'),
                    ('other', 'Без привязки к технике'),
                ],
                default='other',
                max_length=32,
                verbose_name='Рабочая категория',
            ),
        ),
        migrations.AddField(
            model_name='adminactionlog',
            name='object_id',
            field=models.CharField(blank=True, max_length=64, verbose_name='ID объекта'),
        ),
        migrations.RunPython(prepare_oup_role_and_work_categories, migrations.RunPython.noop),
    ]
