from django.contrib import admin

from .models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
    TruckCapacityRule,
)


@admin.register(EquipmentType)
class EquipmentTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')
    search_fields = ('name',)
    list_filter = ('is_active',)


@admin.register(EquipmentModel)
class EquipmentModelAdmin(admin.ModelAdmin):
    list_display = ('name', 'equipment_type', 'payload_tons', 'body_volume_m3', 'is_active')
    search_fields = ('name', 'equipment_type__name')
    list_filter = ('equipment_type', 'is_active')


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ('garage_number', 'equipment_type', 'model', 'vin', 'is_own', 'is_active')
    search_fields = ('garage_number', 'vin', 'model__name')
    list_filter = ('equipment_type', 'model', 'is_own', 'is_active')


@admin.register(RockType)
class RockTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'density', 'loosening_factor', 'is_active')
    search_fields = ('name',)
    list_filter = ('is_active',)


@admin.register(DumpPoint)
class DumpPointAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')
    search_fields = ('name',)
    list_filter = ('is_active',)


@admin.register(TruckCapacityRule)
class TruckCapacityRuleAdmin(admin.ModelAdmin):
    list_display = ('equipment_model', 'rock_type', 'volume_m3')
    search_fields = ('equipment_model__name', 'rock_type__name')
    list_filter = ('equipment_model', 'rock_type')


@admin.register(Dormitory)
class DormitoryAdmin(admin.ModelAdmin):
    list_display = ('number', 'is_active')
    search_fields = ('number',)
    list_filter = ('is_active',)


@admin.register(DormitoryBlock)
class DormitoryBlockAdmin(admin.ModelAdmin):
    list_display = ('dormitory', 'name')
    search_fields = ('dormitory__number', 'name')
    list_filter = ('dormitory',)


@admin.register(DormitorySection)
class DormitorySectionAdmin(admin.ModelAdmin):
    list_display = ('block', 'name', 'day_capacity', 'night_capacity')
    search_fields = ('block__dormitory__number', 'block__name', 'name')
    list_filter = ('block__dormitory',)

# Register your models here.
