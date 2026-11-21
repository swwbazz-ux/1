import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0003_offline_field_event'),
    ]

    operations = [
        migrations.AddField(
            model_name='operationalstateversion',
            name='journal_epoch',
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                verbose_name='Поколение журнала',
            ),
        ),
        migrations.AddConstraint(
            model_name='operationalstateevent',
            constraint=models.UniqueConstraint(
                fields=('key', 'version'),
                name='unique_operational_event_key_version',
            ),
        ),
    ]
