from django import forms

from .models import PilotFeedback


class PilotFeedbackForm(forms.ModelForm):
    class Meta:
        model = PilotFeedback
        fields = ['title', 'category', 'priority', 'status', 'screen', 'description', 'decision']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'decision': forms.Textarea(attrs={'rows': 3}),
        }
