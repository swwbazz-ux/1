from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .forms import EmployeeCardForm
from .models import Employee, Role


class OupAccessRoleForm(forms.Form):
    role = forms.ModelChoiceField(
        label='Рабочая роль',
        queryset=Role.objects.none(),
        empty_label='Выберите роль',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['role'].queryset = (
            Role.objects.filter(is_active=True)
            .exclude(code='admin')
            .order_by('name')
        )


class OupEmployeeForm(EmployeeCardForm):
    issue_access = forms.BooleanField(
        label='Выдать доступ в систему после создания',
        required=False,
    )
    access_role = forms.ModelChoiceField(
        label='Роль доступа',
        required=False,
        queryset=Role.objects.none(),
        empty_label='Выберите роль',
    )
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['access_role'].queryset = (
            Role.objects.filter(is_active=True)
            .exclude(code='admin')
            .order_by('name')
        )
        self.fields['status'].disabled = True
        self.fields['dismissed_at'].disabled = True
        self.fields['issue_access'].widget.attrs['form'] = 'employee-card-form'
        self.fields['access_role'].widget.attrs['form'] = 'employee-card-form'
        self.fields['work_category'].help_text = (
            'Определяет доступность для расстановки по технике и не выдает системный доступ.'
        )
        self.fields['comment'].widget.attrs['placeholder'] = 'Рабочее примечание по сотруднику'

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('issue_access') and not cleaned_data.get('access_role'):
            self.add_error('access_role', 'Выберите роль, для которой нужно выдать доступ.')
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
