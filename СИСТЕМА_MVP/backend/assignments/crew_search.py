"""Compact cross-category index for the deputy's placement search."""
from collections import defaultdict

from rotations.services import approved_watch_extensions_on
from users.models import Employee, Role
from users.work_profiles import effective_specializations_by_employee_id

from .crew_categories import AUXILIARY_CATEGORIES, CATEGORY_LABELS
from .models import AssignmentStatus, CrewPlanSlot, CrewPlanStatus, EquipmentAssignment, WorkShiftType
from .services import WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES, eligible_crew_employee_ids, production_work_date


def placement_people_index(work_date, *, visible_free):
    current = work_date == production_work_date()
    roles = set(Role.objects.filter(code__in=CATEGORY_LABELS, is_active=True).values_list('code', flat=True))
    kinds = {**WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES, **{r: c.equipment_type for r, c in AUXILIARY_CATEGORIES.items()}}
    slots = CrewPlanSlot.objects.filter(
        plan__work_date=work_date, plan__role__code__in=roles,
        plan__status__in=[CrewPlanStatus.PUBLISHED, CrewPlanStatus.DRAFT] if current else [CrewPlanStatus.PUBLISHED],
    )
    seats = {}
    reserved_roles = defaultdict(set)
    for role, status, equipment, number, kind, shift, primary, factual, secondary, active, kind_active in slots.order_by(
        'plan__revision'
    ).values_list('plan__role__code', 'plan__status', 'equipment_id', 'equipment__garage_number',
                  'equipment__equipment_type__name', 'shift_type', 'employee_id', 'reported_employee_id',
                  'secondary_employee_id', 'equipment__is_active', 'equipment__equipment_type__is_active'):
        for person in (primary, factual, secondary):
            if person:
                reserved_roles[person].add(role)
        if current and (not active or not kind_active or kind.casefold() != kinds[role].casefold()):
            continue
        key = (role, equipment, shift)
        # A current draft (including an explicitly empty seat) takes precedence.
        if key not in seats or status == CrewPlanStatus.DRAFT:
            occupant = factual if current and status == CrewPlanStatus.PUBLISHED else primary or factual
            seats[key] = (number.removeprefix(kind + ' № '), occupant, secondary, status)

    if current:
        assignments = EquipmentAssignment.objects.filter(
            role__code__in=roles, status=AssignmentStatus.ACCEPTED, ended_at__isnull=True,
            shift__isnull=True, shift_type__in=WorkShiftType.values,
        ).order_by('-assigned_at', '-pk')
        baseline = set()
        for role, equipment, number, kind, shift, person, active, kind_active in assignments.values_list(
            'role__code', 'equipment_id', 'equipment__garage_number', 'equipment__equipment_type__name',
            'shift_type', 'employee_id', 'equipment__is_active', 'equipment__equipment_type__is_active',
        ):
            reserved_roles[person].add(role)
            key = (role, equipment, shift)
            if key in baseline or not active or not kind_active or kind.casefold() != kinds[role].casefold():
                continue
            baseline.add(key)
            # Before the first visit the live assignment supplies the draft's primary seat.
            previous = seats.get(key)
            if not previous or previous[3] != CrewPlanStatus.DRAFT:
                seats[key] = (number.removeprefix(kind + ' № '), person, previous[2] if previous else None, '')

    locations = defaultdict(list)
    for (role, equipment, shift), (number, primary, secondary, _) in seats.items():
        for position, person in [('primary', primary), ('secondary', secondary)]:
            if person:
                locations[person].append(dict(role=role, equipment=equipment, number=number,
                                              shift=shift, position=position))

    eligible = defaultdict(set)
    free = []
    if current:
        free = list(Employee.objects.filter(Employee.work_eligibility_q(), is_active=True, status=Employee.Status.ACTIVE)
                    .exclude(pk__in=locations).select_related('base_specialization'))
        specs = effective_specializations_by_employee_id(free, infer_from_position=True)
        for role in roles.intersection({'driver', 'excavator_operator'}):
            for person in eligible_crew_employee_ids(role):
                eligible[person].add(role)
        for person, spec in specs.items():
            if spec and spec.is_active:
                if spec.code == 'assistant_excavator_operator' and 'excavator_operator' in roles:
                    eligible[person].add('excavator_operator')
                eligible[person].update(role for role, category in AUXILIARY_CATEGORIES.items()
                                        if role in roles and category.specialization == spec.code)

    employees = list(Employee.objects.filter(pk__in=locations).only('id', 'full_name', 'personnel_number')) + free
    extensions = approved_watch_extensions_on(work_date, employee_ids=[e.pk for e in free]) if free else {}
    result = []
    for employee in employees:
        places = locations.get(employee.pk, [])
        available = sorted(role for role in eligible[employee.pk]
                           if not (reserved_roles[employee.pk] - {role}))
        if not places and not (available and visible_free(employee, extensions)):
            continue
        result.append(dict(id=employee.pk, full_name=employee.full_name, personnel_number=employee.personnel_number,
                           places=places, roles=available))
    return result
