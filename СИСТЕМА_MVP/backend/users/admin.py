from django.contrib import admin

from .models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'personnel_number', 'phone', 'is_active')
    search_fields = ('full_name', 'personnel_number', 'phone')
    list_filter = ('is_active',)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)


@admin.register(EmployeeAccess)
class EmployeeAccessAdmin(admin.ModelAdmin):
    list_display = ('employee', 'role', 'is_active', 'created_at', 'deactivated_at')
    search_fields = ('employee__full_name', 'role__name', 'access_code')
    list_filter = ('role', 'is_active')


@admin.register(DriverPrimaryRegistration)
class DriverPrimaryRegistrationAdmin(admin.ModelAdmin):
    list_display = ('employee', 'shift_type', 'truck', 'dormitory_section', 'created_at')
    search_fields = ('employee__full_name', 'truck__garage_number')
    list_filter = ('shift_type', 'truck', 'dormitory_section')

# Register your models here.
