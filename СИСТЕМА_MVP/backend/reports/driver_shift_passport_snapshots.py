import copy
import hashlib
import json
import logging
from datetime import timedelta
from uuid import uuid4

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from assignments.models import HaulAssignment
from downtimes.models import DowntimeEvent
from shifts.models import EmployeeShift, ShiftReadingCorrection
from trips.models import Trip, TripStatus

from .driver_shift_timeline import (
    _driver_trip_records_for_shift,
    build_driver_shift_timeline,
    cycle_aggregation_inputs_from_samples,
)
from .models import (
    DriverShiftPassportCaptureRequest,
    DriverShiftPassportRequestStatus,
    DriverShiftPassportSnapshot,
    DriverShiftPassportTrigger,
)


logger = logging.getLogger(__name__)

DRIVER_SHIFT_PASSPORT_SCHEMA_VERSION = 1
DRIVER_SHIFT_PASSPORT_CALCULATOR_VERSION = 'driver-shift-passport-v1'
CAPTURE_LATE_AFTER = timedelta(minutes=5)
FORBIDDEN_DIAGNOSTIC_KEYS = {'score', 'place', 'weight'}
CLOSURE_TRIGGERS = {
    DriverShiftPassportTrigger.DRIVER_CLOSE,
    DriverShiftPassportTrigger.SERVICE_CLOSE,
    DriverShiftPassportTrigger.ROLE_SWITCH,
}


def _json_native(value):
    return json.loads(
        json.dumps(
            value,
            cls=DjangoJSONEncoder,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
    )


def _canonical_json_bytes(value):
    return json.dumps(
        _json_native(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _fingerprint(value):
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _assert_diagnostic_payload(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in FORBIDDEN_DIAGNOSTIC_KEYS:
                raise ValueError(
                    f'Диагностический паспорт не может содержать ключ {key!r}.'
                )
            if normalized_key == 'official' and item is not False:
                raise ValueError(
                    'Диагностический паспорт обязан иметь official=false.'
                )
            if normalized_key.startswith('official_') and item is not False:
                raise ValueError(
                    f'Диагностический паспорт обязан иметь {key}=false.'
                )
            _assert_diagnostic_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_diagnostic_payload(item)


def _is_driver_shift(shift):
    if shift.workplace_code:
        return shift.workplace_code == 'driver'
    equipment_type_name = (
        getattr(getattr(shift.equipment, 'equipment_type', None), 'name', '')
        or ''
    ).lower()
    return 'самосвал' in equipment_type_name


def _request_key(
    shift,
    *,
    trigger,
    calculator_version,
    deduplication_token=None,
):
    closed_at = shift.closed_at
    if closed_at is None:
        raise ValueError('Паспорт можно запросить только для закрытой смены.')
    token = deduplication_token or closed_at.isoformat()
    canonical = (
        f'driver-shift-passport:{shift.pk}:{trigger}:'
        f'{calculator_version}:{token}'
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _equipment_context(equipment):
    if equipment is None:
        return None
    model = equipment.model
    return {
        'id': equipment.pk,
        'garage_number': equipment.garage_number,
        'equipment_type_id': equipment.equipment_type_id,
        'equipment_type_name': equipment.equipment_type.name,
        'model_id': equipment.model_id,
        'model_name': model.name if model else None,
        'model_payload_tons': model.payload_tons if model else None,
        'model_body_volume_m3': (
            model.body_volume_m3 if model else None
        ),
    }


def _dump_point_context(dump_point):
    if dump_point is None:
        return None
    return {
        'id': dump_point.pk,
        'name': dump_point.name,
    }


def _rock_context(rock_type):
    return {
        'id': rock_type.pk,
        'name': rock_type.name,
        'density': rock_type.density,
        'loosening_factor': rock_type.loosening_factor,
    }


def _linked_shift_context(shift):
    if shift is None:
        return None
    return {
        'id': shift.pk,
        'employee_id': shift.employee_id,
        'equipment_id': shift.equipment_id,
        'workplace_code': shift.workplace_code,
        'opened_at': shift.opened_at,
        'closed_at': shift.closed_at,
    }


def _trip_manifest_item(trip):
    return {
        'key': f'trip:{trip.pk}',
        'id': trip.pk,
        'excavator_id': trip.excavator_id,
        'excavator': _equipment_context(trip.excavator),
        'truck_id': trip.truck_id,
        'truck': _equipment_context(trip.truck),
        'truck_model_id': trip.truck.model_id,
        'excavator_operator_id': trip.excavator_operator_id,
        'driver_id': trip.driver_id,
        'loading_shift_id': trip.loading_shift_id,
        'loading_shift': _linked_shift_context(trip.loading_shift),
        'unloading_shift_id': trip.unloading_shift_id,
        'unloading_shift': _linked_shift_context(trip.unloading_shift),
        'rock_type_id': trip.rock_type_id,
        'rock_type': _rock_context(trip.rock_type),
        'dump_point_id': trip.dump_point_id,
        'dump_point': _dump_point_context(trip.dump_point),
        'assigned_dump_point_id': trip.assigned_dump_point_id,
        'assigned_dump_point': _dump_point_context(
            trip.assigned_dump_point
        ),
        'actual_dump_point_id': trip.actual_dump_point_id,
        'actual_dump_point': _dump_point_context(trip.actual_dump_point),
        'planned_volume_m3': trip.planned_volume_m3,
        'volume_m3': trip.volume_m3,
        'tonnage': trip.tonnage,
        'loading_horizon': trip.loading_horizon,
        'loading_block': trip.loading_block,
        'transport_distance_km': trip.transport_distance_km,
        'status': trip.status,
        'created_at': trip.created_at,
        'completed_at': trip.completed_at,
        'cancelled_at': trip.cancelled_at,
        'is_carryover': trip.is_carryover,
    }


def _downtime_manifest_item(event):
    reason = event.reason
    equipment_state = reason.equipment_state
    return {
        'key': f'downtime:{event.pk}',
        'id': event.pk,
        'equipment_id': event.equipment_id,
        'employee_id': event.employee_id,
        'reason': {
            'id': reason.pk,
            'name': reason.name,
            'equipment_state_id': reason.equipment_state_id,
            'equipment_state_code': (
                equipment_state.code if equipment_state else None
            ),
            'equipment_state_name': (
                equipment_state.name if equipment_state else None
            ),
            'effective_equipment_state_code': (
                reason.effective_equipment_state_code
            ),
            'is_critical': reason.is_critical,
        },
        'started_at': event.started_at,
        'ended_at': event.ended_at,
    }


def _assignment_manifest_item(assignment):
    return {
        'key': f'assignment:{assignment.pk}',
        'id': assignment.pk,
        'excavator_id': assignment.excavator_id,
        'excavator': _equipment_context(assignment.excavator),
        'truck_id': assignment.truck_id,
        'truck': _equipment_context(assignment.truck),
        'action': assignment.action,
        'status': assignment.status,
        'assigned_at': assignment.assigned_at,
        'effective_at': assignment.effective_at,
        'accepted_at': assignment.accepted_at,
        'ended_at': assignment.ended_at,
    }


def _correction_manifest_item(correction):
    return {
        'key': f'correction:{correction.pk}',
        'id': correction.pk,
        'equipment_id': correction.equipment_id,
        'new_shift_id': correction.new_shift_id,
        'previous_shift_id': correction.previous_shift_id,
        'metric': correction.metric,
        'transferred_value': correction.transferred_value,
        'actual_value': correction.actual_value,
        'employee_id': correction.employee_id,
        'corrected_at': correction.corrected_at,
    }


def _overlap_shift_manifest(shift):
    candidates = (
        EmployeeShift.objects
        .exclude(pk=shift.pk)
        .filter(opened_at__lt=shift.closed_at)
        .filter(
            Q(closed_at__isnull=True)
            | Q(closed_at__gt=shift.opened_at)
        )
        .filter(
            Q(equipment_id=shift.equipment_id)
            | Q(employee_id=shift.employee_id)
        )
        .only(
            'id',
            'employee_id',
            'equipment_id',
            'opened_at',
            'closed_at',
        )
        .order_by('opened_at', 'id')
    )
    return [
        {
            'key': f'overlap-shift:{candidate.pk}',
            'id': candidate.pk,
            'employee_id': candidate.employee_id,
            'equipment_id': candidate.equipment_id,
            'opened_at': candidate.opened_at,
            'closed_at': candidate.closed_at,
        }
        for candidate in candidates
    ]


def _trip_records_for_manifest(shift):
    window_start = shift.opened_at
    window_end = shift.closed_at
    candidates = (
        Trip.objects
        .filter(
            Q(truck_id=shift.equipment_id)
            | Q(unloading_shift_id=shift.pk)
        )
        .filter(
            Q(
                created_at__gte=window_start,
                created_at__lt=window_end,
            )
            | Q(
                completed_at__gte=window_start,
                completed_at__lte=window_end,
            )
            | Q(
                cancelled_at__gte=window_start,
                cancelled_at__lte=window_end,
            )
            | Q(unloading_shift_id=shift.pk)
            | Q(
                created_at__lt=window_end,
                completed_at__isnull=True,
                status__in={
                    TripStatus.ACTIVE,
                    TripStatus.LOADED_WAITING_UNLOAD,
                },
            )
            | Q(
                created_at__lt=window_end,
                completed_at__gt=window_start,
            )
            | Q(
                created_at__lt=window_end,
                cancelled_at__gt=window_start,
            )
            | (
                Q(completed_at__isnull=False)
                & Q(completed_at__lte=F('created_at'))
                & Q(completed_at__lt=window_end)
                & Q(created_at__gt=window_start)
            )
        )
        .select_related('truck')
        .select_related(
            'truck__equipment_type',
            'truck__model',
            'excavator__equipment_type',
            'excavator__model',
            'rock_type',
            'dump_point',
            'assigned_dump_point',
            'actual_dump_point',
            'loading_shift',
            'unloading_shift',
        )
        .order_by('created_at', 'id')
    )
    return _driver_trip_records_for_shift(
        tuple(candidates),
        shift,
        window_start,
        window_end,
    )


def build_driver_shift_source_manifest(shift, timeline):
    trip_records = _trip_records_for_manifest(shift)
    downtime_ids = tuple(
        timeline.source_ids.get('downtime_event_count', ())
    )
    assignment_ids = tuple(
        timeline.source_ids.get('assignment_count', ())
    )
    downtimes = (
        DowntimeEvent.objects
        .filter(pk__in=downtime_ids)
        .select_related('reason', 'reason__equipment_state')
        .order_by('started_at', 'id')
    )
    assignments = (
        HaulAssignment.objects
        .filter(pk__in=assignment_ids)
        .select_related(
            'truck__equipment_type',
            'truck__model',
            'excavator__equipment_type',
            'excavator__model',
        )
        .order_by('accepted_at', 'id')
    )
    corrections = (
        ShiftReadingCorrection.objects
        .filter(new_shift_id=shift.pk)
        .order_by('metric', 'id')
    )
    watch_period = shift.watch_period
    watch_composition = (
        watch_period.watch_composition
        if watch_period and watch_period.watch_composition_id
        else None
    )
    manifest = {
        'manifest_schema_version': 1,
        'shift': {
            'key': f'shift:{shift.pk}',
            'id': shift.pk,
            'employee_id': shift.employee_id,
            'equipment_id': shift.equipment_id,
            'equipment': _equipment_context(shift.equipment),
            'workplace_code': shift.workplace_code,
            'shift_type': shift.shift_type,
            'watch_period': (
                {
                    'id': watch_period.pk,
                    'name': watch_period.name,
                    'starts_on': watch_period.starts_on,
                    'ends_on': watch_period.ends_on,
                    'watch_composition': (
                        {
                            'id': watch_composition.pk,
                            'code': watch_composition.code,
                            'name': watch_composition.name,
                        }
                        if watch_composition
                        else None
                    ),
                }
                if watch_period
                else None
            ),
            'opened_at': shift.opened_at,
            'closed_at': shift.closed_at,
            'opened_by_id': shift.opened_by_id,
            'closed_by_id': shift.closed_by_id,
            'is_service_closed': shift.is_service_closed,
            'start_fuel': shift.start_fuel,
            'start_mileage': shift.start_mileage,
            'start_engine_hours': shift.start_engine_hours,
            'end_fuel': shift.end_fuel,
            'end_mileage': shift.end_mileage,
            'end_engine_hours': shift.end_engine_hours,
        },
        'overlap_shifts': _overlap_shift_manifest(shift),
        'trips': [_trip_manifest_item(trip) for trip in trip_records],
        'downtimes': [
            _downtime_manifest_item(event)
            for event in downtimes
        ],
        'assignments': [
            _assignment_manifest_item(assignment)
            for assignment in assignments
        ],
        'reading_corrections': [
            _correction_manifest_item(correction)
            for correction in corrections
        ],
    }
    return _json_native(manifest)


def _timeline_payload(
    shift,
    timeline,
    source_manifest,
    *,
    calculator_version,
):
    passport = copy.deepcopy(timeline.passport or {})
    passport.pop('source_fingerprint', None)
    passport.setdefault('quality', {})['official_rating_eligible'] = False
    cycle_samples = cycle_aggregation_inputs_from_samples(
        timeline.cycle_samples or {}
    )
    passport.setdefault('aggregation_inputs', {})[
        'cycle_samples'
    ] = cycle_samples
    payload = {
        'schema_version': DRIVER_SHIFT_PASSPORT_SCHEMA_VERSION,
        'calculator_version': calculator_version,
        'official': False,
        'shift_id': shift.pk,
        'source_manifest': source_manifest,
        'passport': passport,
        'aggregation_inputs': {
            'cycle_samples': cycle_samples,
        },
        'timeline': {
            'start': timeline.start,
            'end': timeline.end,
            'intervals': [
                {
                    'start': interval.start,
                    'end': interval.end,
                    'category': interval.category,
                    'source_ids': list(interval.source_ids),
                }
                for interval in timeline.intervals
            ],
        },
    }
    payload = _json_native(payload)
    _assert_diagnostic_payload(payload)
    return payload


def _capture_driver_shift_passport(locked_request):
    shift = (
        EmployeeShift.objects
        .select_for_update(of=('self',))
        .select_related(
            'equipment__equipment_type',
            'equipment__model',
            'watch_period__watch_composition',
        )
        .get(pk=locked_request.shift_id)
    )
    if shift.closed_at is None:
        raise ValueError('Смена снова открыта; snapshot не сформирован.')
    if shift.closed_at != locked_request.closed_at:
        raise ValueError(
            'Момент закрытия смены изменился после постановки snapshot в очередь.'
        )
    if not _is_driver_shift(shift):
        raise ValueError('Snapshot разрешён только для смены водителя.')

    timeline = build_driver_shift_timeline(
        shift,
        as_of=shift.closed_at,
    )
    source_manifest = build_driver_shift_source_manifest(shift, timeline)
    source_fingerprint = _fingerprint(source_manifest)
    payload = _timeline_payload(
        shift,
        timeline,
        source_manifest,
        calculator_version=locked_request.calculator_version,
    )
    payload_fingerprint = _fingerprint(payload)

    existing = (
        DriverShiftPassportSnapshot.objects
        .filter(
            shift=shift,
            calculator_version=locked_request.calculator_version,
            source_fingerprint=source_fingerprint,
        )
        .first()
    )
    if existing:
        if existing.payload_fingerprint != payload_fingerprint:
            raise ValueError(
                'Калькулятор изменил payload без смены calculator_version.'
            )
        return existing

    latest_revision = (
        DriverShiftPassportSnapshot.objects
        .filter(shift=shift)
        .aggregate(value=Max('revision'))['value']
        or 0
    )
    now = timezone.now()
    captured_late = bool(
        locked_request.attempt_count > 1
        or locked_request.trigger not in CLOSURE_TRIGGERS
        or now - locked_request.requested_at > CAPTURE_LATE_AFTER
    )
    return DriverShiftPassportSnapshot.objects.create(
        shift=shift,
        revision=latest_revision + 1,
        schema_version=DRIVER_SHIFT_PASSPORT_SCHEMA_VERSION,
        calculator_version=locked_request.calculator_version,
        source_fingerprint=source_fingerprint,
        payload_fingerprint=payload_fingerprint,
        payload=payload,
        trigger=locked_request.trigger,
        captured_late=captured_late,
        captured_by=locked_request.captured_by,
    )


def _mark_capture_failed(request_id, error):
    now = timezone.now()
    DriverShiftPassportCaptureRequest.objects.filter(pk=request_id).update(
        status=DriverShiftPassportRequestStatus.FAILED,
        attempt_count=F('attempt_count') + 1,
        started_at=now,
        completed_at=None,
        last_error=str(error)[:4000],
    )


def process_driver_shift_passport_request(request_id):
    outermost_transaction = not connection.in_atomic_block
    try:
        with transaction.atomic():
            if outermost_transaction and connection.vendor == 'postgresql':
                # Timeline and raw manifest must observe one database snapshot.
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'
                    )
            capture_request = (
                DriverShiftPassportCaptureRequest.objects
                .select_for_update(of=('self',))
                .select_related('captured_by')
                .get(pk=request_id)
            )
            if (
                capture_request.status
                == DriverShiftPassportRequestStatus.COMPLETED
            ):
                return capture_request.snapshot
            capture_request.status = (
                DriverShiftPassportRequestStatus.PROCESSING
            )
            capture_request.attempt_count += 1
            capture_request.started_at = timezone.now()
            capture_request.completed_at = None
            capture_request.last_error = ''
            capture_request.save(
                update_fields=[
                    'status',
                    'attempt_count',
                    'started_at',
                    'completed_at',
                    'last_error',
                ]
            )
            snapshot = _capture_driver_shift_passport(capture_request)
            capture_request.status = (
                DriverShiftPassportRequestStatus.COMPLETED
            )
            capture_request.snapshot = snapshot
            capture_request.completed_at = timezone.now()
            capture_request.save(
                update_fields=[
                    'status',
                    'snapshot',
                    'completed_at',
                ]
            )
            return snapshot
    except Exception as error:
        _mark_capture_failed(request_id, error)
        raise


def safe_process_driver_shift_passport_request(request_id):
    try:
        return process_driver_shift_passport_request(request_id)
    except Exception:
        logger.exception(
            'Не удалось сформировать диагностический паспорт смены; '
            'закрытие смены сохранено, запрос оставлен для retry.',
            extra={'driver_shift_passport_request_id': request_id},
        )
        return None


def enqueue_driver_shift_passport_capture(
    *,
    shift,
    trigger,
    captured_by=None,
    calculator_version=DRIVER_SHIFT_PASSPORT_CALCULATOR_VERSION,
    deduplication_token=None,
    schedule_on_commit=True,
):
    if not connection.in_atomic_block:
        raise RuntimeError(
            'Outbox-запрос паспорта должен создаваться в транзакции закрытия.'
        )
    if shift.closed_at is None:
        raise ValueError('Нельзя поставить в очередь паспорт открытой смены.')
    if not _is_driver_shift(shift):
        return None

    request_key = _request_key(
        shift,
        trigger=trigger,
        calculator_version=calculator_version,
        deduplication_token=deduplication_token,
    )
    capture_request, created = (
        DriverShiftPassportCaptureRequest.objects.get_or_create(
            request_key=request_key,
            defaults={
                'shift': shift,
                'trigger': trigger,
                'calculator_version': calculator_version,
                'closed_at': shift.closed_at,
                'captured_by': captured_by,
            },
        )
    )
    if (
        schedule_on_commit
        and (
            created
            or capture_request.status
            == DriverShiftPassportRequestStatus.FAILED
        )
    ):
        transaction.on_commit(
            lambda request_id=capture_request.pk: (
                safe_process_driver_shift_passport_request(request_id)
            ),
            robust=True,
        )
    return capture_request


def enqueue_driver_shift_passport_rebuild(
    *,
    shift,
    captured_by=None,
    trigger=DriverShiftPassportTrigger.SOURCE_RECONCILE,
    schedule_on_commit=False,
):
    return enqueue_driver_shift_passport_capture(
        shift=shift,
        trigger=trigger,
        captured_by=captured_by,
        deduplication_token=uuid4().hex,
        schedule_on_commit=schedule_on_commit,
    )
