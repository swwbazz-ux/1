from django.contrib import admin

from .models import AdminActionLog, AdminConflict, DriverPrimaryRegistration, Employee, EmployeeAccess, Role


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'department', 'position', 'work_category', 'status', 'is_active')
    search_fields = ('full_name', 'phone', 'department', 'position')
    list_filter = ('status', 'is_active', 'work_category', 'department')


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)


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
