from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('references', '0010_remove_obsolete_cargo_aliases'),
        ('shifts', '0017_drivershiftreadingconfirmation'),
        ('users', '0024_employee_contractor_access_from_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExcavatorShiftReadingConfirmation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client_action_id', models.CharField(max_length=128, unique=True, verbose_name='ID действия клиента')),
                ('submitted_fuel_percent', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Введённое топливо, %')),
                ('fuel_capacity_l', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Вместимость бака, л')),
                ('start_fuel', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Топливо на начало, л')),
                ('start_engine_hours', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Моточасы на начало')),
                ('end_fuel', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Топливо на конец, л')),
                ('end_engine_hours', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Моточасы на конец')),
                ('warnings', models.JSONField(default=list, verbose_name='Подтверждённые предупреждения')),
                ('confirmed_at', models.DateTimeField(auto_now_add=True, verbose_name='Подтверждено машинистом')),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='excavator_shift_reading_confirmations', to='users.employee', verbose_name='Машинист экскаватора')),
                ('equipment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='excavator_shift_reading_confirmations', to='references.equipment', verbose_name='Экскаватор')),
                ('shift', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='excavator_reading_confirmation', to='shifts.employeeshift', verbose_name='Смена')),
            ],
            options={
                'verbose_name': 'Подтверждение аномальных показаний машинистом экскаватора',
                'verbose_name_plural': 'Подтверждения аномальных показаний машинистами экскаваторов',
                'ordering': ['-confirmed_at'],
            },
        ),
    ]
