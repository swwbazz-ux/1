from django.contrib import admin

from .forms import EquipmentPlanGroupForm
from .models import AchievementPrize, AchievementUnlock, DriverShiftAction, EmployeeShift, EquipmentPlanGroup, EquipmentShiftPlan, ShiftPlan, ShiftReadingCorrection, WatchPeriod


@admin.register(WatchPeriod)
class WatchPeriodAdmin(admin.ModelAdmin):
    list_display = ('name', 'starts_on', 'ends_on', 'is_active')
    search_fields = ('name',)
    list_filter = ('is_active',)


@admin.register(EmployeeShift)
class EmployeeShiftAdmin(admin.ModelAdmin):
    list_display = ('employee', 'shift_type', 'equipment', 'plan_group_name', 'plan_calculation_mode', 'plan_value', 'plan_status', 'opened_at', 'closed_at', 'is_service_closed')
    search_fields = ('employee__full_name',)
    list_filter = ('shift_type', 'watch_period', 'equipment', 'plan_status', 'plan_group', 'is_service_closed')


@admin.register(DriverShiftAction)
class DriverShiftActionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action_type', 'client_action_id', 'shift', 'actor')
    search_fields = ('client_action_id', 'actor__full_name', 'shift__equipment__garage_number')
    readonly_fields = ('created_at',)


@admin.register(ShiftReadingCorrection)
class ShiftReadingCorrectionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'equipment', 'driver', 'field_name', 'inherited_value', 'corrected_value', 'shift')
    search_fields = ('equipment__garage_number', 'driver__full_name', 'field_name')
    readonly_fields = ('created_at',)


@admin.register(AchievementPrize)
class AchievementPrizeAdmin(admin.ModelAdmin):
    list_display = ('title', 'is_active', 'updated_at')
    search_fields = ('title',)
    list_filter = ('is_active',)


@admin.register(AchievementUnlock)
class AchievementUnlockAdmin(admin.ModelAdmin):
    list_display = ('user', 'equipment', 'employee_shift', 'prize', 'percent_at_unlock', 'unlocked_at', 'shown_at')
    search_fields = ('user__full_name', 'equipment__garage_number', 'prize__title')
    list_filter = ('prize', 'equipment', 'unlocked_at', 'shown_at')
    readonly_fields = ('unlocked_at',)


@admin.register(EquipmentPlanGroup)
class EquipmentPlanGroupAdmin(admin.ModelAdmin):
    form = EquipmentPlanGroupForm
    list_display = ('name', 'calculation_mode', 'plan_value', 'is_active', 'active_from', 'updated_by', 'updated_at')
    search_fields = ('name', 'code', 'comment', 'equipment__garage_number', 'equipment__model__name')
    list_filter = ('calculation_mode', 'is_active', 'active_from')
    filter_horizontal = ('equipment',)
    readonly_fields = ('updated_by', 'created_at', 'updated_at')

    def save_model(self, request, obj, form, change):
        employee = getattr(request, 'employee', None)
        if employee:
            obj.updated_by = employee
        super().save_model(request, obj, form, change)


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
