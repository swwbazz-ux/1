from io import BytesIO
from pathlib import Path

from django import forms
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from PIL import Image, ImageOps

from references.models import DormitorySection, Equipment
from shifts.models import EmployeeShift

from .models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role


MAX_EMPLOYEE_PHOTO_UPLOAD_SIZE = 5 * 1024 * 1024
MAX_EMPLOYEE_PHOTO_SIDE = 512
EMPLOYEE_PHOTO_QUALITY = 82
EMPLOYEE_PHOTO_ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


def optimize_employee_photo(uploaded_file):
    if not uploaded_file:
        return uploaded_file

    content_type = getattr(uploaded_file, 'content_type', '')
    if not content_type:
        return uploaded_file

    if content_type not in EMPLOYEE_PHOTO_ALLOWED_TYPES:
        raise ValidationError('Можно загружать только изображения JPG, PNG или WEBP.')

    if uploaded_file.size > MAX_EMPLOYEE_PHOTO_UPLOAD_SIZE:
        raise ValidationError('Фото слишком большое. Максимальный размер файла - 5 МБ.')

    try:
        image = Image.open(uploaded_file)
        image.verify()
    except Exception as exc:
        raise ValidationError('Файл не является корректным изображением.') from exc

    uploaded_file.seek(0)
    image = Image.open(uploaded_file)
    image = ImageOps.exif_transpose(image)
    if image.mode not in ('RGB', 'L'):
        image = image.convert('RGB')
    elif image.mode == 'L':
        image = image.convert('RGB')

    image.thumbnail((MAX_EMPLOYEE_PHOTO_SIDE, MAX_EMPLOYEE_PHOTO_SIDE), Image.Resampling.LANCZOS)

    output = BytesIO()
    image.save(output, format='JPEG', quality=EMPLOYEE_PHOTO_QUALITY, optimize=True)
    output.seek(0)

    source_name = Path(uploaded_file.name).stem or 'employee_photo'
    safe_name = f'{source_name}.jpg'
    return ContentFile(output.read(), name=safe_name)


class AdminEmployeeForm(forms.ModelForm):
    role = forms.ModelChoiceField(
        label='Должность/роль',
        queryset=Role.objects.filter(is_active=True).order_by('name'),
        required=True,
    )
    generate_access = forms.BooleanField(label='Сгенерировать первичный пинкод', required=False, initial=True)

    class Meta:
        model = Employee
        fields = [
            'full_name',
            'position',
            'personnel_number',
            'phone',
            'status',
            'comment',
            'hired_at',
            'dismissed_at',
            'rotation',
            'residence_text',
            'hr_data',
            'photo',
        ]
        labels = {
            'full_name': 'ФИО',
            'position': 'Должность',
            'personnel_number': 'Табельный номер',
            'phone': 'Телефон',
            'status': 'Статус',
            'comment': 'Комментарий',
            'hired_at': 'Дата приема',
            'dismissed_at': 'Дата увольнения',
            'rotation': 'Вахта',
            'residence_text': 'Место проживания',
            'hr_data': 'Паспортные/кадровые данные',
            'photo': 'Фото сотрудника',
        }
        widgets = {
            'hired_at': forms.DateInput(attrs={'type': 'date'}),
            'dismissed_at': forms.DateInput(attrs={'type': 'date'}),
            'comment': forms.Textarea(attrs={'rows': 3}),
            'hr_data': forms.Textarea(attrs={'rows': 3}),
            'photo': forms.FileInput(attrs={'accept': 'image/jpeg,image/png,image/webp', 'class': 'employee-photo-input'}),
        }

    def clean_photo(self):
        return optimize_employee_photo(self.cleaned_data.get('photo'))


class AdminEmployeeEditForm(forms.ModelForm):
    class Meta:
        model = Employee
        fields = [
            'full_name',
            'position',
            'personnel_number',
            'phone',
            'status',
            'comment',
            'hired_at',
            'dismissed_at',
            'rotation',
            'residence_text',
            'hr_data',
            'photo',
        ]
        widgets = {
            'hired_at': forms.DateInput(attrs={'type': 'date'}),
            'dismissed_at': forms.DateInput(attrs={'type': 'date'}),
            'comment': forms.Textarea(attrs={'rows': 3}),
            'hr_data': forms.Textarea(attrs={'rows': 3}),
            'photo': forms.FileInput(attrs={'accept': 'image/jpeg,image/png,image/webp', 'class': 'employee-photo-input'}),
        }

    def clean_photo(self):
        return optimize_employee_photo(self.cleaned_data.get('photo'))


class AdminAccessRoleForm(forms.Form):
    role = forms.ModelChoiceField(
        label='Роль',
        queryset=Role.objects.filter(is_active=True).order_by('name'),
        required=True,
    )


class AdminAccessBlockForm(forms.Form):
    reason = forms.CharField(label='Причина блокировки', required=False, widget=forms.Textarea(attrs={'rows': 2}))


def normalize_phone(value):
    digits = ''.join(char for char in str(value or '') if char.isdigit())
    if len(digits) == 11 and digits.startswith('8'):
        return f'7{digits[1:]}'
    return digits


def is_valid_russian_mobile_phone(value):
    digits = normalize_phone(value)
    return len(digits) == 11 and digits.startswith('79')


class AccessActivationForm(forms.Form):
    phone = forms.CharField(
        label='Номер телефона',
        max_length=32,
        widget=forms.TextInput(attrs={
            'inputmode': 'numeric',
            'autocomplete': 'tel',
            'placeholder': 'Например 79000000000',
            'maxlength': '11',
            'pattern': '[0-9]{11}',
            'data-phone-input': '1',
            'data-hint': 'Введите 11 цифр российского мобильного номера, например 79000000000.',
        }),
    )
    new_access_code = forms.CharField(
        label='Придумайте постоянный пинкод',
        min_length=6,
        max_length=6,
        widget=forms.PasswordInput(attrs={
            'inputmode': 'numeric',
            'autocomplete': 'new-password',
            'placeholder': '6 цифр',
            'maxlength': '6',
            'pattern': '[0-9]{6}',
            'data-pin-input': '1',
            'data-hint': 'Пинкод должен состоять ровно из 6 цифр.',
        }),
    )
    confirm_access_code = forms.CharField(
        label='Повторите пинкод',
        min_length=6,
        max_length=6,
        widget=forms.PasswordInput(attrs={
            'inputmode': 'numeric',
            'autocomplete': 'new-password',
            'placeholder': 'Повторите 6 цифр',
            'maxlength': '6',
            'pattern': '[0-9]{6}',
            'data-pin-input': '1',
            'data-hint': 'Повторите тот же пинкод из 6 цифр.',
        }),
    )

    def __init__(self, *args, access=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.access = access

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        employee_phone = normalize_phone(getattr(self.access.employee, 'phone', '')) if self.access else ''
        if not is_valid_russian_mobile_phone(phone) or not employee_phone or normalize_phone(phone) != employee_phone:
            raise ValidationError('Телефон или пинкод указаны неверно.')
        return phone

    def clean_new_access_code(self):
        code = self.cleaned_data['new_access_code'].strip()
        if not code.isdigit() or len(code) != 6:
            raise ValidationError('Пинкод должен состоять ровно из 6 цифр.')
        if self.access and EmployeeAccess.objects.filter(
            employee=self.access.employee,
            access_code=code,
            is_active=True,
        ).exclude(id=self.access.id).exists():
            raise ValidationError('Этот пинкод нельзя использовать. Выберите другой.')
        return code

    def clean(self):
        cleaned_data = super().clean()
        new_code = cleaned_data.get('new_access_code')
        confirm_code = cleaned_data.get('confirm_access_code')
        if new_code and confirm_code and new_code != confirm_code:
            raise ValidationError('Пинкоды не совпадают.')
        return cleaned_data


class DriverPrimaryRegistrationForm(forms.ModelForm):
    class Meta:
        model = DriverPrimaryRegistration
        fields = ['dormitory_section']
        labels = {
            'dormitory_section': 'Место проживания',
        }

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        self.fields['dormitory_section'].queryset = DormitorySection.objects.select_related('block__dormitory').order_by(
            'block__dormitory__number',
            'block__name',
            'name',
        )


class DriverOpenShiftForm(forms.ModelForm):
    shift_type = forms.ChoiceField(label='Смена', choices=EmployeeShift._meta.get_field('shift_type').choices)
    truck = forms.ModelChoiceField(label='Самосвал', queryset=Equipment.objects.none())

    class Meta:
        model = EmployeeShift
        fields = ['shift_type', 'truck', 'start_fuel', 'start_mileage', 'start_engine_hours']
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

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        self.fields['truck'].queryset = Equipment.objects.filter(equipment_type__name='Самосвал', is_active=True).order_by('garage_number')
        self.fields['truck'].widget.attrs['onchange'] = "if (this.value) window.location='?truck=' + this.value;"
        self.fields['start_fuel'].required = True
        self.fields['start_mileage'].required = True
        self.fields['start_engine_hours'].required = True

    def clean(self):
        cleaned_data = super().clean()
        shift_type = cleaned_data.get('shift_type')
        truck = cleaned_data.get('truck')
        if not shift_type or not truck:
            return cleaned_data

        truck_busy = EmployeeShift.objects.filter(
            shift_type=shift_type,
            equipment=truck,
            closed_at__isnull=True,
        ).exclude(employee=self.employee).exists()
        if truck_busy:
            raise ValidationError('Этот самосвал уже занят в выбранной смене.')

        return cleaned_data


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
