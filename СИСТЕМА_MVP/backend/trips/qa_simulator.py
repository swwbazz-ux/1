from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

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
from core.production_time import production_work_date
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
from shifts.models import (
    EmployeeShift,
    EquipmentPlanGroup,
    PlanAssignmentStatus,
    PlanCalculationMode,
)
from shifts.services import assign_shift_plan_snapshot
from users.forms import normalize_phone
from users.models import Employee, EmployeeAccess, Role

from .models import OPEN_TRIP_STATUSES, Trip, TripStatus
from .trip_creation import create_loaded_waiting_unload_trip


QA_PREFIX = 'RUSTORE-QA'
QA_OPERATOR_NUMBER = f'{QA_PREFIX}-EO-01'
QA_DISPATCHER_NUMBER = f'{QA_PREFIX}-DISPATCHER'
QA_HUMAN_DRIVER_NUMBER = f'{QA_PREFIX}-DRIVER-HUMAN-01'
QA_DRIVER_BOT_OPERATOR_NUMBER = f'{QA_PREFIX}-EO-DRIVER-BOT'
QA_EXCAVATOR_GARAGE = 'QA-EX-01'
QA_TRUCK_GARAGE_PREFIX = 'QA-T-'
QA_DRIVER_TRUCK_GARAGE = 'QA-DRIVER-T-01'
QA_DRIVER_EXCAVATOR_GARAGE = 'QA-DRIVER-EX-01'
QA_EXCAVATOR_PLAN_GROUP_CODE = 'rustore-qa-excavator'
QA_DRIVER_PLAN_GROUP_CODE = 'rustore-qa-driver'

# name, short_label, is_critical, sort_order, equipment_scope,
# show_for_truck_driver, show_for_excavator_operator
QA_DOWNTIME_REASONS = (
    ('Ожидание погрузки', 'Ожидание погрузки', False, 10, 'truck', True, False),
    ('Ожидание разгрузки', 'Ожидание разгрузки', False, 20, 'truck', True, False),
    ('Ожидание разгрузки ККД', 'Ожидание ККД', False, 21, 'truck', True, False),
    ('Ожидание разгрузки СКДР', 'Ожидание СКДР', False, 22, 'truck', True, False),
    ('Ожидание фронта работ', 'ОФР', False, 30, None, True, False),
    ('Заправка', 'Заправка', False, 40, None, True, True),
    ('ТО', 'ТО', False, 50, None, True, True),
    ('Ремонт', 'Ремонт', False, 60, None, True, True),
    ('Поломка', 'Поломка', True, 70, None, True, True),
    ('БВР', 'БВР', False, 80, None, True, True),
    ('Обед', 'Обед', False, 90, None, True, True),
    ('Чистка кузова', 'Чистка кузова', False, 100, 'truck', True, False),
    ('Ожидание самосвалов', 'Ожидание самосвалов', False, 110, 'excavator', False, True),
    ('Зачистка забоя', 'Зачистка забоя', False, 120, 'excavator', False, True),
    ('Подготовка забоя', 'Подготовка забоя', False, 130, 'excavator', False, True),
    ('Перегон экскаватора', 'Перегон', False, 140, 'excavator', False, True),
    ('Климатические условия', 'Погода', False, 150, None, True, True),
    ('Прочие', 'Прочие', False, 160, None, True, True),
)


@dataclass(frozen=True)
class ExcavatorQAScenario:
    operator: Employee
    dispatcher: Employee
    excavator: Equipment
    trucks: tuple[Equipment, ...]
    human_driver: Employee
    human_driver_truck: Equipment
    driver_bot_operator: Employee
    driver_bot_excavator: Equipment


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


def _require_credentials(prefix: str) -> tuple[str, str]:
    phone_setting = f'{prefix}_PHONE'
    pin_setting = f'{prefix}_PIN'
    phone = normalize_phone(getattr(settings, phone_setting, ''))
    pin = str(getattr(settings, pin_setting, '') or '').strip()
    if len(phone) != 11 or not phone.startswith('79'):
        raise ValueError(f'{phone_setting} must be a Russian mobile number.')
    if len(pin) != 6 or not pin.isdigit():
        raise ValueError(f'{pin_setting} must contain exactly 6 digits.')
    return phone, pin


def load_excavator_qa_scenario() -> ExcavatorQAScenario:
    require_excavator_qa_environment()
    operator = Employee.objects.get(personnel_number=QA_OPERATOR_NUMBER)
    dispatcher = Employee.objects.get(personnel_number=QA_DISPATCHER_NUMBER)
    excavator = Equipment.objects.get(garage_number=QA_EXCAVATOR_GARAGE)
    human_driver = Employee.objects.get(personnel_number=QA_HUMAN_DRIVER_NUMBER)
    human_driver_truck = Equipment.objects.get(garage_number=QA_DRIVER_TRUCK_GARAGE)
    driver_bot_operator = Employee.objects.get(
        personnel_number=QA_DRIVER_BOT_OPERATOR_NUMBER
    )
    driver_bot_excavator = Equipment.objects.get(
        garage_number=QA_DRIVER_EXCAVATOR_GARAGE
    )
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
        human_driver=human_driver,
        human_driver_truck=human_driver_truck,
        driver_bot_operator=driver_bot_operator,
        driver_bot_excavator=driver_bot_excavator,
    )


@transaction.atomic
def prepare_excavator_qa_scenario() -> ExcavatorQAScenario:
    require_excavator_qa_environment()
    phone, pin = _require_credentials('EXCAVATOR_QA')
    driver_phone, driver_pin = _require_credentials('DRIVER_QA')
    if driver_phone == phone:
        raise ValueError('DRIVER_QA_PHONE must differ from EXCAVATOR_QA_PHONE.')
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
        defaults={
            'body_volume_m3': '49.40',
            'payload_tons': '130',
            'fuel_capacity_limit_l': 2000,
            'is_active': True,
        },
    )
    equipment_scope = {'truck': truck_type, 'excavator': excavator_type}
    for (
        name,
        short_label,
        is_critical,
        sort_order,
        equipment_scope_name,
        show_for_truck_driver,
        show_for_excavator_operator,
    ) in QA_DOWNTIME_REASONS:
        DowntimeReason.objects.update_or_create(
            name=name,
            defaults={
                'short_label': short_label,
                'equipment_type': equipment_scope.get(equipment_scope_name),
                'is_critical': is_critical,
                'show_for_truck_driver': show_for_truck_driver,
                'show_for_excavator_operator': show_for_excavator_operator,
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
    human_driver = _active_employee(
        QA_HUMAN_DRIVER_NUMBER,
        'Тестовый водитель RuStore',
        phone=driver_phone,
        work_category=Employee.WorkCategory.DRIVER,
    )
    EmployeeAccess.objects.update_or_create(
        employee=human_driver,
        role=driver_role,
        defaults={
            'access_code': driver_pin,
            'status': EmployeeAccess.Status.ACTIVATED,
            'is_active': True,
            'activated_at': timezone.now(),
        },
    )
    driver_bot_operator = _active_employee(
        QA_DRIVER_BOT_OPERATOR_NUMBER,
        'QA-бот машинист для водителя',
        work_category=Employee.WorkCategory.EXCAVATOR_OPERATOR,
    )
    EmployeeAccess.objects.update_or_create(
        employee=driver_bot_operator,
        role=excavator_role,
        defaults={
            # У бота нет телефона, поэтому это не публичные реквизиты входа.
            'access_code': '899999',
            'status': EmployeeAccess.Status.ACTIVATED,
            'is_active': True,
            'activated_at': timezone.now(),
        },
    )
    plan_trips = Decimal(int(getattr(settings, 'EXCAVATOR_QA_PLAN_TRIPS', 20)))
    plan_group, _ = EquipmentPlanGroup.objects.update_or_create(
        code=QA_EXCAVATOR_PLAN_GROUP_CODE,
        defaults={
            'name': 'Тестовый план экскаватора',
            'calculation_mode': PlanCalculationMode.TRIPS,
            'plan_value': plan_trips,
            'is_active': True,
            'active_from': production_work_date(),
            'updated_by': dispatcher,
            'comment': 'Изолированный сценарий закрытого тестирования RuStore.',
        },
    )
    plan_group.equipment.set([excavator])
    for open_shift in EmployeeShift.objects.filter(
        employee=operator,
        equipment=excavator,
        closed_at__isnull=True,
    ):
        if (
            open_shift.plan_group_id != plan_group.id
            or open_shift.plan_status != PlanAssignmentStatus.ASSIGNED
            or open_shift.plan_calculation_mode != PlanCalculationMode.TRIPS
            or open_shift.plan_value != plan_trips
        ):
            assign_shift_plan_snapshot(open_shift)
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

    human_driver_truck, _ = Equipment.objects.update_or_create(
        garage_number=QA_DRIVER_TRUCK_GARAGE,
        defaults={
            'equipment_type': truck_type,
            'model': truck_model,
            'is_active': True,
        },
    )
    human_driver_assignment = (
        EquipmentAssignment.objects
        .filter(
            employee=human_driver,
            equipment=human_driver_truck,
            role=driver_role,
            ended_at__isnull=True,
            shift__isnull=True,
        )
        .order_by('-assigned_at')
        .first()
    )
    if not human_driver_assignment:
        EquipmentAssignment.objects.create(
            employee=human_driver,
            equipment=human_driver_truck,
            role=driver_role,
            shift_type='day',
            assigned_by=dispatcher,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

    driver_plan_group, _ = EquipmentPlanGroup.objects.update_or_create(
        code=QA_DRIVER_PLAN_GROUP_CODE,
        defaults={
            'name': 'Тестовый план водителя',
            'calculation_mode': PlanCalculationMode.TRIPS,
            'plan_value': plan_trips,
            'is_active': True,
            'active_from': production_work_date(),
            'updated_by': dispatcher,
            'comment': 'Изолированный водительский сценарий RuStore QA.',
        },
    )
    driver_plan_group.equipment.set([human_driver_truck])
    for open_shift in EmployeeShift.objects.filter(
        employee=human_driver,
        equipment=human_driver_truck,
        closed_at__isnull=True,
    ):
        if (
            open_shift.plan_group_id != driver_plan_group.id
            or open_shift.plan_status != PlanAssignmentStatus.ASSIGNED
            or open_shift.plan_calculation_mode != PlanCalculationMode.TRIPS
            or open_shift.plan_value != plan_trips
        ):
            assign_shift_plan_snapshot(open_shift)

    driver_bot_excavator, _ = Equipment.objects.update_or_create(
        garage_number=QA_DRIVER_EXCAVATOR_GARAGE,
        defaults={
            'equipment_type': excavator_type,
            'model': excavator_model,
            'is_active': True,
        },
    )
    driver_bot_placement, _ = ExcavatorPlacement.objects.get_or_create(
        excavator=driver_bot_excavator,
        defaults={
            'zone': ExcavatorPlacement.Zone.ACTIVE,
            'work_rock_type': rock,
            'work_dump_point': dump_point,
            'loading_horizon': '240',
            'loading_block': '2',
            'changed_by': dispatcher,
            'work_context_updated_at': timezone.now(),
        },
    )
    if driver_bot_placement.zone != ExcavatorPlacement.Zone.ACTIVE:
        driver_bot_placement.zone = ExcavatorPlacement.Zone.ACTIVE
        driver_bot_placement.changed_by = dispatcher
        driver_bot_placement.save(update_fields=['zone', 'changed_by', 'changed_at'])

    driver_bot_shift = (
        EmployeeShift.objects
        .filter(
            employee=driver_bot_operator,
            equipment=driver_bot_excavator,
            closed_at__isnull=True,
        )
        .order_by('-opened_at')
        .first()
    )
    if not driver_bot_shift:
        from shifts.services import open_excavator_shift

        action_sequence = EmployeeShift.objects.filter(
            employee=driver_bot_operator,
            equipment=driver_bot_excavator,
        ).count() + 1
        response = open_excavator_shift(
            employee=driver_bot_operator,
            equipment=driver_bot_excavator,
            shift_type='day',
            fuel_value='6000',
            engine_hours_value='1200',
            client_action_id=f'qa-driver-bot-shift-{action_sequence}',
        )
        driver_bot_shift = EmployeeShift.objects.get(pk=response['shift_id'])
    driver_bot_assignment = (
        EquipmentAssignment.objects
        .filter(
            employee=driver_bot_operator,
            equipment=driver_bot_excavator,
            role=excavator_role,
            shift=driver_bot_shift,
            ended_at__isnull=True,
        )
        .order_by('-assigned_at')
        .first()
    )
    if not driver_bot_assignment:
        EquipmentAssignment.objects.create(
            employee=driver_bot_operator,
            equipment=driver_bot_excavator,
            role=excavator_role,
            shift=driver_bot_shift,
            shift_type='day',
            assigned_by=dispatcher,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

    return ExcavatorQAScenario(
        operator=operator,
        dispatcher=dispatcher,
        excavator=excavator,
        trucks=tuple(trucks),
        human_driver=human_driver,
        human_driver_truck=human_driver_truck,
        driver_bot_operator=driver_bot_operator,
        driver_bot_excavator=driver_bot_excavator,
    )


def _complete_next_due_trip(scenario: ExcavatorQAScenario, now) -> int:
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
        )
        .order_by('created_at', 'id')
        .values_list('id', flat=True)
    )
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
            # Один тик имитирует одну занятую разгрузочную операцию. Даже если
            # несколько машин приехали одновременно, очередь выпускает их
            # последовательно, а не меняет весь экран одной пачкой.
            return 1
    return 0


def _load_human_driver_truck(
    scenario: ExcavatorQAScenario,
    *,
    assignment: HaulAssignment,
    now,
) -> tuple[Trip | None, str]:
    """Let the QA bot excavator load, but never unload, the human truck."""
    from trips.views import (
        TRUCK_WAITING_LOADING_REASON,
        close_excavator_open_downtimes,
        close_truck_downtime_for_reason,
        excavator_truck_load_block,
        notify_driver_truck_loaded,
        reconcile_excavator_waiting_for_trucks,
        truck_post_unload_cooldown,
    )

    trip = None
    with transaction.atomic():
        driver_shift = (
            EmployeeShift.objects
            .select_for_update(of=('self',))
            .filter(
                employee=scenario.human_driver,
                equipment=scenario.human_driver_truck,
                closed_at__isnull=True,
            )
            .order_by('-opened_at')
            .first()
        )
        bot_shift = (
            EmployeeShift.objects
            .select_for_update(of=('self',))
            .filter(
                employee=scenario.driver_bot_operator,
                equipment=scenario.driver_bot_excavator,
                closed_at__isnull=True,
            )
            .order_by('-opened_at')
            .first()
        )
        locked_assignment = (
            HaulAssignment.objects
            .select_for_update(of=('self',))
            .select_related('truck', 'truck__model', 'excavator')
            .filter(
                pk=assignment.pk,
                truck=scenario.human_driver_truck,
                excavator=scenario.driver_bot_excavator,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
            )
            .first()
        )
        if not driver_shift or not bot_shift or not locked_assignment:
            return None, 'waiting_for_assignment'

        active_trip = (
            Trip.objects
            .select_for_update(of=('self',))
            .filter(
                truck=scenario.human_driver_truck,
                status__in=OPEN_TRIP_STATUSES,
            )
            .order_by('-created_at')
            .first()
        )
        if active_trip:
            return active_trip, 'loaded_waiting_unload'

        cooldown = truck_post_unload_cooldown(
            scenario.human_driver_truck,
            now=now,
        )
        load_block = excavator_truck_load_block(
            locked_assignment,
            current_excavator=scenario.driver_bot_excavator,
            active_trip=False,
            post_unload_cooldown=cooldown or False,
            has_open_truck_shift=True,
            has_driver_assignment=True,
        )
        if load_block:
            return None, load_block['code']

        placement = ExcavatorPlacement.objects.select_related(
            'work_rock_type',
            'work_dump_point',
        ).get(excavator=scenario.driver_bot_excavator)
        if not placement.work_rock_type_id or not placement.work_dump_point_id:
            return None, 'work_context_missing'

        trip = create_loaded_waiting_unload_trip(
            assignment=locked_assignment,
            excavator_operator=scenario.driver_bot_operator,
            loading_shift=bot_shift,
            rock_type=placement.work_rock_type,
            dump_point=placement.work_dump_point,
            loading_horizon=placement.loading_horizon,
            loading_block=placement.loading_block,
            note='Погрузка создана изолированным RuStore QA-симулятором.',
        )
        close_truck_downtime_for_reason(
            scenario.human_driver_truck,
            TRUCK_WAITING_LOADING_REASON,
        )
        close_excavator_open_downtimes(scenario.driver_bot_excavator)
        reconcile_excavator_waiting_for_trucks(
            scenario.driver_bot_excavator,
            scenario.driver_bot_operator,
            start_when_empty=True,
        )
        bump_operational_state(
            'QA simulator:driver_truck_loaded',
            event_type='trip_changed',
            object_type='Trip',
            object_id=trip.id,
            payload={
                'action': 'truck_loaded',
                'trip_id': trip.id,
                'truck_id': trip.truck_id,
                'excavator_id': trip.excavator_id,
                'dump_point_id': trip.dump_point_id,
                'assigned_dump_point_id': trip.assigned_dump_point_id,
                'actual_dump_point_id': trip.actual_dump_point_id,
                'status': TripStatus.LOADED_WAITING_UNLOAD,
                'source': 'excavator_qa_simulator',
            },
        )

    notify_driver_truck_loaded(trip)
    return trip, 'loaded_waiting_unload'


def _run_human_driver_tick(scenario: ExcavatorQAScenario, now) -> dict:
    driver_shift = (
        EmployeeShift.objects
        .filter(
            employee=scenario.human_driver,
            equipment=scenario.human_driver_truck,
            closed_at__isnull=True,
        )
        .order_by('-opened_at')
        .first()
    )
    if not driver_shift:
        return {
            'driver_state': 'waiting_for_driver_shift',
            'driver_assigned': 0,
            'driver_loaded': 0,
            'driver_open_trips': 0,
        }

    assignment = (
        HaulAssignment.objects
        .filter(
            truck=scenario.human_driver_truck,
            excavator=scenario.driver_bot_excavator,
            ended_at__isnull=True,
            status__in=(AssignmentStatus.PENDING, AssignmentStatus.ACCEPTED),
        )
        .order_by('-accepted_at', '-assigned_at', '-id')
        .first()
    )
    created = False
    if not assignment:
        assignment, created = schedule_haul_assignment(
            truck=scenario.human_driver_truck,
            excavator=scenario.driver_bot_excavator,
            assigned_by=scenario.dispatcher,
            now=now,
        )

    open_trip = (
        Trip.objects
        .filter(
            truck=scenario.human_driver_truck,
            status__in=OPEN_TRIP_STATUSES,
        )
        .order_by('-created_at')
        .first()
    )
    if open_trip:
        return {
            'driver_state': 'loaded_waiting_unload',
            'driver_assigned': int(created),
            'driver_loaded': 0,
            'driver_open_trips': 1,
        }
    if assignment.status == AssignmentStatus.PENDING:
        return {
            'driver_state': 'assignment_pending',
            'driver_assigned': int(created),
            'driver_loaded': 0,
            'driver_open_trips': 0,
        }

    loading_started_at = assignment.accepted_at or assignment.assigned_at
    loading_seconds = int(getattr(settings, 'DRIVER_QA_LOADING_SECONDS', 8))
    if loading_started_at and now < loading_started_at + timedelta(seconds=loading_seconds):
        return {
            'driver_state': 'waiting_for_bot_load',
            'driver_assigned': int(created),
            'driver_loaded': 0,
            'driver_open_trips': 0,
        }

    trip, state = _load_human_driver_truck(
        scenario,
        assignment=assignment,
        now=now,
    )
    return {
        'driver_state': state,
        'driver_assigned': int(created),
        'driver_loaded': int(trip is not None and trip.status == TripStatus.LOADED_WAITING_UNLOAD),
        'driver_open_trips': int(
            trip is not None and trip.status in OPEN_TRIP_STATUSES
        ),
    }


def run_excavator_qa_tick(*, now=None) -> dict:
    require_excavator_qa_environment()
    now = now or timezone.now()
    scenario = load_excavator_qa_scenario()
    driver_result = _run_human_driver_tick(scenario, now)
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
        return {
            'state': 'waiting_for_excavator_shift',
            'assigned': 0,
            'completed': 0,
            **driver_result,
        }

    completed = _complete_next_due_trip(scenario, now)
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
        **driver_result,
    }
