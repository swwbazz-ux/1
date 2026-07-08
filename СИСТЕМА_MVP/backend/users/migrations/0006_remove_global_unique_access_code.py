from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0005_employee_position'),
    ]

    operations = [
        migrations.AlterField(
            model_name='employeeaccess',
            name='access_code',
            field=models.CharField(max_length=128, verbose_name='Код доступа'),
        ),
    ]
