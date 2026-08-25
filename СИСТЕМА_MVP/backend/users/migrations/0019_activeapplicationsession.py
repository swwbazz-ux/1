from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0018_employee_watch_profile_guard'),
    ]

    operations = [
        migrations.CreateModel(
            name='ActiveApplicationSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('session_key', models.CharField(max_length=40, unique=True, verbose_name='Ключ сессии')),
                ('role_code', models.CharField(db_index=True, max_length=64, verbose_name='Роль')),
                ('app_code', models.CharField(db_index=True, max_length=64, verbose_name='Приложение')),
                ('path', models.CharField(blank=True, max_length=255, verbose_name='Текущий экран')),
                ('device_kind', models.CharField(blank=True, max_length=16, verbose_name='Тип устройства')),
                ('first_seen_at', models.DateTimeField(auto_now_add=True, verbose_name='Первое обращение')),
                ('last_seen_at', models.DateTimeField(db_index=True, verbose_name='Последняя активность')),
                ('access', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='application_sessions', to='users.employeeaccess', verbose_name='Доступ сотрудника')),
            ],
            options={
                'verbose_name': 'Активная сессия приложения',
                'verbose_name_plural': 'Активные сессии приложений',
                'ordering': ['-last_seen_at'],
                'indexes': [
                    models.Index(fields=['app_code', 'last_seen_at'], name='app_session_app_seen_idx'),
                    models.Index(fields=['access', 'last_seen_at'], name='app_session_access_seen_idx'),
                ],
            },
        ),
    ]
