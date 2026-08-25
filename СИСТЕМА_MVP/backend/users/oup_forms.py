from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .forms import EmployeeCardForm
from .models import Employee, Role


class OupAccessRoleForm(forms.Form):
    role = forms.ModelChoiceField(
        label='Доступ в приложение',
        queryset=Role.objects.none(),
        empty_label='Выберите доступ',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['role'].queryset = (
            Role.objects.filter(is_active=True)
            .exclude(code='admin')
            .order_by('name')
        )


class OupEmployeeForm(EmployeeCardForm):
    PROTECTED_EXISTING_PROFILE_FIELDS = (
        'work_schedule',
        'brigade_number',
        'watch_composition',
    )
    WATCH_PROFILE_EDIT_ERROR = (
        'Изменение графика, бригады и состава вахты действующего сотрудника '
        'выполняет Табельщик.'
    )

    issue_access = forms.BooleanField(
        label='Выдать доступ после создания',
        required=False,
        initial=True,
    )
    access_role = forms.ModelChoiceField(
        label='Доступ в приложение',
        required=False,
        queryset=Role.objects.none(),
        empty_label='Выберите доступ',
    )
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._is_existing_employee = bool(self.instance and self.instance.pk)
        self.fields['personnel_number'].widget = forms.TextInput(attrs={
            'autocomplete': 'off',
            'maxlength': '64',
            'placeholder': 'Например, 573',
        })
        self.fields['access_role'].queryset = (
            Role.objects.filter(is_active=True)
            .exclude(code='admin')
            .order_by('name')
        )
        self.fields['dismissed_at'].disabled = True
        self.fields['issue_access'].widget.attrs['form'] = 'employee-card-form'
        self.fields['access_role'].widget.attrs['form'] = 'employee-card-form'
        self.fields['base_specialization'].help_text = ''
        self.fields['comment'].widget.attrs['placeholder'] = 'Рабочее примечание по сотруднику'
        if self._is_existing_employee:
            for field_name in self.PROTECTED_EXISTING_PROFILE_FIELDS:
                self.fields[field_name].disabled = True

    def validate_protected_profile(self, employee):
        if not self._is_existing_employee or not self.is_bound:
            return

        expected_values = {
            'work_schedule': str(employee.work_schedule_id or ''),
            'brigade_number': str(employee.brigade_number or ''),
            'watch_composition': str(employee.watch_composition_id or ''),
        }
        for field_name, expected_value in expected_values.items():
            prefixed_name = self.add_prefix(field_name)
            if prefixed_name not in self.data:
                continue
            submitted_value = str(self.data.get(prefixed_name) or '').strip()
            if submitted_value != expected_value:
                raise ValidationError(
                    self.WATCH_PROFILE_EDIT_ERROR,
                    code='oup_watch_profile_forbidden',
                )

        rotation_name = self.add_prefix('rotation')
        if rotation_name in self.data:
            submitted_rotation = str(self.data.get(rotation_name) or '').strip()
            if submitted_rotation != str(employee.rotation or '').strip():
                raise ValidationError(
                    self.WATCH_PROFILE_EDIT_ERROR,
                    code='oup_watch_profile_forbidden',
                )

    def clean(self):
        cleaned_data = super().clean()
        specialization = cleaned_data.get('base_specialization')
        expected_role = (
            specialization.access_role
            if specialization and specialization.access_role_id
            else None
        )
        access_role = cleaned_data.get('access_role')
        if expected_role:
            if access_role and access_role.pk != expected_role.pk:
                self.add_error(
                    'access_role',
                    f'Для специализации «{specialization}» используется приложение «{expected_role}».',
                )
            cleaned_data['access_role'] = expected_role
        elif (
            not cleaned_data.get('personnel_position')
            and access_role
            and access_role.code in {'driver', 'excavator_operator'}
        ):
            cleaned_data['work_category'] = access_role.code
        if cleaned_data.get('issue_access') and not cleaned_data.get('access_role'):
            self.add_error('access_role', 'Выберите роль, для которой нужно выдать доступ.')
        return cleaned_data

    def save(self, commit=True):
        if not self._is_existing_employee:
            return super().save(commit=commit)

        protected_profile = {
            'work_schedule_id': self.instance.work_schedule_id,
            'brigade_number': self.instance.brigade_number,
            'watch_composition_id': self.instance.watch_composition_id,
            'rotation': self.instance.rotation,
        }
        employee = super().save(commit=False)
        for field_name, value in protected_profile.items():
            setattr(employee, field_name, value)
        if commit:
            employee.save()
            self.save_m2m()
        return employee


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


class TemporaryWorkTransferReviewForm(forms.Form):
    review_comment = forms.CharField(
        label='Комментарий ОУП',
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={'rows': 2}),
    )
