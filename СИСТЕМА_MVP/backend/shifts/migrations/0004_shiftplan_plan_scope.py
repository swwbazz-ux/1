import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shifts', '0003_shiftplan_equipmentshiftplan'),
    ]

    operations = [
        migrations.AddField(
            model_name='shiftplan',
            name='plan_scope',
            field=models.CharField(
                choices=[
                    ('month', 'Месячный план'),
                    ('day_total', 'Суточный план'),
                    ('day_shift', 'Дневная смена'),
                    ('night_shift', 'Ночная смена'),
                ],
                default='day_shift',
                max_length=16,
                verbose_name='Тип плана',
            ),
        ),
        migrations.AlterField(
            model_name='shiftplan',
            name='date',
            field=models.DateField(default=django.utils.timezone.localdate, verbose_name='Дата начала действия'),
        ),
        migrations.AlterField(
            model_name='shiftplan',
            name='shift_type',
            field=models.CharField(
                choices=[('day', 'Дневная'), ('night', 'Ночная')],
                default='day',
                max_length=16,
                verbose_name='Расчетная смена',
            ),
        ),
    ]
