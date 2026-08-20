import uuid
from datetime import datetime, time, timedelta

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
        original = type(self)._base_manager.filter(pk=self.pk).values(*fields).first()
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


class AccommodationAnchorBedAssignmentQuerySet(models.QuerySet):
    MASS_WRITE_FORBIDDEN_CODE = 'anchor_bed_mass_write_forbidden'
    MASS_WRITE_FORBIDDEN_MESSAGE = (
        'Массовые изменения закреплений жилищных якорей запрещены. '
        'Используйте instance save().'
    )

    def _raise_mass_write_forbidden(self):
        raise ValidationError(
            self.MASS_WRITE_FORBIDDEN_MESSAGE,
            code=self.MASS_WRITE_FORBIDDEN_CODE,
        )

    def update(self, **kwargs):
        self._raise_mass_write_forbidden()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        self._raise_mass_write_forbidden()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_mass_write_forbidden()

class AccommodationAnchorBedAssignment(StableIdentifierModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        CONFIRMED = 'confirmed', 'Подтверждено'
        CANCELLED = 'cancelled', 'Отменено'

    objects = AccommodationAnchorBedAssignmentQuerySet.as_manager()

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


class M4CalendarBindingQuerySet(models.QuerySet):
    MASS_WRITE_FORBIDDEN_CODE = 'm4_calendar_binding_mass_write_forbidden'
    MASS_WRITE_FORBIDDEN_MESSAGE = (
        'Массовые изменения календарных слотов и постоянных закреплений запрещены. '
        'Используйте доменные команды M4.'
    )

    def _raise_mass_write_forbidden(self):
        raise ValidationError(
            self.MASS_WRITE_FORBIDDEN_MESSAGE,
            code=self.MASS_WRITE_FORBIDDEN_CODE,
        )

    def update(self, **kwargs):
        self._raise_mass_write_forbidden()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        self._raise_mass_write_forbidden()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_mass_write_forbidden()


def _inclusive_date_bounds_as_datetimes(valid_from, valid_to):
    current_timezone = timezone.get_current_timezone()
    starts_at = timezone.make_aware(datetime.combine(valid_from, time.min), current_timezone)
    ends_at = timezone.make_aware(
        datetime.combine(valid_to + timedelta(days=1), time.min),
        current_timezone,
    )
    return starts_at, ends_at


def _confirmed_bed_assignment_for_slot(slot):
    starts_at, ends_at = _inclusive_date_bounds_as_datetimes(slot.valid_from, slot.valid_to)
    assignments = list(
        AccommodationAnchorBedAssignment.objects.filter(
            anchor_id=slot.anchor_id,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            valid_from__lte=starts_at,
        )
        .filter(models.Q(valid_to__isnull=True) | models.Q(valid_to__gte=ends_at))
        .order_by('pk')[:2]
    )
    if len(assignments) != 1:
        raise ValidationError({
            'anchor': (
                'Подтверждённый календарный слот требует ровно одно доказанное '
                'закрепление якоря за койкой на весь WatchPeriod.'
            ),
        })
    return assignments[0]


class AccommodationAnchorCalendarSlot(StableIdentifierModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        CONFIRMED = 'confirmed', 'Подтверждён'
        CLOSED = 'closed', 'Закрыт'

    objects = M4CalendarBindingQuerySet.as_manager()

    anchor = models.ForeignKey(
        AccommodationAnchor,
        verbose_name='Жилищный якорь',
        on_delete=models.PROTECT,
        related_name='calendar_slots',
    )
    watch_composition = models.ForeignKey(
        'users.WatchComposition',
        verbose_name='Утверждённый состав вахты',
        on_delete=models.PROTECT,
        related_name='accommodation_calendar_slots',
    )
    watch_period = models.ForeignKey(
        'shifts.WatchPeriod',
        verbose_name='Конкретный период вахты',
        on_delete=models.PROTECT,
        related_name='accommodation_calendar_slots',
    )
    valid_from = models.DateField('Действует с')
    valid_to = models.DateField('Действует по')
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    source_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия-основание',
        on_delete=models.PROTECT,
        related_name='accommodation_calendar_slots',
    )
    approved_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кем подтверждён',
        on_delete=models.PROTECT,
        related_name='approved_accommodation_calendar_slots',
        null=True,
        blank=True,
    )
    approved_at = models.DateTimeField('Когда подтверждён', null=True, blank=True)
    closed_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия закрытия',
        on_delete=models.PROTECT,
        related_name='closed_accommodation_calendar_slots',
        null=True,
        blank=True,
    )
    closed_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кем закрыт',
        on_delete=models.PROTECT,
        related_name='closed_accommodation_calendar_slots',
        null=True,
        blank=True,
    )
    closed_at = models.DateTimeField('Когда закрыт', null=True, blank=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Изменён', auto_now=True)

    class Meta:
        verbose_name = 'Календарный слот жилищного якоря'
        verbose_name_plural = 'Календарные слоты жилищных якорей'
        ordering = ['valid_from', 'anchor_id', 'pk']
        indexes = [
            models.Index(fields=['anchor', 'status', 'valid_from'], name='anchor_slot_anchor_period_idx'),
            models.Index(
                fields=['watch_composition', 'status', 'valid_from'],
                name='anchor_slot_watch_period_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['anchor', 'watch_period'],
                name='unique_anchor_slot_per_watch_period',
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__gte=models.F('valid_from')),
                name='anchor_slot_period_non_empty',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status='draft', approved_by__isnull=True, approved_at__isnull=True,
                        closed_revision__isnull=True, closed_by__isnull=True, closed_at__isnull=True,
                    )
                    | models.Q(
                        status='confirmed', approved_by__isnull=False, approved_at__isnull=False,
                        closed_revision__isnull=True, closed_by__isnull=True, closed_at__isnull=True,
                    )
                    | models.Q(
                        status='closed', approved_by__isnull=False, approved_at__isnull=False,
                        closed_revision__isnull=False, closed_by__isnull=False, closed_at__isnull=False,
                    )
                ),
                name='anchor_slot_lifecycle_metadata',
            ),
        ]

    @property
    def calendar_relation_is_stale(self):
        period = self.watch_period
        return (
            period.watch_composition_id != self.watch_composition_id
            or period.starts_on != self.valid_from
            or period.ends_on != self.valid_to
        )

    def _overlapping_slots(self):
        queryset = type(self).objects.filter(
            status__in=[self.Status.CONFIRMED, self.Status.CLOSED],
            valid_from__lte=self.valid_to,
            valid_to__gte=self.valid_from,
        )
        if self.pk:
            queryset = queryset.exclude(pk=self.pk)
        return queryset.order_by('pk')

    def clean(self):
        super().clean()
        errors = {}
        if self._state.adding and self.status != self.Status.DRAFT:
            errors['status'] = 'CalendarSlot создаётся только как DRAFT и подтверждается доменной командой.'
        if self.watch_period_id:
            period = self.watch_period
            if period.watch_composition_id != self.watch_composition_id:
                errors['watch_composition'] = 'WatchPeriod не принадлежит указанному WatchComposition.'
            if period.starts_on != self.valid_from or period.ends_on != self.valid_to:
                errors['valid_from'] = (
                    'Границы CalendarSlot должны точно совпадать с каноническими границами WatchPeriod.'
                )
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            errors['valid_to'] = 'Окончание периода не может быть раньше начала.'
        if self.status in {self.Status.CONFIRMED, self.Status.CLOSED}:
            if self.source_revision.status != SettlementRevision.Status.CONFIRMED:
                errors['source_revision'] = 'Подтверждённый слот требует подтверждённой ревизии.'
            if self.anchor.status != AccommodationAnchor.Status.ACTIVE:
                errors['anchor'] = 'Подтверждённый слот требует действующего якоря.'
            if not self.approved_by_id or not self.approved_at:
                errors['approved_by'] = 'Подтверждённый слот требует автора и времени подтверждения.'
            try:
                own_assignment = _confirmed_bed_assignment_for_slot(self)
            except ValidationError as error:
                errors.update(error.message_dict)
            else:
                for other in self._overlapping_slots():
                    try:
                        other_assignment = _confirmed_bed_assignment_for_slot(other)
                    except ValidationError:
                        errors['anchor'] = 'Пересекающийся слот имеет недоказанную связь с физической койкой.'
                        break
                    if other_assignment.physical_bed_id == own_assignment.physical_bed_id:
                        errors['valid_from'] = (
                            'Пересекающиеся календарные интервалы одной физической койки запрещены.'
                        )
                        break
        if self.status == self.Status.CLOSED:
            if not self.closed_revision_id or not self.closed_by_id or not self.closed_at:
                errors['closed_revision'] = 'Закрытый слот требует полной истории закрытия.'
            elif self.closed_revision.status != SettlementRevision.Status.CONFIRMED:
                errors['closed_revision'] = 'Закрытие требует подтверждённой ревизии.'
        if errors:
            raise ValidationError(errors)

    def _validate_existing_state(self):
        if not self.pk:
            return
        original = type(self)._base_manager.filter(pk=self.pk).values(
            'stable_id', 'anchor_id', 'watch_composition_id', 'watch_period_id',
            'valid_from', 'valid_to', 'source_revision_id', 'status',
            'approved_by_id', 'approved_at', 'closed_revision_id', 'closed_by_id', 'closed_at',
        ).first()
        if original is None:
            return
        errors = {}
        for field_name in (
            'stable_id', 'anchor_id', 'watch_composition_id', 'watch_period_id',
            'valid_from', 'valid_to', 'source_revision_id',
        ):
            if original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = 'Смысловые поля CalendarSlot после создания неизменяемы.'
        allowed = {
            self.Status.DRAFT: {self.Status.DRAFT, self.Status.CONFIRMED},
            self.Status.CONFIRMED: {self.Status.CONFIRMED, self.Status.CLOSED},
            self.Status.CLOSED: {self.Status.CLOSED},
        }
        if self.status not in allowed[original['status']]:
            errors['status'] = 'Недопустимый переход статуса CalendarSlot.'
        for field_name in ('approved_by_id', 'approved_at', 'closed_revision_id', 'closed_by_id', 'closed_at'):
            if original[field_name] is not None and original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = 'Исторические реквизиты CalendarSlot неизменяемы.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
            self._validate_existing_state()
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.anchor.code}: {self.watch_period.name} ({self.valid_from} — {self.valid_to})'


class EmployeeAccommodationBinding(StableIdentifierModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        CONFIRMED = 'confirmed', 'Подтверждено'
        CLOSED = 'closed', 'Закрыто'

    objects = M4CalendarBindingQuerySet.as_manager()

    resident = models.ForeignKey(
        'SettlementResident',
        verbose_name='Жилец',
        on_delete=models.PROTECT,
        related_name='accommodation_bindings',
    )
    anchor_calendar_slot = models.ForeignKey(
        AccommodationAnchorCalendarSlot,
        verbose_name='Календарный слот жилищного якоря',
        on_delete=models.PROTECT,
        related_name='employee_bindings',
    )
    valid_from = models.DateField('Действует с')
    valid_to = models.DateField('Действует по')
    status = models.CharField(
        'Статус', max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True,
    )
    basis_type = models.CharField('Тип основания', max_length=64)
    basis_id = models.CharField('Идентификатор основания', max_length=128)
    basis_snapshot = models.JSONField('Неизменяемый снимок основания')
    source_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия-основание',
        on_delete=models.PROTECT,
        related_name='employee_accommodation_bindings',
    )
    approved_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кем подтверждено',
        on_delete=models.PROTECT,
        related_name='approved_employee_accommodation_bindings',
        null=True,
        blank=True,
    )
    approved_at = models.DateTimeField('Когда подтверждено', null=True, blank=True)
    closed_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия закрытия',
        on_delete=models.PROTECT,
        related_name='closed_employee_accommodation_bindings',
        null=True,
        blank=True,
    )
    closed_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кем закрыто',
        on_delete=models.PROTECT,
        related_name='closed_employee_accommodation_bindings',
        null=True,
        blank=True,
    )
    closed_at = models.DateTimeField('Когда закрыто', null=True, blank=True)
    supersedes = models.ForeignKey(
        'self',
        verbose_name='Заменяет закрепление',
        on_delete=models.PROTECT,
        related_name='replacements',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Изменено', auto_now=True)

    class Meta:
        verbose_name = 'Постоянное жилищное закрепление жильца'
        verbose_name_plural = 'Постоянные жилищные закрепления жильцов'
        ordering = ['resident_id', 'valid_from', 'pk']
        indexes = [
            models.Index(fields=['resident', 'status', 'valid_from'], name='resident_binding_period_idx'),
            models.Index(
                fields=['anchor_calendar_slot', 'status', 'valid_from'],
                name='slot_binding_period_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['resident', 'anchor_calendar_slot', 'valid_from'],
                name='unique_resident_slot_binding_start',
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__gte=models.F('valid_from')),
                name='employee_binding_period_non_empty',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status='draft', approved_by__isnull=True, approved_at__isnull=True,
                        closed_revision__isnull=True, closed_by__isnull=True, closed_at__isnull=True,
                    )
                    | models.Q(
                        status='confirmed', approved_by__isnull=False, approved_at__isnull=False,
                        closed_revision__isnull=True, closed_by__isnull=True, closed_at__isnull=True,
                    )
                    | models.Q(
                        status='closed', approved_by__isnull=False, approved_at__isnull=False,
                        closed_revision__isnull=False, closed_by__isnull=False, closed_at__isnull=False,
                    )
                ),
                name='employee_binding_lifecycle_metadata',
            ),
            models.CheckConstraint(
                condition=models.Q(supersedes__isnull=True) | ~models.Q(pk=models.F('supersedes_id')),
                name='employee_binding_not_self_supersede',
            ),
        ]

    def _overlapping_bindings(self):
        queryset = type(self).objects.filter(
            status__in=[self.Status.CONFIRMED, self.Status.CLOSED],
            valid_from__lte=self.valid_to,
            valid_to__gte=self.valid_from,
        )
        if self.pk:
            queryset = queryset.exclude(pk=self.pk)
        return queryset.order_by('pk')

    def clean(self):
        super().clean()
        errors = {}
        if self._state.adding and self.status != self.Status.DRAFT:
            errors['status'] = 'Binding создаётся только как DRAFT и подтверждается доменной командой.'
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            errors['valid_to'] = 'Окончание binding не может быть раньше начала.'
        if not self.basis_type:
            errors['basis_type'] = 'Тип основания обязателен.'
        if not self.basis_id:
            errors['basis_id'] = 'Идентификатор основания обязателен.'
        if not isinstance(self.basis_snapshot, dict) or not self.basis_snapshot:
            errors['basis_snapshot'] = 'Неизменяемый снимок основания должен быть непустым объектом.'
        if self.anchor_calendar_slot_id:
            slot = self.anchor_calendar_slot
            if slot.calendar_relation_is_stale:
                errors['anchor_calendar_slot'] = 'CalendarSlot устарел относительно WatchPeriod.'
            if self.valid_from < slot.valid_from or self.valid_to > slot.valid_to:
                errors['valid_from'] = 'Период binding должен целиком находиться внутри CalendarSlot.'
            resident = self.resident
            if (
                self._state.adding or self.status == self.Status.CONFIRMED
            ) and resident.status != SettlementResident.Status.ACTIVE:
                errors['resident'] = 'Архивный resident нельзя использовать для binding.'
            if resident.resident_type == SettlementResident.ResidentType.EMPLOYEE:
                if not resident.employee_id:
                    errors['resident'] = 'Внутренний resident не связан с Employee.'
                else:
                    employee = resident.employee
                    if not employee.is_active or employee.status != employee.Status.ACTIVE:
                        errors['resident'] = 'Внутренний Employee неактивен или уволен.'
                    elif employee.watch_composition_id != slot.watch_composition_id:
                        errors['resident'] = (
                            'Внутренний Employee не принадлежит WatchComposition календарного слота.'
                        )
            elif (
                resident.resident_type not in {
                    SettlementResident.ResidentType.CONTRACTOR,
                    SettlementResident.ResidentType.BUSINESS_TRIP,
                    SettlementResident.ResidentType.EXTERNAL_OTHER,
                }
                or resident.employee_id is not None
            ):
                errors['resident'] = 'Внешний resident имеет недопустимый источник.'
        if self.status in {self.Status.CONFIRMED, self.Status.CLOSED}:
            if self.source_revision.status != SettlementRevision.Status.CONFIRMED:
                errors['source_revision'] = 'Подтверждённый binding требует подтверждённой ревизии.'
            if self.anchor_calendar_slot.status not in {
                AccommodationAnchorCalendarSlot.Status.CONFIRMED,
                AccommodationAnchorCalendarSlot.Status.CLOSED,
            }:
                errors['anchor_calendar_slot'] = 'Binding требует подтверждённого CalendarSlot.'
            if not self.approved_by_id or not self.approved_at:
                errors['approved_by'] = 'Подтверждённый binding требует автора и времени подтверждения.'
            overlaps = self._overlapping_bindings()
            if overlaps.filter(resident_id=self.resident_id).exists():
                errors['resident'] = 'Жилец уже имеет подтверждённый binding в пересекающемся периоде.'
            if overlaps.filter(anchor_calendar_slot_id=self.anchor_calendar_slot_id).exists():
                errors['anchor_calendar_slot'] = 'CalendarSlot уже занят в пересекающемся периоде.'
            try:
                own_assignment = _confirmed_bed_assignment_for_slot(self.anchor_calendar_slot)
            except ValidationError as error:
                errors.update(error.message_dict)
            else:
                for other in overlaps.select_related('anchor_calendar_slot'):
                    try:
                        other_assignment = _confirmed_bed_assignment_for_slot(other.anchor_calendar_slot)
                    except ValidationError:
                        errors['anchor_calendar_slot'] = 'Пересекающийся binding имеет недоказанное физическое место.'
                        break
                    if other_assignment.physical_bed_id == own_assignment.physical_bed_id:
                        errors['anchor_calendar_slot'] = (
                            'Одна физическая койка не может иметь противоречащие подтверждённые binding.'
                        )
                        break
        if self.supersedes_id:
            if self.supersedes_id == self.pk:
                errors['supersedes'] = 'Binding не может заменять сам себя.'
            elif self.supersedes.resident_id != self.resident_id:
                errors['supersedes'] = 'Постоянная коррекция должна относиться к тому же жильцу.'
        if self.status == self.Status.CLOSED:
            if not self.closed_revision_id or not self.closed_by_id or not self.closed_at:
                errors['closed_revision'] = 'Закрытый binding требует полной истории закрытия.'
            elif self.closed_revision.status != SettlementRevision.Status.CONFIRMED:
                errors['closed_revision'] = 'Закрытие требует подтверждённой ревизии.'
        if errors:
            raise ValidationError(errors)

    def _validate_existing_state(self):
        if not self.pk:
            return
        original = type(self)._base_manager.filter(pk=self.pk).values(
            'stable_id', 'resident_id', 'anchor_calendar_slot_id', 'valid_from', 'valid_to',
            'basis_type', 'basis_id', 'basis_snapshot', 'source_revision_id', 'supersedes_id',
            'status', 'approved_by_id', 'approved_at', 'closed_revision_id', 'closed_by_id', 'closed_at',
        ).first()
        if original is None:
            return
        errors = {}
        for field_name in (
            'stable_id', 'resident_id', 'anchor_calendar_slot_id', 'valid_from',
            'basis_type', 'basis_id', 'basis_snapshot', 'source_revision_id', 'supersedes_id',
        ):
            if original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = 'Смысловые поля binding после создания неизменяемы.'
        allowed = {
            self.Status.DRAFT: {self.Status.DRAFT, self.Status.CONFIRMED},
            self.Status.CONFIRMED: {self.Status.CONFIRMED, self.Status.CLOSED},
            self.Status.CLOSED: {self.Status.CLOSED},
        }
        if self.status not in allowed[original['status']]:
            errors['status'] = 'Недопустимый переход статуса binding.'
        if original['valid_to'] != self.valid_to:
            valid_close = (
                original['status'] == self.Status.CONFIRMED
                and self.status == self.Status.CLOSED
                and self.valid_from <= self.valid_to <= original['valid_to']
            )
            if not valid_close:
                errors['valid_to'] = (
                    'Границу binding можно только однократно сократить явной командой закрытия.'
                )
        for field_name in ('approved_by_id', 'approved_at', 'closed_revision_id', 'closed_by_id', 'closed_at'):
            if original[field_name] is not None and original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = 'Исторические реквизиты binding неизменяемы.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
            self._validate_existing_state()
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.resident} → {self.anchor_calendar_slot}: {self.valid_from} — {self.valid_to}'


@receiver(pre_delete, sender=AccommodationAnchorCalendarSlot)
@receiver(pre_delete, sender=EmployeeAccommodationBinding)
def protect_historical_m4_rows(sender, instance, using, origin, **kwargs):
    persisted_status = instance.status
    if origin is instance:
        persisted_status = (
            sender._base_manager.using(using)
            .filter(pk=instance.pk)
            .values_list('status', flat=True)
            .first()
        )
    if persisted_status in {sender.Status.CONFIRMED, sender.Status.CLOSED}:
        raise ProtectedError(
            'Подтверждённые и закрытые строки M4 удалять нельзя.',
            [instance],
        )


class M5CohortQuerySet(models.QuerySet):
    MASS_WRITE_FORBIDDEN_CODE = 'settlement_cohort_mass_write_forbidden'
    MASS_WRITE_FORBIDDEN_MESSAGE = (
        'Массовые изменения cohort и membership запрещены. '
        'Используйте доменные команды M5.'
    )

    def _raise_mass_write_forbidden(self):
        raise ValidationError(
            self.MASS_WRITE_FORBIDDEN_MESSAGE,
            code=self.MASS_WRITE_FORBIDDEN_CODE,
        )

    def update(self, **kwargs):
        self._raise_mass_write_forbidden()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        self._raise_mass_write_forbidden()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_mass_write_forbidden()


class SettlementCohort(StableIdentifierModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        APPROVED = 'approved', 'Утверждён'
        SUPERSEDED = 'superseded', 'Заменён новой версией'

    objects = M5CohortQuerySet.as_manager()

    watch_composition = models.ForeignKey(
        'users.WatchComposition',
        verbose_name='Утверждённый состав вахты',
        on_delete=models.PROTECT,
        related_name='settlement_cohorts',
    )
    watch_period = models.ForeignKey(
        'shifts.WatchPeriod',
        verbose_name='Конкретный период вахты',
        on_delete=models.PROTECT,
        related_name='settlement_cohorts',
    )
    version = models.PositiveIntegerField('Версия состава')
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    source_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия-основание',
        on_delete=models.PROTECT,
        related_name='settlement_cohorts',
        null=True,
        blank=True,
    )
    routing_batch = models.OneToOneField(
        'rotations.ArrivalRosterRoutingBatch',
        verbose_name='Точная передача реестра заезда',
        on_delete=models.PROTECT,
        related_name='settlement_cohort',
        null=True,
        blank=True,
    )
    source_type = models.CharField('Тип источника', max_length=64)
    source_id = models.CharField('Идентификатор источника', max_length=128)
    source_snapshot = models.JSONField('Неизменяемый снимок источника')
    input_fingerprint = models.CharField(
        'Отпечаток входного состава',
        max_length=64,
        validators=[
            RegexValidator(
                regex=r'^[0-9a-f]{64}$',
                message='Отпечаток состава должен быть SHA-256 в нижнем регистре.',
            ),
        ],
    )
    created_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кем зафиксирована версия',
        on_delete=models.PROTECT,
        related_name='created_settlement_cohorts',
    )
    approved_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Кем утверждена версия',
        on_delete=models.PROTECT,
        related_name='approved_settlement_cohorts',
        null=True,
        blank=True,
    )
    approved_at = models.DateTimeField('Когда утверждена', null=True, blank=True)
    supersedes = models.ForeignKey(
        'self',
        verbose_name='Заменяет версию состава',
        on_delete=models.PROTECT,
        related_name='replacements',
        null=True,
        blank=True,
    )
    superseded_at = models.DateTimeField('Когда заменена', null=True, blank=True)
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Изменена', auto_now=True)

    class Meta:
        verbose_name = 'Жилищная версия состава заезда'
        verbose_name_plural = 'Жилищные версии составов заезда'
        ordering = ['watch_period_id', 'version', 'pk']
        indexes = [
            models.Index(
                fields=['watch_period', 'status', 'version'],
                name='cohort_period_status_ver_idx',
            ),
            models.Index(
                fields=['watch_composition', 'status'],
                name='cohort_composition_status_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['watch_period', 'version'],
                name='unique_cohort_watch_period_version',
            ),
            models.UniqueConstraint(
                fields=['watch_period'],
                condition=models.Q(status='approved'),
                name='unique_approved_cohort_per_watch',
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name='cohort_version_gte_1',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status='draft',
                        approved_by__isnull=True,
                        approved_at__isnull=True,
                        superseded_at__isnull=True,
                    )
                    | models.Q(
                        status='approved',
                        approved_by__isnull=False,
                        approved_at__isnull=False,
                        superseded_at__isnull=True,
                    )
                    | models.Q(
                        status='superseded',
                        approved_by__isnull=False,
                        approved_at__isnull=False,
                        superseded_at__isnull=False,
                    )
                ),
                name='cohort_lifecycle_metadata',
            ),
            models.CheckConstraint(
                condition=models.Q(supersedes__isnull=True) | ~models.Q(pk=models.F('supersedes_id')),
                name='cohort_not_self_supersede',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(source_revision__isnull=False, routing_batch__isnull=True)
                    | models.Q(source_revision__isnull=True, routing_batch__isnull=False)
                ),
                name='cohort_source_family_ck',
            ),
        ]

    @property
    def calendar_relation_is_stale(self):
        return self.watch_period.watch_composition_id != self.watch_composition_id

    def clean(self):
        super().clean()
        errors = {}
        if self._state.adding and self.status != self.Status.DRAFT:
            errors['status'] = 'Cohort создаётся только как DRAFT и утверждается доменной командой.'
        if self.watch_period_id and self.watch_composition_id:
            if self.watch_period.watch_composition_id != self.watch_composition_id:
                errors['watch_composition'] = 'WatchPeriod не принадлежит указанному WatchComposition.'
        if not self.source_type:
            errors['source_type'] = 'Тип источника cohort обязателен.'
        if not self.source_id:
            errors['source_id'] = 'Идентификатор источника cohort обязателен.'
        if not isinstance(self.source_snapshot, dict) or not self.source_snapshot:
            errors['source_snapshot'] = 'Снимок источника cohort должен быть непустым объектом.'
        if self.status in {self.Status.APPROVED, self.Status.SUPERSEDED}:
            if (
                self.source_revision_id
                and self.source_revision.status != SettlementRevision.Status.CONFIRMED
            ):
                errors['source_revision'] = 'Утверждённый cohort требует подтверждённой ревизии.'
            if not self.approved_by_id or not self.approved_at:
                errors['approved_by'] = 'Утверждённый cohort требует автора и времени утверждения.'
        if self.supersedes_id:
            previous = self.supersedes
            if previous.watch_period_id != self.watch_period_id:
                errors['supersedes'] = 'Новая версия cohort должна относиться к тому же WatchPeriod.'
            elif previous.watch_composition_id != self.watch_composition_id:
                errors['supersedes'] = 'Новая версия cohort должна сохранять WatchComposition.'
            elif self.version != previous.version + 1:
                errors['version'] = 'Версия cohort должна следовать непосредственно за заменяемой.'
        elif self.version != 1:
            errors['version'] = 'Первая версия cohort должна иметь номер 1.'
        if errors:
            raise ValidationError(errors)

    def _validate_existing_state(self):
        if not self.pk:
            return
        original = type(self)._base_manager.filter(pk=self.pk).values(
            'stable_id', 'watch_composition_id', 'watch_period_id', 'version',
            'source_revision_id', 'routing_batch_id', 'source_type', 'source_id', 'source_snapshot',
            'input_fingerprint', 'created_by_id', 'supersedes_id', 'status',
            'approved_by_id', 'approved_at', 'superseded_at',
        ).first()
        if original is None:
            return
        errors = {}
        for field_name in (
            'stable_id', 'watch_composition_id', 'watch_period_id', 'version',
            'source_revision_id', 'routing_batch_id', 'source_type', 'source_id', 'source_snapshot',
            'input_fingerprint', 'created_by_id', 'supersedes_id',
        ):
            if original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = 'Смысловые поля cohort после создания неизменяемы.'
        allowed = {
            self.Status.DRAFT: {self.Status.DRAFT, self.Status.APPROVED},
            self.Status.APPROVED: {self.Status.APPROVED, self.Status.SUPERSEDED},
            self.Status.SUPERSEDED: {self.Status.SUPERSEDED},
        }
        if self.status not in allowed[original['status']]:
            errors['status'] = 'Недопустимый переход статуса cohort.'
        for field_name in ('approved_by_id', 'approved_at', 'superseded_at'):
            if original[field_name] is not None and original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = 'Исторические реквизиты cohort неизменяемы.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
            self._validate_existing_state()
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.watch_period}: cohort v{self.version}'


class SettlementCohortMember(StableIdentifierModel):
    class WorkShift(models.TextChoices):
        DAY = 'day', 'Дневная смена'
        NIGHT = 'night', 'Ночная смена'

    class ShiftSourceKind(models.TextChoices):
        INTERNAL_ASSIGNMENT = 'internal_assignment', 'Официальное назначение сотрудника'
        CONFIRMED_BRIGADE_PHASE = 'confirmed_brigade_phase', 'Подтверждённая фаза бригады'
        EXTERNAL_CLERK = 'external_clerk', 'Выбор делопроизводителя для внешнего жильца'
        UNVERIFIED_LEGACY = 'unverified_legacy', 'Непроверенная историческая строка'

    class ParticipationStatus(models.TextChoices):
        PARTICIPATING = 'participating', 'Участвует в заезде'
        NOT_ARRIVING = 'not_arriving', 'Не заезжает'
        EXTENDED = 'extended', 'Продление'
        ADDITIONAL = 'additional', 'Дополнительный сотрудник'

    ACTIVE_PARTICIPATION_STATUSES = (
        ParticipationStatus.PARTICIPATING,
        ParticipationStatus.EXTENDED,
        ParticipationStatus.ADDITIONAL,
    )

    objects = M5CohortQuerySet.as_manager()

    cohort = models.ForeignKey(
        SettlementCohort,
        verbose_name='Версия состава заезда',
        on_delete=models.CASCADE,
        related_name='members',
    )
    resident = models.ForeignKey(
        'SettlementResident',
        verbose_name='Жилец',
        on_delete=models.PROTECT,
        related_name='settlement_cohort_memberships',
    )
    arrival_at = models.DateTimeField('Прибытие')
    departure_at = models.DateTimeField('Выбытие')
    participation_status = models.CharField(
        'Статус участия',
        max_length=16,
        choices=ParticipationStatus.choices,
        default=ParticipationStatus.PARTICIPATING,
        db_index=True,
    )
    reason = models.CharField('Причина изменения', max_length=255, blank=True)
    expected_schedule_regime = models.CharField(
        'Ожидаемый режим графика snapshot',
        max_length=64,
        blank=True,
    )
    work_shift = models.CharField(
        'Официальная рабочая смена',
        max_length=16,
        choices=WorkShift.choices,
        blank=True,
        default='',
    )
    shift_source_kind = models.CharField(
        'Источник официальной смены',
        max_length=32,
        choices=ShiftSourceKind.choices,
        default=ShiftSourceKind.UNVERIFIED_LEGACY,
        db_index=True,
    )
    official_equipment_assignment = models.ForeignKey(
        'assignments.EquipmentAssignment',
        verbose_name='Официальное назначение-источник',
        on_delete=models.PROTECT,
        related_name='settlement_cohort_members',
        null=True,
        blank=True,
    )
    shift_source_snapshot = models.JSONField(
        'Неизменяемый снимок источника смены',
        default=dict,
        blank=True,
    )
    shift_source_fingerprint = models.CharField(
        'SHA-256 источника смены',
        max_length=64,
        blank=True,
        default='',
        validators=[RegexValidator(regex=r'^$|^[0-9a-f]{64}$')],
    )
    shift_selected_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ, выбравший смену',
        on_delete=models.PROTECT,
        related_name='selected_external_settlement_shifts',
        null=True,
        blank=True,
    )
    shift_selected_at = models.DateTimeField(
        'Когда выбрана смена внешнего жильца',
        null=True,
        blank=True,
    )
    shift_selection_basis = models.CharField(
        'Основание выбора смены внешнего жильца',
        max_length=255,
        blank=True,
    )
    source_revision = models.ForeignKey(
        SettlementRevision,
        verbose_name='Ревизия-основание строки',
        on_delete=models.PROTECT,
        related_name='settlement_cohort_members',
        null=True,
        blank=True,
    )
    routing_row = models.OneToOneField(
        'rotations.ArrivalRosterRoutingRow',
        verbose_name='Точная строка передачи реестра',
        on_delete=models.PROTECT,
        related_name='settlement_cohort_member',
        null=True,
        blank=True,
    )
    routing_event = models.OneToOneField(
        'rotations.ArrivalRosterRoutingEvent',
        verbose_name='Точное событие готовности строки',
        on_delete=models.PROTECT,
        related_name='settlement_cohort_member',
        null=True,
        blank=True,
    )
    brigade_phase_row = models.ForeignKey(
        'shifts.WatchPeriodBrigadePhaseRow',
        verbose_name='Точная подтверждённая фаза бригады',
        on_delete=models.PROTECT,
        related_name='settlement_cohort_members',
        null=True,
        blank=True,
    )
    basis_type = models.CharField('Тип основания', max_length=64)
    basis_id = models.CharField('Идентификатор основания', max_length=128)
    basis_snapshot = models.JSONField('Неизменяемый снимок основания')
    production_context_snapshot = models.JSONField(
        'Снимок производственного контекста',
        default=dict,
        blank=True,
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)

    class Meta:
        verbose_name = 'Строка жилищного состава заезда'
        verbose_name_plural = 'Строки жилищного состава заезда'
        ordering = ['cohort_id', 'resident_id', 'pk']
        indexes = [
            models.Index(
                fields=['resident', 'participation_status', 'arrival_at'],
                name='cohort_member_resident_idx',
            ),
            models.Index(
                fields=['cohort', 'participation_status'],
                name='cohort_member_scope_idx',
            ),
            models.Index(
                fields=['cohort', 'work_shift', 'shift_source_kind'],
                name='cohort_member_shift_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['cohort', 'resident'],
                name='unique_resident_per_cohort',
            ),
            models.CheckConstraint(
                condition=models.Q(departure_at__gt=models.F('arrival_at')),
                name='cohort_member_period_non_empty',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    participation_status__in=[
                        'participating', 'not_arriving', 'extended', 'additional',
                    ],
                ),
                name='cohort_member_status_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        shift_source_kind='internal_assignment',
                        work_shift__in=['day', 'night'],
                        official_equipment_assignment__isnull=False,
                        shift_selected_by_access__isnull=True,
                        shift_selected_at__isnull=True,
                        shift_selection_basis='',
                    )
                    | models.Q(
                        shift_source_kind='confirmed_brigade_phase',
                        work_shift__in=['day', 'night'],
                        official_equipment_assignment__isnull=True,
                        shift_selected_by_access__isnull=True,
                        shift_selected_at__isnull=True,
                        shift_selection_basis='',
                    )
                    | models.Q(
                        shift_source_kind='external_clerk',
                        work_shift__in=['day', 'night'],
                        official_equipment_assignment__isnull=True,
                        shift_selected_by_access__isnull=False,
                        shift_selected_at__isnull=False,
                    )
                    | models.Q(
                        shift_source_kind='unverified_legacy',
                        work_shift='',
                        official_equipment_assignment__isnull=True,
                        shift_selected_by_access__isnull=True,
                        shift_selected_at__isnull=True,
                        shift_selection_basis='',
                    )
                ),
                name='cohort_member_shift_source_ck',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        source_revision__isnull=False,
                        routing_row__isnull=True,
                        routing_event__isnull=True,
                        brigade_phase_row__isnull=True,
                        shift_source_kind__in=[
                            'internal_assignment', 'external_clerk',
                            'unverified_legacy',
                        ],
                    )
                    | models.Q(
                        source_revision__isnull=True,
                        routing_row__isnull=False,
                        routing_event__isnull=False,
                        brigade_phase_row__isnull=False,
                        shift_source_kind__in=[
                            'internal_assignment', 'confirmed_brigade_phase',
                        ],
                    )
                ),
                name='cohort_member_source_family_ck',
            ),
        ]

    @property
    def participates_in_accommodation(self):
        return self.participation_status in self.ACTIVE_PARTICIPATION_STATUSES

    def clean(self):
        super().clean()
        errors = {}
        if self.arrival_at and self.departure_at and self.departure_at <= self.arrival_at:
            errors['departure_at'] = 'Выбытие должно быть позже прибытия.'
        if not self.basis_type:
            errors['basis_type'] = 'Тип основания membership обязателен.'
        if not self.basis_id:
            errors['basis_id'] = 'Идентификатор основания membership обязателен.'
        if not isinstance(self.basis_snapshot, dict) or not self.basis_snapshot:
            errors['basis_snapshot'] = 'Снимок основания membership должен быть непустым объектом.'
        if self.participation_status != self.ParticipationStatus.PARTICIPATING and not self.reason:
            errors['reason'] = 'Незаезд, продление или дополнительное участие требуют причины.'
        if self.shift_source_kind == self.ShiftSourceKind.UNVERIFIED_LEGACY:
            if self._state.adding:
                errors['shift_source_kind'] = 'Новые membership требуют официальный источник смены.'
        else:
            if self.work_shift not in self.WorkShift.values:
                errors['work_shift'] = 'Для membership требуется дневная или ночная смена.'
            if not isinstance(self.shift_source_snapshot, dict) or not self.shift_source_snapshot:
                errors['shift_source_snapshot'] = 'Снимок источника смены обязателен.'
            if not self.shift_source_fingerprint or len(self.shift_source_fingerprint) != 64:
                errors['shift_source_fingerprint'] = 'Требуется SHA-256 источника смены.'
        if self.shift_source_kind == self.ShiftSourceKind.INTERNAL_ASSIGNMENT:
            if not self.official_equipment_assignment_id:
                errors['official_equipment_assignment'] = 'Внутренний жилец требует точное назначение.'
            if self.shift_selected_by_access_id or self.shift_selected_at or self.shift_selection_basis:
                errors['shift_selected_by_access'] = 'Внутренняя смена не выбирается делопроизводителем.'
        elif self.shift_source_kind == self.ShiftSourceKind.EXTERNAL_CLERK:
            if self.official_equipment_assignment_id:
                errors['official_equipment_assignment'] = 'Внешний жилец не имеет кадрового назначения.'
            if not self.shift_selected_by_access_id or not self.shift_selected_at:
                errors['shift_selected_by_access'] = 'Внешняя смена требует точный доступ и время выбора.'
            if not self.shift_selection_basis.strip():
                errors['shift_selection_basis'] = 'Основание выбора внешней смены обязательно.'
        elif self.shift_source_kind == self.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE:
            if self.official_equipment_assignment_id:
                errors['official_equipment_assignment'] = (
                    'Календарная фаза не использует назначение техники.'
                )
            if self.shift_selected_by_access_id or self.shift_selected_at or self.shift_selection_basis:
                errors['shift_selected_by_access'] = (
                    'Календарная фаза не выбирается делопроизводителем.'
                )
        if self.cohort_id:
            cohort = self.cohort
            period_start = timezone.make_aware(
                datetime.combine(cohort.watch_period.starts_on, time.min),
                timezone.get_current_timezone(),
            )
            period_end = timezone.make_aware(
                datetime.combine(cohort.watch_period.ends_on + timedelta(days=1), time.min),
                timezone.get_current_timezone(),
            )
            if self.arrival_at and self.departure_at:
                if self.departure_at <= period_start or self.arrival_at >= period_end:
                    errors['arrival_at'] = 'Период membership должен пересекать связанный WatchPeriod.'
            if self._state.adding:
                if cohort.status != SettlementCohort.Status.DRAFT:
                    errors['cohort'] = 'Membership добавляется только в DRAFT cohort.'
                resident = self.resident
                if resident.status != SettlementResident.Status.ACTIVE:
                    errors['resident'] = 'Архивный resident нельзя добавить в cohort.'
                if resident.resident_type == SettlementResident.ResidentType.EMPLOYEE:
                    if not resident.employee_id:
                        errors['resident'] = 'Внутренний resident не связан с Employee.'
                    else:
                        employee = resident.employee
                        if not employee.is_active or employee.status != employee.Status.ACTIVE:
                            errors['resident'] = 'Внутренний Employee неактивен или уволен.'
                        elif employee.watch_composition_id != cohort.watch_composition_id:
                            errors['resident'] = (
                                'Внутренний Employee не принадлежит WatchComposition cohort.'
                            )
                elif (
                    resident.resident_type not in {
                        SettlementResident.ResidentType.CONTRACTOR,
                        SettlementResident.ResidentType.BUSINESS_TRIP,
                        SettlementResident.ResidentType.EXTERNAL_OTHER,
                    }
                    or resident.employee_id is not None
                ):
                    errors['resident'] = 'Внешний resident имеет недопустимый источник.'
        if errors:
            raise ValidationError(errors)

    def _validate_existing_state(self):
        if not self.pk:
            return
        original = type(self)._base_manager.filter(pk=self.pk).values(
            'stable_id', 'cohort_id', 'resident_id', 'arrival_at', 'departure_at',
            'participation_status', 'reason', 'expected_schedule_regime',
            'work_shift', 'shift_source_kind', 'official_equipment_assignment_id',
            'shift_source_snapshot', 'shift_source_fingerprint',
            'shift_selected_by_access_id', 'shift_selected_at', 'shift_selection_basis',
            'source_revision_id', 'routing_row_id', 'routing_event_id',
            'brigade_phase_row_id', 'basis_type', 'basis_id', 'basis_snapshot',
            'production_context_snapshot',
        ).first()
        if original is None:
            return
        errors = {}
        for field_name, original_value in original.items():
            if original_value != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = 'Строка membership после создания неизменяема.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
            self._validate_existing_state()
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.cohort} / {self.resident}'


@receiver(pre_delete, sender=SettlementCohort)
@receiver(pre_delete, sender=SettlementCohortMember)
def protect_historical_m5_rows(sender, instance, using, origin, **kwargs):
    cohort = instance if sender is SettlementCohort else instance.cohort
    persisted_status = (
        SettlementCohort._base_manager.using(using)
        .filter(pk=cohort.pk)
        .values_list('status', flat=True)
        .first()
    )
    if persisted_status in {SettlementCohort.Status.APPROVED, SettlementCohort.Status.SUPERSEDED}:
        raise ProtectedError(
            'Утверждённые и заменённые строки M5 удалять нельзя.',
            [instance],
        )


class SettlementResidentQuerySet(models.QuerySet):
    WRITE_FORBIDDEN_CODE = 'settlement.resident.public_write_forbidden'
    WRITE_FORBIDDEN_MESSAGE = (
        'Массовые изменения и физическое удаление жильцов запрещены. '
        'Используйте доменные команды SettlementResident.'
    )

    def _raise_write_forbidden(self):
        raise ValidationError(
            self.WRITE_FORBIDDEN_MESSAGE,
            code=self.WRITE_FORBIDDEN_CODE,
        )

    def update(self, **kwargs):
        self._raise_write_forbidden()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        self._raise_write_forbidden()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_write_forbidden()

    def delete(self):
        self._raise_write_forbidden()


class SettlementResident(StableIdentifierModel):
    class ResidentType(models.TextChoices):
        EMPLOYEE = 'EMPLOYEE', 'Внутренний сотрудник'
        CONTRACTOR = 'CONTRACTOR', 'Сотрудник подрядчика'
        BUSINESS_TRIP = 'BUSINESS_TRIP', 'Командированный'
        EXTERNAL_OTHER = 'EXTERNAL_OTHER', 'Другой внешний жилец'

    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Активен'
        ARCHIVED = 'ARCHIVED', 'В архиве'

    objects = SettlementResidentQuerySet.as_manager()

    employee = models.OneToOneField(
        'users.Employee',
        verbose_name='Внутренний сотрудник',
        on_delete=models.PROTECT,
        related_name='settlement_resident',
        null=True,
        blank=True,
    )
    resident_type = models.CharField(
        'Тип жильца',
        max_length=32,
        choices=ResidentType.choices,
        db_index=True,
    )
    full_name = models.CharField('ФИО внешнего жильца', max_length=255, blank=True)
    photo = models.FileField(
        'Фото внешнего жильца',
        upload_to='settlement_residents/',
        null=True,
        blank=True,
    )
    position_title = models.CharField(
        'Должность или профессия внешнего жильца',
        max_length=255,
        blank=True,
    )
    organization = models.CharField(
        'Организация внешнего жильца',
        max_length=255,
        blank=True,
    )
    phone = models.CharField('Телефон внешнего жильца', max_length=64, blank=True)
    external_sex = models.CharField(
        'Пол внешнего жильца',
        max_length=7,
        choices=(
            ('male', 'Мужской'),
            ('female', 'Женский'),
        ),
        null=True,
        blank=True,
    )
    status = models.CharField(
        'Статус карточки',
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    revision = models.PositiveBigIntegerField('Ревизия карточки', default=1)
    archived_at = models.DateTimeField('Когда архивирована', null=True, blank=True)
    created_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Доступ создателя',
        on_delete=models.PROTECT,
        related_name='created_settlement_residents',
        null=True,
        blank=True,
    )
    updated_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Доступ последнего редактора',
        on_delete=models.PROTECT,
        related_name='updated_settlement_residents',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Изменена', auto_now=True)

    class Meta:
        verbose_name = 'Жилец settlement'
        verbose_name_plural = 'Жильцы settlement'
        ordering = ['resident_type', 'pk']
        indexes = [
            models.Index(fields=['status', 'resident_type'], name='resident_status_type_idx'),
            models.Index(fields=['organization', 'status'], name='resident_org_status_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name='settlement_resident_revision_positive',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status='ACTIVE', archived_at__isnull=True)
                    | models.Q(status='ARCHIVED', archived_at__isnull=False)
                ),
                name='settlement_resident_archive_state_valid',
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(
                            resident_type='EMPLOYEE',
                            employee__isnull=False,
                            full_name='',
                            position_title='',
                            organization='',
                            phone='',
                            external_sex__isnull=True,
                        )
                        & (models.Q(photo__isnull=True) | models.Q(photo=''))
                    )
                    | (
                        models.Q(
                            resident_type__in=[
                                'CONTRACTOR',
                                'BUSINESS_TRIP',
                                'EXTERNAL_OTHER',
                            ],
                            employee__isnull=True,
                            created_by_access__isnull=False,
                        )
                        & ~models.Q(full_name='')
                        & ~models.Q(position_title='')
                        & ~models.Q(organization='')
                        & ~models.Q(phone='')
                    )
                ),
                name='settlement_resident_subject_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        resident_type='EMPLOYEE',
                        external_sex__isnull=True,
                    )
                    | models.Q(
                        resident_type__in=[
                            'CONTRACTOR',
                            'BUSINESS_TRIP',
                            'EXTERNAL_OTHER',
                        ],
                        external_sex__isnull=False,
                        external_sex__in=['male', 'female'],
                    )
                ),
                name='settlement_resident_external_sex_valid',
            ),
        ]

    EXTERNAL_MUTABLE_FIELDS = (
        'full_name',
        'photo',
        'position_title',
        'organization',
        'phone',
        'external_sex',
        'status',
        'archived_at',
    )

    @property
    def is_external(self):
        return self.resident_type != self.ResidentType.EMPLOYEE

    @property
    def display_name(self):
        if self.employee_id:
            return self.employee.full_name
        return self.full_name

    @property
    def display_identity(self):
        return f'{self.stable_id} · {self.display_name}'

    @classmethod
    def _write_forbidden_error(cls):
        return ValidationError(
            SettlementResidentQuerySet.WRITE_FORBIDDEN_MESSAGE,
            code=SettlementResidentQuerySet.WRITE_FORBIDDEN_CODE,
        )

    def clean(self):
        super().clean()
        for field_name in ('full_name', 'position_title', 'organization', 'phone'):
            value = getattr(self, field_name)
            if isinstance(value, str):
                setattr(self, field_name, ' '.join(value.split()))

        errors = {}
        if self.resident_type == self.ResidentType.EMPLOYEE:
            if not self.employee_id:
                errors['employee'] = 'Внутренний resident требует Employee.'
            for field_name in ('full_name', 'position_title', 'organization', 'phone'):
                if getattr(self, field_name):
                    errors[field_name] = (
                        'Карточные поля не являются кадровым источником внутреннего Employee.'
                    )
            if self.photo:
                errors['photo'] = (
                    'Фото карточки не является кадровым источником внутреннего Employee.'
                )
            if self.external_sex is not None:
                errors['external_sex'] = (
                    'Пол внутреннего resident хранится только в Employee.'
                )
        else:
            if self.employee_id:
                errors['employee'] = 'Внешний resident не может ссылаться на Employee.'
            if self.resident_type not in {
                self.ResidentType.CONTRACTOR,
                self.ResidentType.BUSINESS_TRIP,
                self.ResidentType.EXTERNAL_OTHER,
            }:
                errors['resident_type'] = 'Неизвестный тип внешнего жильца.'
            for field_name in ('full_name', 'position_title', 'organization', 'phone'):
                if not getattr(self, field_name):
                    errors[field_name] = 'Поле обязательно для внешнего жильца.'
            if not self.created_by_access_id:
                errors['created_by_access'] = (
                    'Внешняя карточка требует exact EmployeeAccess делопроизводителя.'
                )
            if self.external_sex not in {'male', 'female'}:
                errors['external_sex'] = (
                    'Для внешнего жильца требуется authoritative пол male/female.'
                )
        if self.revision < 1:
            errors['revision'] = 'Ревизия карточки должна быть положительной.'
        if self.status == self.Status.ACTIVE and self.archived_at is not None:
            errors['archived_at'] = 'Активная карточка не может иметь время архивации.'
        if self.status == self.Status.ARCHIVED and self.archived_at is None:
            errors['archived_at'] = 'Архивная карточка требует время архивации.'
        if errors:
            raise ValidationError(errors)

    def _validate_existing_state(self):
        if not self.pk:
            return
        original = type(self)._base_manager.filter(pk=self.pk).values(
            'stable_id',
            'employee_id',
            'resident_type',
            'created_by_access_id',
            'full_name',
            'photo',
            'position_title',
            'organization',
            'phone',
            'external_sex',
            'status',
            'archived_at',
            'revision',
            'updated_by_access_id',
        ).first()
        if original is None:
            return

        errors = {}
        for field_name in ('stable_id', 'employee_id', 'resident_type', 'created_by_access_id'):
            if original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = (
                    'Источник и тип SettlementResident после создания неизменяемы.'
                )

        changed = any(
            original[field_name] != getattr(self, field_name)
            for field_name in self.EXTERNAL_MUTABLE_FIELDS
        )
        if changed:
            if self.revision != original['revision'] + 1:
                errors['revision'] = 'Смысловое изменение требует увеличения ревизии ровно на 1.'
            if not self.updated_by_access_id:
                errors['updated_by_access'] = 'Изменение карточки требует exact EmployeeAccess.'
        elif self.revision != original['revision']:
            errors['revision'] = 'Ревизия меняется только вместе со смысловыми полями карточки.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            if self.pk:
                type(self)._base_manager.using(using).select_for_update().get(pk=self.pk)
            self._validate_existing_state()
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise self._write_forbidden_error()

    def __str__(self):
        return self.display_identity


class PhysicalRoom(models.Model):
    class RoomType(models.TextChoices):
        STANDARD = 'standard', 'Стандартная'
        ITR = 'itr', 'ИТР'

    class TransferStatus(models.TextChoices):
        TRANSFERRED = 'transferred', 'Передана'
        NOT_TRANSFERRED = 'not_transferred', 'Не передана'

    class SexRestriction(models.TextChoices):
        UNKNOWN = 'unknown', 'Не указано'
        MALE_ONLY = 'male_only', 'Мужская'
        FEMALE_ONLY = 'female_only', 'Женская'

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
    sex_restriction = models.CharField(
        'Ограничение пола комнаты',
        max_length=11,
        choices=SexRestriction.choices,
        default=SexRestriction.UNKNOWN,
        blank=False,
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
            models.CheckConstraint(
                condition=models.Q(
                    sex_restriction__in=['unknown', 'male_only', 'female_only'],
                ),
                name='physical_room_sex_restriction_valid',
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


class M7SavedPreviewQuerySet(models.QuerySet):
    WRITE_FORBIDDEN_CODE = 'settlement.preview.public_write_forbidden'
    WRITE_FORBIDDEN_MESSAGE = (
        'Массовые изменения и физическое удаление сохранённого preview запрещены. '
        'Используйте доменные команды M7.'
    )

    def _raise_write_forbidden(self):
        raise ValidationError(
            self.WRITE_FORBIDDEN_MESSAGE,
            code=self.WRITE_FORBIDDEN_CODE,
        )

    def update(self, **kwargs):
        self._raise_write_forbidden()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        self._raise_write_forbidden()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_write_forbidden()

    def delete(self):
        self._raise_write_forbidden()


def _raise_preview_delete_forbidden():
    raise ValidationError(
        M7SavedPreviewQuerySet.WRITE_FORBIDDEN_MESSAGE,
        code=M7SavedPreviewQuerySet.WRITE_FORBIDDEN_CODE,
    )


class SettlementPreviewRun(StableIdentifierModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        CONFIRMED = 'confirmed', 'Подтверждён'
        SUPERSEDED = 'superseded', 'Заменён новым preview'

    objects = M7SavedPreviewQuerySet.as_manager()

    cohort = models.ForeignKey(
        SettlementCohort,
        verbose_name='Утверждённый состав заезда',
        on_delete=models.PROTECT,
        related_name='saved_preview_runs',
    )
    watch_period = models.ForeignKey(
        'shifts.WatchPeriod',
        verbose_name='Конкретный период вахты',
        on_delete=models.PROTECT,
        related_name='settlement_preview_runs',
    )
    watch_composition = models.ForeignKey(
        'users.WatchComposition',
        verbose_name='Утверждённый состав вахты',
        on_delete=models.PROTECT,
        related_name='settlement_preview_runs',
    )
    version = models.PositiveIntegerField('Версия preview')
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    resolver_fingerprint = models.CharField(
        'SHA-256 входов resolver',
        max_length=64,
        validators=[RegexValidator(regex=r'^[0-9a-f]{64}$')],
    )
    result_fingerprint = models.CharField(
        'SHA-256 нормализованного результата',
        max_length=64,
        validators=[RegexValidator(regex=r'^[0-9a-f]{64}$')],
    )
    requires_shift_split = models.BooleanField(
        'Требует раздельного применения смен',
        default=False,
    )
    source_snapshot = models.JSONField('Неизменяемый снимок результата и источников')
    base_confirmed_run = models.ForeignKey(
        'self',
        verbose_name='Подтверждённый preview на момент расчёта',
        on_delete=models.PROTECT,
        related_name='drafts_based_on',
        null=True,
        blank=True,
    )
    supersedes = models.ForeignKey(
        'self',
        verbose_name='Заменяемый подтверждённый preview',
        on_delete=models.PROTECT,
        related_name='replacements',
        null=True,
        blank=True,
    )
    created_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ создателя',
        on_delete=models.PROTECT,
        related_name='created_settlement_preview_runs',
    )
    confirmed_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ подтвердившего',
        on_delete=models.PROTECT,
        related_name='confirmed_settlement_preview_runs',
        null=True,
        blank=True,
    )
    revision = models.PositiveBigIntegerField('Ревизия preview', default=1)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    confirmed_at = models.DateTimeField('Подтверждён', null=True, blank=True)
    superseded_at = models.DateTimeField('Заменён', null=True, blank=True)

    class Meta:
        verbose_name = 'Сохранённый preview расселения'
        verbose_name_plural = 'Сохранённые preview расселения'
        ordering = ['watch_period_id', 'version', 'pk']
        indexes = [
            models.Index(
                fields=['watch_period', 'status', 'version'],
                name='preview_period_status_ver_idx',
            ),
            models.Index(
                fields=['cohort', 'status'],
                name='preview_cohort_status_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['watch_period', 'version'],
                name='unique_preview_watch_period_version',
            ),
            models.UniqueConstraint(
                fields=['watch_period'],
                condition=models.Q(status='confirmed'),
                name='unique_confirmed_preview_per_watch',
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name='preview_version_gte_1',
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name='preview_revision_gte_1',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status='draft',
                        confirmed_by_access__isnull=True,
                        confirmed_at__isnull=True,
                        superseded_at__isnull=True,
                        supersedes__isnull=True,
                    )
                    | models.Q(
                        status='confirmed',
                        confirmed_by_access__isnull=False,
                        confirmed_at__isnull=False,
                        superseded_at__isnull=True,
                    )
                    | models.Q(
                        status='superseded',
                        confirmed_by_access__isnull=False,
                        confirmed_at__isnull=False,
                        superseded_at__isnull=False,
                    )
                ),
                name='preview_lifecycle_metadata',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(base_confirmed_run__isnull=True)
                    | ~models.Q(pk=models.F('base_confirmed_run_id'))
                ),
                name='preview_base_not_self',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(supersedes__isnull=True)
                    | ~models.Q(pk=models.F('supersedes_id'))
                ),
                name='preview_supersedes_not_self',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self._state.adding and self.status != self.Status.DRAFT:
            errors['status'] = 'SettlementPreviewRun создаётся только как DRAFT.'
        if self.cohort_id and self.watch_period_id and self.watch_composition_id:
            if self.cohort.watch_period_id != self.watch_period_id:
                errors['cohort'] = 'Cohort относится к другому WatchPeriod.'
            if self.cohort.watch_composition_id != self.watch_composition_id:
                errors['cohort'] = 'Cohort относится к другому WatchComposition.'
            if self.watch_period.watch_composition_id != self.watch_composition_id:
                errors['watch_composition'] = 'WatchPeriod не принадлежит WatchComposition preview.'
        if not isinstance(self.source_snapshot, dict) or not self.source_snapshot:
            errors['source_snapshot'] = 'Снимок preview должен быть непустым объектом.'
        if self.base_confirmed_run_id:
            base = self.base_confirmed_run
            if base.watch_period_id != self.watch_period_id:
                errors['base_confirmed_run'] = 'Базовый preview относится к другому WatchPeriod.'
            if base.status not in {self.Status.CONFIRMED, self.Status.SUPERSEDED}:
                errors['base_confirmed_run'] = 'Базовый preview должен иметь подтверждённую историю.'
        if self.supersedes_id:
            previous = self.supersedes
            if previous.watch_period_id != self.watch_period_id:
                errors['supersedes'] = 'Заменяемый preview относится к другому WatchPeriod.'
            if self.supersedes_id != self.base_confirmed_run_id:
                errors['supersedes'] = 'Подтверждение может заменить только зафиксированный base preview.'
        if self.status == self.Status.DRAFT:
            if self.confirmed_by_access_id or self.confirmed_at or self.superseded_at or self.supersedes_id:
                errors['status'] = 'DRAFT preview не содержит реквизиты подтверждения или замены.'
        elif self.status == self.Status.CONFIRMED:
            if not self.confirmed_by_access_id or not self.confirmed_at or self.superseded_at:
                errors['status'] = 'CONFIRMED preview требует автора и время подтверждения.'
        elif self.status == self.Status.SUPERSEDED:
            if not self.confirmed_by_access_id or not self.confirmed_at or not self.superseded_at:
                errors['status'] = 'SUPERSEDED preview требует полную историю lifecycle.'
        if errors:
            raise ValidationError(errors)

    def _validate_existing_state(self):
        if not self.pk:
            return
        original = type(self)._base_manager.filter(pk=self.pk).values(
            'stable_id', 'cohort_id', 'watch_period_id', 'watch_composition_id',
            'version', 'resolver_fingerprint', 'result_fingerprint',
            'requires_shift_split', 'source_snapshot', 'base_confirmed_run_id',
            'created_by_access_id',
            'status', 'supersedes_id', 'confirmed_by_access_id',
            'confirmed_at', 'superseded_at', 'revision',
        ).first()
        if original is None:
            return
        errors = {}
        for field_name in (
            'stable_id', 'cohort_id', 'watch_period_id', 'watch_composition_id',
            'version', 'resolver_fingerprint', 'result_fingerprint',
            'requires_shift_split', 'source_snapshot', 'base_confirmed_run_id',
            'created_by_access_id',
        ):
            if original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = 'Смысловые поля preview после создания неизменяемы.'
        allowed = {
            self.Status.DRAFT: {self.Status.DRAFT, self.Status.CONFIRMED},
            self.Status.CONFIRMED: {self.Status.CONFIRMED, self.Status.SUPERSEDED},
            self.Status.SUPERSEDED: {self.Status.SUPERSEDED},
        }
        if self.status not in allowed[original['status']]:
            errors['status'] = 'Недопустимый переход статуса preview.'
        transition = self.status != original['status']
        expected_revision = original['revision'] + (1 if transition else 0)
        if self.revision != expected_revision:
            errors['revision'] = 'Ревизия preview должна увеличиваться ровно на один при смене статуса.'
        if original['supersedes_id'] is not None and original['supersedes_id'] != self.supersedes_id:
            errors['supersedes'] = 'Историческую связь supersedes менять нельзя.'
        for field_name in ('confirmed_by_access_id', 'confirmed_at', 'superseded_at'):
            if original[field_name] is not None and original[field_name] != getattr(self, field_name):
                errors[field_name.removesuffix('_id')] = 'Исторические реквизиты preview неизменяемы.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
            self._validate_existing_state()
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        _raise_preview_delete_forbidden()

    def __str__(self):
        return f'{self.watch_period}: preview v{self.version}'


class SettlementPreviewPlacement(StableIdentifierModel):
    objects = M7SavedPreviewQuerySet.as_manager()

    run = models.ForeignKey(
        SettlementPreviewRun,
        verbose_name='Сохранённый preview',
        on_delete=models.PROTECT,
        related_name='placements',
    )
    resident = models.ForeignKey(
        SettlementResident,
        verbose_name='Жилец',
        on_delete=models.PROTECT,
        related_name='saved_preview_placements',
    )
    calendar_slot = models.ForeignKey(
        AccommodationAnchorCalendarSlot,
        verbose_name='Календарный слот',
        on_delete=models.PROTECT,
        related_name='saved_preview_placements',
    )
    physical_bed = models.ForeignKey(
        PhysicalBed,
        verbose_name='Физическое койко-место',
        on_delete=models.PROTECT,
        related_name='saved_preview_placements',
    )
    action = models.CharField('Действие resolver', max_length=32)
    source_kind = models.CharField('Волна resolver', max_length=64)
    work_shift = models.CharField(
        'Официальная смена',
        max_length=16,
        choices=SettlementCohortMember.WorkShift.choices,
        blank=True,
        default='',
    )
    cohort_member_id_snapshot = models.PositiveBigIntegerField('PK membership snapshot')
    physical_room_id_snapshot = models.PositiveBigIntegerField('PK комнаты snapshot')
    binding_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    equipment_assignment_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    anchor_id_snapshot = models.PositiveBigIntegerField()
    anchor_bed_assignment_id_snapshot = models.PositiveBigIntegerField()
    normalized_provenance = models.JSONField('Неизменяемая нормализованная provenance')
    created_at = models.DateTimeField('Создана', auto_now_add=True)

    class Meta:
        verbose_name = 'Строка размещения сохранённого preview'
        verbose_name_plural = 'Строки размещения сохранённого preview'
        ordering = ['run_id', 'resident_id', 'pk']
        indexes = [
            models.Index(fields=['run', 'source_kind'], name='preview_place_source_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'resident'],
                name='unique_preview_placement_resident',
            ),
            models.UniqueConstraint(
                fields=['run', 'calendar_slot'],
                name='unique_preview_placement_slot',
            ),
            models.UniqueConstraint(
                fields=['run', 'physical_bed'],
                name='unique_preview_placement_bed',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.run_id and self.run.status != SettlementPreviewRun.Status.DRAFT:
            errors['run'] = 'Строки добавляются только в DRAFT preview.'
        if self.calendar_slot_id and self.run_id:
            if self.calendar_slot.watch_period_id != self.run.watch_period_id:
                errors['calendar_slot'] = 'CalendarSlot относится к другому WatchPeriod.'
        if not self.action:
            errors['action'] = 'Действие resolver обязательно.'
        if not self.source_kind:
            errors['source_kind'] = 'Волна resolver обязательна.'
        if not isinstance(self.normalized_provenance, dict) or not self.normalized_provenance:
            errors['normalized_provenance'] = 'Нормализованная provenance обязательна.'
        if self.run_id and self.resident_id:
            if SettlementPreviewUnresolved.objects.filter(
                run_id=self.run_id,
                resident_id=self.resident_id,
            ).exists():
                errors['resident'] = 'Resident уже сохранён как unresolved в этом preview.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
                self._validate_immutable_fields(
                    'run', 'resident', 'calendar_slot', 'physical_bed', 'action',
                    'source_kind', 'work_shift', 'cohort_member_id_snapshot',
                    'physical_room_id_snapshot', 'binding_id_snapshot',
                    'equipment_assignment_id_snapshot', 'anchor_id_snapshot',
                    'anchor_bed_assignment_id_snapshot', 'normalized_provenance',
                )
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        _raise_preview_delete_forbidden()


class SettlementPreviewUnresolved(StableIdentifierModel):
    objects = M7SavedPreviewQuerySet.as_manager()

    run = models.ForeignKey(
        SettlementPreviewRun,
        verbose_name='Сохранённый preview',
        on_delete=models.PROTECT,
        related_name='unresolved_rows',
    )
    resident = models.ForeignKey(
        SettlementResident,
        verbose_name='Жилец',
        on_delete=models.PROTECT,
        related_name='saved_preview_unresolved_rows',
    )
    reason_code = models.CharField('Основной код причины', max_length=64)
    reason_codes = models.JSONField('Все коды причин')
    work_shift = models.CharField(
        'Официальная смена',
        max_length=16,
        choices=SettlementCohortMember.WorkShift.choices,
        blank=True,
        default='',
    )
    cohort_member_id_snapshot = models.PositiveBigIntegerField('PK membership snapshot')
    structured_details = models.JSONField('Неизменяемые структурированные детали')
    created_at = models.DateTimeField('Создана', auto_now_add=True)

    class Meta:
        verbose_name = 'Нерасселённая строка сохранённого preview'
        verbose_name_plural = 'Нерасселённые строки сохранённого preview'
        ordering = ['run_id', 'resident_id', 'pk']
        indexes = [
            models.Index(fields=['run', 'reason_code'], name='preview_unresolved_reason_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'resident'],
                name='unique_preview_unresolved_resident',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.run_id and self.run.status != SettlementPreviewRun.Status.DRAFT:
            errors['run'] = 'Строки добавляются только в DRAFT preview.'
        if not self.reason_code:
            errors['reason_code'] = 'Код причины обязателен.'
        if (
            not isinstance(self.reason_codes, list)
            or not self.reason_codes
            or self.reason_code not in self.reason_codes
        ):
            errors['reason_codes'] = 'Полный непустой список причин должен содержать основной код.'
        if not isinstance(self.structured_details, dict) or not self.structured_details:
            errors['structured_details'] = 'Структурированные детали обязательны.'
        if self.run_id and self.resident_id:
            if SettlementPreviewPlacement.objects.filter(
                run_id=self.run_id,
                resident_id=self.resident_id,
            ).exists():
                errors['resident'] = 'Resident уже сохранён как placement в этом preview.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
                self._validate_immutable_fields(
                    'run', 'resident', 'reason_code', 'reason_codes',
                    'work_shift', 'cohort_member_id_snapshot', 'structured_details',
                )
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        _raise_preview_delete_forbidden()


class SettlementPreviewCorrectionQuerySet(models.QuerySet):
    WRITE_FORBIDDEN_CODE = 'settlement.preview_correction.public_write_forbidden'
    WRITE_FORBIDDEN_MESSAGE = (
        'Публичное изменение или удаление точечных исправлений запрещено. '
        'Используйте доменные команды исправления плана.'
    )

    def _raise_write_forbidden(self):
        raise ValidationError(
            self.WRITE_FORBIDDEN_MESSAGE,
            code=self.WRITE_FORBIDDEN_CODE,
        )

    def update(self, **kwargs):
        self._raise_write_forbidden()

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False,
                    update_conflicts=False, update_fields=None, unique_fields=None):
        self._raise_write_forbidden()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_write_forbidden()

    def delete(self):
        self._raise_write_forbidden()


class SettlementPreviewCorrection(StableIdentifierModel):
    class Action(models.TextChoices):
        MOVE = 'move', 'Переставить'
        EXCLUDE = 'exclude', 'Исключить'
        RESTORE = 'restore', 'Вернуть исходное решение'

    objects = SettlementPreviewCorrectionQuerySet.as_manager()

    preview_run = models.ForeignKey(
        SettlementPreviewRun,
        on_delete=models.PROTECT,
        related_name='corrections',
    )
    resident = models.ForeignKey(
        SettlementResident,
        on_delete=models.PROTECT,
        related_name='preview_corrections',
    )
    work_shift = models.CharField(
        max_length=16,
        choices=SettlementCohortMember.WorkShift.choices,
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    source_placement = models.ForeignKey(
        SettlementPreviewPlacement,
        on_delete=models.PROTECT,
        related_name='corrections',
        null=True,
        blank=True,
    )
    source_unresolved = models.ForeignKey(
        SettlementPreviewUnresolved,
        on_delete=models.PROTECT,
        related_name='corrections',
        null=True,
        blank=True,
    )
    target_calendar_slot = models.ForeignKey(
        AccommodationAnchorCalendarSlot,
        on_delete=models.PROTECT,
        related_name='preview_corrections',
        null=True,
        blank=True,
    )
    target_physical_bed = models.ForeignKey(
        PhysicalBed,
        on_delete=models.PROTECT,
        related_name='preview_corrections',
        null=True,
        blank=True,
    )
    actor_access = models.ForeignKey(
        'users.EmployeeAccess',
        on_delete=models.PROTECT,
        related_name='settlement_preview_corrections',
    )
    reason = models.TextField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    supersedes = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        related_name='superseded_by',
        null=True,
        blank=True,
    )
    source_snapshot = models.JSONField()
    fingerprint = models.CharField(
        max_length=64,
        validators=[RegexValidator(regex=r'^[0-9a-f]{64}$')],
    )

    class Meta:
        ordering = ['preview_run_id', 'resident_id', 'work_shift', 'created_at', 'pk']
        indexes = [
            models.Index(
                fields=['preview_run', 'resident', 'work_shift', 'created_at'],
                name='preview_corr_effective_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(source_placement__isnull=False, source_unresolved__isnull=True)
                    | models.Q(source_placement__isnull=True, source_unresolved__isnull=False)
                ),
                name='preview_corr_source_xor',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        action='move',
                        target_calendar_slot__isnull=False,
                        target_physical_bed__isnull=False,
                    )
                    | models.Q(
                        action__in=['exclude', 'restore'],
                        target_calendar_slot__isnull=True,
                        target_physical_bed__isnull=True,
                    )
                ),
                name='preview_corr_target_by_action',
            ),
            models.CheckConstraint(
                condition=models.Q(supersedes__isnull=True) | ~models.Q(pk=models.F('supersedes_id')),
                name='preview_corr_not_self',
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(action='restore')
                    | models.Q(supersedes__isnull=False)
                ),
                name='preview_corr_restore_has_previous',
            ),
            models.UniqueConstraint(
                fields=['supersedes'],
                condition=models.Q(supersedes__isnull=False),
                name='unique_preview_corr_successor',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        source = self.source_placement or self.source_unresolved
        if bool(self.source_placement_id) == bool(self.source_unresolved_id):
            errors['source_placement'] = 'Исправление требует ровно одну исходную строку.'
        elif (
            source.run_id != self.preview_run_id
            or source.resident_id != self.resident_id
            or source.work_shift != self.work_shift
        ):
            errors['resident'] = 'Исходная строка не соответствует плану, жильцу или смене.'
        if self.action == self.Action.MOVE:
            if not self.target_calendar_slot_id or not self.target_physical_bed_id:
                errors['target_physical_bed'] = 'Перестановка требует точный слот и койку.'
        elif self.target_calendar_slot_id or self.target_physical_bed_id:
            errors['target_physical_bed'] = 'Это действие не допускает целевое место.'
        if self.action == self.Action.RESTORE and not self.supersedes_id:
            errors['supersedes'] = 'Возврат требует предыдущее действующее исправление.'
        if self.supersedes_id:
            previous = self.supersedes
            if (
                previous.preview_run_id != self.preview_run_id
                or previous.resident_id != self.resident_id
                or previous.work_shift != self.work_shift
                or previous.source_placement_id != self.source_placement_id
                or previous.source_unresolved_id != self.source_unresolved_id
            ):
                errors['supersedes'] = 'Цепочка исправлений относится к другому решению.'
        if not str(self.reason or '').strip():
            errors['reason'] = 'Причина исправления обязательна.'
        if not isinstance(self.source_snapshot, dict) or not self.source_snapshot:
            errors['source_snapshot'] = 'Неизменяемый снимок источника обязателен.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(
                SettlementPreviewCorrectionQuerySet.WRITE_FORBIDDEN_MESSAGE,
                code=SettlementPreviewCorrectionQuerySet.WRITE_FORBIDDEN_CODE,
            )
        if not getattr(self, '_allow_domain_insert', False):
            raise ValidationError(
                SettlementPreviewCorrectionQuerySet.WRITE_FORBIDDEN_MESSAGE,
                code=SettlementPreviewCorrectionQuerySet.WRITE_FORBIDDEN_CODE,
            )
        self.reason = str(self.reason).strip()
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            SettlementPreviewCorrectionQuerySet.WRITE_FORBIDDEN_MESSAGE,
            code=SettlementPreviewCorrectionQuerySet.WRITE_FORBIDDEN_CODE,
        )


class M8ApplyQuerySet(models.QuerySet):
    WRITE_FORBIDDEN_CODE = 'settlement.apply.public_write_forbidden'
    WRITE_FORBIDDEN_MESSAGE = (
        'Публичное изменение или удаление истории Apply запрещено. '
        'Используйте canonical settlement Apply service.'
    )

    def _raise_write_forbidden(self):
        raise ValidationError(
            self.WRITE_FORBIDDEN_MESSAGE,
            code=self.WRITE_FORBIDDEN_CODE,
        )

    def update(self, **kwargs):
        self._raise_write_forbidden()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        self._raise_write_forbidden()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_write_forbidden()

    def delete(self):
        self._raise_write_forbidden()


def _raise_apply_write_forbidden():
    raise ValidationError(
        M8ApplyQuerySet.WRITE_FORBIDDEN_MESSAGE,
        code=M8ApplyQuerySet.WRITE_FORBIDDEN_CODE,
    )


class SettlementPreviewApplication(StableIdentifierModel):
    objects = M8ApplyQuerySet.as_manager()

    preview_run = models.ForeignKey(
        SettlementPreviewRun,
        verbose_name='Применённый preview',
        on_delete=models.PROTECT,
        related_name='applications',
    )
    work_shift = models.CharField(
        'Применённая смена',
        max_length=16,
        choices=SettlementCohortMember.WorkShift.choices,
        null=True,
        blank=True,
    )
    legacy_whole_run = models.BooleanField(
        'Историческое применение всего плана',
        default=False,
    )
    watch_period = models.ForeignKey(
        'shifts.WatchPeriod',
        verbose_name='Период вахты',
        on_delete=models.PROTECT,
        related_name='settlement_preview_applications',
    )
    cohort = models.ForeignKey(
        SettlementCohort,
        verbose_name='Утверждённый состав заезда',
        on_delete=models.PROTECT,
        related_name='settlement_preview_applications',
    )
    applied_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ применившего',
        on_delete=models.PROTECT,
        related_name='settlement_preview_applications',
    )
    applied_at = models.DateTimeField('Применено', auto_now_add=True)
    resolver_fingerprint = models.CharField(
        'SHA-256 входов resolver',
        max_length=64,
        validators=[RegexValidator(regex=r'^[0-9a-f]{64}$')],
    )
    normalized_fingerprint = models.CharField(
        'SHA-256 нормализованного результата',
        max_length=64,
        validators=[RegexValidator(regex=r'^[0-9a-f]{64}$')],
    )
    confirm_replace_manual = models.BooleanField(default=False)
    created_occupancy_count = models.PositiveIntegerField(default=0)
    closed_occupancy_count = models.PositiveIntegerField(default=0)
    replaced_occupancy_count = models.PositiveIntegerField(default=0)
    result_snapshot = models.JSONField('Неизменяемый снимок результата Apply')

    class Meta:
        verbose_name = 'Применение сохранённого preview'
        verbose_name_plural = 'Применения сохранённых preview'
        ordering = ['watch_period_id', 'applied_at', 'pk']
        indexes = [
            models.Index(
                fields=['watch_period', 'applied_at'],
                name='preview_apply_period_at_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['preview_run', 'work_shift'],
                name='unique_preview_apply_run_shift',
            ),
            models.UniqueConstraint(
                fields=['preview_run'],
                condition=models.Q(legacy_whole_run=True),
                name='unique_legacy_apply_per_run',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(legacy_whole_run=True, work_shift__isnull=True)
                    | models.Q(
                        legacy_whole_run=False,
                        work_shift__in=[
                            SettlementCohortMember.WorkShift.DAY,
                            SettlementCohortMember.WorkShift.NIGHT,
                        ],
                    )
                ),
                name='preview_apply_shift_scope_valid',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self._state.adding and self.legacy_whole_run:
            errors['legacy_whole_run'] = 'Новые применения всего плана запрещены.'
        if not self.legacy_whole_run and self.work_shift not in {
            SettlementCohortMember.WorkShift.DAY,
            SettlementCohortMember.WorkShift.NIGHT,
        }:
            errors['work_shift'] = 'Для нового Apply требуется дневная или ночная смена.'
        if self.preview_run_id and self.watch_period_id and self.cohort_id:
            if self.preview_run.watch_period_id != self.watch_period_id:
                errors['watch_period'] = 'Application относится к другому WatchPeriod.'
            if self.preview_run.cohort_id != self.cohort_id:
                errors['cohort'] = 'Application относится к другому cohort.'
        if not isinstance(self.result_snapshot, dict) or not self.result_snapshot:
            errors['result_snapshot'] = 'Снимок результата Apply должен быть непустым объектом.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
                self._validate_immutable_fields(
                    'preview_run', 'work_shift', 'legacy_whole_run',
                    'watch_period', 'cohort', 'applied_by_access',
                    'applied_at', 'resolver_fingerprint', 'normalized_fingerprint',
                    'confirm_replace_manual', 'created_occupancy_count',
                    'closed_occupancy_count', 'replaced_occupancy_count',
                    'result_snapshot',
                )
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        _raise_apply_write_forbidden()


class EmployeeBedOccupancyQuerySet(models.QuerySet):
    MASS_WRITE_FORBIDDEN_CODE = 'employee_bed_occupancy_mass_write_forbidden'
    MASS_WRITE_FORBIDDEN_MESSAGE = (
        'Массовые изменения фактического проживания запрещены. '
        'Используйте штатные settlement services или instance save() '
        'для разрешённых операций.'
    )

    def _raise_mass_write_forbidden(self):
        raise ValidationError(
            self.MASS_WRITE_FORBIDDEN_MESSAGE,
            code=self.MASS_WRITE_FORBIDDEN_CODE,
        )

    def update(self, **kwargs):
        self._raise_mass_write_forbidden()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        self._raise_mass_write_forbidden()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_mass_write_forbidden()

    def delete(self):
        _raise_apply_write_forbidden()


class EmployeeBedOccupancyManager(models.Manager.from_queryset(EmployeeBedOccupancyQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(
            replaced_by_application__isnull=True,
            replaced_by_occupancy__isnull=True,
        )


class EmployeeBedOccupancy(models.Model):
    """Legacy technical name; authoritative accommodation subject is resident."""

    class AssignmentType(models.TextChoices):
        PERMANENT = 'permanent', 'Постоянное'
        TEMPORARY = 'temporary', 'Временное'
        PROPOSED = 'proposed', 'Предложенное'

    class SourceKind(models.TextChoices):
        MANUAL = 'manual', 'Ручное'
        AUTO = 'auto', 'Авторосселение'

    class ShiftSourceKind(models.TextChoices):
        UNVERIFIED_LEGACY = 'unverified_legacy', 'Непроверенное историческое'
        AUTO_PREVIEW = 'auto_preview', 'Подтверждённый preview'
        OFFICIAL_ASSIGNMENT = 'official_assignment', 'Официальное назначение'
        CLERK_SELECTED = 'clerk_selected', 'Выбор делопроизводителя'

    objects = EmployeeBedOccupancyManager()

    resident = models.ForeignKey(
        SettlementResident,
        verbose_name='Жилец',
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
    source_kind = models.CharField(
        'Источник размещения',
        max_length=16,
        choices=SourceKind.choices,
        default=SourceKind.MANUAL,
        db_index=True,
    )
    work_shift = models.CharField(
        'Смена фактического проживания',
        max_length=16,
        choices=SettlementCohortMember.WorkShift.choices,
        null=True,
        blank=True,
    )
    shift_source_kind = models.CharField(
        'Источник смены',
        max_length=32,
        choices=ShiftSourceKind.choices,
        default=ShiftSourceKind.UNVERIFIED_LEGACY,
        db_index=True,
    )
    shift_source_fingerprint = models.CharField(
        'SHA-256 источника смены',
        max_length=64,
        blank=True,
        default='',
        validators=[RegexValidator(regex=r'^$|^[0-9a-f]{64}$')],
    )
    shift_official_assignment = models.ForeignKey(
        'assignments.EquipmentAssignment',
        verbose_name='Официальное назначение смены',
        on_delete=models.PROTECT,
        related_name='settlement_shift_occupancies',
        null=True,
        blank=True,
    )
    shift_selected_by_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Точный доступ выбравшего смену',
        on_delete=models.PROTECT,
        related_name='selected_settlement_shift_occupancies',
        null=True,
        blank=True,
    )
    shift_selected_at = models.DateTimeField(
        'Когда выбрана смена',
        null=True,
        blank=True,
    )
    shift_selection_basis = models.TextField(
        'Основание выбора смены',
        blank=True,
        default='',
    )
    source_application = models.ForeignKey(
        SettlementPreviewApplication,
        verbose_name='Application-источник AUTO',
        on_delete=models.PROTECT,
        related_name='created_occupancies',
        null=True,
        blank=True,
    )
    source_preview_placement = models.ForeignKey(
        SettlementPreviewPlacement,
        verbose_name='Placement-источник AUTO',
        on_delete=models.PROTECT,
        related_name='created_occupancies',
        null=True,
        blank=True,
    )
    source_preview_correction = models.ForeignKey(
        SettlementPreviewCorrection,
        verbose_name='Точечное исправление-источник AUTO',
        on_delete=models.PROTECT,
        related_name='created_occupancies',
        null=True,
        blank=True,
    )
    replaced_by_application = models.ForeignKey(
        SettlementPreviewApplication,
        verbose_name='Application, заменивший размещение',
        on_delete=models.PROTECT,
        related_name='replaced_occupancies',
        null=True,
        blank=True,
    )
    replaced_by_occupancy = models.ForeignKey(
        'self',
        verbose_name='Ручная occupancy, заменившая эту строку',
        on_delete=models.PROTECT,
        related_name='replaced_occupancy_history',
        null=True,
        blank=True,
    )
    watch_period = models.ForeignKey(
        'shifts.WatchPeriod',
        verbose_name='Период вахты occupancy',
        on_delete=models.PROTECT,
        related_name='bed_occupancies',
        null=True,
        blank=True,
    )
    cohort_member = models.ForeignKey(
        SettlementCohortMember,
        verbose_name='Строка состава occupancy',
        on_delete=models.PROTECT,
        related_name='bed_occupancies',
        null=True,
        blank=True,
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
        verbose_name = 'Размещение жильца на койко-месте'
        verbose_name_plural = 'Размещения жильцов на койко-местах'
        ordering = ['-settled_at', '-id']
        constraints = [
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
            models.CheckConstraint(
                condition=(
                    models.Q(
                        source_kind='manual',
                        source_application__isnull=True,
                        source_preview_placement__isnull=True,
                        source_preview_correction__isnull=True,
                    )
                    | (
                        models.Q(
                            source_kind='auto',
                            source_application__isnull=False,
                            watch_period__isnull=False,
                            cohort_member__isnull=False,
                        )
                        & (
                            models.Q(source_preview_placement__isnull=False)
                            | models.Q(source_preview_correction__isnull=False)
                        )
                    )
                ),
                name='occupancy_auto_provenance_complete',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        shift_source_kind='unverified_legacy',
                        work_shift__isnull=True,
                        shift_source_fingerprint='',
                        shift_official_assignment__isnull=True,
                        shift_selected_by_access__isnull=True,
                        shift_selected_at__isnull=True,
                        shift_selection_basis='',
                    )
                    | models.Q(
                        shift_source_kind='auto_preview',
                        source_kind='auto',
                        work_shift__in=['day', 'night'],
                        shift_source_fingerprint__gt='',
                        shift_official_assignment__isnull=True,
                        shift_selected_by_access__isnull=True,
                        shift_selected_at__isnull=True,
                        shift_selection_basis='',
                    )
                    | models.Q(
                        shift_source_kind='official_assignment',
                        source_kind='manual',
                        work_shift__in=['day', 'night'],
                        shift_source_fingerprint__gt='',
                        shift_official_assignment__isnull=False,
                        shift_selected_by_access__isnull=True,
                        shift_selected_at__isnull=True,
                        shift_selection_basis='',
                    )
                    | models.Q(
                        shift_source_kind='clerk_selected',
                        source_kind='manual',
                        work_shift__in=['day', 'night'],
                        shift_source_fingerprint__gt='',
                        shift_official_assignment__isnull=True,
                        shift_selected_by_access__isnull=False,
                        shift_selected_at__isnull=False,
                        shift_selection_basis__gt='',
                    )
                ),
                name='occupancy_shift_provenance_valid',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.shift_source_kind == self.ShiftSourceKind.AUTO_PREVIEW:
            application = self.source_application
            placement = self.source_preview_placement
            correction = self.source_preview_correction
            correction_source = (
                correction.source_placement or correction.source_unresolved
                if correction else None
            )
            correction_bed_id = (
                correction.target_physical_bed_id
                if correction and correction.action == SettlementPreviewCorrection.Action.MOVE
                else correction.source_placement.physical_bed_id
                if correction and correction.source_placement_id else None
            )
            member = self.cohort_member
            if not application or application.legacy_whole_run:
                errors['source_application'] = 'AUTO shift требует сменную Application.'
            if (not placement and not correction) or not member:
                errors['source_preview_placement'] = 'AUTO shift требует исходную строку и membership.'
            elif (
                application
                and (
                    self.work_shift != application.work_shift
                    or self.work_shift != (correction.work_shift if correction else placement.work_shift)
                    or application.preview_run_id != (
                        correction.preview_run_id if correction else placement.run_id
                    )
                    or application.watch_period_id != self.watch_period_id
                    or application.cohort_id != member.cohort_id
                    or (correction.resident_id if correction else placement.resident_id)
                    != self.resident_id
                    or (
                        correction_bed_id
                        if correction else placement.physical_bed_id
                    ) != self.physical_bed_id
                    or (
                        correction_source.cohort_member_id_snapshot
                        if correction_source else placement.cohort_member_id_snapshot
                    ) != member.pk
                )
            ):
                errors['work_shift'] = 'AUTO shift provenance не соответствует Application и Placement.'
            if (
                (placement or correction)
                and self.shift_source_fingerprint
                != (
                    correction.source_snapshot.get('shift_source_fingerprint')
                    if correction else placement.normalized_provenance.get('shift_source_fingerprint')
                )
            ):
                errors['shift_source_fingerprint'] = 'Fingerprint смены не соответствует membership.'
        if errors:
            raise ValidationError(errors)

    def _validate_immutable_fields(self, *field_names):
        if not self.pk:
            return
        original = type(self)._base_manager.filter(pk=self.pk).values(*field_names).first()
        if not original:
            return
        errors = {}
        for field_name in field_names:
            field = self._meta.get_field(field_name)
            if original[field_name] != getattr(self, field.attname):
                errors[field_name] = 'После создания это поле изменять нельзя.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update(of=('self',)).get(pk=self.pk)
                self._validate_immutable_fields(
                    'source_kind', 'source_application', 'source_preview_placement',
                    'source_preview_correction',
                    'watch_period', 'cohort_member', 'work_shift', 'shift_source_kind',
                    'shift_source_fingerprint', 'shift_official_assignment',
                    'shift_selected_by_access', 'shift_selected_at',
                    'shift_selection_basis',
                )
            self.clean()
            return super().save(*args, **kwargs)

    def is_active_at(self, moment):
        if (
            self.replaced_by_application_id is not None
            or self.replaced_by_occupancy_id is not None
        ):
            return False
        if moment < self.starts_at:
            return False
        if self.ends_at is not None and moment >= self.ends_at:
            return False
        if self.terminated_at is not None and moment >= self.terminated_at:
            return False
        return True

    @property
    def is_active(self):
        return self.is_active_at(timezone.now())

    def __str__(self):
        return f'{self.resident} — {self.physical_bed}'

    def delete(self, *args, **kwargs):
        _raise_apply_write_forbidden()


class SettlementPreviewApplicationItem(StableIdentifierModel):
    class Action(models.TextChoices):
        CREATED = 'created', 'Создано AUTO-размещение'
        REUSED = 'reused', 'Переиспользовано AUTO-размещение'
        REPLACED_AUTO = 'replaced_auto', 'Заменено прежнее AUTO-размещение'
        REPLACED_MANUAL = 'replaced_manual', 'Заменена ручная корректировка'
        EXCLUDED = 'excluded', 'Жилец намеренно исключён из применения'

    objects = M8ApplyQuerySet.as_manager()

    application = models.ForeignKey(
        SettlementPreviewApplication,
        verbose_name='Применение preview',
        on_delete=models.PROTECT,
        related_name='occupancy_items',
    )
    occupancy = models.ForeignKey(
        EmployeeBedOccupancy,
        verbose_name='Историческая или созданная occupancy',
        on_delete=models.PROTECT,
        related_name='application_items',
        null=True,
        blank=True,
    )
    preview_placement = models.ForeignKey(
        SettlementPreviewPlacement,
        verbose_name='Строка preview',
        on_delete=models.PROTECT,
        related_name='application_items',
        null=True,
        blank=True,
    )
    preview_unresolved = models.ForeignKey(
        SettlementPreviewUnresolved,
        on_delete=models.PROTECT,
        related_name='application_items',
        null=True,
        blank=True,
    )
    effective_correction = models.ForeignKey(
        SettlementPreviewCorrection,
        on_delete=models.PROTECT,
        related_name='application_items',
        null=True,
        blank=True,
    )
    correction_fingerprint = models.CharField(max_length=64, blank=True, default='')
    source_decision_snapshot = models.JSONField(default=dict, blank=True)
    effective_decision_snapshot = models.JSONField(default=dict, blank=True)
    action = models.CharField('Действие Apply', max_length=24, choices=Action.choices)
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'Связь Application с occupancy'
        verbose_name_plural = 'Связи Application с occupancy'
        ordering = ['application_id', 'occupancy_id', 'action', 'pk']
        constraints = [
            models.UniqueConstraint(
                fields=['application', 'occupancy', 'action'],
                name='unique_apply_occupancy_action',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        action='excluded',
                        occupancy__isnull=True,
                        effective_correction__isnull=False,
                    )
                    | (~models.Q(action='excluded') & models.Q(occupancy__isnull=False))
                ),
                name='apply_item_occupancy_by_action',
            ),
        ]

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk:
                type(self)._base_manager.select_for_update().get(pk=self.pk)
                self._validate_immutable_fields(
                    'application', 'occupancy', 'preview_placement',
                    'preview_unresolved', 'effective_correction',
                    'correction_fingerprint', 'source_decision_snapshot',
                    'effective_decision_snapshot', 'action',
                )
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        _raise_apply_write_forbidden()


class SettlementControlLease(models.Model):
    ALLOWED_OWNER_ROLE_CODES = frozenset({'settlement_clerk', 'admin'})

    scope = models.CharField(
        'Контур управления',
        max_length=64,
        unique=True,
    )
    owner_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Доступ текущего владельца',
        on_delete=models.PROTECT,
        related_name='settlement_control_leases',
        null=True,
        blank=True,
    )
    owner_session_hash = models.CharField(
        'Хеш сессии владельца',
        max_length=128,
        blank=True,
    )
    lease_token = models.UUIDField(
        'Токен владения',
        null=True,
        blank=True,
    )
    fencing_revision = models.PositiveBigIntegerField(
        'Ревизия владения',
        default=0,
    )
    acquired_at = models.DateTimeField(
        'Управление получено',
        null=True,
        blank=True,
    )
    heartbeat_at = models.DateTimeField(
        'Последнее подтверждение активности',
        null=True,
        blank=True,
    )
    expires_at = models.DateTimeField(
        'Срок владения истекает',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Изменено', auto_now=True)

    class Meta:
        verbose_name = 'Право управления расселением'
        verbose_name_plural = 'Права управления расселением'
        ordering = ['scope']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        owner_access__isnull=True,
                        owner_session_hash='',
                        lease_token__isnull=True,
                        acquired_at__isnull=True,
                        heartbeat_at__isnull=True,
                        expires_at__isnull=True,
                    )
                    | (
                        models.Q(
                            owner_access__isnull=False,
                            lease_token__isnull=False,
                            acquired_at__isnull=False,
                            heartbeat_at__isnull=False,
                            expires_at__isnull=False,
                        )
                        & ~models.Q(owner_session_hash='')
                    )
                ),
                name='settlement_control_lease_state_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(owner_access__isnull=True)
                    | (
                        models.Q(heartbeat_at__gte=models.F('acquired_at'))
                        & models.Q(expires_at__gt=models.F('heartbeat_at'))
                    )
                ),
                name='settlement_control_lease_time_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(fencing_revision__gte=0),
                name='settlement_control_revision_nonnegative',
            ),
        ]

    def clean(self):
        super().clean()
        if not self.owner_access_id:
            return

        access_model = self._meta.get_field('owner_access').remote_field.model
        owner_access = (
            access_model.objects.select_related('role')
            .filter(pk=self.owner_access_id)
            .first()
        )
        if owner_access is None:
            return

        if (
            not owner_access.is_active
            or owner_access.status != owner_access.Status.ACTIVATED
            or owner_access.role.code not in self.ALLOWED_OWNER_ROLE_CODES
        ):
            raise ValidationError({
                'owner_access': (
                    'Владелец должен иметь действующий доступ '
                    'с ролью settlement_clerk либо admin.'
                ),
            })

    def __str__(self):
        return self.scope


class SettlementControlEventQuerySet(models.QuerySet):
    IMMUTABLE_CODE = 'settlement.control.event_immutable'
    IMMUTABLE_MESSAGE = 'События управления расселением неизменяемы.'

    def _raise_immutable(self):
        raise ValidationError(
            self.IMMUTABLE_MESSAGE,
            code=self.IMMUTABLE_CODE,
        )

    def update(self, **kwargs):
        self._raise_immutable()

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        self._raise_immutable()

    def bulk_update(self, objs, fields, batch_size=None):
        self._raise_immutable()

    def delete(self):
        self._raise_immutable()


class SettlementControlEvent(models.Model):
    IMMUTABLE_CODE = SettlementControlEventQuerySet.IMMUTABLE_CODE
    IMMUTABLE_MESSAGE = SettlementControlEventQuerySet.IMMUTABLE_MESSAGE

    class EventType(models.TextChoices):
        ACQUIRED = 'ACQUIRED', 'Управление получено'
        RELEASED = 'RELEASED', 'Управление освобождено'
        EXPIRED = 'EXPIRED', 'Срок управления истёк'
        TAKEN_OVER = 'TAKEN_OVER', 'Управление перехвачено'

    objects = SettlementControlEventQuerySet.as_manager()

    event_type = models.CharField(
        'Тип события',
        max_length=16,
        choices=EventType.choices,
    )
    scope = models.CharField('Контур управления', max_length=64)
    actor_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Доступ инициатора',
        on_delete=models.PROTECT,
        related_name='settlement_control_events_as_actor',
        null=True,
        blank=True,
    )
    previous_owner_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Прежний владелец',
        on_delete=models.PROTECT,
        related_name='settlement_control_events_as_previous_owner',
        null=True,
        blank=True,
    )
    new_owner_access = models.ForeignKey(
        'users.EmployeeAccess',
        verbose_name='Новый владелец',
        on_delete=models.PROTECT,
        related_name='settlement_control_events_as_new_owner',
        null=True,
        blank=True,
    )
    reason = models.TextField('Причина', blank=True)
    occurred_at = models.DateTimeField('Произошло', default=timezone.now)
    source = models.CharField('Источник', max_length=64)
    previous_fencing_revision = models.PositiveBigIntegerField(
        'Предыдущая ревизия',
    )
    new_fencing_revision = models.PositiveBigIntegerField(
        'Новая ревизия',
    )
    session_metadata = models.JSONField(
        'Безопасные метаданные сессии',
        default=dict,
        blank=True,
    )

    class Meta:
        verbose_name = 'Событие управления расселением'
        verbose_name_plural = 'События управления расселением'
        ordering = ['-occurred_at', '-id']
        indexes = [
            models.Index(
                fields=['scope', 'occurred_at'],
                name='settle_event_scope_time_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(event_type__in=[
                    'ACQUIRED',
                    'RELEASED',
                    'EXPIRED',
                    'TAKEN_OVER',
                ]),
                name='settlement_control_event_type_valid',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    new_fencing_revision__gt=models.F(
                        'previous_fencing_revision',
                    ),
                ),
                name='settlement_control_event_revision_increases',
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(event_type='TAKEN_OVER')
                    | ~models.Q(reason='')
                ),
                name='settlement_takeover_reason_required',
            ),
        ]

    def __str__(self):
        return f'{self.scope}: {self.event_type} @ {self.occurred_at:%Y-%m-%d %H:%M:%S}'

    @classmethod
    def _immutability_error(cls):
        return ValidationError(
            cls.IMMUTABLE_MESSAGE,
            code=cls.IMMUTABLE_CODE,
        )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise self._immutability_error()
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)
        if (
            self.pk is not None
            and type(self)._base_manager.using(using).filter(pk=self.pk).exists()
        ):
            raise self._immutability_error()
        if kwargs.get('force_update'):
            raise self._immutability_error()
        kwargs['force_insert'] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise self._immutability_error()
