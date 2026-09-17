#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

try:
    import fcntl
    import grp
    import pwd
except ImportError:  # pragma: no cover - permits protocol tests on Windows only
    fcntl = None
    grp = None
    pwd = None


APP = Path("/srv/accounting-mvp")
BACKUPS = APP / "backups" / "code"
LOCK = Path("/run/lock/accounting-github-deploy.lock")
MAX_PACKAGE_BYTES = 150 * 1024 * 1024
CODE_MODES = {"verify", "deploy"}
MIGRATION_MODES = {"verify_migrations", "deploy_migrations"}
APK_MODES = {"verify_apk", "publish_apk"}
DATA_MODES = {"verify_data", "apply_data"}
RECEIVER_MODES = {"verify_receiver", "update_receiver"}
DIAGNOSTIC_MODES = {"diagnose"}
ALL_MODES = CODE_MODES | MIGRATION_MODES | APK_MODES | DATA_MODES | RECEIVER_MODES | DIAGNOSTIC_MODES | {"rollback"}
VERIFY_MODES = {"verify", "verify_migrations", "verify_apk", "verify_data", "verify_receiver"}
RECEIVER_PAYLOAD = "deploy/receiver/accounting_github_deploy_receiver.py"
RECEIVER_PATH = Path("/usr/local/sbin/accounting-github-deploy-receiver")
DIAGNOSTIC_OPERATIONS = {"trip_accounting_incident_v1"}
DIAGNOSTIC_METADATA_KEYS = {"operation", "equipment", "from_utc", "to_utc", "max_rows"}
DIAGNOSTIC_EQUIPMENT_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё ._-]{1,64}\Z")
DIAGNOSTIC_MAX_WINDOW = timedelta(hours=24)
DIAGNOSTIC_MAX_ROWS = 500
DIAGNOSTIC_MAX_OUTPUT_BYTES = 512 * 1024
DIAGNOSTIC_PROCESS_TIMEOUT_SECONDS = 45
DIAGNOSTIC_OS_USER = "deploy"
DIAGNOSTIC_OS_GROUP = "www-data"
DIAGNOSTIC_OPENSSL = Path("/usr/bin/openssl")
DIAGNOSTIC_CERT_TEMP_DIR = Path("/run")
DIAGNOSTIC_ENCRYPT_TIMEOUT_SECONDS = 15
DIAGNOSTIC_RECIPIENT_FINGERPRINT = "36:65:5B:1C:BD:66:01:2A:15:2C:97:8B:CF:79:AC:FE:E8:46:5A:7A:1C:50:FD:FD:5C:F7:52:2C:A2:BD:59:6D"
DIAGNOSTIC_RECIPIENT_CERTIFICATE = b'''-----BEGIN CERTIFICATE-----
MIIERTCCAq2gAwIBAgIUFuSdKm+OVGdq+pQqzXlwI5AHX1gwDQYJKoZIhvcNAQEL
BQAwMjEwMC4GA1UEAwwnQ29wcGVyIFByb2R1Y3Rpb24gRGlhZ25vc3RpY3MgUmVj
aXBpZW50MB4XDTI2MDkxNzA4MTkwOVoXDTI4MDkxNjA4MTkwOVowMjEwMC4GA1UE
AwwnQ29wcGVyIFByb2R1Y3Rpb24gRGlhZ25vc3RpY3MgUmVjaXBpZW50MIIBojAN
BgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAvTOWRvNi08Ofu4dnqWJ8SYIZVC7L
rLgJYMnvJs2KUIGe+TrTHrQgeuOwaXIWvUc+/IRcMxDmmHKZn7zlTWB7S4HwHAcv
nqwLzBBQ2GO0ehlltbyu2+gALmw9aiVaYZpDqtZlgfHQr3LpynTIsgk3NchDiH3s
Bu9mW+Fh0RhizU+n7UixMQbmGfS0So79iYtrc1TEMcUBosWKV8YZnE/0d5qerjO2
DY5CadbMYIBhwL+sWMZuMwvA5bshDwQbt7FUpqQLCuqvyKhm945lOdZUFdCd3U+6
MY4uytUaY35LaDbrmYWHZbK4w9CKw18EdQIY13RiHdy5Iqva3TwL6wtxZeUPNGjH
OOZ3Sabww7ov5UOXeJb3cw1bJD9lYX8/MxlxHtr1zwFCpawoDCO3BbGPRnPFpzN3
3A3ePJSwODTgklhKPBmoiWSKtm37dsIiOJGpZh+8jpWxLc0i/zSko72zeCdpIi9d
NTX/Xcln0WSEWMyqOgBKUeRV+BRZQFs9P4pJAgMBAAGjUzBRMB0GA1UdDgQWBBS3
3S8OzmeYOPmPKH+cqV4V+DAfsTAfBgNVHSMEGDAWgBS33S8OzmeYOPmPKH+cqV4V
+DAfsTAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBgQBiFSum7J2m
Z9cXfDW/6E6wy1/ESgkyXygiwfr8DZTCW2K5tCxXKTPI4OZDdSeP1JHyczBywI2b
ivlE+8SUo6DosiBL9Iiqq4Wv9i9FQsjXLx6lK0dy2dAJu7c2hb8P5IeP5nDhmekl
Wma/hb0o8GVEEzL7IKyu9di8QUyCHK8G44LUoL9b3fHM786a4q3xYCDZwWSO6AfK
koasybb1v8iReV43Q7Scz7+6oHnpokRsaME5408wyEVNwyUNUU4zS5sMsDnZ72Nc
cQk9/ATndwAFu0Yf7N0+8SqrpX4e2sPTqq54G2bzweJioAMfCyOoUIT0+JXn4cJL
UYcI3DlslDHKG+EMx879L1hBiKSLdpN8WcOjYfkhn2rivVj2RYHbNUIMY8pcG4lG
AQwpRYR9ynTQpX26DDThJdq2PJXJxYa0cDZR7X+nkvmuC9EVjCnR0UkD5XKNoOT1
e9+eGJoUtxF6kO3VLFRdxU8+kqXOxDgrpvJFjpht3j/S1UvB85V7s3I=
-----END CERTIFICATE-----
'''
ALLOWED_TOP_LEVEL = {
    "assignments", "config", "core", "deploy", "downtimes", "portal",
    "references", "reports", "rotations", "settlement", "shifts", "static",
    "templates", "tools", "trips", "users",
}
ALLOWED_ROOT_FILES = {"manage.py", "requirements.txt"}


DIAGNOSTIC_QUERY_SOURCE = r'''
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
django.setup()

from django.db import connection, transaction
from django.db.models import Q

from assignments.models import EquipmentAssignment, HaulAssignment, HaulAssignmentHandoff
from core.models import OfflineFieldEvent, OfflineFieldEventConflict, OperationalStateEvent, OperationalStateVersion
from downtimes.models import DowntimeEvent
from references.models import Equipment
from shifts.models import EmployeeShift, ShiftClientAction
from trips.models import DispatcherActionLog, FreeBucketAcceptance, Trip, TripClientAction
from users.models import ActiveApplicationSession, AdminConflict, ClientErrorReport


SAFE_CODE = re.compile(r"[0-9A-Za-z_.:@+-]{0,160}\Z")


def iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_text(value, maximum=160):
    text = str(value or "").strip()
    if len(text) <= maximum and "://" not in text and not any(ord(char) < 32 for char in text):
        return text
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:20]


def safe_code(value):
    text = str(value or "").strip()
    if SAFE_CODE.fullmatch(text):
        return text
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:20]


def raw_text_sha256(value):
    return hashlib.sha256(str(value or "").encode("utf-8", "replace")).hexdigest()


def normalized(value):
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return safe_text(value)


def clean_row(row, code_fields=()):
    result = {}
    for key, value in row.items():
        output_key = key.replace("__", "_")
        result[output_key] = safe_code(value) if key in code_fields and value is not None else normalized(value)
    return result


def selector_key(value):
    text = str(value or "").strip().upper().replace("Ё", "Е")
    digits = re.findall(r"\d+", text)
    if len(digits) == 1:
        return str(int(digits[0]))
    return re.sub(r"[^0-9A-ZА-Я]+", "", text)


def equipment_candidates(selector):
    rows = list(
        Equipment.objects.select_related("equipment_type")
        .only("id", "garage_number", "is_active", "equipment_type__name")
        .order_by("id")[:1000]
    )
    exact = [item for item in rows if item.garage_number.casefold() == selector.casefold()]
    if exact:
        return exact
    key = selector_key(selector)
    excavators = [
        item for item in rows
        if "экск" in item.equipment_type.name.casefold() or "excav" in item.equipment_type.name.casefold()
    ]
    return [item for item in excavators if selector_key(item.garage_number) == key]


def main():
    operation, selector, from_text, to_text, max_rows_text = sys.argv[1:6]
    start = datetime.strptime(from_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    end = datetime.strptime(to_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    max_rows = int(max_rows_text)
    budget = {"remaining": max_rows}
    sections = {}

    def collect(name, queryset, fields, order_by, cap, hash_fields=()):
        total = queryset.count()
        take = min(total, budget["remaining"], cap)
        rows = []
        for raw_row in queryset.order_by(*order_by).values(*fields)[:take]:
            for field in hash_fields:
                raw_row[field + "_sha256"] = raw_text_sha256(raw_row.pop(field, None))
            rows.append(clean_row(raw_row, code_fields={
                "status", "shift_type", "workplace_code", "action", "action_type",
                "source", "source_kind", "role_code", "event_type", "error_code",
                "client_kind", "device_kind", "app_code", "app_version", "screen",
                "load_time_source", "unload_time_source", "object_type", "reason",
                "service_close_kind",
            }))
        budget["remaining"] -= len(rows)
        section = {
            "total": total,
            "returned": len(rows),
            "truncated": total > len(rows),
            "rows": rows,
        }
        sections[name] = section
        return rows

    with transaction.atomic():
        with connection.cursor() as cursor:
            if connection.vendor != "postgresql":
                raise RuntimeError("diagnostic database vendor is not supported")
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '12000ms'")
            cursor.execute("SET LOCAL lock_timeout = '2000ms'")
            cursor.execute("SHOW transaction_read_only")
            if cursor.fetchone()[0] != "on":
                raise RuntimeError("diagnostic transaction is not read only")
            cursor.execute("SHOW transaction_isolation")
            if cursor.fetchone()[0] != "repeatable read":
                raise RuntimeError("diagnostic transaction is not repeatable read")

        matches = equipment_candidates(selector)
        base = {
            "schema": 1,
            "operation": operation,
            "request": {
                "equipment": safe_text(selector, 64),
                "from_utc": from_text,
                "to_utc": to_text,
                "max_rows": max_rows,
            },
            "database": {
                "vendor": "postgresql",
                "transaction_read_only": True,
                "transaction_isolation": "repeatable read",
            },
        }
        if len(matches) != 1:
            candidates = [
                {
                    "id": item.id,
                    "garage_number": safe_text(item.garage_number, 64),
                    "equipment_type": safe_text(item.equipment_type.name, 128),
                    "is_active": item.is_active,
                }
                for item in matches[:min(20, max_rows)]
            ]
            base.update({
                "resolution": "not_found" if not matches else "ambiguous",
                "candidates": candidates,
                "sections": {},
                "summary": {"row_count": len(candidates), "truncated": len(matches) > len(candidates)},
            })
            print(json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return

        equipment = matches[0]
        base["resolution"] = "resolved"
        base["equipment"] = {
            "id": equipment.id,
            "garage_number": safe_text(equipment.garage_number, 64),
            "equipment_type": safe_text(equipment.equipment_type.name, 128),
            "is_active": equipment.is_active,
        }

        shifts_qs = EmployeeShift.objects.filter(
            equipment_id=equipment.id,
            opened_at__lt=end,
        ).filter(Q(closed_at__isnull=True) | Q(closed_at__gte=start))
        shift_rows = collect("shifts", shifts_qs, (
            "id", "employee_id", "shift_type", "workplace_code", "equipment_id",
            "opened_at", "closed_at", "is_service_closed", "service_close_kind",
            "plan_group_id", "plan_group_name", "plan_calculation_mode", "plan_value",
        ), ("opened_at", "id"), 40)
        shift_ids = {row["id"] for row in shift_rows}
        employee_ids = {row["employee_id"] for row in shift_rows if row.get("employee_id")}

        trip_time = (
            Q(created_at__gte=start, created_at__lt=end)
            | Q(loaded_at__gte=start, loaded_at__lt=end)
            | Q(load_received_at__gte=start, load_received_at__lt=end)
            | Q(completed_at__gte=start, completed_at__lt=end)
            | Q(unload_received_at__gte=start, unload_received_at__lt=end)
            | Q(cancelled_at__gte=start, cancelled_at__lt=end)
            | Q(operationally_closed_at__gte=start, operationally_closed_at__lt=end)
            | (
                Q(created_at__lt=start)
                & (
                    Q(
                        completed_at__isnull=True,
                        cancelled_at__isnull=True,
                        operationally_closed_at__isnull=True,
                    )
                    | Q(completed_at__gte=start)
                    | Q(cancelled_at__gte=start)
                    | Q(operationally_closed_at__gte=start)
                )
            )
        )
        trips_qs = Trip.objects.filter(
            Q(excavator_id=equipment.id) | Q(truck_id=equipment.id)
        ).filter(trip_time)
        trip_rows = collect("trips", trips_qs, (
            "id", "excavator_id", "excavator__garage_number", "truck_id", "truck__garage_number",
            "excavator_operator_id", "driver_id", "loading_shift_id", "unloading_shift_id",
            "driver_control_shift_id", "status", "created_at", "loaded_at", "load_received_at",
            "load_time_source", "completed_at", "unload_received_at", "unload_time_source",
            "cancelled_at", "operationally_closed_at", "superseded_by_id", "is_carryover",
            "driver_participation_recorded", "volume_m3", "dump_point_id", "dump_point__name",
            "assigned_dump_point_id", "assigned_dump_point__name", "actual_dump_point_id",
            "actual_dump_point__name",
        ), ("created_at", "id"), 180)
        trip_ids = {row["id"] for row in trip_rows}
        related_equipment_ids = {equipment.id}
        for row in trip_rows:
            related_equipment_ids.update(item for item in (row.get("excavator_id"), row.get("truck_id")) if item)
            employee_ids.update(item for item in (row.get("excavator_operator_id"), row.get("driver_id")) if item)
            shift_ids.update(item for item in (
                row.get("loading_shift_id"), row.get("unloading_shift_id"), row.get("driver_control_shift_id")
            ) if item)

        haul_qs = HaulAssignment.objects.filter(
            Q(excavator_id=equipment.id) | Q(truck_id=equipment.id),
            assigned_at__lt=end,
        ).filter(Q(ended_at__isnull=True) | Q(ended_at__gte=start))
        haul_rows = collect("haul_assignments", haul_qs, (
            "id", "excavator_id", "excavator__garage_number", "truck_id", "truck__garage_number",
            "action", "status", "assigned_at", "effective_at", "accepted_at", "ended_at",
        ), ("assigned_at", "id"), 80)
        haul_ids = {row["id"] for row in haul_rows}
        for row in haul_rows:
            related_equipment_ids.update(item for item in (row.get("excavator_id"), row.get("truck_id")) if item)

        acceptance_model_fields = {field.name for field in FreeBucketAcceptance._meta.fields}
        acceptance_time = (
            Q(occurred_at__gte=start, occurred_at__lt=end)
            | Q(received_at__gte=start, received_at__lt=end)
            | Q(cancelled_at__gte=start, cancelled_at__lt=end)
            | Q(used_at__gte=start, used_at__lt=end)
            | Q(closed_at__gte=start, closed_at__lt=end)
            | Q(status="accepted")
        )
        if "accepted_at" in acceptance_model_fields:
            acceptance_time |= Q(accepted_at__gte=start, accepted_at__lt=end)
        acceptances_qs = FreeBucketAcceptance.objects.filter(
            Q(excavator_id=equipment.id) | Q(truck_id=equipment.id)
        ).filter(acceptance_time)
        acceptance_value_fields = tuple(field for field in (
            "id", "client_acceptance_id", "truck_id", "truck__garage_number", "excavator_id",
            "excavator__garage_number", "operator_id", "loading_shift_id", "requested_by_id",
            "requesting_shift_id", "primary_assignment_id", "status", "occurred_at", "received_at",
            "accepted_at", "cancelled_at", "used_at", "closed_at", "used_trip_id",
        ) if field.split("__", 1)[0].removesuffix("_id") in acceptance_model_fields or field.split("__", 1)[0] in acceptance_model_fields)
        acceptance_rows = collect(
            "free_bucket_acceptances", acceptances_qs, acceptance_value_fields, ("occurred_at", "id"), 80
        )
        for row in acceptance_rows:
            related_equipment_ids.update(item for item in (row.get("excavator_id"), row.get("truck_id")) if item)
            employee_ids.update(item for item in (row.get("operator_id"), row.get("requested_by_id")) if item)
            shift_ids.update(item for item in (row.get("loading_shift_id"), row.get("requesting_shift_id")) if item)
            if row.get("used_trip_id"):
                trip_ids.add(row["used_trip_id"])

        equipment_assignments_qs = EquipmentAssignment.objects.filter(
            equipment_id__in=related_equipment_ids,
            assigned_at__lt=end,
        ).filter(Q(ended_at__isnull=True) | Q(ended_at__gte=start))
        assignment_rows = collect("equipment_assignments", equipment_assignments_qs, (
            "id", "employee_id", "role_id", "equipment_id", "equipment__garage_number", "shift_type",
            "shift_id", "status", "assigned_at", "accepted_at", "ended_at", "source_kind",
        ), ("assigned_at", "id"), 60)
        for row in assignment_rows:
            if row.get("employee_id"):
                employee_ids.add(row["employee_id"])
            if row.get("shift_id"):
                shift_ids.add(row["shift_id"])

        downtime_rows = collect("downtimes", DowntimeEvent.objects.filter(
            equipment_id__in=related_equipment_ids,
            started_at__lt=end,
        ).filter(Q(ended_at__isnull=True) | Q(ended_at__gte=start)), (
            "id", "equipment_id", "equipment__garage_number", "reason_id", "reason__name",
            "reason__is_critical", "source", "started_at", "ended_at", "recorded_at",
            "subject_employee_id", "recorded_by_id",
        ), ("started_at", "id"), 80)

        if trip_ids:
            collect("trip_client_actions", TripClientAction.objects.filter(
                trip_id__in=trip_ids,
                created_at__gte=start,
                created_at__lt=end,
            ), ("id", "action_type", "client_action_id", "trip_id", "actor_id", "created_at"),
                ("created_at", "id"), 80)
            collect("dispatcher_actions", DispatcherActionLog.objects.filter(
                Q(trip_id__in=trip_ids) | Q(shift_id__in=shift_ids) | Q(haul_assignment_id__in=haul_ids),
                created_at__gte=start,
                created_at__lt=end,
            ), ("id", "action_type", "trip_id", "shift_id", "haul_assignment_id", "actor_id", "created_at"),
                ("created_at", "id"), 60)
        else:
            sections["trip_client_actions"] = {"total": 0, "returned": 0, "truncated": False, "rows": []}
            sections["dispatcher_actions"] = {"total": 0, "returned": 0, "truncated": False, "rows": []}

        if shift_ids:
            collect("shift_client_actions", ShiftClientAction.objects.filter(
                shift_id__in=shift_ids,
                created_at__gte=start,
                created_at__lt=end,
            ), ("id", "action_type", "client_action_id", "employee_id", "shift_id", "created_at"),
                ("created_at", "id"), 60)
        else:
            sections["shift_client_actions"] = {"total": 0, "returned": 0, "truncated": False, "rows": []}

        event_scope = Q(equipment_id__in=related_equipment_ids)
        if trip_ids:
            event_scope |= Q(trip_id__in=trip_ids)
        if employee_ids:
            event_scope |= Q(actor_id__in=employee_ids)
        offline_qs = OfflineFieldEvent.objects.filter(event_scope).filter(
            Q(occurred_at__gte=start, occurred_at__lt=end)
            | Q(received_at__gte=start, received_at__lt=end)
        )
        offline_rows = collect("offline_events", offline_qs, (
            "id", "event_id", "event_type", "role_code", "sequence", "depends_on", "occurred_at",
            "received_at", "actor_id", "access_id", "shift_id", "equipment_id", "trip_id",
            "downtime_event_id", "local_trip_id", "local_downtime_id", "status", "retryable",
            "error_code", "created_at", "updated_at",
        ), ("received_at", "id"), 140)
        offline_ids = {row["id"] for row in offline_rows}
        if offline_ids:
            collect("offline_conflicts", OfflineFieldEventConflict.objects.filter(
                existing_event_id__in=offline_ids,
                received_at__gte=start,
                received_at__lt=end,
            ), ("id", "existing_event_id", "attempted_event_id", "actor_id", "access_id", "role_code",
                "code", "received_at"), ("received_at", "id"), 60)
        else:
            sections["offline_conflicts"] = {"total": 0, "returned": 0, "truncated": False, "rows": []}

        if employee_ids:
            collect("admin_conflicts", AdminConflict.objects.filter(
                employee_id__in=employee_ids,
            ).filter(
                Q(created_at__gte=start, created_at__lt=end)
                | Q(resolved_at__gte=start, resolved_at__lt=end)
                | Q(status__in=("open", "in_progress"))
            ), (
                "id", "employee_id", "role_id", "conflict_type", "process", "status",
                "created_at", "resolved_at", "resolved_by_id",
            ), ("created_at", "id"), 40)
        else:
            sections["admin_conflicts"] = {"total": 0, "returned": 0, "truncated": False, "rows": []}

        if haul_ids:
            collect("haul_handoffs", HaulAssignmentHandoff.objects.filter(
                Q(source_assignment_id__in=haul_ids) | Q(target_assignment_id__in=haul_ids),
                created_at__lt=end,
            ).filter(Q(resolved_at__isnull=True) | Q(resolved_at__gte=start)), (
                "id", "truck_id", "source_assignment_id", "target_assignment_id", "source_excavator_id",
                "source_shift_id", "status", "created_at", "resolved_at", "resolved_by_trip_id",
            ), ("created_at", "id"), 60)
        else:
            sections["haul_handoffs"] = {"total": 0, "returned": 0, "truncated": False, "rows": []}

        object_scopes = {
            "Equipment": {equipment.id},
            "Trip": set(trip_ids),
            "EmployeeShift": set(shift_ids),
            "HaulAssignment": set(haul_ids),
            "EquipmentAssignment": {row["id"] for row in assignment_rows},
            "FreeBucketAcceptance": {row["id"] for row in acceptance_rows},
            "DowntimeEvent": {row["id"] for row in downtime_rows},
        }
        operational_scope = Q(event_type="test_shift_data_reset")
        for object_type, identifiers in object_scopes.items():
            if identifiers:
                operational_scope |= Q(object_type=object_type, object_id__in=[str(item) for item in identifiers])
        operational_qs = OperationalStateEvent.objects.filter(
            operational_scope,
            created_at__gte=start,
            created_at__lt=end,
        )
        collect("operational_events", operational_qs, (
            "id", "key", "version", "event_type", "object_type", "object_id", "reason", "created_at",
        ), ("version", "id"), 160)
        version_total = OperationalStateVersion.objects.count()
        version_take = min(20, budget["remaining"], version_total)
        current_versions = [clean_row(row, {"key", "reason"}) for row in
            OperationalStateVersion.objects.order_by("key").values("key", "version", "reason", "updated_at")[:version_take]]
        budget["remaining"] -= len(current_versions)
        sections["operational_versions"] = {
            "total": version_total, "returned": len(current_versions),
            "truncated": version_total > len(current_versions),
            "rows": current_versions,
        }

        if employee_ids:
            collect("application_sessions", ActiveApplicationSession.objects.filter(
                access__employee_id__in=employee_ids,
                first_seen_at__lt=end,
                last_seen_at__gte=start,
            ), ("id", "access_id", "access__employee_id", "role_code", "app_code", "device_kind",
                "client_kind", "client_version", "first_seen_at", "last_seen_at", "foreground_seen_at",
                "background_seen_at"), ("last_seen_at", "id"), 60)
            collect("client_errors", ClientErrorReport.objects.filter(
                employee_id__in=employee_ids,
                happened_at__gte=start,
                happened_at__lt=end,
            ), ("id", "employee_id", "role_code", "app_version", "screen", "message", "happened_at"),
                ("happened_at", "id"), 60, ("message",))
        else:
            sections["application_sessions"] = {"total": 0, "returned": 0, "truncated": False, "rows": []}
            sections["client_errors"] = {"total": 0, "returned": 0, "truncated": False, "rows": []}

        returned = sum(section["returned"] for section in sections.values())
        truncated = any(section["truncated"] for section in sections.values())
        base["sections"] = sections
        base["summary"] = {"row_count": returned, "truncated": truncated}
        print(json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
'''


class ReleaseError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_diagnostic_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReleaseError(f"diagnostic {field} must be a UTC timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ReleaseError(f"diagnostic {field} must use YYYY-MM-DDTHH:MM:SSZ") from exc


def validate_diagnostic_metadata(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, dict) or set(metadata) != DIAGNOSTIC_METADATA_KEYS:
        raise ReleaseError("diagnostic metadata keys do not match the fixed contract")
    operation = metadata.get("operation")
    equipment = metadata.get("equipment")
    if operation not in DIAGNOSTIC_OPERATIONS:
        raise ReleaseError("diagnostic operation is not allowlisted")
    if (
        not isinstance(equipment, str)
        or equipment != equipment.strip()
        or not DIAGNOSTIC_EQUIPMENT_RE.fullmatch(equipment)
    ):
        raise ReleaseError("diagnostic equipment identifier is invalid")
    from_utc = parse_diagnostic_utc(metadata.get("from_utc"), "from_utc")
    to_utc = parse_diagnostic_utc(metadata.get("to_utc"), "to_utc")
    if to_utc <= from_utc or to_utc - from_utc > DIAGNOSTIC_MAX_WINDOW:
        raise ReleaseError("diagnostic window must be positive and no longer than 24 hours")
    max_rows = metadata.get("max_rows")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= DIAGNOSTIC_MAX_ROWS:
        raise ReleaseError(f"diagnostic max_rows must be between 1 and {DIAGNOSTIC_MAX_ROWS}")
    return {
        "operation": operation,
        "equipment": equipment,
        "from_utc": from_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to_utc": to_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_rows": max_rows,
    }


FORBIDDEN_DIAGNOSTIC_KEYS = {
    "access_code", "authorization", "cookie", "endpoint", "full_name", "password",
    "path", "payload", "personnel_number", "phone", "pin", "query", "request_body",
    "secret", "session_key", "sql", "stack", "token", "url", "user_agent", "vin",
}

DIAGNOSTIC_ALLOWED_SECTION_FIELDS = {
    "shifts": {
        "id", "employee_id", "shift_type", "workplace_code", "equipment_id", "opened_at",
        "closed_at", "is_service_closed", "service_close_kind", "plan_group_id",
        "plan_group_name", "plan_calculation_mode", "plan_value",
    },
    "trips": {
        "id", "excavator_id", "excavator_garage_number", "truck_id", "truck_garage_number",
        "excavator_operator_id", "driver_id", "loading_shift_id", "unloading_shift_id",
        "driver_control_shift_id", "status", "created_at", "loaded_at", "load_received_at",
        "load_time_source", "completed_at", "unload_received_at", "unload_time_source",
        "cancelled_at", "operationally_closed_at", "superseded_by_id", "is_carryover",
        "driver_participation_recorded", "volume_m3", "dump_point_id", "dump_point_name",
        "assigned_dump_point_id", "assigned_dump_point_name", "actual_dump_point_id",
        "actual_dump_point_name",
    },
    "haul_assignments": {
        "id", "excavator_id", "excavator_garage_number", "truck_id", "truck_garage_number",
        "action", "status", "assigned_at", "effective_at", "accepted_at", "ended_at",
    },
    "free_bucket_acceptances": {
        "id", "client_acceptance_id", "truck_id", "truck_garage_number", "excavator_id",
        "excavator_garage_number", "operator_id", "loading_shift_id", "requested_by_id",
        "requesting_shift_id", "primary_assignment_id", "status", "occurred_at", "received_at",
        "accepted_at", "cancelled_at", "used_at", "closed_at", "used_trip_id",
    },
    "equipment_assignments": {
        "id", "employee_id", "role_id", "equipment_id", "equipment_garage_number", "shift_type",
        "shift_id", "status", "assigned_at", "accepted_at", "ended_at", "source_kind",
    },
    "downtimes": {
        "id", "equipment_id", "equipment_garage_number", "reason_id", "reason_name",
        "reason_is_critical", "source", "started_at", "ended_at", "recorded_at",
        "subject_employee_id", "recorded_by_id",
    },
    "trip_client_actions": {
        "id", "action_type", "client_action_id", "trip_id", "actor_id", "created_at",
    },
    "dispatcher_actions": {
        "id", "action_type", "trip_id", "shift_id", "haul_assignment_id", "actor_id", "created_at",
    },
    "shift_client_actions": {
        "id", "action_type", "client_action_id", "employee_id", "shift_id", "created_at",
    },
    "offline_events": {
        "id", "event_id", "event_type", "role_code", "sequence", "depends_on", "occurred_at",
        "received_at", "actor_id", "access_id", "shift_id", "equipment_id", "trip_id",
        "downtime_event_id", "local_trip_id", "local_downtime_id", "status", "retryable",
        "error_code", "created_at", "updated_at",
    },
    "offline_conflicts": {
        "id", "existing_event_id", "attempted_event_id", "actor_id", "access_id", "role_code",
        "code", "received_at",
    },
    "admin_conflicts": {
        "id", "employee_id", "role_id", "conflict_type", "process", "status", "created_at",
        "resolved_at", "resolved_by_id",
    },
    "haul_handoffs": {
        "id", "truck_id", "source_assignment_id", "target_assignment_id", "source_excavator_id",
        "source_shift_id", "status", "created_at", "resolved_at", "resolved_by_trip_id",
    },
    "operational_events": {
        "id", "key", "version", "event_type", "object_type", "object_id", "reason", "created_at",
    },
    "operational_versions": {"key", "version", "reason", "updated_at"},
    "application_sessions": {
        "id", "access_id", "access_employee_id", "role_code", "app_code", "device_kind",
        "client_kind", "client_version", "first_seen_at", "last_seen_at", "foreground_seen_at",
        "background_seen_at",
    },
    "client_errors": {
        "id", "employee_id", "role_code", "app_version", "screen", "happened_at", "message_sha256",
    },
}


def reject_sensitive_diagnostic_value(value: object, *, key: str = "") -> None:
    normalized_key = key.casefold()
    if normalized_key in FORBIDDEN_DIAGNOSTIC_KEYS or any(
        marker in normalized_key for marker in ("password", "secret", "cookie", "authorization", "token")
    ):
        raise ReleaseError("diagnostic report contains a forbidden field")
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if not isinstance(child_key, str):
                raise ReleaseError("diagnostic report key is invalid")
            reject_sensitive_diagnostic_value(child_value, key=child_key)
    elif isinstance(value, list):
        for child in value:
            reject_sensitive_diagnostic_value(child, key=key)
    elif isinstance(value, str):
        if len(value) > 512 or "://" in value or any(ord(char) < 32 for char in value):
            raise ReleaseError("diagnostic report contains an unsafe string")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ReleaseError("diagnostic report contains an unsupported value")


def validate_diagnostic_report(
    raw: bytes,
    metadata: dict[str, object],
) -> dict[str, Any]:
    if not raw or len(raw) > DIAGNOSTIC_MAX_OUTPUT_BYTES:
        raise ReleaseError("diagnostic report size is invalid")
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("diagnostic report is not valid JSON") from exc
    if not isinstance(report, dict) or report.get("schema") != 1:
        raise ReleaseError("diagnostic report schema is invalid")
    if report.get("operation") != metadata["operation"]:
        raise ReleaseError("diagnostic report operation mismatch")
    expected_request = {
        "equipment": metadata["equipment"],
        "from_utc": metadata["from_utc"],
        "to_utc": metadata["to_utc"],
        "max_rows": metadata["max_rows"],
    }
    if report.get("request") != expected_request:
        raise ReleaseError("diagnostic report request mismatch")
    database = report.get("database")
    if database != {
        "vendor": "postgresql",
        "transaction_read_only": True,
        "transaction_isolation": "repeatable read",
    }:
        raise ReleaseError("diagnostic report does not prove a repeatable read-only PostgreSQL transaction")
    if report.get("resolution") not in {"resolved", "not_found", "ambiguous"}:
        raise ReleaseError("diagnostic equipment resolution is invalid")
    resolved = report["resolution"] == "resolved"
    expected_top_level = {
        "schema", "operation", "request", "database", "resolution", "sections", "summary",
        "equipment" if resolved else "candidates",
    }
    if set(report) != expected_top_level:
        raise ReleaseError("diagnostic report top-level contract mismatch")
    if resolved:
        equipment = report.get("equipment")
        if not isinstance(equipment, dict) or set(equipment) != {
            "id", "garage_number", "equipment_type", "is_active",
        }:
            raise ReleaseError("diagnostic equipment contract is invalid")
    summary = report.get("summary")
    if not isinstance(summary, dict) or set(summary) != {"row_count", "truncated"}:
        raise ReleaseError("diagnostic summary is invalid")
    row_count = summary.get("row_count")
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
        or row_count > metadata["max_rows"]
        or not isinstance(summary.get("truncated"), bool)
    ):
        raise ReleaseError("diagnostic row limit was not respected")
    sections = report.get("sections")
    if not isinstance(sections, dict):
        raise ReleaseError("diagnostic sections are invalid")
    if resolved and set(sections) != set(DIAGNOSTIC_ALLOWED_SECTION_FIELDS):
        raise ReleaseError("diagnostic section allowlist mismatch")
    section_rows = 0
    for name, section in sections.items():
        if (
            not isinstance(name, str)
            or name not in DIAGNOSTIC_ALLOWED_SECTION_FIELDS
            or not isinstance(section, dict)
        ):
            raise ReleaseError("diagnostic section is invalid")
        if set(section) != {"total", "returned", "truncated", "rows"}:
            raise ReleaseError("diagnostic section contract mismatch")
        rows = section.get("rows")
        returned = section.get("returned")
        total = section.get("total")
        if (
            not isinstance(rows, list)
            or isinstance(returned, bool)
            or not isinstance(returned, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or returned != len(rows)
            or total < returned
            or not isinstance(section.get("truncated"), bool)
            or section["truncated"] != (total > returned)
        ):
            raise ReleaseError("diagnostic section limits are invalid")
        allowed_fields = DIAGNOSTIC_ALLOWED_SECTION_FIELDS[name]
        for row in rows:
            if not isinstance(row, dict) or not set(row).issubset(allowed_fields):
                raise ReleaseError("diagnostic row field is not allowlisted")
        section_rows += returned
    if resolved:
        if section_rows != row_count:
            raise ReleaseError("diagnostic summary row count mismatch")
        if summary["truncated"] != any(section["truncated"] for section in sections.values()):
            raise ReleaseError("diagnostic summary truncation mismatch")
    else:
        candidates = report.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != row_count or sections:
            raise ReleaseError("diagnostic equipment candidates are invalid")
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != {
                "id", "garage_number", "equipment_type", "is_active",
            }:
                raise ReleaseError("diagnostic equipment candidate contract is invalid")
    reject_sensitive_diagnostic_value(report)
    return report


def encrypt_diagnostic_report(report: dict[str, Any]) -> dict[str, Any]:
    plaintext = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not plaintext or len(plaintext) > DIAGNOSTIC_MAX_OUTPUT_BYTES:
        raise ReleaseError("diagnostic plaintext size is invalid")
    if not DIAGNOSTIC_OPENSSL.is_file():
        raise ReleaseError("diagnostic encryption tool is unavailable")
    if not DIAGNOSTIC_CERT_TEMP_DIR.is_dir():
        raise ReleaseError("diagnostic certificate runtime directory is unavailable")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix="accounting-diagnostic-recipient-",
        suffix=".pem",
        dir=DIAGNOSTIC_CERT_TEMP_DIR,
    ) as certificate_file:
        certificate_file.write(DIAGNOSTIC_RECIPIENT_CERTIFICATE)
        certificate_file.flush()
        os.chmod(certificate_file.name, 0o644)
        try:
            encrypted = subprocess.run(
                [
                    str(DIAGNOSTIC_OPENSSL),
                    "cms", "-encrypt", "-binary", "-aes-256-cbc", "-outform", "DER",
                    certificate_file.name,
                ],
                check=False,
                input=plaintext,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=DIAGNOSTIC_ENCRYPT_TIMEOUT_SECONDS,
                close_fds=True,
                start_new_session=True,
                user=DIAGNOSTIC_OS_USER,
                group=DIAGNOSTIC_OS_GROUP,
                extra_groups=(),
                umask=0o077,
                env={"HOME": str(APP), "PATH": "/usr/bin:/bin"},
            )
        except subprocess.TimeoutExpired as exc:
            raise ReleaseError("diagnostic encryption timed out") from exc
    if encrypted.returncode != 0:
        error_reference = digest(encrypted.stderr or encrypted.stdout)[:16]
        raise ReleaseError(f"diagnostic encryption failed; reference={error_reference}")
    ciphertext = encrypted.stdout
    if not ciphertext or len(ciphertext) > DIAGNOSTIC_MAX_OUTPUT_BYTES + 65536:
        raise ReleaseError("diagnostic ciphertext size is invalid")
    return {
        "schema": 1,
        "kind": "production_diagnostic_ciphertext",
        "operation": report["operation"],
        "summary": report["summary"],
        "ciphertext_format": "CMS-DER",
        "recipient_fingerprint": DIAGNOSTIC_RECIPIENT_FINGERPRINT,
        "ciphertext_sha256": digest(ciphertext),
        "ciphertext_base64": base64.b64encode(ciphertext).decode("ascii"),
    }


def run(command: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        command, cwd=APP, check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=process_env,
    )


def validate_target(value: str, mode: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ReleaseError(f"unsafe target path: {value}")
    if mode in DIAGNOSTIC_MODES:
        raise ReleaseError("diagnostic mode does not accept payload targets")
    if mode in RECEIVER_MODES:
        if path.as_posix() != RECEIVER_PAYLOAD:
            raise ReleaseError(f"receiver update target is not allowed: {value}")
        return path
    if mode in APK_MODES:
        if path.parts[:2] != ("media", "apk") or len(path.parts) != 3:
            raise ReleaseError(f"APK release target is not allowed: {value}")
        if path.suffix not in {".apk", ".json"}:
            raise ReleaseError(f"APK release file type is not allowed: {value}")
    elif mode in DATA_MODES:
        if path.parts[:2] != ("deploy", "data_updates"):
            raise ReleaseError(f"data update target is not allowed: {value}")
        if path.suffix not in {".py", ".json", ".csv", ".xlsx"}:
            raise ReleaseError(f"data update file type is not allowed: {value}")
    else:
        if "migrations" in path.parts and mode not in MIGRATION_MODES:
            raise ReleaseError(f"database migration is not allowed in {mode}: {value}")
        if len(path.parts) == 1:
            if value not in ALLOWED_ROOT_FILES:
                raise ReleaseError(f"root file is not allowlisted: {value}")
        elif path.parts[0] not in ALLOWED_TOP_LEVEL:
            raise ReleaseError(f"top-level directory is not allowlisted: {value}")
    resolved = (APP / Path(*path.parts)).resolve()
    if APP.resolve() not in resolved.parents:
        raise ReleaseError(f"target escapes application root: {value}")
    return path


def read_package() -> Path:
    handle = tempfile.NamedTemporaryFile(prefix="accounting-release-", suffix=".tar.gz", delete=False)
    total = 0
    try:
        while True:
            chunk = sys.stdin.buffer.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PACKAGE_BYTES:
                raise ReleaseError("release package exceeds 150 MiB")
            handle.write(chunk)
    finally:
        handle.close()
    if total == 0:
        Path(handle.name).unlink(missing_ok=True)
        raise ReleaseError("empty release package")
    return Path(handle.name)


def load_release(package: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    with tarfile.open(package, "r:gz") as archive:
        members = archive.getmembers()
        if any(not member.isfile() for member in members):
            raise ReleaseError("release archive may contain regular files only")
        names = [member.name for member in members]
        if names.count("release-manifest.json") != 1:
            raise ReleaseError("release manifest is missing or duplicated")
        manifest_file = archive.extractfile("release-manifest.json")
        if manifest_file is None:
            raise ReleaseError("release manifest cannot be read")
        manifest = json.loads(manifest_file.read().decode("utf-8"))
        if manifest.get("schema") != 2:
            raise ReleaseError("unsupported release schema")
        mode = manifest.get("mode")
        if mode not in ALL_MODES:
            raise ReleaseError("unsupported release mode")
        commit = manifest.get("commit")
        if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise ReleaseError("invalid release commit")
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            raise ReleaseError("invalid release metadata")
        entries = manifest.get("files")
        if not isinstance(entries, list) or len(entries) > 750:
            raise ReleaseError("invalid release file list")
        if mode not in {"rollback", "diagnose"} and not entries:
            raise ReleaseError("release file list is empty")
        if mode in {"rollback", "diagnose"} and entries:
            raise ReleaseError(f"{mode} package cannot contain files")
        payload: dict[str, bytes] = {}
        expected_names = {"release-manifest.json"}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ReleaseError("invalid release entry")
            path = validate_target(str(entry.get("path", "")), mode)
            target = path.as_posix()
            archive_name = f"payload/{target}"
            expected_names.add(archive_name)
            if target in payload:
                raise ReleaseError(f"duplicate release target: {target}")
            extracted = archive.extractfile(archive_name)
            if extracted is None:
                raise ReleaseError(f"payload is missing: {target}")
            data = extracted.read()
            if len(data) != entry.get("size") or digest(data) != entry.get("sha256"):
                raise ReleaseError(f"payload checksum mismatch: {target}")
            payload[target] = data
        if set(names) != expected_names:
            raise ReleaseError("release archive contains undeclared files")
        validate_mode_contract(manifest, payload)
        return manifest, payload


def validate_mode_contract(manifest: dict[str, Any], payload: dict[str, bytes]) -> None:
    mode = manifest["mode"]
    metadata = manifest["metadata"]
    if mode in APK_MODES:
        profile = metadata.get("apk_profile")
        if profile not in {"driver", "excavator"}:
            raise ReleaseError("invalid APK profile")
        expected_manifest = f"media/apk/{profile}-update.json"
        apk_files = [path for path in payload if path.endswith(".apk")]
        if expected_manifest not in payload or len(apk_files) != 1 or len(payload) != 2:
            raise ReleaseError("APK release must contain one APK and its role manifest")
        validate_apk_payload(profile, apk_files[0], payload)
    elif mode in DATA_MODES:
        operation = metadata.get("operation")
        if not isinstance(operation, str) or operation not in payload or not operation.endswith(".py"):
            raise ReleaseError("data operation script is missing from the package")
    elif mode in RECEIVER_MODES:
        if set(payload) != {RECEIVER_PAYLOAD}:
            raise ReleaseError("receiver release must contain exactly one receiver file")
        try:
            compile(payload[RECEIVER_PAYLOAD], RECEIVER_PAYLOAD, "exec")
        except SyntaxError as exc:
            raise ReleaseError("receiver source is not valid Python") from exc
    elif mode in DIAGNOSTIC_MODES:
        if payload:
            raise ReleaseError("diagnostic package cannot contain payload files")
        validate_diagnostic_metadata(metadata)
    elif mode == "rollback":
        rollback_id = metadata.get("rollback_id")
        if not isinstance(rollback_id, str) or not rollback_id.startswith("github-"):
            raise ReleaseError("invalid rollback id")


def validate_apk_payload(profile: str, apk_target: str, payload: dict[str, bytes]) -> dict[str, Any]:
    manifest_target = f"media/apk/{profile}-update.json"
    try:
        update = json.loads(payload[manifest_target].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("APK update manifest is invalid") from exc
    if update.get("schemaVersion") != 1 or update.get("profile") != profile:
        raise ReleaseError("APK update manifest contract mismatch")
    version_code = update.get("versionCode")
    version_name = update.get("versionName")
    if not isinstance(version_code, int) or version_code <= 0 or not isinstance(version_name, str):
        raise ReleaseError("APK version is invalid")
    parsed = urlparse(str(update.get("apkUrl", "")))
    expected_name = f"{profile}-{version_name}.apk"
    if parsed.scheme != "https" or parsed.netloc != "driverform.ru" or Path(parsed.path).name != expected_name:
        raise ReleaseError("APK URL does not match the production contract")
    if apk_target != f"media/apk/{expected_name}":
        raise ReleaseError("APK target does not match versionName")
    apk_data = payload[apk_target]
    if digest(apk_data) != update.get("sha256") or not apk_data.startswith(b"PK"):
        raise ReleaseError("APK content does not match update manifest")
    return update


def write_atomic(target: Path, data: bytes, uid: int, gid: int, mode: int = 0o664) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chown(target.parent, uid, gid)
    os.chmod(target.parent, 0o755)
    temporary = target.with_name(f".{target.name}.github-deploy-{os.getpid()}")
    temporary.write_bytes(data)
    os.chown(temporary, uid, gid)
    os.chmod(temporary, mode)
    os.replace(temporary, target)


def wait_for_service() -> None:
    for _ in range(45):
        active = run(["systemctl", "is-active", "accounting-mvp"], check=False)
        if active.returncode == 0:
            response = run([
                "curl", "--silent", "--output", "/dev/null", "--write-out", "%{http_code}",
                "--unix-socket", "/run/accounting-mvp/accounting-mvp.sock", "http://localhost/",
            ], check=False)
            code = response.stdout.strip()
            if code.startswith(("2", "3")) or code == "403":
                return
        time.sleep(1)
    raise ReleaseError("application readiness check failed")


def new_backup(manifest: dict[str, Any], payload: dict[str, bytes]) -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup = BACKUPS / f"github-{stamp}-{manifest['commit'][:12]}-{manifest['mode']}-before"
    backup.mkdir(mode=0o750)
    existing: list[str] = []
    created: list[str] = []
    for relative in payload:
        source = APP / Path(*PurePosixPath(relative).parts)
        if source.exists():
            destination = backup / "files" / Path(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            existing.append(relative)
        else:
            created.append(relative)
    (backup / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (backup / "existing.json").write_text(json.dumps(existing) + "\n", encoding="utf-8")
    (backup / "created.json").write_text(json.dumps(created) + "\n", encoding="utf-8")
    return backup


def database_settings() -> dict[str, str]:
    script = (
        "import json, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
        "import django; django.setup(); from django.conf import settings; "
        "d=settings.DATABASES['default']; "
        "print(json.dumps({k:str(d.get(k) or '') for k in ('NAME','USER','PASSWORD','HOST','PORT')}))"
    )
    result = run([str(APP / ".venv/bin/python"), "-c", script])
    try:
        config = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ReleaseError("cannot load production database settings") from exc
    if not config.get("NAME") or not config.get("USER"):
        raise ReleaseError("production database settings are incomplete")
    config["HOST"] = config.get("HOST") or "localhost"
    config["PORT"] = config.get("PORT") or "5432"
    return config


def pg_args(config: dict[str, str]) -> list[str]:
    return ["--host", config["HOST"], "--port", config["PORT"], "--username", config["USER"]]


def backup_database(backup: Path) -> Path:
    config = database_settings()
    target = backup / "database.dump"
    result = run(["pg_dump", *pg_args(config), "--format=custom", "--file", str(target), config["NAME"]], env={"PGPASSWORD": config.get("PASSWORD", "")})
    print(result.stdout, end="")
    if not target.is_file() or target.stat().st_size == 0:
        raise ReleaseError("PostgreSQL backup was not created")
    run(["pg_restore", "--list", str(target)])
    (backup / "database.sha256").write_text(f"{digest(target.read_bytes())}  database.dump\n")
    return target


def restore_database(backup: Path) -> None:
    source = backup / "database.dump"
    if not source.is_file():
        raise ReleaseError("rollback point has no database dump")
    config = database_settings()
    result = run([
        "pg_restore", *pg_args(config), "--dbname", config["NAME"], "--clean", "--if-exists",
        "--no-owner", "--no-privileges", "--exit-on-error", str(source),
    ], env={"PGPASSWORD": config.get("PASSWORD", "")})
    print(result.stdout, end="")


def file_owner() -> tuple[int, int]:
    return pwd.getpwnam("deploy").pw_uid, grp.getgrnam("www-data").gr_gid


def install_payload(payload: dict[str, bytes]) -> None:
    uid, gid = file_owner()
    for relative, data in payload.items():
        mode = 0o775 if relative.startswith("deploy/data_updates/") and relative.endswith(".py") else 0o664
        write_atomic(APP / Path(*PurePosixPath(relative).parts), data, uid, gid, mode)


def restore_files(backup: Path) -> None:
    uid, gid = file_owner()
    existing = json.loads((backup / "existing.json").read_text(encoding="utf-8"))
    created = json.loads((backup / "created.json").read_text(encoding="utf-8"))
    for relative in existing:
        saved = backup / "files" / Path(*PurePosixPath(relative).parts)
        write_atomic(APP / Path(*PurePosixPath(relative).parts), saved.read_bytes(), uid, gid)
    for relative in created:
        (APP / Path(*PurePosixPath(relative).parts)).unlink(missing_ok=True)


def finish_application_release(*, run_migrations: bool) -> None:
    for command in (
        [str(APP / ".venv/bin/python"), "manage.py", "check"],
        [str(APP / ".venv/bin/python"), "manage.py", "makemigrations", "--check", "--dry-run"],
        [str(APP / ".venv/bin/python"), "manage.py", "migrate", "--plan"],
    ):
        result = run(command)
        print(result.stdout, end="")
    if run_migrations:
        result = run([str(APP / ".venv/bin/python"), "manage.py", "migrate", "--noinput"])
        print(result.stdout, end="")
    result = run([str(APP / ".venv/bin/python"), "manage.py", "collectstatic", "--noinput"])
    print(result.stdout, end="")
    result = run(["nginx", "-t"])
    print(result.stdout, end="")
    run(["systemctl", "restart", "accounting-mvp"])
    wait_for_service()


def deploy_code(manifest: dict[str, Any], payload: dict[str, bytes], *, migrations: bool) -> Path:
    backup = new_backup(manifest, payload)
    if migrations:
        backup_database(backup)
        run(["systemctl", "stop", "accounting-mvp"])
    try:
        install_payload(payload)
        finish_application_release(run_migrations=migrations)
    except Exception:
        run(["systemctl", "stop", "accounting-mvp"], check=False)
        restore_files(backup)
        if migrations:
            restore_database(backup)
        run([str(APP / ".venv/bin/python"), "manage.py", "collectstatic", "--noinput"], check=False)
        run(["systemctl", "restart", "accounting-mvp"], check=False)
        raise
    return backup


def publish_apk(manifest: dict[str, Any], payload: dict[str, bytes]) -> Path:
    profile = manifest["metadata"]["apk_profile"]
    apk_target = next(path for path in payload if path.endswith(".apk"))
    update = validate_apk_payload(profile, apk_target, payload)
    current_path = APP / "media" / "apk" / f"{profile}-update.json"
    if current_path.is_file():
        current = json.loads(current_path.read_text(encoding="utf-8"))
        if int(update["versionCode"]) <= int(current.get("versionCode", 0)):
            raise ReleaseError("APK versionCode must be greater than the published version")
    apk_path = APP / Path(*PurePosixPath(apk_target).parts)
    if apk_path.exists() and digest(apk_path.read_bytes()) != update["sha256"]:
        raise ReleaseError("versioned APK path already exists with different content")
    backup = new_backup(manifest, payload)
    uid, gid = file_owner()
    write_atomic(apk_path, payload[apk_target], uid, gid)
    write_atomic(current_path, payload[f"media/apk/{profile}-update.json"], uid, gid)
    return backup


def apply_data(manifest: dict[str, Any], payload: dict[str, bytes]) -> Path:
    backup = new_backup(manifest, payload)
    try:
        install_payload(payload)
        operation = APP / Path(*PurePosixPath(manifest["metadata"]["operation"]).parts)
        dry_run = run([str(APP / ".venv/bin/python"), str(operation), "--dry-run"])
        print(dry_run.stdout, end="")
        backup_database(backup)
        run(["systemctl", "stop", "accounting-mvp"])
        applied = run([str(APP / ".venv/bin/python"), str(operation), "--apply"])
        print(applied.stdout, end="")
        check = run([str(APP / ".venv/bin/python"), "manage.py", "check"])
        print(check.stdout, end="")
        run(["systemctl", "start", "accounting-mvp"])
        wait_for_service()
    except Exception:
        if (backup / "database.dump").is_file():
            restore_database(backup)
        restore_files(backup)
        run(["systemctl", "restart", "accounting-mvp"], check=False)
        raise
    return backup


def run_diagnostic(manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = validate_diagnostic_metadata(manifest["metadata"])
    if not DIAGNOSTIC_QUERY_SOURCE.strip():
        raise ReleaseError("diagnostic helper is unavailable")
    diagnostic_env = os.environ.copy()
    diagnostic_env.update({
        "HOME": str(APP),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    })
    diagnostic_env["PGOPTIONS"] = (
        diagnostic_env.get("PGOPTIONS", "")
        + " -c default_transaction_read_only=on -c statement_timeout=12000 -c lock_timeout=2000"
    ).strip()
    command = [
        str(APP / ".venv/bin/python"),
        "-c",
        DIAGNOSTIC_QUERY_SOURCE,
        str(metadata["operation"]),
        str(metadata["equipment"]),
        str(metadata["from_utc"]),
        str(metadata["to_utc"]),
        str(metadata["max_rows"]),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=APP,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DIAGNOSTIC_PROCESS_TIMEOUT_SECONDS,
            close_fds=True,
            start_new_session=True,
            user=DIAGNOSTIC_OS_USER,
            group=DIAGNOSTIC_OS_GROUP,
            extra_groups=(),
            umask=0o077,
            env=diagnostic_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError("diagnostic process timed out") from exc
    if result.returncode != 0:
        error_reference = digest(result.stderr or result.stdout)[:16]
        raise ReleaseError(
            f"diagnostic helper failed with code {result.returncode}; reference={error_reference}"
        )
    return validate_diagnostic_report(result.stdout, metadata)


def update_receiver(manifest: dict[str, Any], payload: dict[str, bytes]) -> Path:
    source = payload[RECEIVER_PAYLOAD]
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup = RECEIVER_PATH.with_name(
        f"{RECEIVER_PATH.name}.{stamp}-{manifest['commit'][:12]}-before"
    )
    if not RECEIVER_PATH.is_file():
        raise ReleaseError("installed receiver does not exist")
    shutil.copy2(RECEIVER_PATH, backup)
    os.chown(backup, 0, 0)
    os.chmod(backup, 0o755)
    write_atomic(RECEIVER_PATH, source, 0, 0, 0o755)
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(RECEIVER_PATH)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        shutil.copy2(backup, RECEIVER_PATH)
        os.chown(RECEIVER_PATH, 0, 0)
        os.chmod(RECEIVER_PATH, 0o755)
        raise ReleaseError(f"receiver verification failed: {result.stdout.strip()}")
    return backup


def rollback(manifest: dict[str, Any]) -> Path:
    rollback_id = manifest["metadata"]["rollback_id"]
    backup = (BACKUPS / rollback_id).resolve()
    if backup.parent != BACKUPS.resolve() or not backup.is_dir():
        raise ReleaseError("rollback point does not exist")
    run(["systemctl", "stop", "accounting-mvp"])
    if (backup / "database.dump").is_file():
        restore_database(backup)
    restore_files(backup)
    run([str(APP / ".venv/bin/python"), "manage.py", "collectstatic", "--noinput"])
    run(["systemctl", "restart", "accounting-mvp"])
    wait_for_service()
    return backup


def main() -> int:
    if fcntl is None or grp is None or pwd is None:
        raise SystemExit("the production receiver requires POSIX file locking")
    package = read_package()
    try:
        manifest, payload = load_release(package)
        mode = manifest["mode"]
        with LOCK.open("w") as lock_handle:
            lock_flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if mode == "diagnose" else 0)
            try:
                fcntl.flock(lock_handle, lock_flags)
            except BlockingIOError as exc:
                raise ReleaseError("production receiver is busy") from exc
            package_sha = digest(package.read_bytes())
            if mode == "diagnose":
                report = run_diagnostic(manifest)
                envelope = encrypt_diagnostic_report(report)
                print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
                return 0
            if mode in VERIFY_MODES:
                print(f"VERIFY_OK mode={mode} commit={manifest['commit']} files={len(payload)} package_sha256={package_sha}")
                return 0
            if mode == "deploy":
                backup = deploy_code(manifest, payload, migrations=False)
            elif mode == "deploy_migrations":
                backup = deploy_code(manifest, payload, migrations=True)
            elif mode == "publish_apk":
                backup = publish_apk(manifest, payload)
            elif mode == "apply_data":
                backup = apply_data(manifest, payload)
            elif mode == "update_receiver":
                backup = update_receiver(manifest, payload)
            elif mode == "rollback":
                backup = rollback(manifest)
            else:
                raise ReleaseError(f"unsupported executable mode: {mode}")
            print(f"RELEASE_OK mode={mode} commit={manifest['commit']} files={len(payload)} backup={backup}")
            return 0
    except Exception as exc:
        print(f"RELEASE_FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        package.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
