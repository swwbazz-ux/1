from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


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
