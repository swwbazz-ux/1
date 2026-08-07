from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.utils import timezone

from core.production_time import production_work_date
from shifts.models import WatchPeriod
from users.models import Employee

from .models import EmployeeBedOccupancy, PhysicalBed, PhysicalRoom
from .validator import (
    ActualPlacementType,
    EffectivePlacementInterval,
    PlacementConflictValidationRequest,
    validate_placement_conflicts,
)


def _settlement_error(message, code):
    return ValidationError(message, code=code)


def effective_occupancy_at_q(moment):
    return (
        Q(starts_at__lte=moment)
        & (Q(ends_at__isnull=True) | Q(ends_at__gt=moment))
        & (
            Q(terminated_at__isnull=True)
            | Q(terminated_at__gt=moment)
        )
    )


def _current_watch_period_groups(moment):
    """Group applicable periods by their canonical watch composition."""
    as_of = production_work_date(moment)
    return (
        WatchPeriod.objects
        .filter(
            is_active=True,
            watch_composition_id__isnull=False,
            watch_composition__is_active=True,
            starts_on__lte=as_of,
            ends_on__gte=as_of,
        )
        .order_by()
        .values('watch_composition_id')
        .annotate(period_count=Count('pk'))
    )


def current_roster_resolution(moment=None):
    """Describe whether today's canonical roster is usable or ambiguous."""
    moment = moment or timezone.now()
    groups = _current_watch_period_groups(moment)
    return {
        'has_unambiguous': groups.filter(period_count=1).exists(),
        'has_ambiguous': groups.filter(period_count__gt=1).exists(),
    }


def unsettled_current_roster_employees(moment=None):
    """Return active employees from unambiguous current watch rosters."""
    moment = moment or timezone.now()
    unambiguous_composition_ids = (
        _current_watch_period_groups(moment)
        .filter(period_count=1)
        .values('watch_composition_id')
    )
    effective_occupancy = EmployeeBedOccupancy.objects.filter(
        effective_occupancy_at_q(moment),
        employee_id=OuterRef('pk'),
    )
    return (
        Employee.objects
        .filter(
            is_active=True,
            status=Employee.Status.ACTIVE,
            watch_composition__is_active=True,
            watch_composition_id__in=Subquery(
                unambiguous_composition_ids,
            ),
        )
        .annotate(
            has_effective_occupancy=Exists(effective_occupancy),
        )
        .filter(has_effective_occupancy=False)
        .select_related('personnel_position', 'watch_composition')
        .order_by('full_name', 'pk')
    )


def settle_employee_on_bed(
    *,
    bed_stable_id,
    employee_id,
    assignment_type,
    settled_by,
):
    if assignment_type not in EmployeeBedOccupancy.AssignmentType.values:
        raise _settlement_error(
            'Выберите тип закрепления.',
            'settlement_assignment_type_required',
        )

    with transaction.atomic():
        try:
            bed = (
                PhysicalBed.objects
                .select_for_update()
                .select_related('room', 'room__dormitory')
                .get(stable_id=bed_stable_id)
            )
        except PhysicalBed.DoesNotExist as error:
            raise _settlement_error(
                'Койко-место не найдено.',
                'settlement_bed_not_found',
            ) from error

        room = PhysicalRoom.objects.select_for_update().get(pk=bed.room_id)

        try:
            employee = (
                Employee.objects
                .select_for_update()
                .select_related('personnel_position')
                .get(pk=employee_id)
            )
        except (Employee.DoesNotExist, TypeError, ValueError) as error:
            raise _settlement_error(
                'Сотрудник не найден.',
                'settlement_employee_not_found',
            ) from error

        if room.transfer_status != PhysicalRoom.TransferStatus.TRANSFERRED:
            raise _settlement_error(
                'Комната не передана для расселения.',
                'settlement_room_not_transferred',
            )
        if not employee.is_active or employee.status != Employee.Status.ACTIVE:
            raise _settlement_error(
                'Для заселения доступен только активный сотрудник.',
                'settlement_employee_inactive',
            )

        moment = timezone.now()
        persisted_occupancies = list(
            EmployeeBedOccupancy.objects
            .select_for_update()
            .filter(
                Q(physical_bed_id=bed.pk) | Q(employee_id=employee.pk)
            )
            .select_related('physical_bed')
            .order_by('pk')
        )
        effective_placement_intervals = tuple(
            EffectivePlacementInterval(
                occupancy_id=occupancy.pk,
                employee_id=occupancy.employee_id,
                bed_id=occupancy.physical_bed_id,
                placement_type=ActualPlacementType(
                    occupancy.assignment_type
                ),
                starts_at=occupancy.starts_at,
                ends_at=occupancy.ends_at,
                bed_stable_id=occupancy.physical_bed.stable_id,
                terminated_at=occupancy.terminated_at,
            )
            for occupancy in persisted_occupancies
        )
        conflict_result = validate_placement_conflicts(
            PlacementConflictValidationRequest(
                employee_id=employee.pk,
                bed_stable_id=bed.stable_id,
                starts_at=moment,
                ends_at=None,
                terminated_at=None,
                current_occupancy_id=None,
                effective_placement_intervals=effective_placement_intervals,
            )
        )
        if conflict_result.blocks:
            raise ValidationError([
                ValidationError(block.message, code=block.code)
                for block in conflict_result.blocks
            ])

        try:
            with transaction.atomic():
                occupancy = EmployeeBedOccupancy.objects.create(
                    employee=employee,
                    physical_bed=bed,
                    assignment_type=assignment_type,
                    settled_at=moment,
                    starts_at=moment,
                    ends_at=None,
                    terminated_at=None,
                    settled_by=settled_by,
                    ended_at=None,
                )
        except IntegrityError as error:
            if EmployeeBedOccupancy.objects.filter(
                physical_bed=bed,
                ended_at__isnull=True,
            ).exists():
                raise _settlement_error(
                    'Койко-место уже занято.',
                    'settlement_bed_occupied',
                ) from error
            if EmployeeBedOccupancy.objects.filter(
                employee=employee,
                ended_at__isnull=True,
            ).exists():
                raise _settlement_error(
                    'Сотрудник уже занимает другое активное место.',
                    'settlement_employee_already_housed',
                ) from error
            raise

    return occupancy
