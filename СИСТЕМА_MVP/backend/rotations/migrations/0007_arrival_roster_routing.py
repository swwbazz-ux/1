import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('assignments', '0007_equipment_assignment_provenance'),
        ('rotations', '0006_arrival_roster_excel_revision'),
        ('settlement', '0017_m9_preview_corrections'),
        ('shifts', '0013_unique_open_oup_period'),
        ('users', '0017_employee_sex'),
    ]

    operations = [
        migrations.CreateModel(
            name='ArrivalRosterRoutingBatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('confirmation_sha256', models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(regex=r'^[0-9a-f]{64}$')], verbose_name='SHA-256 утверждённого реестра')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Передача создана')),
                ('arrival_roster_version', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='routing_batch', to='rotations.arrivalrosterversion', verbose_name='Точная утверждённая версия реестра')),
                ('created_by_access', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='created_arrival_roster_routing_batches', to='users.employeeaccess', verbose_name='Точный доступ табельщика, создавшего передачу')),
                ('watch_period', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='arrival_roster_routing_batches', to='shifts.watchperiod', verbose_name='Период вахты')),
            ],
            options={
                'verbose_name': 'Передача утверждённого реестра заезда',
                'verbose_name_plural': 'Передачи утверждённых реестров заезда',
                'ordering': ['watch_period_id', 'pk'],
            },
        ),
        migrations.CreateModel(
            name='ArrivalRosterRoutingRow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('participation_snapshot', models.JSONField(verbose_name='Снимок участия в заезде')),
                ('dates_snapshot', models.JSONField(verbose_name='Снимок дат заезда')),
                ('role_snapshot', models.JSONField(verbose_name='Снимок роли, установленной ОУП')),
                ('role_basis_snapshot', models.JSONField(verbose_name='Снимок основания определения роли')),
                ('route_state', models.CharField(choices=[('to_deputy', 'Заместителю начальника участка'), ('to_clerk', 'Напрямую делопроизводителю'), ('not_participating', 'Не участвует в заезде'), ('review_required', 'Требуется проверка')], db_index=True, max_length=24, verbose_name='Маршрутное состояние')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Строка передачи создана')),
                ('batch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rows', to='rotations.arrivalrosterroutingbatch', verbose_name='Передача реестра')),
                ('employee', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='arrival_roster_routing_rows', to='users.employee', verbose_name='Внутренний сотрудник')),
                ('match', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='routing_rows', to='rotations.arrivalrostermatch', verbose_name='Точное сопоставление строки')),
                ('resident', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='arrival_roster_routing_rows', to='settlement.settlementresident', verbose_name='Точный жилец')),
                ('row_review', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='routing_rows', to='rotations.arrivalrosterrowreview', verbose_name='Точная ручная проверка строки')),
            ],
            options={
                'verbose_name': 'Маршрутизированная строка реестра заезда',
                'verbose_name_plural': 'Маршрутизированные строки реестра заезда',
                'ordering': ['batch_id', 'row_review_id', 'pk'],
            },
        ),
        migrations.CreateModel(
            name='ArrivalRosterRoutingEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(choices=[('created', 'Передача создана'), ('sent_to_deputy', 'Направлена заместителю'), ('sent_to_clerk', 'Направлена делопроизводителю'), ('official_assignment_published', 'Официальное назначение опубликовано'), ('requires_review', 'Требуется проверка'), ('stale', 'Передача устарела')], max_length=32, verbose_name='Тип события')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Время события')),
                ('actor_access', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='arrival_roster_routing_events', to='users.employeeaccess', verbose_name='Точный доступ исполнителя')),
                ('crew_plan_slot', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='arrival_roster_routing_events', to='assignments.crewplanslot', verbose_name='Точный слот опубликованного плана')),
                ('equipment_assignment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='arrival_roster_routing_events', to='assignments.equipmentassignment', verbose_name='Точное официальное назначение техники')),
                ('routing_row', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='rotations.arrivalrosterroutingrow', verbose_name='Маршрутизированная строка')),
            ],
            options={
                'verbose_name': 'Событие маршрутизации реестра заезда',
                'verbose_name_plural': 'События маршрутизации реестра заезда',
                'ordering': ['routing_row_id', 'created_at', 'pk'],
            },
        ),
        migrations.AddIndex(
            model_name='arrivalrosterroutingbatch',
            index=models.Index(fields=['watch_period', 'created_at'], name='arrival_route_batch_period_idx'),
        ),
        migrations.AddIndex(
            model_name='arrivalrosterroutingrow',
            index=models.Index(fields=['batch', 'route_state'], name='arrival_route_row_state_idx'),
        ),
        migrations.AddConstraint(
            model_name='arrivalrosterroutingrow',
            constraint=models.UniqueConstraint(fields=('batch', 'row_review'), name='uniq_arrival_route_batch_review'),
        ),
        migrations.AddIndex(
            model_name='arrivalrosterroutingevent',
            index=models.Index(fields=['routing_row', 'created_at'], name='arrival_route_event_row_idx'),
        ),
        migrations.AddConstraint(
            model_name='arrivalrosterroutingevent',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('crew_plan_slot__isnull', False), ('equipment_assignment__isnull', False), ('event_type', 'official_assignment_published')), models.Q(('crew_plan_slot__isnull', True), ('equipment_assignment__isnull', True), ('event_type__in', ['created', 'sent_to_deputy', 'sent_to_clerk', 'requires_review', 'stale'])), _connector='OR'), name='arrival_route_event_assignment_shape'),
        ),
    ]
