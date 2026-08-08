from django.contrib import admin

from .models import PhysicalBed, PhysicalRoom


class PhysicalBedInline(admin.TabularInline):
    model = PhysicalBed
    extra = 0
    readonly_fields = ('stable_id',)


@admin.register(PhysicalRoom)
class PhysicalRoomAdmin(admin.ModelAdmin):
    list_display = (
        'dormitory',
        'floor',
        'number',
        'room_type',
        'transfer_status',
        'sex_restriction',
        'capacity',
        'corridor_side',
        'side_position',
    )
    list_filter = ('dormitory', 'floor', 'room_type', 'transfer_status', 'sex_restriction')
    search_fields = ('dormitory__number', 'number', 'beds__stable_id')
    inlines = (PhysicalBedInline,)


@admin.register(PhysicalBed)
class PhysicalBedAdmin(admin.ModelAdmin):
    list_display = ('stable_id', 'room', 'block', 'position', 'is_available')
    list_filter = ('room__dormitory', 'room__floor', 'room__transfer_status', 'block')
    search_fields = ('stable_id', 'room__dormitory__number', 'room__number')
    readonly_fields = ('stable_id',)
