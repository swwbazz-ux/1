from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_operationalstateevent'),
        ('downtimes', '0005_downtime_actor_subject'),
        ('references', '0010_remove_obsolete_cargo_aliases'),
        ('shifts', '0019_employeeshift_service_close_kind'),
        ('trips', '0012_trip_offline_loading_times'),
        ('users', '0024_employee_contractor_access_from_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='OfflineFieldEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_id', models.CharField(max_length=128, unique=True, verbose_name='Неизменяемый ID события')),
                ('event_type', models.CharField(db_index=True, max_length=96, verbose_name='Тип события')),
                ('format_version', models.PositiveSmallIntegerField(default=1, verbose_name='Версия формата')),
                ('role_code', models.CharField(max_length=64, verbose_name='Роль')),
                ('device_id', models.CharField(max_length=128, verbose_name='ID устройства')),
                ('sequence', models.PositiveBigIntegerField(verbose_name='Порядок на устройстве')),
                ('depends_on', models.JSONField(blank=True, default=list, verbose_name='Зависимости')),
                ('occurred_at', models.DateTimeField(verbose_name='Время на устройстве')),
                ('received_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='Получено сервером')),
                ('local_trip_id', models.CharField(blank=True, max_length=128, verbose_name='Локальный ID рейса')),
                ('local_downtime_id', models.CharField(blank=True, max_length=128, verbose_name='Локальный ID простоя')),
                ('context_snapshot', models.JSONField(blank=True, default=dict, verbose_name='Контекст клиента')),
                ('payload', models.JSONField(blank=True, default=dict, verbose_name='Параметры')),
                ('fingerprint', models.CharField(max_length=64, verbose_name='Отпечаток')),
                ('status', models.CharField(choices=[('processing', 'Обрабатывается'), ('accepted', 'Принято'), ('retry', 'Повторить'), ('conflict', 'Требует сверки'), ('invalid', 'Некорректно')], default='processing', max_length=16, verbose_name='Статус')),
                ('retryable', models.BooleanField(default=False, verbose_name='Можно повторить')),
                ('error_code', models.CharField(blank=True, max_length=64, verbose_name='Код ошибки')),
                ('error_message', models.TextField(blank=True, verbose_name='Ошибка')),
                ('result_payload', models.JSONField(blank=True, default=dict, verbose_name='Подтверждение сервера')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('access', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='offline_field_events', to='users.employeeaccess', verbose_name='Доступ')),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='offline_field_events', to='users.employee', verbose_name='Сотрудник')),
                ('downtime_event', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='offline_field_events', to='downtimes.downtimeevent', verbose_name='Простой')),
                ('equipment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='offline_field_events', to='references.equipment', verbose_name='Техника')),
                ('shift', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='offline_field_events', to='shifts.employeeshift', verbose_name='Смена')),
                ('trip', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='offline_field_events', to='trips.trip', verbose_name='Рейс')),
            ],
            options={
                'verbose_name': 'Offline-событие полевого приложения',
                'verbose_name_plural': 'Offline-события полевых приложений',
                'ordering': ['received_at', 'id'],
                'indexes': [models.Index(fields=['actor', 'device_id', 'status'], name='off_evt_actor_dev_status'), models.Index(fields=['received_at', 'status'], name='off_evt_received_status')],
                'constraints': [models.UniqueConstraint(fields=('actor', 'role_code', 'device_id', 'sequence'), name='unique_offline_device_event_sequence')],
            },
        ),
        migrations.CreateModel(
            name='OfflineFieldEventConflict',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('attempted_event_id', models.CharField(max_length=128, verbose_name='Повторный ID события')),
                ('role_code', models.CharField(max_length=64, verbose_name='Роль')),
                ('device_id', models.CharField(max_length=128, verbose_name='ID устройства')),
                ('fingerprint', models.CharField(max_length=64, verbose_name='Отпечаток повтора')),
                ('code', models.CharField(max_length=64, verbose_name='Код конфликта')),
                ('submitted_event', models.JSONField(default=dict, verbose_name='Повторно полученное событие')),
                ('received_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='Получено')),
                ('access', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='offline_field_event_conflicts', to='users.employeeaccess', verbose_name='Доступ')),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='offline_field_event_conflicts', to='users.employee', verbose_name='Сотрудник')),
                ('existing_event', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='conflict_attempts', to='core.offlinefieldevent', verbose_name='Исходное событие')),
            ],
            options={
                'verbose_name': 'Конфликт offline-события',
                'verbose_name_plural': 'Конфликты offline-событий',
                'ordering': ['-received_at', '-id'],
                'indexes': [models.Index(fields=['attempted_event_id', 'received_at'], name='off_conf_event_time')],
            },
        ),
    ]
