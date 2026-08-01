import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            'reports',
            '0010_driverratingperiodmaterializedsnapshot_participant_group_snapshots',
        ),
    ]

    operations = [
        migrations.AddField(
            model_name='driverratingperiodmaterializedsnapshot',
            name='brigade_number',
            field=models.PositiveSmallIntegerField(
                blank=True,
                choices=[
                    (1, 'Бригада №1'),
                    (2, 'Бригада №2'),
                    (3, 'Бригада №3'),
                    (4, 'Бригада №4'),
                ],
                null=True,
                verbose_name='Бригада',
            ),
        ),
        migrations.AddField(
            model_name='driverratingperiodmaterializedsnapshot',
            name='work_schedule',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='driver_rating_materialized_snapshots',
                to='users.workschedule',
                verbose_name='График работы',
            ),
        ),
        migrations.AddField(
            model_name='driverratingperiodmaterializedsnapshot',
            name='participant_group_fingerprint',
            field=models.CharField(
                blank=True,
                max_length=64,
                verbose_name='Fingerprint исторических данных группы',
            ),
        ),
        migrations.AlterField(
            model_name='driverratingperiodmaterializedsnapshot',
            name='watch_composition',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='driver_rating_materialized_snapshots',
                to='users.watchcomposition',
                verbose_name='Состав вахты',
            ),
        ),
        migrations.AlterModelOptions(
            name='driverratingperiodmaterializedsnapshot',
            options={
                'ordering': [
                    '-rating_period__starts_on',
                    'work_schedule_id',
                    'brigade_number',
                    'watch_composition_id',
                    'shift_type',
                ],
                'verbose_name': 'Текущий серверный снимок рейтинга водителей',
                'verbose_name_plural': (
                    'Текущие серверные снимки рейтинга водителей'
                ),
            },
        ),
        migrations.AddConstraint(
            model_name='driverratingperiodmaterializedsnapshot',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        watch_composition__isnull=False,
                        work_schedule__isnull=True,
                        brigade_number__isnull=True,
                    )
                    | models.Q(
                        watch_composition__isnull=True,
                        work_schedule__isnull=False,
                        brigade_number__isnull=False,
                    )
                ),
                name='drv_rating_group_key_shape',
            ),
        ),
        migrations.AddConstraint(
            model_name='driverratingperiodmaterializedsnapshot',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    work_schedule__isnull=False,
                    brigade_number__isnull=False,
                    watch_composition__isnull=True,
                ),
                fields=(
                    'scope_code',
                    'rating_period',
                    'work_schedule',
                    'brigade_number',
                    'shift_type',
                ),
                name='uniq_drv_rating_mat_ws_brig',
            ),
        ),
    ]
