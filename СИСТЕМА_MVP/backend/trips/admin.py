from django.contrib import admin

from .models import DispatcherActionLog, Trip


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = (
        'truck',
        'excavator',
        'rock_type',
        'dump_point',
        'loading_horizon',
        'loading_block',
        'status',
        'planned_volume_m3',
        'volume_m3',
        'tonnage',
        'is_carryover',
        'created_at',
        'completed_at',
    )
    search_fields = ('truck__garage_number', 'excavator__garage_number', 'driver__full_name', 'excavator_operator__full_name')
    list_filter = ('status', 'rock_type', 'dump_point', 'is_carryover')


@admin.register(DispatcherActionLog)
class DispatcherActionLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'actor', 'action_type', 'target_summary', 'reason')
    search_fields = ('actor__full_name', 'target_summary', 'reason')
    list_filter = ('action_type',)
