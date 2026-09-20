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
        # Только после успешного commit: push никогда не должен сообщать
        # о событии, которое база в итоге откатила.
        from .dispatcher_push import notification_for_event, send_dispatcher_push_for_event
        if notification_for_event(event):
            transaction.on_commit(
                lambda event_id=event.pk: send_dispatcher_push_for_event(event_id)
            )
    return state
