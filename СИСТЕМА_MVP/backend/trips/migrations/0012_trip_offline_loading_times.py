from django.db import migrations, models


def backfill_loading_times(apps, schema_editor):
    Trip = apps.get_model('trips', 'Trip')
    Trip.objects.filter(loaded_at__isnull=True).update(
        loaded_at=models.F('created_at'),
        load_received_at=models.F('created_at'),
        load_time_source='server_receipt',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('trips', '0011_manual_loading_participation'),
    ]

    operations = [
        migrations.AddField(
            model_name='trip',
            name='load_received_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Погрузка получена сервером'),
        ),
        migrations.AddField(
            model_name='trip',
            name='load_time_source',
            field=models.CharField(choices=[('unknown', 'Неизвестно'), ('excavator_device', 'Часы устройства машиниста'), ('server_receipt', 'Время получения сервером')], default='unknown', max_length=24, verbose_name='Источник времени погрузки'),
        ),
        migrations.AddField(
            model_name='trip',
            name='loaded_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Погрузка совершена'),
        ),
        migrations.RunPython(backfill_loading_times, migrations.RunPython.noop),
    ]
