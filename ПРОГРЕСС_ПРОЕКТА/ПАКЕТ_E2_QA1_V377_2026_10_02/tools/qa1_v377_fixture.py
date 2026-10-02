"""Bind the isolated human Driver QA truck to the isolated QA excavator.

Run only after ``prepare_excavator_qa`` and after both QA shifts are open.
The environment guard in ``load_excavator_qa_scenario`` prevents use outside
the explicitly named isolated QA database.
"""

import json

from django.utils import timezone

from assignments.models import AssignmentStatus, ExcavatorPlacement
from assignments.services import (
    apply_pending_haul_assignment,
    schedule_haul_assignment,
)
from shifts.models import EmployeeShift
from trips.qa_simulator import load_excavator_qa_scenario


scenario = load_excavator_qa_scenario()
now = timezone.now()

operator_shift = (
    EmployeeShift.objects.filter(
        employee=scenario.operator,
        equipment=scenario.excavator,
        closed_at__isnull=True,
    )
    .order_by("-opened_at", "-id")
    .first()
)
driver_shift = (
    EmployeeShift.objects.filter(
        employee=scenario.human_driver,
        equipment=scenario.human_driver_truck,
        closed_at__isnull=True,
    )
    .order_by("-opened_at", "-id")
    .first()
)
assert operator_shift is not None, "Open the QA excavator shift first."
assert driver_shift is not None, "Open the QA driver shift first."

placement = ExcavatorPlacement.objects.get(excavator=scenario.excavator)
placement.zone = ExcavatorPlacement.Zone.ACTIVE
placement.changed_by = scenario.dispatcher
placement.work_context_updated_at = now
placement.save(
    update_fields=[
        "zone",
        "changed_by",
        "work_context_updated_at",
        "changed_at",
    ]
)

assignment, created = schedule_haul_assignment(
    truck=scenario.human_driver_truck,
    excavator=scenario.excavator,
    assigned_by=scenario.dispatcher,
    now=now,
)
if assignment.status == AssignmentStatus.PENDING:
    assignment = apply_pending_haul_assignment(
        assignment.id,
        now=assignment.effective_at or now,
    )

assert assignment.status == AssignmentStatus.ACCEPTED
assert assignment.ended_at is None
assert assignment.truck_id == scenario.human_driver_truck.id
assert assignment.excavator_id == scenario.excavator.id

print(
    json.dumps(
        {
            "created": created,
            "assignment_id": assignment.id,
            "assignment_status": assignment.status,
            "operator_shift_id": operator_shift.id,
            "driver_shift_id": driver_shift.id,
            "truck_id": scenario.human_driver_truck.id,
            "truck": scenario.human_driver_truck.garage_number,
            "excavator_id": scenario.excavator.id,
            "excavator": scenario.excavator.garage_number,
            "placement": placement.zone,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
)
