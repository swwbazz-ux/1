"""Verify one accepted Driver unload event and its single completed trip."""

import json
import os

from core.models import OfflineFieldEvent
from trips.models import Trip, TripClientAction


event_id = os.environ["QA1_EVENT_ID"]
receipts = OfflineFieldEvent.objects.filter(event_id=event_id).order_by("id")
actions = TripClientAction.objects.filter(
    action_type="trip_unloaded",
    client_action_id=event_id,
).order_by("id")

assert receipts.count() == 1, "Expected exactly one Driver unload receipt."
assert actions.count() == 1, "Expected exactly one Driver unload action."

receipt = receipts.get()
action = actions.get()
trip = Trip.objects.get(pk=action.trip_id)

assert receipt.event_type == "driver.trip.unloaded"
assert receipt.status == "accepted"
assert not receipt.retryable
assert receipt.trip_id == trip.id
assert action.trip_id == trip.id
assert action.actor_id == receipt.actor_id
assert trip.status == "completed"
assert trip.truck_id == receipt.equipment_id
assert trip.driver_id == receipt.actor_id
assert trip.unloading_shift_id == receipt.shift_id

payload = {
    "event_id": event_id,
    "receipt_count": 1,
    "receipt_id": receipt.id,
    "status": receipt.status,
    "role_code": receipt.role_code,
    "raw_occurred_at": receipt.occurred_at.isoformat(),
    "received_at": receipt.received_at.isoformat(),
    "result": receipt.result_payload,
    "action_count": 1,
    "action_id": action.id,
    "trip_count": 1,
    "trip_id": trip.id,
    "trip_status": trip.status,
    "truck_id": trip.truck_id,
    "driver_id": trip.driver_id,
    "shift_id": trip.unloading_shift_id,
    "completed_at": trip.completed_at.isoformat() if trip.completed_at else None,
    "unload_time_source": trip.unload_time_source,
}

print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
