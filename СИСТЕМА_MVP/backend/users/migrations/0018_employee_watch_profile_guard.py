from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0017_employee_sex'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='employee',
            options={
                'base_manager_name': 'objects',
                'ordering': ['full_name'],
                'verbose_name': 'Сотрудник',
                'verbose_name_plural': 'Сотрудники',
            },
        ),
    ]
