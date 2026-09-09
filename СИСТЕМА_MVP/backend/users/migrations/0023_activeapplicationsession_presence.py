from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0022_employee_protected_card'),
    ]

    operations = [
        migrations.AddField(
            model_name='activeapplicationsession',
            name='background_seen_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Последняя фоновая связь'),
        ),
        migrations.AddField(
            model_name='activeapplicationsession',
            name='client_kind',
            field=models.CharField(
                blank=True,
                choices=[
                    ('android_apk', 'APK Android'),
                    ('android_pwa', 'PWA Android'),
                    ('ios_pwa', 'PWA iPhone/iPad'),
                    ('pwa', 'PWA'),
                    ('safari', 'Safari'),
                    ('browser', 'Браузер'),
                ],
                max_length=16,
                verbose_name='Вариант приложения',
            ),
        ),
        migrations.AddField(
            model_name='activeapplicationsession',
            name='client_version',
            field=models.CharField(blank=True, max_length=32, verbose_name='Версия APK'),
        ),
        migrations.AddField(
            model_name='activeapplicationsession',
            name='foreground_seen_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Последняя активность на экране'),
        ),
    ]
