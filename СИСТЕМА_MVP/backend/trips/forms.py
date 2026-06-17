from django import forms

from assignments.models import AssignmentStatus, HaulAssignment
from references.models import DumpPoint, RockType, TruckCapacityRule

from .models import Trip


class TripCreateForm(forms.Form):
    assignment = forms.ModelChoiceField(label='Самосвал под экскаватором', queryset=HaulAssignment.objects.none())
    rock_type = forms.ModelChoiceField(label='Порода', queryset=RockType.objects.filter(is_active=True).order_by('name'))
    dump_point = forms.ModelChoiceField(label='Точка разгрузки', queryset=DumpPoint.objects.filter(is_active=True).order_by('name'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assignment'].queryset = HaulAssignment.objects.filter(
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        ).select_related('truck', 'excavator').order_by('excavator__garage_number', 'truck__garage_number')

    def create_trip(self, excavator_operator):
        assignment = self.cleaned_data['assignment']
        rock_type = self.cleaned_data['rock_type']
        dump_point = self.cleaned_data['dump_point']
        volume = None
        if assignment.truck.model:
            rule = TruckCapacityRule.objects.filter(equipment_model=assignment.truck.model, rock_type=rock_type).first()
            if rule:
                volume = rule.volume_m3
        return Trip.objects.create(
            excavator=assignment.excavator,
            truck=assignment.truck,
            excavator_operator=excavator_operator,
            rock_type=rock_type,
            dump_point=dump_point,
            volume_m3=volume,
        )
