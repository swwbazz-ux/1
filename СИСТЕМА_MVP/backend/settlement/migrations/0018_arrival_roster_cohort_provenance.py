import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement', '0017_m9_preview_corrections'),
        ('rotations', '0007_arrival_roster_routing'),
        ('shifts', '0015_brigade_phase_actor_accesses'),
    ]

    operations = [
        migrations.AlterField(
            model_name='settlementcohort',
            name='source_revision',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohorts',
                to='settlement.settlementrevision',
                verbose_name='Ревизия-основание',
            ),
        ),
        migrations.AddField(
            model_name='settlementcohort',
            name='routing_batch',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort',
                to='rotations.arrivalrosterroutingbatch',
                verbose_name='Точная передача реестра заезда',
            ),
        ),
        migrations.AlterField(
            model_name='settlementcohortmember',
            name='source_revision',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_members',
                to='settlement.settlementrevision',
                verbose_name='Ревизия-основание строки',
            ),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='routing_row',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_member',
                to='rotations.arrivalrosterroutingrow',
                verbose_name='Точная строка передачи реестра',
            ),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='routing_event',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_member',
                to='rotations.arrivalrosterroutingevent',
                verbose_name='Точное событие готовности строки',
            ),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='brigade_phase_row',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_cohort_members',
                to='shifts.watchperiodbrigadephaserow',
                verbose_name='Точная подтверждённая фаза бригады',
            ),
        ),
        migrations.AlterField(
            model_name='settlementcohortmember',
            name='shift_source_kind',
            field=models.CharField(
                choices=[
                    ('internal_assignment', 'Официальное назначение сотрудника'),
                    ('confirmed_brigade_phase', 'Подтверждённая фаза бригады'),
                    ('external_clerk', 'Выбор делопроизводителя для внешнего жильца'),
                    ('unverified_legacy', 'Непроверенная историческая строка'),
                ],
                db_index=True,
                default='unverified_legacy',
                max_length=32,
                verbose_name='Источник официальной смены',
            ),
        ),
        migrations.RemoveConstraint(
            model_name='settlementcohortmember',
            name='cohort_member_shift_source_ck',
        ),
        migrations.AddConstraint(
            model_name='settlementcohort',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(source_revision__isnull=False, routing_batch__isnull=True)
                    | models.Q(source_revision__isnull=True, routing_batch__isnull=False)
                ),
                name='cohort_source_family_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementcohortmember',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        shift_source_kind='internal_assignment',
                        work_shift__in=['day', 'night'],
                        official_equipment_assignment__isnull=False,
                        shift_selected_by_access__isnull=True,
                        shift_selected_at__isnull=True,
                        shift_selection_basis='',
                    )
                    | models.Q(
                        shift_source_kind='confirmed_brigade_phase',
                        work_shift__in=['day', 'night'],
                        official_equipment_assignment__isnull=True,
                        shift_selected_by_access__isnull=True,
                        shift_selected_at__isnull=True,
                        shift_selection_basis='',
                    )
                    | models.Q(
                        shift_source_kind='external_clerk',
                        work_shift__in=['day', 'night'],
                        official_equipment_assignment__isnull=True,
                        shift_selected_by_access__isnull=False,
                        shift_selected_at__isnull=False,
                    )
                    | models.Q(
                        shift_source_kind='unverified_legacy',
                        work_shift='',
                        official_equipment_assignment__isnull=True,
                        shift_selected_by_access__isnull=True,
                        shift_selected_at__isnull=True,
                        shift_selection_basis='',
                    )
                ),
                name='cohort_member_shift_source_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementcohortmember',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        source_revision__isnull=False,
                        routing_row__isnull=True,
                        routing_event__isnull=True,
                        brigade_phase_row__isnull=True,
                        shift_source_kind__in=[
                            'internal_assignment', 'external_clerk',
                            'unverified_legacy',
                        ],
                    )
                    | models.Q(
                        source_revision__isnull=True,
                        routing_row__isnull=False,
                        routing_event__isnull=False,
                        brigade_phase_row__isnull=False,
                        shift_source_kind__in=[
                            'internal_assignment', 'confirmed_brigade_phase',
                        ],
                    )
                ),
                name='cohort_member_source_family_ck',
            ),
        ),
    ]
