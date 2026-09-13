from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('shifts', '0019_merge_20260912_2035'),
    ]

    operations = [
        migrations.AddField(
            model_name='shiftclientaction',
            name='request_signature',
            field=models.CharField(
                blank=True,
                default='',
                max_length=64,
                verbose_name='Подпись запроса',
            ),
        ),
    ]
