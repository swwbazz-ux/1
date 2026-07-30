import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0008_ratingperiod_nominal_starts_on'),
        ('users', '0016_watchcomposition_employee_watch_composition'),
    ]

    operations = [
        migrations.CreateModel(
            name='DriverRatingPeriodMaterializedSnapshot',
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
                (
                    'scope_code',
                    models.CharField(
                        max_length=64,
                        verbose_name='Техническая область рейтинга',
                    ),
                ),
                (
                    'shift_type',
                    models.CharField(
                        choices=[
                            ('day', 'Дневная'),
                            ('night', 'Ночная'),
                        ],
                        max_length=16,
                        verbose_name='Тип смены',
                    ),
                ),
                (
                    'formula_version',
                    models.CharField(
                        max_length=96,
                        verbose_name='Версия формулы',
                    ),
                ),
                (
                    'payload_schema_version',
                    models.PositiveSmallIntegerField(
                        default=1,
                        verbose_name='Версия схемы готового снимка',
                    ),
                ),
                (
                    'scope_fingerprint',
                    models.CharField(
                        max_length=64,
                        verbose_name='Fingerprint области расчёта',
                    ),
                ),
                (
                    'source_fingerprint',
                    models.CharField(
                        blank=True,
                        max_length=64,
                        verbose_name='Fingerprint источников',
                    ),
                ),
                (
                    'shift_score_fingerprint',
                    models.CharField(
                        blank=True,
                        max_length=64,
                        verbose_name='Fingerprint сменных баллов',
                    ),
                ),
                (
                    'payload_fingerprint',
                    models.CharField(
                        blank=True,
                        max_length=64,
                        verbose_name='Fingerprint готового снимка',
                    ),
                ),
                (
                    'member_fingerprint',
                    models.CharField(
                        blank=True,
                        max_length=64,
                        verbose_name='Fingerprint состава готового снимка',
                    ),
                ),
                (
                    'payload',
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        verbose_name='Готовый серверный снимок',
                    ),
                ),
                (
                    'member_employee_ids',
                    models.JSONField(
                        blank=True,
                        default=list,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        verbose_name='Сотрудники, представленные в группе',
                    ),
                ),
                (
                    'member_latest_closed_at',
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        verbose_name=(
                            'Последнее закрытие смены сотрудника в группе'
                        ),
                    ),
                ),
                (
                    'revision',
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name='Ревизия',
                    ),
                ),
                (
                    'published_at',
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name='Содержимое опубликовано',
                    ),
                ),
                (
                    'last_success_at',
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name='Последняя успешная проверка',
                    ),
                ),
                (
                    'last_attempt_at',
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name='Последняя попытка обновления',
                    ),
                ),
                (
                    'last_failure_at',
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name='Последняя ошибка обновления',
                    ),
                ),
                (
                    'last_refresh_status',
                    models.CharField(
                        choices=[
                            ('ready', 'Готов'),
                            (
                                'failed',
                                (
                                    'Последнее обновление завершилось '
                                    'ошибкой'
                                ),
                            ),
                        ],
                        default='failed',
                        max_length=16,
                        verbose_name='Состояние последнего обновления',
                    ),
                ),
                (
                    'failure_code',
                    models.CharField(
                        blank=True,
                        max_length=64,
                        verbose_name='Код последней ошибки',
                    ),
                ),
                (
                    'consecutive_failure_count',
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name='Ошибок обновления подряд',
                    ),
                ),
                (
                    'last_error',
                    models.CharField(
                        blank=True,
                        max_length=500,
                        verbose_name='Сокращённая внутренняя ошибка',
                    ),
                ),
                (
                    'created_at',
                    models.DateTimeField(
                        auto_now_add=True,
                        verbose_name='Создан',
                    ),
                ),
                (
                    'updated_at',
                    models.DateTimeField(
                        auto_now=True,
                        verbose_name='Изменён',
                    ),
                ),
                (
                    'rating_period',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='driver_rating_materialized_snapshots',
                        to='reports.ratingperiod',
                        verbose_name='Период рейтинга',
                    ),
                ),
                (
                    'watch_composition',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='driver_rating_materialized_snapshots',
                        to='users.watchcomposition',
                        verbose_name='Состав вахты',
                    ),
                ),
            ],
            options={
                'verbose_name': (
                    'Текущий серверный снимок рейтинга водителей'
                ),
                'verbose_name_plural': (
                    'Текущие серверные снимки рейтинга водителей'
                ),
                'ordering': [
                    '-rating_period__starts_on',
                    'watch_composition_id',
                    'shift_type',
                ],
                'indexes': [
                    models.Index(
                        fields=[
                            'scope_code',
                            'rating_period',
                            'formula_version',
                        ],
                        name='drv_rating_mat_scope_period_ix',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=(
                            'scope_code',
                            'rating_period',
                            'watch_composition',
                            'shift_type',
                        ),
                        name='uniq_drv_rating_mat_group',
                    ),
                ],
            },
        ),
    ]
