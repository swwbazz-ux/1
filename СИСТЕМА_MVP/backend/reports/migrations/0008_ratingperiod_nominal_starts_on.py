from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0007_ratingperiod'),
    ]

    operations = [
        migrations.AddField(
            model_name='ratingperiod',
            name='nominal_starts_on',
            field=models.DateField(
                blank=True,
                editable=False,
                help_text=(
                    'Стабильный ключ автоматически созданного периода. '
                    'Не меняется при ручной корректировке дат.'
                ),
                null=True,
                unique=True,
                verbose_name='Обычная дата начала',
            ),
        ),
    ]
