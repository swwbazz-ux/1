from django.db import models
from django.utils import timezone


class ShiftType(models.TextChoices):
    DAY = 'day', 'Дневная'
    NIGHT = 'night', 'Ночная'


class PlanCalculationMode(models.TextChoices):
    TRIPS = 'trips', 'По рейсам'
    VOLUME = 'volume_m3', 'По объему, м3'
    TONNAGE = 'tonnage', 'По тоннажу'
    MIXED = 'mixed', 'Смешанный'


class PlanAssignmentStatus(models.TextChoices):
    ASSIGNED = 'assigned', 'План назначен'
    NO_PLAN_GROUP = 'no_plan_group', 'Нет группы плана'
    NO_ACTIVE_PLAN = 'no_active_plan', 'Нет активного плана'


class EquipmentPlanGroup(models.Model):
    code = models.SlugField('Код группы', max_length=64, unique=True)
    name = models.CharField('Группа техники', max_length=128, unique=True)
    calculation_mode = models.CharField(
        'Тип расчета',
        max_length=16,
        choices=PlanCalculationMode.choices,
        default=PlanCalculationMode.TRIPS,
    )
    plan_value = models.DecimalField('Значение плана', max_digits=12, decimal_places=2, null=True, blank=True)
    equipment = models.ManyToManyField(
        'references.Equipment',
        verbose_name='Техника в группе',
        related_name='plan_groups',
        blank=True,
    )
    is_active = models.BooleanField('Активен', default=True)
    active_from = models.DateField('Дата начала действия', default=timezone.localdate)
    comment = models.TextField('Комментарий', blank=True)
    updated_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто изменил',
        on_delete=models.SET_NULL,
        related_name='updated_equipment_plan_groups',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменен', auto_now=True)

    class Meta:
        verbose_name = 'Ежесменный план техники'
        verbose_name_plural = 'Ежесменные планы техники'
        ordering = ['name']

    @property
    def equipment_list(self):
        items = list(self.equipment.select_related('equipment_type', 'model').order_by('equipment_type__name', 'garage_number'))
        return ', '.join(str(item) for item in items) if items else 'Техника не выбрана'

    @property
    def plan_unit(self):
        if self.calculation_mode == PlanCalculationMode.TRIPS:
            return 'рейсов'
        if self.calculation_mode == PlanCalculationMode.VOLUME:
            return 'м³'
        if self.calculation_mode == PlanCalculationMode.TONNAGE:
            return 'т'
        return ''

    def __str__(self):
        return self.name


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
    workplace_code = models.CharField('Рабочий контур', max_length=32, blank=True, db_index=True)
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
    plan_group = models.ForeignKey(
        EquipmentPlanGroup,
        verbose_name='Группа плана',
        on_delete=models.SET_NULL,
        related_name='shift_snapshots',
        null=True,
        blank=True,
    )
    plan_group_name = models.CharField('Группа плана snapshot', max_length=128, blank=True)
    plan_calculation_mode = models.CharField(
        'Тип расчета snapshot',
        max_length=16,
        choices=PlanCalculationMode.choices,
        blank=True,
    )
    plan_value = models.DecimalField('Значение плана snapshot', max_digits=12, decimal_places=2, null=True, blank=True)
    plan_assigned_at = models.DateTimeField('План назначен', null=True, blank=True)
    plan_status = models.CharField(
        'Статус плана',
        max_length=32,
        choices=PlanAssignmentStatus.choices,
        blank=True,
    )

    class Meta:
        verbose_name = 'Смена сотрудника'
        verbose_name_plural = 'Смены сотрудников'
        ordering = ['-opened_at']
        constraints = [
            models.UniqueConstraint(
                fields=['employee'],
                condition=models.Q(closed_at__isnull=True),
                name='unique_open_shift_per_employee',
            ),
            models.UniqueConstraint(
                fields=['equipment'],
                condition=models.Q(closed_at__isnull=True, equipment__isnull=False),
                name='unique_open_shift_per_equipment',
            ),
        ]

    def __str__(self):
        return f'{self.employee} / {self.get_shift_type_display()} / {self.opened_at:%d.%m.%Y}'


class ShiftClientAction(models.Model):
    action_type = models.CharField('Действие', max_length=64)
    client_action_id = models.CharField('ID действия клиента', max_length=128)
    employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Машинист',
        on_delete=models.PROTECT,
        related_name='shift_client_actions',
    )
    shift = models.ForeignKey(
        EmployeeShift,
        verbose_name='Смена',
        on_delete=models.PROTECT,
        related_name='client_actions',
        null=True,
        blank=True,
    )
    response_payload = models.JSONField('Ответ сервера', default=dict, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'Клиентское действие со сменой'
        verbose_name_plural = 'Клиентские действия со сменами'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['action_type', 'client_action_id'],
                name='unique_shift_client_action',
            ),
        ]


class ShiftReadingCorrection(models.Model):
    class Metric(models.TextChoices):
        FUEL = 'fuel', 'Топливо'
        MILEAGE = 'mileage', 'Одометр'
        ENGINE_HOURS = 'engine_hours', 'Моточасы'

    equipment = models.ForeignKey(
        'references.Equipment',
        verbose_name='Техника',
        on_delete=models.PROTECT,
        related_name='shift_reading_corrections',
    )
    new_shift = models.ForeignKey(
        EmployeeShift,
        verbose_name='Новая смена',
        on_delete=models.PROTECT,
        related_name='reading_corrections',
    )
    previous_shift = models.ForeignKey(
        EmployeeShift,
        verbose_name='Предыдущая смена',
        on_delete=models.PROTECT,
        related_name='handover_corrections',
    )
    metric = models.CharField('Показатель', max_length=32, choices=Metric.choices)
    transferred_value = models.DecimalField('Переданное значение', max_digits=10, decimal_places=2)
    actual_value = models.DecimalField('Фактическое значение', max_digits=10, decimal_places=2)
    employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Сотрудник',
        on_delete=models.PROTECT,
        related_name='shift_reading_corrections',
    )
    corrected_at = models.DateTimeField('Скорректировано', auto_now_add=True)

    class Meta:
        verbose_name = 'Корректировка показаний при передаче смены'
        verbose_name_plural = 'Корректировки показаний при передаче смены'
        ordering = ['-corrected_at']
        constraints = [
            models.UniqueConstraint(
                fields=['new_shift', 'previous_shift', 'metric'],
                name='unique_shift_handover_metric_correction',
            ),
        ]


class AchievementPrize(models.Model):
    title = models.CharField('Название', max_length=128, default='План выполнен')
    image = models.ImageField('Призовая картинка', upload_to='achievement_prizes/')
    is_active = models.BooleanField('Активна', default=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        verbose_name = 'Приз за выполнение плана'
        verbose_name_plural = 'Призы за выполнение плана'
        ordering = ['-is_active', '-updated_at']

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_active:
            AchievementPrize.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)

    def __str__(self):
        return self.title


class AchievementUnlock(models.Model):
    user = models.ForeignKey(
        'users.Employee',
        verbose_name='Пользователь',
        on_delete=models.PROTECT,
        related_name='achievement_unlocks',
    )
    equipment = models.ForeignKey(
        'references.Equipment',
        verbose_name='Техника',
        on_delete=models.PROTECT,
        related_name='achievement_unlocks',
    )
    employee_shift = models.ForeignKey(
        EmployeeShift,
        verbose_name='Смена сотрудника',
        on_delete=models.PROTECT,
        related_name='achievement_unlocks',
    )
    prize = models.ForeignKey(
        AchievementPrize,
        verbose_name='Приз',
        on_delete=models.PROTECT,
        related_name='unlocks',
    )
    percent_at_unlock = models.DecimalField('Процент при разблокировке', max_digits=7, decimal_places=1)
    unlocked_at = models.DateTimeField('Разблокирована', auto_now_add=True)
    shown_at = models.DateTimeField('Показана', null=True, blank=True)

    class Meta:
        verbose_name = 'Разблокировка приза'
        verbose_name_plural = 'Разблокировки призов'
        ordering = ['-unlocked_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'equipment', 'employee_shift', 'prize'],
                name='unique_achievement_unlock_per_shift_prize',
            ),
        ]

    def __str__(self):
        return f'{self.user} / {self.equipment} / {self.prize}'


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
