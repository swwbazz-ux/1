from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    EquipmentAssignment,
    ExcavatorPlacement,
    HaulAssignment,
)
from assignments.services import apply_pending_haul_assignment, schedule_haul_assignment
from core.models import bump_operational_state
from core.qa_environment import require_excavator_qa_environment
from downtimes.models import DowntimeReason
from references.models import (
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
    TruckCapacityRule,
)
from shifts.models import EmployeeShift
from users.forms import normalize_phone
from users.models import Employee, EmployeeAccess, Role

from .models import Trip, TripStatus


QA_PREFIX = 'RUSTORE-QA'
QA_OPERATOR_NUMBER = f'{QA_PREFIX}-EO-01'
QA_DISPATCHER_NUMBER = f'{QA_PREFIX}-DISPATCHER'
QA_EXCAVATOR_GARAGE = 'QA-EX-01'
QA_TRUCK_GARAGE_PREFIX = 'QA-T-'

QA_EXCAVATOR_DOWNTIME_REASONS = (
    ('Заправка', 'Заправка', False, 40, False),
    ('ТО', 'ТО', False, 50, False),
    ('Ремонт', 'Ремонт', False, 60, False),
    ('Поломка', 'Поломка', True, 70, False),
    ('БВР', 'БВР', False, 80, False),
    ('Обед', 'Обед', False, 90, False),
    ('Ожидание самосвалов', 'Ожидание самосвалов', False, 110, True),
    ('Зачистка забоя', 'Зачистка забоя', False, 120, True),
    ('Подготовка забоя', 'Подготовка забоя', False, 130, True),
    ('Перегон экскаватора', 'Перегон', False, 140, True),
    ('Климатические условия', 'Погода', False, 150, False),
    ('Прочие', 'Прочие', False, 160, False),
)


@dataclass(frozen=True)
class ExcavatorQAScenario:
    operator: Employee
    dispatcher: Employee
    excavator: Equipment
    trucks: tuple[Equipment, ...]


def _active_employee(personnel_number: str, full_name: str, **extra) -> Employee:
    employee, _ = Employee.objects.update_or_create(
        personnel_number=personnel_number,
        defaults={
            'full_name': full_name,
            'status': Employee.Status.ACTIVE,
            'is_active': True,
            **extra,
        },
    )
    return employee


def _require_credentials() -> tuple[str, str]:
    phone = normalize_phone(getattr(settings, 'EXCAVATOR_QA_PHONE', ''))
    pin = str(getattr(settings, 'EXCAVATOR_QA_PIN', '') or '').strip()
    if len(phone) != 11 or not phone.startswith('79'):
        raise ValueError('EXCAVATOR_QA_PHONE must be a Russian mobile number.')
    if len(pin) != 6 or not pin.isdigit():
        raise ValueError('EXCAVATOR_QA_PIN must contain exactly 6 digits.')
    return phone, pin


def load_excavator_qa_scenario() -> ExcavatorQAScenario:
    require_excavator_qa_environment()
    operator = Employee.objects.get(personnel_number=QA_OPERATOR_NUMBER)
    dispatcher = Employee.objects.get(personnel_number=QA_DISPATCHER_NUMBER)
    excavator = Equipment.objects.get(garage_number=QA_EXCAVATOR_GARAGE)
    trucks = tuple(
        Equipment.objects.filter(
            garage_number__startswith=QA_TRUCK_GARAGE_PREFIX,
            is_active=True,
        ).order_by('garage_number')
    )
    if not trucks:
        raise RuntimeError('QA scenario is not prepared: no test trucks found.')
    return ExcavatorQAScenario(
        operator=operator,
        dispatcher=dispatcher,
        excavator=excavator,
        trucks=trucks,
    )


@transaction.atomic
def prepare_excavator_qa_scenario() -> ExcavatorQAScenario:
    require_excavator_qa_environment()
    phone, pin = _require_credentials()
    truck_count = int(getattr(settings, 'EXCAVATOR_QA_TRUCK_COUNT', 4))

    excavator_role, _ = Role.objects.update_or_create(
        code='excavator_operator',
        defaults={'name': 'Машинист экскаватора', 'is_active': True},
    )
    driver_role, _ = Role.objects.update_or_create(
        code='driver',
        defaults={'name': 'Водитель', 'is_active': True},
    )
    dispatcher_role, _ = Role.objects.update_or_create(
        code='dispatcher',
        defaults={'name': 'Диспетчер', 'is_active': True},
    )
    excavator_type, _ = EquipmentType.objects.update_or_create(
        name='Экскаватор', defaults={'is_active': True}
    )
    truck_type, _ = EquipmentType.objects.update_or_create(
        name='Самосвал', defaults={'is_active': True}
    )
    excavator_model, _ = EquipmentModel.objects.update_or_create(
        equipment_type=excavator_type,
        name='ЭКГ-10 QA',
        defaults={'fuel_capacity_limit_l': 7000, 'is_active': True},
    )
    truck_model, _ = EquipmentModel.objects.update_or_create(
        equipment_type=truck_type,
        name='БелАЗ-7513 QA',
        defaults={'body_volume_m3': '49.40', 'payload_tons': '130', 'is_active': True},
    )
    for name, short_label, is_critical, sort_order, excavator_only in QA_EXCAVATOR_DOWNTIME_REASONS:
        DowntimeReason.objects.update_or_create(
            name=name,
            defaults={
                'short_label': short_label,
                'equipment_type': excavator_type if excavator_only else None,
                'is_critical': is_critical,
                'show_for_truck_driver': False,
                'show_for_excavator_operator': True,
                'show_for_mechanic': False,
                'sort_order': sort_order,
                'is_active': True,
            },
        )
    excavator, _ = Equipment.objects.update_or_create(
        garage_number=QA_EXCAVATOR_GARAGE,
        defaults={
            'equipment_type': excavator_type,
            'model': excavator_model,
            'is_active': True,
        },
    )
    rocks = []
    for name, density in (
        ('Руда', '2.6000'),
        ('Вскрыша', '2.1000'),
        ('Смешанная руда', '2.3500'),
    ):
        rock, _ = RockType.objects.update_or_create(
            name=name,
            defaults={'density': density, 'is_active': True},
        )
        TruckCapacityRule.objects.update_or_create(
            equipment_model=truck_model,
            rock_type=rock,
            defaults={'volume_m3': '49.40'},
        )
        rocks.append(rock)
    rock = rocks[0]
    dump_points = []
    for name in ('Дробилка', 'Рудный склад', 'Склад вскрыши', 'Буферный склад'):
        point, _ = DumpPoint.objects.update_or_create(
            name=name, defaults={'is_active': True}
        )
        dump_points.append(point)
    dump_point = dump_points[0]

    operator = _active_employee(
        QA_OPERATOR_NUMBER,
        'Тестовый машинист RuStore',
        phone=phone,
        work_category=Employee.WorkCategory.EXCAVATOR_OPERATOR,
    )
    EmployeeAccess.objects.update_or_create(
        employee=operator,
        role=excavator_role,
        defaults={
            'access_code': pin,
            'status': EmployeeAccess.Status.ACTIVATED,
            'is_active': True,
            'activated_at': timezone.now(),
        },
    )
    dispatcher = _active_employee(
        QA_DISPATCHER_NUMBER,
        'QA-бот диспетчер',
    )
    EmployeeAccess.objects.update_or_create(
        employee=dispatcher,
        role=dispatcher_role,
        defaults={
            'access_code': pin,
            'status': EmployeeAccess.Status.ACTIVATED,
            'is_active': True,
            'activated_at': timezone.now(),
        },
    )
    operator_assignment = EquipmentAssignment.objects.filter(
        employee=operator,
        equipment=excavator,
        role=excavator_role,
        ended_at__isnull=True,
    ).order_by('-assigned_at').first()
    if not operator_assignment:
        EquipmentAssignment.objects.create(
            employee=operator,
            equipment=excavator,
            role=excavator_role,
            shift_type='day',
            assigned_by=dispatcher,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
    ExcavatorPlacement.objects.get_or_create(
        excavator=excavator,
        defaults={
            'zone': ExcavatorPlacement.Zone.INACTIVE,
            'work_rock_type': rock,
            'work_dump_point': dump_point,
            'loading_horizon': '240',
            'loading_block': '1',
            'changed_by': dispatcher,
            'work_context_updated_at': timezone.now(),
        },
    )

    trucks = []
    for index in range(1, truck_count + 1):
        truck, _ = Equipment.objects.update_or_create(
            garage_number=f'{QA_TRUCK_GARAGE_PREFIX}{index:02d}',
            defaults={
                'equipment_type': truck_type,
                'model': truck_model,
                'is_active': True,
            },
        )
        driver = _active_employee(
            f'{QA_PREFIX}-DRIVER-{index:02d}',
            f'QA-бот водитель {index:02d}',
            work_category=Employee.WorkCategory.DRIVER,
        )
        EmployeeAccess.objects.update_or_create(
            employee=driver,
            role=driver_role,
            defaults={
                'access_code': f'8{index:05d}',
                'status': EmployeeAccess.Status.ACTIVATED,
                'is_active': True,
                'activated_at': timezone.now(),
            },
        )
        driver_shift = (
            EmployeeShift.objects
            .filter(employee=driver, equipment=truck, closed_at__isnull=True)
            .first()
        )
        if not driver_shift:
            driver_shift = EmployeeShift.objects.create(
                employee=driver,
                equipment=truck,
                shift_type='day',
                workplace_code='driver',
                start_fuel='500',
                start_mileage=str(10000 + index * 100),
                start_engine_hours=str(2000 + index * 10),
                opened_at=timezone.now(),
                opened_by=driver,
            )
        driver_assignment = EquipmentAssignment.objects.filter(
            employee=driver,
            equipment=truck,
            role=driver_role,
            ended_at__isnull=True,
        ).order_by('-assigned_at').first()
        if not driver_assignment:
            EquipmentAssignment.objects.create(
                employee=driver,
                equipment=truck,
                role=driver_role,
                shift=driver_shift,
                assigned_by=dispatcher,
                status=AssignmentStatus.ACCEPTED,
                accepted_at=timezone.now(),
            )
        trucks.append(truck)

    return ExcavatorQAScenario(
        operator=operator,
        dispatcher=dispatcher,
        excavator=excavator,
        trucks=tuple(trucks),
    )


def _complete_due_trips(scenario: ExcavatorQAScenario, now) -> int:
    from trips.views import finalize_trip_unloaded

    cutoff = now - timedelta(
        seconds=int(getattr(settings, 'EXCAVATOR_QA_TRANSIT_SECONDS', 12))
    )
    due_ids = list(
        Trip.objects.filter(
            excavator=scenario.excavator,
            truck__garage_number__startswith=QA_TRUCK_GARAGE_PREFIX,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            created_at__lte=cutoff,
        ).values_list('id', flat=True)
    )
    completed = 0
    for trip_id in due_ids:
        with transaction.atomic():
            trip = (
                Trip.objects.select_for_update(of=('self',))
                .select_related('truck', 'rock_type', 'loading_shift', 'excavator')
                .filter(pk=trip_id, status=TripStatus.LOADED_WAITING_UNLOAD)
                .first()
            )
            if not trip:
                continue
            unloading_shift = (
                EmployeeShift.objects.select_related('employee')
                .filter(equipment=trip.truck, closed_at__isnull=True)
                .order_by('-opened_at')
                .first()
            )
            if not unloading_shift:
                continue
            if not finalize_trip_unloaded(
                trip,
                driver=unloading_shift.employee,
                unloading_shift=unloading_shift,
            ):
                continue
            bump_operational_state(
                'QA simulator:trip_unloaded',
                event_type='trip_changed',
                object_type='Trip',
                object_id=trip.id,
                payload={
                    'action': 'trip_unloaded',
                    'trip_id': trip.id,
                    'truck_id': trip.truck_id,
                    'excavator_id': trip.excavator_id,
                    'status': TripStatus.COMPLETED,
                    'source': 'excavator_qa_simulator',
                },
            )
            completed += 1
    return completed


def run_excavator_qa_tick(*, now=None) -> dict:
    require_excavator_qa_environment()
    now = now or timezone.now()
    scenario = load_excavator_qa_scenario()
    open_shift = (
        EmployeeShift.objects.filter(
            employee=scenario.operator,
            equipment=scenario.excavator,
            closed_at__isnull=True,
        )
        .order_by('-opened_at')
        .first()
    )
    if not open_shift:
        return {'state': 'waiting_for_excavator_shift', 'assigned': 0, 'completed': 0}

    completed = _complete_due_trips(scenario, now)
    assigned = 0
    for truck in scenario.trucks:
        has_assignment = HaulAssignment.objects.filter(
            truck=truck,
            excavator=scenario.excavator,
            ended_at__isnull=True,
            status__in=(AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED),
        ).exists()
        if has_assignment:
            continue
        assignment, created = schedule_haul_assignment(
            truck=truck,
            excavator=scenario.excavator,
            assigned_by=scenario.dispatcher,
            now=now,
        )
        if assignment and assignment.status == AssignmentStatus.PENDING:
            apply_pending_haul_assignment(assignment.id, now=assignment.effective_at or now)
        assigned += int(created)

    return {
        'state': 'running',
        'assigned': assigned,
        'completed': completed,
        'open_trips': Trip.objects.filter(
            excavator=scenario.excavator,
            status=TripStatus.LOADED_WAITING_UNLOAD,
        ).count(),
    }
