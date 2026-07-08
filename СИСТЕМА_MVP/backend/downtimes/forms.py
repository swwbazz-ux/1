from django import forms
from .models import DowntimeReason


class MechanicDowntimeCreateForm(forms.Form):
    reason = forms.ModelChoiceField(
        label='Причина механического простоя',
        queryset=DowntimeReason.objects.none(),
        empty_label='Выберите причину',
    )
    comment = forms.CharField(
        label='Комментарий механика',
        required=False,
        widget=forms.Textarea(attrs={'rows': 3}),
    )

    def __init__(self, *args, equipment=None, source_text='', **kwargs):
        super().__init__(*args, **kwargs)
        if equipment is None:
            self.fields['reason'].queryset = DowntimeReason.for_workplace('mechanic')
        else:
            self.fields['reason'].queryset = DowntimeReason.for_workplace('mechanic', equipment.equipment_type)
        if source_text and not self.is_bound:
            self.initial['comment'] = f'Источник: {source_text}'
