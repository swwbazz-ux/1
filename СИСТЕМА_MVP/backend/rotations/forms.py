from django import forms
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from shifts.models import WatchPeriod
from users.models import Employee, WorkSchedule

from .arrival_roster_parser import MAX_FILE_SIZE
from .models import (
    ArrivalRosterRowReview,
    EmployeeWatchProfileChange,
    RotationCollectionCycle,
    RotationResponse,
)


class EmployeeWatchProfileChangeDraftForm(forms.Form):
    employee_id = forms.ModelChoiceField(
        label='Сотрудник',
        queryset=Employee.objects.none(),
    )
    watch_period_id = forms.ModelChoiceField(
        label='Период действия',
        queryset=WatchPeriod.objects.none(),
    )
    new_work_schedule_id = forms.ModelChoiceField(
        label='Новый график работы',
        queryset=WorkSchedule.objects.none(),
    )
    new_brigade_number = forms.IntegerField(
        label='Номер бригады',
        required=False,
        min_value=1,
    )
    basis_kind = forms.ChoiceField(
        label='Вид официального основания',
        choices=EmployeeWatchProfileChange.BasisKind.choices,
    )
    basis_number = forms.CharField(
        label='Номер основания',
        max_length=128,
    )
    basis_date = forms.DateField(
        label='Дата основания',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    basis = forms.CharField(
        label='Содержание основания',
        widget=forms.Textarea(attrs={'rows': 3}),
    )

    def __init__(self, *args, future_periods_only=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['employee_id'].queryset = (
            Employee.objects.filter(
                is_active=True,
                status=Employee.Status.ACTIVE,
            ).order_by('full_name', 'pk')
        )
        periods = WatchPeriod.objects.select_related('watch_composition').order_by(
            'starts_on',
            'pk',
        )
        if future_periods_only:
            periods = periods.filter(
                is_active=True,
                starts_on__gt=timezone.localdate(),
            )
        self.fields['watch_period_id'].queryset = periods
        self.fields['new_work_schedule_id'].queryset = (
            WorkSchedule.objects.filter(is_active=True).order_by('name', 'pk')
        )


class ArrivalRosterUploadForm(forms.Form):
    watch_period = forms.ModelChoiceField(
        label='Период вахты',
        queryset=WatchPeriod.objects.none(),
    )
    workbook = forms.FileField(
        label='Файл реестра',
        help_text='Только книга .xlsx размером не более 10 МиБ.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['watch_period'].queryset = WatchPeriod.objects.filter(
            is_active=True,
        ).order_by('-starts_on', '-pk')
        self.fields['workbook'].widget.attrs.update({'accept': '.xlsx'})

    def clean_workbook(self):
        workbook = self.cleaned_data['workbook']
        if not str(workbook.name or '').casefold().endswith('.xlsx'):
            raise forms.ValidationError('Разрешены только файлы .xlsx.')
        if workbook.size > MAX_FILE_SIZE:
            raise forms.ValidationError('Размер файла превышает 10 МиБ.')
        return workbook


class ArrivalRosterPoolCreateForm(forms.Form):
    watch_period = forms.ModelChoiceField(
        label='Период вахты',
        queryset=WatchPeriod.objects.none(),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['watch_period'].queryset = (
            WatchPeriod.objects
            .filter(is_active=True, watch_composition__is_active=True)
            .select_related('watch_composition')
            .order_by('-starts_on', '-pk')
        )


class ArrivalRosterConfirmationForm(forms.Form):
    expected_sha256 = forms.RegexField(
        regex=r'^[0-9a-f]{64}$',
        widget=forms.HiddenInput,
    )


class ArrivalRosterEmployeeSearchForm(forms.Form):
    query = forms.CharField(
        label='ФИО или должность',
        min_length=2,
        max_length=255,
    )
    watch_composition = forms.ChoiceField(
        label='Текущая вахта',
        choices=(),
        required=False,
    )
    employment_status = forms.ChoiceField(
        label='Состояние карточки',
        choices=(
            ('active', 'Действующие'),
            ('dismissed', 'Уволенные'),
            ('all', 'Все'),
        ),
        initial='active',
    )

    def __init__(self, *args, watch_compositions=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['watch_composition'].choices = [
            ('', 'Все вахты'),
            *((str(item.pk), item.name) for item in watch_compositions),
        ]


class ArrivalRosterExternalSearchForm(forms.Form):
    external_query = forms.CharField(
        label='ФИО, организация или профессия',
        min_length=2,
        max_length=255,
    )


class ArrivalRosterEmployeeAddForm(forms.Form):
    employee_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class ArrivalRosterExternalAddForm(forms.Form):
    resident_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    basis = forms.CharField(
        label='Основание добавления',
        min_length=1,
        max_length=1000,
        widget=forms.Textarea(attrs={'rows': 2}),
    )


class ArrivalRosterExpectedRevisionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class ArrivalRosterResidentSearchForm(forms.Form):
    query = forms.CharField(
        label='Поиск жильца по ФИО',
        min_length=3,
        max_length=255,
    )


class ArrivalRosterResidentSelectionForm(ArrivalRosterExpectedRevisionForm):
    resident_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class ArrivalRosterParticipationForm(ArrivalRosterExpectedRevisionForm):
    participation_status = forms.ChoiceField(
        label='Участие в заезде',
        choices=ArrivalRosterRowReview.ParticipationStatus.choices,
    )
    arrival_mode = forms.ChoiceField(
        label='Способ прибытия',
        choices=[('', 'Не указан'), *ArrivalRosterRowReview.ArrivalMode.choices],
        required=False,
    )


class ArrivalRosterDatesForm(ArrivalRosterExpectedRevisionForm):
    arrival_on = forms.DateField(
        label='Дата заселения',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    departure_on = forms.DateField(
        label='Дата выбытия',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )


class ArrivalRosterNotesForm(ArrivalRosterExpectedRevisionForm):
    basis = forms.CharField(label='Основание', required=False, widget=forms.Textarea)
    comment = forms.CharField(label='Комментарий', required=False, widget=forms.Textarea)


class ArrivalRosterIssueResolutionForm(ArrivalRosterExpectedRevisionForm):
    resolution_note = forms.CharField(
        label='Пояснение',
        min_length=1,
        max_length=1000,
        widget=forms.Textarea,
    )


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
