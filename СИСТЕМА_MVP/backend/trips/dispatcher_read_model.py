from dataclasses import dataclass
from typing import Any, Callable

from assignments.models import AssignmentStatus, EquipmentAssignment, HaulAssignment
from core.production_time import production_shift_bounds, production_shift_context
from downtimes.models import DowntimeEvent
from references.models import Equipment
from shifts.models import EmployeeShift
from users.live_monitor import attach_application_presence
from users.models import EmployeeAccess

from .dispatcher_header import build_dispatcher_header_context
from .models import DispatcherActionLog, OPEN_TRIP_STATUSES, Trip, TripStatus


@dataclass(frozen=True)
class DispatcherControlReadModel:
    """Read-only snapshot used by the Dispatcher and Mining Master renderers."""

    dispatcher_header: dict[str, Any]
    dispatcher_shift: Any
    dispatcher_dashboard: dict[str, Any]
    active_trips: Any
    pending_assignments: Any
    accepted_assignments: Any
    recent_completed_trips: Any
    open_shifts: list[Any]
    open_mechanic_downtimes: Any
    open_mechanic_downtimes_count: int
    trucks: Any
    excavators: Any
    recent_dispatcher_actions: Any
    filters: dict[str, Any]
    dispatcher_filter_items: list[tuple[str, str]]


def build_dispatcher_control_read_model(
    request,
    access,
    *,
    dashboard_builder: Callable[..., dict[str, Any]],
    equipment_shift_is_current: Callable[[Any], bool],
    dispatcher_header_override=None,
    context_overrides=None,
    equipment_detail=None,
):
    """Build the shared Dispatcher screen/card snapshot without handling HTTP writes."""

    overrides = context_overrides or {}
    dispatcher_header = (
        dispatcher_header_override
        or build_dispatcher_header_context(access, request)
    )
    dispatcher_shift = dispatcher_header.get('active_shift')
    reporting_period = overrides.get('mining_master_reporting_period')
    has_mining_master_reporting_period = bool(
        (reporting_period or {}).get('starts_at')
    )

    truck_id = request.GET.get('truck', '').strip()
    excavator_id = request.GET.get('excavator', '').strip()
    show_active_trips = request.GET.get('show_active_trips', '1') == '1'
    show_pending_assignments = request.GET.get('show_pending_assignments', '1') == '1'
    show_accepted_assignments = request.GET.get('show_accepted_assignments', '1') == '1'

    active_trips = (
        Trip.objects
        .filter(status__in=OPEN_TRIP_STATUSES)
        .select_related(
            'truck',
            'truck__equipment_type',
            'excavator',
            'excavator__equipment_type',
            'rock_type',
            'dump_point',
            'assigned_dump_point',
            'actual_dump_point',
            'excavator_operator',
            'loading_shift',
        )
        .order_by('created_at')
    )
    if not dispatcher_shift and not has_mining_master_reporting_period:
        active_trips = active_trips.none()
    if truck_id:
        active_trips = active_trips.filter(truck_id=truck_id)
    if excavator_id:
        active_trips = active_trips.filter(excavator_id=excavator_id)
    if not show_active_trips:
        active_trips = active_trips.none()

    pending_assignments = (
        HaulAssignment.objects
        .filter(status=AssignmentStatus.PENDING, ended_at__isnull=True)
        .select_related(
            'truck',
            'truck__equipment_type',
            'excavator',
            'excavator__equipment_type',
            'assigned_by',
        )
        .order_by('assigned_at')
    )
    if truck_id:
        pending_assignments = pending_assignments.filter(truck_id=truck_id)
    if excavator_id:
        pending_assignments = pending_assignments.filter(excavator_id=excavator_id)
    if not show_pending_assignments:
        pending_assignments = pending_assignments.none()

    accepted_assignments = (
        HaulAssignment.objects
        .filter(status=AssignmentStatus.ACCEPTED, ended_at__isnull=True)
        .select_related(
            'truck',
            'truck__equipment_type',
            'excavator',
            'excavator__equipment_type',
            'assigned_by',
        )
        .order_by('-accepted_at')
    )
    if truck_id:
        accepted_assignments = accepted_assignments.filter(truck_id=truck_id)
    if excavator_id:
        accepted_assignments = accepted_assignments.filter(excavator_id=excavator_id)
    if not show_accepted_assignments:
        accepted_assignments = accepted_assignments.none()

    production_context = production_shift_context()
    production_shift_start, production_shift_end = production_shift_bounds(
        production_context.production_date,
        production_context.shift_type,
    )
    recent_completed_trips = (
        Trip.objects
        .filter(
            status=TripStatus.COMPLETED,
            completed_at__gte=production_shift_start,
            completed_at__lt=production_shift_end,
        )
        .select_related(
            'truck',
            'truck__equipment_type',
            'excavator',
            'excavator__equipment_type',
            'rock_type',
            'dump_point',
            'driver',
        )
        .order_by('-completed_at')
    )
    equipment_work_assignments = list(
        EquipmentAssignment.objects
        .filter(
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
            shift_type=production_context.shift_type,
            employee__is_active=True,
            equipment__is_active=True,
            role__code__in={'driver', 'excavator_operator'},
        )
        .select_related('employee', 'equipment', 'role')
        .order_by('equipment_id', '-assigned_at', '-id')
    )
    if not dispatcher_shift and not has_mining_master_reporting_period:
        recent_completed_trips = recent_completed_trips.none()
    if truck_id:
        recent_completed_trips = recent_completed_trips.filter(truck_id=truck_id)
    if excavator_id:
        recent_completed_trips = recent_completed_trips.filter(excavator_id=excavator_id)

    open_shifts = (
        EmployeeShift.objects
        .filter(closed_at__isnull=True)
        .select_related(
            'employee',
            'equipment',
            'equipment__equipment_type',
            'plan_group',
            'opened_by',
        )
        .order_by('opened_at')
    )
    if dispatcher_shift:
        open_shifts = open_shifts.exclude(id=dispatcher_shift.id)
    if truck_id:
        open_shifts = open_shifts.filter(equipment_id=truck_id)
    if excavator_id:
        open_shifts = open_shifts.filter(equipment_id=excavator_id)
    open_shifts = list(open_shifts[:120])
    open_shift_employees = attach_application_presence(
        shift.employee for shift in open_shifts
    )
    presence_by_employee_id = {
        employee.pk: employee.application_presence
        for employee in open_shift_employees
    }

    employee_ids = [shift.employee_id for shift in open_shifts]
    role_by_employee_id = {
        employee_access.employee_id: employee_access.role.name
        for employee_access in (
            EmployeeAccess.objects
            .filter(
                employee_id__in=employee_ids,
                is_active=True,
                role__is_active=True,
            )
            .select_related('role')
            .order_by('employee_id', 'id')
        )
    }
    for shift in open_shifts:
        shift.role_name = role_by_employee_id.get(shift.employee_id, '-')
        shift.application_presence = presence_by_employee_id.get(shift.employee_id)
        shift.is_dashboard_stale = bool(
            overrides.get('mining_master_mobile_enabled')
            and shift.equipment_id
            and not equipment_shift_is_current(shift)
        )

    trucks = (
        Equipment.objects
        .filter(equipment_type__name='Самосвал', is_active=True)
        .select_related('equipment_type', 'model')
        .order_by('garage_number')
    )
    excavators = (
        Equipment.objects
        .filter(equipment_type__name='Экскаватор', is_active=True)
        .select_related('equipment_type', 'model')
        .order_by('garage_number')
    )
    recent_dispatcher_actions = (
        DispatcherActionLog.objects
        .select_related('actor')
        .order_by('-created_at')[:12]
    )
    open_mechanic_downtimes = (
        DowntimeEvent.objects
        .filter(ended_at__isnull=True)
        .select_related(
            'equipment',
            'equipment__equipment_type',
            'reason',
            'employee',
        )
        .order_by('started_at')
    )
    downtime_equipment_ids = [
        equipment_id
        for equipment_id in (truck_id, excavator_id)
        if equipment_id
    ]
    if downtime_equipment_ids:
        open_mechanic_downtimes = open_mechanic_downtimes.filter(
            equipment_id__in=downtime_equipment_ids
        )
    open_mechanic_downtimes_count = open_mechanic_downtimes.count()

    equipment_card_ids = None
    if not overrides.get('mining_master_mobile_enabled'):
        equipment_card_ids = set()
    if equipment_detail:
        equipment_card_ids = {equipment_detail['card_key']}

    dispatcher_dashboard = dashboard_builder(
        dispatcher_shift=dispatcher_shift,
        active_trips=active_trips,
        pending_assignments=pending_assignments,
        accepted_assignments=accepted_assignments,
        recent_completed_trips=recent_completed_trips,
        open_shifts=open_shifts,
        open_mechanic_downtimes=open_mechanic_downtimes[:30],
        trucks=trucks,
        excavators=excavators,
        recent_dispatcher_actions=recent_dispatcher_actions,
        equipment_card_ids=equipment_card_ids,
        equipment_work_assignments=equipment_work_assignments,
        reporting_period=reporting_period,
    )

    return DispatcherControlReadModel(
        dispatcher_header=dispatcher_header,
        dispatcher_shift=dispatcher_shift,
        dispatcher_dashboard=dispatcher_dashboard,
        active_trips=active_trips,
        pending_assignments=pending_assignments,
        accepted_assignments=accepted_assignments,
        recent_completed_trips=recent_completed_trips,
        open_shifts=open_shifts,
        open_mechanic_downtimes=open_mechanic_downtimes,
        open_mechanic_downtimes_count=open_mechanic_downtimes_count,
        trucks=trucks,
        excavators=excavators,
        recent_dispatcher_actions=recent_dispatcher_actions,
        filters={
            'truck': truck_id,
            'excavator': excavator_id,
            'show_active_trips': show_active_trips,
            'show_pending_assignments': show_pending_assignments,
            'show_accepted_assignments': show_accepted_assignments,
        },
        dispatcher_filter_items=[
            ('truck', truck_id),
            ('excavator', excavator_id),
            ('show_active_trips', '1' if show_active_trips else '0'),
            ('show_pending_assignments', '1' if show_pending_assignments else '0'),
            ('show_accepted_assignments', '1' if show_accepted_assignments else '0'),
        ],
    )
