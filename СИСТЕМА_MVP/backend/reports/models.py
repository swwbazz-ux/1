from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction

from core.db_locks import lock_idempotency_key

from .rating_period_calendar import (
    RATING_PERIOD_CATALOG_LOCK_ACTION,
    RATING_PERIOD_CATALOG_LOCK_KEY,
    RATING_PERIOD_DEFAULT_START_DAY,
    nominal_rating_period_end,
)


class ReportType(models.TextChoices):
    SHIFT_VOLUME = 'shift_volume', 'Объемы за смену'
    DAILY_VOLUME = 'daily_volume', 'Объемы за сутки'
    CUSTOMER_DAILY = 'customer_daily', 'Суточный отчет заказчику'


class ReportTemplate(models.Model):
    name = models.CharField('Название шаблона', max_length=160, unique=True)
    report_type = models.CharField('Тип отчета', max_length=32, choices=ReportType.choices)
    columns = models.JSONField('Столбцы отчета', default=list)
    column_labels = models.JSONField('Названия столбцов', default=dict, blank=True)
    filters = models.JSONField('Фильтры отчета', default=dict, blank=True)
    group_by = models.CharField('Группировка', max_length=64, blank=True)
    created_by = models.ForeignKey('users.Employee', verbose_name='Кто создал', on_delete=models.PROTECT, null=True, blank=True, related_name='created_report_templates')
    updated_by = models.ForeignKey('users.Employee', verbose_name='Кто изменил', on_delete=models.PROTECT, null=True, blank=True, related_name='updated_report_templates')
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        verbose_name = 'Шаблон отчета'
        verbose_name_plural = 'Шаблоны отчетов'
        ordering = ['name']

    def __str__(self):
        return self.name


class RatingPeriodQuerySet(models.QuerySet):
    mutation_error = (
        'Периоды рейтинга нельзя массово изменять или удалять. '
        'Измените одну запись через справочник либо отключите её.'
    )

    def update(self, **kwargs):
        raise ValidationError(self.mutation_error)

    def delete(self):
        raise ValidationError(self.mutation_error)

    def bulk_create(self, objs, **kwargs):
        raise ValidationError(self.mutation_error)

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError(self.mutation_error)


class RatingPeriod(models.Model):
    objects = RatingPeriodQuerySet.as_manager()

    name = models.CharField(
        'Название периода',
        max_length=160,
        help_text='Например: Премирование за август 2026.',
    )
    starts_on = models.DateField(
        'Считать с',
        help_text='Начальная дата включается в расчёт.',
    )
    ends_before = models.DateField(
        'Считать до (не включая дату)',
        help_text=(
            'Эта дата в расчёт не входит. Например, период 14.07–14.08 '
            'считает данные по 13.08 включительно.'
        ),
    )
    comment = models.TextField(
        'Причина или примечание',
        blank=True,
        help_text=(
            'Укажите основание, если даты замера отличаются от обычного '
            'вахтового периода.'
        ),
    )
    is_active = models.BooleanField(
        'Используется для расчёта',
        default=True,
        help_text=(
            'Только включённые периоды могут использоваться для расчёта '
            'рейтинга.'
        ),
    )
    nominal_starts_on = models.DateField(
        'Обычная дата начала',
        null=True,
        blank=True,
        unique=True,
        editable=False,
        help_text=(
            'Стабильный ключ автоматически созданного периода. '
            'Не меняется при ручной корректировке дат.'
        ),
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Период рейтинга'
        verbose_name_plural = 'Периоды рейтинга'
        ordering = ['-starts_on', '-id']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_before__gt=models.F('starts_on')),
                name='rating_period_end_after_start',
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_automatically_created(self):
        return self.nominal_starts_on is not None

    @property
    def has_manual_override(self):
        if self.starts_on is None or self.ends_before is None:
            return False
        if self.nominal_starts_on is None:
            return (
                self.starts_on.day != RATING_PERIOD_DEFAULT_START_DAY
                or self.ends_before
                != nominal_rating_period_end(self.starts_on)
            )
        return (
            self.starts_on != self.nominal_starts_on
            or self.ends_before
            != nominal_rating_period_end(self.nominal_starts_on)
        )

    def generation_source_label(self):
        if self.is_automatically_created:
            return 'Автоматически'
        return 'Вручную'

    generation_source_label.short_description = 'Создание'

    def manual_override_label(self):
        if self.has_manual_override:
            if self.is_automatically_created:
                return 'Даты изменены вручную'
            return 'Ручное исключение'
        if not self.is_automatically_created:
            return 'По правилу 14-е → 14-е (создан вручную)'
        return 'По правилу 14-е → 14-е'

    manual_override_label.short_description = 'Режим дат'

    def audit_value(self):
        return (
            f'Название: {self.name}; '
            f'границы: {self.starts_on:%d.%m.%Y}–'
            f'{self.ends_before:%d.%m.%Y} '
            '(конечная дата не входит); '
            f'состояние: {"активен" if self.is_active else "отключён"}; '
            f'создание: {self.generation_source_label()}; '
            f'режим: {self.manual_override_label()}; '
            f'примечание: {(self.comment or "").strip() or "нет"}.'
        )

    def clean(self):
        super().clean()
        errors = {}
        if self.pk:
            stored_nominal_starts_on = (
                type(self).objects
                .filter(pk=self.pk)
                .values_list('nominal_starts_on', flat=True)
                .first()
            )
            if stored_nominal_starts_on != self.nominal_starts_on:
                errors['nominal_starts_on'] = (
                    'Обычная дата начала автоматического периода '
                    'не может быть изменена.'
                )
        if (
            self.starts_on is not None
            and self.ends_before is not None
            and self.ends_before <= self.starts_on
        ):
            errors['ends_before'] = (
                'Дата «Считать до» должна быть позже даты «Считать с».'
            )
        if (
            self.starts_on is not None
            and self.ends_before is not None
            and self.has_manual_override
            and not (self.comment or '').strip()
        ):
            errors['comment'] = (
                'Укажите причину, почему даты отличаются от обычного '
                'периода 14-е → 14-е.'
            )
        if errors:
            raise ValidationError(errors)
        if (
            not self.is_active
            or self.starts_on is None
            or self.ends_before is None
            or self.ends_before <= self.starts_on
        ):
            return

        conflicts = type(self).objects.filter(
            is_active=True,
            starts_on__lt=self.ends_before,
            ends_before__gt=self.starts_on,
        )
        if self.pk:
            conflicts = conflicts.exclude(pk=self.pk)
        conflict = conflicts.order_by('starts_on', 'id').first()
        if conflict is not None:
            raise ValidationError(
                'Период пересекается с '
                f'«{conflict.name}» '
                f'({conflict.starts_on:%d.%m.%Y}–'
                f'{conflict.ends_before:%d.%m.%Y}, конечная дата не входит). '
                'Измените даты или отключите пересекающийся период.'
            )

    @classmethod
    def lock_catalog(cls):
        lock_idempotency_key(
            RATING_PERIOD_CATALOG_LOCK_ACTION,
            RATING_PERIOD_CATALOG_LOCK_KEY,
        )

    def _refresh_fields_excluded_from_partial_update(self, update_fields):
        if self._state.adding or self.pk is None or update_fields is None:
            return
        try:
            stored = (
                type(self)._base_manager
                .select_for_update()
                .get(pk=self.pk)
            )
        except type(self).DoesNotExist as error:
            raise ValidationError(
                'Период рейтинга уже удалён или недоступен.'
            ) from error
        included_fields = set(update_fields)
        for field in self._meta.concrete_fields:
            if field.primary_key:
                continue
            if (
                field.name not in included_fields
                and field.attname not in included_fields
            ):
                setattr(self, field.attname, getattr(stored, field.attname))

    def save(self, *args, **kwargs):
        with transaction.atomic():
            self.lock_catalog()
            self._refresh_fields_excluded_from_partial_update(
                kwargs.get('update_fields'),
            )
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            'Период рейтинга нельзя удалить. Отключите его, чтобы '
            'сохранить историю расчётов и изменений.'
        )


class DriverRatingSnapshotRefreshStatus(models.TextChoices):
    READY = 'ready', 'Готов'
    FAILED = 'failed', 'Последнее обновление завершилось ошибкой'


class DriverRatingPeriodMaterializedSnapshot(models.Model):
    """Одна текущая серверная витрина рейтинга для канонической группы."""

    scope_code = models.CharField(
        'Техническая область рейтинга',
        max_length=64,
    )
    rating_period = models.ForeignKey(
        RatingPeriod,
        verbose_name='Период рейтинга',
        on_delete=models.PROTECT,
        related_name='driver_rating_materialized_snapshots',
    )
    watch_composition = models.ForeignKey(
        'users.WatchComposition',
        verbose_name='Состав вахты',
        on_delete=models.PROTECT,
        related_name='driver_rating_materialized_snapshots',
    )
    shift_type = models.CharField(
        'Тип смены',
        max_length=16,
        choices=(
            ('day', 'Дневная'),
            ('night', 'Ночная'),
        ),
    )
    formula_version = models.CharField(
        'Версия формулы',
        max_length=96,
    )
    payload_schema_version = models.PositiveSmallIntegerField(
        'Версия схемы готового снимка',
        default=1,
    )
    scope_fingerprint = models.CharField(
        'Fingerprint области расчёта',
        max_length=64,
    )
    source_fingerprint = models.CharField(
        'Fingerprint источников',
        max_length=64,
        blank=True,
    )
    shift_score_fingerprint = models.CharField(
        'Fingerprint сменных баллов',
        max_length=64,
        blank=True,
    )
    payload_fingerprint = models.CharField(
        'Fingerprint готового снимка',
        max_length=64,
        blank=True,
    )
    member_fingerprint = models.CharField(
        'Fingerprint состава готового снимка',
        max_length=64,
        blank=True,
    )
    payload = models.JSONField(
        'Готовый серверный снимок',
        encoder=DjangoJSONEncoder,
        default=dict,
        blank=True,
    )
    member_employee_ids = models.JSONField(
        'Сотрудники, представленные в группе',
        encoder=DjangoJSONEncoder,
        default=list,
        blank=True,
    )
    member_latest_closed_at = models.JSONField(
        'Последнее закрытие смены сотрудника в группе',
        encoder=DjangoJSONEncoder,
        default=dict,
        blank=True,
    )
    revision = models.PositiveIntegerField('Ревизия', default=0)
    published_at = models.DateTimeField(
        'Содержимое опубликовано',
        null=True,
        blank=True,
    )
    last_success_at = models.DateTimeField(
        'Последняя успешная проверка',
        null=True,
        blank=True,
    )
    last_attempt_at = models.DateTimeField(
        'Последняя попытка обновления',
        null=True,
        blank=True,
    )
    last_failure_at = models.DateTimeField(
        'Последняя ошибка обновления',
        null=True,
        blank=True,
    )
    last_refresh_status = models.CharField(
        'Состояние последнего обновления',
        max_length=16,
        choices=DriverRatingSnapshotRefreshStatus.choices,
        default=DriverRatingSnapshotRefreshStatus.FAILED,
    )
    failure_code = models.CharField(
        'Код последней ошибки',
        max_length=64,
        blank=True,
    )
    consecutive_failure_count = models.PositiveIntegerField(
        'Ошибок обновления подряд',
        default=0,
    )
    last_error = models.CharField(
        'Сокращённая внутренняя ошибка',
        max_length=500,
        blank=True,
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Текущий серверный снимок рейтинга водителей'
        verbose_name_plural = (
            'Текущие серверные снимки рейтинга водителей'
        )
        ordering = [
            '-rating_period__starts_on',
            'watch_composition_id',
            'shift_type',
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'scope_code',
                    'rating_period',
                    'watch_composition',
                    'shift_type',
                ],
                name='uniq_drv_rating_mat_group',
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    'scope_code',
                    'rating_period',
                    'formula_version',
                ],
                name='drv_rating_mat_scope_period_ix',
            ),
        ]

    def __str__(self):
        return (
            f'{self.scope_code}: {self.rating_period} / '
            f'{self.watch_composition} / {self.get_shift_type_display()}'
        )


class PilotFeedbackPriority(models.TextChoices):
    P0 = 'p0', 'P0 - блокирует пилот'
    P1 = 'p1', 'P1 - исправить до запуска'
    P2 = 'p2', 'P2 - можно после запуска'
    P3 = 'p3', 'P3 - идея или улучшение'


class PilotFeedbackStatus(models.TextChoices):
    NEW = 'new', 'Новое'
    IN_WORK = 'in_work', 'В работе'
    DECIDED = 'decided', 'Решение принято'
    POSTPONED = 'postponed', 'Перенесено'
    REJECTED = 'rejected', 'Отклонено'


class PilotFeedbackCategory(models.TextChoices):
    INTERFACE = 'interface', 'Интерфейс'
    DATA = 'data', 'Данные'
    TRIP = 'trip', 'Рейс'
    SHIFT = 'shift', 'Смена'
    ASSIGNMENT = 'assignment', 'Назначение'
    DOWNTIME = 'downtime', 'Простои'
    REPORT = 'report', 'Отчет'
    MANAGEMENT = 'management', 'Витрина'
    ACCESS = 'access', 'Права и доступы'
    NEXT_MODULE = 'next_module', 'Следующий модуль'


class PilotFeedback(models.Model):
    title = models.CharField('Краткое замечание', max_length=220)
    category = models.CharField('Категория', max_length=32, choices=PilotFeedbackCategory.choices)
    priority = models.CharField('Приоритет', max_length=8, choices=PilotFeedbackPriority.choices, default=PilotFeedbackPriority.P2)
    status = models.CharField('Статус', max_length=16, choices=PilotFeedbackStatus.choices, default=PilotFeedbackStatus.NEW)
    screen = models.CharField('Экран или процесс', max_length=160, blank=True)
    description = models.TextField('Описание', blank=True)
    decision = models.TextField('Решение', blank=True)
    created_by = models.ForeignKey('users.Employee', verbose_name='Кто зафиксировал', on_delete=models.PROTECT, related_name='pilot_feedback_created')
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Замечание пилота'
        verbose_name_plural = 'Замечания пилота'
        ordering = ['priority', '-created_at']

    def __str__(self):
        return self.title


class DriverShiftPassportTrigger(models.TextChoices):
    DRIVER_CLOSE = 'driver_close', 'Обычное закрытие водителем'
    SERVICE_CLOSE = 'service_close', 'Служебное закрытие'
    ROLE_SWITCH = 'role_switch', 'Закрытие при переключении роли'
    SOURCE_RECONCILE = 'source_reconcile', 'Пересчёт после изменения источников'
    BACKFILL = 'backfill', 'Ретроспективное заполнение'
    CALCULATOR_UPGRADE = 'calculator_upgrade', 'Новая версия калькулятора'


class DriverShiftPassportRequestStatus(models.TextChoices):
    PENDING = 'pending', 'Ожидает обработки'
    PROCESSING = 'processing', 'Обрабатывается'
    FAILED = 'failed', 'Ошибка'
    COMPLETED = 'completed', 'Готово'


class DriverShiftPassportSnapshotQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError(
            'Готовые паспорта смен нельзя изменять массовым обновлением.'
        )

    def delete(self):
        raise ValidationError(
            'Готовые паспорта смен нельзя удалять массовой операцией.'
        )


class DriverShiftPassportSnapshotManager(
    models.Manager.from_queryset(DriverShiftPassportSnapshotQuerySet)
):
    pass


class DriverShiftPassportSnapshot(models.Model):
    shift = models.ForeignKey(
        'shifts.EmployeeShift',
        verbose_name='Смена водителя',
        on_delete=models.PROTECT,
        related_name='passport_snapshots',
    )
    revision = models.PositiveIntegerField('Ревизия')
    schema_version = models.PositiveSmallIntegerField('Версия схемы паспорта')
    calculator_version = models.CharField('Версия калькулятора', max_length=64)
    source_fingerprint = models.CharField('Fingerprint источников', max_length=64)
    payload_fingerprint = models.CharField('Fingerprint паспорта', max_length=64)
    payload = models.JSONField(
        'Диагностический паспорт',
        encoder=DjangoJSONEncoder,
    )
    trigger = models.CharField(
        'Причина формирования',
        max_length=32,
        choices=DriverShiftPassportTrigger.choices,
    )
    captured_late = models.BooleanField('Сформирован с задержкой', default=False)
    captured_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кем инициирован',
        on_delete=models.PROTECT,
        related_name='captured_driver_shift_passports',
        null=True,
        blank=True,
    )
    captured_at = models.DateTimeField('Сформирован', auto_now_add=True)

    objects = DriverShiftPassportSnapshotManager()

    class Meta:
        verbose_name = 'Диагностический паспорт смены водителя'
        verbose_name_plural = 'Диагностические паспорта смен водителей'
        ordering = ['shift_id', '-revision']
        constraints = [
            models.UniqueConstraint(
                fields=['shift', 'revision'],
                name='unique_driver_passport_shift_revision',
            ),
            models.UniqueConstraint(
                fields=['shift', 'calculator_version', 'source_fingerprint'],
                name='unique_driver_passport_source_version',
            ),
        ]
        indexes = [
            models.Index(
                fields=['shift', '-revision'],
                name='driver_passport_shift_rev_idx',
            ),
        ]

    def __str__(self):
        return f'Смена {self.shift_id}, паспорт r{self.revision}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                'Готовый паспорт смены неизменяем; создайте новую ревизию.'
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            'Готовый паспорт смены нельзя удалить штатным способом.'
        )


class DriverShiftPassportCaptureRequest(models.Model):
    shift = models.ForeignKey(
        'shifts.EmployeeShift',
        verbose_name='Смена водителя',
        on_delete=models.PROTECT,
        related_name='passport_capture_requests',
    )
    request_key = models.CharField('Ключ запроса', max_length=64, unique=True)
    trigger = models.CharField(
        'Причина формирования',
        max_length=32,
        choices=DriverShiftPassportTrigger.choices,
    )
    calculator_version = models.CharField('Версия калькулятора', max_length=64)
    closed_at = models.DateTimeField('Момент закрытия смены')
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=DriverShiftPassportRequestStatus.choices,
        default=DriverShiftPassportRequestStatus.PENDING,
        db_index=True,
    )
    attempt_count = models.PositiveIntegerField('Количество попыток', default=0)
    last_error = models.TextField('Последняя ошибка', blank=True)
    captured_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кем инициирован',
        on_delete=models.PROTECT,
        related_name='driver_shift_passport_requests',
        null=True,
        blank=True,
    )
    snapshot = models.ForeignKey(
        DriverShiftPassportSnapshot,
        verbose_name='Сформированный паспорт',
        on_delete=models.PROTECT,
        related_name='capture_requests',
        null=True,
        blank=True,
    )
    requested_at = models.DateTimeField('Запрошен', auto_now_add=True)
    started_at = models.DateTimeField('Обработка начата', null=True, blank=True)
    completed_at = models.DateTimeField('Обработка завершена', null=True, blank=True)

    class Meta:
        verbose_name = 'Запрос на паспорт смены водителя'
        verbose_name_plural = 'Запросы на паспорта смен водителей'
        ordering = ['requested_at', 'id']
        indexes = [
            models.Index(
                fields=['status', 'requested_at'],
                name='driver_passport_req_status_idx',
            ),
        ]

    def __str__(self):
        return f'Смена {self.shift_id}: {self.status}'
