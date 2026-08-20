import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement', '0018_arrival_roster_cohort_provenance'),
        ('rotations', '0008_employee_watch_profile_change'),
    ]

    operations = [
        migrations.AddField(
            model_name='settlementcohortmember',
            name='employee_watch_profile_change',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_members_by_watch_profile_change',
                to='rotations.employeewatchprofilechange',
                verbose_name='Точное применённое изменение профиля сотрудника',
            ),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='watch_profile_brigade_number',
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                verbose_name='Разрешённый номер бригады сотрудника',
            ),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='watch_profile_fingerprint',
            field=models.CharField(
                blank=True,
                default='',
                max_length=64,
                validators=[
                    django.core.validators.RegexValidator(
                        regex=r'^$|^[0-9a-f]{64}$',
                    ),
                ],
                verbose_name='SHA-256 разрешённого профиля сотрудника',
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='watch_profile_source_kind',
            field=models.CharField(
                choices=[
                    ('unverified_legacy', 'Непроверенный исторический профиль'),
                    ('legacy_baseline', 'Структурированный профиль Employee'),
                    ('applied_change', 'Применённое изменение профиля'),
                ],
                db_index=True,
                default='unverified_legacy',
                max_length=32,
                verbose_name='Источник структурированного профиля сотрудника',
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='watch_profile_watch_composition',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_members_by_watch_profile_composition',
                to='users.watchcomposition',
                verbose_name='Разрешённый состав вахты сотрудника',
            ),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='watch_profile_work_schedule',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_members_by_watch_profile_schedule',
                to='users.workschedule',
                verbose_name='Разрешённый график работы сотрудника',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementcohortmember',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    watch_profile_source_kind__in=[
                        'unverified_legacy', 'legacy_baseline', 'applied_change',
                    ],
                ),
                name='cohort_member_watch_profile_kind_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementcohortmember',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        watch_profile_source_kind='unverified_legacy',
                        employee_watch_profile_change__isnull=True,
                        watch_profile_work_schedule__isnull=True,
                        watch_profile_brigade_number__isnull=True,
                        watch_profile_watch_composition__isnull=True,
                        watch_profile_fingerprint='',
                    )
                    | models.Q(
                        watch_profile_source_kind='legacy_baseline',
                        employee_watch_profile_change__isnull=True,
                        watch_profile_fingerprint__regex=r'^[0-9a-f]{64}$',
                    )
                    | models.Q(
                        watch_profile_source_kind='applied_change',
                        employee_watch_profile_change__isnull=False,
                        watch_profile_work_schedule__isnull=False,
                        watch_profile_watch_composition__isnull=False,
                        watch_profile_fingerprint__regex=r'^[0-9a-f]{64}$',
                    )
                ),
                name='cohort_member_watch_profile_shape_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementcohortmember',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(watch_profile_brigade_number__isnull=True)
                    | models.Q(watch_profile_brigade_number__gte=1)
                ),
                name='cohort_member_watch_profile_brigade_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementcohortmember',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(watch_profile_work_schedule__isnull=False)
                    | models.Q(watch_profile_brigade_number__isnull=True)
                ),
                name='cohort_member_watch_profile_schedule_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementcohortmember',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(source_revision__isnull=True)
                    | models.Q(watch_profile_source_kind='unverified_legacy')
                ),
                name='cohort_member_watch_profile_family_ck',
            ),
        ),
    ]
