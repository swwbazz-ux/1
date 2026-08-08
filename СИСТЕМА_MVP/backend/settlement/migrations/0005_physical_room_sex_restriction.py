from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement', '0004_employeebedoccupancy_temporal_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='physicalroom',
            name='sex_restriction',
            field=models.CharField(
                blank=False,
                choices=[
                    ('unknown', 'Не указано'),
                    ('male_only', 'Мужская'),
                    ('female_only', 'Женская'),
                ],
                default='unknown',
                max_length=11,
                verbose_name='Ограничение пола комнаты',
            ),
        ),
        migrations.AddConstraint(
            model_name='physicalroom',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    sex_restriction__in=['unknown', 'male_only', 'female_only'],
                ),
                name='physical_room_sex_restriction_valid',
            ),
        ),
    ]
