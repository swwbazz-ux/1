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
        OperationalStateEvent.objects.create(
            key=state.key,
            version=state.version,
            event_type=event_type[:64],
            object_type=object_type[:128],
            object_id=str(object_id or '')[:64],
            reason=state.reason,
            payload=payload or {},
            created_at=state.updated_at,
        )
    return state
