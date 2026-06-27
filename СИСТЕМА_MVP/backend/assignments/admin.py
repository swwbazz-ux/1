from django.contrib import admin

from .models import EquipmentAssignment, ExcavatorPlacement, HaulAssignment


@admin.register(EquipmentAssignment)
class EquipmentAssignmentAdmin(admin.ModelAdmin):
    list_display = ('employee', 'equipment', 'shift', 'status', 'assigned_at', 'accepted_at', 'ended_at')
    search_fields = ('employee__full_name', 'equipment__garage_number')
    list_filter = ('status', 'equipment__equipment_type')


@admin.register(HaulAssignment)
class HaulAssignmentAdmin(admin.ModelAdmin):
    list_display = ('truck', 'excavator', 'status', 'assigned_at', 'accepted_at', 'ended_at')
    search_fields = ('truck__garage_number', 'excavator__garage_number')
    list_filter = ('status',)


@admin.register(ExcavatorPlacement)
class ExcavatorPlacementAdmin(admin.ModelAdmin):
    list_display = ('excavator', 'zone', 'changed_by', 'changed_at')
    search_fields = ('excavator__garage_number',)
    list_filter = ('zone',)

# Register your models here.
