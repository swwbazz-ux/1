from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('downtimes', '0005_downtime_actor_subject'),
    ]

    operations = [
        migrations.AddField(
            model_name='downtimeevent',
            name='closure_reason',
            field=models.CharField(
                blank=True,
                default='',
                max_length=64,
                verbose_name='Причина завершения',
            ),
        ),
    ]
