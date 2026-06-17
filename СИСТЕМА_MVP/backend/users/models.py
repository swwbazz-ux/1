from django.db import models


class Employee(models.Model):
    full_name = models.CharField('ФИО', max_length=255)
    personnel_number = models.CharField('Табельный номер', max_length=64, blank=True)
    phone = models.CharField('Телефон', max_length=32, blank=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        verbose_name = 'Сотрудник'
        verbose_name_plural = 'Сотрудники'
        ordering = ['full_name']

    def __str__(self):
        return self.full_name


class Role(models.Model):
    code = models.SlugField('Код роли', max_length=64, unique=True)
    name = models.CharField('Название роли', max_length=128)
    description = models.TextField('Описание', blank=True)
    is_active = models.BooleanField('Активна', default=True)

    class Meta:
        verbose_name = 'Роль'
        verbose_name_plural = 'Роли'
        ordering = ['name']

    def __str__(self):
        return self.name


class EmployeeAccess(models.Model):
    employee = models.ForeignKey(Employee, verbose_name='Сотрудник', on_delete=models.CASCADE, related_name='accesses')
    role = models.ForeignKey(Role, verbose_name='Роль', on_delete=models.PROTECT, related_name='accesses')
    access_code = models.CharField('Код доступа', max_length=128, unique=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    deactivated_at = models.DateTimeField('Отключен', null=True, blank=True)

    class Meta:
        verbose_name = 'Доступ сотрудника'
        verbose_name_plural = 'Доступы сотрудников'
        ordering = ['employee__full_name', 'role__name']

    def __str__(self):
        return f'{self.employee} - {self.role}'


class DriverPrimaryRegistration(models.Model):
    employee = models.OneToOneField(Employee, verbose_name='Водитель', on_delete=models.CASCADE, related_name='driver_registration')
    shift_type = models.CharField('Смена', max_length=16, choices=[('day', 'Дневная'), ('night', 'Ночная')])
    truck = models.ForeignKey('references.Equipment', verbose_name='Самосвал', on_delete=models.PROTECT)
    dormitory_section = models.ForeignKey('references.DormitorySection', verbose_name='Секция проживания', on_delete=models.PROTECT)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Первичная регистрация водителя'
        verbose_name_plural = 'Первичные регистрации водителей'
        ordering = ['employee__full_name']

    def __str__(self):
        return f'{self.employee} / {self.truck} / {self.get_shift_type_display()}'

# Create your models here.
