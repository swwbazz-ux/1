from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path

from .disposable_db import validate_disposable_database, verify_database_marker
from .setup_business_fixture import configure_disposable_django


def _utc_now() -> str:
    return datetime.now(datetime_timezone.utc).isoformat()


def build_independent_expected_matrix(fixture, pair_actions):
    """Build fixture expectations without production routing/collapse code.

    Each synthetic pair owns one truck, one excavator and exactly four semantic
    actions. The matrix is an oracle declared by the fixture, not derived from
    ``event_is_relevant`` or ``collapse_duplicate_business_events``.
    """
    contexts = fixture["contexts"]
    trucks = fixture["equipment"]["truck_ids"]
    excavators = fixture["equipment"]["excavator_ids"]
    all_event_ids = [
        action["event_id"]
        for pair in pair_actions
        for action in pair
    ]
    recipients = {}
    for context in contexts:
        equipment_id = context["equipment_id"]
        pair_index = next(
            index
            for index in range(len(pair_actions))
            if equipment_id in {trucks[index], excavators[index]}
        )
        actions = pair_actions[pair_index]
        expected_ids = [action["event_id"] for action in actions]
        voice_operations = {}
        for action in actions:
            operation_key = action.get("voice_operation_keys", {}).get(
                context["role_app_code"]
            )
            if operation_key:
                voice_operations[str(action["event_id"])] = operation_key
        recipients[context["name"]] = {
            "role_app_code": context["role_app_code"],
            "pair_index": pair_index,
            "expected_event_ids": expected_ids,
            "foreign_event_ids": sorted(set(all_event_ids) - set(expected_ids)),
            "voice_operations": voice_operations,
        }
    return all_event_ids, recipients


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Emit relevant SSE business mutations.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--disposable-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-ms", type=int, default=100)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    database, root = validate_disposable_database(
        args.database,
        args.disposable_root,
        require_database_missing=False,
    )
    if not database.is_file():
        raise ValueError("disposable database does not exist")
    for path in (args.fixture, args.output):
        if path.resolve().parent != root:
            raise ValueError("fixture and output must stay inside disposable root")
    configure_disposable_django(database, root)

    import django

    django.setup()
    from django.db import connection, transaction
    from django.utils import timezone

    from assignments.models import AssignmentStatus, HaulAssignment
    from core.models import OperationalStateEvent, OperationalStateVersion, bump_operational_state
    from references.models import DumpPoint, Equipment, RockType
    from shifts.models import EmployeeShift
    from trips.models import Trip, TripStatus

    verify_database_marker(connection)
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    start_cursor = int(fixture["start_cursor"])
    commit_utc_by_event: dict[str, str] = {}

    def run_action(callback):
        before = OperationalStateVersion.objects.get(key="production").version
        commit_observed = {}
        with transaction.atomic():
            transaction.on_commit(
                lambda: commit_observed.setdefault("utc", _utc_now())
            )
            result = callback()
        committed_at = commit_observed["utc"]
        versions = list(
            OperationalStateEvent.objects
            .filter(key="production", version__gt=before)
            .order_by("version")
            .values_list("version", flat=True)
        )
        for version in versions:
            commit_utc_by_event[str(version)] = committed_at
        if args.interval_ms:
            time.sleep(args.interval_ms / 1000)
        return result

    contexts = {item["name"]: item for item in fixture["contexts"]}
    trucks = [Equipment.objects.get(pk=value) for value in fixture["equipment"]["truck_ids"]]
    excavators = [
        Equipment.objects.get(pk=value)
        for value in fixture["equipment"]["excavator_ids"]
    ]
    point = DumpPoint.objects.get(pk=fixture["dump_point_id"])
    rock = RockType.objects.get(pk=fixture["rock_type_id"])
    driver_shifts = [
        EmployeeShift.objects.get(pk=contexts[f"driver-{index}"]["shift_id"])
        for index in (1, 2)
    ]

    def create_assignment(truck, excavator):
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        state = bump_operational_state(
            "sse_business_load_assignment",
            event_type="assignment_changed",
            object_type="HaulAssignment",
            object_id=assignment.id,
            payload={
                "action": "assignment_applied",
                "assignment_id": assignment.id,
                "truck_id": truck.id,
                "excavator_id": excavator.id,
            },
        )
        return assignment, state.version

    assignment_results = [
        run_action(lambda index=index: create_assignment(trucks[index], excavators[index]))
        for index in (0, 1)
    ]
    assignments = [result[0] for result in assignment_results]
    assignment_versions = [result[1] for result in assignment_results]

    def create_loaded_trip(index):
        shift = driver_shifts[index]
        trip = Trip.objects.create(
            truck=trucks[index],
            excavator=excavators[index],
            driver_id=contexts[f"driver-{index + 1}"]["employee_id"],
            driver_participation_recorded=True,
            driver_control_shift=shift,
            rock_type=rock,
            dump_point=point,
            assigned_dump_point=point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now(),
        )
        action = "truck_loaded" if index == 0 else "driver_manual_loaded"
        state = bump_operational_state(
            "sse_business_load_trip",
            event_type="trip_changed",
            object_type="Trip",
            object_id=trip.id,
            payload={
                "action": action,
                "trip_id": trip.id,
                "truck_id": trip.truck_id,
                "excavator_id": trip.excavator_id,
                "driver_control_shift_id": shift.id,
                "driver_participation_recorded": True,
                "dump_point_id": point.id,
                "status": trip.status,
            },
        )
        return trip, state.version, action

    trip_results = [
        run_action(lambda index=index: create_loaded_trip(index))
        for index in (0, 1)
    ]
    trips = [result[0] for result in trip_results]
    trip_versions = [result[1] for result in trip_results]

    def cancel_assignment(index):
        assignment = assignments[index]
        assignment.status = AssignmentStatus.CANCELLED
        assignment.ended_at = timezone.now()
        assignment.save(update_fields=["status", "ended_at"])
        state = bump_operational_state(
            "sse_business_load_assignment_cancel",
            event_type="assignment_changed",
            object_type="HaulAssignment",
            object_id=assignment.id,
            payload={
                "action": "assignment_cancelled",
                "assignment_id": assignment.id,
                "truck_id": assignment.truck_id,
                "excavator_id": assignment.excavator_id,
            },
        )
        return state.version

    cancellation_versions = [
        run_action(lambda index=index: cancel_assignment(index))
        for index in (0, 1)
    ]

    replacement_results = [
        run_action(
            lambda index=index: create_assignment(trucks[index], excavators[index])
        )
        for index in (0, 1)
    ]
    replacements = [result[0] for result in replacement_results]
    replacement_versions = [result[1] for result in replacement_results]

    pair_actions = []
    for index in (0, 1):
        pair_actions.append([
            {
                "name": "assignment_applied",
                "event_id": assignment_versions[index],
                "voice_operation_keys": {
                    "driver": f"assign:{assignments[index].id}",
                    "excavator_operator": f"assign:{assignments[index].id}",
                },
            },
            {
                "name": trip_results[index][2],
                "event_id": trip_versions[index],
                "voice_operation_keys": {
                    "driver": f"dump:{trips[index].id}:id-{point.id}",
                },
            },
            {
                "name": "assignment_cancelled",
                "event_id": cancellation_versions[index],
                "voice_operation_keys": {},
            },
            {
                "name": "assignment_applied_replacement",
                "event_id": replacement_versions[index],
                "voice_operation_keys": {
                    "driver": f"assign:{replacements[index].id}",
                    "excavator_operator": f"assign:{replacements[index].id}",
                },
            },
        ])
    all_business_ids, recipients = build_independent_expected_matrix(
        fixture,
        pair_actions,
    )

    expected_payload = {
        "schema": 1,
        "start_cursor": start_cursor,
        "end_cursor": OperationalStateVersion.objects.get(key="production").version,
        "all_business_event_ids": all_business_ids,
        "commit_utc_by_event": commit_utc_by_event,
        "recipients": recipients,
        "sequence": {
            "assignment_ids": [item.id for item in assignments],
            "trip_ids": [item.id for item in trips],
            "replacement_assignment_ids": [item.id for item in replacements],
            "pair_actions": pair_actions,
        },
    }
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(expected_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(json.dumps({
        "ok": True,
        "start_cursor": start_cursor,
        "end_cursor": expected_payload["end_cursor"],
        "business_event_count": len(all_business_ids),
        "recipient_expected_counts": {
            key: len(value["expected_event_ids"])
            for key, value in recipients.items()
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
