from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0016_watchcomposition_employee_watch_composition'),
    ]

    operations = [
        migrations.AddField(
            model_name='employee',
            name='sex',
            field=models.CharField(
                choices=[
                    ('unknown', 'Не указан'),
                    ('male', 'Мужской'),
                    ('female', 'Женский'),
                ],
                default='unknown',
                max_length=7,
                verbose_name='Пол',
            ),
        ),
        migrations.AddConstraint(
            model_name='employee',
            constraint=models.CheckConstraint(
                condition=models.Q(sex__in=['unknown', 'male', 'female']),
                name='employee_sex_valid',
            ),
        ),
    ]
