from django.contrib import admin

from .models import (
    CrewPlan,
    CrewPlanSlot,
    EquipmentAssignment,
    ExcavatorPlacement,
    HaulAssignment,
    HaulAssignmentHandoff,
)


class CrewPlanSlotInline(admin.TabularInline):
    model = CrewPlanSlot
    extra = 0
    can_delete = False
    fields = (
        'equipment', 'shift_type', 'employee', 'secondary_employee',
        'baseline_employee', 'baseline_secondary_employee',
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(CrewPlan)
class CrewPlanAdmin(admin.ModelAdmin):
    list_display = (
        'work_date', 'role', 'revision', 'status', 'version',
        'updated_by', 'updated_at', 'published_by', 'published_at',
    )
    list_filter = ('status', 'role', 'work_date')
    search_fields = ('role__name', 'role__code')
    readonly_fields = (
        'work_date', 'role', 'revision', 'status', 'version',
        'created_by', 'updated_by', 'published_by', 'published_at',
        'created_at', 'updated_at',
    )
    inlines = (CrewPlanSlotInline,)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EquipmentAssignment)
class EquipmentAssignmentAdmin(admin.ModelAdmin):
    list_display = ('employee', 'role', 'equipment', 'shift_type', 'shift', 'status', 'assigned_by', 'assigned_at', 'ended_at', 'ended_by')
    search_fields = ('employee__full_name', 'equipment__garage_number')
    list_filter = ('status', 'role', 'shift_type', 'equipment__equipment_type')
    readonly_fields = (
        'employee', 'role', 'equipment', 'shift_type', 'shift', 'assigned_by',
        'status', 'assigned_at', 'accepted_at', 'ended_at', 'ended_by',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(HaulAssignment)
class HaulAssignmentAdmin(admin.ModelAdmin):
    list_display = ('truck', 'excavator', 'status', 'assigned_at', 'accepted_at', 'ended_at')
    search_fields = ('truck__garage_number', 'excavator__garage_number')
    list_filter = ('status',)


@admin.register(HaulAssignmentHandoff)
class HaulAssignmentHandoffAdmin(admin.ModelAdmin):
    list_display = (
        'truck', 'source_excavator', 'source_shift', 'target_assignment',
        'status', 'created_at', 'resolved_at', 'resolved_by_trip',
    )
    list_filter = ('status',)
    search_fields = (
        'truck__garage_number', 'source_excavator__garage_number',
        'source_shift__employee__full_name',
    )
    readonly_fields = (
        'truck', 'source_assignment', 'target_assignment', 'source_excavator',
        'source_shift', 'status', 'created_at', 'resolved_at',
        'resolved_by_trip',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ExcavatorPlacement)
class ExcavatorPlacementAdmin(admin.ModelAdmin):
    list_display = ('excavator', 'zone', 'changed_by', 'changed_at')
    search_fields = ('excavator__garage_number',)
    list_filter = ('zone',)

# Register your models here.
