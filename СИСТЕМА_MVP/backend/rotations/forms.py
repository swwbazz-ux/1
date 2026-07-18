from django import forms
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from shifts.models import WatchPeriod

from .models import RotationCollectionCycle, RotationResponse


class RotationCycleCreateForm(forms.ModelForm):
    class Meta:
        model = RotationCollectionCycle
        fields = ('name', 'target_watch_period', 'response_deadline')
        widgets = {
            'response_deadline': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['target_watch_period'].queryset = WatchPeriod.objects.filter(
            is_active=True,
        ).order_by('-starts_on')
        self.fields['name'].widget.attrs.update(
            {'placeholder': 'Например: Перевахта августа 2026'}
        )

    def clean_response_deadline(self):
        value = self.cleaned_data['response_deadline']
        if value <= timezone.now():
            raise forms.ValidationError('Срок ответа должен быть позже текущего времени.')
        return value


class RotationResponseForm(forms.ModelForm):
    extension_start = forms.DateField(
        label='Продление с',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    extension_end = forms.DateField(
        label='Продление по',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )

    class Meta:
        model = RotationResponse
        fields = (
            'intent',
            'next_shift_type',
            'departure_on',
            'arrival_on',
            'route_text',
            'travel_mode',
            'transfer_mode',
            'transport_details',
            'comment',
        )
        widgets = {
            'departure_on': forms.DateInput(attrs={'type': 'date'}),
            'arrival_on': forms.DateInput(attrs={'type': 'date'}),
            'route_text': forms.Textarea(
                attrs={
                    'rows': 3,
                    'placeholder': 'Полный маршрут с городами, аэропортами или станциями и пересадками',
                }
            ),
            'transport_details': forms.Textarea(
                attrs={'rows': 2, 'placeholder': 'Рейс, поезд, автобус или другие детали'},
            ),
            'comment': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance or not self.instance.pk:
            return
        try:
            extension_case = self.instance.extension_case
        except ObjectDoesNotExist:
            return
        self.fields['extension_start'].initial = extension_case.extension_start
        self.fields['extension_end'].initial = extension_case.extension_end

    def clean(self):
        cleaned = super().clean()
        intent = cleaned.get('intent')
        next_shift_type = cleaned.get('next_shift_type')
        departure_on = cleaned.get('departure_on')
        arrival_on = cleaned.get('arrival_on')

        if intent in {'arrival', 'departure'}:
            required = (
                ('next_shift_type', 'Укажите смену «День» или «Ночь».'),
                ('departure_on', 'Укажите дату начала поездки.'),
                ('arrival_on', 'Укажите дату прибытия.'),
                ('route_text', 'Укажите полный маршрут.'),
                ('travel_mode', 'Укажите основной вид транспорта.'),
                ('transfer_mode', 'Укажите способ трансфера.'),
            )
            for field_name, message in required:
                if not cleaned.get(field_name):
                    self.add_error(field_name, message)
        elif intent == 'extension':
            if not next_shift_type:
                self.add_error('next_shift_type', 'Укажите смену на период продления.')
            if not (cleaned.get('comment') or '').strip():
                self.add_error('comment', 'Кратко укажите причину продления.')
            if not cleaned.get('extension_start'):
                self.add_error('extension_start', 'Укажите дату начала продления.')
            if not cleaned.get('extension_end'):
                self.add_error('extension_end', 'Укажите дату окончания продления.')

        if departure_on and arrival_on and arrival_on < departure_on:
            self.add_error('arrival_on', 'Дата прибытия не может быть раньше даты начала поездки.')
        extension_start = cleaned.get('extension_start')
        extension_end = cleaned.get('extension_end')
        if extension_start and extension_end and extension_end < extension_start:
            self.add_error('extension_end', 'Дата окончания продления не может быть раньше начала.')
        return cleaned
