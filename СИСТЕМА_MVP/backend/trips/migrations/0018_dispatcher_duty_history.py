import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def mark_tracking_start(apps, schema_editor):
    tracking = apps.get_model('trips', 'DispatcherDutyTracking')
    tracking.objects.using(schema_editor.connection.alias).get_or_create(pk=1)


class Migration(migrations.Migration):

    dependencies = [
        ('assignments', '0014_crew_slot_reported_employee'),
        ('shifts', '0021_owner_parallel_role_shifts'),
        ('trips', '0017_free_bucket_accepted_by_driver_action'),
        ('users', '0031_personnel_position_requires_equipment'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dispatcheractionlog',
            name='action_type',
            field=models.CharField(
                verbose_name='Тип действия', max_length=64,
                choices=[
                    ('service_close_shift', 'Служебное закрытие смены'),
                    ('cancel_assignment', 'Снятие назначения'), ('cancel_trip', 'Отмена рейса'),
                    ('complete_trip', 'Служебное завершение рейса'), ('manual_trip', 'Ручной рейс диспетчера'),
                    ('close_downtime', 'Завершение простоя'), ('equipment_settings', 'Настройки экскаватора'),
                    ('report_source_correction', 'Коррекция рейса'),
                ],
            ),
        ),
        migrations.CreateModel(
            name='DispatcherDutyTracking',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='Начало учёта')),
            ],
        ),
        migrations.CreateModel(
            name='DispatcherDutyWindow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('session_digest', models.CharField(max_length=64, verbose_name='Отпечаток сессии')),
                ('window_id', models.UUIDField(verbose_name='Окно пульта')),
                ('state', models.CharField(choices=[('visible', 'Пульт на экране'), ('hidden', 'Пульт свёрнут'), ('closed', 'Окно закрыто'), ('signed_out', 'Выход из приложения')], max_length=16, verbose_name='Состояние')),
                ('started_at', models.DateTimeField(verbose_name='Начало интервала')),
                ('last_seen_at', models.DateTimeField(verbose_name='Последнее подтверждение')),
                ('last_sequence', models.PositiveBigIntegerField(verbose_name='Номер сигнала')),
            ],
        ),
        migrations.AddField(
            model_name='dispatcheractionlog',
            name='actor_role_code',
            field=models.CharField(blank=True, max_length=64, verbose_name='Роль исполнителя'),
        ),
        migrations.AddField(
            model_name='dispatcheractionlog',
            name='duty_detail',
            field=models.TextField(blank=True, verbose_name='Подробности для журнала руководства'),
        ),
        migrations.AddField(
            model_name='dispatcheractionlog',
            name='duty_shift',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='duty_operations', to='shifts.employeeshift', verbose_name='Собственная смена диспетчера'),
        ),
        migrations.AddIndex(
            model_name='dispatcheractionlog',
            index=models.Index(fields=['actor_role_code', 'created_at'], name='dispatch_log_role_time_idx'),
        ),
        migrations.AddField(
            model_name='dispatcherdutywindow',
            name='shift',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='duty_windows', to='shifts.employeeshift'),
        ),
        migrations.AddIndex(
            model_name='dispatcherdutywindow',
            index=models.Index(fields=['shift', 'session_digest', 'window_id', '-id'], name='dispatch_window_latest_idx'),
        ),
        migrations.RunPython(mark_tracking_start, migrations.RunPython.noop),
    ]
