from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


from .protected_cards import guard_access_write, guard_employee_write

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


class WatchComposition(models.Model):
    """Approved personnel roster used as the structural watch identity."""

    code = models.SlugField('Код состава вахты', max_length=64, unique=True)
    name = models.CharField('Утверждённый состав вахты', max_length=160, unique=True)
    is_active = models.BooleanField('Активен', default=True)

    class Meta:
        verbose_name = 'Утверждённый состав вахты'
        verbose_name_plural = 'Утверждённые составы вахт'
        ordering = ['name']

    def __str__(self):
        return self.name


class EmployeeWatchProfileQuerySet(models.QuerySet):
    PROTECTED_FIELDS = frozenset({
        'work_schedule',
        'work_schedule_id',
        'brigade_number',
        'watch_composition',
        'watch_composition_id',
        'rotation',
    })
    WRITE_FORBIDDEN_MESSAGE = (
        'График, бригаду и состав вахты действующего сотрудника '
        'может изменить только Табельщик.'
    )
    WRITE_FORBIDDEN_CODE = 'users.employee.watch_profile_immutable'

    @classmethod
    def _raise_if_protected(cls, field_names):
        if cls.PROTECTED_FIELDS.intersection(field_names):
            raise ValidationError(
                cls.WRITE_FORBIDDEN_MESSAGE,
                code=cls.WRITE_FORBIDDEN_CODE,
            )

    def update(self, **kwargs):
        self._raise_if_protected(kwargs)
        self._raise_if_card_protected()
        return super().update(**kwargs)

    def _raise_if_card_protected(self):
        from .protected_cards import protected_writes_allowed, raise_protected

        if protected_writes_allowed():
            return
        if self.filter(is_protected=True).exists():
            raise_protected()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_if_protected(fields)
        return super().bulk_update(objs, fields, batch_size=batch_size)


class EmployeeWatchProfileManager(
    models.Manager.from_queryset(EmployeeWatchProfileQuerySet)
):
    pass


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

    class Sex(models.TextChoices):
        UNKNOWN = 'unknown', 'Не указан'
        MALE = 'male', 'Мужской'
        FEMALE = 'female', 'Женский'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Активен'
        NOT_ACTIVATED = 'not_activated', 'Не активирован'
        DEACTIVATED = 'deactivated', 'Деактивирован'
        ARCHIVED = 'archived', 'В архиве'
        DISMISSED = 'dismissed', 'Уволен'
        DELETED = 'deleted', 'Удален'

    objects = EmployeeWatchProfileManager()

    full_name = models.CharField('ФИО', max_length=255)
    birth_date = models.DateField('Дата рождения', null=True, blank=True)
    sex = models.CharField(
        'Пол',
        max_length=7,
        choices=Sex.choices,
        default=Sex.UNKNOWN,
    )
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
    watch_composition = models.ForeignKey(
        WatchComposition,
        verbose_name='Утверждённый состав вахты',
        on_delete=models.PROTECT,
        related_name='employees',
        null=True,
        blank=True,
    )
    residence_text = models.CharField('Место проживания', max_length=255, blank=True)
    hr_data = models.TextField('Паспортные/кадровые данные', blank=True)
    photo = models.FileField('Фото сотрудника', upload_to='employee_photos/', blank=True)
    is_active = models.BooleanField('Активен', default=True)
    # Карточка владельца системы: её не должен переписать ни массовый импорт
    # из отдела кадров, ни администратор по ошибке. Иначе войти и всё
    # починить будет уже неоткуда.
    # db_default, а не только default: без значения по умолчанию в самой базе
    # столбец остаётся обязательным и пустым для любой вставки, которая о нём
    # не знает — старой миграции, отката, внешнего скрипта. На этом уже
    # спотыкались 25 августа с другими столбцами.
    is_protected = models.BooleanField(
        'Защищённая карточка', default=False, db_default=False,
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        verbose_name = 'Сотрудник'
        verbose_name_plural = 'Сотрудники'
        ordering = ['full_name']
        base_manager_name = 'objects'
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(brigade_number__isnull=True)
                    | models.Q(brigade_number__gte=1, brigade_number__lte=4)
                ),
                name='employee_brigade_number_1_4',
            ),
            models.CheckConstraint(
                condition=models.Q(sex__in=['unknown', 'male', 'female']),
                name='employee_sex_valid',
            ),
        ]

    def delete(self, *args, **kwargs):
        guard_employee_write(self.pk, type(self))
        return super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        guard_employee_write(self.pk, type(self))
        using = kwargs.get('using')
        force_insert = kwargs.get('force_insert', False)
        update_fields = kwargs.get('update_fields')
        persisted = None
        if self.pk is not None:
            persisted = (
                type(self)._base_manager
                .using(using)
                .filter(pk=self.pk)
                .values(
                    'work_schedule_id',
                    'brigade_number',
                    'watch_composition_id',
                    'rotation',
                )
                .first()
            )

        if force_insert or persisted is None:
            return super().save(*args, **kwargs)

        protected_values = {
            'work_schedule_id': self.work_schedule_id,
            'brigade_number': self.brigade_number,
            'watch_composition_id': self.watch_composition_id,
            'rotation': self.rotation,
        }
        dirty_protected_fields = {
            field_name
            for field_name, value in protected_values.items()
            if value != persisted[field_name]
        }
        if update_fields is None:
            attempted_protected_fields = dirty_protected_fields
        else:
            normalized_update_fields = set(update_fields)
            if {'work_schedule', 'work_schedule_id'} & normalized_update_fields:
                normalized_update_fields.add('work_schedule_id')
            if {'watch_composition', 'watch_composition_id'} & normalized_update_fields:
                normalized_update_fields.add('watch_composition_id')
            attempted_protected_fields = dirty_protected_fields & normalized_update_fields

        if attempted_protected_fields:
            raise ValidationError(
                EmployeeWatchProfileQuerySet.WRITE_FORBIDDEN_MESSAGE,
                code=EmployeeWatchProfileQuerySet.WRITE_FORBIDDEN_CODE,
            )

        if update_fields is not None:
            for field_name in dirty_protected_fields:
                setattr(self, field_name, persisted[field_name])

        return super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name

    @property
    def has_photo_file(self):
        """Фото записано в карточке — это ещё не значит, что файл на месте.

        Имя файла остаётся в базе и после того, как сам файл пропал, и тогда
        вместо снимка показывался значок битой картинки. Проверяем наличие,
        а не только заполненность поля.
        """
        if not self.photo:
            return False
        try:
            return self.photo.storage.exists(self.photo.name)
        except Exception:
            # Из-за недоступного хранилища страница падать не должна:
            # без фотографии она читается, с ошибкой — нет.
            return False

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
            CrewPlanSlot.objects.filter(secondary_employee=self).exists(),
            CrewPlanSlot.objects.filter(baseline_employee=self).exists(),
            CrewPlanSlot.objects.filter(baseline_secondary_employee=self).exists(),
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

    def save(self, *args, **kwargs):
        # Доступ — часть защищённой карточки: сняв его, владельца запрут снаружи.
        guard_access_write(self.employee_id, Employee, kwargs.get('update_fields'))
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        guard_access_write(self.employee_id, Employee)
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f'{self.employee} - {self.role}'


class ActiveApplicationSession(models.Model):
    session_key = models.CharField('Ключ сессии', max_length=40, unique=True)
    access = models.ForeignKey(
        EmployeeAccess,
        verbose_name='Доступ сотрудника',
        on_delete=models.CASCADE,
        related_name='application_sessions',
    )
    role_code = models.CharField('Роль', max_length=64, db_index=True)
    app_code = models.CharField('Приложение', max_length=64, db_index=True)
    path = models.CharField('Текущий экран', max_length=255, blank=True)
    device_kind = models.CharField('Тип устройства', max_length=16, blank=True)
    first_seen_at = models.DateTimeField('Первое обращение', auto_now_add=True)
    last_seen_at = models.DateTimeField('Последняя активность', db_index=True)

    class Meta:
        verbose_name = 'Активная сессия приложения'
        verbose_name_plural = 'Активные сессии приложений'
        ordering = ['-last_seen_at']
        indexes = [
            models.Index(fields=['app_code', 'last_seen_at'], name='app_session_app_seen_idx'),
            models.Index(fields=['access', 'last_seen_at'], name='app_session_access_seen_idx'),
        ]

    def __str__(self):
        return f'{self.access} / {self.app_code}'


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


class ClientErrorReport(models.Model):
    """Падение, случившееся на телефоне сотрудника.

    Собирается только для разбора полевого теста: без этого сломанный экран
    у человека остаётся невидимым, пока он сам не напишет.
    """

    employee = models.ForeignKey('Employee', verbose_name='Сотрудник', on_delete=models.SET_NULL, null=True, blank=True, related_name='client_errors')
    role_code = models.CharField('Роль', max_length=64, blank=True, db_index=True)
    app_version = models.CharField('Версия приложения', max_length=64, blank=True)
    screen = models.CharField('Экран', max_length=120, blank=True)
    message = models.CharField('Ошибка', max_length=500)
    source = models.CharField('Файл', max_length=300, blank=True)
    stack = models.TextField('Стек', blank=True)
    user_agent = models.CharField('Телефон и браузер', max_length=300, blank=True)
    happened_at = models.DateTimeField('Когда', auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Ошибка на телефоне'
        verbose_name_plural = 'Ошибки на телефонах'
        ordering = ['-happened_at']

    def __str__(self):
        return f'{self.happened_at:%d.%m %H:%M} {self.role_code}: {self.message[:60]}'


class WebPushSubscription(models.Model):
    """Телефон сотрудника, подписанный на уведомления.

    У одного человека может быть несколько устройств, поэтому уникален адрес
    подписки, а не сотрудник.
    """

    employee = models.ForeignKey(
        Employee,
        verbose_name='Сотрудник',
        on_delete=models.CASCADE,
        related_name='push_subscriptions',
    )
    endpoint = models.URLField('Адрес подписки', max_length=500, unique=True)
    p256dh = models.CharField('Ключ устройства', max_length=200, blank=True)
    auth = models.CharField('Секрет устройства', max_length=100, blank=True)
    role_code = models.CharField('Роль', max_length=64, blank=True)
    user_agent = models.CharField('Устройство', max_length=300, blank=True)
    is_active = models.BooleanField('Активна', default=True)
    failure_count = models.PositiveSmallIntegerField('Неудач подряд', default=0)
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    last_success_at = models.DateTimeField('Последняя доставка', null=True, blank=True)

    class Meta:
        verbose_name = 'Подписка на уведомления'
        verbose_name_plural = 'Подписки на уведомления'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['employee', 'is_active']),
        ]

    def __str__(self):
        return f'{self.employee} / {self.endpoint[:40]}'


class PushNotification(models.Model):
    """Текст уведомления, который телефон забирает после сигнала.

    Сам push отправляется пустым, поэтому содержимое хранится здесь и выдаётся
    приложению уже по защищённому сеансу.
    """

    employee = models.ForeignKey(
        Employee,
        verbose_name='Сотрудник',
        on_delete=models.CASCADE,
        related_name='push_notifications',
    )
    title = models.CharField('Заголовок', max_length=120)
    body = models.CharField('Текст', max_length=300, blank=True)
    url = models.CharField('Куда открыть', max_length=300, blank=True)
    tag = models.CharField('Метка замены', max_length=64, blank=True)
    kind = models.CharField('Событие', max_length=64, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    shown_at = models.DateTimeField('Показано', null=True, blank=True)

    class Meta:
        verbose_name = 'Push-уведомление'
        verbose_name_plural = 'Push-уведомления'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['employee', 'shown_at']),
        ]

    def __str__(self):
        return f'{self.employee}: {self.title}'
