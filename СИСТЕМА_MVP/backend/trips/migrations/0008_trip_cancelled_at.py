from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('trips', '0007_alter_trip_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='trip',
            name='cancelled_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='Отменён',
            ),
        ),
    ]
