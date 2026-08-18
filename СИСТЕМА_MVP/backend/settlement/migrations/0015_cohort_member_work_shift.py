import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assignments', '0007_equipment_assignment_provenance'),
        ('settlement', '0014_m8_apply_provenance'),
    ]

    operations = [
        migrations.AddField(
            model_name='settlementcohortmember',
            name='official_equipment_assignment',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='settlement_cohort_members', to='assignments.equipmentassignment', verbose_name='Официальное назначение-источник'),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='shift_selected_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Когда выбрана смена внешнего жильца'),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='shift_selected_by_access',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='selected_external_settlement_shifts', to='users.employeeaccess', verbose_name='Точный доступ, выбравший смену'),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='shift_selection_basis',
            field=models.CharField(blank=True, max_length=255, verbose_name='Основание выбора смены внешнего жильца'),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='shift_source_fingerprint',
            field=models.CharField(blank=True, default='', max_length=64, validators=[django.core.validators.RegexValidator(regex='^$|^[0-9a-f]{64}$')], verbose_name='SHA-256 источника смены'),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='shift_source_kind',
            field=models.CharField(choices=[('internal_assignment', 'Официальное назначение сотрудника'), ('external_clerk', 'Выбор делопроизводителя для внешнего жильца'), ('unverified_legacy', 'Непроверенная историческая строка')], db_index=True, default='unverified_legacy', max_length=32, verbose_name='Источник официальной смены'),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='shift_source_snapshot',
            field=models.JSONField(blank=True, default=dict, verbose_name='Неизменяемый снимок источника смены'),
        ),
        migrations.AddField(
            model_name='settlementcohortmember',
            name='work_shift',
            field=models.CharField(blank=True, choices=[('day', 'Дневная смена'), ('night', 'Ночная смена')], default='', max_length=16, verbose_name='Официальная рабочая смена'),
        ),
        migrations.AddField(
            model_name='settlementpreviewplacement',
            name='work_shift',
            field=models.CharField(blank=True, choices=[('day', 'Дневная смена'), ('night', 'Ночная смена')], default='', max_length=16, verbose_name='Официальная смена'),
        ),
        migrations.AddField(
            model_name='settlementpreviewrun',
            name='requires_shift_split',
            field=models.BooleanField(default=False, verbose_name='Требует раздельного применения смен'),
        ),
        migrations.AddField(
            model_name='settlementpreviewunresolved',
            name='work_shift',
            field=models.CharField(blank=True, choices=[('day', 'Дневная смена'), ('night', 'Ночная смена')], default='', max_length=16, verbose_name='Официальная смена'),
        ),
        migrations.AddIndex(
            model_name='settlementcohortmember',
            index=models.Index(fields=['cohort', 'work_shift', 'shift_source_kind'], name='cohort_member_shift_idx'),
        ),
        migrations.AddConstraint(
            model_name='settlementcohortmember',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('official_equipment_assignment__isnull', False), ('shift_selected_at__isnull', True), ('shift_selected_by_access__isnull', True), ('shift_selection_basis', ''), ('shift_source_kind', 'internal_assignment'), ('work_shift__in', ['day', 'night'])), models.Q(('official_equipment_assignment__isnull', True), ('shift_selected_at__isnull', False), ('shift_selected_by_access__isnull', False), ('shift_source_kind', 'external_clerk'), ('work_shift__in', ['day', 'night'])), models.Q(('official_equipment_assignment__isnull', True), ('shift_selected_at__isnull', True), ('shift_selected_by_access__isnull', True), ('shift_selection_basis', ''), ('shift_source_kind', 'unverified_legacy'), ('work_shift', '')), _connector='OR'), name='cohort_member_shift_source_ck'),
        ),
    ]
