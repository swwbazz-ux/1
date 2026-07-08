from django.db import models


class AssignmentStatus(models.TextChoices):
    PENDING = 'pending', 'Ожидает подтверждения'
    ACCEPTED = 'accepted', 'Принято'
    CANCELLED = 'cancelled', 'Отменено'


class EquipmentAssignment(models.Model):
    employee = models.ForeignKey('users.Employee', verbose_name='Сотрудник', on_delete=models.PROTECT)
    equipment = models.ForeignKey('references.Equipment', verbose_name='Техника', on_delete=models.PROTECT)
    shift = models.ForeignKey('shifts.EmployeeShift', verbose_name='Смена сотрудника', on_delete=models.PROTECT, null=True, blank=True)
    assigned_by = models.ForeignKey('users.Employee', verbose_name='Кто назначил', on_delete=models.PROTECT, related_name='created_equipment_assignments', null=True, blank=True)
    status = models.CharField('Статус', max_length=16, choices=AssignmentStatus.choices, default=AssignmentStatus.PENDING)
    assigned_at = models.DateTimeField('Назначено', auto_now_add=True)
    accepted_at = models.DateTimeField('Принято', null=True, blank=True)
    ended_at = models.DateTimeField('Завершено', null=True, blank=True)

    class Meta:
        verbose_name = 'Назначение сотрудника на технику'
        verbose_name_plural = 'Назначения сотрудников на технику'
        ordering = ['-assigned_at']

    def __str__(self):
        return f'{self.employee} -> {self.equipment}'


class HaulAssignment(models.Model):
    excavator = models.ForeignKey('references.Equipment', verbose_name='Экскаватор', on_delete=models.PROTECT, related_name='excavator_haul_assignments')
    truck = models.ForeignKey('references.Equipment', verbose_name='Самосвал', on_delete=models.PROTECT, related_name='truck_haul_assignments')
    assigned_by = models.ForeignKey('users.Employee', verbose_name='Кто назначил', on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField('Статус', max_length=16, choices=AssignmentStatus.choices, default=AssignmentStatus.PENDING)
    assigned_at = models.DateTimeField('Назначено', auto_now_add=True)
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
