from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction

from shifts.models import ShiftType

from .storage import arrival_roster_private_storage


class RotationCollectionCycle(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        OPEN = 'open', 'Сбор открыт'
        CLOSED = 'closed', 'Сбор закрыт'
        ARCHIVED = 'archived', 'В архиве'

    name = models.CharField('Название сбора', max_length=160)
    target_watch_period = models.ForeignKey(
        'shifts.WatchPeriod',
        verbose_name='Целевая вахта',
        on_delete=models.PROTECT,
        related_name='rotation_collection_cycles',
    )
    response_deadline = models.DateTimeField('Срок предоставления ответа')
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    revision = models.PositiveIntegerField('Ревизия', default=1)
    created_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто создал',
        on_delete=models.SET_NULL,
        related_name='created_rotation_cycles',
        null=True,
        blank=True,
    )
    opened_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто открыл сбор',
        on_delete=models.SET_NULL,
        related_name='opened_rotation_cycles',
        null=True,
        blank=True,
    )
    opened_at = models.DateTimeField('Сбор открыт', null=True, blank=True)
    closed_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто закрыл сбор',
        on_delete=models.SET_NULL,
        related_name='closed_rotation_cycles',
        null=True,
        blank=True,
    )
    closed_at = models.DateTimeField('Сбор закрыт', null=True, blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Цикл сбора по перевахте'
        verbose_name_plural = 'Циклы сбора по перевахте'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['target_watch_period', 'status'], name='rot_cycle_watch_status_idx'),
            models.Index(fields=['status', 'response_deadline'], name='rot_cycle_status_due_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['target_watch_period'],
                condition=models.Q(status='open'),
                name='uniq_open_rotation_cycle_watch',
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name='rot_cycle_revision_gte_1',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status='draft',
                        opened_by__isnull=True,
                        opened_at__isnull=True,
                        closed_by__isnull=True,
                        closed_at__isnull=True,
                    )
                    | models.Q(
                        status='open',
                        opened_by__isnull=False,
                        opened_at__isnull=False,
                        closed_by__isnull=True,
                        closed_at__isnull=True,
                    )
                    | models.Q(
                        status__in=['closed', 'archived'],
                        opened_by__isnull=False,
                        opened_at__isnull=False,
                        closed_by__isnull=False,
                        closed_at__isnull=False,
                    )
                ),
                name='rot_cycle_lifecycle_valid',
            ),
        ]

    def __str__(self):
        return f'{self.name} / {self.target_watch_period}'


class RotationResponse(models.Model):
    class State(models.TextChoices):
        PENDING = 'pending', 'Ожидается ответ'
        SUBMITTED = 'submitted', 'Ответ предоставлен'

    class Intent(models.TextChoices):
        ARRIVAL = 'arrival', 'Заезд на вахту'
        DEPARTURE = 'departure', 'Выезд с вахты'
        NOT_TRAVELLING = 'not_travelling', 'Поездка не требуется'
        EXTENSION = 'extension', 'Запрос на продление вахты'

    class ShiftSource(models.TextChoices):
        UNKNOWN = 'unknown', 'Источник не определён'
        ACTIVE_ASSIGNMENT = 'active_assignment', 'Действующая расстановка'
        EMPLOYEE = 'employee', 'Указано сотрудником'
        TIMEKEEPER = 'timekeeper', 'Указано табельщиком'

    class TravelMode(models.TextChoices):
        AIR = 'air', 'Самолёт'
        RAIL = 'rail', 'Поезд'
        BUS = 'bus', 'Автобус'
        CAR = 'car', 'Автомобиль'
        OTHER = 'other', 'Другое'

    class TransferMode(models.TextChoices):
        ORGANIZED = 'organized', 'Организованный трансфер'
        SELF = 'self', 'Самостоятельно'

    cycle = models.ForeignKey(
        RotationCollectionCycle,
        verbose_name='Цикл сбора',
        on_delete=models.CASCADE,
        related_name='responses',
    )
    employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Сотрудник',
        on_delete=models.PROTECT,
        related_name='rotation_responses',
    )
    snapshot_full_name = models.CharField('ФИО snapshot', max_length=255)
    snapshot_personnel_number = models.CharField('Табельный номер snapshot', max_length=64, blank=True)
    snapshot_position = models.CharField('Должность snapshot', max_length=255, blank=True)
    snapshot_department = models.CharField('Подразделение snapshot', max_length=255, blank=True)
    snapshot_work_schedule = models.CharField('График работы snapshot', max_length=255, blank=True)
    snapshot_brigade_number = models.PositiveSmallIntegerField(
        'Бригада snapshot',
        null=True,
        blank=True,
    )
    state = models.CharField(
        'Состояние ответа',
        max_length=16,
        choices=State.choices,
        default=State.PENDING,
        db_index=True,
    )
    intent = models.CharField(
        'Намерение сотрудника',
        max_length=24,
        choices=Intent.choices,
        blank=True,
        default='',
        db_index=True,
    )
    next_shift_type = models.CharField(
        'Смена следующей вахты',
        max_length=16,
        choices=ShiftType.choices,
        blank=True,
        default='',
    )
    shift_source = models.CharField(
        'Источник смены',
        max_length=24,
        choices=ShiftSource.choices,
        default=ShiftSource.UNKNOWN,
    )
    departure_on = models.DateField('Дата выезда', null=True, blank=True)
    arrival_on = models.DateField('Дата заезда', null=True, blank=True)
    route_text = models.TextField('Маршрут', blank=True)
    travel_mode = models.CharField(
        'Вид транспорта',
        max_length=16,
        choices=TravelMode.choices,
        blank=True,
        default='',
    )
    transfer_mode = models.CharField(
        'Способ трансфера',
        max_length=16,
        choices=TransferMode.choices,
        blank=True,
        default='',
    )
    transport_details = models.TextField('Детали транспорта', blank=True)
    comment = models.TextField('Комментарий сотрудника', blank=True)
    submitted_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто предоставил ответ',
        on_delete=models.SET_NULL,
        related_name='submitted_rotation_responses',
        null=True,
        blank=True,
    )
    submitted_at = models.DateTimeField('Ответ предоставлен', null=True, blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Ответ по перевахте'
        verbose_name_plural = 'Ответы по перевахте'
        ordering = ['cycle', 'snapshot_full_name', 'id']
        indexes = [
            models.Index(fields=['cycle', 'state'], name='rot_resp_cycle_state_idx'),
            models.Index(fields=['cycle', 'intent'], name='rot_resp_cycle_intent_idx'),
            models.Index(fields=['employee', 'created_at'], name='rot_resp_employee_date_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['cycle', 'employee'],
                name='uniq_rotation_cycle_employee',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(snapshot_brigade_number__isnull=True)
                    | models.Q(snapshot_brigade_number__gte=1, snapshot_brigade_number__lte=4)
                ),
                name='rot_resp_brigade_1_4',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state='pending',
                        intent='',
                        submitted_by__isnull=True,
                        submitted_at__isnull=True,
                    )
                    | models.Q(
                        state='submitted',
                        intent__in=['arrival', 'departure', 'not_travelling', 'extension'],
                        submitted_by__isnull=False,
                        submitted_at__isnull=False,
                    )
                ),
                name='rot_resp_submission_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(next_shift_type__in=['', ShiftType.DAY, ShiftType.NIGHT]),
                name='rot_resp_next_shift_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    shift_source__in=['unknown', 'active_assignment', 'employee', 'timekeeper'],
                ),
                name='rot_resp_shift_source_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(travel_mode__in=['', 'air', 'rail', 'bus', 'car', 'other']),
                name='rot_resp_travel_mode_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(transfer_mode__in=['', 'organized', 'self']),
                name='rot_resp_transfer_mode_valid',
            ),
        ]

    def __str__(self):
        return f'{self.snapshot_full_name} / {self.cycle}'


class WatchExtensionCase(models.Model):
    class DecisionStatus(models.TextChoices):
        PENDING = 'pending', 'Ожидает решения'
        APPROVED = 'approved', 'Одобрено'
        REJECTED = 'rejected', 'Отклонено'

    class DocumentationStatus(models.TextChoices):
        NOT_STARTED = 'not_started', 'Не начато'
        DATA_READY = 'data_ready', 'Данные подготовлены'
        COMPLETED = 'completed', 'Оформление завершено'

    response = models.OneToOneField(
        RotationResponse,
        verbose_name='Ответ с запросом на продление',
        on_delete=models.CASCADE,
        related_name='extension_case',
    )
    extension_start = models.DateField('Начало продления')
    extension_end = models.DateField('Окончание продления')
    decision_status = models.CharField(
        'Решение начальника участка',
        max_length=16,
        choices=DecisionStatus.choices,
        default=DecisionStatus.PENDING,
        db_index=True,
    )
    decision_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто принял решение',
        on_delete=models.SET_NULL,
        related_name='reviewed_watch_extension_cases',
        null=True,
        blank=True,
    )
    decision_at = models.DateTimeField('Решение принято', null=True, blank=True)
    decision_comment = models.TextField('Комментарий начальника участка', blank=True)
    documentation_status = models.CharField(
        'Статус документального оформления',
        max_length=16,
        choices=DocumentationStatus.choices,
        default=DocumentationStatus.NOT_STARTED,
        db_index=True,
    )
    documentation_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто оформил документы',
        on_delete=models.SET_NULL,
        related_name='documented_watch_extension_cases',
        null=True,
        blank=True,
    )
    documentation_at = models.DateTimeField('Документы оформлены', null=True, blank=True)
    documentation_note = models.TextField('Примечание по оформлению', blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Изменено', auto_now=True)

    class Meta:
        verbose_name = 'Заявка на продление вахты'
        verbose_name_plural = 'Заявки на продление вахты'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['decision_status', 'created_at'], name='rot_ext_decision_date_idx'),
            models.Index(
                fields=['documentation_status', 'decision_status'],
                name='rot_ext_document_status_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(extension_end__gte=models.F('extension_start')),
                name='rot_ext_dates_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        decision_status='pending',
                        decision_by__isnull=True,
                        decision_at__isnull=True,
                    )
                    | models.Q(
                        decision_status__in=['approved', 'rejected'],
                        decision_by__isnull=False,
                        decision_at__isnull=False,
                    )
                ),
                name='rot_ext_decision_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        documentation_status='not_started',
                        documentation_by__isnull=True,
                        documentation_at__isnull=True,
                    )
                    | models.Q(
                        documentation_status__in=['data_ready', 'completed'],
                        documentation_by__isnull=False,
                        documentation_at__isnull=False,
                    )
                ),
                name='rot_ext_documentation_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(documentation_status='not_started')
                    | models.Q(decision_status='approved')
                ),
                name='rot_ext_docs_after_approval',
            ),
        ]

    def __str__(self):
        return f'{self.response.snapshot_full_name}: {self.extension_start:%d.%m.%Y}–{self.extension_end:%d.%m.%Y}'


class RotationActionLog(models.Model):
    cycle = models.ForeignKey(
        RotationCollectionCycle,
        verbose_name='Цикл сбора',
        on_delete=models.CASCADE,
        related_name='action_logs',
    )
    response = models.ForeignKey(
        RotationResponse,
        verbose_name='Ответ',
        on_delete=models.SET_NULL,
        related_name='action_logs',
        null=True,
        blank=True,
    )
    extension_case = models.ForeignKey(
        WatchExtensionCase,
        verbose_name='Заявка на продление',
        on_delete=models.SET_NULL,
        related_name='action_logs',
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто выполнил действие',
        on_delete=models.SET_NULL,
        related_name='rotation_actions',
        null=True,
        blank=True,
    )
    action_code = models.CharField('Код действия', max_length=64, db_index=True)
    details = models.JSONField('Детали действия', default=dict, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'Событие перевахты'
        verbose_name_plural = 'События перевахты'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['cycle', 'created_at'], name='rot_log_cycle_date_idx'),
            models.Index(fields=['action_code', 'created_at'], name='rot_log_action_date_idx'),
        ]

    def __str__(self):
        return f'{self.created_at:%d.%m.%Y %H:%M} / {self.action_code}'


class ArrivalRosterImmutableQuerySet(models.QuerySet):
    WRITE_FORBIDDEN_MESSAGE = (
        'Исходные данные реестра заезда изменяются только доменными командами.'
    )

    def _raise_write_forbidden(self):
        raise ValidationError(
            self.WRITE_FORBIDDEN_MESSAGE,
            code='rotations.arrival_roster.public_write_forbidden',
        )

    def update(self, **kwargs):
        self._raise_write_forbidden()

    def bulk_create(self, objs, *args, **kwargs):
        self._raise_write_forbidden()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_write_forbidden()

    def delete(self):
        self._raise_write_forbidden()


class ArrivalRosterImmutableModel(models.Model):
    objects = ArrivalRosterImmutableQuerySet.as_manager()
    IMMUTABLE_FIELDS = ()
    PUBLIC_CREATE_FORBIDDEN = False

    class Meta:
        abstract = True

    @classmethod
    def _write_forbidden_error(cls):
        return ValidationError(
            ArrivalRosterImmutableQuerySet.WRITE_FORBIDDEN_MESSAGE,
            code='rotations.arrival_roster.public_write_forbidden',
        )

    def _validate_immutable_fields(self):
        if not self.pk or not self.IMMUTABLE_FIELDS:
            return
        original = (
            type(self)._base_manager
            .filter(pk=self.pk)
            .values(*self.IMMUTABLE_FIELDS)
            .first()
        )
        if original is None:
            return
        changed = {
            field_name.removesuffix('_id'): 'После создания это поле изменять нельзя.'
            for field_name in self.IMMUTABLE_FIELDS
            if original[field_name] != getattr(self, field_name)
        }
        if changed:
            raise ValidationError(changed)

    def save(self, *args, **kwargs):
        if self._state.adding and self.PUBLIC_CREATE_FORBIDDEN:
            raise self._write_forbidden_error()
        with transaction.atomic(using=kwargs.get('using')):
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
            self._validate_immutable_fields()
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise self._write_forbidden_error()


class ArrivalRosterSourceFile(ArrivalRosterImmutableModel):
    PUBLIC_CREATE_FORBIDDEN = True
    IMMUTABLE_FIELDS = (
        'sha256', 'original_name', 'byte_size', 'content_type',
        'file', 'uploaded_by_access_id',
    )

    sha256 = models.CharField(
        'SHA-256 файла',
        max_length=64,
        unique=True,
        validators=[RegexValidator(regex=r'^[0-9a-f]{64}$')],
    )
    original_name = models.CharField('Исходное имя файла', max_length=255)
    byte_size = models.PositiveBigIntegerField('Размер файла')
    content_type = models.CharField('Тип содержимого', max_length=128)
    file = models.FileField(
        'Закрытый исходный файл',
        storage=arrival_roster_private_storage,
        upload_to='arrival-rosters/',
    )
    uploaded_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ загрузившего',
        on_delete=models.PROTECT,
        related_name='uploaded_arrival_roster_files',
    )
    created_at = models.DateTimeField('Загружен', auto_now_add=True)

    class Meta:
        verbose_name = 'Исходный файл реестра заезда'
        verbose_name_plural = 'Исходные файлы реестров заезда'
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'{self.original_name} / {self.sha256[:12]}'


class ArrivalRosterParserProfile(ArrivalRosterImmutableModel):
    IMMUTABLE_FIELDS = ('code', 'version', 'configuration', 'configuration_sha256')

    code = models.SlugField('Код профиля', max_length=64)
    version = models.PositiveIntegerField('Версия профиля')
    configuration = models.JSONField('Снимок правил разбора')
    configuration_sha256 = models.CharField(
        'SHA-256 правил разбора',
        max_length=64,
        validators=[RegexValidator(regex=r'^[0-9a-f]{64}$')],
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)

    class Meta:
        verbose_name = 'Профиль разбора реестра заезда'
        verbose_name_plural = 'Профили разбора реестра заезда'
        ordering = ['code', 'version']
        constraints = [
            models.UniqueConstraint(
                fields=['code', 'version'],
                name='uniq_arrival_profile_version',
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name='arrival_profile_version_gte_1',
            ),
        ]

    def __str__(self):
        return f'{self.code} / v{self.version}'


class ArrivalRosterVersion(ArrivalRosterImmutableModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        REVIEW_REQUIRED = 'review_required', 'Требуется проверка'

    IMMUTABLE_FIELDS = (
        'watch_period_id', 'version_number', 'source_file_id', 'parser_profile_id',
        'uploaded_by_access_id',
    )

    watch_period = models.ForeignKey(
        'shifts.WatchPeriod',
        verbose_name='Период вахты',
        on_delete=models.PROTECT,
        related_name='arrival_roster_versions',
    )
    version_number = models.PositiveIntegerField('Номер версии')
    status = models.CharField(
        'Состояние',
        max_length=24,
        choices=Status.choices,
        default=Status.REVIEW_REQUIRED,
        db_index=True,
    )
    source_file = models.ForeignKey(
        ArrivalRosterSourceFile,
        verbose_name='Исходный файл',
        on_delete=models.PROTECT,
        related_name='roster_versions',
    )
    parser_profile = models.ForeignKey(
        ArrivalRosterParserProfile,
        verbose_name='Профиль разбора',
        on_delete=models.PROTECT,
        related_name='roster_versions',
    )
    uploaded_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ загрузившего версию',
        on_delete=models.PROTECT,
        related_name='uploaded_arrival_roster_versions',
    )
    source_row_count = models.PositiveIntegerField('Исходных строк', default=0)
    normalized_row_count = models.PositiveIntegerField('Распознано строк', default=0)
    blocking_issue_count = models.PositiveIntegerField('Блокирующих замечаний', default=0)
    warning_count = models.PositiveIntegerField('Предупреждений', default=0)
    snapshot = models.JSONField('Неизменяемый снимок результата', default=dict, blank=True)
    snapshot_sha256 = models.CharField(
        'SHA-256 снимка',
        max_length=64,
        blank=True,
        validators=[RegexValidator(regex=r'^$|^[0-9a-f]{64}$')],
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        verbose_name = 'Версия реестра заезда'
        verbose_name_plural = 'Версии реестра заезда'
        ordering = ['watch_period_id', '-version_number', '-pk']
        indexes = [
            models.Index(
                fields=['watch_period', 'status', 'version_number'],
                name='arrival_ver_period_status_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['watch_period', 'version_number'],
                name='uniq_arrival_period_version',
            ),
            models.UniqueConstraint(
                fields=['watch_period', 'source_file', 'parser_profile'],
                name='uniq_arrival_period_file_profile',
            ),
            models.CheckConstraint(
                condition=models.Q(version_number__gte=1),
                name='arrival_version_number_gte_1',
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=['draft', 'review_required']),
                name='arrival_version_status_t11',
            ),
        ]

    def __str__(self):
        return f'{self.watch_period} / версия {self.version_number}'

    def _validate_immutable_fields(self):
        super()._validate_immutable_fields()
        if not self.pk:
            return
        original = (
            type(self)._base_manager
            .filter(pk=self.pk)
            .values(
                'status', 'source_row_count', 'normalized_row_count',
                'blocking_issue_count', 'warning_count', 'snapshot',
                'snapshot_sha256',
            )
            .first()
        )
        if original is None or not original['snapshot_sha256']:
            return
        changed = {
            field_name: 'Завершённый результат предварительной проверки неизменяем.'
            for field_name, original_value in original.items()
            if original_value != getattr(self, field_name)
        }
        if changed:
            raise ValidationError(changed)


class ArrivalRosterSourceRow(ArrivalRosterImmutableModel):
    PUBLIC_CREATE_FORBIDDEN = True

    class RowKind(models.TextChoices):
        HEADER = 'header', 'Заголовок'
        PERSON = 'person', 'Строка человека'
        SUMMARY = 'summary', 'Сверочная строка'

    IMMUTABLE_FIELDS = (
        'version_id', 'sheet_name', 'row_number', 'row_kind',
        'raw_values', 'raw_styles', 'row_sha256',
    )

    version = models.ForeignKey(
        ArrivalRosterVersion,
        verbose_name='Версия реестра',
        on_delete=models.CASCADE,
        related_name='source_rows',
    )
    sheet_name = models.CharField('Исходный лист', max_length=128)
    row_number = models.PositiveIntegerField('Номер строки')
    row_kind = models.CharField('Тип строки', max_length=16, choices=RowKind.choices)
    raw_values = models.JSONField('Исходные значения')
    raw_styles = models.JSONField('Исходные стили')
    row_sha256 = models.CharField(
        'SHA-256 строки',
        max_length=64,
        validators=[RegexValidator(regex=r'^[0-9a-f]{64}$')],
    )
    created_at = models.DateTimeField('Сохранена', auto_now_add=True)

    class Meta:
        verbose_name = 'Исходная строка реестра заезда'
        verbose_name_plural = 'Исходные строки реестра заезда'
        ordering = ['version_id', 'sheet_name', 'row_number']
        constraints = [
            models.UniqueConstraint(
                fields=['version', 'sheet_name', 'row_number'],
                name='uniq_arrival_source_sheet_row',
            ),
            models.CheckConstraint(
                condition=models.Q(row_number__gte=1),
                name='arrival_source_row_gte_1',
            ),
        ]


class ArrivalRosterNormalizedRow(ArrivalRosterImmutableModel):
    class ParticipationHint(models.TextChoices):
        ARRIVING = 'arriving', 'Предположительно участвует'
        ADDITIONAL = 'additional', 'Предположительно добавлен'
        SELF_TRANSFER = 'self_transfer', 'Предположительно прибывает самостоятельно'
        EXTENDED = 'extended', 'Предположительно продлевает вахту'
        NOT_ARRIVING = 'not_arriving', 'Предположительно не заезжает'
        REVIEW_REQUIRED = 'review_required', 'Требуется проверка'

    IMMUTABLE_FIELDS = (
        'source_row_id', 'raw_full_name', 'normalized_full_name', 'normalized_name_key',
        'name_comment', 'source_position', 'normalized_position_key', 'raw_shift_hint',
        'raw_date', 'arrival_date_candidate', 'date_comment', 'route_text',
        'raw_phone', 'normalized_phones', 'comments', 'participation_hint',
        'color_hint',
    )

    source_row = models.OneToOneField(
        ArrivalRosterSourceRow,
        verbose_name='Исходная строка',
        on_delete=models.PROTECT,
        related_name='normalized',
    )
    raw_full_name = models.TextField('Исходное ФИО')
    normalized_full_name = models.CharField('Нормализованное ФИО', max_length=255)
    normalized_name_key = models.CharField('Ключ ФИО', max_length=255, db_index=True)
    name_comment = models.TextField('Комментарий из ФИО', blank=True)
    source_position = models.CharField('Исходная должность', max_length=255, blank=True)
    normalized_position_key = models.CharField('Ключ должности', max_length=255, blank=True)
    raw_shift_hint = models.CharField('Исходная смена-подсказка', max_length=64, blank=True)
    raw_date = models.TextField('Исходная дата', blank=True)
    arrival_date_candidate = models.DateField('Предполагаемая дата прибытия', null=True, blank=True)
    date_comment = models.TextField('Комментарий к дате', blank=True)
    route_text = models.TextField('Маршрут или способ прибытия', blank=True)
    raw_phone = models.TextField('Исходный телефон', blank=True)
    normalized_phones = models.JSONField('Нормализованные телефоны', default=list, blank=True)
    comments = models.TextField('Исходные пояснения', blank=True)
    participation_hint = models.CharField(
        'Предполагаемое участие',
        max_length=24,
        choices=ParticipationHint.choices,
        default=ParticipationHint.REVIEW_REQUIRED,
    )
    color_hint = models.JSONField('Цветовая подсказка', default=dict, blank=True)
    created_at = models.DateTimeField('Сохранена', auto_now_add=True)

    class Meta:
        verbose_name = 'Нормализованная строка реестра заезда'
        verbose_name_plural = 'Нормализованные строки реестра заезда'
        ordering = ['source_row__version_id', 'source_row__sheet_name', 'source_row__row_number']
        indexes = [
            models.Index(fields=['normalized_name_key'], name='arrival_norm_name_idx'),
        ]

    @property
    def masked_phone(self):
        if not self.normalized_phones:
            return ''
        phone = str(self.normalized_phones[0])
        if len(phone) < 4:
            return '••••'
        return f'•••••••{phone[-4:]}'


class ArrivalRosterMatch(ArrivalRosterImmutableModel):
    class Status(models.TextChoices):
        EXACT = 'exact', 'Точное сопоставление'
        PROBABLE = 'probable', 'Вероятное сопоставление'
        AMBIGUOUS = 'ambiguous', 'Требуется сопоставление'
        UNMATCHED = 'unmatched', 'Жилец не найден'
        CONFLICT = 'conflict', 'Обнаружен конфликт'

    IMMUTABLE_FIELDS = (
        'version_id', 'status', 'method', 'quality', 'matched_resident_id',
        'evidence',
    )

    version = models.ForeignKey(
        ArrivalRosterVersion,
        verbose_name='Версия реестра',
        on_delete=models.CASCADE,
        related_name='matches',
    )
    status = models.CharField('Результат сопоставления', max_length=16, choices=Status.choices)
    method = models.CharField('Способ сопоставления', max_length=64)
    quality = models.CharField('Качество сопоставления', max_length=32)
    matched_resident = models.ForeignKey(
        'settlement.SettlementResident',
        verbose_name='Точный жилец',
        on_delete=models.PROTECT,
        related_name='arrival_roster_matches',
        null=True,
        blank=True,
    )
    evidence = models.JSONField('Доказательства сопоставления', default=dict)
    created_at = models.DateTimeField('Сохранено', auto_now_add=True)

    class Meta:
        verbose_name = 'Результат сопоставления реестра заезда'
        verbose_name_plural = 'Результаты сопоставления реестра заезда'
        ordering = ['version_id', 'pk']
        constraints = [
            models.UniqueConstraint(
                fields=['version', 'matched_resident'],
                condition=models.Q(matched_resident__isnull=False),
                name='uniq_arrival_exact_resident',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status='exact', matched_resident__isnull=False)
                    | models.Q(
                        status__in=['probable', 'ambiguous', 'unmatched', 'conflict'],
                        matched_resident__isnull=True,
                    )
                ),
                name='arrival_match_result_shape',
            ),
        ]


class ArrivalRosterMatchRow(ArrivalRosterImmutableModel):
    IMMUTABLE_FIELDS = ('match_id', 'normalized_row_id')

    match = models.ForeignKey(
        ArrivalRosterMatch,
        verbose_name='Результат сопоставления',
        on_delete=models.CASCADE,
        related_name='row_links',
    )
    normalized_row = models.OneToOneField(
        ArrivalRosterNormalizedRow,
        verbose_name='Нормализованная строка',
        on_delete=models.PROTECT,
        related_name='match_link',
    )
    created_at = models.DateTimeField('Связана', auto_now_add=True)

    class Meta:
        verbose_name = 'Связь результата с исходной строкой'
        verbose_name_plural = 'Связи результатов с исходными строками'


class ArrivalRosterMatchCandidate(ArrivalRosterImmutableModel):
    IMMUTABLE_FIELDS = ('match_id', 'resident_id', 'evidence')

    match = models.ForeignKey(
        ArrivalRosterMatch,
        verbose_name='Результат сопоставления',
        on_delete=models.CASCADE,
        related_name='candidates',
    )
    resident = models.ForeignKey(
        'settlement.SettlementResident',
        verbose_name='Возможный жилец',
        on_delete=models.PROTECT,
        related_name='arrival_roster_match_candidates',
    )
    evidence = models.JSONField('Доказательства кандидата', default=dict)
    created_at = models.DateTimeField('Сохранён', auto_now_add=True)

    class Meta:
        verbose_name = 'Кандидат сопоставления реестра заезда'
        verbose_name_plural = 'Кандидаты сопоставления реестра заезда'
        constraints = [
            models.UniqueConstraint(
                fields=['match', 'resident'],
                name='uniq_arrival_match_candidate',
            ),
        ]


class ArrivalRosterIssue(ArrivalRosterImmutableModel):
    class Severity(models.TextChoices):
        ERROR = 'error', 'Ошибка'
        WARNING = 'warning', 'Предупреждение'

    IMMUTABLE_FIELDS = (
        'version_id', 'source_row_id', 'normalized_row_id', 'match_id',
        'severity', 'code', 'message', 'details',
    )

    version = models.ForeignKey(
        ArrivalRosterVersion,
        verbose_name='Версия реестра',
        on_delete=models.CASCADE,
        related_name='issues',
    )
    source_row = models.ForeignKey(
        ArrivalRosterSourceRow,
        verbose_name='Исходная строка',
        on_delete=models.PROTECT,
        related_name='issues',
        null=True,
        blank=True,
    )
    normalized_row = models.ForeignKey(
        ArrivalRosterNormalizedRow,
        verbose_name='Нормализованная строка',
        on_delete=models.PROTECT,
        related_name='issues',
        null=True,
        blank=True,
    )
    match = models.ForeignKey(
        ArrivalRosterMatch,
        verbose_name='Результат сопоставления',
        on_delete=models.PROTECT,
        related_name='issues',
        null=True,
        blank=True,
    )
    severity = models.CharField('Важность', max_length=16, choices=Severity.choices)
    code = models.CharField('Код', max_length=64, db_index=True)
    message = models.CharField('Описание', max_length=255)
    details = models.JSONField('Безопасные детали', default=dict, blank=True)
    created_at = models.DateTimeField('Обнаружено', auto_now_add=True)

    class Meta:
        verbose_name = 'Замечание реестра заезда'
        verbose_name_plural = 'Замечания реестра заезда'
        ordering = ['version_id', 'severity', 'pk']
        indexes = [
            models.Index(fields=['version', 'severity'], name='arrival_issue_severity_idx'),
        ]


class ArrivalRosterProtectedProjection(ArrivalRosterImmutableModel):
    PUBLIC_CREATE_FORBIDDEN = True

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        raise self._write_forbidden_error()


class ArrivalRosterRowReview(ArrivalRosterProtectedProjection):
    class ResidentResolution(models.TextChoices):
        UNREVIEWED = 'unreviewed', 'Не проверено'
        SELECTED = 'selected', 'Жилец выбран'
        CLEARED = 'cleared', 'Сопоставление отменено'

    class ParticipationStatus(models.TextChoices):
        ARRIVING = 'arriving', 'Заезжает'
        NOT_ARRIVING = 'not_arriving', 'Не заезжает'
        EXTENDED = 'extended', 'Продлевается'
        ADDITIONAL = 'additional', 'Дополнительный человек'

    class ArrivalMode(models.TextChoices):
        TRANSFER = 'transfer', 'Трансфер'
        SELF = 'self', 'Самостоятельно'

    version = models.ForeignKey(
        ArrivalRosterVersion,
        verbose_name='Версия реестра',
        on_delete=models.PROTECT,
        related_name='row_reviews',
    )
    match = models.OneToOneField(
        ArrivalRosterMatch,
        verbose_name='Исходное сопоставление',
        on_delete=models.PROTECT,
        related_name='row_review',
    )
    resident_resolution = models.CharField(
        'Решение по жильцу',
        max_length=16,
        choices=ResidentResolution.choices,
        default=ResidentResolution.UNREVIEWED,
    )
    selected_resident = models.ForeignKey(
        'settlement.SettlementResident',
        verbose_name='Выбранный жилец',
        on_delete=models.PROTECT,
        related_name='arrival_roster_row_reviews',
        null=True,
        blank=True,
    )
    participation_status = models.CharField(
        'Участие в заезде',
        max_length=16,
        choices=ParticipationStatus.choices,
        null=True,
        blank=True,
    )
    arrival_mode = models.CharField(
        'Способ прибытия',
        max_length=16,
        choices=ArrivalMode.choices,
        null=True,
        blank=True,
    )
    arrival_on = models.DateField('Дата заселения', null=True, blank=True)
    departure_on = models.DateField('Дата выбытия', null=True, blank=True)
    basis = models.TextField('Основание', blank=True)
    comment = models.TextField('Комментарий', blank=True)
    revision = models.PositiveIntegerField('Ревизия', default=1)
    updated_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ табельщика',
        on_delete=models.PROTECT,
        related_name='updated_arrival_roster_rows',
    )
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Ручная проверка строки реестра'
        verbose_name_plural = 'Ручная проверка строк реестра'
        ordering = ['version_id', 'match_id']
        indexes = [
            models.Index(fields=['version', 'resident_resolution'], name='arrival_review_state_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['version', 'selected_resident'],
                condition=models.Q(selected_resident__isnull=False),
                name='uniq_arrival_selected_resident',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(resident_resolution='selected', selected_resident__isnull=False)
                    | models.Q(
                        resident_resolution__in=['unreviewed', 'cleared'],
                        selected_resident__isnull=True,
                    )
                ),
                name='arrival_review_resident_shape',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(arrival_on__isnull=True)
                    | models.Q(departure_on__isnull=True)
                    | models.Q(departure_on__gte=models.F('arrival_on'))
                ),
                name='arrival_review_dates_order',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(arrival_mode__isnull=True)
                    | models.Q(participation_status='arriving')
                ),
                name='arrival_review_mode_shape',
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name='arrival_review_revision_gte_1',
            ),
        ]

    def clean(self):
        super().clean()
        if self.match_id and self.version_id and self.match.version_id != self.version_id:
            raise ValidationError({'match': 'Сопоставление относится к другой версии реестра.'})


class ArrivalRosterIssueResolution(ArrivalRosterProtectedProjection):
    issue = models.OneToOneField(
        ArrivalRosterIssue,
        verbose_name='Вопрос',
        on_delete=models.PROTECT,
        related_name='resolution',
    )
    is_resolved = models.BooleanField('Вопрос решён', default=False)
    resolution_note = models.TextField('Пояснение')
    revision = models.PositiveIntegerField('Ревизия', default=1)
    updated_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ табельщика',
        on_delete=models.PROTECT,
        related_name='resolved_arrival_roster_issues',
    )
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Решение вопроса реестра'
        verbose_name_plural = 'Решения вопросов реестра'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name='arrival_resolution_revision_gte_1',
            ),
            models.CheckConstraint(
                condition=~models.Q(resolution_note=''),
                name='arrival_resolution_note_required',
            ),
        ]


class ArrivalRosterEvent(ArrivalRosterImmutableModel):
    PUBLIC_CREATE_FORBIDDEN = True

    class Action(models.TextChoices):
        UPLOADED = 'uploaded', 'Файл загружен'
        REUSED = 'reused', 'Повторная загрузка распознана'
        PARSED = 'parsed', 'Предварительная проверка завершена'
        RESIDENT_SELECTED = 'resident_selected', 'Жилец выбран'
        RESIDENT_CLEARED = 'resident_cleared', 'Сопоставление отменено'
        PARTICIPATION_CHANGED = 'participation_changed', 'Участие изменено'
        ARRIVAL_MODE_CHANGED = 'arrival_mode_changed', 'Способ прибытия изменён'
        DATES_CHANGED = 'dates_changed', 'Даты изменены'
        NOTES_CHANGED = 'notes_changed', 'Основание или комментарий изменены'
        ISSUE_RESOLVED = 'issue_resolved', 'Вопрос решён'
        ISSUE_REOPENED = 'issue_reopened', 'Вопрос возвращён на проверку'

    IMMUTABLE_FIELDS = (
        'version_id', 'actor_access_id', 'match_id', 'issue_id',
        'review_revision', 'action', 'details',
    )

    version = models.ForeignKey(
        ArrivalRosterVersion,
        verbose_name='Версия реестра',
        on_delete=models.CASCADE,
        related_name='events',
    )
    actor_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ исполнителя',
        on_delete=models.PROTECT,
        related_name='arrival_roster_events',
    )
    match = models.ForeignKey(
        ArrivalRosterMatch,
        verbose_name='Сопоставление',
        on_delete=models.PROTECT,
        related_name='review_events',
        null=True,
        blank=True,
    )
    issue = models.ForeignKey(
        ArrivalRosterIssue,
        verbose_name='Вопрос',
        on_delete=models.PROTECT,
        related_name='review_events',
        null=True,
        blank=True,
    )
    review_revision = models.PositiveIntegerField(
        'Ревизия ручной проверки',
        null=True,
        blank=True,
    )
    action = models.CharField('Действие', max_length=24, choices=Action.choices)
    details = models.JSONField('Безопасные детали', default=dict, blank=True)
    created_at = models.DateTimeField('Время', auto_now_add=True)

    class Meta:
        verbose_name = 'Событие реестра заезда'
        verbose_name_plural = 'События реестра заезда'
        ordering = ['version_id', 'created_at', 'pk']
        indexes = [
            models.Index(fields=['version', 'created_at'], name='arrival_event_version_idx'),
        ]
