from django import forms

from .models import PilotFeedback, RatingPeriod
from .rating_period_calendar import (
    RATING_PERIOD_DEFAULT_START_DAY,
    nominal_rating_period_end,
)


class RatingPeriodReferenceForm(forms.ModelForm):
    def clean(self):
        cleaned_data = super().clean()
        starts_on = cleaned_data.get('starts_on')
        ends_before = cleaned_data.get('ends_before')
        comment = cleaned_data.get('comment')
        if (
            self.instance.nominal_starts_on is None
            and starts_on is not None
            and ends_before is not None
            and (
                starts_on.day != RATING_PERIOD_DEFAULT_START_DAY
                or ends_before != nominal_rating_period_end(starts_on)
            )
            and not (comment or '').strip()
        ):
            self.add_error(
                'comment',
                'Укажите причину, почему даты отличаются от обычного '
                'периода 14-е → 14-е.',
            )
        return cleaned_data

    class Meta:
        model = RatingPeriod
        fields = [
            'name',
            'starts_on',
            'ends_before',
            'comment',
            'is_active',
        ]
        widgets = {
            'starts_on': forms.DateInput(
                format='%Y-%m-%d',
                attrs={'type': 'date'},
            ),
            'ends_before': forms.DateInput(
                format='%Y-%m-%d',
                attrs={'type': 'date'},
            ),
            'comment': forms.Textarea(attrs={'rows': 3}),
        }


class PilotFeedbackForm(forms.ModelForm):
    class Meta:
        model = PilotFeedback
        fields = ['title', 'category', 'priority', 'status', 'screen', 'description', 'decision']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'decision': forms.Textarea(attrs={'rows': 3}),
        }
