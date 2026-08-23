#!/usr/bin/env python
"""Prepare and load one realistic active Dispatcher shift in isolated QA."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import transaction  # noqa: E402

from assignments.models import (  # noqa: E402
    AssignmentStatus,
    CrewPlan,
    CrewPlanStatus,
    CrewPlanSlot,
    EquipmentAssignment,
    ExcavatorPlacement,
    HaulAssignment,
)
from core.production_time import production_shift_context  # noqa: E402
from core.pwa_performance_qa import (  # noqa: E402
    validate_pwa_performance_qa_run_id,
    verify_pwa_performance_qa_database,
)
from downtimes.models import DowntimeEvent  # noqa: E402
from references.models import Equipment  # noqa: E402
from shifts.models import EmployeeShift  # noqa: E402
from tools.full_pwa_traffic_audit import (  # noqa: E402
    ARTIFACT_ROOT_NAME,
    validate_artifact_output_path,
    write_canonical_new_json,
)
from tools.full_week_qa import (  # noqa: E402
    FullWeekRunner,
    ReferenceCatalog,
    RoleHttpClient,
    RunConfig,
    StaffMember,
    WeekOnboarding,
)
from tools.prepare_rating_30d_qa_database import (  # noqa: E402
    REFERENCE_MODELS_BY_LABEL,
    clear_reference_tables,
    lock_preparation_tables,
    validate_fixture,
    verify_no_business_data,
)
from trips.models import OPEN_TRIP_STATUSES, Trip  # noqa: E402
from users.models import (  # noqa: E402
    DriverPrimaryRegistration,
    Employee,
    EmployeeAccess,
)


DEFAULT_RUN_ID = 'PWA-PERF-20260823-DISPATCHER-01'
DEFAULT_MARKER = 'ТЕСТ_ДИСПЕТЧЕР_PERF_20260823'
REFERENCE_FIXTURE = Path(
    r'C:\Users\swwba\AppData\Local\Temp'
    r'\codex-pg16-pwa-perf-20260823-01\reference-fixture.json'
)
REFERENCE_FIXTURE_SHA256 = (
    '2144E2444172654780C4F37BB96C41DD6DFACDFD5C2235FE11D11673A266F35D'
)
EXPECTED_EXCAVATOR_GARAGES = frozenset(str(value) for value in range(1, 9))


class DispatcherPerformanceQaError(RuntimeError):
    """Fail-closed stop for dispatcher performance data preparation."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'phase',
        choices=('prepare', 'seal-open', 'late-load'),
    )
    parser.add_argument('--run-id', default=DEFAULT_RUN_ID)
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest().upper()


def scenario_artifact_dir(run_id: str, *, require_empty: bool) -> Path:
    normalized_run_id = validate_pwa_performance_qa_run_id(run_id)
    root = (
        Path(tempfile.gettempdir()).resolve()
        / ARTIFACT_ROOT_NAME
        / normalized_run_id
        / 'scenario'
    )
    current = Path(tempfile.gettempdir()).resolve()
    for part in root.relative_to(current).parts:
        current = current / part
        if current.exists() and (
            current.is_symlink()
            or int(getattr(current.lstat(), 'st_file_attributes', 0)) & 0x400
        ):
            raise DispatcherPerformanceQaError(
                f'Artifact path contains a reparse boundary: {current}'
            )
    if require_empty and root.exists() and any(root.iterdir()):
        raise DispatcherPerformanceQaError(
            f'Scenario artifact directory is not empty: {root}'
        )
    root.mkdir(parents=True, exist_ok=True)
    validate_artifact_output_path(root / 'probe.json')
    return root


def load_reference_fixture() -> dict[str, int]:
    fixture = REFERENCE_FIXTURE.resolve()
    if not fixture.is_file() or sha256_file(fixture) != REFERENCE_FIXTURE_SHA256:
        raise DispatcherPerformanceQaError(
            'Разрешённый reference fixture отсутствует или изменён.'
        )
    expected_counts = validate_fixture(fixture)
    verify_no_business_data()
    with transaction.atomic():
        lock_preparation_tables()
        verify_no_business_data()
        clear_reference_tables()
        call_command('loaddata', str(fixture), verbosity=0)
        actual_counts = Counter({
            label: model._default_manager.count()
            for label, model in REFERENCE_MODELS_BY_LABEL.items()
        })
        if actual_counts != expected_counts:
            raise DispatcherPerformanceQaError(
                'Загруженные справочники не совпали с fixture.'
            )
    return dict(sorted(expected_counts.items()))


def normalize_excavator_scope() -> list[dict[str, object]]:
    excavators = list(
        Equipment.objects.filter(
            equipment_type__name__iexact='Экскаватор',
        ).order_by('garage_number')
    )
    by_garage = {item.garage_number: item for item in excavators}
    if not EXPECTED_EXCAVATOR_GARAGES.issubset(by_garage):
        raise DispatcherPerformanceQaError(
            'В справочнике нет полного набора Экскаваторов №1–8.'
        )
    unexpected_active = {
        item.garage_number
        for item in excavators
        if item.is_active
        and item.garage_number not in EXPECTED_EXCAVATOR_GARAGES
    }
    if unexpected_active != {'QA-CSS-23'}:
        raise DispatcherPerformanceQaError(
            'Неожиданный активный набор тестовых Экскаваторов.'
        )
    changes = []
    with transaction.atomic():
        synthetic = by_garage['QA-CSS-23']
        synthetic.is_active = False
        synthetic.save(update_fields=['is_active'])
        changes.append(
            {'garage_number': 'QA-CSS-23', 'from': True, 'to': False}
        )
        for garage_number in ('4', '7', '8'):
            excavator = by_garage[garage_number]
            if excavator.is_active:
                raise DispatcherPerformanceQaError(
                    f'Экскаватор №{garage_number} неожиданно уже активен.'
                )
            excavator.is_active = True
            excavator.save(update_fields=['is_active'])
            changes.append(
                {'garage_number': garage_number, 'from': False, 'to': True}
            )
    active = set(
        Equipment.objects.filter(
            equipment_type__name__iexact='Экскаватор',
            is_active=True,
        ).values_list('garage_number', flat=True)
    )
    if active != EXPECTED_EXCAVATOR_GARAGES:
        raise DispatcherPerformanceQaError(
            f'QA-нормализация дала неверный парк Экскаваторов: {active!r}.'
        )
    return changes


def scenario_counts() -> dict[str, int]:
    return {
        'employees': Employee.objects.count(),
        'active_accesses': EmployeeAccess.objects.filter(is_active=True).count(),
        'driver_registrations': DriverPrimaryRegistration.objects.count(),
        'crew_plans': CrewPlan.objects.count(),
        'published_crew_plans': CrewPlan.objects.filter(
            status=CrewPlanStatus.PUBLISHED,
        ).count(),
        'draft_crew_plans': CrewPlan.objects.filter(
            status=CrewPlanStatus.DRAFT,
        ).count(),
        'crew_plan_slots': CrewPlanSlot.objects.count(),
        'equipment_assignments': EquipmentAssignment.objects.count(),
        'employee_shifts': EmployeeShift.objects.count(),
        'open_employee_shifts': EmployeeShift.objects.filter(
            closed_at__isnull=True,
        ).count(),
        'active_excavator_placements': ExcavatorPlacement.objects.filter(
            zone=ExcavatorPlacement.Zone.ACTIVE,
        ).count(),
        'accepted_haul_assignments': HaulAssignment.objects.filter(
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        ).count(),
        'pending_haul_assignments': HaulAssignment.objects.filter(
            status=AssignmentStatus.PENDING,
            ended_at__isnull=True,
        ).count(),
        'trips': Trip.objects.count(),
        'completed_trips': Trip.objects.filter(status='completed').count(),
        'open_trips': Trip.objects.filter(status__in=OPEN_TRIP_STATUSES).count(),
        'open_downtimes': DowntimeEvent.objects.filter(ended_at__isnull=True).count(),
    }


def assert_open_phase_counts(counts: dict[str, int]) -> None:
    expected = {
        'employees': 256,
        'active_accesses': 256,
        'driver_registrations': 212,
        'crew_plans': 4,
        'published_crew_plans': 2,
        'draft_crew_plans': 2,
        'crew_plan_slots': 244,
        'equipment_assignments': 122,
        'employee_shifts': 64,
        'open_employee_shifts': 63,
        'active_excavator_placements': 8,
        'accepted_haul_assignments': 53,
        'pending_haul_assignments': 0,
        'trips': 0,
        'completed_trips': 0,
        'open_trips': 0,
        'open_downtimes': 8,
    }
    failures = {
        key: {'actual': counts.get(key), 'expected': value}
        for key, value in expected.items()
        if counts.get(key) != value
    }
    if failures:
        raise DispatcherPerformanceQaError(
            f'Активный срез Диспетчера неполон: {failures!r}.'
        )

    open_downtimes = list(
        DowntimeEvent.objects.filter(ended_at__isnull=True)
        .values_list('equipment__garage_number', 'reason__name', 'comment')
        .order_by('equipment__garage_number')
    )
    if (
        {row[0] for row in open_downtimes} != EXPECTED_EXCAVATOR_GARAGES
        or {row[1] for row in open_downtimes} != {'Перегон экскаватора'}
        or {row[2] for row in open_downtimes}
        != {'Автоматически по производственному событию'}
    ):
        raise DispatcherPerformanceQaError(
            'Начальные автоматические простои Экскаваторов имеют '
            'неожиданную причину или состав.'
        )


def _reference_counts_without_loading() -> dict[str, int]:
    fixture = REFERENCE_FIXTURE.resolve()
    if not fixture.is_file() or sha256_file(fixture) != REFERENCE_FIXTURE_SHA256:
        raise DispatcherPerformanceQaError(
            'Разрешённый reference fixture отсутствует или изменён.'
        )
    expected_counts = validate_fixture(fixture)
    actual_counts = Counter({
        label: model._default_manager.count()
        for label, model in REFERENCE_MODELS_BY_LABEL.items()
    })
    if actual_counts != expected_counts:
        raise DispatcherPerformanceQaError(
            'Текущие QA-справочники не совпадают с разрешённым fixture.'
        )
    return dict(sorted(expected_counts.items()))


def _qa_normalization_manifest():
    active_excavators = set(
        Equipment.objects.filter(
            equipment_type__name__iexact='Экскаватор',
            is_active=True,
        ).values_list('garage_number', flat=True)
    )
    synthetic = Equipment.objects.filter(
        equipment_type__name__iexact='Экскаватор',
        garage_number='QA-CSS-23',
        is_active=False,
    ).exists()
    if active_excavators != EXPECTED_EXCAVATOR_GARAGES or not synthetic:
        raise DispatcherPerformanceQaError(
            'QA-only normalization is absent or no longer exact.'
        )
    return [
        {'garage_number': 'QA-CSS-23', 'from': True, 'to': False},
        {'garage_number': '4', 'from': False, 'to': True},
        {'garage_number': '7', 'from': False, 'to': True},
        {'garage_number': '8', 'from': False, 'to': True},
    ]


def _build_open_manifest(
    *,
    run_id: str,
    artifact_dir: Path,
    database: dict[str, object],
    reference_counts: dict[str, int],
    normalization: list[dict[str, object]],
    config: RunConfig,
    catalog: ReferenceCatalog,
    shift_index: int,
) -> dict[str, object]:
    open_time, close_time = FullWeekRunner(
        config,
        catalog,
        _rebuild_minimal_runner_onboarding(config, catalog),
    ).shift_bounds(shift_index)
    counts = scenario_counts()
    assert_open_phase_counts(counts)
    dispatcher_shift = (
        EmployeeShift.objects.filter(
            employee__full_name__startswith=config.marker,
            workplace_code='dispatcher',
            closed_at__isnull=True,
        )
        .select_related('employee')
        .get()
    )
    dispatcher_access = EmployeeAccess.objects.get(
        employee=dispatcher_shift.employee,
        role__code='dispatcher',
        is_active=True,
        status=EmployeeAccess.Status.ACTIVATED,
    )
    payload = {
        'schema': 'copper-dispatcher-performance-qa-scenario',
        'schema_version': 1,
        'synthetic': True,
        'official': False,
        'run_id': run_id,
        'marker': DEFAULT_MARKER,
        'production_date': config.start_date.isoformat(),
        'shift_index': shift_index,
        'shift_type': 'day' if shift_index == 0 else 'night',
        'open_time': open_time.isoformat(),
        'close_time': close_time.isoformat(),
        'database_fingerprint': database['fingerprint'],
        'dispatcher_shift_id': dispatcher_shift.id,
        'dispatcher_employee_id': dispatcher_shift.employee_id,
        'dispatcher_access_id': dispatcher_access.id,
        'reference_fixture': {
            'sha256': REFERENCE_FIXTURE_SHA256,
            'rows': sum(reference_counts.values()),
            'model_counts': reference_counts,
        },
        'qa_only_equipment_normalization': normalization,
        'active_trucks': len(catalog.trucks),
        'active_excavators': len(catalog.excavators),
        'counts': counts,
        'initial_open_downtime_reason': 'Перегон экскаватора',
        'late_load_added': False,
    }
    write_canonical_new_json(
        artifact_dir / 'scenario_manifest.json',
        payload,
    )
    return payload


def _rebuild_minimal_runner_onboarding(
    config: RunConfig,
    catalog: ReferenceCatalog,
) -> WeekOnboarding:
    onboarding = WeekOnboarding(config, catalog)
    access = (
        EmployeeAccess.objects.filter(
            employee__full_name__startswith=config.marker,
            role__code='deputy_mining_manager',
            is_active=True,
        )
        .select_related('employee', 'role')
        .get()
    )
    onboarding.deputy = StaffMember(
        employee_id=access.employee_id,
        access_id=access.id,
        role_code='deputy_mining_manager',
        phone=access.employee.phone,
        permanent_pin=access.access_code,
        brigade=None,
        ordinal=access.employee_id,
        client=RoleHttpClient('deputy_mining_manager'),
    )
    return onboarding


def prepare_open_phase(run_id: str) -> dict[str, object]:
    artifact_dir = scenario_artifact_dir(run_id, require_empty=True)
    database = verify_pwa_performance_qa_database(run_id)
    reference_counts = load_reference_fixture()
    normalization = normalize_excavator_scope()
    context = production_shift_context()
    config = RunConfig(
        run_id=run_id,
        marker=DEFAULT_MARKER,
        start_date=context.production_date,
        artifact_dir=artifact_dir,
    )
    catalog = ReferenceCatalog(config)
    onboarding = WeekOnboarding(config, catalog).run()
    runner = FullWeekRunner(config, catalog, onboarding)
    runner.publish_daily_plans(0)
    shift_index = 0 if context.shift_type == 'day' else 1
    open_time, close_time = runner.shift_bounds(shift_index)
    dispatcher, mining_master = runner.open_shift_roles(
        shift_index=shift_index,
        open_time=open_time,
    )
    runner.open_equipment_shifts(
        shift_index=shift_index,
        open_time=open_time,
    )
    runner.establish_initial_complexes(
        shift_index=shift_index,
        dispatcher=dispatcher,
        mining_master=mining_master,
        when=open_time + timedelta(minutes=3),
    )
    runner.apply_excavator_settings(
        shift_index=shift_index,
        when=open_time + timedelta(minutes=5),
    )
    return _build_open_manifest(
        run_id=run_id,
        artifact_dir=artifact_dir,
        database=database,
        reference_counts=reference_counts,
        normalization=normalization,
        config=config,
        catalog=catalog,
        shift_index=shift_index,
    )


def seal_existing_open_phase(run_id: str) -> dict[str, object]:
    artifact_dir = scenario_artifact_dir(run_id, require_empty=True)
    database = verify_pwa_performance_qa_database(run_id)
    reference_counts = _reference_counts_without_loading()
    normalization = _qa_normalization_manifest()
    open_dispatcher_shift = (
        EmployeeShift.objects.filter(
            employee__full_name__startswith=DEFAULT_MARKER,
            workplace_code='dispatcher',
            closed_at__isnull=True,
        )
        .select_related('employee')
        .get()
    )
    context = production_shift_context(open_dispatcher_shift.opened_at)
    shift_index = 0 if context.shift_type == 'day' else 1
    config = RunConfig(
        run_id=run_id,
        marker=DEFAULT_MARKER,
        start_date=context.production_date,
        artifact_dir=artifact_dir,
    )
    catalog = ReferenceCatalog(config)
    return _build_open_manifest(
        run_id=run_id,
        artifact_dir=artifact_dir,
        database=database,
        reference_counts=reference_counts,
        normalization=normalization,
        config=config,
        catalog=catalog,
        shift_index=shift_index,
    )


def _load_scenario_manifest(artifact_dir: Path, run_id: str):
    path = artifact_dir / 'scenario_manifest.json'
    if not path.is_file():
        raise DispatcherPerformanceQaError('Open-phase manifest not found.')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if (
        payload.get('schema') != 'copper-dispatcher-performance-qa-scenario'
        or payload.get('run_id') != run_id
        or payload.get('late_load_added') is not False
    ):
        raise DispatcherPerformanceQaError('Open-phase manifest is invalid.')
    return payload


def _rebuild_onboarding(
    config: RunConfig,
    catalog: ReferenceCatalog,
    shift_index: int,
) -> WeekOnboarding:
    onboarding = WeekOnboarding(config, catalog)
    assignment_by_employee = {
        item.employee_id: item.equipment_id
        for item in EquipmentAssignment.objects.filter(
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            role__code__in=('driver', 'excavator_operator'),
        ).select_related('role')
    }
    required_client_roles = {
        'deputy_mining_manager',
        'driver',
        'excavator_operator',
    }
    access_rows = (
        EmployeeAccess.objects.filter(
            employee__full_name__startswith=config.marker,
            is_active=True,
        )
        .select_related('employee', 'role')
        .order_by('employee_id')
    )
    for access in access_rows:
        employee = access.employee
        role_code = access.role.code
        client = None
        if role_code in required_client_roles:
            client = RoleHttpClient(role_code)
            client.login(
                employee.phone,
                access.access_code,
                device_kind=(
                    'shared'
                    if role_code in {'dispatcher', 'mining_master'}
                    else 'personal'
                ),
            )
        member = StaffMember(
            employee_id=employee.id,
            access_id=access.id,
            role_code=role_code,
            phone=employee.phone,
            permanent_pin=access.access_code,
            brigade=employee.brigade_number,
            ordinal=employee.id,
            equipment_id=assignment_by_employee.get(employee.id),
            client=client,
        )
        onboarding._register_member(member)
        if role_code == 'deputy_mining_manager':
            onboarding.deputy = member
        elif role_code in {'dispatcher', 'mining_master'}:
            if employee.brigade_number is not None:
                onboarding.shift_roles_by_brigade[role_code][
                    employee.brigade_number
                ] = member
        elif role_code == 'driver' and member.equipment_id:
            onboarding.drivers_by_brigade[employee.brigade_number][
                member.equipment_id
            ] = member
        elif role_code == 'excavator_operator' and member.equipment_id:
            onboarding.operators_by_brigade[employee.brigade_number][
                member.equipment_id
            ] = member
    if shift_index not in {0, 1}:
        raise DispatcherPerformanceQaError(
            'Dispatcher performance slice supports only its first day/night.'
        )
    brigade = 1 if shift_index == 0 else 3
    for role_code, expected in (
        ('driver', len(catalog.trucks)),
        ('excavator_operator', len(catalog.excavators)),
    ):
        selected = (
            onboarding.drivers_by_brigade[brigade]
            if role_code == 'driver'
            else onboarding.operators_by_brigade[brigade]
        )
        if len(selected) != expected or any(
            member.client is None for member in selected.values()
        ):
            raise DispatcherPerformanceQaError(
                f'Не восстановлены клиенты текущей смены {role_code}.'
            )
    if not onboarding.deputy or not onboarding.deputy.client:
        raise DispatcherPerformanceQaError('Не восстановлен клиент заместителя.')
    return onboarding


def add_late_load(run_id: str) -> dict[str, object]:
    artifact_dir = scenario_artifact_dir(run_id, require_empty=False)
    started_path = artifact_dir / 'late_load_started.json'
    completion_path = artifact_dir / 'scenario_completion.json'
    late_manifest_path = artifact_dir / 'late_load_manifest.json'
    validate_artifact_output_path(started_path)
    validate_artifact_output_path(completion_path)
    validate_artifact_output_path(late_manifest_path)
    if (
        started_path.exists()
        or completion_path.exists()
        or late_manifest_path.exists()
    ):
        raise DispatcherPerformanceQaError(
            'Late-load phase is already claimed, complete, or has immutable evidence.'
        )
    manifest = _load_scenario_manifest(artifact_dir, run_id)
    database = verify_pwa_performance_qa_database(run_id)
    if database['fingerprint'] != manifest['database_fingerprint']:
        raise DispatcherPerformanceQaError('Database fingerprint changed.')
    config = RunConfig(
        run_id=run_id,
        marker=manifest['marker'],
        start_date=date.fromisoformat(manifest['production_date']),
        artifact_dir=artifact_dir,
    )
    catalog = ReferenceCatalog(config)
    shift_index = int(manifest['shift_index'])
    initial_counts = scenario_counts()
    assert_open_phase_counts(initial_counts)
    if Trip.objects.exists():
        raise DispatcherPerformanceQaError(
            'Late-load phase requires exactly zero trips before it starts.'
        )
    for evidence_name in ('trip_manifest.jsonl', 'action_log.jsonl'):
        if (artifact_dir / evidence_name).exists():
            raise DispatcherPerformanceQaError(
                'Partial late-load evidence exists; retry is forbidden.'
            )
    write_canonical_new_json(
        started_path,
        {
            'schema': 'copper-dispatcher-performance-qa-late-load-claim',
            'schema_version': 1,
            'synthetic': True,
            'official': False,
            'run_id': run_id,
            'database_fingerprint': database['fingerprint'],
            'initial_counts': initial_counts,
        },
    )
    onboarding = _rebuild_onboarding(config, catalog, shift_index)
    runner = FullWeekRunner(config, catalog, onboarding)
    runner.current_haul_map = runner._active_haul_map()
    open_time, close_time = runner.shift_bounds(shift_index)
    contexts = runner.apply_excavator_settings(
        shift_index=shift_index,
        when=open_time + timedelta(minutes=5),
    )
    driver_shifts = {
        shift.equipment_id: shift
        for shift in EmployeeShift.objects.filter(
            workplace_code='driver',
            closed_at__isnull=True,
        )
    }
    operator_shifts = {
        shift.equipment_id: shift
        for shift in EmployeeShift.objects.filter(
            workplace_code='excavator_operator',
            closed_at__isnull=True,
        )
    }
    started = time.perf_counter()
    carry_trip_id, carry_truck_id = runner.execute_trip_cycle(
        shift_index=shift_index,
        open_time=open_time,
        close_time=close_time,
        contexts=contexts,
        driver_shifts=driver_shifts,
        operator_shifts=operator_shifts,
    )
    duration_seconds = time.perf_counter() - started
    counts = scenario_counts()
    expected_loaded = sum(
        runner.target_trip_count(index, shift_index)
        for index, _truck in enumerate(catalog.trucks)
    ) + 1
    expected_completed = expected_loaded - 1
    failures = {}
    expected_counts = {
        'open_employee_shifts': 63,
        'accepted_haul_assignments': 53,
        'trips': expected_loaded,
        'completed_trips': expected_completed,
        'open_trips': 1,
    }
    for key, expected in expected_counts.items():
        if counts.get(key) != expected:
            failures[key] = {'actual': counts.get(key), 'expected': expected}
    trip_distribution = {
        truck.garage_number: Trip.objects.filter(
            truck=truck,
            status='completed',
        ).count()
        for truck in catalog.trucks
    }
    if any(not 17 <= value <= 23 for value in trip_distribution.values()):
        failures['trip_distribution'] = trip_distribution
    if failures:
        raise DispatcherPerformanceQaError(
            f'Late-load phase violated its contract: {failures!r}.'
        )
    payload = {
        'schema': 'copper-dispatcher-performance-qa-late-load',
        'schema_version': 1,
        'synthetic': True,
        'official': False,
        'run_id': run_id,
        'duration_seconds': round(duration_seconds, 3),
        'carryover_trip_id': carry_trip_id,
        'carryover_truck_id': carry_truck_id,
        'trip_distribution_min': min(trip_distribution.values()),
        'trip_distribution_max': max(trip_distribution.values()),
        'counts': counts,
    }
    write_canonical_new_json(late_manifest_path, payload)
    write_canonical_new_json(
        completion_path,
        {
            'schema': 'copper-dispatcher-performance-qa-completion',
            'schema_version': 1,
            'run_id': run_id,
            'late_load_added': True,
            'late_load_manifest_sha256': sha256_file(late_manifest_path),
            'late_load_claim_sha256': sha256_file(started_path),
            'trip_manifest_sha256': sha256_file(
                artifact_dir / 'trip_manifest.jsonl'
            ),
        },
    )
    return payload


def main(argv=None):
    args = parse_args(argv)
    run_id = validate_pwa_performance_qa_run_id(args.run_id)
    if args.phase == 'prepare':
        payload = prepare_open_phase(run_id)
    elif args.phase == 'seal-open':
        payload = seal_existing_open_phase(run_id)
    else:
        payload = add_late_load(run_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
