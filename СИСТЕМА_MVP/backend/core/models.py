from django.db import models, transaction
from django.utils import timezone


class OperationalStateVersion(models.Model):
    """Single row marker for screens that must notice live production changes."""

    key = models.CharField('Ключ состояния', max_length=64, unique=True)
    version = models.PositiveBigIntegerField('Версия', default=0)
    reason = models.CharField('Причина изменения', max_length=128, blank=True)
    updated_at = models.DateTimeField('Обновлено', default=timezone.now)

    class Meta:
        verbose_name = 'Версия оперативного состояния'
        verbose_name_plural = 'Версии оперативного состояния'
        ordering = ['key']

    def __str__(self):
        return f'{self.key}: {self.version}'


class OperationalStateEvent(models.Model):
    """Immutable event record for clients that can update screens without full reload."""

    key = models.CharField('Ключ состояния', max_length=64, db_index=True)
    version = models.PositiveBigIntegerField('Версия события')
    event_type = models.CharField('Тип события', max_length=64, db_index=True)
    object_type = models.CharField('Тип объекта', max_length=128, blank=True)
    object_id = models.CharField('ID объекта', max_length=64, blank=True)
    reason = models.CharField('Причина изменения', max_length=128, blank=True)
    payload = models.JSONField('Данные события', default=dict, blank=True)
    created_at = models.DateTimeField('Создано', default=timezone.now)

    class Meta:
        verbose_name = 'Событие оперативного состояния'
        verbose_name_plural = 'События оперативного состояния'
        ordering = ['version']
        indexes = [
            models.Index(fields=['key', 'version']),
            models.Index(fields=['key', 'event_type']),
        ]

    def __str__(self):
        return f'{self.key} #{self.version}: {self.event_type}'


class OfflineFieldEventStatus(models.TextChoices):
    PROCESSING = 'processing', 'Обрабатывается'
    ACCEPTED = 'accepted', 'Принято'
    RETRY = 'retry', 'Повторить'
    CONFLICT = 'conflict', 'Требует сверки'
    INVALID = 'invalid', 'Некорректно'


class OfflineFieldEvent(models.Model):
    """Durable server receipt for one field-device outbox event.

    ``occurred_at`` is the timestamp reported by the device.  It is preserved
    separately from ``received_at`` and is never sufficient on its own to
    grant an operation: processors also validate the recorded shift,
    equipment and assignment history.
    """

    event_id = models.CharField('Неизменяемый ID события', max_length=128, unique=True)
    event_type = models.CharField('Тип события', max_length=96, db_index=True)
    format_version = models.PositiveSmallIntegerField('Версия формата', default=1)
    actor = models.ForeignKey(
        'users.Employee', verbose_name='Сотрудник', on_delete=models.PROTECT,
        related_name='offline_field_events',
    )
    access = models.ForeignKey(
        'users.EmployeeAccess', verbose_name='Доступ', on_delete=models.PROTECT,
        related_name='offline_field_events',
    )
    role_code = models.CharField('Роль', max_length=64)
    device_id = models.CharField('ID устройства', max_length=128)
    sequence = models.PositiveBigIntegerField('Порядок на устройстве')
    depends_on = models.JSONField('Зависимости', default=list, blank=True)
    occurred_at = models.DateTimeField('Время на устройстве')
    received_at = models.DateTimeField('Получено сервером', default=timezone.now)
    shift = models.ForeignKey(
        'shifts.EmployeeShift', verbose_name='Смена', on_delete=models.PROTECT,
        related_name='offline_field_events', null=True, blank=True,
    )
    equipment = models.ForeignKey(
        'references.Equipment', verbose_name='Техника', on_delete=models.PROTECT,
        related_name='offline_field_events', null=True, blank=True,
    )
    trip = models.ForeignKey(
        'trips.Trip', verbose_name='Рейс', on_delete=models.PROTECT,
        related_name='offline_field_events', null=True, blank=True,
    )
    downtime_event = models.ForeignKey(
        'downtimes.DowntimeEvent', verbose_name='Простой', on_delete=models.PROTECT,
        related_name='offline_field_events', null=True, blank=True,
    )
    local_trip_id = models.CharField('Локальный ID рейса', max_length=128, blank=True)
    local_downtime_id = models.CharField('Локальный ID простоя', max_length=128, blank=True)
    context_snapshot = models.JSONField('Контекст клиента', default=dict, blank=True)
    payload = models.JSONField('Параметры', default=dict, blank=True)
    fingerprint = models.CharField('Отпечаток', max_length=64)
    # The original request is independent of the mutable application links above.
    # Empty means a pre-F3 receipt; it cannot be reconstructed from those links.
    input_envelope = models.JSONField('Исходный серверный конверт', default=dict, editable=False)
    retry_attempts = models.PositiveIntegerField('Попыток применения', default=0)
    next_retry_at = models.DateTimeField('Следующая попытка', null=True, blank=True)
    status = models.CharField(
        'Статус', max_length=16, choices=OfflineFieldEventStatus.choices,
        default=OfflineFieldEventStatus.PROCESSING,
    )
    retryable = models.BooleanField('Можно повторить', default=False)
    error_code = models.CharField('Код ошибки', max_length=64, blank=True)
    error_message = models.TextField('Ошибка', blank=True)
    result_payload = models.JSONField('Подтверждение сервера', default=dict, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Offline-событие полевого приложения'
        verbose_name_plural = 'Offline-события полевых приложений'
        ordering = ['received_at', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['actor', 'role_code', 'device_id', 'sequence'],
                name='unique_offline_device_event_sequence',
            ),
        ]
        indexes = [
            models.Index(fields=['actor', 'device_id', 'status'], name='off_evt_actor_dev_status'),
            models.Index(fields=['received_at', 'status'], name='off_evt_received_status'),
            models.Index(fields=['status', 'next_retry_at'], name='off_evt_retry_due'),
        ]

    def __str__(self):
        return f'{self.event_type}: {self.event_id} ({self.status})'


class OfflineFieldEventConflict(models.Model):
    """Immutable evidence of an incompatible reuse of an id or sequence."""

    existing_event = models.ForeignKey(
        OfflineFieldEvent, verbose_name='Исходное событие', on_delete=models.PROTECT,
        related_name='conflict_attempts', null=True, blank=True,
    )
    attempted_event_id = models.CharField('Повторный ID события', max_length=128)
    actor = models.ForeignKey(
        'users.Employee', verbose_name='Сотрудник', on_delete=models.PROTECT,
        related_name='offline_field_event_conflicts',
    )
    access = models.ForeignKey(
        'users.EmployeeAccess', verbose_name='Доступ', on_delete=models.PROTECT,
        related_name='offline_field_event_conflicts',
    )
    role_code = models.CharField('Роль', max_length=64)
    device_id = models.CharField('ID устройства', max_length=128)
    fingerprint = models.CharField('Отпечаток повтора', max_length=64)
    code = models.CharField('Код конфликта', max_length=64)
    submitted_event = models.JSONField('Повторно полученное событие', default=dict)
    received_at = models.DateTimeField('Получено', default=timezone.now)

    class Meta:
        verbose_name = 'Конфликт offline-события'
        verbose_name_plural = 'Конфликты offline-событий'
        ordering = ['-received_at', '-id']
        indexes = [
            models.Index(
                fields=['attempted_event_id', 'received_at'],
                name='off_conf_event_time',
            ),
        ]


class NotificationIntent(models.Model):
    """One immutable user-visible effect, written in the domain transaction."""

    effect_key = models.CharField('Ключ эффекта', max_length=192, unique=True)
    payload = models.JSONField('Снимок уведомления', editable=False)
    notification = models.OneToOneField(
        'users.PushNotification', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='delivery_intent', verbose_name='Текст для приложения',
    )
    created_at = models.DateTimeField('Создано', default=timezone.now)

    class Meta:
        verbose_name = 'Намерение уведомления'
        verbose_name_plural = 'Намерения уведомлений'


class NotificationDelivery(models.Model):
    """Durable per-endpoint retry; a lease fences acknowledgements, not HTTP."""

    intent = models.ForeignKey(NotificationIntent, on_delete=models.CASCADE, related_name='deliveries')
    channel = models.CharField('Канал', max_length=16, choices=[('web', 'Web Push'), ('native', 'Native Push')])
    target_id = models.PositiveBigIntegerField('ID подписки')
    target_fingerprint = models.CharField('Отпечаток адресата', max_length=64)
    available_at = models.DateTimeField('Следующая попытка', default=timezone.now)
    lease_token = models.CharField('Владелец попытки', max_length=32, blank=True)
    leased_until = models.DateTimeField('Аренда до', null=True, blank=True)
    attempts = models.PositiveIntegerField('Попыток доставки', default=0)
    delivered_at = models.DateTimeField('Доставлено', null=True, blank=True)
    terminal_reason = models.CharField('Причина завершения', max_length=64, blank=True)
    last_error = models.CharField('Последняя ошибка', max_length=128, blank=True)

    class Meta:
        verbose_name = 'Доставка уведомления'
        verbose_name_plural = 'Доставки уведомлений'
        constraints = [
            models.UniqueConstraint(
                fields=['intent', 'channel', 'target_id'], name='notification_target_once',
            ),
        ]
        indexes = [models.Index(fields=['available_at', 'leased_until'], name='notification_delivery_due')]


def lock_production_state():
    OperationalStateVersion.objects.get_or_create(key='production')
    return OperationalStateVersion.objects.select_for_update().get(key='production')


def bump_operational_state(
    reason='',
    *,
    event_type='state_changed',
    object_type='',
    object_id='',
    payload=None,
):
    with transaction.atomic():
        state, _ = (
            OperationalStateVersion.objects
            .select_for_update()
            .get_or_create(key='production')
        )
        state.version += 1
        state.reason = reason[:128]
        state.updated_at = timezone.now()
        state.save(update_fields=['version', 'reason', 'updated_at'])
        event = OperationalStateEvent.objects.create(
            key=state.key,
            version=state.version,
            event_type=event_type[:64],
            object_type=object_type[:128],
            object_id=str(object_id or '')[:64],
            reason=state.reason,
            payload=payload or {},
            created_at=state.updated_at,
        )
        # Persist the intent with the effect. A separate worker performs HTTP
        # after commit; losing an on_commit callback cannot lose the notification.
        from .dispatcher_push import enqueue_dispatcher_push_for_event
        enqueue_dispatcher_push_for_event(event)
    return state
