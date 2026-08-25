from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class TripStatus(models.TextChoices):
    ACTIVE = 'active', 'Активный'
    LOADED_WAITING_UNLOAD = 'loaded_waiting_unload', 'На разгрузку'
    COMPLETED = 'completed', 'Выполнен'
    CANCELLED = 'cancelled', 'Отменен'


class DispatcherActionType(models.TextChoices):
    SERVICE_CLOSE_SHIFT = 'service_close_shift', 'Служебное закрытие смены'
    CANCEL_ASSIGNMENT = 'cancel_assignment', 'Снятие назначения'
    CANCEL_TRIP = 'cancel_trip', 'Отмена рейса'
    COMPLETE_TRIP = 'complete_trip', 'Служебное завершение рейса'
    START_DOWNTIME = 'start_downtime', 'Служебное начало простоя'
    CLOSE_DOWNTIME = 'close_downtime', 'Служебное завершение простоя'


class InterventionSource(models.TextChoices):
    EMPLOYEE = 'employee', 'Сотрудник'
    DISPATCHER_OVERRIDE = 'dispatcher_override', 'Служебное действие диспетчера'
    TELEMETRY = 'telemetry', 'Телеметрия'
    SYSTEM = 'system', 'Система'


class InterventionActionType(models.TextChoices):
    COMPLETE_TRIP = 'complete_trip', 'Подтверждение разгрузки'
    START_DOWNTIME = 'start_downtime', 'Начало простоя'
    CLOSE_DOWNTIME = 'close_downtime', 'Завершение простоя'
    CANCEL_TRIP = 'cancel_trip', 'Отмена рейса'
    SERVICE_CLOSE_SHIFT = 'service_close_shift', 'Служебное закрытие смены'


class InterventionReasonCode(models.TextChoices):
    DEVICE_FAILURE = 'device_failure', 'Неисправность устройства'
    NO_CONNECTION = 'no_connection', 'Нет связи'
    EMPLOYEE_UNAVAILABLE = 'employee_unavailable', 'Сотрудник недоступен'
    OPERATIONAL_CORRECTION = 'operational_correction', 'Оперативная корректировка'
    OTHER = 'other', 'Другая причина'


class OperationalEffectStatus(models.TextChoices):
    CREATED = 'created', 'Создано'
    VALIDATED = 'validated', 'Проверено'
    APPLIED = 'applied', 'Применено'
    COMPENSATED = 'compensated', 'Скорректировано отдельным событием'
    REJECTED = 'rejected', 'Отклонено'


class InterventionReviewStatus(models.TextChoices):
    NOT_REQUIRED = 'not_required', 'Проверка не требуется'
    AWAITING_DELIVERY = 'awaiting_delivery', 'Ожидает доставки уведомления'
    AWAITING_OBJECTION = 'awaiting_objection', 'Ожидает возможного возражения'
    ACCEPTED_SILENTLY = 'accepted_silently', 'Принято без возражения'
    DISPUTED = 'disputed', 'Оспорено'
    UNDER_REVIEW = 'under_review', 'На рассмотрении'
    UPHELD = 'upheld', 'Подтверждено руководителем'
    ADJUSTED = 'adjusted', 'Скорректировано'
    REJECTED = 'rejected', 'Отклонено'


class InterventionMetricCode(models.TextChoices):
    TRIP_COUNT = 'trip_count', 'Количество рейсов'
    TRANSPORTED_VOLUME_M3 = 'transported_volume_m3', 'Перевезённый объём, м3'
    TRANSPORTED_TONNAGE = 'transported_tonnage', 'Перевезённый тоннаж'
    DOWNTIME_MINUTES = 'downtime_minutes', 'Минуты простоя'
    PRODUCTIVE_TIME_MINUTES = 'productive_time_minutes', 'Минуты производительной работы'
    PAID_DOWNTIME_MINUTES = 'paid_downtime_minutes', 'Оплачиваемые минуты простоя'
    EXCAVATOR_LOADED_VOLUME_M3 = 'excavator_loaded_volume_m3', 'Объём погрузки экскаватора, м3'


class InterventionImpactStatus(models.TextChoices):
    PENDING_REVIEW = 'pending_review', 'Ожидает проверки'
    HELD = 'held', 'Приостановлено спором'
    ACCEPTED = 'accepted', 'Принято в расчёт'
    BOOKED = 'booked', 'Включено в закрытый расчёт'
    CORRECTION_REQUIRED = 'correction_required', 'Требуется корректировка'
    CORRECTED = 'corrected', 'Скорректировано'
    REJECTED = 'rejected', 'Исключено из расчёта'


class InterventionContour(models.TextChoices):
    OPERATIONAL = 'operational', 'Операционный'
    REVIEW = 'review', 'Проверка и спор'
    CALCULATION = 'calculation', 'Расчётный'


class InterventionReviewEventType(models.TextChoices):
    NOTIFICATION_ATTEMPTED = 'notification_attempted', 'Попытка уведомления'
    DELIVERED = 'delivered', 'Уведомление доставлено'
    ACKNOWLEDGED = 'acknowledged', 'Сотрудник ознакомлен'
    DISPUTED = 'disputed', 'Сотрудник оспорил'
    REVIEW_STARTED = 'review_started', 'Рассмотрение начато'
    UPHELD = 'upheld', 'Действие подтверждено'
    ADJUSTED = 'adjusted', 'Действие скорректировано'
    REJECTED = 'rejected', 'Действие отклонено'
    SILENT_ACCEPTANCE = 'silent_acceptance', 'Срок возражения истёк'
    ESCALATED = 'escalated', 'Просроченный разбор эскалирован'


class InterventionEscalationLevel(models.TextChoices):
    NONE = 'none', 'Без эскалации'
    MANAGEMENT = 'management', 'Руководство'
    PERSONNEL_ACCOUNTING = 'personnel_accounting', 'ОУП / расчётный контур'


class InterventionResolutionCode(models.TextChoices):
    UPHOLD = 'uphold', 'Подтвердить исходное действие'
    ADJUST = 'adjust', 'Скорректировать расчётные последствия'
    REJECT = 'reject', 'Исключить расчётные последствия'


class InterventionAcknowledgementChannel(models.TextChoices):
    PWA = 'pwa', 'Приложение сотрудника'
    RADIO = 'radio', 'Рация'
    PHONE = 'phone', 'Телефон'
    IN_PERSON = 'in_person', 'Лично'
    PAPER = 'paper', 'Подпись на бумаге'
    SUPERVISOR = 'supervisor', 'Через непосредственного руководителя'


class Trip(models.Model):
    excavator = models.ForeignKey('references.Equipment', verbose_name='Экскаватор', on_delete=models.PROTECT, related_name='excavator_trips')
    truck = models.ForeignKey('references.Equipment', verbose_name='Самосвал', on_delete=models.PROTECT, related_name='truck_trips')
    excavator_operator = models.ForeignKey('users.Employee', verbose_name='Машинист экскаватора', on_delete=models.PROTECT, related_name='excavator_trips', null=True, blank=True)
    driver = models.ForeignKey('users.Employee', verbose_name='Водитель', on_delete=models.PROTECT, related_name='driver_trips', null=True, blank=True)
    loading_shift = models.ForeignKey('shifts.EmployeeShift', verbose_name='Смена загрузки', on_delete=models.PROTECT, related_name='loaded_trips', null=True, blank=True)
    unloading_shift = models.ForeignKey('shifts.EmployeeShift', verbose_name='Смена разгрузки', on_delete=models.PROTECT, related_name='unloaded_trips', null=True, blank=True)
    rock_type = models.ForeignKey('references.RockType', verbose_name='Порода', on_delete=models.PROTECT)
    dump_point = models.ForeignKey('references.DumpPoint', verbose_name='Точка разгрузки', on_delete=models.PROTECT)
    assigned_dump_point = models.ForeignKey(
        'references.DumpPoint',
        verbose_name='Назначенная точка разгрузки',
        on_delete=models.PROTECT,
        related_name='assigned_trips',
        null=True,
        blank=True,
    )
    actual_dump_point = models.ForeignKey(
        'references.DumpPoint',
        verbose_name='Фактическая точка разгрузки',
        on_delete=models.PROTECT,
        related_name='actual_trips',
        null=True,
        blank=True,
    )
    planned_volume_m3 = models.DecimalField('Плановое задание, м3', max_digits=10, decimal_places=2, null=True, blank=True)
    volume_m3 = models.DecimalField('Объем, м3', max_digits=10, decimal_places=2, null=True, blank=True)
    tonnage = models.DecimalField('Тоннаж', max_digits=10, decimal_places=2, null=True, blank=True)
    loading_horizon = models.CharField('Горизонт погрузки', max_length=64, blank=True)
    loading_block = models.CharField('Блок', max_length=64, blank=True)
    transport_distance_km = models.DecimalField('Плечо транспортировки, км', max_digits=8, decimal_places=2, null=True, blank=True)
    downtime_text = models.CharField('Простои', max_length=255, blank=True)
    note = models.TextField('Примечание', blank=True)
    status = models.CharField('Статус', max_length=32, choices=TripStatus.choices, default=TripStatus.ACTIVE)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    completed_at = models.DateTimeField('Выполнен', null=True, blank=True)
    cancelled_at = models.DateTimeField('Отменён', null=True, blank=True)
    is_carryover = models.BooleanField('Переходящий рейс', default=False)

    class Meta:
        verbose_name = 'Рейс'
        verbose_name_plural = 'Рейсы'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if (
            self.status == TripStatus.CANCELLED
            and self.cancelled_at is None
        ):
            self.cancelled_at = timezone.now()
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = tuple(dict.fromkeys((
                    *update_fields,
                    'cancelled_at',
                )))
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.truck} -> {self.dump_point} ({self.rock_type})'


OPEN_TRIP_STATUSES = (TripStatus.ACTIVE, TripStatus.LOADED_WAITING_UNLOAD)


class TripClientAction(models.Model):
    action_type = models.CharField('Тип действия клиента', max_length=64)
    client_action_id = models.CharField('ID действия клиента', max_length=128)
    trip = models.ForeignKey('trips.Trip', verbose_name='Рейс', on_delete=models.PROTECT, related_name='client_actions')
    actor = models.ForeignKey('users.Employee', verbose_name='Кто выполнил действие', on_delete=models.PROTECT, related_name='trip_client_actions')
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'Клиентское действие рейса'
        verbose_name_plural = 'Клиентские действия рейсов'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['action_type', 'client_action_id'], name='unique_trip_client_action'),
        ]

    def __str__(self):
        return f'{self.action_type}: {self.client_action_id}'


class DispatcherActionLog(models.Model):
    actor = models.ForeignKey('users.Employee', verbose_name='Кто выполнил действие', on_delete=models.PROTECT, related_name='dispatcher_action_logs')
    action_type = models.CharField('Тип действия', max_length=64, choices=DispatcherActionType.choices)
    trip = models.ForeignKey('trips.Trip', verbose_name='Рейс', on_delete=models.SET_NULL, null=True, blank=True, related_name='dispatcher_action_logs')
    shift = models.ForeignKey('shifts.EmployeeShift', verbose_name='Смена', on_delete=models.SET_NULL, null=True, blank=True, related_name='dispatcher_action_logs')
    haul_assignment = models.ForeignKey('assignments.HaulAssignment', verbose_name='Назначение', on_delete=models.SET_NULL, null=True, blank=True, related_name='dispatcher_action_logs')
    target_summary = models.CharField('Краткое описание объекта', max_length=255)
    reason = models.CharField('Причина действия', max_length=255, blank=True)
    created_at = models.DateTimeField('Когда выполнено', auto_now_add=True)

    class Meta:
        verbose_name = 'Диспетчерское действие'
        verbose_name_plural = 'Диспетчерские действия'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_action_type_display()}: {self.target_summary}'


class OperationalIntervention(models.Model):
    source = models.CharField(
        'Источник',
        max_length=32,
        choices=InterventionSource.choices,
        default=InterventionSource.DISPATCHER_OVERRIDE,
    )
    action_type = models.CharField('Тип действия', max_length=64, choices=InterventionActionType.choices)
    actor = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто зарегистрировал',
        on_delete=models.PROTECT,
        related_name='recorded_operational_interventions',
        null=True,
        blank=True,
    )
    subject_employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Кого затрагивает',
        on_delete=models.PROTECT,
        related_name='operational_interventions',
        null=True,
        blank=True,
    )
    equipment = models.ForeignKey(
        'references.Equipment',
        verbose_name='Техника',
        on_delete=models.PROTECT,
        related_name='operational_interventions',
        null=True,
        blank=True,
    )
    subject_shift = models.ForeignKey(
        'shifts.EmployeeShift',
        verbose_name='Затронутая смена',
        on_delete=models.PROTECT,
        related_name='operational_interventions',
        null=True,
        blank=True,
    )
    actor_shift = models.ForeignKey(
        'shifts.EmployeeShift',
        verbose_name='Смена регистратора',
        on_delete=models.PROTECT,
        related_name='recorded_operational_interventions',
        null=True,
        blank=True,
    )
    trip = models.ForeignKey(
        'trips.Trip',
        verbose_name='Рейс',
        on_delete=models.PROTECT,
        related_name='operational_interventions',
        null=True,
        blank=True,
    )
    downtime_event = models.ForeignKey(
        'downtimes.DowntimeEvent',
        verbose_name='Событие простоя',
        on_delete=models.PROTECT,
        related_name='operational_interventions',
        null=True,
        blank=True,
    )
    dispatcher_action_log = models.OneToOneField(
        'trips.DispatcherActionLog',
        verbose_name='Запись старого журнала диспетчера',
        on_delete=models.PROTECT,
        related_name='operational_intervention',
        null=True,
        blank=True,
    )
    compensation_for = models.ForeignKey(
        'self',
        verbose_name='Корректирует вмешательство',
        on_delete=models.PROTECT,
        related_name='compensations',
        null=True,
        blank=True,
    )
    reason_code = models.CharField('Код причины', max_length=64, choices=InterventionReasonCode.choices)
    reason = models.CharField('Причина', max_length=255)
    comment = models.TextField('Комментарий', blank=True)
    occurred_at = models.DateTimeField('Фактическое время события')
    recorded_at = models.DateTimeField('Время регистрации', auto_now_add=True)
    operational_status = models.CharField(
        'Операционное состояние',
        max_length=32,
        choices=OperationalEffectStatus.choices,
        default=OperationalEffectStatus.APPLIED,
    )
    review_status = models.CharField(
        'Состояние проверки',
        max_length=32,
        choices=InterventionReviewStatus.choices,
        default=InterventionReviewStatus.AWAITING_DELIVERY,
    )
    notification_attempted_at = models.DateTimeField('Попытка уведомления', null=True, blank=True)
    delivered_at = models.DateTimeField('Уведомление доставлено', null=True, blank=True)
    acknowledged_at = models.DateTimeField('Сотрудник ознакомлен', null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто подтвердил ознакомление',
        on_delete=models.PROTECT,
        related_name='acknowledged_operational_interventions',
        null=True,
        blank=True,
    )
    acknowledgement_channel = models.CharField(
        'Канал ознакомления',
        max_length=32,
        choices=InterventionAcknowledgementChannel.choices,
        blank=True,
    )
    acknowledgement_comment = models.TextField('Комментарий к ознакомлению', blank=True)
    objection_deadline = models.DateTimeField('Срок возражения', null=True, blank=True)
    review_started_at = models.DateTimeField('Рассмотрение начато', null=True, blank=True)
    review_due_at = models.DateTimeField('Срок рассмотрения', null=True, blank=True)
    reviewer = models.ForeignKey(
        'users.Employee',
        verbose_name='Ответственный за рассмотрение',
        on_delete=models.PROTECT,
        related_name='reviewing_operational_interventions',
        null=True,
        blank=True,
    )
    escalation_level = models.CharField(
        'Уровень эскалации',
        max_length=32,
        choices=InterventionEscalationLevel.choices,
        default=InterventionEscalationLevel.NONE,
    )
    escalated_at = models.DateTimeField('Эскалировано', null=True, blank=True)
    resolved_at = models.DateTimeField('Спор разрешён', null=True, blank=True)
    resolved_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто разрешил спор',
        on_delete=models.PROTECT,
        related_name='resolved_operational_interventions',
        null=True,
        blank=True,
    )
    resolution_comment = models.TextField('Решение по спору', blank=True)
    resolution_code = models.CharField(
        'Код решения',
        max_length=24,
        choices=InterventionResolutionCode.choices,
        blank=True,
    )
    idempotency_key = models.CharField('Ключ идемпотентности', max_length=128, blank=True)
    metadata = models.JSONField('Дополнительные данные', default=dict, blank=True)

    IMMUTABLE_FIELD_NAMES = (
        'source',
        'action_type',
        'actor_id',
        'subject_employee_id',
        'equipment_id',
        'subject_shift_id',
        'actor_shift_id',
        'trip_id',
        'downtime_event_id',
        'dispatcher_action_log_id',
        'compensation_for_id',
        'reason_code',
        'reason',
        'comment',
        'occurred_at',
        'recorded_at',
        'idempotency_key',
        'metadata',
    )

    class Meta:
        verbose_name = 'Операционное вмешательство'
        verbose_name_plural = 'Операционные вмешательства'
        ordering = ['-recorded_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'action_type', 'idempotency_key'],
                condition=~Q(idempotency_key=''),
                name='unique_operational_intervention_action',
            ),
            models.CheckConstraint(
                condition=~Q(source=InterventionSource.DISPATCHER_OVERRIDE) | Q(actor__isnull=False),
                name='dispatcher_override_requires_actor',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELD_NAMES).first()
            if original:
                changed = [
                    field_name
                    for field_name in self.IMMUTABLE_FIELD_NAMES
                    if original[field_name] != getattr(self, field_name)
                ]
                if changed:
                    raise ValidationError(
                        f'Нельзя изменять исходные данные вмешательства: {", ".join(changed)}.'
                    )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Операционное вмешательство нельзя удалить; создайте корректирующее событие.')

    def __str__(self):
        return f'{self.get_action_type_display()}: {self.equipment or self.trip or self.subject_employee or self.pk}'


class InterventionImpact(models.Model):
    intervention = models.ForeignKey(
        OperationalIntervention,
        verbose_name='Вмешательство',
        on_delete=models.PROTECT,
        related_name='impacts',
    )
    metric_code = models.CharField('Показатель', max_length=64, choices=InterventionMetricCode.choices)
    value = models.DecimalField('Изменение', max_digits=14, decimal_places=3)
    unit = models.CharField('Единица измерения', max_length=24)
    status = models.CharField(
        'Расчётное состояние',
        max_length=32,
        choices=InterventionImpactStatus.choices,
        default=InterventionImpactStatus.PENDING_REVIEW,
    )
    correction_for = models.ForeignKey(
        'self',
        verbose_name='Корректирует последствие',
        on_delete=models.PROTECT,
        related_name='corrections',
        null=True,
        blank=True,
    )
    accepted_at = models.DateTimeField('Принято в расчёт', null=True, blank=True)
    booked_at = models.DateTimeField('Включено в закрытый расчёт', null=True, blank=True)
    corrected_at = models.DateTimeField('Скорректировано', null=True, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    IMMUTABLE_FIELD_NAMES = (
        'intervention_id',
        'metric_code',
        'value',
        'unit',
        'correction_for_id',
        'created_at',
    )

    class Meta:
        verbose_name = 'Расчётное последствие вмешательства'
        verbose_name_plural = 'Расчётные последствия вмешательств'
        ordering = ['intervention_id', 'metric_code', 'id']

    def __str__(self):
        return f'{self.get_metric_code_display()}: {self.value} {self.unit}'

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELD_NAMES).first()
            if original:
                changed = [
                    field_name
                    for field_name in self.IMMUTABLE_FIELD_NAMES
                    if original[field_name] != getattr(self, field_name)
                ]
                if changed:
                    raise ValidationError(
                        f'Нельзя изменять исходные данные расчётного последствия: {", ".join(changed)}.'
                    )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Расчётное последствие нельзя удалить; создайте корректирующую запись.')


class InterventionStateTransition(models.Model):
    intervention = models.ForeignKey(
        OperationalIntervention,
        verbose_name='Вмешательство',
        on_delete=models.PROTECT,
        related_name='state_transitions',
    )
    impact = models.ForeignKey(
        InterventionImpact,
        verbose_name='Расчётное последствие',
        on_delete=models.PROTECT,
        related_name='state_transitions',
        null=True,
        blank=True,
    )
    contour = models.CharField('Контур', max_length=24, choices=InterventionContour.choices)
    from_status = models.CharField('Исходное состояние', max_length=32, blank=True)
    to_status = models.CharField('Новое состояние', max_length=32)
    actor = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто выполнил переход',
        on_delete=models.PROTECT,
        related_name='operational_intervention_transitions',
        null=True,
        blank=True,
    )
    comment = models.TextField('Комментарий', blank=True)
    created_at = models.DateTimeField('Когда выполнено', auto_now_add=True)

    class Meta:
        verbose_name = 'Переход состояния вмешательства'
        verbose_name_plural = 'Переходы состояний вмешательств'
        ordering = ['created_at', 'id']

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Переход состояния является неизменяемой записью журнала.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Переход состояния нельзя удалить.')


class InterventionReviewEvent(models.Model):
    intervention = models.ForeignKey(
        OperationalIntervention,
        verbose_name='Вмешательство',
        on_delete=models.PROTECT,
        related_name='review_events',
    )
    event_type = models.CharField('Событие проверки', max_length=32, choices=InterventionReviewEventType.choices)
    actor = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто зафиксировал',
        on_delete=models.PROTECT,
        related_name='operational_intervention_review_events',
        null=True,
        blank=True,
    )
    channel = models.CharField('Канал', max_length=32, blank=True)
    comment = models.TextField('Комментарий', blank=True)
    created_at = models.DateTimeField('Когда выполнено', auto_now_add=True)

    class Meta:
        verbose_name = 'Событие проверки вмешательства'
        verbose_name_plural = 'События проверки вмешательств'
        ordering = ['created_at', 'id']

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Событие проверки является неизменяемой записью журнала.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Событие проверки нельзя удалить.')
