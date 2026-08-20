from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shifts', '0013_unique_open_oup_period'),
        ('users', '0017_employee_sex'),
    ]

    operations = [
        migrations.CreateModel(
            name='WatchPeriodBrigadePhaseVersion',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('version_number', models.PositiveIntegerField(verbose_name='Номер версии')),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('draft', 'Черновик'),
                            ('confirmed', 'Утверждена'),
                            ('superseded', 'Заменена'),
                        ],
                        db_index=True,
                        default='draft',
                        max_length=16,
                        verbose_name='Статус',
                    ),
                ),
                (
                    'confirmed_at',
                    models.DateTimeField(blank=True, null=True, verbose_name='Утверждена'),
                ),
                (
                    'superseded_at',
                    models.DateTimeField(blank=True, null=True, verbose_name='Заменена'),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создана')),
                (
                    'source_snapshot',
                    models.JSONField(verbose_name='Снимок официального источника'),
                ),
                (
                    'source_fingerprint',
                    models.CharField(
                        max_length=64,
                        verbose_name='SHA-256 снимка официального источника',
                    ),
                ),
                (
                    'based_on_version',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='replacement_versions',
                        to='shifts.watchperiodbrigadephaseversion',
                        verbose_name='Основана на версии',
                    ),
                ),
                (
                    'watch_period',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='brigade_phase_versions',
                        to='shifts.watchperiod',
                        verbose_name='Период вахты',
                    ),
                ),
                (
                    'work_schedule',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='watch_period_phase_versions',
                        to='users.workschedule',
                        verbose_name='График работы',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Версия календаря фаз бригад',
                'verbose_name_plural': 'Версии календаря фаз бригад',
                'ordering': [
                    'watch_period_id',
                    'work_schedule_id',
                    '-version_number',
                    '-pk',
                ],
                'indexes': [
                    models.Index(
                        fields=['watch_period', 'work_schedule', 'status'],
                        name='watch_phase_period_status_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('watch_period', 'work_schedule', 'version_number'),
                        name='uniq_watch_phase_revision',
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(('status', 'confirmed')),
                        fields=('watch_period', 'work_schedule'),
                        name='uniq_watch_phase_confirmed',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(('version_number__gte', 1)),
                        name='watch_phase_version_gte_1',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(('status__in', ['draft', 'confirmed', 'superseded'])),
                        name='watch_phase_status_valid',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                ('confirmed_at__isnull', True),
                                ('status', 'draft'),
                                ('superseded_at__isnull', True),
                            )
                            | models.Q(
                                ('confirmed_at__isnull', False),
                                ('status', 'confirmed'),
                                ('superseded_at__isnull', True),
                            )
                            | models.Q(
                                ('confirmed_at__isnull', False),
                                ('status', 'superseded'),
                                ('superseded_at__isnull', False),
                            )
                        ),
                        name='watch_phase_status_dates',
                    ),
                    models.CheckConstraint(
                        condition=(
                            ~models.Q(('status', 'superseded'))
                            | models.Q(('superseded_at__gte', models.F('confirmed_at')))
                        ),
                        name='watch_phase_supersede_order',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(('based_on_version__isnull', True))
                            | ~models.Q(('pk', models.F('based_on_version_id')))
                        ),
                        name='watch_phase_not_self_based',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='WatchPeriodBrigadePhaseRow',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('brigade_number', models.PositiveSmallIntegerField(verbose_name='Номер бригады')),
                (
                    'phase',
                    models.CharField(
                        choices=[
                            ('day', 'Дневная смена'),
                            ('night', 'Ночная смена'),
                            ('off', 'Межвахта'),
                        ],
                        max_length=16,
                        verbose_name='Фаза бригады',
                    ),
                ),
                (
                    'version',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='rows',
                        to='shifts.watchperiodbrigadephaseversion',
                        verbose_name='Версия календаря фаз',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Строка календаря фаз бригад',
                'verbose_name_plural': 'Строки календаря фаз бригад',
                'ordering': ['version_id', 'brigade_number', 'pk'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('version', 'brigade_number'),
                        name='uniq_watch_phase_brigade',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(('brigade_number__gte', 1)),
                        name='watch_phase_brigade_gte_1',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(('phase__in', ['day', 'night', 'off'])),
                        name='watch_phase_value_valid',
                    ),
                ],
            },
        ),
    ]
