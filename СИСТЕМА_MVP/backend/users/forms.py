import json
from io import BytesIO
from pathlib import Path

from django import forms
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from PIL import Image, ImageOps

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from assignments.services import (
    WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES,
    clear_active_equipment_assignment,
    equipment_queryset_for_work_role,
    get_active_equipment_assignment,
    set_active_equipment_assignment,
    validate_work_assignment,
)
from references.models import DormitorySection, Equipment
from shifts.models import EmployeeShift

from .models import (
    DriverPrimaryRegistration,
    Employee,
    EmployeeAccess,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
)
from .work_profiles import legacy_work_category_for_specialization, validate_base_specialization


MAX_EMPLOYEE_PHOTO_UPLOAD_SIZE = 5 * 1024 * 1024
MAX_EMPLOYEE_PHOTO_SIDE = 512
MAX_EMPLOYEE_PHOTO_PIXELS = 25_000_000
EMPLOYEE_PHOTO_QUALITY = 82
EMPLOYEE_PHOTO_ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
EMPLOYEE_PHOTO_ALLOWED_FORMATS = {'JPEG', 'PNG', 'WEBP'}


class WorkAssignmentRoleSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, 'instance', None)
        if instance:
            option['attrs']['data-work-role'] = instance.code
            option['attrs']['data-supports-equipment'] = (
                'true' if instance.code in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES else 'false'
            )
        return option


class WorkAssignmentEquipmentSelect(forms.Select):
    busy_assignments = None

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, 'instance', None)
        if instance:
            option['attrs']['data-base-label'] = str(label)
            equipment_type = (instance.equipment_type.name or '').casefold()
            for role_code, type_name in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES.items():
                if equipment_type == type_name.casefold():
                    option['attrs']['data-work-role'] = role_code
                    break
            for shift_type, employee_name in (self.busy_assignments or {}).get(instance.id, {}).items():
                option['attrs'][f'data-busy-{shift_type}'] = employee_name
        return option


class ProductionSpecializationSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, 'instance', None)
        if instance:
            option['attrs']['data-access-role-id'] = str(instance.access_role_id or '')
        return option


def optimize_employee_photo(uploaded_file):
    if not uploaded_file:
        return uploaded_file

    content_type = (getattr(uploaded_file, 'content_type', '') or '').lower()
    if content_type and content_type not in EMPLOYEE_PHOTO_ALLOWED_TYPES:
        raise ValidationError('Можно загружать только изображения JPG, PNG или WEBP.')

    if uploaded_file.size > MAX_EMPLOYEE_PHOTO_UPLOAD_SIZE:
        raise ValidationError('Фото слишком большое. Максимальный размер файла - 5 МБ.')

    try:
        uploaded_file.seek(0)
        image = Image.open(uploaded_file)
        detected_format = (image.format or '').upper()
        width, height = image.size
        image.verify()
    except Exception as exc:
        raise ValidationError('Файл не является корректным изображением.') from exc

    if detected_format not in EMPLOYEE_PHOTO_ALLOWED_FORMATS:
        raise ValidationError('Можно загружать только изображения JPG, PNG или WEBP.')
    if width * height > MAX_EMPLOYEE_PHOTO_PIXELS:
        raise ValidationError('Разрешение фото слишком большое.')

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


class EmployeeCardForm(forms.ModelForm):
    """Single employee profile contract shared by admin and OUP screens."""

    class Meta:
        model = Employee
        fields = [
            'full_name',
            'birth_date',
            'personnel_number',
            'phone',
            'photo',
            'personnel_position',
            'base_specialization',
            'position',
            'department',
            'work_category',
            'status',
            'hired_at',
            'dismissed_at',
            'rotation',
            'residence_text',
            'comment',
            'hr_data',
        ]
        labels = {
            'full_name': 'ФИО',
            'birth_date': 'Дата рождения',
            'phone': 'Мобильный телефон',
            'photo': 'Фото сотрудника',
            'personnel_position': 'Кадровая должность',
            'base_specialization': 'Производственная специализация',
            'position': 'Должность (архивное поле)',
            'department': 'Подразделение',
            'work_category': 'Рабочая категория',
            'status': 'Статус',
            'hired_at': 'Дата приема',
            'dismissed_at': 'Дата увольнения',
            'rotation': 'Вахта / график',
            'residence_text': 'Место проживания',
            'comment': 'Комментарий',
            'hr_data': 'Паспортные / кадровые данные',
        }
        widgets = {
            'birth_date': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
            'personnel_number': forms.HiddenInput(),
            'position': forms.HiddenInput(),
            'work_category': forms.HiddenInput(),
            'base_specialization': ProductionSpecializationSelect,
            'phone': forms.TextInput(attrs={
                'type': 'tel',
                'inputmode': 'tel',
                'autocomplete': 'tel',
                'placeholder': '+7 900 000-00-00',
                'maxlength': '18',
                'data-employee-phone': '1',
            }),
            'hired_at': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
            'dismissed_at': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
            'residence_text': forms.TextInput(attrs={
                'placeholder': 'Город, общежитие, комната или другое место проживания',
            }),
            'comment': forms.Textarea(attrs={'rows': 3}),
            'hr_data': forms.Textarea(attrs={'rows': 3}),
            'photo': forms.FileInput(attrs={
                'accept': 'image/jpeg,image/png,image/webp',
                'class': 'employee-photo-input',
                'data-employee-photo-input': '1',
                'aria-label': 'Выбрать фото сотрудника',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        is_existing_employee = bool(self.instance and self.instance.pk)
        self.fields['full_name'].required = True
        self.fields['phone'].required = not is_existing_employee
        self.fields['personnel_number'].required = False
        self.fields['work_category'].required = False
        self.fields['position'].required = False
        self.fields['personnel_position'].required = False
        self.fields['base_specialization'].required = False
        self.fields['status'].disabled = is_existing_employee
        self.fields['phone'].widget.attrs['maxlength'] = '18'
        self.fields['photo'].help_text = 'JPG, PNG или WEBP до 5 МБ.'

        current_position_id = getattr(self.instance, 'personnel_position_id', None)
        current_specialization_id = getattr(self.instance, 'base_specialization_id', None)
        self.fields['personnel_position'].queryset = (
            PersonnelPosition.objects.filter(Q(is_active=True) | Q(pk=current_position_id))
            .prefetch_related('allowed_specializations')
            .order_by('name')
        )
        self.fields['base_specialization'].queryset = (
            ProductionSpecialization.objects.filter(Q(is_active=True) | Q(pk=current_specialization_id))
            .select_related('access_role')
            .order_by('name')
        )
        position_catalog = {}
        for position in self.fields['personnel_position'].queryset:
            position_catalog[str(position.id)] = {
                'allowed': list(position.allowed_specializations.values_list('id', flat=True)),
                'default': position.default_specialization_id,
                'required': position.requires_specialization,
            }
        self.fields['base_specialization'].widget.attrs.update({
            'data-specialization-catalog': json.dumps(position_catalog),
            'data-base-specialization': '1',
        })
        self.fields['personnel_position'].widget.attrs['data-personnel-position'] = '1'

    def clean_full_name(self):
        value = ' '.join((self.cleaned_data.get('full_name') or '').split())
        if len(value.split()) < 2:
            raise ValidationError('Укажите фамилию и имя сотрудника.')
        return value

    def clean_personnel_number(self):
        value = (self.cleaned_data.get('personnel_number') or '').strip()
        if not value:
            return ''
        queryset = Employee.objects.filter(personnel_number__iexact=value)
        if self.instance and self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError('Сотрудник с таким архивным идентификатором уже существует.')
        return value

    def clean_phone(self):
        value = (self.cleaned_data.get('phone') or '').strip()
        if not value:
            return ''
        if not is_valid_russian_mobile_phone(value):
            raise ValidationError('Укажите российский мобильный номер в формате +7 900 000-00-00.')
        normalized = normalize_phone(value)
        previous_normalized = ''
        if self.instance and self.instance.pk:
            previous_phone = (
                Employee.objects.filter(pk=self.instance.pk)
                .values_list('phone', flat=True)
                .first()
            )
            previous_normalized = normalize_phone(previous_phone)

        if not self.instance.pk or normalized != previous_normalized:
            candidates = Employee.objects.exclude(phone='')
            if self.instance and self.instance.pk:
                candidates = candidates.exclude(pk=self.instance.pk)
            if any(normalize_phone(phone) == normalized for phone in candidates.values_list('phone', flat=True)):
                raise ValidationError('Этот мобильный номер уже указан в карточке другого сотрудника.')
        return f'+{normalized}'

    def clean_birth_date(self):
        value = self.cleaned_data.get('birth_date')
        if value and value > timezone.localdate():
            raise ValidationError('Дата рождения не может быть позже сегодняшней даты.')
        return value

    def clean_work_category(self):
        value = self.cleaned_data.get('work_category')
        if value:
            return value
        if self.instance and self.instance.pk:
            return self.instance.work_category
        return Employee.WorkCategory.OTHER

    def clean_hired_at(self):
        value = self.cleaned_data.get('hired_at')
        if value and value > timezone.localdate():
            raise ValidationError('Дата приема не может быть позже сегодняшней даты.')
        return value

    def clean_photo(self):
        return optimize_employee_photo(self.cleaned_data.get('photo'))

    def clean(self):
        cleaned_data = super().clean()
        hired_at = cleaned_data.get('hired_at')
        dismissed_at = cleaned_data.get('dismissed_at')
        if hired_at and dismissed_at and dismissed_at < hired_at:
            self.add_error('dismissed_at', 'Дата увольнения не может быть раньше даты приема.')

        personnel_position = cleaned_data.get('personnel_position')
        specialization = cleaned_data.get('base_specialization')
        legacy_position = (cleaned_data.get('position') or '').strip()
        if (
            not personnel_position
            and not (self.instance and self.instance.pk)
            and self.add_prefix('personnel_position') in self.data
            and not legacy_position
            and 'personnel_position' not in self.errors
        ):
            self.add_error('personnel_position', 'Выберите кадровую должность.')
        if personnel_position and not specialization and personnel_position.default_specialization_id:
            specialization = personnel_position.default_specialization
            cleaned_data['base_specialization'] = specialization
        if personnel_position:
            try:
                validate_base_specialization(
                    personnel_position=personnel_position,
                    specialization=specialization,
                )
            except ValidationError as error:
                self.add_error('base_specialization', error)
            else:
                cleaned_data['position'] = personnel_position.name
                cleaned_data['work_category'] = legacy_work_category_for_specialization(specialization)

        if not self.instance or not self.instance.pk:
            return cleaned_data
        previous_values = (
            Employee.objects.filter(pk=self.instance.pk)
            .values_list('personnel_position_id', 'base_specialization_id', 'work_category')
            .first()
        )
        if not previous_values:
            return cleaned_data
        previous_position_id, previous_specialization_id, previous_category = previous_values
        work_category = cleaned_data.get('work_category')
        specialization_changed = previous_specialization_id != getattr(specialization, 'id', None)
        position_changed = previous_position_id != getattr(personnel_position, 'id', None)
        if not specialization_changed and not position_changed and previous_category == work_category:
            return cleaned_data

        from .oup_services import employee_work_category_blockers

        blockers = employee_work_category_blockers(self.instance)
        if blockers:
            self.add_error(
                'base_specialization',
                'Сначала освободите сотрудника от рабочих операций: ' + '; '.join(blockers) + '.',
            )
        return cleaned_data

    def save(self, commit=True):
        employee = super().save(commit=False)
        personnel_position = self.cleaned_data.get('personnel_position')
        specialization = self.cleaned_data.get('base_specialization')
        if personnel_position:
            employee.position = personnel_position.name
            employee.work_category = legacy_work_category_for_specialization(specialization)
        if commit:
            employee.save()
            self.save_m2m()
        return employee


class AdminEmployeeForm(EmployeeCardForm):
    role = forms.ModelChoiceField(
        label='Доступ в приложение',
        queryset=Role.objects.filter(is_active=True).order_by('name'),
        required=False,
        widget=WorkAssignmentRoleSelect,
    )
    assignment_shift_type = forms.ChoiceField(
        label='Назначенная смена',
        choices=[('', 'Нет назначения'), *WorkShiftType.choices],
        required=False,
    )
    assignment_equipment = forms.ModelChoiceField(
        label='Назначенная техника',
        queryset=Equipment.objects.none(),
        required=False,
        empty_label='Нет назначения',
        widget=WorkAssignmentEquipmentSelect,
    )
    generate_access = forms.BooleanField(label='Выдать доступ после создания', required=False, initial=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['generate_access'].widget.attrs['form'] = 'employee-card-form'
        equipment_ids = set()
        for role_code in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES:
            equipment_ids.update(equipment_queryset_for_work_role(role_code).values_list('id', flat=True))
        equipment_queryset = (
            Equipment.objects
            .filter(id__in=equipment_ids)
            .select_related('equipment_type', 'model')
            .order_by('equipment_type__name', 'garage_number')
        )
        self.fields['assignment_equipment'].queryset = equipment_queryset
        occupied_assignments = (
            EquipmentAssignment.objects
            .filter(
                equipment_id__in=equipment_queryset.values_list('id', flat=True),
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
                shift__isnull=True,
                role__isnull=False,
                shift_type__in=WorkShiftType.values,
            )
            .select_related('employee')
            .order_by('equipment_id', 'shift_type', '-assigned_at')
        )
        busy_assignments = {}
        for assignment in occupied_assignments:
            equipment_busy = busy_assignments.setdefault(assignment.equipment_id, {})
            equipment_busy.setdefault(assignment.shift_type, assignment.employee.full_name)
        self.fields['assignment_equipment'].widget.busy_assignments = busy_assignments

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get('role')
        specialization = cleaned_data.get('base_specialization')
        expected_role = (
            specialization.access_role
            if specialization and specialization.access_role_id
            else None
        )
        if expected_role:
            if role and role.pk != expected_role.pk:
                self.add_error(
                    'role',
                    f'Для специализации «{specialization}» используется приложение «{expected_role}».',
                )
            role = expected_role
            cleaned_data['role'] = role
        elif (
            not cleaned_data.get('personnel_position')
            and role
            and role.code in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES
        ):
            cleaned_data['work_category'] = role.code
        shift_type = cleaned_data.get('assignment_shift_type')
        equipment = cleaned_data.get('assignment_equipment')
        if cleaned_data.get('generate_access') and not role:
            self.add_error(
                'generate_access',
                'Для выдачи PIN выберите доступ в приложение.',
            )
        if not shift_type and not equipment:
            return cleaned_data
        if not shift_type:
            self.add_error('assignment_shift_type', 'Выберите смену 1 или смену 2.')
        if not equipment:
            self.add_error('assignment_equipment', 'Выберите технику для назначения.')
            return cleaned_data
        if not role or role.code not in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES:
            self.add_error('assignment_equipment', 'Для выбранной роли назначение техники не поддерживается.')
            return cleaned_data
        if not equipment_queryset_for_work_role(role.code).filter(id=equipment.id).exists():
            self.add_error('assignment_equipment', 'Выбранная техника не соответствует рабочей роли или неактивна.')
            return cleaned_data
        if shift_type in WorkShiftType.values and EquipmentAssignment.objects.filter(
            equipment=equipment,
            shift_type=shift_type,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
        ).exists():
            self.add_error('assignment_equipment', 'Эта техника уже назначена другому сотруднику в выбранной смене.')
        return cleaned_data

    def save_work_assignment(self, *, employee, assigned_by):
        equipment = self.cleaned_data.get('assignment_equipment')
        if not equipment:
            return None
        assignment, _created = set_active_equipment_assignment(
            employee=employee,
            role=self.cleaned_data['role'],
            equipment=equipment,
            shift_type=self.cleaned_data['assignment_shift_type'],
            assigned_by=assigned_by,
        )
        return assignment


class AdminEmployeeEditForm(EmployeeCardForm):
    assignment_role = forms.ModelChoiceField(
        label='Рабочая роль',
        queryset=Role.objects.none(),
        required=False,
        widget=WorkAssignmentRoleSelect,
    )
    assignment_shift_type = forms.ChoiceField(
        label='Назначенная смена',
        choices=[('', 'Нет назначения'), *WorkShiftType.choices],
        required=False,
    )
    assignment_equipment = forms.ModelChoiceField(
        label='Назначенная техника',
        queryset=Equipment.objects.none(),
        required=False,
        empty_label='Нет назначения',
        widget=WorkAssignmentEquipmentSelect,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        employee = self.instance if self.instance and self.instance.pk else None
        if not employee:
            return

        active_assignment = get_active_equipment_assignment(employee)
        available_accesses = (
            employee.accesses
            .filter(role__is_active=True, is_active=True)
            .exclude(status=EmployeeAccess.Status.DEACTIVATED)
            .select_related('role')
            .order_by('role__name')
        )
        selected_specialization = employee.base_specialization
        if self.is_bound:
            specialization_id = self.data.get(self.add_prefix('base_specialization'))
            if specialization_id and str(specialization_id).isdigit():
                selected_specialization = (
                    ProductionSpecialization.objects
                    .select_related('access_role')
                    .filter(pk=int(specialization_id))
                    .first()
                )
        available_role_ids = set(available_accesses.values_list('role_id', flat=True))
        if selected_specialization and selected_specialization.access_role_id:
            available_role_ids.add(selected_specialization.access_role_id)
        available_roles = Role.objects.filter(id__in=available_role_ids).order_by('name')
        self.fields['assignment_role'].queryset = available_roles

        selected_role_id = self.data.get(self.add_prefix('assignment_role')) if self.is_bound else None
        selected_role_id = selected_role_id or (active_assignment.role_id if active_assignment else None)
        selected_role_id = selected_role_id or available_roles.values_list('id', flat=True).first()

        equipment_ids = set()
        equipment_role_codes = available_roles.filter(
            code__in=WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES,
        ).values_list('code', flat=True)
        for role_code in equipment_role_codes:
            equipment_ids.update(equipment_queryset_for_work_role(role_code).values_list('id', flat=True))
        equipment_queryset = (
            Equipment.objects
            .filter(id__in=equipment_ids)
            .select_related('equipment_type', 'model')
            .order_by('equipment_type__name', 'garage_number')
        )
        if active_assignment and not equipment_queryset.filter(id=active_assignment.equipment_id).exists():
            equipment_queryset = Equipment.objects.filter(
                id__in=[*equipment_queryset.values_list('id', flat=True), active_assignment.equipment_id],
            ).select_related('equipment_type', 'model').order_by('garage_number')
        self.fields['assignment_equipment'].queryset = equipment_queryset
        occupied_assignments = (
            EquipmentAssignment.objects
            .filter(
                equipment_id__in=equipment_queryset.values_list('id', flat=True),
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
                shift__isnull=True,
                role__isnull=False,
                shift_type__in=WorkShiftType.values,
            )
            .exclude(employee=employee)
            .select_related('employee')
            .order_by('equipment_id', 'shift_type', '-assigned_at')
        )
        busy_assignments = {}
        for assignment in occupied_assignments:
            equipment_busy = busy_assignments.setdefault(assignment.equipment_id, {})
            equipment_busy.setdefault(assignment.shift_type, assignment.employee.full_name)
        self.fields['assignment_equipment'].widget.busy_assignments = busy_assignments

        if not self.is_bound:
            self.initial.update({
                'assignment_role': active_assignment.role_id if active_assignment else selected_role_id,
                'assignment_shift_type': active_assignment.shift_type if active_assignment else '',
                'assignment_equipment': active_assignment.equipment_id if active_assignment else None,
            })

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get('assignment_role')
        shift_type = cleaned_data.get('assignment_shift_type')
        equipment = cleaned_data.get('assignment_equipment')
        specialization = cleaned_data.get('base_specialization')
        expected_role = (
            specialization.access_role
            if specialization and specialization.access_role_id
            else None
        )
        if equipment and expected_role and role and role.pk != expected_role.pk:
            self.add_error(
                'assignment_role',
                f'Для специализации «{specialization}» доступно назначение только по роли «{expected_role}».',
            )
            return cleaned_data
        if not equipment:
            return cleaned_data
        if not role:
            self.add_error('assignment_role', 'Выберите рабочую роль.')
        if not shift_type:
            self.add_error('assignment_shift_type', 'Выберите смену 1 или смену 2.')
        if role and shift_type:
            try:
                validate_work_assignment(
                    employee=self.instance,
                    role=role,
                    equipment=equipment,
                    shift_type=shift_type,
                    exclude_assignment=get_active_equipment_assignment(self.instance),
                )
            except ValidationError as error:
                self.add_error('assignment_equipment', error)
        return cleaned_data

    def save_work_assignment(self, *, assigned_by):
        equipment = self.cleaned_data.get('assignment_equipment')
        if not equipment:
            clear_active_equipment_assignment(employee=self.instance, assigned_by=assigned_by)
            return None
        assignment, _created = set_active_equipment_assignment(
            employee=self.instance,
            role=self.cleaned_data['assignment_role'],
            equipment=equipment,
            shift_type=self.cleaned_data['assignment_shift_type'],
            assigned_by=assigned_by,
        )
        return assignment


class PersonnelPositionReferenceForm(forms.ModelForm):
    """Maintain the official-position catalogue without allowing invalid links."""

    class Meta:
        model = PersonnelPosition
        fields = (
            'name',
            'code',
            'requires_specialization',
            'allowed_specializations',
            'default_specialization',
            'is_active',
        )
        widgets = {
            'allowed_specializations': forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_default_id = getattr(self.instance, 'default_specialization_id', None)
        current_allowed_ids = (
            self.instance.allowed_specializations.values_list('id', flat=True)
            if self.instance and self.instance.pk
            else []
        )
        visible_specializations = ProductionSpecialization.objects.filter(
            Q(is_active=True)
            | Q(pk=current_default_id)
            | Q(pk__in=current_allowed_ids)
        ).select_related('equipment_type', 'access_role').order_by('name')
        self.fields['allowed_specializations'].queryset = visible_specializations
        self.fields['default_specialization'].queryset = visible_specializations
        self.fields['allowed_specializations'].help_text = (
            'Определяют, какие базовые специализации можно выбрать для этой кадровой должности.'
        )
        self.fields['default_specialization'].help_text = (
            'Подставляется автоматически при создании сотрудника, если не выбрано другое допустимое значение.'
        )

    def clean(self):
        cleaned_data = super().clean()
        allowed_specializations = cleaned_data.get('allowed_specializations')
        default_specialization = cleaned_data.get('default_specialization')
        requires_specialization = cleaned_data.get('requires_specialization')
        if requires_specialization and not allowed_specializations:
            self.add_error(
                'allowed_specializations',
                'Для этой кадровой должности выберите хотя бы одну производственную специализацию.',
            )
        if (
            default_specialization
            and allowed_specializations is not None
            and default_specialization not in allowed_specializations
        ):
            self.add_error(
                'default_specialization',
                'Специализация по умолчанию должна входить в разрешенный список.',
            )
        return cleaned_data


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
    client_action_id = forms.CharField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = EmployeeShift
        fields = ['shift_type', 'truck', 'start_fuel', 'start_mileage', 'start_engine_hours']
        labels = {
            'start_fuel': 'Топливо на начало смены',
            'start_mileage': 'Одометр на начало смены',
            'start_engine_hours': 'Моточасы на начало смены',
        }
        widgets = {
            'start_fuel': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'start_mileage': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'start_engine_hours': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
        }

    def __init__(self, *args, employee=None, work_assignment=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        self.work_assignment = work_assignment
        self.fields['truck'].queryset = Equipment.objects.filter(equipment_type__name='Самосвал', is_active=True).select_related('model').order_by('garage_number')
        self.fields['truck'].widget.attrs['onchange'] = "if (this.value) window.location='?truck=' + this.value;"
        self.fields['start_fuel'].required = True
        self.fields['start_mileage'].required = True
        self.fields['start_engine_hours'].required = True
        if work_assignment:
            self.fields.pop('shift_type')
            self.fields.pop('truck')

    def clean(self):
        cleaned_data = super().clean()
        if self.work_assignment:
            shift_type = self.work_assignment.shift_type
            truck = self.work_assignment.equipment
            cleaned_data['shift_type'] = shift_type
            cleaned_data['truck'] = truck
        else:
            shift_type = cleaned_data.get('shift_type')
            truck = cleaned_data.get('truck')
        if not shift_type or not truck:
            return cleaned_data

        truck_busy = EmployeeShift.objects.filter(equipment=truck, closed_at__isnull=True).exists()
        if truck_busy:
            raise ValidationError('Смена по этому самосвалу уже открыта другим водителем.')

        from shifts.services import validate_driver_fuel_reading
        try:
            validate_driver_fuel_reading(truck, cleaned_data.get('start_fuel'))
        except ValidationError as error:
            self.add_error('start_fuel', error)

        return cleaned_data


class DriverCloseShiftForm(forms.ModelForm):
    client_action_id = forms.CharField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = EmployeeShift
        fields = ['end_fuel', 'end_mileage', 'end_engine_hours']
        labels = {
            'end_fuel': 'Топливо на конец смены',
            'end_mileage': 'Одометр на конец смены',
            'end_engine_hours': 'Моточасы на конец смены',
        }
        widgets = {
            'end_fuel': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'end_mileage': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'end_engine_hours': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ('end_fuel', 'end_mileage', 'end_engine_hours'):
            self.fields[field_name].required = True

    def clean(self):
        cleaned_data = super().clean()
        if self.instance and self.instance.pk and not self.errors:
            from shifts.services import validate_driver_close_readings
            try:
                validate_driver_close_readings(
                    self.instance,
                    end_fuel=cleaned_data.get('end_fuel'),
                    end_mileage=cleaned_data.get('end_mileage'),
                    end_engine_hours=cleaned_data.get('end_engine_hours'),
                )
            except ValidationError as error:
                if hasattr(error, 'error_dict'):
                    for field_name, field_errors in error.error_dict.items():
                        for field_error in field_errors:
                            self.add_error(field_name, field_error)
                else:
                    self.add_error('end_fuel', error)
        return cleaned_data
