from django.contrib import admin

from .forms import EquipmentPlanGroupForm
from .models import AchievementPrize, AchievementUnlock, EmployeeShift, EquipmentPlanGroup, EquipmentShiftPlan, ShiftPlan, WatchPeriod


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
