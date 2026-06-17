from django import forms

from references.models import Equipment

from .models import HaulAssignment


class HaulAssignmentForm(forms.ModelForm):
    class Meta:
        model = HaulAssignment
        fields = ['excavator', 'truck']
        labels = {
            'excavator': 'Экскаватор',
            'truck': 'Самосвал',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['excavator'].queryset = Equipment.objects.filter(equipment_type__name='Экскаватор', is_active=True).order_by('garage_number')
        self.fields['truck'].queryset = Equipment.objects.filter(equipment_type__name='Самосвал', is_active=True).order_by('garage_number')
