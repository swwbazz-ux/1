from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('trips', '0004_dispatcheractionlog_reason'),
        ('users', '0008_remove_employeeaccess_unique_employee_role_access'),
    ]

    operations = [
        migrations.AlterField(
            model_name='trip',
            name='status',
            field=models.CharField(
                choices=[
                    ('active', 'Активный'),
                    ('loaded_waiting_unload', 'В пути на разгрузку'),
                    ('completed', 'Выполнен'),
                    ('cancelled', 'Отменен'),
                ],
                default='active',
                max_length=32,
                verbose_name='Статус',
            ),
        ),
        migrations.CreateModel(
            name='TripClientAction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action_type', models.CharField(max_length=64, verbose_name='Тип действия клиента')),
                ('client_action_id', models.CharField(max_length=128, verbose_name='ID действия клиента')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='trip_client_actions', to='users.employee', verbose_name='Кто выполнил действие')),
                ('trip', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='client_actions', to='trips.trip', verbose_name='Рейс')),
            ],
            options={
                'verbose_name': 'Клиентское действие рейса',
                'verbose_name_plural': 'Клиентские действия рейсов',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='tripclientaction',
            constraint=models.UniqueConstraint(fields=('action_type', 'client_action_id'), name='unique_trip_client_action'),
        ),
    ]
