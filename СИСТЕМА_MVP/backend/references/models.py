from django.db import models


class EquipmentType(models.Model):
    name = models.CharField('Вид техники', max_length=128, unique=True)
    is_active = models.BooleanField('Активен', default=True)

    class Meta:
        verbose_name = 'Вид техники'
        verbose_name_plural = 'Виды техники'
        ordering = ['name']

    def __str__(self):
        return self.name


class EquipmentModel(models.Model):
    equipment_type = models.ForeignKey(EquipmentType, verbose_name='Вид техники', on_delete=models.PROTECT)
    name = models.CharField('Модель', max_length=128)
    payload_tons = models.DecimalField('Грузоподъемность, т', max_digits=10, decimal_places=2, null=True, blank=True)
    body_volume_m3 = models.DecimalField('Объем кузова/ковша, м3', max_digits=10, decimal_places=2, null=True, blank=True)
    fuel_capacity_limit_l = models.PositiveIntegerField(
        'Допустимый объем топлива, л',
        null=True,
        blank=True,
        help_text='Максимальное фактическое показание остатка топлива для открытия и закрытия смены.',
    )
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Модель техники'
        verbose_name_plural = 'Модели техники'
        ordering = ['equipment_type__name', 'name']
        unique_together = [('equipment_type', 'name')]

    def __str__(self):
        return self.name


class Equipment(models.Model):
    equipment_type = models.ForeignKey(EquipmentType, verbose_name='Вид техники', on_delete=models.PROTECT)
    model = models.ForeignKey(EquipmentModel, verbose_name='Модель', on_delete=models.PROTECT, null=True, blank=True)
    garage_number = models.CharField('Гаражный номер', max_length=64, unique=True)
    vin = models.CharField('VIN/серийный номер', max_length=128, blank=True)
    is_own = models.BooleanField('Своя техника', default=True)
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Техника'
        verbose_name_plural = 'Техника'
        ordering = ['equipment_type__name', 'garage_number']

    def __str__(self):
        return f'{self.equipment_type} {self.garage_number}'


class EquipmentState(models.Model):
    class ColorGroup(models.TextChoices):
        GRAY = 'gray', 'Серый'
        YELLOW = 'yellow', 'Желтый'
        GREEN = 'green', 'Зеленый'
        BLUE = 'blue', 'Синий'
        ORANGE = 'orange', 'Оранжевый'
        RED = 'red', 'Красный'

    class SemanticGroup(models.TextChoices):
        AVAILABILITY = 'availability', 'Доступность'
        ASSIGNMENT = 'assignment', 'Назначение'
        OPERATION = 'operation', 'Рабочая операция'
        TECHNICAL = 'technical', 'Техническое состояние'
        SYSTEM = 'system', 'Системное состояние'
        TERMINAL = 'terminal', 'Выведено из работы'

    code = models.SlugField('Код состояния', max_length=64, unique=True)
    name = models.CharField('Название', max_length=128)
    short_label = models.CharField('Короткая подпись', max_length=64, blank=True)
    color_group = models.CharField('Цветовая группа', max_length=16, choices=ColorGroup.choices)
    semantic_group = models.CharField('Смысловая группа', max_length=32, choices=SemanticGroup.choices)
    priority = models.PositiveIntegerField('Приоритет отображения', default=100)
    allows_assignment = models.BooleanField('Можно назначать', default=False)
    allows_drag = models.BooleanField('Можно перетаскивать', default=False)
    blocks_operation = models.BooleanField('Блокирует работу', default=False)
    requires_attention = models.BooleanField('Требует внимания', default=False)
    requires_reason = models.BooleanField('Требует причину', default=False)
    is_terminal = models.BooleanField('Финальное состояние', default=False)
    is_active = models.BooleanField('Активно', default=True)
    description = models.TextField('Описание', blank=True)

    class Meta:
        verbose_name = 'Состояние техники'
        verbose_name_plural = 'Состояния техники'
        ordering = ['priority', 'name']

    @property
    def label(self):
        return self.short_label or self.name

    @property
    def css_class(self):
        return f'status-{self.color_group}'

    def __str__(self):
        return self.name


class RockType(models.Model):
    name = models.CharField('Порода/груз', max_length=128, unique=True)
    density = models.DecimalField('Плотность', max_digits=10, decimal_places=4, null=True, blank=True)
    loosening_factor = models.DecimalField('Коэффициент разрыхления', max_digits=10, decimal_places=4, null=True, blank=True)
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Порода'
        verbose_name_plural = 'Породы'
        ordering = ['name']

    def __str__(self):
        return self.name


class DumpPoint(models.Model):
    name = models.CharField('Точка разгрузки', max_length=128, unique=True)
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Точка разгрузки'
        verbose_name_plural = 'Точки разгрузки'
        ordering = ['name']

    def __str__(self):
        return self.name


class TruckCapacityRule(models.Model):
    equipment_model = models.ForeignKey(EquipmentModel, verbose_name='Модель самосвала', on_delete=models.CASCADE)
    rock_type = models.ForeignKey(RockType, verbose_name='Порода', on_delete=models.PROTECT)
    volume_m3 = models.DecimalField('Кубатура рейса, м3', max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = 'Правило кубатуры самосвала'
        verbose_name_plural = 'Правила кубатуры самосвалов'
        unique_together = [('equipment_model', 'rock_type')]

    def __str__(self):
        return f'{self.equipment_model} / {self.rock_type}: {self.volume_m3}'


class Dormitory(models.Model):
    number = models.CharField('Номер общежития', max_length=16, unique=True)
    is_active = models.BooleanField('Активно', default=True)

    class Meta:
        verbose_name = 'Общежитие'
        verbose_name_plural = 'Общежития'
        ordering = ['number']

    def __str__(self):
        return f'Общежитие {self.number}'


class DormitoryBlock(models.Model):
    dormitory = models.ForeignKey(Dormitory, verbose_name='Общежитие', on_delete=models.CASCADE, related_name='blocks')
    name = models.CharField('Блок', max_length=64)

    class Meta:
        verbose_name = 'Блок общежития'
        verbose_name_plural = 'Блоки общежитий'
        unique_together = [('dormitory', 'name')]
        ordering = ['dormitory__number', 'name']

    def __str__(self):
        return f'{self.dormitory}, блок {self.name}'


class DormitorySection(models.Model):
    block = models.ForeignKey(DormitoryBlock, verbose_name='Блок', on_delete=models.CASCADE, related_name='sections')
    name = models.CharField('Секция', max_length=16)
    day_capacity = models.PositiveIntegerField('Мест для дневной смены', default=3)
    night_capacity = models.PositiveIntegerField('Мест для ночной смены', default=3)

    class Meta:
        verbose_name = 'Секция общежития'
        verbose_name_plural = 'Секции общежитий'
        unique_together = [('block', 'name')]
        ordering = ['block__dormitory__number', 'block__name', 'name']

    def __str__(self):
        return f'{self.block}, секция {self.name}'

# Create your models here.
