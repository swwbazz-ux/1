from django.db import models

from shifts.models import ShiftType


class RotationCollectionCycle(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        OPEN = 'open', 'Сбор открыт'
        CLOSED = 'closed', 'Сбор закрыт'
        ARCHIVED = 'archived', 'В архиве'

    name = models.CharField('Название сбора', max_length=160)
    target_watch_period = models.ForeignKey(
        'shifts.WatchPeriod',
        verbose_name='Целевая вахта',
        on_delete=models.PROTECT,
        related_name='rotation_collection_cycles',
    )
    response_deadline = models.DateTimeField('Срок предоставления ответа')
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    revision = models.PositiveIntegerField('Ревизия', default=1)
    created_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто создал',
        on_delete=models.SET_NULL,
        related_name='created_rotation_cycles',
        null=True,
        blank=True,
    )
    opened_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто открыл сбор',
        on_delete=models.SET_NULL,
        related_name='opened_rotation_cycles',
        null=True,
        blank=True,
    )
    opened_at = models.DateTimeField('Сбор открыт', null=True, blank=True)
    closed_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто закрыл сбор',
        on_delete=models.SET_NULL,
        related_name='closed_rotation_cycles',
        null=True,
        blank=True,
    )
    closed_at = models.DateTimeField('Сбор закрыт', null=True, blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Цикл сбора по перевахте'
        verbose_name_plural = 'Циклы сбора по перевахте'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['target_watch_period', 'status'], name='rot_cycle_watch_status_idx'),
            models.Index(fields=['status', 'response_deadline'], name='rot_cycle_status_due_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['target_watch_period'],
                condition=models.Q(status='open'),
                name='uniq_open_rotation_cycle_watch',
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name='rot_cycle_revision_gte_1',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status='draft',
                        opened_by__isnull=True,
                        opened_at__isnull=True,
                        closed_by__isnull=True,
                        closed_at__isnull=True,
                    )
                    | models.Q(
                        status='open',
                        opened_by__isnull=False,
                        opened_at__isnull=False,
                        closed_by__isnull=True,
                        closed_at__isnull=True,
                    )
                    | models.Q(
                        status__in=['closed', 'archived'],
                        opened_by__isnull=False,
                        opened_at__isnull=False,
                        closed_by__isnull=False,
                        closed_at__isnull=False,
                    )
                ),
                name='rot_cycle_lifecycle_valid',
            ),
        ]

    def __str__(self):
        return f'{self.name} / {self.target_watch_period}'


class RotationResponse(models.Model):
    class State(models.TextChoices):
        PENDING = 'pending', 'Ожидается ответ'
        SUBMITTED = 'submitted', 'Ответ предоставлен'

    class Intent(models.TextChoices):
        ARRIVAL = 'arrival', 'Заезд на вахту'
        DEPARTURE = 'departure', 'Выезд с вахты'
        NOT_TRAVELLING = 'not_travelling', 'Поездка не требуется'
        EXTENSION = 'extension', 'Запрос на продление вахты'

    class ShiftSource(models.TextChoices):
        UNKNOWN = 'unknown', 'Источник не определён'
        ACTIVE_ASSIGNMENT = 'active_assignment', 'Действующая расстановка'
        EMPLOYEE = 'employee', 'Указано сотрудником'
        TIMEKEEPER = 'timekeeper', 'Указано табельщиком'

    class TravelMode(models.TextChoices):
        AIR = 'air', 'Самолёт'
        RAIL = 'rail', 'Поезд'
        BUS = 'bus', 'Автобус'
        CAR = 'car', 'Автомобиль'
        OTHER = 'other', 'Другое'

    class TransferMode(models.TextChoices):
        ORGANIZED = 'organized', 'Организованный трансфер'
        SELF = 'self', 'Самостоятельно'

    cycle = models.ForeignKey(
        RotationCollectionCycle,
        verbose_name='Цикл сбора',
        on_delete=models.CASCADE,
        related_name='responses',
    )
    employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Сотрудник',
        on_delete=models.PROTECT,
        related_name='rotation_responses',
    )
    snapshot_full_name = models.CharField('ФИО snapshot', max_length=255)
    snapshot_personnel_number = models.CharField('Табельный номер snapshot', max_length=64, blank=True)
    snapshot_position = models.CharField('Должность snapshot', max_length=255, blank=True)
    snapshot_department = models.CharField('Подразделение snapshot', max_length=255, blank=True)
    snapshot_work_schedule = models.CharField('График работы snapshot', max_length=255, blank=True)
    snapshot_brigade_number = models.PositiveSmallIntegerField(
        'Бригада snapshot',
        null=True,
        blank=True,
    )
    state = models.CharField(
        'Состояние ответа',
        max_length=16,
        choices=State.choices,
        default=State.PENDING,
        db_index=True,
    )
    intent = models.CharField(
        'Намерение сотрудника',
        max_length=24,
        choices=Intent.choices,
        blank=True,
        default='',
        db_index=True,
    )
    next_shift_type = models.CharField(
        'Смена следующей вахты',
        max_length=16,
        choices=ShiftType.choices,
        blank=True,
        default='',
    )
    shift_source = models.CharField(
        'Источник смены',
        max_length=24,
        choices=ShiftSource.choices,
        default=ShiftSource.UNKNOWN,
    )
    departure_on = models.DateField('Дата выезда', null=True, blank=True)
    arrival_on = models.DateField('Дата заезда', null=True, blank=True)
    route_text = models.TextField('Маршрут', blank=True)
    travel_mode = models.CharField(
        'Вид транспорта',
        max_length=16,
        choices=TravelMode.choices,
        blank=True,
        default='',
    )
    transfer_mode = models.CharField(
        'Способ трансфера',
        max_length=16,
        choices=TransferMode.choices,
        blank=True,
        default='',
    )
    transport_details = models.TextField('Детали транспорта', blank=True)
    comment = models.TextField('Комментарий сотрудника', blank=True)
    submitted_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто предоставил ответ',
        on_delete=models.SET_NULL,
        related_name='submitted_rotation_responses',
        null=True,
        blank=True,
    )
    submitted_at = models.DateTimeField('Ответ предоставлен', null=True, blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Ответ по перевахте'
        verbose_name_plural = 'Ответы по перевахте'
        ordering = ['cycle', 'snapshot_full_name', 'id']
        indexes = [
            models.Index(fields=['cycle', 'state'], name='rot_resp_cycle_state_idx'),
            models.Index(fields=['cycle', 'intent'], name='rot_resp_cycle_intent_idx'),
            models.Index(fields=['employee', 'created_at'], name='rot_resp_employee_date_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['cycle', 'employee'],
                name='uniq_rotation_cycle_employee',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(snapshot_brigade_number__isnull=True)
                    | models.Q(snapshot_brigade_number__gte=1, snapshot_brigade_number__lte=4)
                ),
                name='rot_resp_brigade_1_4',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state='pending',
                        intent='',
                        submitted_by__isnull=True,
                        submitted_at__isnull=True,
                    )
                    | models.Q(
                        state='submitted',
                        intent__in=['arrival', 'departure', 'not_travelling', 'extension'],
                        submitted_by__isnull=False,
                        submitted_at__isnull=False,
                    )
                ),
                name='rot_resp_submission_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(next_shift_type__in=['', ShiftType.DAY, ShiftType.NIGHT]),
                name='rot_resp_next_shift_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    shift_source__in=['unknown', 'active_assignment', 'employee', 'timekeeper'],
                ),
                name='rot_resp_shift_source_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(travel_mode__in=['', 'air', 'rail', 'bus', 'car', 'other']),
                name='rot_resp_travel_mode_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(transfer_mode__in=['', 'organized', 'self']),
                name='rot_resp_transfer_mode_valid',
            ),
        ]

    def __str__(self):
        return f'{self.snapshot_full_name} / {self.cycle}'


class WatchExtensionCase(models.Model):
    class DecisionStatus(models.TextChoices):
        PENDING = 'pending', 'Ожидает решения'
        APPROVED = 'approved', 'Одобрено'
        REJECTED = 'rejected', 'Отклонено'

    class DocumentationStatus(models.TextChoices):
        NOT_STARTED = 'not_started', 'Не начато'
        DATA_READY = 'data_ready', 'Данные подготовлены'
        COMPLETED = 'completed', 'Оформление завершено'

    response = models.OneToOneField(
        RotationResponse,
        verbose_name='Ответ с запросом на продление',
        on_delete=models.CASCADE,
        related_name='extension_case',
    )
    extension_start = models.DateField('Начало продления')
    extension_end = models.DateField('Окончание продления')
    decision_status = models.CharField(
        'Решение начальника участка',
        max_length=16,
        choices=DecisionStatus.choices,
        default=DecisionStatus.PENDING,
        db_index=True,
    )
    decision_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто принял решение',
        on_delete=models.SET_NULL,
        related_name='reviewed_watch_extension_cases',
        null=True,
        blank=True,
    )
    decision_at = models.DateTimeField('Решение принято', null=True, blank=True)
    decision_comment = models.TextField('Комментарий начальника участка', blank=True)
    documentation_status = models.CharField(
        'Статус документального оформления',
        max_length=16,
        choices=DocumentationStatus.choices,
        default=DocumentationStatus.NOT_STARTED,
        db_index=True,
    )
    documentation_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто оформил документы',
        on_delete=models.SET_NULL,
        related_name='documented_watch_extension_cases',
        null=True,
        blank=True,
    )
    documentation_at = models.DateTimeField('Документы оформлены', null=True, blank=True)
    documentation_note = models.TextField('Примечание по оформлению', blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Изменено', auto_now=True)

    class Meta:
        verbose_name = 'Заявка на продление вахты'
        verbose_name_plural = 'Заявки на продление вахты'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['decision_status', 'created_at'], name='rot_ext_decision_date_idx'),
            models.Index(
                fields=['documentation_status', 'decision_status'],
                name='rot_ext_document_status_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(extension_end__gte=models.F('extension_start')),
                name='rot_ext_dates_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        decision_status='pending',
                        decision_by__isnull=True,
                        decision_at__isnull=True,
                    )
                    | models.Q(
                        decision_status__in=['approved', 'rejected'],
                        decision_by__isnull=False,
                        decision_at__isnull=False,
                    )
                ),
                name='rot_ext_decision_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        documentation_status='not_started',
                        documentation_by__isnull=True,
                        documentation_at__isnull=True,
                    )
                    | models.Q(
                        documentation_status__in=['data_ready', 'completed'],
                        documentation_by__isnull=False,
                        documentation_at__isnull=False,
                    )
                ),
                name='rot_ext_documentation_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(documentation_status='not_started')
                    | models.Q(decision_status='approved')
                ),
                name='rot_ext_docs_after_approval',
            ),
        ]

    def __str__(self):
        return f'{self.response.snapshot_full_name}: {self.extension_start:%d.%m.%Y}–{self.extension_end:%d.%m.%Y}'


class RotationActionLog(models.Model):
    cycle = models.ForeignKey(
        RotationCollectionCycle,
        verbose_name='Цикл сбора',
        on_delete=models.CASCADE,
        related_name='action_logs',
    )
    response = models.ForeignKey(
        RotationResponse,
        verbose_name='Ответ',
        on_delete=models.SET_NULL,
        related_name='action_logs',
        null=True,
        blank=True,
    )
    extension_case = models.ForeignKey(
        WatchExtensionCase,
        verbose_name='Заявка на продление',
        on_delete=models.SET_NULL,
        related_name='action_logs',
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто выполнил действие',
        on_delete=models.SET_NULL,
        related_name='rotation_actions',
        null=True,
        blank=True,
    )
    action_code = models.CharField('Код действия', max_length=64, db_index=True)
    details = models.JSONField('Детали действия', default=dict, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'Событие перевахты'
        verbose_name_plural = 'События перевахты'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['cycle', 'created_at'], name='rot_log_cycle_date_idx'),
            models.Index(fields=['action_code', 'created_at'], name='rot_log_action_date_idx'),
        ]

    def __str__(self):
        return f'{self.created_at:%d.%m.%Y %H:%M} / {self.action_code}'
