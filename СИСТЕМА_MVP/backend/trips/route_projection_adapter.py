"""Read-only P28-I2 adapter from current Django records to the P28 route core.

The adapter is deliberately not registered in a view, signal, task, middleware,
or ``AppConfig``.  It reports provenance and gaps in legacy history; it does not
authenticate a caller or grant permission to perform a new route action.

The function below performs several ordinary SELECT statements.  Under the
database's default READ COMMITTED isolation those statements are not claimed to
be one historical snapshot when concurrent writes happen.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from django.db.models import Q

from core.models import OfflineFieldEvent, OfflineFieldEventConflict, OfflineFieldEventStatus

from .models import DispatcherActionLog, DispatcherActionType, Trip, TripClientAction, TripStatus
from .route_projection_core import LifecycleEvidence, Projection, RouteLedger, TripRouteContext, route_event


ROUTE_EVENT_TYPE = 'driver.trip.dump_point_changed'
LOAD_EVENT_TYPES = frozenset({
    'driver.trip.loaded',
    'excavator.trip.loaded',
    'excavator.free_bucket.loaded',
})


@dataclass(frozen=True)
class TripRouteEvidence:
    trip_id: int
    trip_snapshot: dict[str, Any]
    sources: tuple[dict[str, Any], ...]
    normalized_route_inputs: tuple[dict[str, Any], ...]
    incomplete_reasons: tuple[str, ...]
    history_complete: bool
    loading_reference: dict[str, Any]
    author_context: dict[str, Any]
    lifecycle_detail: dict[str, Any]
    projection: Projection
    read_consistency: str = 'multiple_selects_database_default_isolation_no_snapshot_claim'

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            'projection': self.projection.to_dict(),
        }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _positive_int(value: Any) -> int | None:
    if value in (None, '', False):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _mapping_trip_ids(value: Mapping[str, Any] | None) -> tuple[int, ...]:
    value = value if isinstance(value, Mapping) else {}
    payload = value.get('payload') if isinstance(value.get('payload'), Mapping) else {}
    context = value.get('context_snapshot') if isinstance(value.get('context_snapshot'), Mapping) else {}
    result = value.get('result_payload') if isinstance(value.get('result_payload'), Mapping) else {}
    server_ids = result.get('server_ids') if isinstance(result.get('server_ids'), Mapping) else {}
    ids = {
        item
        for item in (
            _positive_int(value.get('trip_id')),
            _positive_int(payload.get('trip_id')),
            _positive_int(context.get('trip_id')),
            _positive_int(server_ids.get('trip_id')),
        )
        if item is not None
    }
    return tuple(sorted(ids))


def _receipt_trip_ids(receipt: OfflineFieldEvent) -> tuple[int, ...]:
    ids = set(_mapping_trip_ids({
        'payload': receipt.payload,
        'context_snapshot': receipt.context_snapshot,
        'result_payload': receipt.result_payload,
    }))
    if receipt.trip_id:
        ids.add(receipt.trip_id)
    return tuple(sorted(ids))


def _receipt_disposition(receipt: OfflineFieldEvent) -> str:
    result = receipt.result_payload if isinstance(receipt.result_payload, Mapping) else {}
    if receipt.status != OfflineFieldEventStatus.ACCEPTED:
        return receipt.status
    if result.get('no_effect'):
        return 'accepted_no_effect'
    if result.get('no_change'):
        return 'accepted_no_change'
    return 'accepted_effect_recorded'


def _receipt_source(receipt: OfflineFieldEvent) -> dict[str, Any]:
    return {
        'source_kind': 'offline_field_event',
        'source_pk': receipt.pk,
        'event_id': receipt.event_id,
        'event_type': receipt.event_type,
        'trip_ids': _receipt_trip_ids(receipt),
        'linked_trip_id': receipt.trip_id,
        'actor_id': receipt.actor_id,
        'access_id': receipt.access_id,
        'role_code': receipt.role_code,
        'shift_id': receipt.shift_id,
        'equipment_id': receipt.equipment_id,
        'device_id': receipt.device_id,
        'sequence': receipt.sequence,
        'depends_on': tuple(receipt.depends_on or ()),
        'occurred_at': _iso(receipt.occurred_at),
        'received_at': _iso(receipt.received_at),
        'payload': receipt.payload or {},
        'context_snapshot': receipt.context_snapshot or {},
        'fingerprint': receipt.fingerprint,
        'status': receipt.status,
        'retryable': receipt.retryable,
        'error_code': receipt.error_code,
        'error_message': receipt.error_message,
        'result_payload': receipt.result_payload or {},
        'effect_disposition': _receipt_disposition(receipt),
    }


def _conflict_source(conflict: OfflineFieldEventConflict) -> dict[str, Any]:
    submitted = conflict.submitted_event if isinstance(conflict.submitted_event, Mapping) else {}
    submitted_trip_ids = _mapping_trip_ids(submitted)
    return {
        'source_kind': 'offline_field_event_conflict',
        'source_pk': conflict.pk,
        'attempted_event_id': conflict.attempted_event_id,
        'code': conflict.code,
        'existing_event_pk': conflict.existing_event_id,
        'existing_event_id': conflict.existing_event.event_id if conflict.existing_event_id else None,
        'existing_trip_id': conflict.existing_event.trip_id if conflict.existing_event_id else None,
        'submitted_trip_ids': submitted_trip_ids,
        'actor_id': conflict.actor_id,
        'access_id': conflict.access_id,
        'role_code': conflict.role_code,
        'device_id': conflict.device_id,
        'fingerprint': conflict.fingerprint,
        'submitted_event': dict(submitted),
        'received_at': _iso(conflict.received_at),
    }


def _action_source(action: TripClientAction, *, receipt_ids: set[str]) -> dict[str, Any]:
    return {
        'source_kind': 'trip_client_action',
        'source_pk': action.pk,
        'action_type': action.action_type,
        'client_action_id': action.client_action_id,
        'trip_id': action.trip_id,
        'actor_id': action.actor_id,
        'created_at': _iso(action.created_at),
        'matching_offline_receipt': action.client_action_id in receipt_ids,
        'payload_available': False,
    }


def _dispatcher_source(item: DispatcherActionLog) -> dict[str, Any]:
    return {
        'source_kind': 'dispatcher_action_log',
        'source_pk': item.pk,
        'action_type': item.action_type,
        'trip_id': item.trip_id,
        'actor_id': item.actor_id,
        'shift_id': item.shift_id,
        'target_summary': item.target_summary,
        'reason': item.reason,
        'created_at': _iso(item.created_at),
        'before_after_available': False,
    }


def _trip_snapshot(trip: Trip) -> dict[str, Any]:
    return {
        'source_kind': 'trip_snapshot',
        'source_pk': trip.pk,
        'trip_id': trip.pk,
        'status': trip.status,
        'excavator_id': trip.excavator_id,
        'truck_id': trip.truck_id,
        'excavator_operator_id': trip.excavator_operator_id,
        'driver_id': trip.driver_id,
        'loading_shift_id': trip.loading_shift_id,
        'unloading_shift_id': trip.unloading_shift_id,
        'driver_control_shift_id': trip.driver_control_shift_id,
        'dump_point_id': trip.dump_point_id,
        'assigned_dump_point_id': trip.assigned_dump_point_id,
        'actual_dump_point_id': trip.actual_dump_point_id,
        'created_at': _iso(trip.created_at),
        'loaded_at': _iso(trip.loaded_at),
        'load_received_at': _iso(trip.load_received_at),
        'load_time_source': trip.load_time_source,
        'completed_at': _iso(trip.completed_at),
        'unload_received_at': _iso(trip.unload_received_at),
        'unload_time_source': trip.unload_time_source,
        'cancelled_at': _iso(trip.cancelled_at),
        'operationally_closed_at': _iso(trip.operationally_closed_at),
        'closure_recorded_by_id': trip.closure_recorded_by_id,
        'superseded_by_id': trip.superseded_by_id,
        'is_carryover': trip.is_carryover,
        'legacy_values_are_current_snapshot_not_value_history': True,
    }


def _target_point_id(value: Mapping[str, Any]) -> int | None:
    payload = value.get('payload') if isinstance(value.get('payload'), Mapping) else {}
    result = value.get('result_payload') if isinstance(value.get('result_payload'), Mapping) else {}
    server_ids = result.get('server_ids') if isinstance(result.get('server_ids'), Mapping) else {}
    return _positive_int(payload.get('dump_point_id')) or _positive_int(server_ids.get('dump_point_id'))


def _route_from_receipt(receipt: OfflineFieldEvent, *, loading_event_id: str):
    if receipt.event_type != ROUTE_EVENT_TYPE:
        return None
    target_point_id = _target_point_id({
        'payload': receipt.payload,
        'result_payload': receipt.result_payload,
    })
    trip_ids = _receipt_trip_ids(receipt)
    if target_point_id is None or len(trip_ids) != 1:
        return None
    return route_event(
        event_id=receipt.event_id,
        trip_id=str(trip_ids[0]),
        event_type=receipt.event_type,
        actor_id=str(receipt.actor_id),
        actor_role=receipt.role_code,
        target_point_id=str(target_point_id),
        # Current depends_on records queue order, not a proven UI observation.
        observed_ancestor_ids=(),
        action_at=_iso(receipt.occurred_at) or '',
        received_at=_iso(receipt.received_at) or '',
        loading_event_id=loading_event_id,
    )


def _route_from_submitted(conflict: OfflineFieldEventConflict, *, loading_event_id: str):
    submitted = conflict.submitted_event if isinstance(conflict.submitted_event, Mapping) else {}
    event_type = str(submitted.get('event_type') or '')
    target_point_id = _target_point_id(submitted)
    trip_ids = _mapping_trip_ids(submitted)
    if event_type != ROUTE_EVENT_TYPE or target_point_id is None or len(trip_ids) != 1:
        return None
    actor_id = _positive_int(submitted.get('claimed_actor_id')) or conflict.actor_id
    action_at = submitted.get('occurred_at') or ''
    return route_event(
        event_id=conflict.attempted_event_id,
        trip_id=str(trip_ids[0]),
        event_type=event_type,
        actor_id=str(actor_id),
        actor_role=str(submitted.get('role_code') or conflict.role_code),
        target_point_id=str(target_point_id),
        observed_ancestor_ids=(),
        action_at=str(action_at),
        received_at=_iso(conflict.received_at) or '',
        loading_event_id=loading_event_id,
    )


def _lifecycle(
    trip: Trip,
    *,
    receipts: list[OfflineFieldEvent],
    actions: list[TripClientAction],
    logs: list[DispatcherActionLog],
    possible_successor_ids: tuple[int, ...],
) -> tuple[LifecycleEvidence, dict[str, Any]]:
    unload_receipts = [
        item for item in receipts
        if item.trip_id == trip.pk
        and item.event_type == 'driver.trip.unloaded'
        and item.status == OfflineFieldEventStatus.ACCEPTED
        and not (item.result_payload or {}).get('no_effect')
    ]
    unload_actions = [item for item in actions if item.action_type == 'trip_unloaded']
    service_logs = [item for item in logs if item.action_type == DispatcherActionType.COMPLETE_TRIP]
    evidence_ids = tuple(sorted({
        *(f'offline:{item.event_id}' for item in unload_receipts),
        *(f'client_action:{item.client_action_id}' for item in unload_actions),
        *(f'dispatcher_log:{item.pk}' for item in service_logs),
    }))
    detail: dict[str, Any] = {
        'trip_status': trip.status,
        'driver_unload_receipt_ids': tuple(item.event_id for item in unload_receipts),
        'driver_unload_action_ids': tuple(item.client_action_id for item in unload_actions),
        'service_completion_log_ids': tuple(item.pk for item in service_logs),
        'possible_successor_trip_ids': possible_successor_ids,
        'physical_unload_not_inferred_from_status_alone': True,
    }
    if trip.status == TripStatus.CANCELLED:
        detail['basis'] = 'trip_cancelled_state'
        return LifecycleEvidence(state='cancelled', evidence_ids=(f'trip:{trip.pk}:cancelled',)), detail
    if trip.status == TripStatus.COMPLETED:
        if unload_receipts or unload_actions:
            detail['basis'] = 'driver_unload_confirmation_recorded'
            return LifecycleEvidence(state='unloaded', evidence_ids=evidence_ids), detail
        if service_logs:
            detail['basis'] = 'service_completion_without_driver_unload_confirmation'
            return LifecycleEvidence(state='completed_service', evidence_ids=evidence_ids), detail
        detail['basis'] = 'completed_without_recorded_origin'
        return LifecycleEvidence(
            state='completed_origin_unknown', evidence_ids=(f'trip:{trip.pk}:completed',),
        ), detail
    if trip.status == TripStatus.UNCONTROLLED:
        if trip.superseded_by_id:
            detail['basis'] = 'positive_superseded_by_foreign_key'
            return LifecycleEvidence(
                state='superseded',
                evidence_ids=(f'trip:{trip.pk}:superseded_by',),
                successor_trip_id=str(trip.superseded_by_id),
            ), detail
        if possible_successor_ids:
            detail['basis'] = 'possible_later_trip_without_causal_link'
            return LifecycleEvidence(
                state='replacement_unknown',
                evidence_ids=(f'trip:{trip.pk}:operational_close',),
            ), detail
        detail['basis'] = 'technical_uncontrolled_without_successor_evidence'
        return LifecycleEvidence(
            state='technical_uncontrolled',
            evidence_ids=(f'trip:{trip.pk}:operational_close',),
        ), detail
    detail['basis'] = 'trip_ongoing'
    return LifecycleEvidence(state='ongoing'), detail


def read_trip_route_evidence(trip_id: int) -> TripRouteEvidence:
    """Collect current route evidence for one trip without writing anything."""
    trip = (
        Trip.objects
        .select_related(
            'loading_shift__employee', 'unloading_shift__employee',
            'driver_control_shift__employee', 'superseded_by',
        )
        .get(pk=trip_id)
    )
    trip_id_text = str(trip.pk)
    receipt_filter = (
        Q(trip_id=trip.pk)
        | Q(payload__trip_id=trip.pk)
        | Q(payload__trip_id=trip_id_text)
        | Q(result_payload__server_ids__trip_id=trip.pk)
        | Q(result_payload__server_ids__trip_id=trip_id_text)
        | Q(context_snapshot__trip_id=trip.pk)
        | Q(context_snapshot__trip_id=trip_id_text)
    )
    receipts = list(
        OfflineFieldEvent.objects
        .filter(receipt_filter)
        .select_related('actor', 'access', 'shift', 'equipment', 'trip')
        .distinct()
        .order_by('event_id', 'pk')
    )
    conflict_filter = (
        Q(existing_event__trip_id=trip.pk)
        | Q(submitted_event__trip_id=trip.pk)
        | Q(submitted_event__trip_id=trip_id_text)
        | Q(submitted_event__payload__trip_id=trip.pk)
        | Q(submitted_event__payload__trip_id=trip_id_text)
        | Q(submitted_event__context_snapshot__trip_id=trip.pk)
        | Q(submitted_event__context_snapshot__trip_id=trip_id_text)
    )
    conflicts = list(
        OfflineFieldEventConflict.objects
        .filter(conflict_filter)
        .select_related('existing_event', 'actor', 'access')
        .distinct()
        .order_by('attempted_event_id', 'pk')
    )
    actions = list(
        TripClientAction.objects
        .filter(trip_id=trip.pk)
        .select_related('actor')
        .order_by('action_type', 'client_action_id', 'pk')
    )
    logs = list(
        DispatcherActionLog.objects
        .filter(trip_id=trip.pk)
        .select_related('actor', 'shift')
        .order_by('action_type', 'created_at', 'pk')
    )

    possible_successor_ids: tuple[int, ...] = ()
    if trip.status == TripStatus.UNCONTROLLED and not trip.superseded_by_id:
        loaded_after = trip.loaded_at or trip.created_at
        possible_successor_ids = tuple(
            Trip.objects
            .filter(truck_id=trip.truck_id, loaded_at__gt=loaded_after)
            .exclude(pk=trip.pk)
            .order_by('loaded_at', 'pk')
            .values_list('pk', flat=True)
        )

    accepted_load_receipts = [
        item for item in receipts
        if item.trip_id == trip.pk
        and item.event_type in LOAD_EVENT_TYPES
        and item.status == OfflineFieldEventStatus.ACCEPTED
        and not (item.result_payload or {}).get('no_effect')
    ]
    if len(accepted_load_receipts) == 1:
        loading_receipt = accepted_load_receipts[0]
        loading_event_id = loading_receipt.event_id
        loading_actor_id = str(loading_receipt.actor_id)
        loading_reference = {
            'value': loading_event_id,
            'source_kind': 'offline_field_event',
            'source_pk': loading_receipt.pk,
            'synthetic': False,
        }
    else:
        loading_receipt = None
        loading_event_id = f'source:trip:{trip.pk}:loading_snapshot'
        loading_actor_id = str(trip.excavator_operator_id or trip.driver_id or '')
        loading_reference = {
            'value': loading_event_id,
            'source_kind': 'trip_snapshot',
            'source_pk': trip.pk,
            'synthetic': True,
        }

    receipt_ids = {item.event_id for item in receipts}
    sources: list[dict[str, Any]] = [_trip_snapshot(trip)]
    sources.extend(_receipt_source(item) for item in receipts)
    sources.extend(_conflict_source(item) for item in conflicts)
    sources.extend(_action_source(item, receipt_ids=receipt_ids) for item in actions)
    sources.extend(_dispatcher_source(item) for item in logs)

    matching_actions = {
        item.client_action_id
        for item in actions
        if item.action_type == 'change_actual_unload_point'
    }
    ledger = RouteLedger()
    normalized_inputs: list[dict[str, Any]] = []
    appended_receipts: set[int] = set()
    applied_receipt_pks: set[int] = set()
    for receipt in receipts:
        route = _route_from_receipt(receipt, loading_event_id=loading_event_id)
        applied = bool(
            route is not None
            and receipt.trip_id is not None
            and receipt.status == OfflineFieldEventStatus.ACCEPTED
            and _receipt_disposition(receipt) == 'accepted_effect_recorded'
            and receipt.event_id in matching_actions
        )
        if applied:
            ledger.append(route)
            appended_receipts.add(receipt.pk)
            applied_receipt_pks.add(receipt.pk)
            normalized_inputs.append({
                'event': route.to_record(),
                'source_kind': 'offline_field_event',
                'source_pk': receipt.pk,
                'applied_effect_proven_by_client_action': True,
                'depends_on_preserved_as_queue_metadata': tuple(receipt.depends_on or ()),
                'observed_ancestors_proven': False,
                'raw_fingerprint': receipt.fingerprint,
            })

    for conflict in conflicts:
        original_receipt = conflict.existing_event
        original = (
            _route_from_receipt(original_receipt, loading_event_id=loading_event_id)
            if original_receipt is not None else None
        )
        incoming = _route_from_submitted(conflict, loading_event_id=loading_event_id)
        if original is None or incoming is None:
            continue
        if original_receipt.pk not in appended_receipts:
            ledger.append(original)
            appended_receipts.add(original_receipt.pk)
        ledger.append(incoming)
        normalized_inputs.append({
            'event': incoming.to_record(),
            'source_kind': 'offline_field_event_conflict',
            'source_pk': conflict.pk,
            'applied_effect_proven_by_client_action': False,
            'collision_with_event_id': original.event_id,
            'observed_ancestors_proven': False,
            'raw_fingerprint': conflict.fingerprint,
        })

    route_receipts_with_effect = {
        item.event_id for item in receipts
        if item.pk in applied_receipt_pks and item.trip_id == trip.pk
    }
    direct_route_actions = [
        item for item in actions
        if item.action_type == 'change_actual_unload_point'
        and item.client_action_id not in route_receipts_with_effect
    ]
    correction_logs = [
        item for item in logs if item.action_type == 'report_source_correction'
    ]

    if trip.actual_dump_point_id is None:
        legacy_actual_origin = 'unset'
    elif route_receipts_with_effect:
        legacy_actual_origin = 'offline_driver_change_with_receipt_and_client_action'
    elif direct_route_actions:
        legacy_actual_origin = 'legacy_direct_action_without_value_payload'
    elif correction_logs:
        legacy_actual_origin = 'administrative_correction_log_without_before_after'
    else:
        legacy_actual_origin = 'trip_snapshot_origin_unknown'

    incomplete_reasons = {
        'dedicated_route_event_journal_absent',
        'ui_observation_not_recorded',
    }
    if not accepted_load_receipts:
        incomplete_reasons.add('loading_event_not_recorded')
    elif len(accepted_load_receipts) > 1:
        incomplete_reasons.add('loading_event_ambiguous')
    if legacy_actual_origin in {
        'legacy_direct_action_without_value_payload',
        'trip_snapshot_origin_unknown',
    }:
        incomplete_reasons.add('actual_point_origin_incomplete')
    if direct_route_actions:
        incomplete_reasons.add('legacy_route_action_payload_missing')
    if correction_logs:
        incomplete_reasons.add('administrative_correction_before_after_missing')
    if trip.status == TripStatus.UNCONTROLLED and not trip.superseded_by_id and possible_successor_ids:
        incomplete_reasons.add('replacement_link_missing')

    lifecycle, lifecycle_detail = _lifecycle(
        trip,
        receipts=receipts,
        actions=actions,
        logs=logs,
        possible_successor_ids=possible_successor_ids,
    )
    if lifecycle_detail['basis'] in {
        'service_completion_without_driver_unload_confirmation',
        'completed_without_recorded_origin',
    }:
        incomplete_reasons.add('completed_without_driver_unload_confirmation')

    original_point_id = trip.assigned_dump_point_id or trip.dump_point_id
    context = TripRouteContext(
        trip_id=trip_id_text,
        loading_event_id=loading_event_id,
        loading_actor_id=loading_actor_id,
        loading_excavator_id=str(trip.excavator_id),
        original_point_id=str(original_point_id) if original_point_id else None,
        # The current schema has no complete append-only route journal and no
        # proof of what the phone displayed when legacy commands were created.
        history_complete=False,
        legacy_actual_point_id=(
            str(trip.actual_dump_point_id) if trip.actual_dump_point_id else None
        ),
        legacy_actual_origin=legacy_actual_origin,
    )
    projection = ledger.project(context, lifecycle)
    author_context = {
        'loading_actor_id': loading_actor_id or None,
        'loading_actor_source': loading_reference,
        'trip_excavator_operator_id': trip.excavator_operator_id,
        'trip_driver_id': trip.driver_id,
        'loading_shift_id': trip.loading_shift_id,
        'loading_shift_employee_id': (
            trip.loading_shift.employee_id if trip.loading_shift_id else None
        ),
        'driver_control_shift_id': trip.driver_control_shift_id,
        'driver_control_employee_id': (
            trip.driver_control_shift.employee_id if trip.driver_control_shift_id else None
        ),
        'unloading_shift_id': trip.unloading_shift_id,
        'unloading_shift_employee_id': (
            trip.unloading_shift.employee_id if trip.unloading_shift_id else None
        ),
        'route_action_actor_ids': tuple(sorted({
            item.actor_id for item in receipts if item.event_type == ROUTE_EVENT_TYPE
        })),
        'administrative_correction_actor_ids': tuple(sorted({
            item.actor_id for item in correction_logs
        })),
        'current_access_not_used_as_historical_authority': True,
    }
    sources.sort(key=lambda item: (
        item['source_kind'], str(item.get('event_id') or item.get('client_action_id') or ''),
        int(item.get('source_pk') or 0),
    ))
    normalized_inputs.sort(key=lambda item: (
        str(item['event']['event_id']), item['source_kind'], int(item['source_pk']),
    ))
    return TripRouteEvidence(
        trip_id=trip.pk,
        trip_snapshot=_trip_snapshot(trip),
        sources=tuple(sources),
        normalized_route_inputs=tuple(normalized_inputs),
        incomplete_reasons=tuple(sorted(incomplete_reasons)),
        history_complete=False,
        loading_reference=loading_reference,
        author_context=author_context,
        lifecycle_detail=lifecycle_detail,
        projection=projection,
    )
