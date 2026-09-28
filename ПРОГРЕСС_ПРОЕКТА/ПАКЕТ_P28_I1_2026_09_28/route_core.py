"""Pure P28-I1 route-direction projection core.

This module deliberately has no Django, database, network, UI or audio imports.
Future adapters must authenticate authors, validate authority and prove that an
``observed_ancestor_id`` was actually shown by the creating application.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Iterable, Mapping


ROUTE_ROLES = frozenset({'driver', 'excavator_operator'})
TERMINAL_LIFECYCLES = frozenset({'cancelled', 'unloaded', 'superseded'})


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@dataclass(frozen=True)
class RouteEvent:
    event_id: str
    trip_id: str
    event_type: str
    actor_id: str
    actor_role: str
    target_point_id: str
    observed_ancestor_ids: tuple[str, ...] = ()
    action_at: str = ''
    received_at: str = ''
    loading_event_id: str = ''
    fingerprint: str = ''

    def content(self) -> dict[str, object]:
        """Immutable business content; receipt time is audit, not identity."""
        return {
            'event_id': self.event_id,
            'trip_id': self.trip_id,
            'event_type': self.event_type,
            'actor_id': self.actor_id,
            'actor_role': self.actor_role,
            'target_point_id': self.target_point_id,
            'observed_ancestor_ids': list(self.observed_ancestor_ids),
            'action_at': self.action_at,
            'loading_event_id': self.loading_event_id,
        }

    def calculated_fingerprint(self) -> str:
        return sha256(_canonical_json(self.content()).encode('utf-8')).hexdigest()

    def with_calculated_fingerprint(self) -> 'RouteEvent':
        payload = asdict(self)
        payload['fingerprint'] = self.calculated_fingerprint()
        return RouteEvent(**payload)

    def to_record(self) -> dict[str, object]:
        return {
            **self.content(),
            'observed_ancestor_ids': list(self.observed_ancestor_ids),
            'received_at': self.received_at,
            'fingerprint': self.fingerprint,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> 'RouteEvent':
        data = dict(record)
        data['observed_ancestor_ids'] = tuple(data.get('observed_ancestor_ids') or ())
        return cls(**data)


def route_event(**values: object) -> RouteEvent:
    """Convenience factory used by adapters/tests to bind content fingerprint."""
    values.setdefault('event_type', 'route_direction_changed')
    values.setdefault('observed_ancestor_ids', ())
    values.setdefault('action_at', '')
    values.setdefault('received_at', '')
    values.setdefault('loading_event_id', '')
    values.setdefault('fingerprint', '')
    return RouteEvent(**values).with_calculated_fingerprint()


@dataclass(frozen=True)
class TripRouteContext:
    trip_id: str
    loading_event_id: str
    loading_actor_id: str
    loading_excavator_id: str
    original_point_id: str | None
    history_complete: bool = True
    legacy_actual_point_id: str | None = None
    legacy_actual_origin: str = 'unknown'


@dataclass(frozen=True)
class LifecycleEvidence:
    state: str = 'ongoing'
    evidence_ids: tuple[str, ...] = ()
    successor_trip_id: str | None = None
    successor_proven_without_fk: bool = False

    def operational_decision(self) -> tuple[bool, str]:
        if self.state in TERMINAL_LIFECYCLES:
            return False, f'lifecycle_{self.state}'
        if self.state == 'replacement_unknown':
            return False, 'replacement_history_incomplete'
        if self.state == 'technical_uncontrolled':
            return True, 'technical_uncontrolled_is_not_unload'
        if self.state == 'ongoing':
            return True, 'trip_ongoing'
        return False, 'lifecycle_unknown'


@dataclass(frozen=True)
class Collision:
    event_id: str
    original_fingerprint: str
    incoming_fingerprint: str


@dataclass(frozen=True)
class Projection:
    trip_id: str
    status: str
    selected_point_id: str | None
    selected_event_id: str | None
    selection_reason: str
    causal_maxima: tuple[str, ...]
    missing_ancestor_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]
    history: tuple[dict[str, object], ...]
    loading_event_id: str
    loading_actor_id: str
    original_point_id: str | None
    lifecycle_state: str
    lifecycle_evidence_ids: tuple[str, ...]
    successor_trip_id: str | None
    successor_proven_without_fk: bool
    operational_allowed: bool
    operational_reason: str
    notification_key: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class RouteLedger:
    """Append-only identity ledger. The first persisted value of an ID wins."""

    _events: dict[str, RouteEvent] = field(default_factory=dict)
    _collisions: list[Collision] = field(default_factory=list)
    duplicate_count: int = 0

    def append(self, event: RouteEvent) -> str:
        existing = self._events.get(event.event_id)
        if existing is None:
            self._events[event.event_id] = event
            return 'stored'
        # A repeated delivery has a new receipt observation but the same worker
        # action. Receipt time is deliberately excluded from event identity.
        if existing.content() == event.content() and existing.fingerprint == event.fingerprint:
            self.duplicate_count += 1
            return 'duplicate'
        self._collisions.append(Collision(
            event_id=event.event_id,
            original_fingerprint=existing.fingerprint,
            incoming_fingerprint=event.fingerprint,
        ))
        return 'id_conflict'

    def extend(self, events: Iterable[RouteEvent]) -> list[str]:
        return [self.append(event) for event in events]

    def snapshot(self) -> dict[str, object]:
        return {
            'events': [self._events[key].to_record() for key in sorted(self._events)],
            'collisions': [asdict(item) for item in self._collisions],
            'duplicate_count': self.duplicate_count,
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> 'RouteLedger':
        ledger = cls()
        for record in snapshot.get('events', []):
            event = RouteEvent.from_record(record)
            ledger._events[event.event_id] = event
        ledger._collisions = [Collision(**item) for item in snapshot.get('collisions', [])]
        ledger.duplicate_count = int(snapshot.get('duplicate_count', 0))
        return ledger

    def project(self, context: TripRouteContext, lifecycle: LifecycleEvidence | None = None) -> Projection:
        lifecycle = lifecycle or LifecycleEvidence()
        trip_events = {
            event_id: event
            for event_id, event in self._events.items()
            if event.trip_id == context.trip_id
        }
        history = tuple(event.to_record() for event in sorted(trip_events.values(), key=lambda item: item.event_id))
        operational_allowed, operational_reason = lifecycle.operational_decision()
        diagnostics: set[str] = set()
        collision_ids = {
            collision.event_id
            for collision in self._collisions
            if collision.event_id in trip_events
        }
        invalid_fingerprints = {
            event.event_id
            for event in trip_events.values()
            if event.fingerprint != event.calculated_fingerprint()
        }
        invalid_roles = {
            event.event_id
            for event in trip_events.values()
            if event.actor_role not in ROUTE_ROLES
        }
        if collision_ids:
            diagnostics.update(f'id_collision:{item}' for item in sorted(collision_ids))
        if invalid_fingerprints:
            diagnostics.update(f'fingerprint_mismatch:{item}' for item in sorted(invalid_fingerprints))
        if invalid_roles:
            diagnostics.update(f'unsupported_role:{item}' for item in sorted(invalid_roles))
        if diagnostics:
            return self._projection(
                context, history, lifecycle, operational_allowed, operational_reason,
                status='integrity_conflict', reason='event_integrity_not_proven',
                diagnostics=diagnostics,
            )

        parents: dict[str, set[str]] = {event_id: set() for event_id in trip_events}
        missing: set[str] = set()
        for event in trip_events.values():
            for ancestor_id in event.observed_ancestor_ids:
                ancestor = self._events.get(ancestor_id)
                if ancestor is None:
                    missing.add(ancestor_id)
                elif ancestor.trip_id != context.trip_id:
                    diagnostics.add(f'cross_trip_ancestor_ignored:{event.event_id}:{ancestor_id}')
                else:
                    parents[event.event_id].add(ancestor_id)

        if missing:
            return self._projection(
                context, history, lifecycle, operational_allowed, operational_reason,
                status='causality_incomplete', reason='missing_ancestor',
                missing=missing, diagnostics=diagnostics,
            )
        if not context.history_complete and len(trip_events) > 1:
            diagnostics.add('legacy_history_incomplete')
            return self._projection(
                context, history, lifecycle, operational_allowed, operational_reason,
                status='causality_incomplete', reason='legacy_history_incomplete',
                diagnostics=diagnostics,
            )
        if self._has_cycle(parents):
            diagnostics.add('causality_cycle')
            return self._projection(
                context, history, lifecycle, operational_allowed, operational_reason,
                status='causality_cycle', reason='causality_cycle', diagnostics=diagnostics,
            )

        ancestors_of_any = {ancestor for items in parents.values() for ancestor in items}
        maxima = tuple(sorted(set(trip_events) - ancestors_of_any))
        if not maxima:
            return self._projection(
                context, history, lifecycle, operational_allowed, operational_reason,
                status='no_route_event', reason='original_loading_point', diagnostics=diagnostics,
                point=context.original_point_id,
            )
        if len(maxima) == 1:
            selected = trip_events[maxima[0]]
            return self._resolved(
                context, history, lifecycle, operational_allowed, operational_reason,
                selected, maxima, 'single_causal_maximum', diagnostics,
            )

        maximum_events = [trip_events[event_id] for event_id in maxima]
        drivers = [event for event in maximum_events if event.actor_role == 'driver']
        operators = [event for event in maximum_events if event.actor_role == 'excavator_operator']
        if len(maximum_events) == 2 and len(drivers) == 1 and len(operators) == 1:
            return self._resolved(
                context, history, lifecycle, operational_allowed, operational_reason,
                drivers[0], maxima, 'independent_driver_over_operator_p28_v3a', diagnostics,
            )
        diagnostics.add('independent_maxima_without_approved_policy:' + ','.join(maxima))
        return self._projection(
            context, history, lifecycle, operational_allowed, operational_reason,
            status='policy_unresolved', reason='independent_combination_not_approved',
            maxima=maxima, diagnostics=diagnostics,
        )

    @staticmethod
    def _has_cycle(parents: Mapping[str, set[str]]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            for parent in parents[node]:
                if visit(parent):
                    return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in parents)

    def _resolved(
        self, context: TripRouteContext, history: tuple[dict[str, object], ...],
        lifecycle: LifecycleEvidence, operational_allowed: bool, operational_reason: str,
        selected: RouteEvent, maxima: tuple[str, ...], reason: str, diagnostics: set[str],
    ) -> Projection:
        notification_key = None
        if operational_allowed:
            notification_key = f'route:{context.trip_id}:{selected.event_id}:{selected.fingerprint[:16]}'
        return Projection(
            trip_id=context.trip_id,
            status='resolved',
            selected_point_id=selected.target_point_id,
            selected_event_id=selected.event_id,
            selection_reason=reason,
            causal_maxima=maxima,
            missing_ancestor_ids=(),
            diagnostics=tuple(sorted(diagnostics)),
            history=history,
            loading_event_id=context.loading_event_id,
            loading_actor_id=context.loading_actor_id,
            original_point_id=context.original_point_id,
            lifecycle_state=lifecycle.state,
            lifecycle_evidence_ids=lifecycle.evidence_ids,
            successor_trip_id=lifecycle.successor_trip_id,
            successor_proven_without_fk=lifecycle.successor_proven_without_fk,
            operational_allowed=operational_allowed,
            operational_reason=operational_reason,
            notification_key=notification_key,
        )

    @staticmethod
    def _projection(
        context: TripRouteContext, history: tuple[dict[str, object], ...],
        lifecycle: LifecycleEvidence, operational_allowed: bool, operational_reason: str,
        *, status: str, reason: str, point: str | None = None,
        maxima: tuple[str, ...] = (), missing: set[str] | None = None,
        diagnostics: set[str] | None = None,
    ) -> Projection:
        return Projection(
            trip_id=context.trip_id,
            status=status,
            selected_point_id=point,
            selected_event_id=None,
            selection_reason=reason,
            causal_maxima=maxima,
            missing_ancestor_ids=tuple(sorted(missing or ())),
            diagnostics=tuple(sorted(diagnostics or ())),
            history=history,
            loading_event_id=context.loading_event_id,
            loading_actor_id=context.loading_actor_id,
            original_point_id=context.original_point_id,
            lifecycle_state=lifecycle.state,
            lifecycle_evidence_ids=lifecycle.evidence_ids,
            successor_trip_id=lifecycle.successor_trip_id,
            successor_proven_without_fk=lifecycle.successor_proven_without_fk,
            operational_allowed=operational_allowed,
            operational_reason=operational_reason,
            notification_key=None,
        )
