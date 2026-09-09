from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('shifts', '0016_numbered_shift_labels'),
        ('users', '0024_employee_contractor_access_from_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='DriverShiftReadingConfirmation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client_action_id', models.CharField(max_length=128, unique=True, verbose_name='ID действия клиента')),
                ('start_fuel', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Топливо на начало')),
                ('start_mileage', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Одометр на начало')),
                ('start_engine_hours', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Моточасы на начало')),
                ('end_fuel', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Топливо на конец')),
                ('end_mileage', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Одометр на конец')),
                ('end_engine_hours', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Моточасы на конец')),
                ('warnings', models.JSONField(default=list, verbose_name='Подтверждённые предупреждения')),
                ('confirmed_at', models.DateTimeField(auto_now_add=True, verbose_name='Подтверждено водителем')),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='driver_shift_reading_confirmations', to='users.employee', verbose_name='Водитель')),
                ('shift', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='driver_reading_confirmation', to='shifts.employeeshift', verbose_name='Смена')),
            ],
            options={
                'verbose_name': 'Подтверждение аномальных показаний водителем',
                'verbose_name_plural': 'Подтверждения аномальных показаний водителем',
                'ordering': ['-confirmed_at'],
            },
        ),
    ]
