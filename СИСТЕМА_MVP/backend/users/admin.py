from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError

from .models import (
    ActiveApplicationSession,
    AdminActionLog,
    AdminConflict,
    DriverPrimaryRegistration,
    Employee,
    EmployeeAccess,
    PersonnelDepartment,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
    TemporaryWorkTransfer,
    WatchComposition,
    WorkSchedule,
)


class EmployeeAdminForm(forms.ModelForm):
    PROTECTED_WATCH_PROFILE_FIELDS = (
        'work_schedule',
        'brigade_number',
        'watch_composition',
        'rotation',
    )
    WATCH_PROFILE_EDIT_ERROR = (
        'Изменение графика, бригады и состава вахты действующего сотрудника '
        'выполняет Табельщик.'
    )

    class Meta:
        model = Employee
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance or not self.instance.pk:
            return
        for field_name in self.PROTECTED_WATCH_PROFILE_FIELDS:
            field = self.fields[field_name]
            field.disabled = True
            field.help_text = self.WATCH_PROFILE_EDIT_ERROR

    def clean(self):
        cleaned_data = super().clean()
        if not self.instance or not self.instance.pk:
            return cleaned_data

        expected_values = {
            'work_schedule': str(self.instance.work_schedule_id or ''),
            'brigade_number': str(self.instance.brigade_number or ''),
            'watch_composition': str(self.instance.watch_composition_id or ''),
            'rotation': str(self.instance.rotation or ''),
        }
        for field_name, expected_value in expected_values.items():
            posted_name = self.add_prefix(field_name)
            if posted_name not in self.data:
                continue
            submitted_value = str(self.data.get(posted_name) or '')
            if submitted_value != expected_value:
                raise ValidationError(
                    self.WATCH_PROFILE_EDIT_ERROR,
                    code='django_admin_watch_profile_forbidden',
                )
        return cleaned_data


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    form = EmployeeAdminForm
    list_display = (
        'full_name',
        'sex',
        'personnel_department',
        'personnel_position',
        'base_specialization',
        'work_schedule',
        'brigade_number',
        'watch_composition',
        'status',
        'is_active',
    )
    search_fields = ('full_name', 'phone', 'personnel_department__name', 'department', 'position')
    list_filter = (
        'status',
        'is_active',
        'sex',
        'work_category',
        'personnel_department',
        'work_schedule',
        'brigade_number',
        'watch_composition',
    )


@admin.register(PersonnelDepartment)
class PersonnelDepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)


@admin.register(WorkSchedule)
class WorkScheduleAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'brigade_count', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('brigade_count', 'is_active')


@admin.register(WatchComposition)
class WatchCompositionAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)


@admin.register(ProductionSpecialization)
class ProductionSpecializationAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'equipment_type', 'access_role', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('is_active', 'equipment_type', 'access_role')


@admin.register(PersonnelPosition)
class PersonnelPositionAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'requires_specialization', 'default_specialization', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('requires_specialization', 'is_active')
    filter_horizontal = ('allowed_specializations',)


@admin.register(TemporaryWorkTransfer)
class TemporaryWorkTransferAdmin(admin.ModelAdmin):
    list_display = (
        'employee',
        'target_specialization',
        'watch_period',
        'effective_from',
        'effective_to',
        'status',
        'requested_by',
        'reviewed_by',
    )
    search_fields = ('employee__full_name', 'target_specialization__name', 'reason')
    list_filter = ('status', 'watch_period', 'target_specialization')
    readonly_fields = ('requested_at', 'reviewed_at', 'closed_at')


@admin.register(EmployeeAccess)
class EmployeeAccessAdmin(admin.ModelAdmin):
    list_display = ('employee', 'role', 'status', 'is_active', 'last_login_at', 'created_at', 'deactivated_at')
    search_fields = ('employee__full_name', 'role__name', 'access_code')
    list_filter = ('role', 'status', 'is_active')


@admin.register(ActiveApplicationSession)
class ActiveApplicationSessionAdmin(admin.ModelAdmin):
    list_display = ('last_seen_at', 'access', 'app_code', 'path', 'device_kind')
    search_fields = ('access__employee__full_name', 'role_code', 'app_code', 'path')
    list_filter = ('app_code', 'role_code', 'device_kind')
    readonly_fields = (
        'session_key', 'access', 'role_code', 'app_code', 'path',
        'device_kind', 'first_seen_at', 'last_seen_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DriverPrimaryRegistration)
class DriverPrimaryRegistrationAdmin(admin.ModelAdmin):
    list_display = ('employee', 'dormitory_section', 'created_at')
    search_fields = ('employee__full_name',)
    list_filter = ('dormitory_section',)


@admin.register(AdminActionLog)
class AdminActionLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'actor', 'action', 'action_code', 'object_type', 'object_repr', 'reversal_of')
    search_fields = ('actor__full_name', 'action', 'object_repr', 'comment')
    list_filter = ('action', 'action_code', 'object_type')
    readonly_fields = (
        'created_at', 'actor', 'action', 'action_code', 'object_type', 'object_id', 'object_repr',
        'old_value', 'new_value', 'comment', 'undo_payload', 'reversal_of',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return super().has_view_permission(request, obj=obj)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AdminConflict)
class AdminConflictAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'conflict_type', 'employee', 'status', 'resolved_by', 'resolved_at')
    search_fields = ('employee__full_name', 'conflict_type', 'description', 'comment')
    list_filter = ('status', 'conflict_type')

# Register your models here.
