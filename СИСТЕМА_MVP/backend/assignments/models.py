from django.db import models


class AssignmentStatus(models.TextChoices):
    PENDING = 'pending', 'Ожидает подтверждения'
    ACCEPTED = 'accepted', 'Принято'
    CANCELLED = 'cancelled', 'Отменено'


class WorkShiftType(models.TextChoices):
    SHIFT_1 = 'day', 'Смена 1 · 07:00–19:00'
    SHIFT_2 = 'night', 'Смена 2 · 19:00–07:00'


class HaulAssignmentAction(models.TextChoices):
    ASSIGN = 'assign', 'Назначить'
    RELEASE = 'release', 'Снять назначение'


class EquipmentAssignment(models.Model):
    employee = models.ForeignKey('users.Employee', verbose_name='Сотрудник', on_delete=models.PROTECT)
    role = models.ForeignKey(
        'users.Role',
        verbose_name='Рабочая роль',
        on_delete=models.PROTECT,
        related_name='equipment_assignments',
        null=True,
        blank=True,
    )
    equipment = models.ForeignKey('references.Equipment', verbose_name='Техника', on_delete=models.PROTECT)
    shift_type = models.CharField(
        'Рабочая смена',
        max_length=16,
        choices=WorkShiftType.choices,
        null=True,
        blank=True,
    )
    shift = models.ForeignKey('shifts.EmployeeShift', verbose_name='Смена сотрудника', on_delete=models.PROTECT, null=True, blank=True)
    assigned_by = models.ForeignKey('users.Employee', verbose_name='Кто назначил', on_delete=models.PROTECT, related_name='created_equipment_assignments', null=True, blank=True)
    status = models.CharField('Статус', max_length=16, choices=AssignmentStatus.choices, default=AssignmentStatus.PENDING)
    assigned_at = models.DateTimeField('Назначено', auto_now_add=True)
    accepted_at = models.DateTimeField('Принято', null=True, blank=True)
    ended_at = models.DateTimeField('Завершено', null=True, blank=True)
    ended_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто завершил',
        on_delete=models.PROTECT,
        related_name='ended_equipment_assignments',
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = 'Назначение сотрудника на технику'
        verbose_name_plural = 'Назначения сотрудников на технику'
        ordering = ['-assigned_at']
        constraints = [
            models.UniqueConstraint(
                fields=['employee'],
                condition=models.Q(
                    status=AssignmentStatus.ACCEPTED,
                    ended_at__isnull=True,
                    shift__isnull=True,
                    role__isnull=False,
                    shift_type__isnull=False,
                ),
                name='unique_active_employee_equipment_assignment',
            ),
            models.UniqueConstraint(
                fields=['equipment', 'shift_type'],
                condition=models.Q(
                    status=AssignmentStatus.ACCEPTED,
                    ended_at__isnull=True,
                    shift__isnull=True,
                    role__isnull=False,
                    shift_type__isnull=False,
                ),
                name='unique_active_equipment_work_shift',
            ),
        ]

    @property
    def work_shift_label(self):
        if self.shift_type == WorkShiftType.SHIFT_1:
            return 'Смена 1'
        if self.shift_type == WorkShiftType.SHIFT_2:
            return 'Смена 2'
        return 'Смена не указана'

    def __str__(self):
        return f'{self.employee} -> {self.equipment}'


class HaulAssignment(models.Model):
    excavator = models.ForeignKey('references.Equipment', verbose_name='Экскаватор', on_delete=models.PROTECT, related_name='excavator_haul_assignments')
    truck = models.ForeignKey('references.Equipment', verbose_name='Самосвал', on_delete=models.PROTECT, related_name='truck_haul_assignments')
    assigned_by = models.ForeignKey('users.Employee', verbose_name='Кто назначил', on_delete=models.PROTECT, null=True, blank=True)
    action = models.CharField('Действие', max_length=16, choices=HaulAssignmentAction.choices, default=HaulAssignmentAction.ASSIGN)
    status = models.CharField('Статус', max_length=16, choices=AssignmentStatus.choices, default=AssignmentStatus.PENDING)
    assigned_at = models.DateTimeField('Назначено', auto_now_add=True)
    effective_at = models.DateTimeField('Вступает в силу', null=True, blank=True)
    accepted_at = models.DateTimeField('Принято водителем', null=True, blank=True)
    ended_at = models.DateTimeField('Завершено', null=True, blank=True)

    class Meta:
        verbose_name = 'Назначение самосвала под экскаватор'
        verbose_name_plural = 'Назначения самосвалов под экскаваторы'
        ordering = ['-assigned_at']

    def __str__(self):
        return f'{self.truck} под {self.excavator}'

class ExcavatorPlacement(models.Model):
    class Zone(models.TextChoices):
        ACTIVE = 'active', 'Активная смена'
        INACTIVE = 'inactive', 'Неактивная смена'

    excavator = models.OneToOneField(
        'references.Equipment',
        verbose_name='Экскаватор',
        on_delete=models.CASCADE,
        related_name='excavator_placement',
    )
    zone = models.CharField('Зона', max_length=16, choices=Zone.choices, default=Zone.INACTIVE)
    work_rock_type = models.ForeignKey(
        'references.RockType',
        verbose_name='Порода текущего забоя',
        on_delete=models.SET_NULL,
        related_name='excavator_work_placements',
        null=True,
        blank=True,
    )
    work_dump_point = models.ForeignKey(
        'references.DumpPoint',
        verbose_name='Основная точка разгрузки',
        on_delete=models.SET_NULL,
        related_name='excavator_work_placements',
        null=True,
        blank=True,
    )
    loading_horizon = models.CharField('Горизонт погрузки', max_length=64, blank=True)
    loading_block = models.CharField('Блок погрузки', max_length=64, blank=True)
    work_context_updated_at = models.DateTimeField('Контекст забоя обновлен', null=True, blank=True)
    changed_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто изменил',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    changed_at = models.DateTimeField('Изменено', auto_now=True)

    class Meta:
        verbose_name = 'Размещение экскаватора'
        verbose_name_plural = 'Размещения экскаваторов'
        ordering = ['excavator__garage_number']

    def __str__(self):
        return f'{self.excavator} / {self.get_zone_display()}'


# Create your models here.
