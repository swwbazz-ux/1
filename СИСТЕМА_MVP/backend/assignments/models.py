from django.core.exceptions import ValidationError
from django.db import models, transaction


class AssignmentStatus(models.TextChoices):
    PENDING = 'pending', 'Ожидает подтверждения'
    ACCEPTED = 'accepted', 'Принято'
    CANCELLED = 'cancelled', 'Отменено'


class WorkShiftType(models.TextChoices):
    SHIFT_1 = 'day', 'Первая смена'
    SHIFT_2 = 'night', 'Вторая смена'


class CrewPlanStatus(models.TextChoices):
    DRAFT = 'draft', 'Черновик'
    PUBLISHED = 'published', 'Опубликован'
    SUPERSEDED = 'superseded', 'Заменен новой публикацией'


class HaulAssignmentAction(models.TextChoices):
    ASSIGN = 'assign', 'Назначить'
    RELEASE = 'release', 'Снять назначение'


class HaulAssignmentHandoffStatus(models.TextChoices):
    OPEN = 'open', 'Можно завершить погрузку'
    RESOLVED = 'resolved', 'Завершено рейсом'
    EXPIRED = 'expired', 'Смена завершена'


class EquipmentAssignmentQuerySet(models.QuerySet):
    PROVENANCE_FIELDS = {
        'source_kind',
        'source_crew_plan_slot',
        'source_crew_plan_slot_id',
    }

    def update(self, **kwargs):
        if self.PROVENANCE_FIELDS.intersection(kwargs):
            raise ValidationError(
                'Происхождение назначения после создания изменять нельзя.',
                code='immutable_assignment_provenance',
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if self.PROVENANCE_FIELDS.intersection(fields):
            raise ValidationError(
                'Происхождение назначения после создания изменять нельзя.',
                code='immutable_assignment_provenance',
            )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        assignments = list(objs)
        for assignment in assignments:
            if (
                assignment.source_kind == EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN
                or assignment.source_crew_plan_slot_id is not None
            ):
                raise ValidationError(
                    'Официальное назначение может создать только публикация плана.',
                    code='official_assignment_requires_published_plan_service',
                )
        return super().bulk_create(assignments, *args, **kwargs)


class EquipmentAssignment(models.Model):
    class SourceKind(models.TextChoices):
        UNVERIFIED = 'unverified', 'Непроверенный или неофициальный источник'
        DEPUTY_PUBLISHED_PLAN = 'deputy_published_plan', 'Опубликованный план заместителя'

    objects = EquipmentAssignmentQuerySet.as_manager()

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
    source_kind = models.CharField(
        'Источник назначения',
        max_length=32,
        choices=SourceKind.choices,
        default=SourceKind.UNVERIFIED,
    )
    source_crew_plan_slot = models.ForeignKey(
        'CrewPlanSlot',
        verbose_name='Слот опубликованного плана-источника',
        on_delete=models.PROTECT,
        related_name='equipment_assignments',
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
            models.CheckConstraint(
                condition=(
                    models.Q(
                        source_kind='deputy_published_plan',
                        source_crew_plan_slot__isnull=False,
                    )
                    | models.Q(
                        source_kind='unverified',
                        source_crew_plan_slot__isnull=True,
                    )
                ),
                name='assignment_source_shape_valid',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.source_kind == self.SourceKind.DEPUTY_PUBLISHED_PLAN:
            if self.source_crew_plan_slot_id is None:
                errors['source_crew_plan_slot'] = (
                    'Официальное назначение требует точный слот опубликованного плана.'
                )
            else:
                slot = self.source_crew_plan_slot
                if slot.plan.status not in {
                    CrewPlanStatus.PUBLISHED,
                    CrewPlanStatus.SUPERSEDED,
                }:
                    errors['source_crew_plan_slot'] = (
                        'Официальный источник должен относиться к опубликованному плану или его истории.'
                    )
                if slot.employee_id != self.employee_id:
                    errors['employee'] = 'Сотрудник назначения не соответствует слоту плана.'
                if slot.equipment_id != self.equipment_id:
                    errors['equipment'] = 'Техника назначения не соответствует слоту плана.'
                if slot.plan.role_id != self.role_id:
                    errors['role'] = 'Роль назначения не соответствует плану слота.'
                if slot.shift_type != self.shift_type:
                    errors['shift_type'] = 'Рабочая смена назначения не соответствует слоту плана.'
        elif self.source_crew_plan_slot_id is not None:
            errors['source_crew_plan_slot'] = (
                'Неофициальное назначение не может ссылаться на опубликованный слот.'
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self._state.adding and (
                self.source_kind == self.SourceKind.DEPUTY_PUBLISHED_PLAN
                or self.source_crew_plan_slot_id is not None
            ):
                raise ValidationError(
                    'Официальное назначение может создать только публикация плана.',
                    code='official_assignment_requires_published_plan_service',
                )
            if self.pk:
                persisted = (
                    type(self)._base_manager.select_for_update()
                    .only('source_kind', 'source_crew_plan_slot_id')
                    .get(pk=self.pk)
                )
                if (
                    persisted.source_kind != self.source_kind
                    or persisted.source_crew_plan_slot_id != self.source_crew_plan_slot_id
                ):
                    raise ValidationError(
                        'Происхождение назначения после создания изменять нельзя.',
                        code='immutable_assignment_provenance',
                    )
            self.full_clean()
            return super().save(*args, **kwargs)

    @property
    def work_shift_label(self):
        if self.shift_type == WorkShiftType.SHIFT_1:
            return 'Смена 1'
        if self.shift_type == WorkShiftType.SHIFT_2:
            return 'Смена 2'
        return 'Смена не указана'

    def __str__(self):
        return f'{self.employee} -> {self.equipment}'


class CrewPlan(models.Model):
    work_date = models.DateField('Производственные сутки')
    role = models.ForeignKey(
        'users.Role',
        verbose_name='Рабочая роль',
        on_delete=models.PROTECT,
        related_name='crew_plans',
    )
    revision = models.PositiveIntegerField('Ревизия', default=1)
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=CrewPlanStatus.choices,
        default=CrewPlanStatus.DRAFT,
    )
    version = models.PositiveIntegerField('Версия черновика', default=1)
    created_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто создал',
        on_delete=models.SET_NULL,
        related_name='created_crew_plans',
        null=True,
        blank=True,
    )
    updated_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто изменил',
        on_delete=models.SET_NULL,
        related_name='updated_crew_plans',
        null=True,
        blank=True,
    )
    published_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто опубликовал',
        on_delete=models.SET_NULL,
        related_name='published_crew_plans',
        null=True,
        blank=True,
    )
    published_at = models.DateTimeField('Опубликован', null=True, blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменен', auto_now=True)

    class Meta:
        verbose_name = 'План расстановки экипажей'
        verbose_name_plural = 'Планы расстановки экипажей'
        ordering = ['-work_date', 'role__name', '-revision']
        constraints = [
            models.UniqueConstraint(
                fields=['work_date', 'role', 'revision'],
                name='unique_crew_plan_revision',
            ),
            models.UniqueConstraint(
                fields=['work_date', 'role'],
                condition=models.Q(status=CrewPlanStatus.DRAFT),
                name='unique_draft_crew_plan',
            ),
            models.UniqueConstraint(
                fields=['work_date', 'role'],
                condition=models.Q(status=CrewPlanStatus.PUBLISHED),
                name='unique_published_crew_plan',
            ),
        ]

    def __str__(self):
        return f'{self.work_date:%d.%m.%Y} / {self.role} / r{self.revision}'


class CrewPlanSlot(models.Model):
    plan = models.ForeignKey(
        CrewPlan,
        verbose_name='План расстановки',
        on_delete=models.CASCADE,
        related_name='slots',
    )
    equipment = models.ForeignKey(
        'references.Equipment',
        verbose_name='Техника',
        on_delete=models.PROTECT,
        related_name='crew_plan_slots',
    )
    shift_type = models.CharField(
        'Рабочая смена',
        max_length=16,
        choices=WorkShiftType.choices,
    )
    employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Назначенный сотрудник',
        on_delete=models.SET_NULL,
        related_name='crew_plan_slots',
        null=True,
        blank=True,
    )
    secondary_employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Дополнительный участник экипажа',
        on_delete=models.SET_NULL,
        related_name='secondary_crew_plan_slots',
        null=True,
        blank=True,
    )
    baseline_employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Сотрудник в базовой расстановке',
        on_delete=models.SET_NULL,
        related_name='baseline_crew_plan_slots',
        null=True,
        blank=True,
    )
    baseline_secondary_employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Дополнительный участник в базовой расстановке',
        on_delete=models.SET_NULL,
        related_name='baseline_secondary_crew_plan_slots',
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = 'Слот плана расстановки'
        verbose_name_plural = 'Слоты плана расстановки'
        ordering = ['equipment__garage_number', 'shift_type']
        constraints = [
            models.UniqueConstraint(
                fields=['plan', 'equipment', 'shift_type'],
                name='unique_crew_plan_equipment_shift',
            ),
            models.UniqueConstraint(
                fields=['plan', 'employee'],
                condition=models.Q(employee__isnull=False),
                name='unique_crew_plan_employee',
            ),
            models.UniqueConstraint(
                fields=['plan', 'secondary_employee'],
                condition=models.Q(secondary_employee__isnull=False),
                name='unique_crew_plan_secondary_employee',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(employee__isnull=True)
                    | models.Q(secondary_employee__isnull=True)
                    | ~models.Q(employee=models.F('secondary_employee'))
                ),
                name='crew_plan_slot_distinct_employees',
            ),
        ]

    def __str__(self):
        return f'{self.plan} / {self.equipment} / {self.get_shift_type_display()}'


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
        constraints = [
            models.UniqueConstraint(
                fields=['truck'],
                condition=models.Q(
                    status=AssignmentStatus.ACCEPTED,
                    ended_at__isnull=True,
                ),
                name='uniq_open_accepted_haul_truck',
            ),
            models.UniqueConstraint(
                fields=['truck'],
                condition=models.Q(
                    status=AssignmentStatus.PENDING,
                    ended_at__isnull=True,
                ),
                name='uniq_open_pending_haul_truck',
            ),
        ]

    def __str__(self):
        return f'{self.truck} под {self.excavator}'


class HaulAssignmentHandoff(models.Model):
    """Одноразовое право прежней смены завершить физическую погрузку.

    Диспетчерская проекция при этом уже следует новому ``target_assignment``.
    Запись не является вторым назначением самосвала и никогда не участвует в
    расчёте текущего комплекса.
    """

    truck = models.ForeignKey(
        'references.Equipment',
        verbose_name='Самосвал',
        on_delete=models.PROTECT,
        related_name='haul_assignment_handoffs',
    )
    source_assignment = models.ForeignKey(
        HaulAssignment,
        verbose_name='Прежнее назначение',
        on_delete=models.PROTECT,
        related_name='outgoing_handoffs',
    )
    target_assignment = models.ForeignKey(
        HaulAssignment,
        verbose_name='Новое решение диспетчера',
        on_delete=models.PROTECT,
        related_name='incoming_handoffs',
    )
    source_excavator = models.ForeignKey(
        'references.Equipment',
        verbose_name='Прежний экскаватор',
        on_delete=models.PROTECT,
        related_name='outgoing_haul_handoffs',
    )
    source_shift = models.ForeignKey(
        'shifts.EmployeeShift',
        verbose_name='Смена, завершающая погрузку',
        on_delete=models.PROTECT,
        related_name='haul_handoff_completions',
    )
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=HaulAssignmentHandoffStatus.choices,
        default=HaulAssignmentHandoffStatus.OPEN,
    )
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    resolved_at = models.DateTimeField('Завершено', null=True, blank=True)
    resolved_by_trip = models.ForeignKey(
        'trips.Trip',
        verbose_name='Рейс, завершивший передачу',
        on_delete=models.PROTECT,
        related_name='resolved_haul_handoffs',
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = 'Передача погрузки между экскаваторами'
        verbose_name_plural = 'Передачи погрузки между экскаваторами'
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['source_assignment', 'target_assignment', 'source_shift'],
                condition=models.Q(status=HaulAssignmentHandoffStatus.OPEN),
                name='uniq_open_haul_handoff_transition',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status=HaulAssignmentHandoffStatus.OPEN,
                        resolved_at__isnull=True,
                        resolved_by_trip__isnull=True,
                    )
                    | models.Q(
                        status=HaulAssignmentHandoffStatus.RESOLVED,
                        resolved_at__isnull=False,
                        resolved_by_trip__isnull=False,
                    )
                    | models.Q(
                        status=HaulAssignmentHandoffStatus.EXPIRED,
                        resolved_at__isnull=False,
                        resolved_by_trip__isnull=True,
                    )
                ),
                name='haul_handoff_resolution_consistent',
            ),
        ]

    def __str__(self):
        return f'{self.truck}: {self.source_excavator} → {self.target_assignment}'

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
    transport_distance_km = models.DecimalField(
        'Рабочее плечо до разгрузки, км',
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )
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


class ExcavatorDumpPointSetting(models.Model):
    placement = models.ForeignKey(
        ExcavatorPlacement,
        verbose_name='Размещение экскаватора',
        on_delete=models.CASCADE,
        related_name='dump_point_settings',
    )
    dump_point = models.ForeignKey(
        'references.DumpPoint',
        verbose_name='Точка разгрузки',
        on_delete=models.CASCADE,
        related_name='excavator_work_settings',
    )
    transport_distance_km = models.DecimalField(
        'Плечо до точки разгрузки, км',
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )
    position = models.PositiveSmallIntegerField('Порядок', default=0)
    changed_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто изменил',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    changed_at = models.DateTimeField('Изменено', auto_now=True)

    class Meta:
        verbose_name = 'Рабочая точка разгрузки экскаватора'
        verbose_name_plural = 'Рабочие точки разгрузки экскаваторов'
        ordering = ['position', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['placement', 'dump_point'],
                name='assignments_unique_excavator_dump_point',
            ),
        ]

    def __str__(self):
        return f'{self.placement.excavator} → {self.dump_point}'


# Create your models here.
