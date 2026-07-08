from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from trips.models import Trip, TripStatus

from .models import EquipmentShiftPlan, PlanCalculationMode, ShiftPlan, ShiftPlanScope


def decimal_zero():
    return Decimal('0')


def percent(fact, plan):
    if not plan:
        return None
    return min(Decimal('100'), ((Decimal(fact or 0) / Decimal(plan)) * Decimal('100')).quantize(Decimal('0.1')))


def shift_plan_totals_by_shift(date):
    totals = defaultdict(lambda: {
        'trips': 0,
        'volume_m3': decimal_zero(),
        'tonnage': decimal_zero(),
    })
    plans = (
        ShiftPlan.objects
        .filter(
            date=date,
            is_active=True,
            plan_scope__in=[ShiftPlanScope.DAY_SHIFT, ShiftPlanScope.NIGHT_SHIFT],
        )
        .prefetch_related('equipment_plans')
    )
    for shift_plan in plans:
        equipment_plans = [
            item
            for item in shift_plan.equipment_plans.all()
            if item.is_active
        ]
        if equipment_plans:
            totals[shift_plan.shift_type]['trips'] += sum(item.plan_trips or 0 for item in equipment_plans)
            totals[shift_plan.shift_type]['volume_m3'] += sum((item.plan_volume_m3 or 0 for item in equipment_plans), Decimal('0'))
            totals[shift_plan.shift_type]['tonnage'] += sum((item.plan_tonnage or 0 for item in equipment_plans), Decimal('0'))
            continue

        totals[shift_plan.shift_type]['trips'] += shift_plan.plan_trips or 0
        totals[shift_plan.shift_type]['volume_m3'] += shift_plan.plan_volume_m3 or Decimal('0')
        totals[shift_plan.shift_type]['tonnage'] += shift_plan.plan_tonnage or Decimal('0')
    return totals


def shift_plan_totals(date):
    by_shift = shift_plan_totals_by_shift(date)
    return {
        'trips': sum(item['trips'] for item in by_shift.values()),
        'volume_m3': sum((item['volume_m3'] for item in by_shift.values()), Decimal('0')),
        'tonnage': sum((item['tonnage'] for item in by_shift.values()), Decimal('0')),
        'by_shift': by_shift,
    }


def get_equipment_shift_plan(equipment, date, shift_type):
    if not equipment or not date or not shift_type:
        return None
    return (
        EquipmentShiftPlan.objects
        .select_related('shift_plan', 'equipment', 'employee')
        .filter(
            equipment=equipment,
            is_active=True,
            shift_plan__date=date,
            shift_plan__shift_type=shift_type,
            shift_plan__is_active=True,
        )
        .first()
    )


def equipment_is_excavator(equipment):
    return bool(equipment and equipment.equipment_type and 'экскаватор' in equipment.equipment_type.name.lower())


def equipment_shift_trip_queryset(equipment, date, shift_type):
    if not equipment or not date or not shift_type:
        return Trip.objects.none()

    shift_filter = {
        'shift_type': shift_type,
        'opened_at__date': date,
    }
    if equipment_is_excavator(equipment):
        query = Q(loading_shift__equipment=equipment, **{f'loading_shift__{key}': value for key, value in shift_filter.items()})
    else:
        query = Q(unloading_shift__equipment=equipment, **{f'unloading_shift__{key}': value for key, value in shift_filter.items()})
    return Trip.objects.filter(status=TripStatus.COMPLETED).filter(query)


def calculate_equipment_shift_progress(equipment, date, shift_type):
    plan = get_equipment_shift_plan(equipment, date, shift_type)
    trips = equipment_shift_trip_queryset(equipment, date, shift_type)
    facts = trips.aggregate(
        trip_count=Count('id'),
        volume_m3=Sum('volume_m3'),
        tonnage=Sum('tonnage'),
    )
    trip_count = facts['trip_count'] or 0
    volume_m3 = facts['volume_m3'] or Decimal('0')
    tonnage = facts['tonnage'] or Decimal('0')

    if not plan:
        return {
            'equipment': equipment,
            'date': date,
            'shift_type': shift_type,
            'plan': None,
            'trip_count': trip_count,
            'volume_m3': volume_m3,
            'tonnage': tonnage,
            'progress_percent': None,
        }

    trip_percent = percent(trip_count, plan.plan_trips)
    volume_percent = percent(volume_m3, plan.plan_volume_m3)
    tonnage_percent = percent(tonnage, plan.plan_tonnage)
    if plan.calculation_mode == PlanCalculationMode.TRIPS:
        progress_percent = trip_percent
    elif plan.calculation_mode == PlanCalculationMode.TONNAGE:
        progress_percent = tonnage_percent
    elif plan.calculation_mode == PlanCalculationMode.MIXED:
        available = [value for value in (trip_percent, volume_percent, tonnage_percent) if value is not None]
        progress_percent = (sum(available, Decimal('0')) / len(available)).quantize(Decimal('0.1')) if available else None
    else:
        progress_percent = volume_percent

    return {
        'equipment': equipment,
        'date': date,
        'shift_type': shift_type,
        'plan': plan,
        'trip_count': trip_count,
        'volume_m3': volume_m3,
        'tonnage': tonnage,
        'progress_percent': progress_percent,
    }


def calculate_open_shift_progress(open_shift):
    if not open_shift:
        return None
    return calculate_equipment_shift_progress(
        open_shift.equipment,
        timezone.localtime(open_shift.opened_at).date(),
        open_shift.shift_type,
    )
