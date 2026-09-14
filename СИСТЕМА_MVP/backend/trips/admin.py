from django.contrib import admin

from .models import DispatcherActionLog, FreeBucketAcceptance, Trip, TripClientAction


@admin.register(FreeBucketAcceptance)
class FreeBucketAcceptanceAdmin(admin.ModelAdmin):
    list_display = ('truck', 'excavator', 'operator', 'status', 'occurred_at', 'used_trip', 'closed_at')
    list_filter = ('status', 'excavator')
    search_fields = ('truck__garage_number', 'excavator__garage_number', 'operator__full_name', 'client_acceptance_id')
    readonly_fields = (
        'client_acceptance_id', 'truck', 'excavator', 'operator', 'loading_shift',
        'primary_assignment', 'status', 'occurred_at', 'received_at', 'cancelled_at',
        'used_at', 'closed_at', 'used_trip',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {'GET', 'HEAD'}


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
        'cancelled_at',
    )
    search_fields = ('truck__garage_number', 'excavator__garage_number', 'driver__full_name', 'excavator_operator__full_name')
    list_filter = ('status', 'rock_type', 'dump_point', 'is_carryover')
    readonly_fields = ('cancelled_at',)


@admin.register(DispatcherActionLog)
class DispatcherActionLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'actor', 'action_type', 'target_summary', 'reason')
    search_fields = ('actor__full_name', 'target_summary', 'reason')
    list_filter = ('action_type',)


@admin.register(TripClientAction)
class TripClientActionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action_type', 'client_action_id', 'trip', 'actor')
    search_fields = ('action_type', 'client_action_id', 'trip__truck__garage_number', 'actor__full_name')
    list_filter = ('action_type',)
