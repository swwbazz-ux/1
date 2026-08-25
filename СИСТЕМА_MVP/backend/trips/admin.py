from django.contrib import admin

from .models import (
    DispatcherActionLog,
    InterventionImpact,
    InterventionReviewEvent,
    InterventionStateTransition,
    OperationalIntervention,
    Trip,
    TripClientAction,
)


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


class InterventionImpactInline(admin.TabularInline):
    model = InterventionImpact
    extra = 0
    can_delete = False
    readonly_fields = ('metric_code', 'value', 'unit', 'status', 'correction_for', 'accepted_at', 'booked_at', 'corrected_at', 'created_at')


class InterventionReviewEventInline(admin.TabularInline):
    model = InterventionReviewEvent
    extra = 0
    can_delete = False
    readonly_fields = ('event_type', 'actor', 'channel', 'comment', 'created_at')


class InterventionStateTransitionInline(admin.TabularInline):
    model = InterventionStateTransition
    extra = 0
    can_delete = False
    readonly_fields = ('contour', 'impact', 'from_status', 'to_status', 'actor', 'comment', 'created_at')


@admin.register(OperationalIntervention)
class OperationalInterventionAdmin(admin.ModelAdmin):
    list_display = (
        'recorded_at',
        'action_type',
        'source',
        'equipment',
        'subject_employee',
        'actor',
        'operational_status',
        'review_status',
    )
    list_filter = ('source', 'action_type', 'operational_status', 'review_status')
    search_fields = (
        'equipment__garage_number',
        'subject_employee__full_name',
        'actor__full_name',
        'reason',
        'comment',
    )
    readonly_fields = tuple(field.name for field in OperationalIntervention._meta.fields)
    inlines = (InterventionImpactInline, InterventionReviewEventInline, InterventionStateTransitionInline)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
