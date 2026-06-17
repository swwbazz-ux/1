from django.contrib import admin

from .models import DowntimeEvent, DowntimeReason


@admin.register(DowntimeReason)
class DowntimeReasonAdmin(admin.ModelAdmin):
    list_display = ('name', 'equipment_type', 'is_critical', 'is_active')
    search_fields = ('name',)
    list_filter = ('equipment_type', 'is_critical', 'is_active')


@admin.register(DowntimeEvent)
class DowntimeEventAdmin(admin.ModelAdmin):
    list_display = ('equipment', 'reason', 'employee', 'started_at', 'ended_at')
    search_fields = ('equipment__garage_number', 'reason__name', 'employee__full_name')
    list_filter = ('reason', 'equipment__equipment_type')

# Register your models here.
