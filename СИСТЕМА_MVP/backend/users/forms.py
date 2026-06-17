from django import forms
from django.core.exceptions import ValidationError

from references.models import DormitorySection, Equipment
from shifts.models import EmployeeShift

from .models import DriverPrimaryRegistration


class DriverPrimaryRegistrationForm(forms.ModelForm):
    class Meta:
        model = DriverPrimaryRegistration
        fields = ['shift_type', 'truck', 'dormitory_section']
        labels = {
            'shift_type': 'Смена',
            'truck': 'Самосвал',
            'dormitory_section': 'Место проживания',
        }

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        self.fields['truck'].queryset = Equipment.objects.filter(equipment_type__name='Самосвал', is_active=True).order_by('garage_number')
        self.fields['dormitory_section'].queryset = DormitorySection.objects.select_related('block__dormitory').order_by(
            'block__dormitory__number',
            'block__name',
            'name',
        )

    def clean(self):
        cleaned_data = super().clean()
        shift_type = cleaned_data.get('shift_type')
        truck = cleaned_data.get('truck')
        section = cleaned_data.get('dormitory_section')
        if not shift_type or not truck or not section:
            return cleaned_data

        truck_busy = DriverPrimaryRegistration.objects.filter(
            shift_type=shift_type,
            truck=truck,
        ).exclude(employee=self.employee).exists()
        if truck_busy:
            raise ValidationError('Этот самосвал уже занят водителем в выбранной смене.')

        registrations_in_section = DriverPrimaryRegistration.objects.filter(
            shift_type=shift_type,
            dormitory_section=section,
        ).exclude(employee=self.employee).count()
        capacity = section.day_capacity if shift_type == 'day' else section.night_capacity
        if registrations_in_section >= capacity:
            raise ValidationError('В выбранной секции уже нет свободных мест для этой смены.')

        return cleaned_data


class DriverOpenShiftForm(forms.ModelForm):
    class Meta:
        model = EmployeeShift
        fields = ['start_fuel', 'start_mileage', 'start_engine_hours']
        labels = {
            'start_fuel': 'Топливо на начало смены',
            'start_mileage': 'Пробег на начало смены',
            'start_engine_hours': 'Моточасы на начало смены',
        }
        widgets = {
            'start_fuel': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'start_mileage': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'start_engine_hours': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
        }


class DriverCloseShiftForm(forms.ModelForm):
    class Meta:
        model = EmployeeShift
        fields = ['end_fuel', 'end_mileage', 'end_engine_hours']
        labels = {
            'end_fuel': 'Топливо на конец смены',
            'end_mileage': 'Пробег на конец смены',
            'end_engine_hours': 'Моточасы на конец смены',
        }
        widgets = {
            'end_fuel': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'end_mileage': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'end_engine_hours': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
        }
