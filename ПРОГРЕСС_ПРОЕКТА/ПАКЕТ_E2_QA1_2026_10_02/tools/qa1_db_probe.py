import json
import os

from core.models import OfflineFieldEvent
from trips.models import Trip, TripClientAction


event_id = os.environ["QA1_EVENT_ID"]
receipts = OfflineFieldEvent.objects.filter(event_id=event_id).order_by("id")
actions = TripClientAction.objects.filter(
    action_type="truck_loaded",
    client_action_id=event_id,
).order_by("id")
trip_ids = list(actions.values_list("trip_id", flat=True))
trips = Trip.objects.filter(pk__in=trip_ids).order_by("id")

payload = {
    "event_id": event_id,
    "receipt_count": receipts.count(),
    "receipts": [
        {
            "id": receipt.id,
            "status": receipt.status,
            "retryable": receipt.retryable,
            "error_code": receipt.error_code,
            "event_type": receipt.event_type,
            "occurred_at": receipt.occurred_at.isoformat(),
            "received_at": receipt.received_at.isoformat(),
            "sequence": receipt.sequence,
            "depends_on": receipt.depends_on,
            "trip_id": receipt.trip_id,
            "result_payload": receipt.result_payload,
        }
        for receipt in receipts
    ],
    "action_count": actions.count(),
    "actions": [
        {
            "id": action.id,
            "trip_id": action.trip_id,
            "created_at": action.created_at.isoformat(),
        }
        for action in actions
    ],
    "trip_count": trips.count(),
    "trips": [
        {
            "id": trip.id,
            "truck_id": trip.truck_id,
            "status": trip.status,
            "loaded_at": trip.loaded_at.isoformat() if trip.loaded_at else None,
            "load_received_at": (
                trip.load_received_at.isoformat() if trip.load_received_at else None
            ),
            "load_time_source": trip.load_time_source,
            "completed_at": trip.completed_at.isoformat() if trip.completed_at else None,
            "cancelled_at": trip.cancelled_at.isoformat() if trip.cancelled_at else None,
        }
        for trip in trips
    ],
}

print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
