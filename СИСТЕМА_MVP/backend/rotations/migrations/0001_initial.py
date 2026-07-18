# Generated manually for the rotations domain initial schema.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('shifts', '0011_employeeshift_workplace_code'),
        ('users', '0014_allow_work_schedules_without_brigades'),
    ]

    operations = [
        migrations.CreateModel(
            name='RotationCollectionCycle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=160, verbose_name='Название сбора')),
                ('response_deadline', models.DateTimeField(verbose_name='Срок предоставления ответа')),
                ('status', models.CharField(choices=[('draft', 'Черновик'), ('open', 'Сбор открыт'), ('closed', 'Сбор закрыт'), ('archived', 'В архиве')], db_index=True, default='draft', max_length=16, verbose_name='Статус')),
                ('revision', models.PositiveIntegerField(default=1, verbose_name='Ревизия')),
                ('opened_at', models.DateTimeField(blank=True, null=True, verbose_name='Сбор открыт')),
                ('closed_at', models.DateTimeField(blank=True, null=True, verbose_name='Сбор закрыт')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создан')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Изменён')),
                ('closed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='closed_rotation_cycles', to='users.employee', verbose_name='Кто закрыл сбор')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_rotation_cycles', to='users.employee', verbose_name='Кто создал')),
                ('opened_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='opened_rotation_cycles', to='users.employee', verbose_name='Кто открыл сбор')),
                ('target_watch_period', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='rotation_collection_cycles', to='shifts.watchperiod', verbose_name='Целевая вахта')),
            ],
            options={
                'verbose_name': 'Цикл сбора по перевахте',
                'verbose_name_plural': 'Циклы сбора по перевахте',
                'ordering': ['-created_at', '-id'],
                'indexes': [
                    models.Index(fields=['target_watch_period', 'status'], name='rot_cycle_watch_status_idx'),
                    models.Index(fields=['status', 'response_deadline'], name='rot_cycle_status_due_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(condition=models.Q(('status', 'open')), fields=('target_watch_period',), name='uniq_open_rotation_cycle_watch'),
                    models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='rot_cycle_revision_gte_1'),
                    models.CheckConstraint(condition=models.Q(models.Q(('closed_at__isnull', True), ('closed_by__isnull', True), ('opened_at__isnull', True), ('opened_by__isnull', True), ('status', 'draft')), models.Q(('closed_at__isnull', True), ('closed_by__isnull', True), ('opened_at__isnull', False), ('opened_by__isnull', False), ('status', 'open')), models.Q(('closed_at__isnull', False), ('closed_by__isnull', False), ('opened_at__isnull', False), ('opened_by__isnull', False), ('status__in', ['closed', 'archived'])), _connector='OR'), name='rot_cycle_lifecycle_valid'),
                ],
            },
        ),
        migrations.CreateModel(
            name='RotationResponse',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('snapshot_full_name', models.CharField(max_length=255, verbose_name='ФИО snapshot')),
                ('snapshot_personnel_number', models.CharField(blank=True, max_length=64, verbose_name='Табельный номер snapshot')),
                ('snapshot_position', models.CharField(blank=True, max_length=255, verbose_name='Должность snapshot')),
                ('snapshot_department', models.CharField(blank=True, max_length=255, verbose_name='Подразделение snapshot')),
                ('snapshot_work_schedule', models.CharField(blank=True, max_length=255, verbose_name='График работы snapshot')),
                ('snapshot_brigade_number', models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='Бригада snapshot')),
                ('state', models.CharField(choices=[('pending', 'Ожидается ответ'), ('submitted', 'Ответ предоставлен')], db_index=True, default='pending', max_length=16, verbose_name='Состояние ответа')),
                ('intent', models.CharField(blank=True, choices=[('arrival', 'Заезд на вахту'), ('departure', 'Выезд с вахты'), ('not_travelling', 'Поездка не требуется'), ('extension', 'Запрос на продление вахты')], db_index=True, default='', max_length=24, verbose_name='Намерение сотрудника')),
                ('next_shift_type', models.CharField(blank=True, choices=[('day', 'Дневная'), ('night', 'Ночная')], default='', max_length=16, verbose_name='Смена следующей вахты')),
                ('shift_source', models.CharField(choices=[('unknown', 'Источник не определён'), ('active_assignment', 'Действующая расстановка'), ('employee', 'Указано сотрудником'), ('timekeeper', 'Указано табельщиком')], default='unknown', max_length=24, verbose_name='Источник смены')),
                ('departure_on', models.DateField(blank=True, null=True, verbose_name='Дата выезда')),
                ('arrival_on', models.DateField(blank=True, null=True, verbose_name='Дата заезда')),
                ('route_text', models.TextField(blank=True, verbose_name='Маршрут')),
                ('travel_mode', models.CharField(blank=True, choices=[('air', 'Самолёт'), ('rail', 'Поезд'), ('bus', 'Автобус'), ('car', 'Автомобиль'), ('other', 'Другое')], default='', max_length=16, verbose_name='Вид транспорта')),
                ('transfer_mode', models.CharField(blank=True, choices=[('organized', 'Организованный трансфер'), ('self', 'Самостоятельно')], default='', max_length=16, verbose_name='Способ трансфера')),
                ('transport_details', models.TextField(blank=True, verbose_name='Детали транспорта')),
                ('comment', models.TextField(blank=True, verbose_name='Комментарий сотрудника')),
                ('submitted_at', models.DateTimeField(blank=True, null=True, verbose_name='Ответ предоставлен')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создан')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Изменён')),
                ('cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='responses', to='rotations.rotationcollectioncycle', verbose_name='Цикл сбора')),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='rotation_responses', to='users.employee', verbose_name='Сотрудник')),
                ('submitted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='submitted_rotation_responses', to='users.employee', verbose_name='Кто предоставил ответ')),
            ],
            options={
                'verbose_name': 'Ответ по перевахте',
                'verbose_name_plural': 'Ответы по перевахте',
                'ordering': ['cycle', 'snapshot_full_name', 'id'],
                'indexes': [
                    models.Index(fields=['cycle', 'state'], name='rot_resp_cycle_state_idx'),
                    models.Index(fields=['cycle', 'intent'], name='rot_resp_cycle_intent_idx'),
                    models.Index(fields=['employee', 'created_at'], name='rot_resp_employee_date_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('cycle', 'employee'), name='uniq_rotation_cycle_employee'),
                    models.CheckConstraint(condition=models.Q(('snapshot_brigade_number__isnull', True), models.Q(('snapshot_brigade_number__gte', 1), ('snapshot_brigade_number__lte', 4)), _connector='OR'), name='rot_resp_brigade_1_4'),
                    models.CheckConstraint(condition=models.Q(models.Q(('intent', ''), ('state', 'pending'), ('submitted_at__isnull', True), ('submitted_by__isnull', True)), models.Q(('intent__in', ['arrival', 'departure', 'not_travelling', 'extension']), ('state', 'submitted'), ('submitted_at__isnull', False), ('submitted_by__isnull', False)), _connector='OR'), name='rot_resp_submission_valid'),
                    models.CheckConstraint(condition=models.Q(('next_shift_type__in', ['', 'day', 'night'])), name='rot_resp_next_shift_valid'),
                    models.CheckConstraint(condition=models.Q(('shift_source__in', ['unknown', 'active_assignment', 'employee', 'timekeeper'])), name='rot_resp_shift_source_valid'),
                    models.CheckConstraint(condition=models.Q(('travel_mode__in', ['', 'air', 'rail', 'bus', 'car', 'other'])), name='rot_resp_travel_mode_valid'),
                    models.CheckConstraint(condition=models.Q(('transfer_mode__in', ['', 'organized', 'self'])), name='rot_resp_transfer_mode_valid'),
                ],
            },
        ),
        migrations.CreateModel(
            name='WatchExtensionCase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('extension_start', models.DateField(verbose_name='Начало продления')),
                ('extension_end', models.DateField(verbose_name='Окончание продления')),
                ('decision_status', models.CharField(choices=[('pending', 'Ожидает решения'), ('approved', 'Одобрено'), ('rejected', 'Отклонено')], db_index=True, default='pending', max_length=16, verbose_name='Решение начальника участка')),
                ('decision_at', models.DateTimeField(blank=True, null=True, verbose_name='Решение принято')),
                ('decision_comment', models.TextField(blank=True, verbose_name='Комментарий начальника участка')),
                ('documentation_status', models.CharField(choices=[('not_started', 'Не начато'), ('data_ready', 'Данные подготовлены'), ('completed', 'Оформление завершено')], db_index=True, default='not_started', max_length=16, verbose_name='Статус документального оформления')),
                ('documentation_at', models.DateTimeField(blank=True, null=True, verbose_name='Документы оформлены')),
                ('documentation_note', models.TextField(blank=True, verbose_name='Примечание по оформлению')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Изменено')),
                ('decision_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_watch_extension_cases', to='users.employee', verbose_name='Кто принял решение')),
                ('documentation_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='documented_watch_extension_cases', to='users.employee', verbose_name='Кто оформил документы')),
                ('response', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='extension_case', to='rotations.rotationresponse', verbose_name='Ответ с запросом на продление')),
            ],
            options={
                'verbose_name': 'Заявка на продление вахты',
                'verbose_name_plural': 'Заявки на продление вахты',
                'ordering': ['-created_at', '-id'],
                'indexes': [
                    models.Index(fields=['decision_status', 'created_at'], name='rot_ext_decision_date_idx'),
                    models.Index(fields=['documentation_status', 'decision_status'], name='rot_ext_document_status_idx'),
                ],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('extension_end__gte', models.F('extension_start'))), name='rot_ext_dates_valid'),
                    models.CheckConstraint(condition=models.Q(models.Q(('decision_at__isnull', True), ('decision_by__isnull', True), ('decision_status', 'pending')), models.Q(('decision_at__isnull', False), ('decision_by__isnull', False), ('decision_status__in', ['approved', 'rejected'])), _connector='OR'), name='rot_ext_decision_valid'),
                    models.CheckConstraint(condition=models.Q(models.Q(('documentation_at__isnull', True), ('documentation_by__isnull', True), ('documentation_status', 'not_started')), models.Q(('documentation_at__isnull', False), ('documentation_by__isnull', False), ('documentation_status__in', ['data_ready', 'completed'])), _connector='OR'), name='rot_ext_documentation_valid'),
                    models.CheckConstraint(condition=models.Q(('documentation_status', 'not_started'), ('decision_status', 'approved'), _connector='OR'), name='rot_ext_docs_after_approval'),
                ],
            },
        ),
        migrations.CreateModel(
            name='RotationActionLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action_code', models.CharField(db_index=True, max_length=64, verbose_name='Код действия')),
                ('details', models.JSONField(blank=True, default=dict, verbose_name='Детали действия')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rotation_actions', to='users.employee', verbose_name='Кто выполнил действие')),
                ('cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='action_logs', to='rotations.rotationcollectioncycle', verbose_name='Цикл сбора')),
                ('extension_case', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='action_logs', to='rotations.watchextensioncase', verbose_name='Заявка на продление')),
                ('response', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='action_logs', to='rotations.rotationresponse', verbose_name='Ответ')),
            ],
            options={
                'verbose_name': 'Событие перевахты',
                'verbose_name_plural': 'События перевахты',
                'ordering': ['-created_at', '-id'],
                'indexes': [
                    models.Index(fields=['cycle', 'created_at'], name='rot_log_cycle_date_idx'),
                    models.Index(fields=['action_code', 'created_at'], name='rot_log_action_date_idx'),
                ],
            },
        ),
    ]
