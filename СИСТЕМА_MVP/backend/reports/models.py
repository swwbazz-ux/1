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

# Create your models here.
