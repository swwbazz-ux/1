from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class PersonnelDepartment(models.Model):
    """Official organizational unit imported from 1C."""

    code = models.SlugField('Код подразделения', max_length=64, unique=True)
    name = models.CharField('Подразделение', max_length=255, unique=True)
    is_active = models.BooleanField('Активно', default=True)

    class Meta:
        verbose_name = 'Подразделение'
        verbose_name_plural = 'Подразделения'
        ordering = ['name']

    def __str__(self):
        return self.name


class WorkSchedule(models.Model):
    """Standard personnel work schedule; the brigade is stored on Employee."""

    code = models.SlugField('Код графика', max_length=64, unique=True)
    name = models.CharField('График работы', max_length=255, unique=True)
    brigade_count = models.PositiveSmallIntegerField(
        'Количество бригад',
        default=2,
        validators=[MinValueValidator(0), MaxValueValidator(4)],
    )
    is_active = models.BooleanField('Активен', default=True)

    class Meta:
        verbose_name = 'График работы'
        verbose_name_plural = 'Графики работы'
        ordering = ['name']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(brigade_count__gte=0, brigade_count__lte=4),
                name='work_schedule_brigade_count_0_4',
            ),
        ]

    def __str__(self):
        return self.name


class Employee(models.Model):
    class BrigadeNumber(models.IntegerChoices):
        BRIGADE_1 = 1, 'Бригада №1'
        BRIGADE_2 = 2, 'Бригада №2'
        BRIGADE_3 = 3, 'Бригада №3'
        BRIGADE_4 = 4, 'Бригада №4'

    class WorkCategory(models.TextChoices):
        DRIVER = 'driver', 'Водитель самосвала'
        EXCAVATOR_OPERATOR = 'excavator_operator', 'Машинист экскаватора'
        OTHER = 'other', 'Без привязки к технике'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Активен'
        NOT_ACTIVATED = 'not_activated', 'Не активирован'
        DEACTIVATED = 'deactivated', 'Деактивирован'
        ARCHIVED = 'archived', 'В архиве'
        DISMISSED = 'dismissed', 'Уволен'
        DELETED = 'deleted', 'Удален'

    full_name = models.CharField('ФИО', max_length=255)
    birth_date = models.DateField('Дата рождения', null=True, blank=True)
    personnel_position = models.ForeignKey(
        'PersonnelPosition',
        verbose_name='Кадровая должность',
        on_delete=models.PROTECT,
        related_name='employees',
        null=True,
        blank=True,
    )
    base_specialization = models.ForeignKey(
        'ProductionSpecialization',
        verbose_name='Базовая производственная специализация',
        on_delete=models.PROTECT,
        related_name='base_employees',
        null=True,
        blank=True,
    )
    position = models.CharField('Должность', max_length=128, blank=True)
    department = models.CharField('Подразделение', max_length=160, blank=True)
    personnel_department = models.ForeignKey(
        PersonnelDepartment,
        verbose_name='Подразделение',
        on_delete=models.PROTECT,
        related_name='employees',
        null=True,
        blank=True,
    )
    work_category = models.CharField(
        'Рабочая категория',
        max_length=32,
        choices=WorkCategory.choices,
        default=WorkCategory.OTHER,
    )
    personnel_number = models.CharField('Табельный номер', max_length=64, blank=True)
    phone = models.CharField('Телефон', max_length=32, blank=True)
    status = models.CharField('Статус', max_length=32, choices=Status.choices, default=Status.NOT_ACTIVATED)
    comment = models.TextField('Комментарий', blank=True)
    hired_at = models.DateField('Дата приема', null=True, blank=True)
    dismissed_at = models.DateField('Дата увольнения', null=True, blank=True)
    rotation = models.CharField('Вахта', max_length=128, blank=True)
    work_schedule = models.ForeignKey(
        WorkSchedule,
        verbose_name='График работы',
        on_delete=models.PROTECT,
        related_name='employees',
        null=True,
        blank=True,
    )
    brigade_number = models.PositiveSmallIntegerField(
        'Бригада',
        choices=BrigadeNumber.choices,
        null=True,
        blank=True,
    )
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
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(brigade_number__isnull=True)
                    | models.Q(brigade_number__gte=1, brigade_number__lte=4)
                ),
                name='employee_brigade_number_1_4',
            ),
        ]

    def __str__(self):
        return self.full_name

    @property
    def department_label(self):
        return self.personnel_department.name if self.personnel_department_id else self.department

    @property
    def work_schedule_label(self):
        return self.work_schedule.name if self.work_schedule_id else self.rotation

    def has_production_history(self):
        from assignments.models import CrewPlan, CrewPlanSlot, EquipmentAssignment, HaulAssignment
        from downtimes.models import DowntimeEvent
        from rotations.models import (
            RotationActionLog,
            RotationCollectionCycle,
            RotationResponse,
            WatchExtensionCase,
        )
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
            CrewPlanSlot.objects.filter(employee=self).exists(),
            CrewPlanSlot.objects.filter(baseline_employee=self).exists(),
            CrewPlan.objects.filter(created_by=self).exists(),
            CrewPlan.objects.filter(updated_by=self).exists(),
            CrewPlan.objects.filter(published_by=self).exists(),
            HaulAssignment.objects.filter(assigned_by=self).exists(),
            DispatcherActionLog.objects.filter(actor=self).exists(),
            RotationCollectionCycle.objects.filter(created_by=self).exists(),
            RotationCollectionCycle.objects.filter(opened_by=self).exists(),
            RotationCollectionCycle.objects.filter(closed_by=self).exists(),
            RotationResponse.objects.filter(employee=self).exists(),
            RotationResponse.objects.filter(submitted_by=self).exists(),
            WatchExtensionCase.objects.filter(decision_by=self).exists(),
            WatchExtensionCase.objects.filter(documentation_by=self).exists(),
            RotationActionLog.objects.filter(actor=self).exists(),
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


class ProductionSpecialization(models.Model):
    """Operational specialization used for equipment eligibility and app access."""

    code = models.SlugField('Код специализации', max_length=64, unique=True)
    name = models.CharField('Производственная специализация', max_length=160, unique=True)
    equipment_type = models.ForeignKey(
        'references.EquipmentType',
        verbose_name='Тип техники',
        on_delete=models.PROTECT,
        related_name='production_specializations',
        null=True,
        blank=True,
    )
    access_role = models.ForeignKey(
        Role,
        verbose_name='Роль приложения',
        on_delete=models.PROTECT,
        related_name='production_specializations',
        null=True,
        blank=True,
    )
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Производственная специализация'
        verbose_name_plural = 'Производственные специализации'
        ordering = ['name']

    def __str__(self):
        return self.name


class PersonnelPosition(models.Model):
    """Official personnel position imported from 1C or selected in the employee card."""

    code = models.SlugField('Код должности', max_length=96, unique=True)
    name = models.CharField('Кадровая должность', max_length=255, unique=True)
    requires_specialization = models.BooleanField(
        'Требует производственную специализацию',
        default=False,
    )
    allowed_specializations = models.ManyToManyField(
        ProductionSpecialization,
        verbose_name='Разрешенные производственные специализации',
        related_name='personnel_positions',
        blank=True,
    )
    default_specialization = models.ForeignKey(
        ProductionSpecialization,
        verbose_name='Специализация по умолчанию',
        on_delete=models.SET_NULL,
        related_name='default_for_personnel_positions',
        null=True,
        blank=True,
    )
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Кадровая должность'
        verbose_name_plural = 'Кадровые должности'
        ordering = ['name']

    def __str__(self):
        return self.name


class TemporaryWorkTransfer(models.Model):
    """OUP-approved temporary specialization change, bounded by a watch period."""

    class Status(models.TextChoices):
        REQUESTED = 'requested', 'Запрошен'
        APPROVED = 'approved', 'Одобрен'
        REJECTED = 'rejected', 'Отклонен'
        CANCELLED = 'cancelled', 'Отменен'
        EXPIRED = 'expired', 'Завершен по окончании вахты'

    employee = models.ForeignKey(
        Employee,
        verbose_name='Сотрудник',
        on_delete=models.PROTECT,
        related_name='temporary_work_transfers',
    )
    source_specialization = models.ForeignKey(
        ProductionSpecialization,
        verbose_name='Исходная специализация',
        on_delete=models.PROTECT,
        related_name='outgoing_temporary_transfers',
        null=True,
        blank=True,
    )
    target_specialization = models.ForeignKey(
        ProductionSpecialization,
        verbose_name='Целевая специализация',
        on_delete=models.PROTECT,
        related_name='incoming_temporary_transfers',
    )
    watch_period = models.ForeignKey(
        'shifts.WatchPeriod',
        verbose_name='Вахта',
        on_delete=models.PROTECT,
        related_name='temporary_work_transfers',
    )
    effective_from = models.DateField('Действует с')
    effective_to = models.DateField('Действует по')
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.REQUESTED,
        db_index=True,
    )
    reason = models.TextField('Причина запроса', blank=True)
    review_comment = models.TextField('Комментарий ОУП', blank=True)
    requested_by = models.ForeignKey(
        Employee,
        verbose_name='Кто запросил',
        on_delete=models.SET_NULL,
        related_name='requested_temporary_work_transfers',
        null=True,
        blank=True,
    )
    requested_at = models.DateTimeField('Запрошен', auto_now_add=True)
    reviewed_by = models.ForeignKey(
        Employee,
        verbose_name='Кто рассмотрел',
        on_delete=models.SET_NULL,
        related_name='reviewed_temporary_work_transfers',
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField('Рассмотрен', null=True, blank=True)
    closed_at = models.DateTimeField('Завершен', null=True, blank=True)

    class Meta:
        verbose_name = 'Временный производственный перевод'
        verbose_name_plural = 'Временные производственные переводы'
        ordering = ['-requested_at']
        indexes = [
            models.Index(fields=['employee', 'status'], name='tmp_transfer_emp_status_idx'),
            models.Index(fields=['status', 'effective_to'], name='temp_transfer_status_end_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(effective_to__gte=models.F('effective_from')),
                name='temp_transfer_dates_valid',
            ),
        ]

    def __str__(self):
        return f'{self.employee}: {self.target_specialization}'


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
    action_code = models.CharField('Код действия', max_length=64, blank=True, db_index=True)
    object_type = models.CharField('Тип объекта', max_length=128, blank=True)
    object_id = models.CharField('ID объекта', max_length=64, blank=True)
    object_repr = models.CharField('Объект', max_length=255, blank=True)
    old_value = models.TextField('Старое значение', blank=True)
    new_value = models.TextField('Новое значение', blank=True)
    comment = models.TextField('Комментарий', blank=True)
    undo_payload = models.JSONField('Снимок для отмены', default=dict, blank=True)
    reversal_of = models.OneToOneField(
        'self',
        verbose_name='Отмененное действие',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='reversal',
    )

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
