from django.db import models


class DowntimeReason(models.Model):
    name = models.CharField('Причина простоя', max_length=160, unique=True)
    equipment_type = models.ForeignKey('references.EquipmentType', verbose_name='Вид техники', on_delete=models.PROTECT, null=True, blank=True)
    is_critical = models.BooleanField('Критический простой', default=False)
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Причина простоя'
        verbose_name_plural = 'Причины простоев'
        ordering = ['name']

    def __str__(self):
        return self.name


class DowntimeEvent(models.Model):
    equipment = models.ForeignKey('references.Equipment', verbose_name='Техника', on_delete=models.PROTECT)
    employee = models.ForeignKey('users.Employee', verbose_name='Кто зафиксировал', on_delete=models.PROTECT, null=True, blank=True)
    reason = models.ForeignKey(DowntimeReason, verbose_name='Причина', on_delete=models.PROTECT)
    started_at = models.DateTimeField('Начало')
    ended_at = models.DateTimeField('Окончание', null=True, blank=True)
    comment = models.TextField('Комментарий', blank=True)

    class Meta:
        verbose_name = 'Событие простоя'
        verbose_name_plural = 'События простоев'
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.equipment} / {self.reason}'

# Create your models here.
