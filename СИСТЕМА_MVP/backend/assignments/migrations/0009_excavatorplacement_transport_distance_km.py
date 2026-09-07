from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('assignments', '0008_crew_plan_secondary_employee'),
    ]

    operations = [
        migrations.AddField(
            model_name='excavatorplacement',
            name='transport_distance_km',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=8,
                null=True,
                verbose_name='Рабочее плечо до разгрузки, км',
            ),
        ),
    ]
