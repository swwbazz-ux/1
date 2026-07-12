from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
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
    ShiftClientAction,
    ShiftReadingCorrection,
)


DRIVER_SHIFT_READING_FIELDS = (
    ('start_fuel', 'end_fuel', ShiftReadingCorrection.Metric.FUEL),
    ('start_mileage', 'end_mileage', ShiftReadingCorrection.Metric.MILEAGE),
    ('start_engine_hours', 'end_engine_hours', ShiftReadingCorrection.Metric.ENGINE_HOURS),
)


def validate_driver_fuel_reading(equipment, value):
    if value is None:
        raise ValidationError('Укажите остаток топлива.')
    if value < 0:
        raise ValidationError('Остаток топлива не может быть отрицательным.')
    limit = getattr(getattr(equipment, 'model', None), 'fuel_capacity_limit_l', None)
    if limit is None:
        raise ValidationError('Для модели этого самосвала не настроен максимальный остаток топлива.')
    if value > limit:
        raise ValidationError(f'Остаток топлива не может превышать {limit:g} л для этой модели.')


def validate_driver_close_readings(shift, *, end_fuel, end_mileage, end_engine_hours):
    validate_driver_fuel_reading(shift.equipment, end_fuel)
    errors = {}
    if shift.start_mileage is None:
        errors['end_mileage'] = 'В открытой смене отсутствует начальное показание одометра. Обратитесь к диспетчеру.'
    elif end_mileage is None:
        errors['end_mileage'] = 'Укажите одометр на конец смены.'
    elif end_mileage < shift.start_mileage:
        errors['end_mileage'] = 'Одометр на конец смены не может быть меньше показания на начало.'
    elif end_mileage - shift.start_mileage > Decimal('250'):
        errors['end_mileage'] = 'Пробег за смену не может превышать 250 км. Проверьте показания.'
    if shift.start_engine_hours is None:
        errors['end_engine_hours'] = 'В открытой смене отсутствует начальное показание моточасов. Обратитесь к диспетчеру.'
    elif end_engine_hours is None:
        errors['end_engine_hours'] = 'Укажите моточасы на конец смены.'
    elif end_engine_hours < shift.start_engine_hours:
        errors['end_engine_hours'] = 'Моточасы на конец смены не могут быть меньше показания на начало.'
    elif end_engine_hours - shift.start_engine_hours > Decimal('12'):
        errors['end_engine_hours'] = 'Моточасы за смену не могут увеличиться более чем на 12. Проверьте показания.'
    if errors:
        raise ValidationError(errors)


def _existing_driver_shift_action(action_type, client_action_id):
    action = ShiftClientAction.objects.select_related('shift').filter(
        action_type=action_type,
        client_action_id=client_action_id,
    ).first()
    return action.shift if action else None


def open_driver_shift(*, employee, work_assignment, readings, client_action_id):
    existing_shift = _existing_driver_shift_action('driver_shift_opened', client_action_id)
    if existing_shift:
        return existing_shift, False
    from references.models import Equipment
    from users.models import Employee
    try:
        with transaction.atomic():
            Employee.objects.select_for_update().get(pk=employee.pk)
            equipment = Equipment.objects.select_for_update().select_related('model').get(pk=work_assignment.equipment_id)
            existing_shift = _existing_driver_shift_action('driver_shift_opened', client_action_id)
            if existing_shift:
                return existing_shift, False
            if EmployeeShift.objects.filter(employee=employee, closed_at__isnull=True).exists():
                raise ValidationError('У этого водителя уже открыта смена.')
            if EmployeeShift.objects.filter(equipment=equipment, closed_at__isnull=True).exists():
                raise ValidationError('Смена по этому самосвалу уже открыта другим водителем.')
            validate_driver_fuel_reading(equipment, readings['start_fuel'])
            previous_shift = EmployeeShift.objects.filter(
                equipment=equipment, closed_at__isnull=False,
            ).order_by('-closed_at').first()
            shift = EmployeeShift.objects.create(
                employee=employee,
                opened_by=employee,
                shift_type=work_assignment.shift_type,
                equipment=equipment,
                opened_at=timezone.now(),
                **readings,
            )
            assign_shift_plan_snapshot(shift)
            corrections = []
            if previous_shift:
                for start_field, end_field, metric in DRIVER_SHIFT_READING_FIELDS:
                    inherited = getattr(previous_shift, end_field)
                    actual = getattr(shift, start_field)
                    if inherited is not None and actual != inherited:
                        corrections.append(ShiftReadingCorrection(
                            equipment=equipment,
                            new_shift=shift,
                            previous_shift=previous_shift,
                            metric=metric,
                            transferred_value=inherited,
                            actual_value=actual,
                            employee=employee,
                        ))
                ShiftReadingCorrection.objects.bulk_create(corrections)
            response = {'ok': True, 'shift_id': shift.pk, 'truck_id': equipment.pk, 'driver_id': employee.pk}
            ShiftClientAction.objects.create(
                action_type='driver_shift_opened',
                client_action_id=client_action_id,
                employee=employee,
                shift=shift,
                response_payload=response,
            )
            from core.models import bump_operational_state
            if corrections:
                bump_operational_state(
                    'DriverShift:readings_corrected', event_type='shift_readings_corrected',
                    object_type='EmployeeShift', object_id=shift.pk,
                    payload={**response, 'previous_shift_id': previous_shift.pk, 'fields': [item.metric for item in corrections]},
                )
            bump_operational_state(
                'DriverShift:opened', event_type='driver_shift_opened', object_type='EmployeeShift', object_id=shift.pk,
                payload=response,
            )
            return shift, True
    except IntegrityError as error:
        existing_shift = _existing_driver_shift_action('driver_shift_opened', client_action_id)
        if existing_shift:
            return existing_shift, False
        raise ValidationError('Смена по этому самосвалу уже открыта другим водителем.') from error


def close_driver_shift(*, shift, employee, readings, client_action_id):
    existing_shift = _existing_driver_shift_action('driver_shift_closed', client_action_id)
    if existing_shift:
        return existing_shift, False
    from downtimes.models import DowntimeEvent
    from references.models import Equipment
    from trips.models import OPEN_TRIP_STATUSES
    from users.models import Employee
    with transaction.atomic():
        Employee.objects.select_for_update().get(pk=employee.pk)
        locked_shift = EmployeeShift.objects.select_for_update().select_related('equipment__model').get(pk=shift.pk)
        Equipment.objects.select_for_update().get(pk=locked_shift.equipment_id)
        existing_shift = _existing_driver_shift_action('driver_shift_closed', client_action_id)
        if existing_shift:
            return existing_shift, False
        if locked_shift.closed_at:
            raise ValidationError('Смена уже закрыта.')
        if Trip.objects.filter(truck=locked_shift.equipment, status__in=OPEN_TRIP_STATUSES).exists():
            raise ValidationError('Нельзя закрыть смену: у самосвала есть активный рейс.')
        if DowntimeEvent.objects.filter(equipment=locked_shift.equipment, ended_at__isnull=True).exists():
            raise ValidationError('Нельзя закрыть смену: у самосвала есть активный простой, ремонт или авария.')
        validate_driver_close_readings(locked_shift, **readings)
        for field, value in readings.items():
            setattr(locked_shift, field, value)
        locked_shift.closed_at = timezone.now()
        locked_shift.closed_by = employee
        locked_shift.save(update_fields=[*readings, 'closed_at', 'closed_by'])
        response = {
            'ok': True, 'shift_id': locked_shift.pk, 'truck_id': locked_shift.equipment_id, 'driver_id': employee.pk,
        }
        ShiftClientAction.objects.create(
            action_type='driver_shift_closed', client_action_id=client_action_id,
            employee=employee, shift=locked_shift, response_payload=response,
        )
        from core.models import bump_operational_state
        bump_operational_state(
            'DriverShift:closed', event_type='driver_shift_closed', object_type='EmployeeShift', object_id=locked_shift.pk,
            payload=response,
        )
        return locked_shift, True


def decimal_zero():
    return Decimal('0')


def plan_status_label(status):
    labels = {
        PlanAssignmentStatus.ASSIGNED: 'План назначен',
        PlanAssignmentStatus.NO_PLAN_GROUP: 'Нет группы плана',
        PlanAssignmentStatus.NO_ACTIVE_PLAN: 'Нет активного плана',
        'plan_not_assigned': 'План не назначен',
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


def format_progress_percent(value):
    if value is None:
        return None
    try:
        value = Decimal(value)
    except Exception:
        return None
    return int(max(Decimal('0'), value).quantize(Decimal('1')))


def progress_cycle_visual_context(percent):
    value = format_progress_percent(percent)
    if value is None:
        value = 0

    completed_loops, loop_progress = divmod(value, 100)
    # Keep an exact completed boundary visible as a finished active cycle.
    # This prevents the indicator from jumping from 99% to an empty ring.
    if value and loop_progress == 0:
        loop_progress = 100
        completed_loops = max(0, completed_loops - 1)

    if completed_loops == 0:
        phase = 'green'
    elif completed_loops == 1:
        phase = 'amber'
    elif completed_loops == 2:
        phase = 'cyan'
    else:
        phase = 'orange'
    return {
        'percent': value,
        'loop_progress': loop_progress,
        'completed_loops': completed_loops,
        'phase': phase,
        'has_completed_loops': completed_loops > 0,
        'is_overrun': completed_loops > 0,
        'text_class': '',
    }


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


def equipment_is_truck(equipment):
    return bool(equipment and equipment.equipment_type and 'самосвал' in equipment.equipment_type.name.lower())


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


def calculate_truck_shift_progress(truck, reference_shift=None):
    if not truck:
        return None
    if reference_shift and getattr(reference_shift, 'equipment_id', None) == getattr(truck, 'id', None):
        trips = Trip.objects.filter(
            status=TripStatus.COMPLETED,
            truck=truck,
            unloading_shift=reference_shift,
        )
        return calculate_progress_from_snapshot(reference_shift, trips)
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

    if reference_shift and reference_shift.opened_at and reference_shift.shift_type:
        date = timezone.localtime(reference_shift.opened_at).date()
        return calculate_equipment_shift_progress(truck, date, reference_shift.shift_type)
    return empty_progress(truck, status=PlanAssignmentStatus.NO_PLAN_GROUP)


def calculate_truck_progress_for_excavator_shift(truck, excavator_shift):
    return calculate_truck_shift_progress(truck, reference_shift=excavator_shift)


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


class ExcavatorShiftError(Exception):
    def __init__(self, message, *, field_errors=None, status=400, code='invalid_readings'):
        super().__init__(message)
        self.message = message
        self.field_errors = field_errors or {}
        self.status = status
        self.code = code


def parse_required_shift_decimal(value, label, field_name):
    raw = str(value if value is not None else '').strip().replace('\u00a0', '').replace(' ', '').replace(',', '.')
    if not raw:
        raise ExcavatorShiftError(
            f'{label}: укажите значение.',
            field_errors={field_name: f'Укажите значение «{label.lower()}».'},
        )
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        parsed = None
    if parsed is None or not parsed.is_finite():
        raise ExcavatorShiftError(
            f'{label}: укажите корректное число.',
            field_errors={field_name: 'Введите корректное число.'},
        )
    if parsed < 0:
        raise ExcavatorShiftError(
            f'{label}: значение не может быть меньше нуля.',
            field_errors={field_name: 'Значение не может быть меньше нуля.'},
        )
    try:
        return parsed.quantize(Decimal('0.01'))
    except InvalidOperation:
        raise ExcavatorShiftError(
            f'{label}: значение имеет недопустимый формат.',
            field_errors={field_name: 'Допустимо не более двух знаков после запятой.'},
        )


def excavator_fuel_limit(equipment):
    model = getattr(equipment, 'model', None)
    limit = getattr(model, 'fuel_capacity_limit_l', None)
    if limit is None:
        raise ExcavatorShiftError(
            'Для модели этого экскаватора не настроен допустимый объем топлива. Обратитесь к администратору.',
            field_errors={'fuel': 'Лимит топлива для модели не настроен.'},
            status=409,
            code='fuel_limit_not_configured',
        )
    return Decimal(limit)


def validate_excavator_shift_readings(equipment, fuel_value, engine_hours_value, *, opening_shift=None):
    fuel = parse_required_shift_decimal(fuel_value, 'Топливо', 'fuel')
    engine_hours = parse_required_shift_decimal(engine_hours_value, 'Моточасы', 'engine_hours')
    fuel_limit = excavator_fuel_limit(equipment)
    if fuel > fuel_limit:
        raise ExcavatorShiftError(
            f'Топливо не может превышать {int(fuel_limit)} л для модели этого экскаватора.',
            field_errors={'fuel': f'Максимум для этой модели: {int(fuel_limit)} л.'},
        )
    if opening_shift is not None:
        start_hours = opening_shift.start_engine_hours
        if start_hours is None:
            raise ExcavatorShiftError('В открытой смене отсутствуют начальные моточасы.', status=409)
        if engine_hours < start_hours:
            raise ExcavatorShiftError(
                'Конечные моточасы не могут быть меньше начальных.',
                field_errors={'engine_hours': f'Не меньше {start_hours}.'},
            )
        if engine_hours - start_hours > Decimal('12'):
            raise ExcavatorShiftError(
                'Моточасы за смену не могут увеличиться более чем на 12. Проверьте показания.',
                field_errors={'engine_hours': 'Допустимый прирост за смену: не более 12 моточасов.'},
            )
    return fuel, engine_hours


def existing_shift_action_payload(action_type, client_action_id):
    action = ShiftClientAction.objects.filter(
        action_type=action_type,
        client_action_id=client_action_id,
    ).first()
    if not action:
        return None
    payload = dict(action.response_payload or {})
    payload['deduplicated'] = True
    return payload


@transaction.atomic
def open_excavator_shift(*, employee, equipment, shift_type, fuel_value, engine_hours_value, client_action_id):
    from references.models import Equipment
    from users.models import Employee

    action_type = 'excavator_shift_opened'
    existing = existing_shift_action_payload(action_type, client_action_id)
    if existing:
        return existing

    Employee.objects.select_for_update().get(pk=employee.pk)
    equipment = Equipment.objects.select_for_update().select_related('model', 'equipment_type').get(pk=equipment.pk)
    existing = existing_shift_action_payload(action_type, client_action_id)
    if existing:
        return existing

    if EmployeeShift.objects.filter(employee=employee, closed_at__isnull=True).exists():
        raise ExcavatorShiftError(
            'У машиниста уже есть открытая смена.',
            status=409,
            code='employee_shift_already_open',
        )
    if EmployeeShift.objects.filter(equipment=equipment, closed_at__isnull=True).exists():
        raise ExcavatorShiftError(
            'Смена на этом экскаваторе уже открыта другим машинистом.',
            status=409,
            code='equipment_shift_already_open',
        )

    fuel, engine_hours = validate_excavator_shift_readings(equipment, fuel_value, engine_hours_value)
    previous_shift = (
        EmployeeShift.objects.select_for_update()
        .filter(equipment=equipment, closed_at__isnull=False)
        .order_by('-closed_at', '-opened_at')
        .first()
    )
    shift = EmployeeShift.objects.create(
        employee=employee,
        equipment=equipment,
        shift_type=shift_type,
        start_fuel=fuel,
        start_mileage=None,
        start_engine_hours=engine_hours,
        opened_at=timezone.now(),
        opened_by=employee,
    )
    shift_progress = assign_shift_plan_snapshot(shift)

    corrected_metrics = []
    if previous_shift:
        transferred_values = {
            ShiftReadingCorrection.Metric.FUEL: previous_shift.end_fuel,
            ShiftReadingCorrection.Metric.ENGINE_HOURS: previous_shift.end_engine_hours,
        }
        actual_values = {
            ShiftReadingCorrection.Metric.FUEL: fuel,
            ShiftReadingCorrection.Metric.ENGINE_HOURS: engine_hours,
        }
        for metric, transferred in transferred_values.items():
            if transferred is not None and transferred != actual_values[metric]:
                ShiftReadingCorrection.objects.create(
                    equipment=equipment,
                    new_shift=shift,
                    previous_shift=previous_shift,
                    metric=metric,
                    transferred_value=transferred,
                    actual_value=actual_values[metric],
                    employee=employee,
                )
                corrected_metrics.append(metric)

    if corrected_metrics:
        from core.models import bump_operational_state
        bump_operational_state(
            'excavator_shift_readings_corrected',
            event_type='excavator_shift_readings_corrected',
            object_type='EmployeeShift',
            object_id=shift.id,
            payload={
                'action': 'excavator_shift_readings_corrected',
                'shift_id': shift.id,
                'previous_shift_id': previous_shift.id,
                'equipment_id': equipment.id,
                'employee_id': employee.id,
                'metrics': corrected_metrics,
            },
        )

    response = {
        'ok': True,
        'action': action_type,
        'client_action_id': client_action_id,
        'shift_id': shift.id,
        'shift_open': True,
        'equipment_id': equipment.id,
        'plan_status': shift_progress.get('plan_status'),
        'plan_value': str(shift_progress.get('plan_value') or ''),
        'calculation_mode': shift_progress.get('calculation_mode') or '',
    }
    client_action = ShiftClientAction.objects.create(
        action_type=action_type,
        client_action_id=client_action_id,
        employee=employee,
        shift=shift,
        response_payload=response,
    )
    from core.models import bump_operational_state
    state = bump_operational_state(
        action_type,
        event_type=action_type,
        object_type='EmployeeShift',
        object_id=shift.id,
        payload={**response, 'employee_id': employee.id},
    )
    response['version'] = state.version
    client_action.response_payload = response
    client_action.save(update_fields=['response_payload'])
    return response


@transaction.atomic
def close_excavator_shift(*, employee, fuel_value, engine_hours_value, client_action_id):
    from trips.models import OPEN_TRIP_STATUSES, Trip
    from users.models import Employee

    action_type = 'excavator_shift_closed'
    existing = existing_shift_action_payload(action_type, client_action_id)
    if existing:
        return existing

    Employee.objects.select_for_update().get(pk=employee.pk)
    existing = existing_shift_action_payload(action_type, client_action_id)
    if existing:
        return existing
    shift = (
        EmployeeShift.objects.select_for_update()
        .select_related('equipment', 'equipment__model', 'equipment__equipment_type')
        .filter(employee=employee, closed_at__isnull=True)
        .order_by('-opened_at')
        .first()
    )
    if not shift:
        raise ExcavatorShiftError('Открытая смена уже закрыта.', status=409, code='shift_already_closed')

    fuel, engine_hours = validate_excavator_shift_readings(
        shift.equipment,
        fuel_value,
        engine_hours_value,
        opening_shift=shift,
    )
    shift.end_fuel = fuel
    shift.end_mileage = None
    shift.end_engine_hours = engine_hours
    shift.closed_at = timezone.now()
    shift.closed_by = employee
    shift.save(update_fields=['end_fuel', 'end_mileage', 'end_engine_hours', 'closed_at', 'closed_by'])
    Trip.objects.filter(loading_shift=shift, status__in=OPEN_TRIP_STATUSES).update(is_carryover=True)

    response = {
        'ok': True,
        'action': action_type,
        'client_action_id': client_action_id,
        'shift_id': shift.id,
        'shift_open': False,
    }
    client_action = ShiftClientAction.objects.create(
        action_type=action_type,
        client_action_id=client_action_id,
        employee=employee,
        shift=shift,
        response_payload=response,
    )
    from core.models import bump_operational_state
    state = bump_operational_state(
        action_type,
        event_type=action_type,
        object_type='EmployeeShift',
        object_id=shift.id,
        payload={**response, 'employee_id': employee.id, 'equipment_id': shift.equipment_id},
    )
    response['version'] = state.version
    client_action.response_payload = response
    client_action.save(update_fields=['response_payload'])
    return response
