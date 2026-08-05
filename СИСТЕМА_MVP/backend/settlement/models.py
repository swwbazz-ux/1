import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, router, transaction
from django.db.models import sql
from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone


class StableIdentifierModel(models.Model):
    stable_id = models.UUIDField(
        'Стабильный идентификатор',
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )

    class Meta:
        abstract = True

    def _validate_immutable_fields(self, *field_names):
        if not self.pk:
            return

        fields = ('stable_id', *field_names)
        original = type(self).objects.filter(pk=self.pk).values(*fields).first()
        if not original:
            return

        errors = {}
        for field_name in fields:
            field = self._meta.get_field(field_name)
            current_value = getattr(self, field.attname)
            if original[field_name] != current_value:
                errors[field_name] = 'После создания это поле изменять нельзя.'
        if errors:
            raise ValidationError(errors)


class SettlementSourceQuerySet(models.QuerySet):
    def _validate_mass_immutable_fields(self, fields):
        immutable_fields = set(fields).intersection(
            self.model.ALWAYS_IMMUTABLE_FIELDS,
        )
        if immutable_fields:
            raise ValidationError(
                {
                    field_name: 'После создания это поле изменять нельзя.'
                    for field_name in immutable_fields
                }
            )

    def _lock_and_validate_confirmed_sources(self):
        locked_rows = tuple(
            self.select_for_update().values_list('pk', 'status')
        )
        if any(
            status == self.model.Status.CONFIRMED
            for _pk, status in locked_rows
        ):
            raise self.model._confirmed_immutability_error()
        return tuple(dict.fromkeys(pk for pk, _status in locked_rows))

    def update(self, **kwargs):
        self._not_support_combined_queries('update')
        if self.query.is_sliced:
            raise TypeError('Cannot update a query once a slice has been taken.')

        self._for_write = True
        validation_query = self.query.chain(sql.UpdateQuery)
        validation_query.add_update_values(kwargs)

        db_alias = self.db
        updated_count = 0

        with transaction.atomic(using=db_alias):
            locked_rows = tuple(
                self.using(db_alias)
                .select_for_update()
                .values_list('pk', 'status')
            )
            locked_pks = tuple(
                dict.fromkeys(pk for pk, _status in locked_rows)
            )

            if locked_pks:
                self._validate_mass_immutable_fields(kwargs)
                protected_fields = set(kwargs).intersection(
                    self.model.CONFIRMED_IMMUTABLE_FIELDS,
                )
                if protected_fields and any(
                    status == self.model.Status.CONFIRMED
                    for _pk, status in locked_rows
                ):
                    raise self.model._confirmed_immutability_error()

                locked_queryset = models.QuerySet(
                    model=self.model,
                    using=db_alias,
                ).filter(pk__in=locked_pks)
                updated_count = models.QuerySet.update(
                    locked_queryset,
                    **kwargs,
                )

        self._result_cache = None
        return updated_count

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if not update_conflicts:
            return models.QuerySet.bulk_create(
                self,
                objs,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )

        if batch_size is not None and batch_size <= 0:
            raise ValueError('Batch size must be a positive integer.')
        for parent in self.model._meta.all_parents:
            if parent._meta.concrete_model is not self.model._meta.concrete_model:
                raise ValueError("Can't bulk create a multi-table inherited model")
        if not objs:
            return models.QuerySet.bulk_create(
                self,
                objs,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )

        opts = self.model._meta
        unique_field_names = (
            tuple(unique_fields)
            if unique_fields is not None
            else None
        )
        update_field_names = (
            tuple(update_fields)
            if update_fields is not None
            else None
        )
        normalized_unique_fields = None
        if unique_field_names:
            normalized_unique_fields = [
                opts.get_field(opts.pk.name if name == 'pk' else name)
                for name in unique_field_names
            ]
        normalized_update_fields = None
        if update_field_names:
            normalized_update_fields = [
                opts.get_field(name)
                for name in update_field_names
            ]
        self._check_bulk_create_options(
            ignore_conflicts,
            update_conflicts,
            normalized_update_fields,
            normalized_unique_fields,
        )

        materialized_objs = list(objs)
        if not materialized_objs:
            return materialized_objs

        normalized_update_field_names = {
            field.name
            for field in normalized_update_fields
        }
        immutable_fields = (
            set(self.model.ALWAYS_IMMUTABLE_FIELDS)
            | set(self.model.CONFIRMED_IMMUTABLE_FIELDS)
        )
        if normalized_update_field_names.intersection(immutable_fields):
            self._validate_mass_immutable_fields(normalized_update_field_names)
            raise self.model._confirmed_immutability_error()

        return models.QuerySet.bulk_create(
            self,
            materialized_objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_field_names,
            unique_fields=unique_field_names,
        )

    bulk_create.alters_data = True

    def bulk_update(self, objs, fields, batch_size=None):
        objs = tuple(objs)
        if objs:
            self._validate_mass_immutable_fields(fields)
        protected_fields = set(fields).intersection(
            self.model.CONFIRMED_IMMUTABLE_FIELDS,
        )
        if not protected_fields:
            return super().bulk_update(objs, fields, batch_size=batch_size)

        object_ids = [obj.pk for obj in objs if obj.pk is not None]
        with transaction.atomic():
            self.filter(pk__in=object_ids)._lock_and_validate_confirmed_sources()
            return super().bulk_update(objs, fields, batch_size=batch_size)


class SettlementSource(StableIdentifierModel):
    class SourceType(models.TextChoices):
        DOCUMENT = 'document', 'Документ'
        FILE = 'file', 'Файл'
        MANUAL = 'manual', 'Ручное решение'
        SYSTEM = 'system', 'Информационная система'
        OTHER = 'other', 'Другой источник'

    class Status(models.TextChoices):
        CANDIDATE = 'candidate', 'Кандидат'
        CONFIRMED = 'confirmed', 'Подтверждён'
        REJECTED = 'rejected', 'Отклонён'
        ARCHIVED = 'archived', 'Архивный'

    ALWAYS_IMMUTABLE_FIELDS = ('stable_id',)
    CONFIRMED_IMMUTABLE_FIELDS = (
        'source_type',
        'title',
        'external_reference',
        'version',
        'document_number',
        'document_date',
        'file_sha256',
        'status',
        'confirmed_at',
        'confirmed_by_label',
        'notes',
    )
    CONFIRMED_IMMUTABILITY_MESSAGE = (
        'Подтверждённый источник данных расселения изменять нельзя.'
    )

    objects = SettlementSourceQuerySet.as_manager()

    source_type = models.CharField(
        'Тип источника',
        max_length=16,
        choices=SourceType.choices,
    )
    title = models.CharField('Наименование', max_length=255)
    external_reference = models.CharField(
        'Внешняя ссылка или идентификатор',
        max_length=500,
        blank=True,
    )
    version = models.CharField('Версия', max_length=128, blank=True)
    document_number = models.CharField('Номер документа', max_length=128, blank=True)
    document_date = models.DateField('Дата документа', null=True, blank=True)
    file_sha256 = models.CharField(
        'SHA-256 файла',
        max_length=64,
        blank=True,
        validators=[
            RegexValidator(
                regex=r'^[0-9a-fA-F]{64}$',
                message='SHA-256 должен содержать ровно 64 шестнадцатеричных символа.',
            ),
        ],
    )
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.CANDIDATE,
        db_index=True,
    )
    confirmed_at = models.DateTimeField('Подтверждён', null=True, blank=True)
    confirmed_by_label = models.CharField(
        'Кем подтверждён',
        max_length=255,
        blank=True,
        help_text='Снимок имени или реквизитов подтвердившего лица без связи с сотрудником.',
    )
    notes = models.TextField('Примечание', blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Источник данных расселения'
        verbose_name_plural = 'Источники данных расселения'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~models.Q(status='confirmed')
                    | (
                        models.Q(confirmed_at__isnull=False)
                        & ~models.Q(confirmed_by_label='')
                    )
                ),
                name='settlement_source_confirmed_meta',
            ),
        ]

    def clean(self):
        super().clean()
        self.file_sha256 = self.file_sha256.strip().lower()
        if self.status == self.Status.CONFIRMED:
            errors = {}
            if not self.confirmed_at:
                errors['confirmed_at'] = 'Для подтверждённого источника укажите время подтверждения.'
            if not self.confirmed_by_label.strip():
                errors['confirmed_by_label'] = 'Для подтверждённого источника укажите подтвердившее лицо.'
            if errors:
                raise ValidationError(errors)

    @classmethod
    def _confirmed_immutability_error(cls):
        return ValidationError(cls.CONFIRMED_IMMUTABILITY_MESSAGE)

    def _validate_confirmed_immutability(self, original, update_fields=None):
        if not original or original['status'] != self.Status.CONFIRMED:
            return

        fields_to_check = set(self.CONFIRMED_IMMUTABLE_FIELDS)
        if update_fields is not None:
            fields_to_check.intersection_update(update_fields)

        if any(
            original[field_name] != getattr(self, field_name)
            for field_name in fields_to_check
        ):
            raise self._confirmed_immutability_error()

    def save(self, *args, **kwargs):
        with transaction.atomic():
            original = None
            if self.pk:
                original = (
                    type(self)._base_manager.select_for_update()
                    .filter(pk=self.pk)
                    .values(*self.CONFIRMED_IMMUTABLE_FIELDS)
                    .first()
                )
            self._validate_confirmed_immutability(
                original,
                update_fields=kwargs.get('update_fields'),
            )
            self._validate_immutable_fields()
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        version = f' / {self.version}' if self.version else ''
        return f'{self.title}{version}'


class SettlementRevisionQuerySet(models.QuerySet):
    def _validate_persisted_identity_fields(self, fields):
        immutable_fields = set(fields).intersection(
            self.model.PERSISTED_IMMUTABLE_FIELDS,
        )
        if immutable_fields:
            raise ValidationError(
                {
                    field_name: self.model.IMMUTABLE_FIELD_MESSAGE
                    for field_name in immutable_fields
                }
            )

    def update(self, **kwargs):
        if self.query.is_empty():
            return super().update(**kwargs)

        self._not_support_combined_queries('update')
        if self.query.is_sliced:
            raise TypeError('Cannot update a query once a slice has been taken.')

        self._for_write = True
        validation_query = self.query.chain(sql.UpdateQuery)
        validation_query.add_update_values(kwargs)
        db_alias = self.db
        updated_count = 0

        with transaction.atomic(using=db_alias):
            locked_rows = tuple(
                self.using(db_alias)
                .select_for_update()
                .values_list('pk', 'status')
            )
            locked_pks = tuple(
                dict.fromkeys(pk for pk, _status in locked_rows)
            )

            if locked_pks:
                if any(
                    status == self.model.Status.CONFIRMED
                    for _pk, status in locked_rows
                ):
                    raise self.model._confirmed_revision_immutability_error()
                self._validate_persisted_identity_fields(kwargs)

                locked_queryset = models.QuerySet(
                    model=self.model,
                    using=db_alias,
                ).filter(pk__in=locked_pks)
                updated_count = models.QuerySet.update(
                    locked_queryset,
                    **kwargs,
                )

        self._result_cache = None
        return updated_count

    update.alters_data = True
    update.queryset_only = False

    def bulk_update(self, objs, fields, batch_size=None):
        if batch_size is not None and batch_size <= 0:
            raise ValueError('Batch size must be a positive integer.')
        if not fields:
            raise ValueError('Field names must be given to bulk_update().')

        objs = tuple(objs)
        if not all(obj._is_pk_set() for obj in objs):
            raise ValueError('All bulk_update() objects must have a primary key set.')

        opts = self.model._meta
        field_names = tuple(fields)
        normalized_fields = [opts.get_field(name) for name in field_names]
        if any(not field.concrete for field in normalized_fields):
            raise ValueError('bulk_update() can only be used with concrete fields.')
        all_pk_fields = set(opts.pk_fields)
        for parent in opts.all_parents:
            all_pk_fields.update(parent._meta.pk_fields)
        if any(field in all_pk_fields for field in normalized_fields):
            raise ValueError('bulk_update() cannot be used with primary key fields.')
        if not objs:
            return 0

        for obj in objs:
            obj._prepare_related_fields_for_save(
                operation_name='bulk_update',
                fields=normalized_fields,
            )

        self._for_write = True
        db_alias = self.db
        input_pks = tuple(dict.fromkeys(obj.pk for obj in objs))
        write_queryset = self.using(db_alias)

        with transaction.atomic(using=db_alias):
            locked_rows = tuple(
                write_queryset
                .select_for_update()
                .filter(pk__in=input_pks)
                .values_list('pk', 'status')
            )
            locked_pks = tuple(
                dict.fromkeys(pk for pk, _status in locked_rows)
            )

            if locked_pks:
                if any(
                    status == self.model.Status.CONFIRMED
                    for _pk, status in locked_rows
                ):
                    raise self.model._confirmed_revision_immutability_error()
                self._validate_persisted_identity_fields(
                    field.name for field in normalized_fields
                )

            updated_count = super(
                SettlementRevisionQuerySet,
                write_queryset,
            ).bulk_update(
                objs,
                field_names,
                batch_size=batch_size,
            )

        return updated_count

    bulk_update.alters_data = True

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if not update_conflicts:
            return models.QuerySet.bulk_create(
                self,
                objs,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )

        if batch_size is not None and batch_size <= 0:
            raise ValueError('Batch size must be a positive integer.')
        for parent in self.model._meta.all_parents:
            if parent._meta.concrete_model is not self.model._meta.concrete_model:
                raise ValueError("Can't bulk create a multi-table inherited model")
        if not objs:
            return models.QuerySet.bulk_create(
                self,
                objs,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            )

        opts = self.model._meta
        unique_field_names = (
            tuple(unique_fields)
            if unique_fields is not None
            else None
        )
        update_field_names = (
            tuple(update_fields)
            if update_fields is not None
            else None
        )
        normalized_unique_fields = None
        if unique_field_names:
            normalized_unique_fields = [
                opts.get_field(opts.pk.name if name == 'pk' else name)
                for name in unique_field_names
            ]
        normalized_update_fields = None
        if update_field_names:
            normalized_update_fields = [
                opts.get_field(name)
                for name in update_field_names
            ]
        self._check_bulk_create_options(
            ignore_conflicts,
            update_conflicts,
            normalized_update_fields,
            normalized_unique_fields,
        )

        materialized_objs = list(objs)
        if not materialized_objs:
            return materialized_objs

        raise self.model._revision_upsert_forbidden_error()

    bulk_create.alters_data = True


class SettlementRevision(StableIdentifierModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        CONFIRMED = 'confirmed', 'Подтверждена'
        CANCELLED = 'cancelled', 'Отменена'

    PERSISTED_IMMUTABLE_FIELDS = ('stable_id', 'code')
    IMMUTABLE_FIELD_MESSAGE = 'После создания это поле изменять нельзя.'
    CONFIRMED_REVISION_IMMUTABILITY_MESSAGE = (
        'Подтверждённую редакцию расселения изменять нельзя.'
    )
    CONFIRMED_REVISION_DELETE_MESSAGE = (
        'Подтверждённую редакцию расселения удалять нельзя.'
    )
    REVISION_UPSERT_FORBIDDEN_MESSAGE = (
        'Редакции расселения нельзя изменять через bulk_create upsert.'
    )

    objects = SettlementRevisionQuerySet.as_manager()

    code = models.CharField('Код ревизии', max_length=64, unique=True)
    source = models.ForeignKey(
        SettlementSource,
        verbose_name='Источник',
        on_delete=models.PROTECT,
        related_name='revisions',
    )
    supersedes = models.ForeignKey(
        'self',
        verbose_name='Заменяет ревизию',
        on_delete=models.PROTECT,
        related_name='replacements',
        null=True,
        blank=True,
    )
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    effective_at = models.DateTimeField('Действует с', null=True, blank=True)
    confirmed_at = models.DateTimeField('Подтверждена', null=True, blank=True)
    confirmed_by_label = models.CharField(
        'Кем подтверждена',
        max_length=255,
        blank=True,
        help_text='Снимок имени или реквизитов подтвердившего лица без связи с сотрудником.',
    )
    reason = models.TextField('Основание изменения')
    comment = models.TextField('Комментарий', blank=True)
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Изменена', auto_now=True)

    class Meta:
        verbose_name = 'Ревизия расселения'
        verbose_name_plural = 'Ревизии расселения'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~models.Q(status='confirmed')
                    | (
                        models.Q(effective_at__isnull=False)
                        & models.Q(confirmed_at__isnull=False)
                        & ~models.Q(confirmed_by_label='')
                    )
                ),
                name='settlement_revision_confirmed_meta',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(supersedes__isnull=True)
                    | ~models.Q(pk=models.F('supersedes_id'))
                ),
                name='settlement_revision_not_self',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.supersedes_id and self.supersedes_id == self.pk:
            errors['supersedes'] = 'Ревизия не может заменять саму себя.'
        if self.status == self.Status.CONFIRMED:
            if self.source.status != SettlementSource.Status.CONFIRMED:
                errors['source'] = 'Подтверждённая ревизия требует подтверждённого источника.'
            if not self.effective_at:
                errors['effective_at'] = 'Для подтверждённой ревизии укажите начало действия.'
            if not self.confirmed_at:
                errors['confirmed_at'] = 'Для подтверждённой ревизии укажите время подтверждения.'
            if not self.confirmed_by_label.strip():
                errors['confirmed_by_label'] = 'Укажите подтвердившее лицо.'
        if errors:
            raise ValidationError(errors)

    @classmethod
    def _confirmed_revision_immutability_error(cls):
        return ValidationError(
            cls.CONFIRMED_REVISION_IMMUTABILITY_MESSAGE,
            code='confirmed_revision_immutable',
        )

    @classmethod
    def _confirmed_revision_delete_error(cls):
        return ValidationError(
            cls.CONFIRMED_REVISION_DELETE_MESSAGE,
            code='confirmed_revision_delete_protected',
        )

    @classmethod
    def _revision_upsert_forbidden_error(cls):
        return ValidationError(
            cls.REVISION_UPSERT_FORBIDDEN_MESSAGE,
            code='revision_upsert_forbidden',
        )

    def _validate_persisted_identity(self, original):
        if not original:
            return

        errors = {}
        for field_name in self.PERSISTED_IMMUTABLE_FIELDS:
            if original[field_name] != getattr(self, field_name):
                errors[field_name] = self.IMMUTABLE_FIELD_MESSAGE
        if errors:
            raise ValidationError(errors)

    def save(
        self,
        *,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):
        if update_fields is not None and not update_fields:
            return super().save(
                force_insert=force_insert,
                force_update=force_update,
                using=using,
                update_fields=update_fields,
            )

        db_alias = using or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=db_alias):
            original = None
            if self._is_pk_set():
                original = (
                    type(self)._base_manager.using(db_alias)
                    .select_for_update()
                    .filter(pk=self.pk)
                    .values('status', *self.PERSISTED_IMMUTABLE_FIELDS)
                    .first()
                )
            if original and original['status'] == self.Status.CONFIRMED:
                raise self._confirmed_revision_immutability_error()

            self._validate_persisted_identity(original)
            self.full_clean()
            return super().save(
                force_insert=force_insert,
                force_update=force_update,
                using=db_alias,
                update_fields=update_fields,
            )

    def __str__(self):
        return self.code


@receiver(
    pre_delete,
    sender=SettlementRevision,
    dispatch_uid='settlement.protect_confirmed_revision_delete',
)
def protect_confirmed_settlement_revision(sender, instance, using, **kwargs):
    persisted_status = (
        sender._base_manager.using(using)
        .select_for_update()
        .filter(pk=instance.pk)
        .values_list('status', flat=True)
        .first()
    )
    if persisted_status == sender.Status.CONFIRMED:
        raise sender._confirmed_revision_delete_error()


class AccommodationAnchor(StableIdentifierModel):
    class AnchorType(models.TextChoices):
        EQUIPMENT = 'equipment', 'Техника'
        FUNCTION = 'function', 'Должность или функция'
        RESERVE = 'reserve', 'Резерв'
        GROUP = 'group', 'Группа'
        SERVICE = 'service', 'Служебный'
        PROTECTED = 'protected', 'Защищённый'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        ACTIVE = 'active', 'Действует'
        ARCHIVED = 'archived', 'Архивный'

    code = models.CharField('Код якоря', max_length=64, unique=True)
    display_name = models.CharField('Отображаемое наименование', max_length=255)
    anchor_type = models.CharField(
        'Тип якоря',
        max_length=16,
        choices=AnchorType.choices,
    )
    equipment = models.ForeignKey(
        'references.Equipment',
        verbose_name='Техника',
        on_delete=models.PROTECT,
        related_name='accommodation_anchors',
        null=True,
        blank=True,
    )
    personnel_position = models.ForeignKey(
        'users.PersonnelPosition',
        verbose_name='Кадровая должность',
        on_delete=models.PROTECT,
        related_name='accommodation_anchors',
        null=True,
        blank=True,
    )
    personnel_department = models.ForeignKey(
        'users.PersonnelDepartment',
        verbose_name='Подразделение',
        on_delete=models.PROTECT,
        related_name='accommodation_anchors',
        null=True,
        blank=True,
    )
    function_key = models.CharField('Код функции', max_length=128, blank=True)
    group_key = models.CharField('Код группы', max_length=128, blank=True)
    ordinal = models.PositiveSmallIntegerField(
        'Порядковый номер внутри группы',
        default=1,
        validators=[MinValueValidator(1)],
    )
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    created_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия создания',
        on_delete=models.PROTECT,
        related_name='created_anchors',
    )
    archived_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия архивирования',
        on_delete=models.PROTECT,
        related_name='archived_anchors',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Жилищный якорь'
        verbose_name_plural = 'Жилищные якоря'
        ordering = ['code']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ordinal__gte=1),
                name='accommodation_anchor_ordinal_gte_1',
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status='archived')
                    | models.Q(archived_revision__isnull=False)
                ),
                name='accommodation_anchor_archive_revision',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.status in {self.Status.ACTIVE, self.Status.ARCHIVED}:
            if self.created_revision.status != SettlementRevision.Status.CONFIRMED:
                errors['created_revision'] = 'Действующий якорь требует подтверждённой ревизии создания.'
        if self.status == self.Status.ARCHIVED:
            if not self.archived_revision_id:
                errors['archived_revision'] = 'Для архивного якоря укажите ревизию архивирования.'
            elif self.archived_revision.status != SettlementRevision.Status.CONFIRMED:
                errors['archived_revision'] = 'Архивирование требует подтверждённой ревизии.'
        elif self.archived_revision_id:
            errors['archived_revision'] = 'Ревизия архивирования допустима только для архивного якоря.'
        if errors:
            raise ValidationError(errors)

    def _validate_existing_state(self):
        if not self.pk:
            return
        original = type(self).objects.filter(pk=self.pk).values(
            'stable_id',
            'code',
            'anchor_type',
            'equipment_id',
            'personnel_position_id',
            'personnel_department_id',
            'function_key',
            'group_key',
            'ordinal',
            'status',
            'created_revision_id',
            'archived_revision_id',
        ).first()
        if not original:
            return

        errors = {}
        for field_name in (
            'stable_id',
            'code',
            'anchor_type',
            'equipment_id',
            'personnel_position_id',
            'personnel_department_id',
            'function_key',
            'group_key',
            'ordinal',
            'created_revision_id',
        ):
            if original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = (
                    'Структуру созданного жилищного якоря изменять нельзя.'
                )
        if original['status'] == self.Status.ACTIVE and self.status == self.Status.DRAFT:
            errors['status'] = 'Действующий якорь нельзя вернуть в черновик.'
        if original['status'] == self.Status.ARCHIVED and self.status != self.Status.ARCHIVED:
            errors['status'] = 'Архивный якорь нельзя восстановить.'
        if (
            original['archived_revision_id'] is not None
            and original['archived_revision_id'] != self.archived_revision_id
        ):
            errors['archived_revision'] = 'Ревизию архивирования менять нельзя.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self).objects.select_for_update().get(pk=self.pk)
            self._validate_existing_state()
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.code} — {self.display_name}'


class AccommodationAnchorBedAssignment(StableIdentifierModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        CONFIRMED = 'confirmed', 'Подтверждено'
        CANCELLED = 'cancelled', 'Отменено'

    anchor = models.ForeignKey(
        AccommodationAnchor,
        verbose_name='Жилищный якорь',
        on_delete=models.PROTECT,
        related_name='bed_assignments',
    )
    physical_bed = models.ForeignKey(
        'PhysicalBed',
        verbose_name='Физическое койко-место',
        on_delete=models.PROTECT,
        related_name='anchor_assignments',
    )
    valid_from = models.DateTimeField('Действует с')
    valid_to = models.DateTimeField(
        'Действует до',
        null=True,
        blank=True,
        help_text='Пустое значение означает открытый период. Окончание в период не входит.',
    )
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    started_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия начала',
        on_delete=models.PROTECT,
        related_name='started_anchor_bed_assignments',
    )
    ended_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия окончания',
        on_delete=models.PROTECT,
        related_name='ended_anchor_bed_assignments',
        null=True,
        blank=True,
    )
    cancelled_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия отмены',
        on_delete=models.PROTECT,
        related_name='cancelled_anchor_bed_assignments',
        null=True,
        blank=True,
    )
    comment = models.TextField('Комментарий', blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Изменено', auto_now=True)

    class Meta:
        verbose_name = 'Закрепление жилищного якоря за койко-местом'
        verbose_name_plural = 'Закрепления жилищных якорей за койко-местами'
        ordering = ['anchor', '-valid_from']
        indexes = [
            models.Index(
                fields=['anchor', 'status', 'valid_from'],
                name='anchor_bed_anchor_period_idx',
            ),
            models.Index(
                fields=['physical_bed', 'status', 'valid_from'],
                name='anchor_bed_bed_period_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(valid_to__isnull=True)
                    | models.Q(valid_to__gt=models.F('valid_from'))
                ),
                name='anchor_bed_period_non_empty',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(valid_to__isnull=True, ended_revision__isnull=True)
                    | models.Q(valid_to__isnull=False, ended_revision__isnull=False)
                ),
                name='anchor_bed_end_revision_consistent',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status='cancelled', cancelled_revision__isnull=False)
                    | (~models.Q(status='cancelled') & models.Q(cancelled_revision__isnull=True))
                ),
                name='anchor_bed_cancel_revision_consistent',
            ),
            models.UniqueConstraint(
                fields=['anchor'],
                condition=models.Q(status='confirmed', valid_to__isnull=True),
                name='unique_open_confirmed_bed_per_anchor',
            ),
            models.UniqueConstraint(
                fields=['physical_bed'],
                condition=models.Q(status='confirmed', valid_to__isnull=True),
                name='unique_open_confirmed_anchor_per_bed',
            ),
        ]

    def _overlapping_confirmed_assignments(self):
        queryset = type(self).objects.filter(status=self.Status.CONFIRMED)
        if self.pk:
            queryset = queryset.exclude(pk=self.pk)
        if self.valid_to is not None:
            queryset = queryset.filter(valid_from__lt=self.valid_to)
        return queryset.filter(
            models.Q(valid_to__isnull=True) | models.Q(valid_to__gt=self.valid_from),
        )

    def clean(self):
        super().clean()
        errors = {}
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            errors['valid_to'] = 'Окончание должно быть строго позже начала периода.'
        if (self.valid_to is None) != (self.ended_revision_id is None):
            errors['ended_revision'] = 'Ревизия окончания и окончание периода указываются вместе.'

        if self.status == self.Status.CONFIRMED:
            if self.anchor.status != AccommodationAnchor.Status.ACTIVE:
                errors['anchor'] = 'Подтверждённое закрепление требует действующего якоря.'
            if self.started_revision.status != SettlementRevision.Status.CONFIRMED:
                errors['started_revision'] = 'Начало закрепления требует подтверждённой ревизии.'
            if self.ended_revision_id and self.ended_revision.status != SettlementRevision.Status.CONFIRMED:
                errors['ended_revision'] = 'Окончание закрепления требует подтверждённой ревизии.'

            overlaps = self._overlapping_confirmed_assignments()
            if self.anchor_id and overlaps.filter(anchor_id=self.anchor_id).exists():
                errors['anchor'] = 'У якоря уже есть подтверждённая койка в пересекающемся периоде.'
            if self.physical_bed_id and overlaps.filter(physical_bed_id=self.physical_bed_id).exists():
                errors['physical_bed'] = 'Койка уже связана с другим якорем в пересекающемся периоде.'

        if self.status == self.Status.CANCELLED:
            if not self.cancelled_revision_id:
                errors['cancelled_revision'] = 'Для отмены укажите подтверждающую ревизию.'
            elif self.cancelled_revision.status != SettlementRevision.Status.CONFIRMED:
                errors['cancelled_revision'] = 'Отмена требует подтверждённой ревизии.'
        elif self.cancelled_revision_id:
            errors['cancelled_revision'] = 'Ревизия отмены допустима только для отменённой записи.'

        if errors:
            raise ValidationError(errors)

    def _validate_existing_state(self):
        if not self.pk:
            return
        original = type(self).objects.filter(pk=self.pk).values(
            'stable_id',
            'anchor_id',
            'physical_bed_id',
            'valid_from',
            'valid_to',
            'status',
            'started_revision_id',
            'ended_revision_id',
            'cancelled_revision_id',
        ).first()
        if not original:
            return

        immutable_errors = {}
        for field_name in (
            'stable_id',
            'anchor_id',
            'physical_bed_id',
            'valid_from',
            'started_revision_id',
        ):
            if original[field_name] != getattr(self, field_name):
                immutable_errors[field_name.removesuffix('_id')] = (
                    'После создания это поле изменять нельзя.'
                )
        if original['valid_to'] is not None and original['valid_to'] != self.valid_to:
            immutable_errors['valid_to'] = 'Закрытый период нельзя повторно изменять.'
        if (
            original['ended_revision_id'] is not None
            and original['ended_revision_id'] != self.ended_revision_id
        ):
            immutable_errors['ended_revision'] = 'Ревизию закрытого периода менять нельзя.'
        if original['status'] == self.Status.CANCELLED and self.status != self.Status.CANCELLED:
            immutable_errors['status'] = 'Отменённую запись нельзя восстановить.'
        if original['status'] == self.Status.CONFIRMED and self.status == self.Status.DRAFT:
            immutable_errors['status'] = 'Подтверждённую запись нельзя вернуть в черновик.'
        if (
            original['cancelled_revision_id'] is not None
            and original['cancelled_revision_id'] != self.cancelled_revision_id
        ):
            immutable_errors['cancelled_revision'] = 'Ревизию отмены менять нельзя.'
        if immutable_errors:
            raise ValidationError(immutable_errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            AccommodationAnchor.objects.select_for_update().get(pk=self.anchor_id)
            PhysicalBed.objects.select_for_update().get(pk=self.physical_bed_id)
            self._validate_existing_state()
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        end = self.valid_to.isoformat() if self.valid_to else 'открытый период'
        return f'{self.anchor.code} → {self.physical_bed.stable_id}: {self.valid_from.isoformat()} — {end}'


@receiver(pre_delete, sender=AccommodationAnchorBedAssignment)
def protect_historical_anchor_bed_assignment(sender, instance, using, origin, **kwargs):
    persisted_status = instance.status
    if origin is instance:
        persisted_status = (
            sender._base_manager.using(using)
            .filter(pk=instance.pk)
            .values_list('status', flat=True)
            .first()
        )

    if persisted_status in {
        AccommodationAnchorBedAssignment.Status.CONFIRMED,
        AccommodationAnchorBedAssignment.Status.CANCELLED,
    }:
        raise ProtectedError(
            'Подтверждённые и отменённые закрепления жилищных якорей удалять нельзя.',
            [instance],
        )


class PhysicalRoom(models.Model):
    class RoomType(models.TextChoices):
        STANDARD = 'standard', 'Стандартная'
        ITR = 'itr', 'ИТР'

    class TransferStatus(models.TextChoices):
        TRANSFERRED = 'transferred', 'Передана'
        NOT_TRANSFERRED = 'not_transferred', 'Не передана'

    class CorridorSide(models.TextChoices):
        LEFT = 'left', 'Левая сторона'
        RIGHT = 'right', 'Правая сторона'

    dormitory = models.ForeignKey(
        'references.Dormitory',
        verbose_name='Общежитие',
        on_delete=models.PROTECT,
        related_name='physical_rooms',
    )
    floor = models.PositiveSmallIntegerField(
        'Этаж',
        validators=[MinValueValidator(1), MaxValueValidator(2)],
    )
    number = models.PositiveSmallIntegerField('Номер комнаты')
    room_type = models.CharField(
        'Тип комнаты',
        max_length=16,
        choices=RoomType.choices,
        default=RoomType.STANDARD,
    )
    transfer_status = models.CharField(
        'Статус передачи',
        max_length=24,
        choices=TransferStatus.choices,
        default=TransferStatus.NOT_TRANSFERRED,
        db_index=True,
    )
    capacity = models.PositiveSmallIntegerField(
        'Вместимость',
        validators=[MinValueValidator(1), MaxValueValidator(6)],
    )
    corridor_side = models.CharField(
        'Сторона коридора',
        max_length=8,
        choices=CorridorSide.choices,
    )
    side_position = models.PositiveSmallIntegerField(
        'Позиция на стороне этажа',
        validators=[MinValueValidator(1)],
        help_text=(
            'Порядок комнаты только внутри стороны выбранного этажа. '
            'Не задаёт соответствие номеров между этажами.'
        ),
    )

    class Meta:
        verbose_name = 'Физическая комната'
        verbose_name_plural = 'Физические комнаты'
        ordering = [
            'dormitory__number',
            'floor',
            'corridor_side',
            'side_position',
            'number',
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['dormitory', 'floor', 'number'],
                name='unique_physical_room_number',
            ),
            models.UniqueConstraint(
                fields=['dormitory', 'floor', 'corridor_side', 'side_position'],
                name='unique_physical_room_side_position',
            ),
            models.CheckConstraint(
                condition=models.Q(floor__gte=1, floor__lte=2),
                name='physical_room_floor_1_2',
            ),
            models.CheckConstraint(
                condition=models.Q(capacity__gte=1, capacity__lte=6),
                name='physical_room_capacity_1_6',
            ),
        ]

    @property
    def is_transferred(self):
        return self.transfer_status == self.TransferStatus.TRANSFERRED

    def __str__(self):
        return f'КИС-{self.dormitory.number}, этаж {self.floor}, комната {self.number}'


class PhysicalBed(models.Model):
    class Block(models.TextChoices):
        A = 'A', 'А'
        B = 'B', 'Б'
        ITR = 'ITR', 'ИТР'

    room = models.ForeignKey(
        PhysicalRoom,
        verbose_name='Комната',
        on_delete=models.CASCADE,
        related_name='beds',
    )
    stable_id = models.CharField(
        'Стабильный идентификатор',
        max_length=64,
        unique=True,
        editable=False,
    )
    block = models.CharField('Блок', max_length=3, choices=Block.choices)
    position = models.PositiveSmallIntegerField(
        'Номер койко-места в блоке',
        validators=[MinValueValidator(1), MaxValueValidator(3)],
    )

    class Meta:
        verbose_name = 'Физическое койко-место'
        verbose_name_plural = 'Физические койко-места'
        ordering = ['room', 'block', 'position']
        constraints = [
            models.UniqueConstraint(
                fields=['room', 'block', 'position'],
                name='unique_physical_bed_in_room',
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1, position__lte=3),
                name='physical_bed_position_1_3',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(block__in=['A', 'B'])
                    | models.Q(block='ITR', position__lte=2)
                ),
                name='physical_bed_block_position_valid',
            ),
        ]

    @property
    def is_available(self):
        return self.room.is_transferred

    def save(self, *args, **kwargs):
        if self.pk:
            original_stable_id = (
                type(self).objects
                .filter(pk=self.pk)
                .values_list('stable_id', flat=True)
                .first()
            )
            if original_stable_id and original_stable_id != self.stable_id:
                raise ValidationError({
                    'stable_id': 'Стабильный идентификатор койко-места менять нельзя.',
                })
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.room}, {self.get_block_display()}-{self.position}'


class EmployeeBedOccupancy(models.Model):
    class AssignmentType(models.TextChoices):
        PERMANENT = 'permanent', 'Постоянное'
        TEMPORARY = 'temporary', 'Временное'
        PROPOSED = 'proposed', 'Предложенное'

    employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Сотрудник',
        on_delete=models.PROTECT,
        related_name='bed_occupancies',
    )
    physical_bed = models.ForeignKey(
        PhysicalBed,
        verbose_name='Физическое койко-место',
        on_delete=models.PROTECT,
        related_name='occupancies',
    )
    assignment_type = models.CharField(
        'Тип закрепления',
        max_length=16,
        choices=AssignmentType.choices,
    )
    settled_at = models.DateTimeField('Заселён', default=timezone.now)
    starts_at = models.DateTimeField(
        'Начало размещения',
        default=timezone.now,
    )
    ends_at = models.DateTimeField(
        'Плановое окончание размещения',
        null=True,
        blank=True,
    )
    terminated_at = models.DateTimeField(
        'Досрочное прекращение размещения',
        null=True,
        blank=True,
    )
    settled_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто заселил',
        on_delete=models.PROTECT,
        related_name='created_bed_occupancies',
    )
    ended_at = models.DateTimeField(
        'Проживание завершено',
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = 'Размещение сотрудника на койко-месте'
        verbose_name_plural = 'Размещения сотрудников на койко-местах'
        ordering = ['-settled_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['physical_bed'],
                condition=models.Q(ended_at__isnull=True),
                name='unique_active_employee_bed_occupancy',
            ),
            models.UniqueConstraint(
                fields=['employee'],
                condition=models.Q(ended_at__isnull=True),
                name='unique_active_employee_occupancy',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(ended_at__isnull=True)
                    | models.Q(ended_at__gt=models.F('settled_at'))
                ),
                name='employee_bed_occupancy_period_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(ends_at__isnull=True)
                    | models.Q(ends_at__gt=models.F('starts_at'))
                ),
                name='occupancy_ends_after_start',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(terminated_at__isnull=True)
                    | models.Q(terminated_at__gt=models.F('starts_at'))
                ),
                name='occupancy_term_after_start',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(ends_at__isnull=True)
                    | models.Q(terminated_at__isnull=True)
                    | models.Q(terminated_at__lt=models.F('ends_at'))
                ),
                name='occupancy_term_before_end',
            ),
        ]

    @property
    def is_active(self):
        return self.ended_at is None

    def __str__(self):
        return f'{self.employee} — {self.physical_bed}'
