from django.db import models
from django.db.models import Q

from .defaults import (
    downtime_reason_color_group_for_state_code,
    infer_downtime_reason_state_code,
)


class DowntimeReason(models.Model):
    name = models.CharField('Причина простоя', max_length=160, unique=True)
    short_label = models.CharField('Короткая подпись для кнопки', max_length=80, blank=True)
    equipment_type = models.ForeignKey('references.EquipmentType', verbose_name='Вид техники', on_delete=models.PROTECT, null=True, blank=True)
    equipment_state = models.ForeignKey('references.EquipmentState', verbose_name='Состояние техники', on_delete=models.SET_NULL, null=True, blank=True)
    is_critical = models.BooleanField('Критический простой', default=False)
    show_for_truck_driver = models.BooleanField('Показывать водителю самосвала', default=False)
    show_for_excavator_operator = models.BooleanField('Показывать машинисту экскаватора', default=False)
    show_for_mechanic = models.BooleanField('Показывать механику', default=False)
    sort_order = models.PositiveIntegerField('Порядок', default=100)
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Причина простоя'
        verbose_name_plural = 'Причины простоев'
        ordering = ['sort_order', 'name']

    @classmethod
    def for_workplace(cls, workplace_code, equipment_type=None):
        workplace_flags = {
            'truck_driver': 'show_for_truck_driver',
            'excavator_operator': 'show_for_excavator_operator',
            'mechanic': 'show_for_mechanic',
        }
        flag_name = workplace_flags.get(workplace_code)
        queryset = cls.objects.filter(is_active=True)
        if flag_name:
            queryset = queryset.filter(**{flag_name: True})
        if equipment_type is not None:
            queryset = queryset.filter(Q(equipment_type=equipment_type) | Q(equipment_type__isnull=True))
        return queryset.select_related('equipment_type', 'equipment_state').order_by('sort_order', 'name')

    @property
    def button_label(self):
        return self.short_label or self.name

    @property
    def effective_equipment_state_code(self):
        if self.equipment_state_id and self.equipment_state:
            return self.equipment_state.code
        return infer_downtime_reason_state_code(self.name, is_critical=self.is_critical)

    @property
    def effective_color_group(self):
        if self.equipment_state_id and self.equipment_state:
            return self.equipment_state.color_group or 'yellow'
        return downtime_reason_color_group_for_state_code(self.effective_equipment_state_code)

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
