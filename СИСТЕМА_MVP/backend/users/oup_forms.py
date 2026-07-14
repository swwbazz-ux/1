from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .forms import is_valid_russian_mobile_phone, normalize_phone, optimize_employee_photo
from .models import Employee


class OupEmployeeForm(forms.ModelForm):
    class Meta:
        model = Employee
        fields = [
            'full_name',
            'birth_date',
            'personnel_number',
            'phone',
            'photo',
            'position',
            'department',
            'work_category',
            'hired_at',
            'rotation',
            'comment',
        ]
        labels = {
            'full_name': 'ФИО',
            'birth_date': 'Дата рождения',
            'personnel_number': 'Табельный номер',
            'phone': 'Мобильный телефон',
            'photo': 'Фото сотрудника',
            'position': 'Должность',
            'department': 'Подразделение',
            'work_category': 'Рабочая категория',
            'hired_at': 'Дата приема',
            'rotation': 'Вахта / график',
            'comment': 'Комментарий ОУП',
        }
        widgets = {
            'birth_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
            'hired_at': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
            'photo': forms.FileInput(attrs={
                'accept': 'image/jpeg,image/png,image/webp',
                'class': 'employee-photo-input',
                'data-oup-photo-input': '1',
                'aria-label': 'Выбрать фото сотрудника',
            }),
            'comment': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': 'Только рабочее примечание без паспортных данных',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        required_fields = (
            'full_name',
            'personnel_number',
            'phone',
            'position',
            'department',
            'work_category',
            'hired_at',
            'rotation',
        )
        for field_name in required_fields:
            self.fields[field_name].required = True
        self.fields['work_category'].help_text = (
            'Определяет доступность для расстановки по технике и не выдает системный доступ.'
        )
        self.fields['photo'].help_text = 'JPG, PNG или WEBP до 5 МБ. Фото будет уменьшено автоматически.'

    def clean_full_name(self):
        value = ' '.join(self.cleaned_data['full_name'].split())
        if len(value.split()) < 2:
            raise ValidationError('Укажите фамилию и имя сотрудника.')
        return value

    def clean_personnel_number(self):
        value = self.cleaned_data['personnel_number'].strip()
        queryset = Employee.objects.filter(personnel_number__iexact=value)
        if self.instance and self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError('Сотрудник с таким табельным номером уже существует.')
        return value

    def clean_phone(self):
        value = self.cleaned_data['phone'].strip()
        if not is_valid_russian_mobile_phone(value):
            raise ValidationError('Укажите российский мобильный номер в формате +7 900 000-00-00.')
        digits = normalize_phone(value)
        return f'+{digits}'

    def clean_photo(self):
        return optimize_employee_photo(self.cleaned_data.get('photo'))

    def clean_hired_at(self):
        value = self.cleaned_data['hired_at']
        if value and value > timezone.localdate():
            raise ValidationError('Дата приема не может быть позже сегодняшней даты.')
        return value

    def clean(self):
        cleaned_data = super().clean()
        if not self.instance.pk:
            return cleaned_data

        work_category = cleaned_data.get('work_category')
        previous_category = (
            Employee.objects.filter(pk=self.instance.pk)
            .values_list('work_category', flat=True)
            .first()
        )
        if not work_category or previous_category == work_category:
            return cleaned_data

        from .oup_services import employee_work_category_blockers

        blockers = employee_work_category_blockers(self.instance)
        if blockers:
            self.add_error(
                'work_category',
                'Сначала освободите сотрудника от рабочих операций: ' + '; '.join(blockers) + '.',
            )
        return cleaned_data


class OupDismissEmployeeForm(forms.Form):
    dismissed_at = forms.DateField(
        label='Дата увольнения',
        initial=timezone.localdate,
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
    )
    reason = forms.CharField(
        label='Комментарий',
        required=False,
        max_length=255,
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': 'Необязательно. Например: данные об увольнении получены из 1С',
        }),
    )

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee

    def clean_dismissed_at(self):
        value = self.cleaned_data['dismissed_at']
        if value > timezone.localdate():
            raise ValidationError('Будущее увольнение в этой версии не поддерживается.')
        if self.employee and self.employee.hired_at and value < self.employee.hired_at:
            raise ValidationError('Дата увольнения не может быть раньше даты приема.')
        return value
