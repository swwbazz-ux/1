#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
import re
import signal
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
from urllib.parse import urlparse
import zipfile

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
FCM_MODES = {"verify_fcm", "configure_fcm"}
DIAGNOSTIC_MODES = {"diagnose"}
SSE_QA_SEED_FIX_MODES = {"repair_sse_qa_seed"}
SSE_QA_MODES = {
    "verify_sse_qa", "prepare_sse_qa_host_key", "install_sse_qa",
    "inspect_sse_qa_https", "prepare_sse_qa_https",
    "enable_sse_qa", "smoke_sse_qa", "disable_sse_qa", "remove_sse_qa",
} | SSE_QA_SEED_FIX_MODES
ALL_MODES = CODE_MODES | MIGRATION_MODES | APK_MODES | DATA_MODES | RECEIVER_MODES | FCM_MODES | DIAGNOSTIC_MODES | SSE_QA_MODES | {"rollback"}
VERIFY_MODES = {"verify", "verify_migrations", "verify_apk", "verify_data", "verify_receiver", "verify_fcm"}
RECEIVER_PAYLOAD = "deploy/receiver/accounting_github_deploy_receiver.py"
RECEIVER_PATH = Path("/usr/local/sbin/accounting-github-deploy-receiver")
FCM_PAYLOAD = "deploy/secrets/firebase-service-account.json"
FCM_CONFIG_PATH = Path("/etc/accounting-mvp/firebase-service-account.json")
SSE_QA_PACKAGE_PAYLOAD = "deploy/sse-qa/package.zip"
SSE_QA_SECRETS_PAYLOAD = "deploy/sse-qa/secrets.json"
SSE_QA_ALLOW_CIDR_PAYLOAD = "deploy/sse-qa/allow-cidr.txt"
SSE_QA_SEED_FIX_BASE_CONTROLLER_PAYLOAD = "deploy/sse-qa-seed-fix/scripts/sse_qa_ctl.py"
SSE_QA_SEED_FIX_CONTROLLER_PAYLOAD = "deploy/sse-qa-seed-fix/scripts/sse_qa_seed_fix_ctl.py"
SSE_QA_SEED_FIX_DB_HELPER_PAYLOAD = "deploy/sse-qa-seed-fix/scripts/sse_qa_seed_fix_db.py"
SSE_QA_SEED_COMMAND_PAYLOAD = "deploy/sse-qa-seed-fix/payload/seed_sse_qa.py"
SSE_QA_SEED_TEST_PAYLOAD = "deploy/sse-qa-seed-fix/payload/test_sse_qa_seed.py"
SSE_QA_SEED_FIX_PAYLOADS = {
    SSE_QA_SEED_FIX_BASE_CONTROLLER_PAYLOAD,
    SSE_QA_SEED_FIX_CONTROLLER_PAYLOAD,
    SSE_QA_SEED_FIX_DB_HELPER_PAYLOAD,
    SSE_QA_SEED_COMMAND_PAYLOAD,
    SSE_QA_SEED_TEST_PAYLOAD,
}
SSE_QA_MAX_PACKAGE_BYTES = 140 * 1024 * 1024
SSE_QA_MAX_MEMBERS = 2500
SSE_QA_MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
SSE_QA_SLICE_UNIT = "sse-qa.slice"
SSE_QA_SLICE_CGROUP = "/sse.slice/sse-qa.slice"
SSE_QA_RUNTIME_SLICE_PATH = Path("/run/systemd/system/sse-qa.slice")
SSE_QA_PERSISTENT_SLICE_PATH = Path("/etc/systemd/system/sse-qa.slice")
SSE_QA_CANDIDATE_COMMIT = "9d336723f3dc2fc574937a57602a27b54c54fd77"
SSE_QA_CONTROLLER_SHA256 = "3e3ee8af9b2877bb93a7487f89a832834331a647d87f721180fe4b2ae8c2ea44"
SSE_QA_RUNTIME_SHA256 = "8717926a7c9d437e96e76243ce9bd2c14acf45b6a8fa325f08e885d9a296366e"
SSE_QA_HTTPS_CONTROLLER_SHA256 = "a70916ea7e39d234cbd2fd232eccdeb79c632f69f6cc01fef4fcb2978746a722"
SSE_QA_SEED_FIX_VERSION = "C2 + seed-fix"
SSE_QA_SEED_FIX_CONTROLLER_SHA256 = "f17e2b143b99c5b0970d9b408eae9d2dbbc050e5b48546ba3449bb7678b4f7c2"
SSE_QA_SEED_FIX_DB_HELPER_SHA256 = "2e25eabe333c99178caab70141711ba580abf0d6569c4693bc0afaa4c16f86da"
SSE_QA_SEED_COMMAND_SHA256 = "0bf8580308073684e1e1ae4cce024620e36179f75c920feb18e5d6e16e98326c"
SSE_QA_SEED_TEST_SHA256 = "26288fad768c1ea00383d9cfed686d1f4ab6aa9ef1eaebaff99404a955531236"
SSE_QA_SEED_FIX_PAYLOAD_SHA256 = {
    SSE_QA_SEED_FIX_BASE_CONTROLLER_PAYLOAD: SSE_QA_CONTROLLER_SHA256,
    SSE_QA_SEED_FIX_CONTROLLER_PAYLOAD: SSE_QA_SEED_FIX_CONTROLLER_SHA256,
    SSE_QA_SEED_FIX_DB_HELPER_PAYLOAD: SSE_QA_SEED_FIX_DB_HELPER_SHA256,
    SSE_QA_SEED_COMMAND_PAYLOAD: SSE_QA_SEED_COMMAND_SHA256,
    SSE_QA_SEED_TEST_PAYLOAD: SSE_QA_SEED_TEST_SHA256,
}
SSE_QA_METADATA = {
    "qa_schema": 2,
    "candidate_commit": SSE_QA_CANDIDATE_COMMIT,
    "controller_sha256": SSE_QA_CONTROLLER_SHA256,
    "runtime_sha256": SSE_QA_RUNTIME_SHA256,
}
SSE_QA_HTTPS_METADATA = {
    **SSE_QA_METADATA,
    "qa_https_schema": 1,
    "https_controller_sha256": SSE_QA_HTTPS_CONTROLLER_SHA256,
}
SSE_QA_SEED_FIX_METADATA = {
    **SSE_QA_METADATA,
    "seed_fix_schema": 1,
    "seed_fix_version": SSE_QA_SEED_FIX_VERSION,
    "seed_fix_controller_sha256": SSE_QA_SEED_FIX_CONTROLLER_SHA256,
    "seed_fix_db_helper_sha256": SSE_QA_SEED_FIX_DB_HELPER_SHA256,
    "seed_command_sha256": SSE_QA_SEED_COMMAND_SHA256,
    "seed_test_sha256": SSE_QA_SEED_TEST_SHA256,
}
SSE_QA_SEED_FIX_OVERLAY = {
    "schema": "SSE_QA_SEED_FIX_V1",
    "version": SSE_QA_SEED_FIX_VERSION,
    "controller_sha256": SSE_QA_SEED_FIX_CONTROLLER_SHA256,
    "db_helper_sha256": SSE_QA_SEED_FIX_DB_HELPER_SHA256,
    "seed_sha256": SSE_QA_SEED_COMMAND_SHA256,
    "test_sha256": SSE_QA_SEED_TEST_SHA256,
}
SSE_QA_OWNERSHIP_PATH = Path("/var/lib/sse-qa/OWNERSHIP.json")
SSE_QA_INSTALLED_SEED_PATH = Path(
    "/srv/sse-qa/releases/r3/backend/users/management/commands/seed_sse_qa.py"
)
SSE_QA_INSTALLED_SEED_TEST_PATH = Path(
    "/srv/sse-qa/releases/r3/backend/users/test_sse_qa_seed.py"
)
SSE_QA_INSTALLED_SEED_LOGICAL = "/srv/sse-qa/releases/r3/backend/users/management/commands/seed_sse_qa.py"
SSE_QA_INSTALLED_SEED_TEST_LOGICAL = "/srv/sse-qa/releases/r3/backend/users/test_sse_qa_seed.py"
SSE_QA_SEED_FIX_SUMMARY = re.compile(
    r"^SSE_QA_SEED_FIX_OK version=C2\+seed-fix "
    r"action=(applied|already_applied) "
    r"source=(updated|already_fixed) database=(updated|already_fixed) "
    r"history=preserved access=preserved qa=disabled$"
)
APP_ENV_PATH = APP / ".env"
DIAGNOSTIC_OPERATIONS = {
    "trip_accounting_incident_v1",
    "infra_capacity_v1",
    "sse_qa_http_503_v1",
}
TRIP_DIAGNOSTIC_METADATA_KEYS = {"operation", "equipment", "from_utc", "to_utc", "max_rows"}
INFRA_DIAGNOSTIC_METADATA_KEYS = {"operation"}
SSE_QA_HTTP_503_METADATA_KEYS = {"operation"}
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
SSE_QA_HTTP_503_WINDOW_FROM = "2026-10-04T07:09:30Z"
SSE_QA_HTTP_503_WINDOW_TO = "2026-10-04T07:10:05Z"
SSE_QA_HTTP_503_TARGET_AROUND = "2026-10-04T07:09:52Z"
SSE_QA_HTTP_503_ERROR_LOG = Path("/srv/sse-qa/log/nginx-error.log")
SSE_QA_HTTP_503_ACCESS_LOG = Path("/srv/sse-qa/log/nginx-access.log")
SSE_QA_HTTP_503_NGINX_SITE = Path("/etc/sse-qa/nginx.conf")
SSE_QA_HTTP_503_WSGI_UNIT = "sse-qa-wsgi.service"
SSE_QA_HTTP_503_MAX_LOG_BYTES = 16 * 1024 * 1024
SSE_QA_HTTP_503_MAX_CONFIG_BYTES = 128 * 1024
SSE_QA_HTTP_503_MAX_RECORDS = 240
SSE_QA_HTTP_503_MAX_JOURNAL_BYTES = 256 * 1024
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


INFRA_CAPACITY_SOURCE = r'''
import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


APP_ROOT = Path("/srv/accounting-mvp")
SYSTEMCTL = Path("/usr/bin/systemctl")
SERVICE_ALLOWLIST = (
    ("accounting_mvp", "accounting-mvp.service"),
    ("nginx", "nginx.service"),
    ("postgresql", "postgresql.service"),
    ("redis_server", "redis-server.service"),
)
SERVICE_PROPERTIES = (
    "LoadState", "ActiveState", "SubState", "MainPID", "NRestarts",
    "CPUUsageNSec", "MemoryCurrent", "MemoryPeak", "TasksCurrent",
    "TasksMax", "LimitNOFILE",
)
REDIS_PORTS = (6379, 6381)
LIMITATIONS = (
    "snapshot_not_capacity_baseline",
    "no_historical_normal_or_peak_window",
    "no_application_event_loop_probe",
    "no_nginx_effective_config_or_routes",
    "no_redis_acl_or_channel_names",
)
SAFE_STATE = re.compile(r"[a-z0-9_.@-]{1,48}\Z")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path, maximum=65536):
    try:
        value = Path(path).read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None
    return value if len(value) <= maximum else None


def integer(value):
    if value in (None, "", "infinity", "[not set]"):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def safe_state(value, fallback="unknown"):
    value = str(value or "").strip().lower()
    return value if SAFE_STATE.fullmatch(value) else fallback


def cpu_totals():
    text = read_text("/proc/stat", 16384)
    if not text:
        raise RuntimeError("cpu counters unavailable")
    fields = text.splitlines()[0].split()
    if not fields or fields[0] != "cpu" or len(fields) < 9:
        raise RuntimeError("cpu counters invalid")
    values = [int(value) for value in fields[1:]]
    # Linux user/nice already include guest/guest_nice, so fields 9-10 must
    # not be summed again.  iowait is treated as idle; steal remains busy.
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values[:8]), idle


def cpu_samples():
    samples = []
    total_before, idle_before = cpu_totals()
    for _ in range(5):
        time.sleep(1)
        total_after, idle_after = cpu_totals()
        total_delta = total_after - total_before
        idle_delta = idle_after - idle_before
        if total_delta <= 0 or idle_delta < 0:
            raise RuntimeError("cpu counters did not advance")
        samples.append(round(100.0 * (total_delta - idle_delta) / total_delta, 3))
        total_before, idle_before = total_after, idle_after
    return samples


def memory_bytes():
    text = read_text("/proc/meminfo", 65536)
    if not text:
        raise RuntimeError("memory counters unavailable")
    values = {}
    for line in text.splitlines():
        key, separator, tail = line.partition(":")
        if not separator:
            continue
        token = tail.strip().split()[0]
        if token.isdigit():
            values[key] = int(token) * 1024
    required = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
    if any(key not in values for key in required):
        raise RuntimeError("memory counters invalid")
    return {
        "total": values["MemTotal"],
        "available": values["MemAvailable"],
        "swap_total": values["SwapTotal"],
        "swap_free": values["SwapFree"],
    }


def pressure(kind):
    text = read_text(f"/proc/pressure/{kind}", 4096)
    if not text:
        return None
    line = next((item for item in text.splitlines() if item.startswith("some ")), None)
    if not line:
        return None
    values = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
    try:
        return {
            "avg10": float(values["avg10"]),
            "avg60": float(values["avg60"]),
            "avg300": float(values["avg300"]),
            "total_us": int(values["total"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def file_handles():
    text = read_text("/proc/sys/fs/file-nr", 256)
    if not text:
        return {"allocated": None, "maximum": None}
    fields = text.split()
    if len(fields) != 3:
        return {"allocated": None, "maximum": None}
    return {"allocated": integer(fields[0]), "maximum": integer(fields[2])}


def disk_bytes(path):
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def service_metrics(service):
    result = {
        "load_state": "unknown", "active_state": "unknown", "sub_state": "unknown",
        "main_pid": 0, "restarts": None, "cpu_usage_ns": None,
        "memory_current_bytes": None, "memory_peak_bytes": None,
        "tasks_current": None, "tasks_max": None, "limit_nofile": None,
    }
    if not SYSTEMCTL.is_file():
        return result
    try:
        completed = subprocess.run(
            [str(SYSTEMCTL), "show", service, "--no-pager", "--property=" + ",".join(SERVICE_PROPERTIES)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return result
    if completed.returncode not in (0, 1) or len(completed.stdout) > 16384:
        return result
    values = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    result.update({
        "load_state": safe_state(values.get("LoadState")),
        "active_state": safe_state(values.get("ActiveState")),
        "sub_state": safe_state(values.get("SubState")),
        "main_pid": integer(values.get("MainPID")) or 0,
        "restarts": integer(values.get("NRestarts")),
        "cpu_usage_ns": integer(values.get("CPUUsageNSec")),
        "memory_current_bytes": integer(values.get("MemoryCurrent")),
        "memory_peak_bytes": integer(values.get("MemoryPeak")),
        "tasks_current": integer(values.get("TasksCurrent")),
        "tasks_max": integer(values.get("TasksMax")),
        "limit_nofile": integer(values.get("LimitNOFILE")),
    })
    return result


POSTGRES_ACTIVITY_COUNT_SQL = """
    SELECT COUNT(*),
           COUNT(*) FILTER (WHERE backend_type = 'client backend'),
           COUNT(*) FILTER (WHERE backend_type IS NULL),
           COUNT(*) FILTER (WHERE datname = current_database()),
           COUNT(*) FILTER (
               WHERE datname = current_database() AND backend_type = 'client backend'
           ),
           COUNT(*) FILTER (
               WHERE datname = current_database() AND backend_type IS NULL
           ),
           COUNT(*) FILTER (
               WHERE datname = current_database() AND backend_type IS NOT NULL
                 AND backend_type <> 'client backend'
           ),
           COUNT(*) FILTER (
               WHERE backend_type = 'client backend'
                 AND datname = current_database() AND state IS NOT NULL
                 AND state <> 'disabled'
           ),
           COUNT(*) FILTER (
               WHERE backend_type = 'client backend'
                 AND datname = current_database() AND state IS NULL
           ),
           COUNT(*) FILTER (
               WHERE backend_type = 'client backend'
                 AND datname = current_database() AND state = 'disabled'
           ),
           COUNT(*) FILTER (
               WHERE backend_type = 'client backend'
                 AND datname = current_database() AND state = 'active'
           ),
           COUNT(*) FILTER (
               WHERE backend_type = 'client backend'
                 AND datname = current_database() AND state = 'idle in transaction'
           ),
           COUNT(*) FILTER (
               WHERE backend_type = 'client backend'
                 AND datname = current_database() AND state IS NOT NULL
                 AND wait_event_type = 'Lock'
           )
    FROM pg_stat_activity
"""


def postgres_metrics():
    keys = (
        "server_version_num", "transaction_read_only", "transaction_isolation",
        "max_connections", "reserved_connections_supported", "reserved_connections",
        "superuser_reserved_connections", "role_connection_limit",
        "server_process_rows", "observed_client_backend_connections",
        "rows_with_unknown_backend_type", "client_backend_count_complete",
        "database_process_rows", "observed_database_client_connections",
        "database_rows_with_unknown_backend_type",
        "database_rows_with_known_nonclient_backend_type",
        "database_client_backend_count_complete",
        "observed_database_client_connections_with_visible_details",
        "observed_database_client_connections_with_hidden_details",
        "observed_database_client_connections_with_disabled_tracking",
        "activity_details_visibility",
        "visible_active_database_client_connections",
        "visible_idle_in_transaction_database_client_connections",
        "visible_lock_waiting_database_client_connections", "locks", "ungranted_locks",
        "database_size_bytes", "visible_oldest_transaction_seconds",
    )
    result = {"status": "error", **{key: None for key in keys}}
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django
        django.setup()
        from django.db import connection, transaction
        if connection.vendor != "postgresql":
            result["status"] = "unsupported"
            return result
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '3000ms'")
                cursor.execute("SET LOCAL lock_timeout = '1000ms'")
                cursor.execute("SHOW transaction_read_only")
                result["transaction_read_only"] = cursor.fetchone()[0] == "on"
                cursor.execute("SHOW transaction_isolation")
                result["transaction_isolation"] = safe_state(cursor.fetchone()[0].replace(" ", "_"))
                cursor.execute("SHOW server_version_num")
                result["server_version_num"] = int(cursor.fetchone()[0])
                cursor.execute("SHOW max_connections")
                result["max_connections"] = int(cursor.fetchone()[0])
                if result["server_version_num"] >= 160000:
                    cursor.execute("SHOW reserved_connections")
                    result["reserved_connections"] = int(cursor.fetchone()[0])
                    result["reserved_connections_supported"] = True
                else:
                    result["reserved_connections"] = None
                    result["reserved_connections_supported"] = False
                cursor.execute("SHOW superuser_reserved_connections")
                result["superuser_reserved_connections"] = int(cursor.fetchone()[0])
                cursor.execute("SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user")
                result["role_connection_limit"] = int(cursor.fetchone()[0])
                cursor.execute(POSTGRES_ACTIVITY_COUNT_SQL)
                row = cursor.fetchone()
                result.update({
                    "server_process_rows": int(row[0]),
                    "observed_client_backend_connections": int(row[1]),
                    "rows_with_unknown_backend_type": int(row[2]),
                    "client_backend_count_complete": int(row[2]) == 0,
                    "database_process_rows": int(row[3]),
                    "observed_database_client_connections": int(row[4]),
                    "database_rows_with_unknown_backend_type": int(row[5]),
                    "database_rows_with_known_nonclient_backend_type": int(row[6]),
                    "database_client_backend_count_complete": int(row[5]) == 0,
                    "observed_database_client_connections_with_visible_details": int(row[7]),
                    "observed_database_client_connections_with_hidden_details": int(row[8]),
                    "observed_database_client_connections_with_disabled_tracking": int(row[9]),
                    "activity_details_visibility": "partial" if int(row[5]) or int(row[8]) or int(row[9]) else "full",
                    "visible_active_database_client_connections": int(row[10]),
                    "visible_idle_in_transaction_database_client_connections": int(row[11]),
                    "visible_lock_waiting_database_client_connections": int(row[12]),
                })
                cursor.execute("""
                    SELECT COALESCE(MAX(EXTRACT(EPOCH FROM clock_timestamp() - xact_start)), 0)
                    FROM pg_stat_activity
                    WHERE backend_type = 'client backend'
                      AND datname = current_database()
                      AND state IS NOT NULL AND state <> 'disabled'
                      AND xact_start IS NOT NULL
                """)
                result["visible_oldest_transaction_seconds"] = round(float(cursor.fetchone()[0]), 3)
                cursor.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE NOT granted) FROM pg_locks")
                row = cursor.fetchone()
                result["locks"] = int(row[0])
                result["ungranted_locks"] = int(row[1])
                cursor.execute("SELECT pg_database_size(current_database())")
                result["database_size_bytes"] = int(cursor.fetchone()[0])
        result["status"] = "ok"
    except Exception:
        result = {"status": "error", **{key: None for key in keys}}
    return result


def redis_exchange(port, command):
    payload = "*{}\r\n".format(len(command))
    for part in command:
        encoded = part.encode("ascii")
        payload += "${}\r\n".format(len(encoded)) + part + "\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=0.5) as connection:
        connection.settimeout(0.5)
        connection.sendall(payload.encode("ascii"))
        data = b""
        while len(data) <= 131072:
            chunk = connection.recv(16384)
            if not chunk:
                break
            data += chunk
            if data.startswith((b"+", b"-", b":")) and data.endswith(b"\r\n"):
                break
            if data.startswith(b"$") and b"\r\n" in data:
                length = int(data[1:data.index(b"\r\n")])
                header = data.index(b"\r\n") + 2
                if len(data) >= header + length + 2:
                    break
        if not data or len(data) > 131072:
            raise RuntimeError("redis response invalid")
        return data


def redis_metrics(port):
    fields = (
        "version", "uptime_seconds", "connected_clients", "blocked_clients", "maxclients",
        "used_memory_bytes", "used_memory_peak_bytes", "maxmemory_bytes", "maxmemory_policy",
        "instantaneous_ops_per_sec", "rejected_connections", "evicted_keys", "pubsub_channels",
    )
    result = {
        "status": "unreachable", "port": port,
        **{field: None for field in fields},
        "acl_details_returned": False, "channel_names_returned": False,
    }
    try:
        ping = redis_exchange(port, ("PING",))
        if ping.startswith(b"-NOAUTH"):
            result["status"] = "auth_required"
            return result
        if ping != b"+PONG\r\n":
            result["status"] = "error"
            return result
        raw = redis_exchange(port, ("INFO",))
        if raw.startswith(b"-NOAUTH"):
            result["status"] = "auth_required"
            return result
        if not raw.startswith(b"$") or b"\r\n" not in raw:
            result["status"] = "error"
            return result
        header = raw.index(b"\r\n") + 2
        length = int(raw[1:header - 2])
        text = raw[header:header + length].decode("utf-8", "strict")
        values = dict(line.split(":", 1) for line in text.splitlines() if ":" in line and not line.startswith("#"))
        version = values.get("redis_version")
        result.update({
            "status": "ok",
            "version": version if version and re.fullmatch(r"[0-9.]{1,24}", version) else None,
            "uptime_seconds": integer(values.get("uptime_in_seconds")),
            "connected_clients": integer(values.get("connected_clients")),
            "blocked_clients": integer(values.get("blocked_clients")),
            "maxclients": integer(values.get("maxclients")),
            "used_memory_bytes": integer(values.get("used_memory")),
            "used_memory_peak_bytes": integer(values.get("used_memory_peak")),
            "maxmemory_bytes": integer(values.get("maxmemory")),
            "maxmemory_policy": safe_state(values.get("maxmemory_policy"), fallback="unknown"),
            "instantaneous_ops_per_sec": integer(values.get("instantaneous_ops_per_sec")),
            "rejected_connections": integer(values.get("rejected_connections")),
            "evicted_keys": integer(values.get("evicted_keys")),
            "pubsub_channels": integer(values.get("pubsub_channels")),
        })
    except (OSError, UnicodeError, ValueError, RuntimeError):
        result["status"] = "unreachable"
    return result


def main():
    started_utc = utc_now()
    started = time.monotonic()
    samples = cpu_samples()
    load_average = os.getloadavg()
    uptime_text = read_text("/proc/uptime", 256)
    if not uptime_text:
        raise RuntimeError("uptime unavailable")
    uptime_seconds = round(float(uptime_text.split()[0]), 3)
    services = {key: service_metrics(service) for key, service in SERVICE_ALLOWLIST}
    host = {
        "logical_cpu_count": os.cpu_count() or 1,
        "cpu_usage_percent_samples": samples,
        "cpu_usage_percent_average": round(sum(samples) / len(samples), 3),
        "cpu_usage_percent_maximum": max(samples),
        "load_average": {
            "one_minute": round(load_average[0], 3),
            "five_minutes": round(load_average[1], 3),
            "fifteen_minutes": round(load_average[2], 3),
        },
        "uptime_seconds": uptime_seconds,
        "memory_bytes": memory_bytes(),
        "pressure": {kind: pressure(kind) for kind in ("cpu", "memory", "io")},
        "file_handles": file_handles(),
        "disk_bytes": {
            "root": disk_bytes(Path("/")),
            "application": disk_bytes(APP_ROOT),
        },
    }
    postgresql = postgres_metrics()
    redis = {str(port): redis_metrics(port) for port in REDIS_PORTS}
    finished_utc = utc_now()
    sample_seconds = round(time.monotonic() - started, 3)
    report = {
        "schema": 1,
        "operation": "infra_capacity_v1",
        "request": {},
        "scope": {
            "sample_started_utc": started_utc,
            "sample_finished_utc": finished_utc,
            "sample_seconds": sample_seconds,
            "historical_window_available": False,
            "application_event_loop_probe_available": False,
        },
        "host": host,
        "services": services,
        "postgresql": postgresql,
        "redis": redis,
        "summary": {"row_count": 0, "truncated": False},
        "limitations": list(LIMITATIONS),
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))


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
    if not isinstance(metadata, dict):
        raise ReleaseError("diagnostic metadata keys do not match the fixed contract")
    operation = metadata.get("operation")
    if operation not in DIAGNOSTIC_OPERATIONS:
        raise ReleaseError("diagnostic operation is not allowlisted")
    if operation in {"infra_capacity_v1", "sse_qa_http_503_v1"}:
        expected_keys = (
            INFRA_DIAGNOSTIC_METADATA_KEYS
            if operation == "infra_capacity_v1"
            else SSE_QA_HTTP_503_METADATA_KEYS
        )
        if set(metadata) != expected_keys:
            raise ReleaseError(f"{operation} metadata keys do not match the fixed contract")
        return {"operation": operation}
    if set(metadata) != TRIP_DIAGNOSTIC_METADATA_KEYS:
        raise ReleaseError("diagnostic metadata keys do not match the fixed contract")
    equipment = metadata.get("equipment")
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


def validate_trip_diagnostic_report(
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


INFRA_CAPACITY_LIMITATIONS = [
    "snapshot_not_capacity_baseline",
    "no_historical_normal_or_peak_window",
    "no_application_event_loop_probe",
    "no_nginx_effective_config_or_routes",
    "no_redis_acl_or_channel_names",
]
INFRA_SERVICE_KEYS = {
    "load_state", "active_state", "sub_state", "main_pid", "restarts",
    "cpu_usage_ns", "memory_current_bytes", "memory_peak_bytes",
    "tasks_current", "tasks_max", "limit_nofile",
}
INFRA_POSTGRES_KEYS = {
    "status", "server_version_num", "transaction_read_only", "transaction_isolation",
    "max_connections", "reserved_connections_supported", "reserved_connections",
    "superuser_reserved_connections", "role_connection_limit",
    "server_process_rows", "observed_client_backend_connections",
    "rows_with_unknown_backend_type", "client_backend_count_complete",
    "database_process_rows", "observed_database_client_connections",
    "database_rows_with_unknown_backend_type",
    "database_rows_with_known_nonclient_backend_type",
    "database_client_backend_count_complete",
    "observed_database_client_connections_with_visible_details",
    "observed_database_client_connections_with_hidden_details",
    "observed_database_client_connections_with_disabled_tracking",
    "activity_details_visibility",
    "visible_active_database_client_connections",
    "visible_idle_in_transaction_database_client_connections",
    "visible_lock_waiting_database_client_connections", "locks", "ungranted_locks",
    "database_size_bytes", "visible_oldest_transaction_seconds",
}
INFRA_REDIS_KEYS = {
    "status", "port", "version", "uptime_seconds", "connected_clients",
    "blocked_clients", "maxclients", "used_memory_bytes", "used_memory_peak_bytes",
    "maxmemory_bytes", "maxmemory_policy", "instantaneous_ops_per_sec",
    "rejected_connections", "evicted_keys", "pubsub_channels",
    "acl_details_returned", "channel_names_returned",
}
INFRA_SAFE_STATE_RE = re.compile(r"[a-z0-9_.@-]{1,48}\Z")


def require_number(value: object, *, minimum: float = 0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReleaseError("infra capacity report contains an invalid number")
    number = float(value)
    if not math.isfinite(number):
        raise ReleaseError("infra capacity report contains a non-finite number")
    if number < minimum or (maximum is not None and number > maximum):
        raise ReleaseError("infra capacity report number is out of bounds")
    return number


def require_nonnegative_integer(value: object, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReleaseError("infra capacity report contains an invalid integer")
    return value


def validate_infra_capacity_report(raw: bytes, metadata: dict[str, object]) -> dict[str, Any]:
    if metadata != {"operation": "infra_capacity_v1"}:
        raise ReleaseError("infra capacity request contract mismatch")
    if not raw or len(raw) > DIAGNOSTIC_MAX_OUTPUT_BYTES:
        raise ReleaseError("diagnostic report size is invalid")
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("diagnostic report is not valid JSON") from exc
    if not isinstance(report, dict) or set(report) != {
        "schema", "operation", "request", "scope", "host", "services",
        "postgresql", "redis", "summary", "limitations",
    }:
        raise ReleaseError("infra capacity top-level contract mismatch")
    if type(report.get("schema")) is not int or report["schema"] != 1:
        raise ReleaseError("infra capacity schema is invalid")
    if report.get("operation") != "infra_capacity_v1":
        raise ReleaseError("infra capacity operation contract mismatch")
    if report.get("request") != {}:
        raise ReleaseError("infra capacity operation does not accept parameters")

    scope = report.get("scope")
    if not isinstance(scope, dict) or set(scope) != {
        "sample_started_utc", "sample_finished_utc", "sample_seconds",
        "historical_window_available", "application_event_loop_probe_available",
    }:
        raise ReleaseError("infra capacity sampling scope is invalid")
    try:
        started = datetime.strptime(scope["sample_started_utc"], "%Y-%m-%dT%H:%M:%SZ")
        finished = datetime.strptime(scope["sample_finished_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseError("infra capacity sampling timestamps are invalid") from exc
    if finished < started:
        raise ReleaseError("infra capacity sampling timestamps are reversed")
    require_number(scope.get("sample_seconds"), minimum=4.5, maximum=30)
    if scope.get("historical_window_available") is not False:
        raise ReleaseError("infra capacity report cannot claim historical coverage")
    if scope.get("application_event_loop_probe_available") is not False:
        raise ReleaseError("infra capacity report cannot claim application event-loop coverage")

    host = report.get("host")
    if not isinstance(host, dict) or set(host) != {
        "logical_cpu_count", "cpu_usage_percent_samples", "cpu_usage_percent_average",
        "cpu_usage_percent_maximum", "load_average", "uptime_seconds", "memory_bytes",
        "pressure", "file_handles", "disk_bytes",
    }:
        raise ReleaseError("infra capacity host contract mismatch")
    cpu_count = require_nonnegative_integer(host.get("logical_cpu_count"))
    if cpu_count is None or not 1 <= cpu_count <= 4096:
        raise ReleaseError("infra capacity CPU count is invalid")
    samples = host.get("cpu_usage_percent_samples")
    if not isinstance(samples, list) or len(samples) != 5:
        raise ReleaseError("infra capacity CPU sample count is invalid")
    normalized_samples = [require_number(item, maximum=100) for item in samples]
    average = require_number(host.get("cpu_usage_percent_average"), maximum=100)
    maximum = require_number(host.get("cpu_usage_percent_maximum"), maximum=100)
    if abs(average - sum(normalized_samples) / len(normalized_samples)) > 0.01:
        raise ReleaseError("infra capacity CPU average mismatch")
    if abs(maximum - max(normalized_samples)) > 0.001:
        raise ReleaseError("infra capacity CPU maximum mismatch")
    require_number(host.get("uptime_seconds"))

    load_average = host.get("load_average")
    if not isinstance(load_average, dict) or set(load_average) != {
        "one_minute", "five_minutes", "fifteen_minutes",
    }:
        raise ReleaseError("infra capacity load average is invalid")
    for value in load_average.values():
        require_number(value, maximum=1000000)

    memory = host.get("memory_bytes")
    if not isinstance(memory, dict) or set(memory) != {"total", "available", "swap_total", "swap_free"}:
        raise ReleaseError("infra capacity memory contract mismatch")
    for value in memory.values():
        require_nonnegative_integer(value)
    if memory["available"] > memory["total"] or memory["swap_free"] > memory["swap_total"]:
        raise ReleaseError("infra capacity memory values are inconsistent")

    pressure = host.get("pressure")
    if not isinstance(pressure, dict) or set(pressure) != {"cpu", "memory", "io"}:
        raise ReleaseError("infra capacity pressure contract mismatch")
    for item in pressure.values():
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) != {"avg10", "avg60", "avg300", "total_us"}:
            raise ReleaseError("infra capacity pressure sample is invalid")
        for field in ("avg10", "avg60", "avg300"):
            require_number(item[field], maximum=100)
        require_nonnegative_integer(item["total_us"])

    handles = host.get("file_handles")
    if not isinstance(handles, dict) or set(handles) != {"allocated", "maximum"}:
        raise ReleaseError("infra capacity file handle contract mismatch")
    for value in handles.values():
        require_nonnegative_integer(value, nullable=True)

    disks = host.get("disk_bytes")
    if not isinstance(disks, dict) or set(disks) != {"root", "application"}:
        raise ReleaseError("infra capacity disk contract mismatch")
    for disk in disks.values():
        if not isinstance(disk, dict) or set(disk) != {"total", "used", "free"}:
            raise ReleaseError("infra capacity disk sample is invalid")
        for value in disk.values():
            require_nonnegative_integer(value)
        # shutil.disk_usage().free is space available to this unprivileged
        # process.  Reserved filesystem blocks may make used + free < total.
        if (
            disk["used"] > disk["total"]
            or disk["free"] > disk["total"]
            or disk["used"] + disk["free"] > disk["total"]
        ):
            raise ReleaseError("infra capacity disk values are inconsistent")

    services = report.get("services")
    if not isinstance(services, dict) or set(services) != {
        "accounting_mvp", "nginx", "postgresql", "redis_server",
    }:
        raise ReleaseError("infra capacity service allowlist mismatch")
    for service in services.values():
        if not isinstance(service, dict) or set(service) != INFRA_SERVICE_KEYS:
            raise ReleaseError("infra capacity service contract mismatch")
        for field in ("load_state", "active_state", "sub_state"):
            if not isinstance(service[field], str) or not INFRA_SAFE_STATE_RE.fullmatch(service[field]):
                raise ReleaseError("infra capacity service state is invalid")
        require_nonnegative_integer(service["main_pid"])
        for field in INFRA_SERVICE_KEYS - {"load_state", "active_state", "sub_state", "main_pid"}:
            require_nonnegative_integer(service[field], nullable=True)

    postgres = report.get("postgresql")
    if not isinstance(postgres, dict) or set(postgres) != INFRA_POSTGRES_KEYS:
        raise ReleaseError("infra capacity PostgreSQL contract mismatch")
    if postgres.get("status") not in {"ok", "error", "unsupported"}:
        raise ReleaseError("infra capacity PostgreSQL status is invalid")
    postgres_values = INFRA_POSTGRES_KEYS - {"status"}
    if postgres["status"] == "ok":
        if postgres["transaction_read_only"] is not True:
            raise ReleaseError("infra capacity PostgreSQL transaction is not read only")
        if not isinstance(postgres["transaction_isolation"], str) or not INFRA_SAFE_STATE_RE.fullmatch(postgres["transaction_isolation"]):
            raise ReleaseError("infra capacity PostgreSQL isolation is invalid")
        if type(postgres["reserved_connections_supported"]) is not bool:
            raise ReleaseError("infra capacity PostgreSQL reserved connection support is invalid")
        for field in ("client_backend_count_complete", "database_client_backend_count_complete"):
            if type(postgres[field]) is not bool:
                raise ReleaseError("infra capacity PostgreSQL completeness flag is invalid")
        if postgres["reserved_connections_supported"]:
            require_nonnegative_integer(postgres["reserved_connections"])
        elif postgres["reserved_connections"] is not None:
            raise ReleaseError("infra capacity PostgreSQL unsupported reserved connections must be empty")
        for field in postgres_values - {
            "transaction_read_only", "transaction_isolation", "reserved_connections_supported",
            "client_backend_count_complete", "database_client_backend_count_complete",
            "reserved_connections", "activity_details_visibility",
            "visible_oldest_transaction_seconds", "role_connection_limit",
        }:
            require_nonnegative_integer(postgres[field])
        if isinstance(postgres["role_connection_limit"], bool) or not isinstance(postgres["role_connection_limit"], int) or postgres["role_connection_limit"] < -1:
            raise ReleaseError("infra capacity PostgreSQL role limit is invalid")
        if postgres["activity_details_visibility"] not in {"full", "partial"}:
            raise ReleaseError("infra capacity PostgreSQL visibility is invalid")
        unknown = postgres["rows_with_unknown_backend_type"]
        database_unknown = postgres["database_rows_with_unknown_backend_type"]
        database_nonclient = postgres["database_rows_with_known_nonclient_backend_type"]
        observed_database_clients = postgres["observed_database_client_connections"]
        hidden = postgres["observed_database_client_connections_with_hidden_details"]
        disabled = postgres["observed_database_client_connections_with_disabled_tracking"]
        visible = postgres["observed_database_client_connections_with_visible_details"]
        if visible + hidden + disabled != observed_database_clients:
            raise ReleaseError("infra capacity PostgreSQL visibility counts are inconsistent")
        if observed_database_clients + database_unknown + database_nonclient != postgres["database_process_rows"]:
            raise ReleaseError("infra capacity PostgreSQL database row partition is inconsistent")
        if postgres["observed_client_backend_connections"] + unknown > postgres["server_process_rows"]:
            raise ReleaseError("infra capacity PostgreSQL server row counts are inconsistent")
        if postgres["database_process_rows"] > postgres["server_process_rows"]:
            raise ReleaseError("infra capacity PostgreSQL database row counts are inconsistent")
        if postgres["client_backend_count_complete"] != (unknown == 0):
            raise ReleaseError("infra capacity PostgreSQL client completeness flag is inconsistent")
        if postgres["database_client_backend_count_complete"] != (database_unknown == 0):
            raise ReleaseError("infra capacity PostgreSQL database completeness flag is inconsistent")
        if postgres["activity_details_visibility"] != ("partial" if database_unknown or hidden or disabled else "full"):
            raise ReleaseError("infra capacity PostgreSQL visibility flag is inconsistent")
        if postgres["observed_client_backend_connections"] < observed_database_clients:
            raise ReleaseError("infra capacity PostgreSQL client connection counts are inconsistent")
        for field in (
            "visible_active_database_client_connections",
            "visible_idle_in_transaction_database_client_connections",
            "visible_lock_waiting_database_client_connections",
        ):
            if postgres[field] > visible:
                raise ReleaseError("infra capacity PostgreSQL visible detail count is inconsistent")
        require_number(postgres["visible_oldest_transaction_seconds"])
    elif any(postgres[field] is not None for field in postgres_values):
        raise ReleaseError("infra capacity unavailable PostgreSQL metrics must be empty")

    redis = report.get("redis")
    if not isinstance(redis, dict) or set(redis) != {"6379", "6381"}:
        raise ReleaseError("infra capacity Redis endpoint allowlist mismatch")
    for key, item in redis.items():
        if not isinstance(item, dict) or set(item) != INFRA_REDIS_KEYS:
            raise ReleaseError("infra capacity Redis contract mismatch")
        if item.get("status") not in {"ok", "auth_required", "unreachable", "error"}:
            raise ReleaseError("infra capacity Redis status is invalid")
        if type(item.get("port")) is not int or item["port"] != int(key):
            raise ReleaseError("infra capacity Redis port mismatch")
        if item.get("acl_details_returned") is not False or item.get("channel_names_returned") is not False:
            raise ReleaseError("infra capacity Redis report exposes forbidden details")
        metric_fields = INFRA_REDIS_KEYS - {
            "status", "port", "acl_details_returned", "channel_names_returned", "version", "maxmemory_policy",
        }
        if item["status"] == "ok":
            for field in metric_fields:
                require_nonnegative_integer(item[field], nullable=True)
            if item["version"] is not None and (
                not isinstance(item["version"], str) or not re.fullmatch(r"[0-9.]{1,24}", item["version"])
            ):
                raise ReleaseError("infra capacity Redis version is invalid")
            if not isinstance(item["maxmemory_policy"], str) or not INFRA_SAFE_STATE_RE.fullmatch(item["maxmemory_policy"]):
                raise ReleaseError("infra capacity Redis policy is invalid")
        elif any(item[field] is not None for field in metric_fields | {"version", "maxmemory_policy"}):
            raise ReleaseError("infra capacity unavailable Redis metrics must be empty")

    summary = report.get("summary")
    if (
        not isinstance(summary, dict)
        or set(summary) != {"row_count", "truncated"}
        or type(summary["row_count"]) is not int
        or summary["row_count"] != 0
        or type(summary["truncated"]) is not bool
        or summary["truncated"] is not False
    ):
        raise ReleaseError("infra capacity public summary is invalid")
    if report.get("limitations") != INFRA_CAPACITY_LIMITATIONS:
        raise ReleaseError("infra capacity limitations contract mismatch")
    reject_sensitive_diagnostic_value(report)
    return report


SSE_QA_HTTP_503_LIMITATIONS = [
    "fixed_historical_window_only",
    "bounded_log_tail_may_omit_rotated_records",
    "nginx_error_timezone_derived_from_single_access_log_offset",
    "static_access_log_disabled",
    "no_raw_client_addresses_credentials_cookies_or_user_agents",
    "wsgi_journal_queried_only_when_target_limit_not_confirmed",
]
SSE_QA_HTTP_503_ACCESS_RE = re.compile(
    r'^\S+\s+\S+\s+\S+\s+\[(?P<time>[^]]+)\]\s+'
    r'"(?P<method>[A-Z]{1,12})\s+(?P<target>\S+)\s+(?P<protocol>HTTP/[0-9.]{3,8})"\s+'
    r'(?P<status>[0-9]{3})\s+(?P<bytes>[0-9]+|-)\s'
)
SSE_QA_HTTP_503_ERROR_TIME_RE = re.compile(
    r"^(?P<time>[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})"
)
SSE_QA_HTTP_503_ERROR_REQUEST_RE = re.compile(
    r'request:\s+"(?P<method>[A-Z]{1,12})\s+(?P<target>\S+)\s+(?P<protocol>HTTP/[0-9.]{3,8})"'
)
SSE_QA_HTTP_503_ZONE_RE = re.compile(r'limiting connections by zone "(?P<zone>[a-z0-9_]{1,64})"')
SSE_QA_HTTP_503_SAFE_ROUTE_RE = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/\-]{0,255}\Z")
SSE_QA_HTTP_503_MONTHS = {
    name: index for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        1,
    )
}


def _sse_qa_http_503_fixed_window() -> tuple[datetime, datetime]:
    return (
        datetime.strptime(SSE_QA_HTTP_503_WINDOW_FROM, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
        datetime.strptime(SSE_QA_HTTP_503_WINDOW_TO, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
    )


def _sse_qa_http_503_in_fixed_window(moment: datetime) -> bool:
    window_from, window_to = _sse_qa_http_503_fixed_window()
    return window_from <= moment <= window_to


def _sse_qa_http_503_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sse_qa_http_503_access_parts(value: str) -> tuple[datetime, int] | None:
    match = re.fullmatch(
        r"([0-9]{2})/([A-Z][a-z]{2})/([0-9]{4}):([0-9]{2}):([0-9]{2}):([0-9]{2}) ([+-])([0-9]{2})([0-9]{2})",
        value,
    )
    if not match or match.group(2) not in SSE_QA_HTTP_503_MONTHS:
        return None
    offset_minutes = (int(match.group(8)) * 60 + int(match.group(9))) * (-1 if match.group(7) == "-" else 1)
    try:
        moment = datetime(
            int(match.group(3)),
            SSE_QA_HTTP_503_MONTHS[match.group(2)],
            int(match.group(1)),
            int(match.group(4)),
            int(match.group(5)),
            int(match.group(6)),
            tzinfo=timezone(timedelta(minutes=offset_minutes)),
        ).astimezone(timezone.utc)
        return moment, offset_minutes
    except ValueError:
        return None


def _sse_qa_http_503_access_time(value: str) -> datetime | None:
    parts = _sse_qa_http_503_access_parts(value)
    return parts[0] if parts else None


def _sse_qa_http_503_error_time(value: str, offset_minutes: int | None) -> datetime | None:
    if offset_minutes is None:
        return None
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(minutes=offset_minutes))
        ).astimezone(timezone.utc)
    except ValueError:
        return None


def _sse_qa_http_503_route(target: str) -> str | None:
    route = target.split("?", 1)[0]
    if not SSE_QA_HTTP_503_SAFE_ROUTE_RE.fullmatch(route):
        return None
    return route


def _sse_qa_http_503_classification(route: str) -> str:
    if route == "/realtime/stream/":
        return "realtime_stream"
    if route.startswith("/static/"):
        return "static"
    return "ordinary_http"


def _sse_qa_http_503_read_tail(
    path: Path,
    maximum: int,
) -> tuple[dict[str, object], list[str]]:
    base = {
        "status": "unavailable",
        "reason": "read_error",
        "bytes_examined": 0,
        "lines_examined": 0,
        "tail_truncated": False,
    }
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {**base, "reason": "missing"}, []
    except OSError:
        return base, []
    if stat.S_ISLNK(metadata.st_mode):
        return {**base, "reason": "symlink_target_rejected"}, []
    if not stat.S_ISREG(metadata.st_mode):
        return {**base, "reason": "type_rejected"}, []
    try:
        size = metadata.st_size
        offset = max(0, size - maximum)
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(maximum + 1)
    except OSError:
        return base, []
    if len(data) > maximum:
        data = data[:maximum]
    if offset:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    lines = data.decode("utf-8", errors="ignore").splitlines()
    return {
        "status": "ok",
        "reason": "ok_regular",
        "bytes_examined": len(data),
        "lines_examined": len(lines),
        "tail_truncated": bool(offset),
    }, lines


def _sse_qa_http_503_config_scope(lines: list[str], source: dict[str, object]) -> dict[str, object]:
    contexts: list[str] = []
    zone_found = False
    per_ip_server_limits: list[int] = []
    location_limits: dict[str, list[tuple[str, int]]] = {
        "ordinary_http": [], "static": [], "realtime_stream": [],
    }
    locations_seen: set[str] = set()
    static_access_off = False
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "limit_conn_zone $binary_remote_addr zone=sse_qa_per_ip:64k;" and not contexts:
            zone_found = True
        if line.endswith("{"):
            if line == "server {":
                contexts.append("server")
            elif contexts and contexts[0] == "server" and line.startswith("location "):
                if line == "location = /realtime/stream/ {":
                    context = "realtime_stream"
                elif line == "location /static/ {":
                    context = "static"
                elif line == "location / {":
                    context = "ordinary_http"
                else:
                    context = "other"
                contexts.append(context)
                locations_seen.add(context)
            else:
                contexts.append("other")
            continue
        if line == "}":
            if contexts:
                contexts.pop()
            continue
        match = re.fullmatch(r"limit_conn ([a-z0-9_]{1,64}) ([0-9]{1,4});", line)
        if match:
            item = (match.group(1), int(match.group(2)))
            if contexts == ["server"] and item[0] == "sse_qa_per_ip":
                per_ip_server_limits.append(item[1])
            elif len(contexts) == 2 and contexts[1] in location_limits:
                location_limits[contexts[1]].append(item)
        if contexts == ["server", "static"] and line == "access_log off;":
            static_access_off = True

    per_ip_limit = per_ip_server_limits[0] if len(per_ip_server_limits) == 1 else None
    exact_locations = {"ordinary_http", "static", "realtime_stream"}.issubset(locations_seen)
    confirmed = bool(zone_found and per_ip_limit == 8 and exact_locations)

    def inherited_per_ip(name: str) -> int | None:
        local = location_limits[name]
        if local:
            for zone, limit in local:
                if zone == "sse_qa_per_ip":
                    return limit
            return None
        return per_ip_limit

    stream_total = None
    for zone, limit in location_limits["realtime_stream"]:
        if zone == "sse_qa_total":
            stream_total = limit
    if stream_total != 2:
        confirmed = False
    return {
        "status": "confirmed" if source["status"] == "ok" and confirmed else (
            "mismatch" if source["status"] == "ok" else "unavailable"
        ),
        "per_ip_zone": "sse_qa_per_ip" if zone_found else None,
        "per_ip_limit": per_ip_limit,
        "per_ip_placement": "server" if per_ip_limit is not None else None,
        "ordinary_http_per_ip_limit": inherited_per_ip("ordinary_http") if exact_locations else None,
        "static_per_ip_limit": inherited_per_ip("static") if exact_locations else None,
        "realtime_per_ip_limit": inherited_per_ip("realtime_stream") if exact_locations else None,
        "realtime_total_limit": stream_total,
        "static_access_logged": False if static_access_off else None,
    }


def _sse_qa_http_503_journal() -> tuple[dict[str, object], list[dict[str, object]]]:
    command = [
        "/usr/bin/journalctl",
        "--unit", SSE_QA_HTTP_503_WSGI_UNIT,
        "--since", "2026-10-04 07:09:30 UTC",
        "--until", "2026-10-04 07:10:05 UTC",
        "--output=short-iso-precise",
        "--no-pager",
        "--lines=200",
    ]
    base = {
        "status": "unavailable",
        "reason": "command_error",
        "queried": True,
        "bytes_examined": 0,
        "lines_examined": 0,
        "tail_truncated": False,
    }
    try:
        result = subprocess.run(
            command,
            cwd="/",
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            close_fds=True,
            start_new_session=True,
            env={"HOME": "/", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return base, []
    raw = result.stdout
    truncated = len(raw) > SSE_QA_HTTP_503_MAX_JOURNAL_BYTES
    raw = raw[:SSE_QA_HTTP_503_MAX_JOURNAL_BYTES]
    lines = raw.decode("utf-8", errors="ignore").splitlines()
    source = {
        "status": "ok" if result.returncode == 0 else "error",
        "reason": "ok_command" if result.returncode == 0 else "command_error",
        "queried": True,
        "bytes_examined": len(raw),
        "lines_examined": len(lines),
        "tail_truncated": truncated,
    }
    records: list[dict[str, object]] = []
    for line in lines:
        lowered = line.casefold()
        classification = None
        for marker, label in (
            ("traceback", "traceback"),
            ("exception", "exception"),
            ("timed out", "timeout"),
            ("timeout", "timeout"),
            ("worker", "worker"),
            (" 503 ", "http_503"),
            ("error", "error"),
        ):
            if marker in lowered:
                classification = label
                break
        if classification is None:
            continue
        time_match = re.match(r"(?P<time>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(?:Z|[+-][0-9:]+))", line)
        timestamp = "2026-10-04T07:09:30Z"
        if time_match:
            try:
                timestamp = _sse_qa_http_503_iso(datetime.fromisoformat(time_match.group("time").replace("Z", "+00:00")))
            except ValueError:
                pass
        records.append({
            "timestamp_utc": timestamp,
            "classification": classification,
            "message_sha256": digest(line.encode("utf-8")),
        })
        if len(records) >= 50:
            source["tail_truncated"] = True
            break
    return source, records


def collect_sse_qa_http_503_report() -> dict[str, Any]:
    window_from, window_to = _sse_qa_http_503_fixed_window()
    error_source, error_lines = _sse_qa_http_503_read_tail(
        SSE_QA_HTTP_503_ERROR_LOG, SSE_QA_HTTP_503_MAX_LOG_BYTES,
    )
    access_source, access_lines = _sse_qa_http_503_read_tail(
        SSE_QA_HTTP_503_ACCESS_LOG, SSE_QA_HTTP_503_MAX_LOG_BYTES,
    )
    config_source, config_lines = _sse_qa_http_503_read_tail(
        SSE_QA_HTTP_503_NGINX_SITE,
        SSE_QA_HTTP_503_MAX_CONFIG_BYTES,
    )
    configuration = _sse_qa_http_503_config_scope(config_lines, config_source)
    target_access: list[dict[str, object]] = []
    limit_events: list[dict[str, object]] = []
    upstream_events: list[dict[str, object]] = []
    observed_counts = {"ordinary_http": 0, "static": 0, "realtime_stream": 0}
    status_counts: dict[str, int] = {}
    access_offsets: set[int] = set()
    truncated = any(
        bool(source["tail_truncated"])
        for source in (error_source, access_source, config_source)
    )

    for line in access_lines:
        match = SSE_QA_HTTP_503_ACCESS_RE.match(line)
        if not match:
            continue
        moment = _sse_qa_http_503_access_time(match.group("time"))
        route = _sse_qa_http_503_route(match.group("target"))
        if moment is None or route is None or not window_from <= moment <= window_to:
            continue
        access_parts = _sse_qa_http_503_access_parts(match.group("time"))
        if access_parts is not None:
            access_offsets.add(access_parts[1])
        classification = _sse_qa_http_503_classification(route)
        observed_counts[classification] += 1
        status = int(match.group("status"))
        status_counts[str(status)] = status_counts.get(str(status), 0) + 1
        if (
            match.group("method") == "GET"
            and route == "/driver/"
            and status == 503
        ):
            response_bytes = None if match.group("bytes") == "-" else int(match.group("bytes"))
            timestamp = _sse_qa_http_503_iso(moment)
            record = {
                "timestamp_utc": timestamp,
                "method": "GET",
                "route": route,
                "protocol": match.group("protocol"),
                "status": status,
                "response_bytes": response_bytes,
                "classification": classification,
            }
            record["sanitized_log_line"] = (
                f'{timestamp} GET {route} {record["protocol"]} status=503 '
                f'bytes={response_bytes if response_bytes is not None else "unknown"} class={classification}'
            )
            target_access.append(record)
            if len(target_access) >= 20:
                truncated = True
                break

    error_offset_minutes = next(iter(access_offsets)) if len(access_offsets) == 1 else None
    time_basis = {
        "access_offsets_minutes": sorted(access_offsets),
        "nginx_error_offset_minutes": error_offset_minutes,
        "nginx_error_timezone_source": (
            "single_access_log_offset" if error_offset_minutes is not None else "unavailable"
        ),
    }
    upstream_markers = (
        ("upstream timed out", "upstream_timeout"),
        ("upstream prematurely closed", "upstream_closed"),
        ("connect() failed", "upstream_connect_failed"),
        ("no live upstreams", "no_live_upstream"),
    )
    for line in error_lines:
        time_match = SSE_QA_HTTP_503_ERROR_TIME_RE.match(line)
        if not time_match:
            continue
        moment = _sse_qa_http_503_error_time(time_match.group("time"), error_offset_minutes)
        if moment is None or not window_from <= moment <= window_to:
            continue
        request_match = SSE_QA_HTTP_503_ERROR_REQUEST_RE.search(line)
        method = request_match.group("method") if request_match else None
        route = _sse_qa_http_503_route(request_match.group("target")) if request_match else None
        protocol = request_match.group("protocol") if request_match else None
        timestamp = _sse_qa_http_503_iso(moment)
        zone_match = SSE_QA_HTTP_503_ZONE_RE.search(line)
        if zone_match and method and route and protocol:
            zone = zone_match.group("zone")
            record = {
                "timestamp_utc": timestamp,
                "method": method,
                "route": route,
                "protocol": protocol,
                "zone": zone,
                "classification": _sse_qa_http_503_classification(route),
            }
            record["sanitized_log_line"] = (
                f'{timestamp} limiting connections by zone "{zone}" '
                f'request="{method} {route} {protocol}" class={record["classification"]}'
            )
            limit_events.append(record)
        for marker, classification in upstream_markers:
            if marker in line:
                if len(upstream_events) < 50:
                    upstream_events.append({
                        "timestamp_utc": timestamp,
                        "method": method,
                        "route": route,
                        "classification": classification,
                        "message_sha256": digest(line.encode("utf-8")),
                    })
                else:
                    truncated = True
                break
        if len(limit_events) + len(upstream_events) >= SSE_QA_HTTP_503_MAX_RECORDS:
            truncated = True
            break

    target_limit = [
        item for item in limit_events
        if item["zone"] == "sse_qa_per_ip"
        and item["method"] == "GET"
        and item["route"] == "/driver/"
        and _sse_qa_http_503_in_fixed_window(
            datetime.strptime(item["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        )
    ]
    journal_queried = not bool(target_limit)
    if journal_queried:
        journal_source, wsgi_events = _sse_qa_http_503_journal()
    else:
        journal_source = {
            "status": "not_required",
            "reason": "not_queried",
            "queried": False,
            "bytes_examined": 0,
            "lines_examined": 0,
            "tail_truncated": False,
        }
        wsgi_events = []
    truncated = truncated or bool(journal_source["tail_truncated"])

    if target_limit:
        cause = "limit_conn_sse_qa_per_ip"
        zone: str | None = "sse_qa_per_ip"
    elif upstream_events or wsgi_events:
        cause = "upstream_or_application"
        zone = None
    elif target_access:
        cause = "other_nginx_or_access_layer"
        zone = None
    else:
        cause = "not_established"
        zone = None
    row_count = len(target_access) + len(limit_events) + len(upstream_events) + len(wsgi_events)
    report = {
        "schema": 1,
        "operation": "sse_qa_http_503_v1",
        "request": {
            "from_utc": SSE_QA_HTTP_503_WINDOW_FROM,
            "to_utc": SSE_QA_HTTP_503_WINDOW_TO,
            "target_around_utc": SSE_QA_HTTP_503_TARGET_AROUND,
            "method": "GET",
            "route": "/driver/",
            "status": 503,
        },
        "sources": {
            "nginx_error": error_source,
            "nginx_access": access_source,
            "nginx_site": config_source,
            "wsgi_journal": journal_source,
        },
        "configuration": configuration,
        "time_basis": time_basis,
        "observed_access_counts": observed_counts,
        "observed_status_counts": status_counts,
        "target_access": target_access,
        "limit_events": limit_events,
        "upstream_events": upstream_events,
        "wsgi_events": wsgi_events,
        "finding": {
            "cause": cause,
            "target_limit_correlated": bool(target_limit),
            "target_access_seen": bool(target_access),
            "zone": zone,
            "wsgi_journal_queried": journal_queried,
        },
        "summary": {"row_count": row_count, "truncated": truncated},
        "limitations": SSE_QA_HTTP_503_LIMITATIONS,
    }
    return validate_sse_qa_http_503_report(
        json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        {"operation": "sse_qa_http_503_v1"},
    )


def validate_sse_qa_http_503_report(raw: bytes, metadata: dict[str, object]) -> dict[str, Any]:
    if metadata != {"operation": "sse_qa_http_503_v1"}:
        raise ReleaseError("SSE QA HTTP 503 request contract mismatch")
    if not raw or len(raw) > DIAGNOSTIC_MAX_OUTPUT_BYTES:
        raise ReleaseError("diagnostic report size is invalid")
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("diagnostic report is not valid JSON") from exc
    expected_top = {
        "schema", "operation", "request", "sources", "configuration", "time_basis",
        "observed_access_counts", "observed_status_counts", "target_access",
        "limit_events", "upstream_events", "wsgi_events", "finding", "summary",
        "limitations",
    }
    if not isinstance(report, dict) or set(report) != expected_top:
        raise ReleaseError("SSE QA HTTP 503 top-level contract mismatch")
    if type(report.get("schema")) is not int or report["schema"] != 1:
        raise ReleaseError("SSE QA HTTP 503 schema is invalid")
    if report.get("operation") != "sse_qa_http_503_v1":
        raise ReleaseError("SSE QA HTTP 503 operation mismatch")
    if report.get("request") != {
        "from_utc": SSE_QA_HTTP_503_WINDOW_FROM,
        "to_utc": SSE_QA_HTTP_503_WINDOW_TO,
        "target_around_utc": SSE_QA_HTTP_503_TARGET_AROUND,
        "method": "GET",
        "route": "/driver/",
        "status": 503,
    }:
        raise ReleaseError("SSE QA HTTP 503 fixed request mismatch")
    sources = report.get("sources")
    if not isinstance(sources, dict) or set(sources) != {
        "nginx_error", "nginx_access", "nginx_site", "wsgi_journal",
    }:
        raise ReleaseError("SSE QA HTTP 503 sources contract mismatch")
    source_keys = {"status", "reason", "bytes_examined", "lines_examined", "tail_truncated"}
    for name, source in sources.items():
        expected = source_keys | ({"queried"} if name == "wsgi_journal" else set())
        if not isinstance(source, dict) or set(source) != expected:
            raise ReleaseError("SSE QA HTTP 503 source contract mismatch")
        allowed_status = {"ok", "unavailable"} if name != "wsgi_journal" else {
            "ok", "error", "unavailable", "not_required",
        }
        if source["status"] not in allowed_status:
            raise ReleaseError("SSE QA HTTP 503 source status is invalid")
        if name == "wsgi_journal":
            expected_reason = {
                "ok": "ok_command",
                "error": "command_error",
                "unavailable": "command_error",
                "not_required": "not_queried",
            }[source["status"]]
            if source["reason"] != expected_reason:
                raise ReleaseError("SSE QA HTTP 503 journal reason is invalid")
        elif source["status"] == "ok":
            if source["reason"] != "ok_regular":
                raise ReleaseError("SSE QA HTTP 503 readable source reason is invalid")
        elif source["reason"] not in {
            "missing", "type_rejected", "symlink_target_rejected", "read_error",
        }:
            raise ReleaseError("SSE QA HTTP 503 unavailable source reason is invalid")
        for field in ("bytes_examined", "lines_examined"):
            if type(source[field]) is not int or source[field] < 0:
                raise ReleaseError("SSE QA HTTP 503 source count is invalid")
        if type(source["tail_truncated"]) is not bool:
            raise ReleaseError("SSE QA HTTP 503 source truncation is invalid")
        if name == "wsgi_journal" and type(source["queried"]) is not bool:
            raise ReleaseError("SSE QA HTTP 503 journal query flag is invalid")
        maximum_bytes = (
            SSE_QA_HTTP_503_MAX_JOURNAL_BYTES
            if name == "wsgi_journal"
            else SSE_QA_HTTP_503_MAX_CONFIG_BYTES
            if name == "nginx_site"
            else SSE_QA_HTTP_503_MAX_LOG_BYTES
        )
        if source["bytes_examined"] > maximum_bytes:
            raise ReleaseError("SSE QA HTTP 503 source byte bound is invalid")
    journal_source = sources["wsgi_journal"]
    if journal_source["status"] == "not_required":
        if journal_source != {
            "status": "not_required",
            "reason": "not_queried",
            "queried": False,
            "bytes_examined": 0,
            "lines_examined": 0,
            "tail_truncated": False,
        }:
            raise ReleaseError("SSE QA HTTP 503 skipped journal state is invalid")
    elif journal_source["queried"] is not True:
        raise ReleaseError("SSE QA HTTP 503 journal state is invalid")

    configuration = report.get("configuration")
    if not isinstance(configuration, dict) or set(configuration) != {
        "status", "per_ip_zone", "per_ip_limit", "per_ip_placement",
        "ordinary_http_per_ip_limit", "static_per_ip_limit", "realtime_per_ip_limit",
        "realtime_total_limit", "static_access_logged",
    }:
        raise ReleaseError("SSE QA HTTP 503 configuration contract mismatch")
    if configuration["status"] not in {"confirmed", "mismatch", "unavailable"}:
        raise ReleaseError("SSE QA HTTP 503 configuration status is invalid")
    if configuration["per_ip_zone"] not in {None, "sse_qa_per_ip"}:
        raise ReleaseError("SSE QA HTTP 503 zone is invalid")
    if configuration["per_ip_placement"] not in {None, "server"}:
        raise ReleaseError("SSE QA HTTP 503 placement is invalid")
    for field in (
        "per_ip_limit", "ordinary_http_per_ip_limit", "static_per_ip_limit",
        "realtime_per_ip_limit", "realtime_total_limit",
    ):
        if configuration[field] is not None and (
            type(configuration[field]) is not int or not 1 <= configuration[field] <= 4096
        ):
            raise ReleaseError("SSE QA HTTP 503 connection limit is invalid")
    if (
        configuration["static_access_logged"] is not None
        and type(configuration["static_access_logged"]) is not bool
    ):
        raise ReleaseError("SSE QA HTTP 503 static access logging state is invalid")
    if configuration["status"] == "confirmed" and configuration != {
        "status": "confirmed",
        "per_ip_zone": "sse_qa_per_ip",
        "per_ip_limit": 8,
        "per_ip_placement": "server",
        "ordinary_http_per_ip_limit": 8,
        "static_per_ip_limit": 8,
        "realtime_per_ip_limit": None,
        "realtime_total_limit": 2,
        "static_access_logged": False,
    }:
        raise ReleaseError("SSE QA HTTP 503 confirmed configuration is inconsistent")

    time_basis = report.get("time_basis")
    if not isinstance(time_basis, dict) or set(time_basis) != {
        "access_offsets_minutes", "nginx_error_offset_minutes", "nginx_error_timezone_source",
    }:
        raise ReleaseError("SSE QA HTTP 503 time basis contract mismatch")
    offsets = time_basis["access_offsets_minutes"]
    if (
        not isinstance(offsets, list)
        or len(offsets) > 4
        or any(type(value) is not int or not -840 <= value <= 840 for value in offsets)
        or offsets != sorted(set(offsets))
    ):
        raise ReleaseError("SSE QA HTTP 503 access offsets are invalid")
    error_offset = time_basis["nginx_error_offset_minutes"]
    source = time_basis["nginx_error_timezone_source"]
    if len(offsets) == 1:
        if error_offset != offsets[0] or source != "single_access_log_offset":
            raise ReleaseError("SSE QA HTTP 503 error timezone is inconsistent")
    elif error_offset is not None or source != "unavailable":
        raise ReleaseError("SSE QA HTTP 503 unavailable error timezone is inconsistent")

    observed = report.get("observed_access_counts")
    if not isinstance(observed, dict) or set(observed) != {
        "ordinary_http", "static", "realtime_stream",
    }:
        raise ReleaseError("SSE QA HTTP 503 access count contract mismatch")
    if any(type(value) is not int or value < 0 for value in observed.values()):
        raise ReleaseError("SSE QA HTTP 503 access count is invalid")
    status_counts = report.get("observed_status_counts")
    if not isinstance(status_counts, dict) or any(
        not isinstance(key, str)
        or not re.fullmatch(r"[1-5][0-9]{2}", key)
        or type(value) is not int
        or value < 1
        for key, value in status_counts.items()
    ):
        raise ReleaseError("SSE QA HTTP 503 status count is invalid")

    def valid_timestamp(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        start, finish = _sse_qa_http_503_fixed_window()
        return start <= moment <= finish

    access_rows = report.get("target_access")
    if not isinstance(access_rows, list) or len(access_rows) > 20:
        raise ReleaseError("SSE QA HTTP 503 target access rows are invalid")
    for row in access_rows:
        if not isinstance(row, dict) or set(row) != {
            "timestamp_utc", "method", "route", "protocol", "status",
            "response_bytes", "classification", "sanitized_log_line",
        }:
            raise ReleaseError("SSE QA HTTP 503 target access row contract mismatch")
        if (
            not valid_timestamp(row["timestamp_utc"])
            or row["method"] != "GET"
            or row["route"] != "/driver/"
            or not re.fullmatch(r"HTTP/[0-9.]{3,8}", row["protocol"])
            or row["status"] != 503
            or row["classification"] != "ordinary_http"
            or (row["response_bytes"] is not None and (type(row["response_bytes"]) is not int or row["response_bytes"] < 0))
        ):
            raise ReleaseError("SSE QA HTTP 503 target access row is invalid")
        expected = (
            f'{row["timestamp_utc"]} GET /driver/ {row["protocol"]} status=503 '
            f'bytes={row["response_bytes"] if row["response_bytes"] is not None else "unknown"} class=ordinary_http'
        )
        if row["sanitized_log_line"] != expected:
            raise ReleaseError("SSE QA HTTP 503 target access line is invalid")

    limit_rows = report.get("limit_events")
    if not isinstance(limit_rows, list) or len(limit_rows) > SSE_QA_HTTP_503_MAX_RECORDS:
        raise ReleaseError("SSE QA HTTP 503 limit rows are invalid")
    for row in limit_rows:
        if not isinstance(row, dict) or set(row) != {
            "timestamp_utc", "method", "route", "protocol", "zone",
            "classification", "sanitized_log_line",
        }:
            raise ReleaseError("SSE QA HTTP 503 limit row contract mismatch")
        if (
            not valid_timestamp(row["timestamp_utc"])
            or not isinstance(row["method"], str)
            or not re.fullmatch(r"[A-Z]{1,12}", row["method"])
            or not isinstance(row["route"], str)
            or not SSE_QA_HTTP_503_SAFE_ROUTE_RE.fullmatch(row["route"])
            or not re.fullmatch(r"HTTP/[0-9.]{3,8}", row["protocol"])
            or not re.fullmatch(r"[a-z0-9_]{1,64}", row["zone"])
            or row["classification"] != _sse_qa_http_503_classification(row["route"])
        ):
            raise ReleaseError("SSE QA HTTP 503 limit row is invalid")
        expected = (
            f'{row["timestamp_utc"]} limiting connections by zone "{row["zone"]}" '
            f'request="{row["method"]} {row["route"]} {row["protocol"]}" class={row["classification"]}'
        )
        if row["sanitized_log_line"] != expected:
            raise ReleaseError("SSE QA HTTP 503 limit line is invalid")

    hash_rows = (
        ("upstream_events", {"upstream_timeout", "upstream_closed", "upstream_connect_failed", "no_live_upstream"}, True),
        ("wsgi_events", {"traceback", "exception", "timeout", "worker", "http_503", "error"}, False),
    )
    for name, classifications, has_request in hash_rows:
        rows = report.get(name)
        if not isinstance(rows, list) or len(rows) > 50:
            raise ReleaseError("SSE QA HTTP 503 hashed rows are invalid")
        expected_keys = {"timestamp_utc", "classification", "message_sha256"}
        if has_request:
            expected_keys |= {"method", "route"}
        for row in rows:
            if not isinstance(row, dict) or set(row) != expected_keys:
                raise ReleaseError("SSE QA HTTP 503 hashed row contract mismatch")
            if (
                not valid_timestamp(row["timestamp_utc"])
                or row["classification"] not in classifications
                or not isinstance(row["message_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["message_sha256"])
            ):
                raise ReleaseError("SSE QA HTTP 503 hashed row is invalid")
            if has_request:
                if row["method"] is not None and (
                    not isinstance(row["method"], str) or not re.fullmatch(r"[A-Z]{1,12}", row["method"])
                ):
                    raise ReleaseError("SSE QA HTTP 503 upstream method is invalid")
                if row["route"] is not None and (
                    not isinstance(row["route"], str) or not SSE_QA_HTTP_503_SAFE_ROUTE_RE.fullmatch(row["route"])
                ):
                    raise ReleaseError("SSE QA HTTP 503 upstream route is invalid")

    finding = report.get("finding")
    if not isinstance(finding, dict) or set(finding) != {
        "cause", "target_limit_correlated", "target_access_seen", "zone",
        "wsgi_journal_queried",
    }:
        raise ReleaseError("SSE QA HTTP 503 finding contract mismatch")
    if finding["cause"] not in {
        "limit_conn_sse_qa_per_ip", "upstream_or_application",
        "other_nginx_or_access_layer", "not_established",
    }:
        raise ReleaseError("SSE QA HTTP 503 cause is invalid")
    for field in ("target_limit_correlated", "target_access_seen", "wsgi_journal_queried"):
        if type(finding[field]) is not bool:
            raise ReleaseError("SSE QA HTTP 503 finding flag is invalid")
    if finding["zone"] not in {None, "sse_qa_per_ip"}:
        raise ReleaseError("SSE QA HTTP 503 finding zone is invalid")
    if finding["target_limit_correlated"] != any(
        row["zone"] == "sse_qa_per_ip"
        and row["method"] == "GET"
        and row["route"] == "/driver/"
        and _sse_qa_http_503_in_fixed_window(
            datetime.strptime(row["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        )
        for row in limit_rows
    ):
        raise ReleaseError("SSE QA HTTP 503 target limit correlation is inconsistent")
    if finding["target_access_seen"] != bool(access_rows):
        raise ReleaseError("SSE QA HTTP 503 target access finding is inconsistent")
    if finding["wsgi_journal_queried"] != sources["wsgi_journal"]["queried"]:
        raise ReleaseError("SSE QA HTTP 503 journal finding is inconsistent")
    correlated = finding["target_limit_correlated"]
    if correlated:
        expected_cause, expected_zone = "limit_conn_sse_qa_per_ip", "sse_qa_per_ip"
    elif report["upstream_events"] or report["wsgi_events"]:
        expected_cause, expected_zone = "upstream_or_application", None
    elif access_rows:
        expected_cause, expected_zone = "other_nginx_or_access_layer", None
    else:
        expected_cause, expected_zone = "not_established", None
    if finding["cause"] != expected_cause or finding["zone"] != expected_zone:
        raise ReleaseError("SSE QA HTTP 503 cause is inconsistent")
    if finding["wsgi_journal_queried"] != (not correlated):
        raise ReleaseError("SSE QA HTTP 503 conditional journal decision is inconsistent")

    summary = report.get("summary")
    row_count = len(access_rows) + len(limit_rows) + len(report["upstream_events"]) + len(report["wsgi_events"])
    if (
        not isinstance(summary, dict)
        or set(summary) != {"row_count", "truncated"}
        or type(summary["row_count"]) is not int
        or summary["row_count"] != row_count
        or row_count > 500
        or type(summary["truncated"]) is not bool
    ):
        raise ReleaseError("SSE QA HTTP 503 summary is invalid")
    if report.get("limitations") != SSE_QA_HTTP_503_LIMITATIONS:
        raise ReleaseError("SSE QA HTTP 503 limitations contract mismatch")
    reject_sensitive_diagnostic_value(report)
    return report


def validate_diagnostic_report(raw: bytes, metadata: dict[str, object]) -> dict[str, Any]:
    if metadata.get("operation") == "infra_capacity_v1":
        return validate_infra_capacity_report(raw, metadata)
    if metadata.get("operation") == "sse_qa_http_503_v1":
        return validate_sse_qa_http_503_report(raw, metadata)
    return validate_trip_diagnostic_report(raw, metadata)


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
    envelope = {
        "schema": 1,
        "kind": "production_diagnostic_ciphertext",
        "operation": report["operation"],
        "summary": report["summary"],
        "ciphertext_format": "CMS-DER",
        "recipient_fingerprint": DIAGNOSTIC_RECIPIENT_FINGERPRINT,
        "ciphertext_sha256": digest(ciphertext),
        "ciphertext_base64": base64.b64encode(ciphertext).decode("ascii"),
    }
    if report["operation"] == "sse_qa_http_503_v1":
        target_line = report["target_access"][0]["sanitized_log_line"] if report["target_access"] else None
        limit_line = next(
            (
                row["sanitized_log_line"]
                for row in report["limit_events"]
                if row["zone"] == "sse_qa_per_ip"
                and row["method"] == "GET"
                and row["route"] == "/driver/"
                and _sse_qa_http_503_in_fixed_window(
                    datetime.strptime(row["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                )
            ),
            None,
        )
        configuration = report["configuration"]
        sources = report["sources"]
        limit_counts = {
            name: sum(1 for row in report["limit_events"] if row["classification"] == name)
            for name in ("ordinary_http", "static", "realtime_stream")
        }
        driver_limit_count = sum(
            1
            for row in report["limit_events"]
            if row["zone"] == "sse_qa_per_ip"
            and row["method"] == "GET"
            and row["route"] == "/driver/"
        )
        envelope["public_evidence"] = {
            "cause": report["finding"]["cause"],
            "zone": report["finding"]["zone"],
            "target_access": target_line,
            "limit_event": limit_line,
            "configuration_status": configuration["status"],
            "ordinary_http_per_ip_limit": configuration["ordinary_http_per_ip_limit"],
            "static_per_ip_limit": configuration["static_per_ip_limit"],
            "realtime_per_ip_limit": configuration["realtime_per_ip_limit"],
            "realtime_total_limit": configuration["realtime_total_limit"],
            "nginx_error_source": (
                f'{sources["nginx_error"]["status"]}:{sources["nginx_error"]["reason"]}'
            ),
            "nginx_error_lines": sources["nginx_error"]["lines_examined"],
            "nginx_access_source": (
                f'{sources["nginx_access"]["status"]}:{sources["nginx_access"]["reason"]}'
            ),
            "nginx_access_lines": sources["nginx_access"]["lines_examined"],
            "nginx_site_source": (
                f'{sources["nginx_site"]["status"]}:{sources["nginx_site"]["reason"]}'
            ),
            "nginx_site_lines": sources["nginx_site"]["lines_examined"],
            "wsgi_source": (
                f'{sources["wsgi_journal"]["status"]}:{sources["wsgi_journal"]["reason"]}'
            ),
            "wsgi_lines": sources["wsgi_journal"]["lines_examined"],
            "nginx_error_offset_minutes": report["time_basis"]["nginx_error_offset_minutes"],
            "target_access_seen": report["finding"]["target_access_seen"],
            "limit_event_count": len(report["limit_events"]),
            "limit_ordinary_count": limit_counts["ordinary_http"],
            "limit_static_count": limit_counts["static"],
            "limit_realtime_count": limit_counts["realtime_stream"],
            "driver_limit_count": driver_limit_count,
        }
    return envelope


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
    if mode in FCM_MODES:
        if path.as_posix() != FCM_PAYLOAD:
            raise ReleaseError(f"FCM configuration target is not allowed: {value}")
        return path
    if mode in SSE_QA_SEED_FIX_MODES:
        if path.as_posix() not in SSE_QA_SEED_FIX_PAYLOADS:
            raise ReleaseError(f"SSE QA seed-fix target is not allowed: {value}")
        return path
    if mode in SSE_QA_MODES:
        allowed = {SSE_QA_PACKAGE_PAYLOAD}
        if mode == "install_sse_qa":
            allowed.add(SSE_QA_SECRETS_PAYLOAD)
        if mode == "prepare_sse_qa_https":
            allowed.add(SSE_QA_ALLOW_CIDR_PAYLOAD)
        if path.as_posix() not in allowed:
            raise ReleaseError(f"SSE QA target is not allowed: {value}")
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
    elif mode in FCM_MODES:
        validate_fcm_payload(manifest, payload)
    elif mode in SSE_QA_SEED_FIX_MODES:
        if set(payload) != SSE_QA_SEED_FIX_PAYLOADS:
            raise ReleaseError("invalid SSE QA seed-fix payload set")
        if metadata != SSE_QA_SEED_FIX_METADATA:
            raise ReleaseError("invalid SSE QA seed-fix metadata")
        for path, expected_sha256 in SSE_QA_SEED_FIX_PAYLOAD_SHA256.items():
            if digest(payload[path]) != expected_sha256:
                raise ReleaseError(f"SSE QA seed-fix payload hash mismatch: {path}")
        for path in (
            SSE_QA_SEED_FIX_BASE_CONTROLLER_PAYLOAD,
            SSE_QA_SEED_FIX_CONTROLLER_PAYLOAD,
            SSE_QA_SEED_FIX_DB_HELPER_PAYLOAD,
            SSE_QA_SEED_COMMAND_PAYLOAD,
            SSE_QA_SEED_TEST_PAYLOAD,
        ):
            try:
                compile(payload[path], path, "exec")
            except SyntaxError as exc:
                raise ReleaseError(f"SSE QA seed-fix Python is invalid: {path}") from exc
    elif mode in SSE_QA_MODES:
        expected = {SSE_QA_PACKAGE_PAYLOAD}
        if mode == "install_sse_qa":
            expected.add(SSE_QA_SECRETS_PAYLOAD)
        if mode == "prepare_sse_qa_https":
            expected.add(SSE_QA_ALLOW_CIDR_PAYLOAD)
        expected_metadata = (
            SSE_QA_HTTPS_METADATA
            if mode in {"inspect_sse_qa_https", "prepare_sse_qa_https"}
            else SSE_QA_METADATA
        )
        if set(payload) != expected or metadata != expected_metadata:
            raise ReleaseError("invalid SSE QA package contract")
        if len(payload[SSE_QA_PACKAGE_PAYLOAD]) > SSE_QA_MAX_PACKAGE_BYTES:
            raise ReleaseError("SSE QA package is too large")
        if mode == "install_sse_qa":
            try:
                secrets = json.loads(payload[SSE_QA_SECRETS_PAYLOAD])
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleaseError("invalid SSE QA secrets envelope") from exc
            if not isinstance(secrets, dict) or secrets.get("schema") != 1:
                raise ReleaseError("invalid SSE QA secrets schema")
        if mode == "prepare_sse_qa_https":
            try:
                allow_cidr = payload[SSE_QA_ALLOW_CIDR_PAYLOAD].decode("ascii")
                network = ipaddress.ip_network(allow_cidr, strict=True)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ReleaseError("invalid SSE QA HTTPS allow_cidr") from exc
            if (
                network.version != 4
                or network.prefixlen != 32
                or str(network) != allow_cidr
            ):
                raise ReleaseError("invalid SSE QA HTTPS allow_cidr")
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


def validate_fcm_payload(manifest: dict[str, Any], payload: dict[str, bytes]) -> dict[str, Any]:
    if set(payload) != {FCM_PAYLOAD}:
        raise ReleaseError("FCM release must contain exactly one service account file")
    try:
        credentials = json.loads(payload[FCM_PAYLOAD].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("FCM service account is invalid") from exc
    required = {"type", "project_id", "private_key_id", "private_key", "client_email", "token_uri"}
    if credentials.get("type") != "service_account" or not all(credentials.get(key) for key in required):
        raise ReleaseError("FCM service account is incomplete")
    project_id = credentials["project_id"]
    if manifest["metadata"].get("project_id") != project_id:
        raise ReleaseError("FCM project id does not match release metadata")
    return credentials


def render_fcm_env(current: bytes, project_id: str) -> bytes:
    try:
        lines = current.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReleaseError("production environment file is not UTF-8") from exc
    replacements = {
        "DJANGO_FCM_SERVICE_ACCOUNT_FILE": str(FCM_CONFIG_PATH),
        "DJANGO_FCM_PROJECT_ID": project_id,
    }
    rendered: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in replacements:
            if key not in seen:
                rendered.append(f"{key}={replacements[key]}")
                seen.add(key)
            continue
        rendered.append(line)
    for key, value in replacements.items():
        if key not in seen:
            rendered.append(f"{key}={value}")
    return ("\n".join(rendered).rstrip("\n") + "\n").encode("utf-8")


def write_atomic(
    target: Path,
    data: bytes,
    uid: int,
    gid: int,
    mode: int = 0o664,
    parent_mode: int = 0o755,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chown(target.parent, uid, gid)
    os.chmod(target.parent, parent_mode)
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
    if metadata["operation"] == "sse_qa_http_503_v1":
        return collect_sse_qa_http_503_report()
    source = (
        INFRA_CAPACITY_SOURCE
        if metadata["operation"] == "infra_capacity_v1"
        else DIAGNOSTIC_QUERY_SOURCE
    )
    if not source.strip():
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
        source,
    ]
    if metadata["operation"] == "trip_accounting_incident_v1":
        command.extend([
            str(metadata["operation"]),
            str(metadata["equipment"]),
            str(metadata["from_utc"]),
            str(metadata["to_utc"]),
            str(metadata["max_rows"]),
        ])
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


def configure_fcm(manifest: dict[str, Any], payload: dict[str, bytes]) -> Path:
    credentials = validate_fcm_payload(manifest, payload)
    if not APP_ENV_PATH.is_file():
        raise ReleaseError("production environment file does not exist")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup = BACKUPS / f"github-{stamp}-{manifest['commit'][:12]}-configure_fcm-before"
    backup.mkdir(parents=True, mode=0o750)
    env_before = APP_ENV_PATH.read_bytes()
    (backup / "environment.before").write_bytes(env_before)
    os.chmod(backup / "environment.before", 0o600)
    config_existed = FCM_CONFIG_PATH.is_file()
    if config_existed:
        (backup / "firebase-service-account.before").write_bytes(FCM_CONFIG_PATH.read_bytes())
        os.chmod(backup / "firebase-service-account.before", 0o600)
    (backup / "metadata.json").write_text(
        json.dumps({"config_existed": config_existed, "project_id": credentials["project_id"]}) + "\n",
        encoding="utf-8",
    )
    env_stat = APP_ENV_PATH.stat()
    uid = 0
    gid = grp.getgrnam("www-data").gr_gid
    try:
        write_atomic(
            FCM_CONFIG_PATH,
            payload[FCM_PAYLOAD],
            uid,
            gid,
            0o640,
            0o750,
        )
        write_atomic(
            APP_ENV_PATH,
            render_fcm_env(env_before, credentials["project_id"]),
            env_stat.st_uid,
            env_stat.st_gid,
            env_stat.st_mode & 0o777,
        )
        run(["systemctl", "restart", "accounting-mvp"])
        wait_for_service()
    except Exception:
        write_atomic(
            APP_ENV_PATH,
            env_before,
            env_stat.st_uid,
            env_stat.st_gid,
            env_stat.st_mode & 0o777,
        )
        if config_existed:
            write_atomic(
                FCM_CONFIG_PATH,
                (backup / "firebase-service-account.before").read_bytes(),
                uid,
                gid,
                0o640,
                0o750,
            )
        else:
            FCM_CONFIG_PATH.unlink(missing_ok=True)
        run(["systemctl", "restart", "accounting-mvp"], check=False)
        raise
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


def _stage_sse_qa_runtime_slice(bundle: Path) -> str:
    """Create the first-install aggregate slice before any QA child starts."""
    source = bundle / "config/systemd/sse-qa.slice"
    if not source.is_file():
        raise ReleaseError("SSE QA slice source is missing")
    if SSE_QA_PERSISTENT_SLICE_PATH.exists() or SSE_QA_RUNTIME_SLICE_PATH.exists():
        raise ReleaseError("SSE QA slice path already exists before install")
    data = source.read_bytes()
    expected_digest = digest(data)
    try:
        SSE_QA_RUNTIME_SLICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SSE_QA_RUNTIME_SLICE_PATH.open("xb") as output:
            output.write(data)
        os.chown(SSE_QA_RUNTIME_SLICE_PATH, 0, 0)
        os.chmod(SSE_QA_RUNTIME_SLICE_PATH, 0o644)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "start", SSE_QA_SLICE_UNIT])
    except BaseException:
        if (
            SSE_QA_RUNTIME_SLICE_PATH.is_file()
            and digest(SSE_QA_RUNTIME_SLICE_PATH.read_bytes()) == expected_digest
        ):
            SSE_QA_RUNTIME_SLICE_PATH.unlink()
            run(["systemctl", "daemon-reload"], check=False)
        raise
    return expected_digest


def _cleanup_sse_qa_runtime_slice(expected_digest: str, *, keep_installed: bool) -> None:
    """Remove only our runtime slice and end its failed first-install lifecycle."""
    if not SSE_QA_RUNTIME_SLICE_PATH.is_file():
        raise ReleaseError("SSE QA runtime slice disappeared")
    if digest(SSE_QA_RUNTIME_SLICE_PATH.read_bytes()) != expected_digest:
        raise ReleaseError("refusing changed SSE QA runtime slice")
    persistent = SSE_QA_PERSISTENT_SLICE_PATH.is_file()
    missing_persistent = keep_installed and not persistent
    errors: list[str] = []
    if not keep_installed or not persistent:
        stopped = run(["systemctl", "stop", SSE_QA_SLICE_UNIT], check=False)
        if stopped.returncode != 0:
            errors.append("bootstrap slice did not stop")
    SSE_QA_RUNTIME_SLICE_PATH.unlink()
    reloaded = run(["systemctl", "daemon-reload"], check=False)
    if reloaded.returncode != 0:
        errors.append("systemd daemon-reload failed")
    if missing_persistent:
        errors.append("successful install left no persistent slice")
    if errors:
        raise ReleaseError("SSE QA slice lifecycle incomplete: " + "; ".join(errors))


def _receiver_unified_cgroup() -> str:
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and controllers == "":
            return path
    raise ReleaseError("receiver unified cgroup is unavailable")


def _sse_qa_slice_cgroup() -> str:
    started = run(["systemctl", "start", SSE_QA_SLICE_UNIT], check=False)
    if started.returncode != 0:
        raise ReleaseError("SSE QA slice could not be activated")
    completed = run(
        ["systemctl", "show", SSE_QA_SLICE_UNIT, "--property", "ControlGroup", "--value"],
        check=False,
    )
    actual = completed.stdout.strip()
    if completed.returncode != 0 or actual != SSE_QA_SLICE_CGROUP:
        raise ReleaseError("SSE QA slice cgroup hierarchy mismatch")
    return actual


def _cgroup_is_at_or_below(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent + "/")


def _terminate_sse_qa_process(
    process: subprocess.Popen[str], mode: str, scoped_unit: str | None = None,
) -> str:
    """Stop a timed-out fixed QA operation and prove that it is no longer active."""
    errors: list[str] = []
    unit = scoped_unit or ("sse-qa-install.service" if mode == "install_sse_qa" else None)
    if unit is not None:
        try:
            stopped = subprocess.run(
                ["/usr/bin/systemctl", "stop", unit],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=100,
            )
            if stopped.returncode != 0:
                errors.append("scoped QA unit stop failed")
        except subprocess.TimeoutExpired:
            errors.append("scoped QA unit stop timed out")
        except OSError:
            errors.append("scoped QA unit stop could not run")

    # The receiver-side systemd-run client is a separate process group.  It is
    # always reaped even when systemctl stop itself fails or times out.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        errors.append("operation process group SIGTERM failed")
    try:
        output, _ = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            errors.append("operation process group SIGKILL failed")
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            output = ""
            errors.append("operation process group did not exit")

    if unit is not None:
        try:
            status = subprocess.run(
                [
                    "/usr/bin/systemctl", "show", unit,
                    "--property", "ActiveState", "--value",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            if status.returncode != 0 or status.stdout.strip() != "inactive":
                errors.append("scoped QA unit is not confirmed inactive")
        except (subprocess.TimeoutExpired, OSError):
            errors.append("scoped QA unit state check failed")
    if errors:
        raise ReleaseError("SSE QA operation timed out; termination not confirmed: " + "; ".join(errors))
    return output


def _verify_sse_qa_seed_fix_overlay() -> None:
    """Fail closed before enabling a QA install that lacks the accepted seed fix."""
    if SSE_QA_OWNERSHIP_PATH.is_symlink() or not SSE_QA_OWNERSHIP_PATH.is_file():
        raise ReleaseError("SSE QA seed-fix ownership overlay is missing")
    try:
        ownership = json.loads(SSE_QA_OWNERSHIP_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("SSE QA seed-fix ownership overlay is invalid") from exc
    if (
        not isinstance(ownership, dict)
        or ownership.get("schema") != "SSE_QA_OWNERSHIP_V2"
        or ownership.get("complete") is not True
        or ownership.get("seed_fix") != SSE_QA_SEED_FIX_OVERLAY
    ):
        raise ReleaseError("SSE QA seed-fix ownership overlay mismatch")
    files = ownership.get("files")
    if not isinstance(files, dict) or (
        files.get(SSE_QA_INSTALLED_SEED_LOGICAL) != SSE_QA_SEED_COMMAND_SHA256
        or files.get(SSE_QA_INSTALLED_SEED_TEST_LOGICAL) != SSE_QA_SEED_TEST_SHA256
    ):
        raise ReleaseError("SSE QA seed-fix ownership file hashes mismatch")
    installed = (
        (SSE_QA_INSTALLED_SEED_PATH, SSE_QA_SEED_COMMAND_SHA256),
        (SSE_QA_INSTALLED_SEED_TEST_PATH, SSE_QA_SEED_TEST_SHA256),
    )
    for path, expected_sha256 in installed:
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"SSE QA seed-fix installed file is missing: {path}")
        try:
            actual_sha256 = digest(path.read_bytes())
        except OSError as exc:
            raise ReleaseError(f"SSE QA seed-fix installed file cannot be read: {path}") from exc
        if actual_sha256 != expected_sha256:
            raise ReleaseError(f"SSE QA seed-fix installed file hash mismatch: {path}")


def run_sse_qa_seed_fix(payload: dict[str, bytes]) -> str:
    """Run the one fixed, hash-pinned in-place repair for the isolated QA seed."""
    bundle_members = {
        SSE_QA_SEED_FIX_BASE_CONTROLLER_PAYLOAD: PurePosixPath("scripts/sse_qa_ctl.py"),
        SSE_QA_SEED_FIX_CONTROLLER_PAYLOAD: PurePosixPath("scripts/sse_qa_seed_fix_ctl.py"),
        SSE_QA_SEED_FIX_DB_HELPER_PAYLOAD: PurePosixPath("scripts/sse_qa_seed_fix_db.py"),
        SSE_QA_SEED_COMMAND_PAYLOAD: PurePosixPath("payload/seed_sse_qa.py"),
        SSE_QA_SEED_TEST_PAYLOAD: PurePosixPath("payload/test_sse_qa_seed.py"),
    }
    if set(payload) != set(bundle_members):
        raise ReleaseError("invalid SSE QA seed-fix payload set")
    with tempfile.TemporaryDirectory(prefix="accounting-sse-qa-seed-fix-") as raw:
        bundle = Path(raw) / "bundle"
        for source_name, member in bundle_members.items():
            target = bundle.joinpath(*member.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload[source_name])
            target.chmod(0o600)
        controller = bundle / "scripts" / "sse_qa_seed_fix_ctl.py"
        receiver_cgroup = _receiver_unified_cgroup()
        qa_slice_cgroup = _sse_qa_slice_cgroup()
        if _cgroup_is_at_or_below(receiver_cgroup, qa_slice_cgroup):
            raise ReleaseError("production receiver must remain outside SSE QA slice")
        scoped_unit = "sse-qa-seed-fix.service"
        command = [
            "/usr/bin/systemd-run", "--system", "--quiet", "--wait", "--pipe", "--collect",
            "--service-type=exec", f"--unit={scoped_unit}",
            f"--slice={SSE_QA_SLICE_UNIT}",
            "--property=CPUQuota=100%", "--property=MemoryMax=1G",
            "--property=MemorySwapMax=0", "--property=TasksMax=128",
            "--property=IOWeight=10", "--property=TimeoutStopSec=90s",
            "/usr/bin/python3.12", str(controller), "repair",
            "--bundle-root", str(bundle),
        ]
        process = subprocess.Popen(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=900)
        except subprocess.TimeoutExpired as exc:
            _terminate_sse_qa_process(process, "repair_sse_qa_seed", scoped_unit)
            raise ReleaseError("SSE QA seed-fix operation timed out; termination confirmed") from exc
        if process.returncode != 0:
            raise ReleaseError("SSE QA seed-fix operation failed: " + output[-2000:])
        lines = output.strip().splitlines()
        summary = lines[-1] if lines else ""
        if SSE_QA_SEED_FIX_SUMMARY.fullmatch(summary) is None:
            raise ReleaseError("SSE QA seed-fix operation returned no fixed summary")
        if _receiver_unified_cgroup() != receiver_cgroup:
            raise ReleaseError("production receiver cgroup changed during SSE QA seed-fix")
        return summary


def run_sse_qa(mode: str, payload: dict[str, bytes]) -> str:
    """Run one fixed QA operation from a strictly validated package."""
    if mode in {"enable_sse_qa", "smoke_sse_qa"}:
        _verify_sse_qa_seed_fix_overlay()
    operation = {
        "verify_sse_qa": "preflight",
        "prepare_sse_qa_host_key": "prepare-host-key",
        "install_sse_qa": "install",
        "inspect_sse_qa_https": "inspect",
        "prepare_sse_qa_https": "prepare",
        "enable_sse_qa": "enable",
        "smoke_sse_qa": "smoke",
        "disable_sse_qa": "disable",
        "remove_sse_qa": "remove",
    }[mode]
    with tempfile.TemporaryDirectory(prefix="accounting-sse-qa-") as raw:
        root = Path(raw)
        package_path = root / "package.zip"
        package_path.write_bytes(payload[SSE_QA_PACKAGE_PAYLOAD])
        with zipfile.ZipFile(package_path) as archive:
            members = archive.infolist()
            if not members or len(members) > SSE_QA_MAX_MEMBERS:
                raise ReleaseError("invalid SSE QA archive member count")
            names: set[str] = set()
            folded_names: set[str] = set()
            total_uncompressed = 0
            for member in members:
                path = PurePosixPath(member.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ReleaseError("unsafe SSE QA archive path")
                if member.filename in names:
                    raise ReleaseError("duplicate SSE QA archive path")
                names.add(member.filename)
                folded = member.filename.casefold()
                if folded in folded_names:
                    raise ReleaseError("case-colliding SSE QA archive path")
                folded_names.add(folded)
                unix_mode = (member.external_attr >> 16) & 0o170000
                if unix_mode not in {0, 0o100000, 0o040000}:
                    raise ReleaseError("SSE QA archive links/devices are forbidden")
                if member.file_size > 50 * 1024 * 1024:
                    raise ReleaseError("SSE QA archive member is too large")
                total_uncompressed += member.file_size
                if total_uncompressed > SSE_QA_MAX_UNCOMPRESSED_BYTES:
                    raise ReleaseError("SSE QA archive expands beyond its limit")
            archive.extractall(root / "bundle")
        https_mode = mode in {"inspect_sse_qa_https", "prepare_sse_qa_https"}
        controller_name = "sse_qa_https_ctl.py" if https_mode else "sse_qa_ctl.py"
        controller = root / "bundle" / "scripts" / controller_name
        checker = root / "bundle" / "scripts" / "package_self_check.py"
        control_only = mode in {
            "prepare_sse_qa_host_key", "enable_sse_qa", "smoke_sse_qa",
            "disable_sse_qa", "remove_sse_qa",
            "inspect_sse_qa_https", "prepare_sse_qa_https",
        }
        if not controller.is_file():
            raise ReleaseError("SSE QA controller is missing")
        expected_controller_sha256 = (
            SSE_QA_HTTPS_CONTROLLER_SHA256 if https_mode else SSE_QA_CONTROLLER_SHA256
        )
        if digest(controller.read_bytes()) != expected_controller_sha256:
            raise ReleaseError("SSE QA controller does not match accepted candidate")
        if control_only:
            extracted_files = {
                path.relative_to(root / "bundle").as_posix()
                for path in (root / "bundle").rglob("*") if path.is_file()
            }
            if extracted_files != {f"scripts/{controller_name}"}:
                raise ReleaseError("SSE QA control package contains unexpected files")
        else:
            runtime = root / "bundle" / "generated" / "runtime.tar.gz"
            if not runtime.is_file() or digest(runtime.read_bytes()) != SSE_QA_RUNTIME_SHA256:
                raise ReleaseError("SSE QA runtime does not match accepted candidate")
            if not checker.is_file():
                raise ReleaseError("SSE QA package checker is missing")
            checked = subprocess.run(
                ["/usr/bin/python3", str(checker), str(root / "bundle")],
                check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=60,
            )
            if checked.returncode != 0 or "PACKAGE_SELF_CHECK_OK" not in checked.stdout:
                raise ReleaseError("SSE QA package self-check failed")
        command = ["/usr/bin/python3", str(controller), operation]
        if not https_mode:
            command.extend(("--bundle-root", str(root / "bundle")))
        operation_input: str | None = None
        if mode == "install_sse_qa":
            try:
                operation_input = payload[SSE_QA_SECRETS_PAYLOAD].decode("utf-8")
            except UnicodeError as exc:
                raise ReleaseError("SSE QA secrets payload is not UTF-8") from exc
            command.append("--secrets-stdin")
        elif mode == "prepare_sse_qa_https":
            try:
                operation_input = payload[SSE_QA_ALLOW_CIDR_PAYLOAD].decode("ascii")
            except UnicodeError as exc:
                raise ReleaseError("SSE QA allow_cidr payload is not ASCII") from exc
            command.append("--allow-cidr-stdin")
        scoped_unit: str | None = None
        if mode == "install_sse_qa":
            scoped_unit = "sse-qa-install.service"
        elif mode == "enable_sse_qa":
            scoped_unit = "sse-qa-enable.service"
        elif mode == "smoke_sse_qa":
            scoped_unit = "sse-qa-smoke.service"
        elif mode == "prepare_sse_qa_https":
            scoped_unit = "sse-qa-https.service"
        runtime_slice_digest = (
            _stage_sse_qa_runtime_slice(root / "bundle")
            if mode == "install_sse_qa" else None
        )
        receiver_cgroup: str | None = None
        try:
            if scoped_unit is not None:
                receiver_cgroup = _receiver_unified_cgroup()
                qa_slice_cgroup = _sse_qa_slice_cgroup()
                if _cgroup_is_at_or_below(receiver_cgroup, qa_slice_cgroup):
                    raise ReleaseError("production receiver must remain outside SSE QA slice")
                properties = ["--property=TimeoutStopSec=90s"]
                if mode == "install_sse_qa":
                    properties = [
                        "--property=CPUQuota=100%", "--property=MemoryMax=2G",
                        "--property=MemorySwapMax=0", "--property=TasksMax=256",
                        "--property=IOWeight=10", *properties,
                    ]
                command = [
                    "/usr/bin/systemd-run", "--system", "--quiet", "--wait", "--pipe", "--collect",
                    "--service-type=exec", f"--unit={scoped_unit}",
                    f"--slice={SSE_QA_SLICE_UNIT}", *properties,
                    "/usr/bin/python3.12", str(controller), operation,
                ]
                if not https_mode:
                    command.extend(("--bundle-root", str(root / "bundle")))
                if mode == "install_sse_qa":
                    command.append("--secrets-stdin")
                elif mode == "prepare_sse_qa_https":
                    command.append("--allow-cidr-stdin")
        except BaseException:
            if runtime_slice_digest is not None:
                _cleanup_sse_qa_runtime_slice(runtime_slice_digest, keep_installed=False)
            raise
        operation_succeeded = False
        try:
            process = subprocess.Popen(
                command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=(subprocess.PIPE if operation_input is not None else subprocess.DEVNULL),
                start_new_session=True,
            )
            try:
                if operation_input is None:
                    output, _ = process.communicate(timeout=900)
                else:
                    output, _ = process.communicate(input=operation_input, timeout=900)
            except subprocess.TimeoutExpired as exc:
                output = _terminate_sse_qa_process(process, mode, scoped_unit)
                raise ReleaseError("SSE QA operation timed out; termination confirmed") from exc
            completed_returncode = process.returncode
            if completed_returncode != 0:
                raise ReleaseError("SSE QA operation failed: " + output[-2000:])
            summary = output.strip().splitlines()[-1]
            if not summary.startswith("SSE_QA_"):
                raise ReleaseError("SSE QA operation returned no fixed summary")
            if receiver_cgroup is not None and _receiver_unified_cgroup() != receiver_cgroup:
                raise ReleaseError("production receiver cgroup changed during SSE QA operation")
            operation_succeeded = True
            return summary
        finally:
            if runtime_slice_digest is not None:
                _cleanup_sse_qa_runtime_slice(
                    runtime_slice_digest, keep_installed=operation_succeeded,
                )


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
            if mode == "verify_sse_qa":
                summary = run_sse_qa(mode, payload)
                print(
                    f"VERIFY_OK mode={mode} commit={manifest['commit']} "
                    f"candidate={SSE_QA_CANDIDATE_COMMIT} "
                    f"controller_sha256={SSE_QA_CONTROLLER_SHA256} "
                    f"runtime_sha256={SSE_QA_RUNTIME_SHA256} "
                    f"files={len(payload)} package_sha256={package_sha} summary={summary}"
                )
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
            elif mode == "configure_fcm":
                backup = configure_fcm(manifest, payload)
            elif mode == "rollback":
                backup = rollback(manifest)
            elif mode in SSE_QA_SEED_FIX_MODES:
                backup = run_sse_qa_seed_fix(payload)
            elif mode in SSE_QA_MODES:
                backup = run_sse_qa(mode, payload)
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
