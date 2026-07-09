from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction

from references.models import Equipment

from .models import EquipmentPlanGroup, PlanCalculationMode


DEFAULT_EQUIPMENT_PLAN_GROUPS = {
    'belaz_trucks': {
        'name': 'Самосвалы БелАЗ',
        'calculation_mode': PlanCalculationMode.TRIPS,
        'comment': 'Стартовая группа для ежесменного плана самосвалов БелАЗ.',
    },
    'nhl_trucks': {
        'name': 'Самосвалы NHL',
        'calculation_mode': PlanCalculationMode.TRIPS,
        'comment': 'Стартовая группа для ежесменного плана самосвалов NHL.',
    },
    'excavators_4000': {
        'name': 'Экскаваторы 4000',
        'calculation_mode': PlanCalculationMode.VOLUME,
        'comment': 'Стартовая группа для экскаваторов 1 и 8.',
    },
    'excavators_3000': {
        'name': 'Экскаваторы 3000',
        'calculation_mode': PlanCalculationMode.VOLUME,
        'comment': 'Стартовая группа для остальных экскаваторов.',
    },
}

EXCAVATOR_4000_NUMBERS = {1, 8}


def _casefold(value):
    return str(value or '').casefold()


def _equipment_type_name(equipment):
    return _casefold(getattr(getattr(equipment, 'equipment_type', None), 'name', ''))


def _equipment_model_name(equipment):
    return _casefold(getattr(getattr(equipment, 'model', None), 'name', ''))


def _equipment_garage_number(equipment):
    return str(getattr(equipment, 'garage_number', '') or '')


def equipment_garage_number_int(equipment):
    digits = ''.join(char for char in _equipment_garage_number(equipment) if char.isdigit())
    return int(digits) if digits else None


def equipment_is_truck(equipment):
    return 'самосвал' in _equipment_type_name(equipment)


def equipment_is_excavator(equipment):
    return 'экскаватор' in _equipment_type_name(equipment)


def equipment_is_belaz_truck(equipment):
    model_name = _equipment_model_name(equipment)
    return equipment_is_truck(equipment) and ('белаз' in model_name or 'belaz' in model_name)


def equipment_is_nhl_truck(equipment):
    model_name = _equipment_model_name(equipment)
    return equipment_is_truck(equipment) and ('nhl' in model_name or 'nte' in model_name)


def equipment_is_excavator_4000(equipment):
    return equipment_is_excavator(equipment) and equipment_garage_number_int(equipment) in EXCAVATOR_4000_NUMBERS


def expected_plan_group_code(equipment):
    if equipment_is_belaz_truck(equipment):
        return 'belaz_trucks'
    if equipment_is_nhl_truck(equipment):
        return 'nhl_trucks'
    if equipment_is_excavator_4000(equipment):
        return 'excavators_4000'
    if equipment_is_excavator(equipment):
        return 'excavators_3000'
    return ''


def equipment_plan_group_label(group_code):
    defaults = DEFAULT_EQUIPMENT_PLAN_GROUPS.get(group_code)
    return defaults['name'] if defaults else group_code


def describe_equipment(equipment):
    model_name = getattr(getattr(equipment, 'model', None), 'name', '') or ''
    base = str(equipment)
    return f'{base} ({model_name})' if model_name else base


def validate_equipment_plan_group_membership(group, equipment_items, *, group_code=None, is_active=None):
    group_code = group_code or getattr(group, 'code', '')
    effective_active = getattr(group, 'is_active', False) if is_active is None else bool(is_active)
    equipment_list = list(equipment_items or [])
    errors = []

    if group_code in DEFAULT_EQUIPMENT_PLAN_GROUPS:
        for equipment in equipment_list:
            expected_code = expected_plan_group_code(equipment)
            if expected_code != group_code:
                expected_label = equipment_plan_group_label(expected_code) if expected_code else 'нет подходящей стандартной группы'
                errors.append(
                    f'{describe_equipment(equipment)} относится к группе "{expected_label}", '
                    f'его нельзя сохранить в "{equipment_plan_group_label(group_code)}".'
                )

    if effective_active and equipment_list:
        conflicting_groups = (
            EquipmentPlanGroup.objects
            .filter(is_active=True, equipment__in=equipment_list)
            .exclude(pk=getattr(group, 'pk', None))
            .prefetch_related('equipment')
            .distinct()
        )
        conflicts_by_equipment_id = defaultdict(list)
        selected_ids = {item.id for item in equipment_list if item.id}
        for other_group in conflicting_groups:
            for equipment in other_group.equipment.all():
                if equipment.id in selected_ids:
                    conflicts_by_equipment_id[equipment.id].append(other_group.name)
        equipment_by_id = {item.id: item for item in equipment_list if item.id}
        for equipment_id, group_names in conflicts_by_equipment_id.items():
            errors.append(
                f'{describe_equipment(equipment_by_id[equipment_id])} уже входит в активную группу: '
                f'{", ".join(group_names)}.'
            )

    if errors:
        raise ValidationError({'equipment': errors})


def classify_equipment_for_default_groups(equipment_queryset=None):
    equipment_queryset = equipment_queryset or Equipment.objects.all()
    groups = defaultdict(list)
    unassigned = []
    for equipment in equipment_queryset.select_related('equipment_type', 'model'):
        group_code = expected_plan_group_code(equipment)
        if group_code:
            groups[group_code].append(equipment)
        else:
            unassigned.append(equipment)
    return groups, unassigned


def reconcile_default_equipment_plan_groups(*, dry_run=False):
    report = {
        'equipment_total': Equipment.objects.count(),
        'groups': {},
        'removed_total': 0,
        'added_total': 0,
        'unassigned': [],
    }

    with transaction.atomic():
        target_equipment, unassigned = classify_equipment_for_default_groups()
        report['unassigned'] = [describe_equipment(item) for item in unassigned]

        for group_code, defaults in DEFAULT_EQUIPMENT_PLAN_GROUPS.items():
            group, created = EquipmentPlanGroup.objects.get_or_create(
                code=group_code,
                defaults={
                    'name': defaults['name'],
                    'calculation_mode': defaults['calculation_mode'],
                    'is_active': False,
                    'comment': defaults['comment'],
                },
            )
            update_fields = []
            if group.name != defaults['name']:
                group.name = defaults['name']
                update_fields.append('name')
            if group.calculation_mode != defaults['calculation_mode']:
                group.calculation_mode = defaults['calculation_mode']
                update_fields.append('calculation_mode')
            if not group.comment:
                group.comment = defaults['comment']
                update_fields.append('comment')
            if update_fields and not dry_run:
                group.save(update_fields=update_fields)

            before_ids = set(group.equipment.values_list('id', flat=True))
            target_ids = {equipment.id for equipment in target_equipment.get(group_code, [])}
            removed_ids = before_ids - target_ids
            added_ids = target_ids - before_ids
            if not dry_run:
                group.equipment.set(target_ids)

            report['groups'][group_code] = {
                'name': defaults['name'],
                'created': created,
                'target_count': len(target_ids),
                'before_count': len(before_ids),
                'removed_count': len(removed_ids),
                'added_count': len(added_ids),
                'removed': list(
                    Equipment.objects
                    .filter(id__in=removed_ids)
                    .select_related('equipment_type', 'model')
                    .order_by('equipment_type__name', 'garage_number')
                    .values_list('garage_number', flat=True)
                ),
                'added': list(
                    Equipment.objects
                    .filter(id__in=added_ids)
                    .select_related('equipment_type', 'model')
                    .order_by('equipment_type__name', 'garage_number')
                    .values_list('garage_number', flat=True)
                ),
            }
            report['removed_total'] += len(removed_ids)
            report['added_total'] += len(added_ids)

        if dry_run:
            transaction.set_rollback(True)

    return report


def find_open_shift_plan_group_mismatches():
    from .models import EmployeeShift

    mismatches = []
    shifts = (
        EmployeeShift.objects
        .filter(closed_at__isnull=True, equipment__isnull=False, plan_group__isnull=False)
        .select_related('equipment', 'equipment__equipment_type', 'equipment__model', 'plan_group')
    )
    for shift in shifts:
        expected_code = expected_plan_group_code(shift.equipment)
        actual_code = getattr(shift.plan_group, 'code', '')
        if expected_code and actual_code != expected_code:
            mismatches.append({
                'shift_id': shift.id,
                'equipment': describe_equipment(shift.equipment),
                'actual_group': shift.plan_group.name,
                'actual_group_code': actual_code,
                'expected_group': equipment_plan_group_label(expected_code),
                'expected_group_code': expected_code,
                'plan_status': shift.plan_status,
            })
    return mismatches
