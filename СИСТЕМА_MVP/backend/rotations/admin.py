from django.contrib import admin

from .models import (
    RotationActionLog,
    RotationCollectionCycle,
    RotationResponse,
    WatchExtensionCase,
)


@admin.register(RotationCollectionCycle)
class RotationCollectionCycleAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'target_watch_period',
        'status',
        'revision',
        'response_deadline',
        'opened_at',
        'closed_at',
    )
    list_filter = ('status', 'target_watch_period')
    search_fields = ('name', 'target_watch_period__name')
    list_select_related = ('target_watch_period', 'created_by', 'opened_by', 'closed_by')
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'response_deadline'


@admin.register(RotationResponse)
class RotationResponseAdmin(admin.ModelAdmin):
    list_display = (
        'snapshot_full_name',
        'cycle',
        'state',
        'intent',
        'next_shift_type',
        'departure_on',
        'arrival_on',
        'submitted_at',
    )
    list_filter = (
        'state',
        'intent',
        'next_shift_type',
        'shift_source',
        'travel_mode',
        'transfer_mode',
        'cycle',
    )
    search_fields = (
        'snapshot_full_name',
        'snapshot_personnel_number',
        'employee__full_name',
        'route_text',
        'transport_details',
    )
    list_select_related = ('cycle', 'employee', 'submitted_by')
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'


@admin.register(WatchExtensionCase)
class WatchExtensionCaseAdmin(admin.ModelAdmin):
    list_display = (
        'response',
        'extension_start',
        'extension_end',
        'decision_status',
        'decision_by',
        'documentation_status',
        'documentation_by',
    )
    list_filter = ('decision_status', 'documentation_status', 'extension_start', 'extension_end')
    search_fields = (
        'response__snapshot_full_name',
        'response__snapshot_personnel_number',
        'decision_comment',
        'documentation_note',
    )
    list_select_related = ('response', 'decision_by', 'documentation_by')
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'


@admin.register(RotationActionLog)
class RotationActionLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action_code', 'cycle', 'response', 'extension_case', 'actor')
    list_filter = ('action_code', 'created_at')
    search_fields = (
        'action_code',
        'cycle__name',
        'response__snapshot_full_name',
        'actor__full_name',
    )
    list_select_related = ('cycle', 'response', 'extension_case', 'actor')
    readonly_fields = (
        'cycle',
        'response',
        'extension_case',
        'actor',
        'action_code',
        'details',
        'created_at',
    )
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
