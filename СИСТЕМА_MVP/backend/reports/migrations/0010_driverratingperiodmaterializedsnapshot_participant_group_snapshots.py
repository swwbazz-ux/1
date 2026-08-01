import django.core.serializers.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0009_driver_rating_period_materialized_snapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='driverratingperiodmaterializedsnapshot',
            name='participant_group_snapshots',
            field=models.JSONField(
                blank=True,
                default=None,
                encoder=django.core.serializers.json.DjangoJSONEncoder,
                null=True,
                verbose_name='Исторические данные группы участников',
            ),
        ),
    ]
