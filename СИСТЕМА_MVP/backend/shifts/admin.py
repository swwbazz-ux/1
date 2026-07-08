from django.contrib import admin

from .models import EmployeeShift, EquipmentShiftPlan, ShiftPlan, WatchPeriod


@admin.register(WatchPeriod)
class WatchPeriodAdmin(admin.ModelAdmin):
    list_display = ('name', 'starts_on', 'ends_on', 'is_active')
    search_fields = ('name',)
    list_filter = ('is_active',)


@admin.register(EmployeeShift)
class EmployeeShiftAdmin(admin.ModelAdmin):
    list_display = ('employee', 'shift_type', 'equipment', 'watch_period', 'opened_at', 'closed_at', 'is_service_closed')
    search_fields = ('employee__full_name',)
    list_filter = ('shift_type', 'watch_period', 'equipment', 'is_service_closed')


@admin.register(ShiftPlan)
class ShiftPlanAdmin(admin.ModelAdmin):
    list_display = ('date', 'plan_scope', 'name', 'plan_volume_m3', 'is_active')
    search_fields = ('name', 'comment')
    list_filter = ('plan_scope', 'is_active', 'date')


@admin.register(EquipmentShiftPlan)
class EquipmentShiftPlanAdmin(admin.ModelAdmin):
    list_display = ('shift_plan', 'equipment', 'employee', 'calculation_mode', 'plan_trips', 'plan_volume_m3', 'is_active')
    search_fields = ('equipment__garage_number', 'employee__full_name', 'comment')
    list_filter = ('shift_plan__date', 'shift_plan__shift_type', 'calculation_mode', 'is_active', 'equipment__equipment_type')
