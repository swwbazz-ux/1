from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from trips.models import Trip, TripStatus

from .models import (
    EmployeeShift,
    EquipmentPlanGroup,
    EquipmentShiftPlan,
    PlanAssignmentStatus,
    PlanCalculationMode,
    ShiftPlan,
    ShiftPlanScope,
)


def decimal_zero():
    return Decimal('0')


def plan_status_label(status):
    labels = {
        PlanAssignmentStatus.ASSIGNED: 'План назначен',
        PlanAssignmentStatus.NO_PLAN_GROUP: 'Нет группы плана',
        PlanAssignmentStatus.NO_ACTIVE_PLAN: 'Нет активного плана',
        'legacy_plan': 'План из старой схемы',
    }
    return labels.get(status or '', 'Нет плана')


def plan_unit_label(calculation_mode):
    if calculation_mode == PlanCalculationMode.TRIPS:
        return 'рейсов'
    if calculation_mode == PlanCalculationMode.VOLUME:
        return 'м³'
    if calculation_mode == PlanCalculationMode.TONNAGE:
        return 'т'
    return ''


def percent(fact, plan):
    if not plan or Decimal(plan) <= 0:
        return None
    return ((Decimal(fact or 0) / Decimal(plan)) * Decimal('100')).quantize(Decimal('0.1'))


def empty_progress(equipment, date=None, shift_type=None, *, status=PlanAssignmentStatus.NO_PLAN_GROUP, shift=None):
    return {
        'equipment': equipment,
        'date': date,
        'shift_type': shift_type,
        'shift': shift,
        'plan': None,
        'plan_group': None,
        'plan_group_name': '',
        'plan_status': status,
        'calculation_mode': '',
        'plan_value': None,
        'trip_count': 0,
        'volume_m3': Decimal('0'),
        'tonnage': Decimal('0'),
        'progress_percent': None,
    }


def shift_plan_totals_by_shift(date):
    totals = defaultdict(lambda: {
        'trips': 0,
        'volume_m3': decimal_zero(),
        'tonnage': decimal_zero(),
    })

    snapshot_shifts = (
        EmployeeShift.objects
        .filter(opened_at__date=date, plan_status=PlanAssignmentStatus.ASSIGNED, plan_value__isnull=False)
    )
    snapshot_found = False
    for shift in snapshot_shifts:
        snapshot_found = True
        if shift.plan_calculation_mode == PlanCalculationMode.TRIPS:
            totals[shift.shift_type]['trips'] += int(shift.plan_value or 0)
        elif shift.plan_calculation_mode == PlanCalculationMode.VOLUME:
            totals[shift.shift_type]['volume_m3'] += shift.plan_value or Decimal('0')
        elif shift.plan_calculation_mode == PlanCalculationMode.TONNAGE:
            totals[shift.shift_type]['tonnage'] += shift.plan_value or Decimal('0')
    if snapshot_found:
        return totals

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


def get_equipment_plan_group(equipment):
    if not equipment:
        return None
    return (
        EquipmentPlanGroup.objects
        .filter(equipment=equipment)
        .prefetch_related('equipment')
        .order_by('-is_active', '-active_from', 'name')
        .first()
    )


def assign_shift_plan_snapshot(shift):
    if not shift or not shift.equipment_id:
        if shift:
            shift.plan_assigned_at = timezone.now()
            shift.plan_group = None
            shift.plan_group_name = ''
            shift.plan_status = PlanAssignmentStatus.NO_PLAN_GROUP
            shift.plan_calculation_mode = ''
            shift.plan_value = None
            shift.save(update_fields=[
                'plan_group',
                'plan_group_name',
                'plan_calculation_mode',
                'plan_value',
                'plan_assigned_at',
                'plan_status',
            ])
        return empty_progress(None, shift=shift)

    group = get_equipment_plan_group(shift.equipment)
    shift_date = timezone.localtime(shift.opened_at).date() if shift.opened_at else timezone.localdate()
    shift.plan_assigned_at = timezone.now()
    shift.plan_group = group
    shift.plan_group_name = group.name if group else ''

    if not group:
        shift.plan_status = PlanAssignmentStatus.NO_PLAN_GROUP
        shift.plan_calculation_mode = ''
        shift.plan_value = None
    elif (
        not group.is_active
        or (group.active_from and group.active_from > shift_date)
        or not group.plan_value
        or Decimal(group.plan_value) <= 0
    ):
        shift.plan_status = PlanAssignmentStatus.NO_ACTIVE_PLAN
        shift.plan_calculation_mode = group.calculation_mode
        shift.plan_value = None
    else:
        shift.plan_status = PlanAssignmentStatus.ASSIGNED
        shift.plan_calculation_mode = group.calculation_mode
        shift.plan_value = group.plan_value

    shift.save(update_fields=[
        'plan_group',
        'plan_group_name',
        'plan_calculation_mode',
        'plan_value',
        'plan_assigned_at',
        'plan_status',
    ])
    return calculate_open_shift_progress(shift)


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


def aggregate_trip_facts(trips):
    facts = trips.aggregate(
        trip_count=Count('id'),
        volume_m3=Sum('volume_m3'),
        tonnage=Sum('tonnage'),
    )
    return {
        'trip_count': facts['trip_count'] or 0,
        'volume_m3': facts['volume_m3'] or Decimal('0'),
        'tonnage': facts['tonnage'] or Decimal('0'),
    }


def calculate_progress_from_snapshot(shift, trips):
    facts = aggregate_trip_facts(trips)
    date = timezone.localtime(shift.opened_at).date() if shift and shift.opened_at else None
    result = {
        'equipment': shift.equipment if shift else None,
        'date': date,
        'shift_type': shift.shift_type if shift else None,
        'shift': shift,
        'plan': None,
        'plan_group': shift.plan_group if shift else None,
        'plan_group_name': shift.plan_group_name if shift else '',
        'plan_status': shift.plan_status or PlanAssignmentStatus.NO_PLAN_GROUP,
        'calculation_mode': shift.plan_calculation_mode or '',
        'plan_value': shift.plan_value,
        **facts,
        'progress_percent': None,
    }

    if result['plan_status'] != PlanAssignmentStatus.ASSIGNED:
        return result

    if shift.plan_calculation_mode == PlanCalculationMode.TRIPS:
        progress_percent = percent(result['trip_count'], shift.plan_value)
    elif shift.plan_calculation_mode == PlanCalculationMode.TONNAGE:
        progress_percent = percent(result['tonnage'], shift.plan_value)
    else:
        progress_percent = percent(result['volume_m3'], shift.plan_value)
    result['progress_percent'] = progress_percent
    return result


def calculate_progress_from_trip_queryset(equipment, date, shift_type, trips):
    plan = get_equipment_shift_plan(equipment, date, shift_type)
    facts = aggregate_trip_facts(trips)
    trip_count = facts['trip_count']
    volume_m3 = facts['volume_m3']
    tonnage = facts['tonnage']

    if not plan:
        return {
            'equipment': equipment,
            'date': date,
            'shift_type': shift_type,
            'shift': None,
            'plan': None,
            'plan_group': None,
            'plan_group_name': '',
            'plan_status': PlanAssignmentStatus.NO_ACTIVE_PLAN,
            'calculation_mode': '',
            'plan_value': None,
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
        'shift': None,
        'plan': plan,
        'plan_group': None,
        'plan_group_name': '',
        'plan_status': 'legacy_plan',
        'calculation_mode': plan.calculation_mode,
        'plan_value': (
            plan.plan_trips
            if plan.calculation_mode == PlanCalculationMode.TRIPS
            else plan.plan_tonnage
            if plan.calculation_mode == PlanCalculationMode.TONNAGE
            else plan.plan_volume_m3
        ),
        'trip_count': trip_count,
        'volume_m3': volume_m3,
        'tonnage': tonnage,
        'progress_percent': progress_percent,
    }


def calculate_equipment_shift_progress(equipment, date, shift_type):
    trips = equipment_shift_trip_queryset(equipment, date, shift_type)
    return calculate_progress_from_trip_queryset(equipment, date, shift_type, trips)


def calculate_truck_progress_for_excavator_shift(truck, excavator_shift):
    if not truck or not excavator_shift:
        return None
    truck_shift = (
        EmployeeShift.objects
        .filter(equipment=truck, closed_at__isnull=True)
        .select_related('equipment', 'equipment__equipment_type', 'plan_group')
        .order_by('-opened_at')
        .first()
    )
    if truck_shift:
        trips = Trip.objects.filter(
            status=TripStatus.COMPLETED,
            truck=truck,
            unloading_shift=truck_shift,
        )
        return calculate_progress_from_snapshot(truck_shift, trips)

    date = timezone.localtime(excavator_shift.opened_at).date()
    shift_type = excavator_shift.shift_type
    trips = Trip.objects.filter(
        status=TripStatus.COMPLETED,
        truck=truck,
        loading_shift=excavator_shift,
    )
    return calculate_progress_from_trip_queryset(truck, date, shift_type, trips)


def calculate_open_shift_progress(open_shift):
    if not open_shift:
        return None
    if open_shift.plan_status:
        if equipment_is_excavator(open_shift.equipment):
            trips = Trip.objects.filter(status=TripStatus.COMPLETED, loading_shift=open_shift)
        else:
            trips = Trip.objects.filter(status=TripStatus.COMPLETED, unloading_shift=open_shift)
        return calculate_progress_from_snapshot(open_shift, trips)

    return calculate_equipment_shift_progress(
        open_shift.equipment,
        timezone.localtime(open_shift.opened_at).date(),
        open_shift.shift_type,
    )
