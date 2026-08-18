import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


def mark_existing_rows_unverified(apps, schema_editor):
    using = schema_editor.connection.alias
    Application = apps.get_model('settlement', 'SettlementPreviewApplication')
    Occupancy = apps.get_model('settlement', 'EmployeeBedOccupancy')
    Application.objects.using(using).update(
        legacy_whole_run=True,
        work_shift=None,
    )
    Occupancy.objects.using(using).update(
        work_shift=None,
        shift_source_kind='unverified_legacy',
        shift_source_fingerprint='',
        shift_official_assignment=None,
        shift_selected_by_access=None,
        shift_selected_at=None,
        shift_selection_basis='',
    )


def reverse_fail_closed(apps, schema_editor):
    using = schema_editor.connection.alias
    Application = apps.get_model('settlement', 'SettlementPreviewApplication')
    Occupancy = apps.get_model('settlement', 'EmployeeBedOccupancy')
    shift_applications = Application.objects.using(using).filter(
        models.Q(legacy_whole_run=False) | models.Q(work_shift__isnull=False)
    ).order_by('pk')
    classified_occupancies = Occupancy.objects.using(using).filter(
        models.Q(work_shift__isnull=False)
        | ~models.Q(shift_source_kind='unverified_legacy')
    ).order_by('pk')
    application_count = shift_applications.count()
    occupancy_count = classified_occupancies.count()
    if application_count or occupancy_count:
        raise RuntimeError(
            'settlement.0016 reverse would destroy shift-scoped history: '
            f'applications={application_count}, '
            f'application_pks={list(shift_applications.values_list("pk", flat=True)[:20])}, '
            f'occupancies={occupancy_count}, '
            f'occupancy_pks={list(classified_occupancies.values_list("pk", flat=True)[:20])}.'
        )


class Migration(migrations.Migration):

    dependencies = [
        ('settlement', '0015_cohort_member_work_shift'),
    ]

    operations = [
        migrations.AlterField(
            model_name='settlementpreviewapplication',
            name='preview_run',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='applications',
                to='settlement.settlementpreviewrun',
                verbose_name='Применённый preview',
            ),
        ),
        migrations.AddField(
            model_name='settlementpreviewapplication',
            name='work_shift',
            field=models.CharField(
                blank=True,
                choices=[('day', 'Дневная смена'), ('night', 'Ночная смена')],
                max_length=16,
                null=True,
                verbose_name='Применённая смена',
            ),
        ),
        migrations.AddField(
            model_name='settlementpreviewapplication',
            name='legacy_whole_run',
            field=models.BooleanField(default=False, verbose_name='Историческое применение всего плана'),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='work_shift',
            field=models.CharField(
                blank=True,
                choices=[('day', 'Дневная смена'), ('night', 'Ночная смена')],
                max_length=16,
                null=True,
                verbose_name='Смена фактического проживания',
            ),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='shift_source_kind',
            field=models.CharField(
                choices=[
                    ('unverified_legacy', 'Непроверенное историческое'),
                    ('auto_preview', 'Подтверждённый preview'),
                    ('official_assignment', 'Официальное назначение'),
                    ('clerk_selected', 'Выбор делопроизводителя'),
                ],
                db_index=True,
                default='unverified_legacy',
                max_length=32,
                verbose_name='Источник смены',
            ),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='shift_source_fingerprint',
            field=models.CharField(
                blank=True,
                default='',
                max_length=64,
                validators=[django.core.validators.RegexValidator(regex='^$|^[0-9a-f]{64}$')],
                verbose_name='SHA-256 источника смены',
            ),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='shift_official_assignment',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='settlement_shift_occupancies',
                to='assignments.equipmentassignment',
                verbose_name='Официальное назначение смены',
            ),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='shift_selected_by_access',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='selected_settlement_shift_occupancies',
                to='users.employeeaccess',
                verbose_name='Точный доступ выбравшего смену',
            ),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='shift_selected_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Когда выбрана смена'),
        ),
        migrations.AddField(
            model_name='employeebedoccupancy',
            name='shift_selection_basis',
            field=models.TextField(blank=True, default='', verbose_name='Основание выбора смены'),
        ),
        migrations.RunPython(mark_existing_rows_unverified, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='settlementpreviewapplication',
            constraint=models.UniqueConstraint(
                fields=('preview_run', 'work_shift'),
                name='unique_preview_apply_run_shift',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewapplication',
            constraint=models.UniqueConstraint(
                condition=models.Q(('legacy_whole_run', True)),
                fields=('preview_run',),
                name='unique_legacy_apply_per_run',
            ),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewapplication',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(('legacy_whole_run', True), ('work_shift__isnull', True)),
                    models.Q(
                        ('legacy_whole_run', False),
                        ('work_shift__in', ['day', 'night']),
                    ),
                    _connector='OR',
                ),
                name='preview_apply_shift_scope_valid',
            ),
        ),
        migrations.AddConstraint(
            model_name='employeebedoccupancy',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ('shift_official_assignment__isnull', True),
                        ('shift_selected_at__isnull', True),
                        ('shift_selected_by_access__isnull', True),
                        ('shift_selection_basis', ''),
                        ('shift_source_fingerprint', ''),
                        ('shift_source_kind', 'unverified_legacy'),
                        ('work_shift__isnull', True),
                    ),
                    models.Q(
                        ('shift_official_assignment__isnull', True),
                        ('shift_selected_at__isnull', True),
                        ('shift_selected_by_access__isnull', True),
                        ('shift_selection_basis', ''),
                        ('shift_source_fingerprint__gt', ''),
                        ('shift_source_kind', 'auto_preview'),
                        ('source_kind', 'auto'),
                        ('work_shift__in', ['day', 'night']),
                    ),
                    models.Q(
                        ('shift_official_assignment__isnull', False),
                        ('shift_selected_at__isnull', True),
                        ('shift_selected_by_access__isnull', True),
                        ('shift_selection_basis', ''),
                        ('shift_source_fingerprint__gt', ''),
                        ('shift_source_kind', 'official_assignment'),
                        ('source_kind', 'manual'),
                        ('work_shift__in', ['day', 'night']),
                    ),
                    models.Q(
                        ('shift_official_assignment__isnull', True),
                        ('shift_selected_at__isnull', False),
                        ('shift_selected_by_access__isnull', False),
                        ('shift_selection_basis__gt', ''),
                        ('shift_source_fingerprint__gt', ''),
                        ('shift_source_kind', 'clerk_selected'),
                        ('source_kind', 'manual'),
                        ('work_shift__in', ['day', 'night']),
                    ),
                    _connector='OR',
                ),
                name='occupancy_shift_provenance_valid',
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, reverse_fail_closed),
    ]
