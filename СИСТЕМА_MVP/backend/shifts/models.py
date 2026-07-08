from django.db import models
from django.utils import timezone


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


class PlanCalculationMode(models.TextChoices):
    TRIPS = 'trips', 'По рейсам'
    VOLUME = 'volume_m3', 'По объему, м3'
    TONNAGE = 'tonnage', 'По тоннажу'
    MIXED = 'mixed', 'Смешанный'


class ShiftPlanScope(models.TextChoices):
    MONTH = 'month', 'Месячный план'
    DAY = 'day_total', 'Суточный план'
    DAY_SHIFT = 'day_shift', 'Дневная смена'
    NIGHT_SHIFT = 'night_shift', 'Ночная смена'


class ShiftPlan(models.Model):
    plan_scope = models.CharField(
        'Тип плана',
        max_length=16,
        choices=ShiftPlanScope.choices,
        default=ShiftPlanScope.DAY_SHIFT,
    )
    date = models.DateField('Дата начала действия', default=timezone.localdate)
    shift_type = models.CharField('Расчетная смена', max_length=16, choices=ShiftType.choices, default=ShiftType.DAY)
    name = models.CharField('Название плана', max_length=128, default='Сменный план')
    plan_trips = models.PositiveIntegerField('План рейсов', null=True, blank=True)
    plan_volume_m3 = models.DecimalField('План объема, м3', max_digits=12, decimal_places=2, null=True, blank=True)
    plan_tonnage = models.DecimalField('План тоннажа', max_digits=12, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField('Активен', default=True)
    created_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто создал',
        on_delete=models.SET_NULL,
        related_name='created_shift_plans',
        null=True,
        blank=True,
    )
    comment = models.TextField('Комментарий', blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменен', auto_now=True)

    class Meta:
        verbose_name = 'Сменный план'
        verbose_name_plural = 'Сменные планы'
        ordering = ['-date', 'shift_type', 'name']
        unique_together = [('date', 'shift_type', 'name')]

    def __str__(self):
        return f'{self.name} / {self.get_plan_scope_display()}'

    def save(self, *args, **kwargs):
        if self.plan_scope == ShiftPlanScope.NIGHT_SHIFT:
            self.shift_type = ShiftType.NIGHT
        else:
            self.shift_type = ShiftType.DAY
        super().save(*args, **kwargs)


class EquipmentShiftPlan(models.Model):
    shift_plan = models.ForeignKey(
        ShiftPlan,
        verbose_name='Сменный план',
        on_delete=models.CASCADE,
        related_name='equipment_plans',
    )
    equipment = models.ForeignKey(
        'references.Equipment',
        verbose_name='Техника',
        on_delete=models.PROTECT,
        related_name='shift_plans',
    )
    employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Сотрудник',
        on_delete=models.SET_NULL,
        related_name='equipment_shift_plans',
        null=True,
        blank=True,
    )
    plan_trips = models.PositiveIntegerField('План рейсов', null=True, blank=True)
    plan_volume_m3 = models.DecimalField('План объема, м3', max_digits=12, decimal_places=2, null=True, blank=True)
    plan_tonnage = models.DecimalField('План тоннажа', max_digits=12, decimal_places=2, null=True, blank=True)
    calculation_mode = models.CharField(
        'Расчет выполнения',
        max_length=16,
        choices=PlanCalculationMode.choices,
        default=PlanCalculationMode.VOLUME,
    )
    is_active = models.BooleanField('Активен', default=True)
    comment = models.TextField('Комментарий', blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменен', auto_now=True)

    class Meta:
        verbose_name = 'План техники на смену'
        verbose_name_plural = 'Планы техники на смену'
        ordering = ['shift_plan__date', 'shift_plan__shift_type', 'equipment__garage_number']
        unique_together = [('shift_plan', 'equipment')]

    def __str__(self):
        return f'{self.equipment} / {self.shift_plan}'
