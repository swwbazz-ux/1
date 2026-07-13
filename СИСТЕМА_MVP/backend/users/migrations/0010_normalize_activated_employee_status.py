from django.db import migrations


def normalize_activated_employee_status(apps, schema_editor):
    Employee = apps.get_model('users', 'Employee')

    Employee.objects.filter(
        status='not_activated',
        is_active=True,
        accesses__status='activated',
        accesses__is_active=True,
    ).distinct().update(status='active')


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0009_employee_oup_fields_and_audit_target'),
    ]

    operations = [
        migrations.RunPython(
            normalize_activated_employee_status,
            migrations.RunPython.noop,
        ),
    ]
