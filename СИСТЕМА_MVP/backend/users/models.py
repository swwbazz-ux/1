from django.db import models


class Employee(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Активен'
        NOT_ACTIVATED = 'not_activated', 'Не активирован'
        DEACTIVATED = 'deactivated', 'Деактивирован'
        ARCHIVED = 'archived', 'В архиве'
        DISMISSED = 'dismissed', 'Уволен'
        DELETED = 'deleted', 'Удален'

    full_name = models.CharField('ФИО', max_length=255)
    position = models.CharField('Должность', max_length=128, blank=True)
    personnel_number = models.CharField('Табельный номер', max_length=64, blank=True)
    phone = models.CharField('Телефон', max_length=32, blank=True)
    status = models.CharField('Статус', max_length=32, choices=Status.choices, default=Status.NOT_ACTIVATED)
    comment = models.TextField('Комментарий', blank=True)
    hired_at = models.DateField('Дата приема', null=True, blank=True)
    dismissed_at = models.DateField('Дата увольнения', null=True, blank=True)
    rotation = models.CharField('Вахта', max_length=128, blank=True)
    residence_text = models.CharField('Место проживания', max_length=255, blank=True)
    hr_data = models.TextField('Паспортные/кадровые данные', blank=True)
    photo = models.FileField('Фото сотрудника', upload_to='employee_photos/', blank=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        verbose_name = 'Сотрудник'
        verbose_name_plural = 'Сотрудники'
        ordering = ['full_name']

    def __str__(self):
        return self.full_name

    def has_production_history(self):
        from assignments.models import EquipmentAssignment, HaulAssignment
        from downtimes.models import DowntimeEvent
        from shifts.models import EmployeeShift
        from trips.models import DispatcherActionLog, Trip

        return any([
            EmployeeShift.objects.filter(employee=self).exists(),
            Trip.objects.filter(excavator_operator=self).exists(),
            Trip.objects.filter(driver=self).exists(),
            DowntimeEvent.objects.filter(employee=self).exists(),
            EquipmentAssignment.objects.filter(employee=self).exists(),
            EquipmentAssignment.objects.filter(assigned_by=self).exists(),
            EquipmentAssignment.objects.filter(ended_by=self).exists(),
            HaulAssignment.objects.filter(assigned_by=self).exists(),
            DispatcherActionLog.objects.filter(actor=self).exists(),
        ])


class Role(models.Model):
    code = models.SlugField('Код роли', max_length=64, unique=True)
    name = models.CharField('Название роли', max_length=128)
    description = models.TextField('Описание', blank=True)
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Роль'
        verbose_name_plural = 'Роли'
        ordering = ['name']

    def __str__(self):
        return self.name


class EmployeeAccess(models.Model):
    class Status(models.TextChoices):
        NOT_ACTIVATED = 'not_activated', 'Не активирован'
        ACTIVATED = 'activated', 'Активирован'
        BLOCKED = 'blocked', 'Заблокирован'
        DEACTIVATED = 'deactivated', 'Деактивирован'

    employee = models.ForeignKey(Employee, verbose_name='Сотрудник', on_delete=models.CASCADE, related_name='accesses')
    role = models.ForeignKey(Role, verbose_name='Роль', on_delete=models.PROTECT, related_name='accesses')
    access_code = models.CharField('Код доступа', max_length=128)
    status = models.CharField('Статус доступа', max_length=32, choices=Status.choices, default=Status.NOT_ACTIVATED)
    primary_code_issued_at = models.DateTimeField('Первичный пинкод выдан', null=True, blank=True)
    activated_at = models.DateTimeField('Активирован', null=True, blank=True)
    last_login_at = models.DateTimeField('Последний вход', null=True, blank=True)
    blocked_at = models.DateTimeField('Заблокирован', null=True, blank=True)
    block_reason = models.TextField('Причина блокировки', blank=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    deactivated_at = models.DateTimeField('Отключен', null=True, blank=True)

    class Meta:
        verbose_name = 'Доступ сотрудника'
        verbose_name_plural = 'Доступы сотрудников'
        ordering = ['employee__full_name', 'role__name']

    def __str__(self):
        return f'{self.employee} - {self.role}'


class AdminActionLog(models.Model):
    created_at = models.DateTimeField('Дата и время', auto_now_add=True)
    actor = models.ForeignKey(Employee, verbose_name='Кто выполнил', on_delete=models.SET_NULL, null=True, blank=True, related_name='admin_actions')
    action = models.CharField('Действие', max_length=128)
    object_type = models.CharField('Тип объекта', max_length=128, blank=True)
    object_repr = models.CharField('Объект', max_length=255, blank=True)
    old_value = models.TextField('Старое значение', blank=True)
    new_value = models.TextField('Новое значение', blank=True)
    comment = models.TextField('Комментарий', blank=True)

    class Meta:
        verbose_name = 'Журнал действия администратора'
        verbose_name_plural = 'Журнал действий администратора'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.created_at:%d.%m.%Y %H:%M} / {self.action}'


class AdminConflict(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Открыт'
        IN_PROGRESS = 'in_progress', 'В работе'
        RESOLVED = 'resolved', 'Решен'
        REJECTED = 'rejected', 'Отклонен'

    created_at = models.DateTimeField('Дата и время', auto_now_add=True)
    employee = models.ForeignKey(Employee, verbose_name='Сотрудник', on_delete=models.SET_NULL, null=True, blank=True, related_name='admin_conflicts')
    role = models.ForeignKey(Role, verbose_name='Роль', on_delete=models.SET_NULL, null=True, blank=True)
    conflict_type = models.CharField('Тип конфликта', max_length=128)
    process = models.CharField('Процесс', max_length=128, blank=True)
    description = models.TextField('Описание')
    status = models.CharField('Статус', max_length=32, choices=Status.choices, default=Status.OPEN)
    resolved_by = models.ForeignKey(Employee, verbose_name='Кто разобрал', on_delete=models.SET_NULL, null=True, blank=True, related_name='resolved_admin_conflicts')
    resolved_at = models.DateTimeField('Дата разбора', null=True, blank=True)
    comment = models.TextField('Комментарий', blank=True)

    class Meta:
        verbose_name = 'Административный конфликт'
        verbose_name_plural = 'Административные конфликты'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.conflict_type}: {self.status}'


class DriverPrimaryRegistration(models.Model):
    employee = models.OneToOneField(Employee, verbose_name='Водитель', on_delete=models.CASCADE, related_name='driver_registration')
    dormitory_section = models.ForeignKey('references.DormitorySection', verbose_name='Секция проживания', on_delete=models.PROTECT)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Первичная регистрация водителя'
        verbose_name_plural = 'Первичные регистрации водителей'
        ordering = ['employee__full_name']

    def __str__(self):
        return f'{self.employee} / {self.dormitory_section}'

# Create your models here.
