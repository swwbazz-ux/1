from django import forms

from .equipment_plan_groups import describe_equipment, validate_equipment_plan_group_membership
from .models import EquipmentPlanGroup


class EquipmentPlanGroupForm(forms.ModelForm):
    class Meta:
        model = EquipmentPlanGroup
        fields = ['name', 'code', 'calculation_mode', 'plan_value', 'equipment', 'is_active', 'active_from', 'comment']
        widgets = {
            'equipment': forms.CheckboxSelectMultiple(attrs={'class': 'reference-checkbox-grid'}),
            'comment': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'equipment' in self.fields:
            field = self.fields['equipment']
            field.queryset = field.queryset.select_related('equipment_type', 'model').order_by('equipment_type__name', 'garage_number')
            field.label_from_instance = describe_equipment
            field.help_text = 'Отметьте технику, которая входит в эту группу. Несовместимые типы не сохраняются.'

    def clean(self):
        cleaned_data = super().clean()
        equipment = cleaned_data.get('equipment') or []
        group_code = cleaned_data.get('code') or self.instance.code
        is_active = cleaned_data.get('is_active')
        try:
            validate_equipment_plan_group_membership(
                self.instance,
                equipment,
                group_code=group_code,
                is_active=is_active,
            )
        except forms.ValidationError as error:
            if hasattr(error, 'error_dict') and 'equipment' in error.error_dict:
                for item in error.error_dict['equipment']:
                    self.add_error('equipment', item)
            else:
                self.add_error('equipment', error)
        return cleaned_data
