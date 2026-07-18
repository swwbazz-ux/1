from django.db import migrations


ROTATION_ROLES = (
    (
        'timekeeper',
        'Табельщик',
        'Сбор данных перевахты, контроль ответов, выгрузка и оформление согласованных продлений.',
    ),
    (
        'site_manager',
        'Начальник участка',
        'Согласование запросов сотрудников на продление вахты.',
    ),
    (
        'employee_portal',
        'Сотрудник',
        'Базовый доступ сотрудника к личным запросам учетной системы.',
    ),
)


def seed_rotation_roles(apps, schema_editor):
    role_model = apps.get_model('users', 'Role')
    for code, name, description in ROTATION_ROLES:
        role_model.objects.update_or_create(
            code=code,
            defaults={
                'name': name,
                'description': description,
                'is_active': True,
            },
        )


def remove_rotation_roles(apps, schema_editor):
    role_model = apps.get_model('users', 'Role')
    role_model.objects.filter(
        code__in=[code for code, _name, _description in ROTATION_ROLES],
        accesses__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0014_allow_work_schedules_without_brigades'),
    ]

    operations = [
        migrations.RunPython(seed_rotation_roles, remove_rotation_roles),
    ]
