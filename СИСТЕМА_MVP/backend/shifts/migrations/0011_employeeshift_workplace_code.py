from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shifts', '0010_alter_shiftreadingcorrection_employee_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='employeeshift',
            name='workplace_code',
            field=models.CharField(blank=True, db_index=True, max_length=32, verbose_name='Рабочий контур'),
        ),
    ]
