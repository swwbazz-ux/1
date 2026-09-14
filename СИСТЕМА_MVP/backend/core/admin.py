from django.contrib import admin

from .models import OfflineFieldEvent, OfflineFieldEventConflict


@admin.register(OfflineFieldEvent)
class OfflineFieldEventAdmin(admin.ModelAdmin):
    """Read-only operational receipt ledger, including events requiring review."""

    list_display = (
        'event_id', 'event_type', 'status', 'occurred_at', 'received_at',
        'actor', 'equipment', 'trip', 'error_code',
    )
    list_filter = ('status', 'event_type', 'role_code')
    search_fields = ('event_id', 'device_id', 'actor__full_name', 'trip__truck__garage_number')
    readonly_fields = (
        'event_id', 'event_type', 'format_version', 'actor', 'access', 'role_code',
        'device_id', 'sequence', 'depends_on', 'occurred_at', 'received_at',
        'shift', 'equipment', 'trip', 'local_trip_id', 'local_downtime_id',
        'context_snapshot', 'payload', 'fingerprint', 'status', 'retryable',
        'error_code', 'error_message', 'result_payload', 'created_at', 'updated_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {'GET', 'HEAD'}


@admin.register(OfflineFieldEventConflict)
class OfflineFieldEventConflictAdmin(admin.ModelAdmin):
    list_display = ('received_at', 'code', 'attempted_event_id', 'actor', 'role_code', 'device_id', 'existing_event')
    list_filter = ('code', 'role_code')
    search_fields = ('attempted_event_id', 'actor__full_name', 'device_id')
    readonly_fields = [field.name for field in OfflineFieldEventConflict._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {'GET', 'HEAD'}
