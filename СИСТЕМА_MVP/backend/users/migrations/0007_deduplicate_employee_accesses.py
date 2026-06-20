from django.db import migrations, models


def deduplicate_employee_accesses(apps, schema_editor):
    EmployeeAccess = apps.get_model('users', 'EmployeeAccess')
    duplicate_keys = (
        EmployeeAccess.objects
        .values('employee_id', 'role_id')
        .annotate(count=models.Count('id'))
        .filter(count__gt=1)
    )
    status_priority = {
        'activated': 0,
        'not_activated': 1,
        'blocked': 2,
        'deactivated': 3,
    }
    for key in duplicate_keys:
        accesses = list(
            EmployeeAccess.objects
            .filter(employee_id=key['employee_id'], role_id=key['role_id'])
            .order_by('-created_at', '-id')
        )
        accesses.sort(key=lambda item: (status_priority.get(item.status, 9), -item.id))
        keep = accesses[0]
        EmployeeAccess.objects.filter(
            employee_id=key['employee_id'],
            role_id=key['role_id'],
        ).exclude(id=keep.id).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0006_remove_global_unique_access_code'),
    ]

    operations = [
        migrations.RunPython(deduplicate_employee_accesses, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='employeeaccess',
            constraint=models.UniqueConstraint(fields=('employee', 'role'), name='unique_employee_role_access'),
        ),
    ]
