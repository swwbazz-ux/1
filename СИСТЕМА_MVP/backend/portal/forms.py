from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from users.models import Employee

from .models import (
    LeadershipMessage,
    MaterialSuggestion,
    Poll,
    PortalStaffPermission,
    Publication,
)


class PortalLoginForm(forms.Form):
    phone = forms.CharField(
        label='Номер телефона',
        max_length=32,
        widget=forms.TextInput(attrs={'autocomplete': 'tel', 'inputmode': 'tel', 'placeholder': '+7 900 000-00-00'}),
    )
    access_code = forms.CharField(
        label='Рабочий PIN',
        min_length=6,
        max_length=6,
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password', 'inputmode': 'numeric', 'placeholder': '6 цифр'}),
    )

    def clean_access_code(self):
        value = self.cleaned_data['access_code'].strip()
        if not value.isdigit():
            raise ValidationError('PIN должен состоять из шести цифр.')
        return value


class PublicationForm(forms.ModelForm):
    class Meta:
        model = Publication
        fields = (
            'title',
            'publication_type',
            'summary',
            'body',
            'cover_image',
            'visibility',
            'audience',
            'target_work_category',
            'target_employee',
            'subject_employee',
            'is_mandatory',
            'allow_reactions',
            'pin_to_dashboard',
            'public_consent_confirmed',
        )
        widgets = {
            'summary': forms.Textarea(attrs={'rows': 3}),
            'body': forms.Textarea(attrs={'rows': 12}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        employees = Employee.objects.filter(is_active=True, status=Employee.Status.ACTIVE).order_by('full_name')
        self.fields['target_employee'].queryset = employees
        self.fields['subject_employee'].queryset = employees


class PollForm(forms.ModelForm):
    options_text = forms.CharField(
        label='Варианты ответа',
        help_text='Каждый вариант — с новой строки. От 2 до 6 вариантов.',
        widget=forms.Textarea(attrs={'rows': 6, 'placeholder': 'Первый вариант\nВторой вариант'}),
    )

    class Meta:
        model = Poll
        fields = (
            'title',
            'description',
            'is_anonymous',
            'audience',
            'target_work_category',
            'target_employee',
            'opens_at',
            'closes_at',
            'results_published',
        )
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'opens_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'closes_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['target_employee'].queryset = Employee.objects.filter(
            is_active=True,
            status=Employee.Status.ACTIVE,
        ).order_by('full_name')
        if self.instance and self.instance.pk:
            self.fields['options_text'].initial = '\n'.join(self.instance.options.values_list('text', flat=True))
            if self.instance.status != Poll.Status.DRAFT:
                self.fields['is_anonymous'].disabled = True
                self.fields['options_text'].disabled = True
        if not self.instance.pk or self.instance.status != Poll.Status.CLOSED:
            self.fields['results_published'].disabled = True

    def clean_options_text(self):
        options = [line.strip() for line in self.cleaned_data['options_text'].splitlines() if line.strip()]
        if len(options) < 2 or len(options) > 6:
            raise ValidationError('Укажите от 2 до 6 вариантов ответа.')
        if len({option.casefold() for option in options}) != len(options):
            raise ValidationError('Варианты ответа не должны повторяться.')
        if self.instance.pk and (
            self.instance.status != Poll.Status.DRAFT or self.instance.votes.exists()
        ):
            current = list(self.instance.options.values_list('text', flat=True))
            if options != current:
                raise ValidationError('Нельзя менять варианты после открытия опроса.')
        return options

    def save(self, commit=True):
        poll = super().save(commit=commit)
        if commit and poll.status == Poll.Status.DRAFT and not poll.votes.exists():
            poll.options.all().delete()
            poll.options.bulk_create([
                poll.options.model(poll=poll, text=text, order=index)
                for index, text in enumerate(self.cleaned_data['options_text'])
            ])
        return poll


class PollVoteForm(forms.Form):
    option = forms.ChoiceField(label='Выберите один вариант', widget=forms.RadioSelect)

    def __init__(self, poll, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.poll = poll
        self.fields['option'].choices = [(str(option.pk), option.text) for option in poll.options.all()]

    def clean_option(self):
        option_id = self.cleaned_data['option']
        option = self.poll.options.filter(pk=option_id).first()
        if not option:
            raise ValidationError('Выбранный вариант больше недоступен.')
        return option


class LeadershipMessageForm(forms.ModelForm):
    class Meta:
        model = LeadershipMessage
        fields = ('text', 'photo', 'is_anonymous')
        labels = {
            'text': 'Что вы хотите сообщить?',
            'photo': 'Добавить фотографию',
            'is_anonymous': 'Отправить без имени',
        }
        widgets = {'text': forms.Textarea(attrs={'rows': 7, 'placeholder': 'Опишите вопрос или предложение своими словами'})}


class MaterialSuggestionForm(forms.ModelForm):
    class Meta:
        model = MaterialSuggestion
        fields = ('title', 'text', 'photo')
        labels = {
            'title': 'О чём стоит рассказать?',
            'text': 'Расскажите подробнее',
            'photo': 'Добавить фотографию',
        }
        widgets = {'text': forms.Textarea(attrs={'rows': 7})}


class PortalStaffPermissionForm(forms.ModelForm):
    class Meta:
        model = PortalStaffPermission
        fields = ('employee', 'can_author', 'can_edit', 'can_publish', 'receives_feedback', 'is_active')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        employees = Employee.objects.filter(is_active=True, status=Employee.Status.ACTIVE).order_by('full_name')
        if not self.instance.pk:
            employees = employees.exclude(portal_staff_permission__isnull=False)
        self.fields['employee'].queryset = employees


class FeedbackManagementForm(forms.ModelForm):
    class Meta:
        model = LeadershipMessage
        fields = ('status', 'response')
        labels = {'response': 'Ответ сотруднику'}
        widgets = {'response': forms.Textarea(attrs={'rows': 7})}

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('status') == LeadershipMessage.Status.ANSWERED and not cleaned_data.get('response', '').strip():
            self.add_error('response', 'Чтобы поставить статус «Есть ответ», напишите ответ сотруднику.')
        return cleaned_data
