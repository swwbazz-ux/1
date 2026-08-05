from django import forms

from .models import PilotFeedback, RatingPeriod


class RatingPeriodReferenceForm(forms.ModelForm):
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
