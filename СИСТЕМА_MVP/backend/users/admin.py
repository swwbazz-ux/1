from django.contrib import admin

from .models import AdminActionLog, AdminConflict, DriverPrimaryRegistration, Employee, EmployeeAccess, Role


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'personnel_number', 'phone', 'status', 'is_active')
    search_fields = ('full_name', 'personnel_number', 'phone')
    list_filter = ('status', 'is_active')


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
    list_display = ('created_at', 'actor', 'action', 'object_type', 'object_repr')
    search_fields = ('actor__full_name', 'action', 'object_repr', 'comment')
    list_filter = ('action', 'object_type')
    readonly_fields = ('created_at',)


@admin.register(AdminConflict)
class AdminConflictAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'conflict_type', 'employee', 'status', 'resolved_by', 'resolved_at')
    search_fields = ('employee__full_name', 'conflict_type', 'description', 'comment')
    list_filter = ('status', 'conflict_type')

# Register your models here.
