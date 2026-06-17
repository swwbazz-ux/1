from django.db import models


class ShiftType(models.TextChoices):
    DAY = 'day', 'Дневная'
    NIGHT = 'night', 'Ночная'


class WatchPeriod(models.Model):
    name = models.CharField('Название вахты', max_length=128)
    starts_on = models.DateField('Дата начала')
    ends_on = models.DateField('Дата окончания')
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Вахта'
        verbose_name_plural = 'Вахты'
        ordering = ['-starts_on']

    def __str__(self):
        return self.name


class EmployeeShift(models.Model):
    employee = models.ForeignKey('users.Employee', verbose_name='Сотрудник', on_delete=models.PROTECT)
    shift_type = models.CharField('Смена', max_length=16, choices=ShiftType.choices)
    watch_period = models.ForeignKey(WatchPeriod, verbose_name='Вахта', on_delete=models.PROTECT, null=True, blank=True)
    equipment = models.ForeignKey('references.Equipment', verbose_name='Техника', on_delete=models.PROTECT, null=True, blank=True)
    start_fuel = models.DecimalField('Топливо на начало', max_digits=10, decimal_places=2, null=True, blank=True)
    start_mileage = models.DecimalField('Пробег на начало', max_digits=10, decimal_places=2, null=True, blank=True)
    start_engine_hours = models.DecimalField('Моточасы на начало', max_digits=10, decimal_places=2, null=True, blank=True)
    end_fuel = models.DecimalField('Топливо на конец', max_digits=10, decimal_places=2, null=True, blank=True)
    end_mileage = models.DecimalField('Пробег на конец', max_digits=10, decimal_places=2, null=True, blank=True)
    end_engine_hours = models.DecimalField('Моточасы на конец', max_digits=10, decimal_places=2, null=True, blank=True)
    opened_at = models.DateTimeField('Открыта')
    closed_at = models.DateTimeField('Закрыта', null=True, blank=True)
    opened_by = models.ForeignKey('users.Employee', verbose_name='Кто открыл', on_delete=models.PROTECT, related_name='opened_shifts', null=True, blank=True)
    closed_by = models.ForeignKey('users.Employee', verbose_name='Кто закрыл', on_delete=models.PROTECT, related_name='closed_shifts', null=True, blank=True)
    is_service_closed = models.BooleanField('Служебное закрытие', default=False)

    class Meta:
        verbose_name = 'Смена сотрудника'
        verbose_name_plural = 'Смены сотрудников'
        ordering = ['-opened_at']

    def __str__(self):
        return f'{self.employee} / {self.get_shift_type_display()} / {self.opened_at:%d.%m.%Y}'

# Create your models here.
