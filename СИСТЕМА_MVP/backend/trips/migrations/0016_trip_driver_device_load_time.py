from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('trips', '0015_free_bucket_driver_request'),
    ]

    operations = [
        migrations.AlterField(
            model_name='trip',
            name='load_time_source',
            field=models.CharField(
                choices=[
                    ('unknown', 'Неизвестно'),
                    ('excavator_device', 'Часы устройства машиниста'),
                    ('driver_device', 'Часы устройства водителя'),
                    ('server_receipt', 'Время получения сервером'),
                ],
                default='unknown',
                max_length=24,
                verbose_name='Источник времени погрузки',
            ),
        ),
    ]
