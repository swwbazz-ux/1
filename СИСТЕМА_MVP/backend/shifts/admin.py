from django.contrib import admin

from .models import EmployeeShift, WatchPeriod


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

# Register your models here.
