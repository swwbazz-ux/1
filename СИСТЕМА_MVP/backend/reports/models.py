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
