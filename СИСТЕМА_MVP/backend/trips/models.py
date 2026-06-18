from django.db import models


class TripStatus(models.TextChoices):
    ACTIVE = 'active', 'Активный'
    COMPLETED = 'completed', 'Выполнен'
    CANCELLED = 'cancelled', 'Отменен'


class DispatcherActionType(models.TextChoices):
    SERVICE_CLOSE_SHIFT = 'service_close_shift', 'Служебное закрытие смены'
    CANCEL_ASSIGNMENT = 'cancel_assignment', 'Снятие назначения'
    CANCEL_TRIP = 'cancel_trip', 'Отмена рейса'
    COMPLETE_TRIP = 'complete_trip', 'Служебное завершение рейса'


class Trip(models.Model):
    excavator = models.ForeignKey('references.Equipment', verbose_name='Экскаватор', on_delete=models.PROTECT, related_name='excavator_trips')
    truck = models.ForeignKey('references.Equipment', verbose_name='Самосвал', on_delete=models.PROTECT, related_name='truck_trips')
    excavator_operator = models.ForeignKey('users.Employee', verbose_name='Машинист экскаватора', on_delete=models.PROTECT, related_name='excavator_trips', null=True, blank=True)
    driver = models.ForeignKey('users.Employee', verbose_name='Водитель', on_delete=models.PROTECT, related_name='driver_trips', null=True, blank=True)
    loading_shift = models.ForeignKey('shifts.EmployeeShift', verbose_name='Смена загрузки', on_delete=models.PROTECT, related_name='loaded_trips', null=True, blank=True)
    unloading_shift = models.ForeignKey('shifts.EmployeeShift', verbose_name='Смена разгрузки', on_delete=models.PROTECT, related_name='unloaded_trips', null=True, blank=True)
    rock_type = models.ForeignKey('references.RockType', verbose_name='Порода', on_delete=models.PROTECT)
    dump_point = models.ForeignKey('references.DumpPoint', verbose_name='Точка разгрузки', on_delete=models.PROTECT)
    planned_volume_m3 = models.DecimalField('Плановое задание, м3', max_digits=10, decimal_places=2, null=True, blank=True)
    volume_m3 = models.DecimalField('Объем, м3', max_digits=10, decimal_places=2, null=True, blank=True)
    tonnage = models.DecimalField('Тоннаж', max_digits=10, decimal_places=2, null=True, blank=True)
    loading_horizon = models.CharField('Горизонт погрузки', max_length=64, blank=True)
    loading_block = models.CharField('Блок', max_length=64, blank=True)
    transport_distance_km = models.DecimalField('Плечо транспортировки, км', max_digits=8, decimal_places=2, null=True, blank=True)
    downtime_text = models.CharField('Простои', max_length=255, blank=True)
    note = models.TextField('Примечание', blank=True)
    status = models.CharField('Статус', max_length=16, choices=TripStatus.choices, default=TripStatus.ACTIVE)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    completed_at = models.DateTimeField('Выполнен', null=True, blank=True)
    is_carryover = models.BooleanField('Переходящий рейс', default=False)

    class Meta:
        verbose_name = 'Рейс'
        verbose_name_plural = 'Рейсы'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.truck} -> {self.dump_point} ({self.rock_type})'


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
