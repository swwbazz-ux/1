from django import forms
from assignments.models import AssignmentStatus, HaulAssignment
from references.models import DumpPoint, RockType
from shifts.models import EmployeeShift

from .models import Trip
from .trip_creation import create_loaded_waiting_unload_trip


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
        self.excavator_operator = kwargs.pop('excavator_operator', None)
        super().__init__(*args, **kwargs)
        self.fields['assignment'].queryset = HaulAssignment.objects.filter(
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        ).select_related('truck', 'excavator').order_by('excavator__garage_number', 'truck__garage_number')
        if not self.is_bound:
            self.apply_last_trip_initials()

    def apply_last_trip_initials(self):
        if not self.excavator_operator:
            return
        last_trip = (
            Trip.objects
            .filter(excavator_operator=self.excavator_operator)
            .exclude(planned_volume_m3__isnull=True, loading_horizon='', loading_block='', transport_distance_km__isnull=True)
            .order_by('-created_at')
            .first()
        )
        if not last_trip:
            return
        self.fields['planned_volume_m3'].initial = last_trip.planned_volume_m3
        self.fields['loading_horizon'].initial = last_trip.loading_horizon
        self.fields['loading_block'].initial = last_trip.loading_block
        self.fields['transport_distance_km'].initial = last_trip.transport_distance_km

    def create_trip(self, excavator_operator, *, loading_shift=None):
        assignment = self.cleaned_data['assignment']
        rock_type = self.cleaned_data['rock_type']
        dump_point = self.cleaned_data['dump_point']
        if loading_shift is None:
            loading_shift = EmployeeShift.objects.filter(
                employee=excavator_operator,
                closed_at__isnull=True,
            ).order_by('-opened_at').first()
        return create_loaded_waiting_unload_trip(
            assignment=assignment,
            excavator_operator=excavator_operator,
            loading_shift=loading_shift,
            rock_type=rock_type,
            dump_point=dump_point,
            planned_volume_m3=self.cleaned_data.get('planned_volume_m3'),
            loading_horizon=self.cleaned_data.get('loading_horizon', ''),
            loading_block=self.cleaned_data.get('loading_block', ''),
            transport_distance_km=self.cleaned_data.get('transport_distance_km'),
            downtime_text=self.cleaned_data.get('downtime_text', ''),
            note=self.cleaned_data.get('note', ''),
        )
