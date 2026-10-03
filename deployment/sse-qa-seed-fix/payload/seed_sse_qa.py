"""Create the two synthetic identities allowed in the isolated SSE QA."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone


MARKER = "SSE_QA_DATABASE_V1"
DRIVER_PHONE = "+79000000001"
EXCAVATOR_PHONE = "+79000000002"
DRIVER_NAME = "SSE QA Водитель 01"
EXCAVATOR_NAME = "SSE QA Экскаваторщик 01"
TRUCK_NUMBER = "SSE-QA-TRUCK-01"
EXCAVATOR_NUMBER = "SSE-QA-EXC-01"
TRUCK_MODEL_NAME = "SSE QA Самосвал 01"
EXCAVATOR_MODEL_NAME = "SSE QA Экскаватор 01"
TRUCK_BODY_VOLUME_M3 = Decimal("40.00")
ROCK_NAME = "Скальная порода"
ROCK_DENSITY = Decimal("2.7100")
ROCK_LOOSENING_FACTOR = Decimal("1.5100")


def is_expected_trip_event(event, *, trip_id, version):
    """Match the one journal event produced for this smoke-test trip."""
    if not isinstance(event, dict) or event.get("type") != "trip_changed":
        return False
    try:
        event_version = int(event.get("version"))
        expected_version = int(version)
    except (TypeError, ValueError):
        return False
    payload = event.get("payload")
    return (
        event_version == expected_version
        and str(event.get("object_type") or "") == "Trip"
        and str(event.get("object_id") or "") == str(trip_id)
        and isinstance(payload, dict)
        and str(payload.get("trip_id") or "") == str(trip_id)
    )


class Command(BaseCommand):
    help = "Seed an empty isolated SSE QA database with synthetic identities."

    def add_arguments(self, parser):
        parser.add_argument("--verify-only", action="store_true")
        parser.add_argument("--business-smoke", action="store_true")

    def _validate_database_target(self):
        settings_dict = connection.settings_dict
        if connection.vendor != "postgresql":
            raise CommandError("SSE QA seed requires PostgreSQL")
        if settings_dict.get("NAME") != "sseqa":
            raise CommandError("refusing non-QA database")
        if str(settings_dict.get("HOST")) not in {"127.0.0.1", "localhost"}:
            raise CommandError("refusing non-loopback PostgreSQL")
        if str(settings_dict.get("PORT")) != "55432":
            raise CommandError("refusing unexpected PostgreSQL port")

    def _verify_marker(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT identity FROM sse_qa_install_marker WHERE identity = %s",
                [MARKER],
            )
            if cursor.fetchone() != (MARKER,):
                raise CommandError("SSE QA database marker is missing")

    def _ensure_marker(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS sse_qa_install_marker "
                "(identity text PRIMARY KEY CHECK (identity = %s))",
                [MARKER],
            )
            cursor.execute(
                "INSERT INTO sse_qa_install_marker(identity) VALUES (%s) "
                "ON CONFLICT (identity) DO NOTHING",
                [MARKER],
            )

    def handle(self, *args, **options):
        self._validate_database_target()

        pins = {
            "driver": str(getattr(settings, "SSE_QA_DRIVER_PIN", "")).strip(),
            "excavator_operator": str(
                getattr(settings, "SSE_QA_EXCAVATOR_PIN", "")
            ).strip(),
        }
        if any(len(pin) != 6 or not pin.isdigit() for pin in pins.values()):
            raise CommandError("both QA PIN values must contain exactly six digits")
        if pins["driver"] == pins["excavator_operator"]:
            raise CommandError("QA PIN values must differ")

        from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
        from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType
        from shifts.models import EmployeeShift, ShiftType
        from users.models import Employee, EmployeeAccess, Role
        from users.forms import is_valid_russian_mobile_phone

        if options["verify_only"]:
            self._verify_marker()
            roles_count = Role.objects.filter(code__in=pins).count()
            employees_qs = Employee.objects.filter(
                full_name__in=(DRIVER_NAME, EXCAVATOR_NAME),
            )
            employees = {item.full_name: item for item in employees_qs}
            equipment_qs = Equipment.objects.filter(
                garage_number__in=(TRUCK_NUMBER, EXCAVATOR_NUMBER),
            ).select_related("equipment_type", "model")
            equipment = {item.garage_number: item for item in equipment_qs}
            shifts_count = EmployeeShift.objects.filter(
                employee__in=employees_qs, closed_at__isnull=True,
            ).count()
            assignments_count = HaulAssignment.objects.filter(
                truck__garage_number=TRUCK_NUMBER,
                excavator__garage_number=EXCAVATOR_NUMBER,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
            ).count()
            rock = RockType.objects.filter(name=ROCK_NAME).first()
            if (
                roles_count != 2 or employees_qs.count() != 2 or len(equipment) != 2
                or shifts_count != 2 or assignments_count != 1
                or equipment[TRUCK_NUMBER].equipment_type.name != "Самосвал"
                or equipment[EXCAVATOR_NUMBER].equipment_type.name != "Экскаватор"
                or equipment[TRUCK_NUMBER].model is None
                or equipment[TRUCK_NUMBER].model.name != TRUCK_MODEL_NAME
                or equipment[TRUCK_NUMBER].model.body_volume_m3 != TRUCK_BODY_VOLUME_M3
                or equipment[EXCAVATOR_NUMBER].model is None
                or equipment[EXCAVATOR_NUMBER].model.name != EXCAVATOR_MODEL_NAME
                or rock is None or rock.density != ROCK_DENSITY
                or rock.loosening_factor != ROCK_LOOSENING_FACTOR
                or employees.get(DRIVER_NAME) is None
                or employees.get(DRIVER_NAME).phone != DRIVER_PHONE
                or employees.get(EXCAVATOR_NAME) is None
                or employees.get(EXCAVATOR_NAME).phone != EXCAVATOR_PHONE
                or not all(is_valid_russian_mobile_phone(item.phone) for item in employees.values())
            ):
                raise CommandError("SSE QA fixture is missing or incompatible")
            for code, label in (("driver", DRIVER_NAME), ("excavator_operator", EXCAVATOR_NAME)):
                if not EmployeeAccess.objects.filter(
                    employee__full_name=label,
                    role__code=code,
                    access_code=pins[code],
                    status=EmployeeAccess.Status.ACTIVATED,
                    is_active=True,
                ).exists():
                    raise CommandError(f"SSE QA access mismatch: {code}")
            self.stdout.write("SSE_QA_FIXTURE_OK roles=2 employees=2 equipment=2 models=2 open_shifts=2 assignments=1 canonical_types=2 measurements=2 phones=2")
            return

        with transaction.atomic():
            self._ensure_marker()

            roles = {
                code: Role.objects.update_or_create(
                    code=code,
                    defaults={"name": name, "is_active": True},
                )[0]
                for code, name in (
                    ("driver", "Водитель SSE QA"),
                    ("excavator_operator", "Машинист экскаватора SSE QA"),
                )
            }
            # Runtime role screens use these canonical names in exact filters.
            # Isolation is expressed by the database, garage numbers and users,
            # never by changing a domain discriminator.
            truck_type, _ = EquipmentType.objects.get_or_create(name="Самосвал")
            excavator_type, _ = EquipmentType.objects.get_or_create(name="Экскаватор")
            truck_model, _ = EquipmentModel.objects.update_or_create(
                equipment_type=truck_type,
                name=TRUCK_MODEL_NAME,
                defaults={
                    "payload_tons": Decimal("100.00"),
                    "body_volume_m3": TRUCK_BODY_VOLUME_M3,
                    "is_active": True,
                },
            )
            excavator_model, _ = EquipmentModel.objects.update_or_create(
                equipment_type=excavator_type,
                name=EXCAVATOR_MODEL_NAME,
                defaults={
                    "body_volume_m3": Decimal("10.00"),
                    "is_active": True,
                },
            )
            truck, _ = Equipment.objects.update_or_create(
                garage_number=TRUCK_NUMBER,
                defaults={
                    "equipment_type": truck_type,
                    "model": truck_model,
                    "is_active": True,
                },
            )
            excavator, _ = Equipment.objects.update_or_create(
                garage_number=EXCAVATOR_NUMBER,
                defaults={
                    "equipment_type": excavator_type,
                    "model": excavator_model,
                    "is_active": True,
                },
            )
            dump_point, _ = DumpPoint.objects.get_or_create(name="Отвал SSE QA")
            rock, _ = RockType.objects.update_or_create(
                name=ROCK_NAME,
                defaults={
                    "density": ROCK_DENSITY,
                    "loosening_factor": ROCK_LOOSENING_FACTOR,
                    "is_active": True,
                },
            )
            ExcavatorPlacement.objects.update_or_create(
                excavator=excavator,
                defaults={
                    "zone": ExcavatorPlacement.Zone.ACTIVE,
                    "work_rock_type": rock,
                    "work_dump_point": dump_point,
                },
            )

            employees = {}
            for code, label, phone, equipment in (
                ("driver", DRIVER_NAME, DRIVER_PHONE, truck),
                ("excavator_operator", EXCAVATOR_NAME, EXCAVATOR_PHONE, excavator),
            ):
                employee, _ = Employee.objects.update_or_create(
                    full_name=label,
                    defaults={
                        "phone": phone,
                        "status": Employee.Status.ACTIVE,
                        "is_active": True,
                        "comment": "Synthetic isolated SSE QA identity; no personal data.",
                    },
                )
                employees[code] = employee
                access, _ = EmployeeAccess.objects.get_or_create(
                    employee=employee,
                    role=roles[code],
                    defaults={
                        "access_code": pins[code],
                        "status": EmployeeAccess.Status.ACTIVATED,
                        "is_active": True,
                    },
                )
                changed = []
                if access.access_code != pins[code]:
                    access.access_code = pins[code]
                    changed.append("access_code")
                if not access.is_active:
                    access.is_active = True
                    changed.append("is_active")
                if access.status != EmployeeAccess.Status.ACTIVATED:
                    access.status = EmployeeAccess.Status.ACTIVATED
                    changed.append("status")
                if changed:
                    access.save(update_fields=changed)
                EmployeeShift.objects.get_or_create(
                    employee=employee,
                    workplace_code=code,
                    equipment=equipment,
                    closed_at__isnull=True,
                    defaults={
                        "shift_type": ShiftType.DAY,
                        "opened_at": timezone.now(),
                    },
                )
            HaulAssignment.objects.get_or_create(
                truck=truck,
                excavator=excavator,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
                defaults={
                    "assigned_by": employees["excavator_operator"],
                    "accepted_at": timezone.now(),
                },
            )

        expected = {
            "roles": Role.objects.filter(code__in=roles).count(),
            "employees": Employee.objects.filter(full_name__in=(DRIVER_NAME, EXCAVATOR_NAME)).count(),
            "equipment": Equipment.objects.filter(garage_number__in=(TRUCK_NUMBER, EXCAVATOR_NUMBER)).count(),
            "open_shifts": EmployeeShift.objects.filter(employee__in=employees.values(), closed_at__isnull=True).count(),
            "assignments": HaulAssignment.objects.filter(truck=truck, excavator=excavator, status=AssignmentStatus.ACCEPTED, ended_at__isnull=True).count(),
        }
        if expected != {"roles": 2, "employees": 2, "equipment": 2, "open_shifts": 2, "assignments": 1}:
            raise CommandError(f"SSE QA fixture mismatch: {expected}")
        if truck.equipment_type.name != "Самосвал" or excavator.equipment_type.name != "Экскаватор":
            raise CommandError("SSE QA equipment types are not canonical")

        if options["business_smoke"]:
            clients = {}
            for code, phone in (("driver", DRIVER_PHONE), ("excavator_operator", EXCAVATOR_PHONE)):
                client = Client(HTTP_HOST="sse-qa.driverform.ru")
                response = client.post(
                    reverse("login"),
                    {"phone": phone, "access_code": pins[code], "device_kind": "personal", "action": "login"},
                    HTTP_HOST="sse-qa.driverform.ru",
                )
                if response.status_code != 302:
                    raise CommandError(f"QA {code} login failed: HTTP {response.status_code}")
                screen = reverse("driver_work") if code == "driver" else reverse("excavator_work")
                response = client.get(screen, HTTP_HOST="sse-qa.driverform.ru")
                if response.status_code != 200:
                    raise CommandError(f"QA {code} screen failed: HTTP {response.status_code}")
                clients[code] = client

            from core.models import OperationalStateVersion
            from trips.models import OPEN_TRIP_STATUSES, Trip

            before = OperationalStateVersion.objects.filter(key="production").values_list("version", flat=True).first() or 0
            existing_trip = Trip.objects.filter(truck=truck, status__in=OPEN_TRIP_STATUSES).first()
            sse_result = {"events": [], "error": ""}
            cookie_header = "; ".join(
                f"{name}={morsel.value}" for name, morsel in clients["driver"].cookies.items()
            )

            def read_sse():
                request = urllib.request.Request(
                    f"http://127.0.0.1:18082/realtime/stream/?after={before}",
                    headers={"Host": "sse-qa.driverform.ru", "Cookie": cookie_header, "Accept": "text/event-stream"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=20) as stream:
                        for raw_line in stream:
                            line = raw_line.decode("utf-8").strip()
                            if not line.startswith("data:"):
                                continue
                            payload = json.loads(line[5:].strip())
                            events = payload.get("events", []) if isinstance(payload, dict) else []
                            new_trip_events = [
                                event for event in events
                                if isinstance(event, dict)
                                and event.get("type") == "trip_changed"
                                and int(event.get("version") or 0) > before
                            ]
                            if new_trip_events:
                                sse_result["events"].extend(new_trip_events)
                                return
                except Exception as exc:  # surfaced below without credentials
                    sse_result["error"] = type(exc).__name__

            sse_thread = threading.Thread(target=read_sse, name="sse-qa-smoke", daemon=True)
            sse_thread.start()
            time.sleep(0.5)
            response = clients["excavator_operator"].post(
                reverse("excavator_truck_loaded"),
                data=json.dumps({
                    "client_action_id": f"sse-qa-smoke-{uuid.uuid4()}",
                    "assignment_id": HaulAssignment.objects.get(
                        truck=truck, excavator=excavator, status=AssignmentStatus.ACCEPTED, ended_at__isnull=True,
                    ).id,
                    "truck_id": truck.id,
                    "excavator_id": excavator.id,
                    "dump_point_id": dump_point.id,
                    "rock_type_id": rock.id,
                    "planned_volume_m3": "10.00",
                    "transport_distance_km": "1.00",
                    "expected_open_trip_id": existing_trip.id if existing_trip else None,
                }),
                content_type="application/json",
                HTTP_HOST="sse-qa.driverform.ru",
            )
            response_payload = response.json() if response.status_code == 200 else {}
            if response.status_code != 200 or not response_payload.get("ok", False):
                raise CommandError(f"synthetic trip creation failed: HTTP {response.status_code} {response.content[:300]!r}")
            try:
                trip_id = int(response_payload["trip_id"])
                new_version = int(response_payload["version"])
            except (KeyError, TypeError, ValueError) as exc:
                raise CommandError("synthetic trip response is missing trip_id/version") from exc
            if new_version <= before:
                raise CommandError("synthetic trip did not advance production version")
            if not Trip.objects.filter(
                pk=trip_id,
                truck=truck,
                excavator=excavator,
                status__in=OPEN_TRIP_STATUSES,
            ).exists():
                raise CommandError("synthetic trip response does not identify the created open trip")
            catchup = clients["driver"].get(
                f"/realtime/state/?after={before}&include_events=1",
                HTTP_HOST="sse-qa.driverform.ru",
            )
            if catchup.status_code != 200:
                raise CommandError(f"driver realtime catch-up failed: HTTP {catchup.status_code}")
            payload = catchup.json()
            if int(payload.get("version") or 0) < new_version or not any(
                is_expected_trip_event(event, trip_id=trip_id, version=new_version)
                for event in payload.get("events", [])
            ):
                raise CommandError("new synthetic trip/version was not delivered to Driver catch-up")
            sse_thread.join(timeout=20)
            if not any(
                is_expected_trip_event(event, trip_id=trip_id, version=new_version)
                for event in sse_result["events"]
            ):
                raise CommandError(f"new synthetic trip/version was not delivered by SSE: {sse_result['error'] or 'timeout'}")
            self.stdout.write(
                f"SSE_QA_BUSINESS_SMOKE_OK logins=2 screens=2 trip_id={trip_id} "
                f"version={new_version} catchup=1 sse=1"
            )

        self.stdout.write("SSE_QA_SEED_OK roles=2 employees=2 equipment=2 open_shifts=2 assignments=1 pii=0")
