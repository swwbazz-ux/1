from django import forms
from decimal import Decimal

from assignments.models import AssignmentStatus, HaulAssignment
from references.models import DumpPoint, RockType, TruckCapacityRule
from shifts.models import EmployeeShift

from .models import Trip


class TripCreateForm(forms.Form):
    assignment = forms.ModelChoiceField(label='Самосвал под экскаватором', queryset=HaulAssignment.objects.none())
    rock_type = forms.ModelChoiceField(label='Порода', queryset=RockType.objects.filter(is_active=True).order_by('name'))
    dump_point = forms.ModelChoiceField(label='Точка разгрузки', queryset=DumpPoint.objects.filter(is_active=True).order_by('name'))
    planned_volume_m3 = forms.DecimalField(label='Плановое задание, м3', required=False, min_value=0)
    loading_horizon = forms.CharField(label='Горизонт погрузки', required=False, max_length=64)
    loading_block = forms.CharField(label='Блок', required=False, max_length=64)
    transport_distance_km = forms.DecimalField(label='Плечо транспортировки, км', required=False, min_value=0)
    downtime_text = forms.CharField(label='Простои', required=False, max_length=255)
    note = forms.CharField(label='Примечание', required=False, widget=forms.Textarea(attrs={'rows': 3}))

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
        loading_shift = EmployeeShift.objects.filter(
            employee=excavator_operator,
            closed_at__isnull=True,
        ).order_by('-opened_at').first()
        volume = self.get_trip_volume(assignment, rock_type)
        tonnage = self.get_trip_tonnage(volume, rock_type)
        return Trip.objects.create(
            excavator=assignment.excavator,
            truck=assignment.truck,
            excavator_operator=excavator_operator,
            loading_shift=loading_shift,
            rock_type=rock_type,
            dump_point=dump_point,
            planned_volume_m3=self.cleaned_data.get('planned_volume_m3'),
            volume_m3=volume,
            tonnage=tonnage,
            loading_horizon=self.cleaned_data.get('loading_horizon', ''),
            loading_block=self.cleaned_data.get('loading_block', ''),
            transport_distance_km=self.cleaned_data.get('transport_distance_km'),
            downtime_text=self.cleaned_data.get('downtime_text', ''),
            note=self.cleaned_data.get('note', ''),
        )

    def get_trip_volume(self, assignment, rock_type):
        if assignment.truck.model:
            rule = TruckCapacityRule.objects.filter(equipment_model=assignment.truck.model, rock_type=rock_type).first()
            if rule:
                return rule.volume_m3
            if assignment.truck.model.body_volume_m3:
                return assignment.truck.model.body_volume_m3
        return None

    def get_trip_tonnage(self, volume, rock_type):
        if not volume or not rock_type.density:
            return None
        return (Decimal(volume) * Decimal(rock_type.density)).quantize(Decimal('0.01'))
