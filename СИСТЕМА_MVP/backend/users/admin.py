from django.contrib import admin

from .models import (
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
    WorkSchedule,
)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        'full_name',
        'personnel_department',
        'personnel_position',
        'base_specialization',
        'work_schedule',
        'brigade_number',
        'status',
        'is_active',
    )
    search_fields = ('full_name', 'phone', 'personnel_department__name', 'department', 'position')
    list_filter = ('status', 'is_active', 'work_category', 'personnel_department', 'work_schedule', 'brigade_number')


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
