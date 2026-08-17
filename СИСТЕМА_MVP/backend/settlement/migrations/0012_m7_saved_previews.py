import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement', '0011_resident_subject_transition'),
        ('shifts', '0013_unique_open_oup_period'),
        ('users', '0017_employee_sex'),
    ]

    operations = [
        migrations.CreateModel(
            name='SettlementPreviewRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('stable_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='Стабильный идентификатор')),
                ('version', models.PositiveIntegerField(verbose_name='Версия preview')),
                ('status', models.CharField(choices=[('draft', 'Черновик'), ('confirmed', 'Подтверждён'), ('superseded', 'Заменён новым preview')], db_index=True, default='draft', max_length=16, verbose_name='Статус')),
                ('resolver_fingerprint', models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(regex='^[0-9a-f]{64}$')], verbose_name='SHA-256 входов resolver')),
                ('result_fingerprint', models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(regex='^[0-9a-f]{64}$')], verbose_name='SHA-256 нормализованного результата')),
                ('source_snapshot', models.JSONField(verbose_name='Неизменяемый снимок результата и источников')),
                ('revision', models.PositiveBigIntegerField(default=1, verbose_name='Ревизия preview')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создан')),
                ('confirmed_at', models.DateTimeField(blank=True, null=True, verbose_name='Подтверждён')),
                ('superseded_at', models.DateTimeField(blank=True, null=True, verbose_name='Заменён')),
                ('base_confirmed_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='drafts_based_on', to='settlement.settlementpreviewrun', verbose_name='Подтверждённый preview на момент расчёта')),
                ('cohort', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='saved_preview_runs', to='settlement.settlementcohort', verbose_name='Утверждённый состав заезда')),
                ('confirmed_by_access', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='confirmed_settlement_preview_runs', to='users.employeeaccess', verbose_name='Точный доступ подтвердившего')),
                ('created_by_access', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='created_settlement_preview_runs', to='users.employeeaccess', verbose_name='Точный доступ создателя')),
                ('supersedes', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='replacements', to='settlement.settlementpreviewrun', verbose_name='Заменяемый подтверждённый preview')),
                ('watch_composition', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='settlement_preview_runs', to='users.watchcomposition', verbose_name='Утверждённый состав вахты')),
                ('watch_period', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='settlement_preview_runs', to='shifts.watchperiod', verbose_name='Конкретный период вахты')),
            ],
            options={
                'verbose_name': 'Сохранённый preview расселения',
                'verbose_name_plural': 'Сохранённые preview расселения',
                'ordering': ['watch_period_id', 'version', 'pk'],
            },
        ),
        migrations.CreateModel(
            name='SettlementPreviewPlacement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('stable_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='Стабильный идентификатор')),
                ('action', models.CharField(max_length=32, verbose_name='Действие resolver')),
                ('source_kind', models.CharField(max_length=64, verbose_name='Волна resolver')),
                ('cohort_member_id_snapshot', models.PositiveBigIntegerField(verbose_name='PK membership snapshot')),
                ('physical_room_id_snapshot', models.PositiveBigIntegerField(verbose_name='PK комнаты snapshot')),
                ('binding_id_snapshot', models.PositiveBigIntegerField(blank=True, null=True)),
                ('equipment_assignment_id_snapshot', models.PositiveBigIntegerField(blank=True, null=True)),
                ('anchor_id_snapshot', models.PositiveBigIntegerField()),
                ('anchor_bed_assignment_id_snapshot', models.PositiveBigIntegerField()),
                ('normalized_provenance', models.JSONField(verbose_name='Неизменяемая нормализованная provenance')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создана')),
                ('calendar_slot', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='saved_preview_placements', to='settlement.accommodationanchorcalendarslot', verbose_name='Календарный слот')),
                ('physical_bed', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='saved_preview_placements', to='settlement.physicalbed', verbose_name='Физическое койко-место')),
                ('resident', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='saved_preview_placements', to='settlement.settlementresident', verbose_name='Жилец')),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='placements', to='settlement.settlementpreviewrun', verbose_name='Сохранённый preview')),
            ],
            options={
                'verbose_name': 'Строка размещения сохранённого preview',
                'verbose_name_plural': 'Строки размещения сохранённого preview',
                'ordering': ['run_id', 'resident_id', 'pk'],
            },
        ),
        migrations.CreateModel(
            name='SettlementPreviewUnresolved',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('stable_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='Стабильный идентификатор')),
                ('reason_code', models.CharField(max_length=64, verbose_name='Основной код причины')),
                ('reason_codes', models.JSONField(verbose_name='Все коды причин')),
                ('cohort_member_id_snapshot', models.PositiveBigIntegerField(verbose_name='PK membership snapshot')),
                ('structured_details', models.JSONField(verbose_name='Неизменяемые структурированные детали')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создана')),
                ('resident', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='saved_preview_unresolved_rows', to='settlement.settlementresident', verbose_name='Жилец')),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='unresolved_rows', to='settlement.settlementpreviewrun', verbose_name='Сохранённый preview')),
            ],
            options={
                'verbose_name': 'Нерасселённая строка сохранённого preview',
                'verbose_name_plural': 'Нерасселённые строки сохранённого preview',
                'ordering': ['run_id', 'resident_id', 'pk'],
            },
        ),
        migrations.AddIndex(
            model_name='settlementpreviewrun',
            index=models.Index(fields=['watch_period', 'status', 'version'], name='preview_period_status_ver_idx'),
        ),
        migrations.AddIndex(
            model_name='settlementpreviewrun',
            index=models.Index(fields=['cohort', 'status'], name='preview_cohort_status_idx'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewrun',
            constraint=models.UniqueConstraint(fields=('watch_period', 'version'), name='unique_preview_watch_period_version'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewrun',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'confirmed')), fields=('watch_period',), name='unique_confirmed_preview_per_watch'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewrun',
            constraint=models.CheckConstraint(condition=models.Q(('version__gte', 1)), name='preview_version_gte_1'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewrun',
            constraint=models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='preview_revision_gte_1'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewrun',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('confirmed_at__isnull', True), ('confirmed_by_access__isnull', True), ('status', 'draft'), ('superseded_at__isnull', True), ('supersedes__isnull', True)), models.Q(('confirmed_at__isnull', False), ('confirmed_by_access__isnull', False), ('status', 'confirmed'), ('superseded_at__isnull', True)), models.Q(('confirmed_at__isnull', False), ('confirmed_by_access__isnull', False), ('status', 'superseded'), ('superseded_at__isnull', False)), _connector='OR'), name='preview_lifecycle_metadata'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewrun',
            constraint=models.CheckConstraint(condition=models.Q(('base_confirmed_run__isnull', True), models.Q(('pk', models.F('base_confirmed_run_id')), _negated=True), _connector='OR'), name='preview_base_not_self'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewrun',
            constraint=models.CheckConstraint(condition=models.Q(('supersedes__isnull', True), models.Q(('pk', models.F('supersedes_id')), _negated=True), _connector='OR'), name='preview_supersedes_not_self'),
        ),
        migrations.AddIndex(
            model_name='settlementpreviewplacement',
            index=models.Index(fields=['run', 'source_kind'], name='preview_place_source_idx'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewplacement',
            constraint=models.UniqueConstraint(fields=('run', 'resident'), name='unique_preview_placement_resident'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewplacement',
            constraint=models.UniqueConstraint(fields=('run', 'calendar_slot'), name='unique_preview_placement_slot'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewplacement',
            constraint=models.UniqueConstraint(fields=('run', 'physical_bed'), name='unique_preview_placement_bed'),
        ),
        migrations.AddIndex(
            model_name='settlementpreviewunresolved',
            index=models.Index(fields=['run', 'reason_code'], name='preview_unresolved_reason_idx'),
        ),
        migrations.AddConstraint(
            model_name='settlementpreviewunresolved',
            constraint=models.UniqueConstraint(fields=('run', 'resident'), name='unique_preview_unresolved_resident'),
        ),
    ]
