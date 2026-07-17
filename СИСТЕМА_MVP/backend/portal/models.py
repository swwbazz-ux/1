from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from .storage import portal_private_storage


def _unique_publication_slug(instance):
    base = slugify(instance.title, allow_unicode=True)[:150] or 'material'
    slug = base
    suffix = 2
    queryset = Publication.objects.exclude(pk=instance.pk)
    while queryset.filter(slug=slug).exists():
        tail = f'-{suffix}'
        slug = f'{base[: 160 - len(tail)]}{tail}'
        suffix += 1
    return slug


def validate_portal_image_size(upload):
    if upload.size > 10 * 1024 * 1024:
        raise ValidationError('Размер изображения не должен превышать 10 МБ.')


PORTAL_IMAGE_VALIDATORS = [
    FileExtensionValidator(allowed_extensions=['jpg', 'jpeg', 'png', 'webp']),
    validate_portal_image_size,
]


class Publication(models.Model):
    class Type(models.TextChoices):
        NEWS = 'news', 'Новость'
        ANNOUNCEMENT = 'announcement', 'Объявление'
        INTERVIEW = 'interview', 'Интервью'
        STORY = 'story', 'История сотрудника'
        ACHIEVEMENT = 'achievement', 'Достижение'
        BIRTHDAY = 'birthday', 'Поздравление'
        QNA = 'qna', 'Ответ руководства'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        REVIEW = 'review', 'На согласовании'
        PUBLISHED = 'published', 'Опубликовано'
        ARCHIVED = 'archived', 'В архиве'

    class Visibility(models.TextChoices):
        INTERNAL = 'internal', 'Только сотрудникам'
        PUBLIC = 'public', 'Только на открытом сайте'
        BOTH = 'both', 'На открытом сайте и сотрудникам'

    class Audience(models.TextChoices):
        ALL = 'all', 'Все сотрудники участка № 2'
        DAY_SHIFT = 'day_shift', 'Дневная смена'
        NIGHT_SHIFT = 'night_shift', 'Ночная смена'
        PROFESSION = 'profession', 'Выбранная профессия'
        EMPLOYEE = 'employee', 'Выбранный сотрудник'

    title = models.CharField('Заголовок', max_length=220)
    slug = models.SlugField('Адрес', max_length=170, unique=True, allow_unicode=True, blank=True)
    publication_type = models.CharField('Тип материала', max_length=24, choices=Type.choices, default=Type.NEWS)
    status = models.CharField('Статус', max_length=16, choices=Status.choices, default=Status.DRAFT)
    visibility = models.CharField(
        'Где показывать',
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.INTERNAL,
        help_text='Безопасное значение по умолчанию — только закрытая часть.',
    )
    audience = models.CharField('Кому внутри портала', max_length=24, choices=Audience.choices, default=Audience.ALL)
    target_work_category = models.CharField(
        'Профессия получателей',
        max_length=32,
        choices=(
            ('driver', 'Водители самосвалов'),
            ('excavator_operator', 'Машинисты экскаваторов'),
            ('other', 'Остальные сотрудники'),
        ),
        blank=True,
    )
    target_employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Сотрудник-получатель',
        on_delete=models.PROTECT,
        related_name='targeted_portal_publications',
        null=True,
        blank=True,
    )
    subject_employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Герой материала',
        on_delete=models.PROTECT,
        related_name='portal_stories',
        null=True,
        blank=True,
    )
    summary = models.TextField('Короткое описание', blank=True)
    body = models.TextField('Текст')
    cover_image = models.ImageField(
        'Обложка',
        upload_to='portal/publications/%Y/%m/',
        storage=portal_private_storage,
        validators=PORTAL_IMAGE_VALIDATORS,
        blank=True,
    )
    is_mandatory = models.BooleanField('Требуется отметка «Ознакомился»', default=False)
    allow_reactions = models.BooleanField('Разрешить реакцию «Спасибо»', default=True)
    pin_to_dashboard = models.BooleanField('Закрепить на главной портала', default=False)
    public_consent_confirmed = models.BooleanField(
        'Согласие сотрудника на открытую публикацию подтверждено',
        default=False,
    )
    author = models.ForeignKey(
        'users.Employee',
        verbose_name='Автор',
        on_delete=models.PROTECT,
        related_name='authored_portal_publications',
    )
    editor = models.ForeignKey(
        'users.Employee',
        verbose_name='Последний редактор',
        on_delete=models.PROTECT,
        related_name='edited_portal_publications',
        null=True,
        blank=True,
    )
    publisher = models.ForeignKey(
        'users.Employee',
        verbose_name='Опубликовал',
        on_delete=models.PROTECT,
        related_name='published_portal_publications',
        null=True,
        blank=True,
    )
    published_at = models.DateTimeField('Дата публикации', null=True, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Публикация портала'
        verbose_name_plural = 'Публикации портала'
        ordering = ['-pin_to_dashboard', '-published_at', '-created_at']
        indexes = [
            models.Index(fields=['status', 'visibility', 'published_at']),
            models.Index(fields=['publication_type', 'status']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(subject_employee__isnull=True)
                    | models.Q(visibility='internal')
                    | models.Q(public_consent_confirmed=True)
                ),
                name='portal_public_story_requires_consent',
            ),
            models.CheckConstraint(
                condition=models.Q(is_mandatory=False) | ~models.Q(visibility='public'),
                name='portal_mandatory_requires_internal_visibility',
            ),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        errors = {}
        if self.audience == self.Audience.PROFESSION and not self.target_work_category:
            errors['target_work_category'] = 'Выберите профессию получателей.'
        if self.audience == self.Audience.EMPLOYEE and not self.target_employee_id:
            errors['target_employee'] = 'Выберите сотрудника-получателя.'
        if (
            self.subject_employee_id
            and self.visibility in {self.Visibility.PUBLIC, self.Visibility.BOTH}
            and not self.public_consent_confirmed
        ):
            errors['public_consent_confirmed'] = 'Для открытой истории сотрудника нужно подтвердить его согласие.'
        if self.is_mandatory and self.visibility == self.Visibility.PUBLIC:
            errors['is_mandatory'] = 'Обязательное ознакомление доступно только в закрытой части.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_publication_slug(self)
        super().save(*args, **kwargs)

    @property
    def is_public(self):
        return self.visibility in {self.Visibility.PUBLIC, self.Visibility.BOTH}

    @property
    def is_internal(self):
        return self.visibility in {self.Visibility.INTERNAL, self.Visibility.BOTH}


class PublicationImage(models.Model):
    publication = models.ForeignKey(Publication, verbose_name='Публикация', on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(
        'Фотография',
        upload_to='portal/publications/%Y/%m/',
        storage=portal_private_storage,
        validators=PORTAL_IMAGE_VALIDATORS,
    )
    caption = models.CharField('Подпись', max_length=220, blank=True)
    order = models.PositiveSmallIntegerField('Порядок', default=0)

    class Meta:
        verbose_name = 'Фотография публикации'
        verbose_name_plural = 'Фотографии публикации'
        ordering = ['order', 'id']

    def __str__(self):
        return self.caption or f'Фото к публикации {self.publication_id}'


class PublicationAcknowledgement(models.Model):
    publication = models.ForeignKey(Publication, verbose_name='Публикация', on_delete=models.CASCADE, related_name='acknowledgements')
    employee = models.ForeignKey('users.Employee', verbose_name='Сотрудник', on_delete=models.CASCADE, related_name='portal_acknowledgements')
    acknowledged_at = models.DateTimeField('Ознакомился', auto_now_add=True)

    class Meta:
        verbose_name = 'Ознакомление с публикацией'
        verbose_name_plural = 'Ознакомления с публикациями'
        constraints = [
            models.UniqueConstraint(fields=['publication', 'employee'], name='unique_portal_publication_acknowledgement'),
        ]


class PublicationReaction(models.Model):
    publication = models.ForeignKey(Publication, verbose_name='Публикация', on_delete=models.CASCADE, related_name='reactions')
    employee = models.ForeignKey('users.Employee', verbose_name='Сотрудник', on_delete=models.CASCADE, related_name='portal_reactions')
    created_at = models.DateTimeField('Поставлено', auto_now_add=True)

    class Meta:
        verbose_name = 'Реакция на публикацию'
        verbose_name_plural = 'Реакции на публикации'
        constraints = [
            models.UniqueConstraint(fields=['publication', 'employee'], name='unique_portal_publication_reaction'),
        ]


class MaterialSuggestion(models.Model):
    class Status(models.TextChoices):
        NEW = 'new', 'Новое'
        ACCEPTED = 'accepted', 'Принято в работу'
        REJECTED = 'rejected', 'Отклонено'

    employee = models.ForeignKey('users.Employee', verbose_name='Предложил', on_delete=models.PROTECT, related_name='portal_suggestions')
    title = models.CharField('Тема', max_length=220)
    text = models.TextField('Описание')
    photo = models.ImageField(
        'Фотография',
        upload_to='portal/suggestions/%Y/%m/',
        storage=portal_private_storage,
        validators=PORTAL_IMAGE_VALIDATORS,
        blank=True,
    )
    status = models.CharField('Статус', max_length=16, choices=Status.choices, default=Status.NEW)
    reviewed_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Рассмотрел',
        on_delete=models.PROTECT,
        related_name='reviewed_portal_suggestions',
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField('Рассмотрено', null=True, blank=True)
    created_at = models.DateTimeField('Отправлено', auto_now_add=True)

    class Meta:
        verbose_name = 'Предложение материала'
        verbose_name_plural = 'Предложения материалов'
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class Poll(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Черновик'
        OPEN = 'open', 'Открыт'
        CLOSED = 'closed', 'Завершён'

    title = models.CharField('Вопрос', max_length=220)
    description = models.TextField('Пояснение', blank=True)
    status = models.CharField('Статус', max_length=16, choices=Status.choices, default=Status.DRAFT)
    is_anonymous = models.BooleanField('Анонимный опрос', default=True)
    audience = models.CharField('Кому', max_length=24, choices=Publication.Audience.choices, default=Publication.Audience.ALL)
    target_work_category = models.CharField(
        'Профессия получателей',
        max_length=32,
        choices=(
            ('driver', 'Водители самосвалов'),
            ('excavator_operator', 'Машинисты экскаваторов'),
            ('other', 'Остальные сотрудники'),
        ),
        blank=True,
    )
    target_employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Сотрудник-получатель',
        on_delete=models.PROTECT,
        related_name='targeted_portal_polls',
        null=True,
        blank=True,
    )
    results_published = models.BooleanField('Показать итог сотрудникам', default=False)
    opens_at = models.DateTimeField('Начало', null=True, blank=True)
    closes_at = models.DateTimeField('Завершение', null=True, blank=True)
    author = models.ForeignKey('users.Employee', verbose_name='Автор', on_delete=models.PROTECT, related_name='authored_portal_polls')
    publisher = models.ForeignKey(
        'users.Employee',
        verbose_name='Открыл опрос',
        on_delete=models.PROTECT,
        related_name='published_portal_polls',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)

    class Meta:
        verbose_name = 'Опрос'
        verbose_name_plural = 'Опросы'
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    def clean(self):
        errors = {}
        if self.audience == Publication.Audience.PROFESSION and not self.target_work_category:
            errors['target_work_category'] = 'Выберите профессию получателей.'
        if self.audience == Publication.Audience.EMPLOYEE and not self.target_employee_id:
            errors['target_employee'] = 'Выберите сотрудника-получателя.'
        if self.pk:
            original = Poll.objects.filter(pk=self.pk).values('is_anonymous', 'status').first()
            if (
                original
                and original['is_anonymous'] != self.is_anonymous
                and (original['status'] != self.Status.DRAFT or self.votes.exists())
            ):
                errors['is_anonymous'] = 'Нельзя менять анонимность после открытия опроса.'
        if errors:
            raise ValidationError(errors)

    @property
    def accepts_votes(self):
        now = timezone.now()
        return (
            self.status == self.Status.OPEN
            and (not self.opens_at or self.opens_at <= now)
            and (not self.closes_at or self.closes_at > now)
        )


class PollOption(models.Model):
    poll = models.ForeignKey(Poll, verbose_name='Опрос', on_delete=models.CASCADE, related_name='options')
    text = models.CharField('Вариант ответа', max_length=220)
    order = models.PositiveSmallIntegerField('Порядок', default=0)

    class Meta:
        verbose_name = 'Вариант ответа'
        verbose_name_plural = 'Варианты ответа'
        ordering = ['order', 'id']

    def __str__(self):
        return self.text


class PollVote(models.Model):
    poll = models.ForeignKey(Poll, verbose_name='Опрос', on_delete=models.CASCADE, related_name='votes')
    option = models.ForeignKey(PollOption, verbose_name='Ответ', on_delete=models.PROTECT, related_name='votes')
    voter_key = models.CharField('Анонимный ключ участника', max_length=64)
    employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Сотрудник (только для именного опроса)',
        on_delete=models.PROTECT,
        related_name='portal_poll_votes',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField('Проголосовал', auto_now_add=True)

    class Meta:
        verbose_name = 'Голос в опросе'
        verbose_name_plural = 'Голоса в опросах'
        constraints = [
            models.UniqueConstraint(fields=['poll', 'voter_key'], name='unique_portal_poll_voter'),
        ]


class LeadershipMessage(models.Model):
    class Status(models.TextChoices):
        SENT = 'sent', 'Отправлено'
        IN_PROGRESS = 'in_progress', 'В работе'
        ANSWERED = 'answered', 'Есть ответ'
        CLOSED = 'closed', 'Закрыто'

    employee = models.ForeignKey(
        'users.Employee',
        verbose_name='Автор',
        on_delete=models.PROTECT,
        related_name='portal_leadership_messages',
    )
    is_anonymous = models.BooleanField('Отправлено без имени', default=False)
    text = models.TextField('Сообщение')
    photo = models.ImageField(
        'Фотография',
        upload_to='portal/messages/%Y/%m/',
        storage=portal_private_storage,
        validators=PORTAL_IMAGE_VALIDATORS,
        blank=True,
    )
    status = models.CharField('Статус', max_length=16, choices=Status.choices, default=Status.SENT)
    response = models.TextField('Ответ руководства', blank=True)
    responded_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Ответил',
        on_delete=models.PROTECT,
        related_name='answered_portal_messages',
        null=True,
        blank=True,
    )
    responded_at = models.DateTimeField('Дата ответа', null=True, blank=True)
    created_at = models.DateTimeField('Отправлено', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Обращение руководству'
        verbose_name_plural = 'Обращения руководству'
        ordering = ['-created_at']

    def __str__(self):
        return f'Обращение №{self.pk or "новое"}'

    @property
    def recipient_author_label(self):
        return 'Анонимный сотрудник' if self.is_anonymous else self.employee.full_name


class PortalStaffPermission(models.Model):
    employee = models.OneToOneField(
        'users.Employee',
        verbose_name='Сотрудник',
        on_delete=models.CASCADE,
        related_name='portal_staff_permission',
    )
    can_author = models.BooleanField('Может создавать материалы', default=False)
    can_edit = models.BooleanField('Может редактировать материалы', default=False)
    can_publish = models.BooleanField('Может публиковать', default=False)
    receives_feedback = models.BooleanField('Получает обращения руководству', default=False)
    is_active = models.BooleanField('Права активны', default=True)
    assigned_by = models.ForeignKey(
        'users.Employee',
        verbose_name='Назначил',
        on_delete=models.PROTECT,
        related_name='assigned_portal_permissions',
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Редакционное право портала'
        verbose_name_plural = 'Редакционные права портала'
        ordering = ['employee__full_name']

    def __str__(self):
        return self.employee.full_name


class PortalAuditLog(models.Model):
    created_at = models.DateTimeField('Дата и время', auto_now_add=True)
    actor = models.ForeignKey(
        'users.Employee',
        verbose_name='Кто выполнил',
        on_delete=models.SET_NULL,
        related_name='portal_audit_actions',
        null=True,
        blank=True,
    )
    action = models.CharField('Действие', max_length=80)
    object_type = models.CharField('Тип объекта', max_length=80, blank=True)
    object_id = models.CharField('ID объекта', max_length=64, blank=True)
    object_repr = models.CharField('Объект', max_length=255, blank=True)
    details = models.JSONField('Подробности', default=dict, blank=True)

    class Meta:
        verbose_name = 'Запись журнала портала'
        verbose_name_plural = 'Журнал портала'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.created_at:%d.%m.%Y %H:%M} — {self.action}'
