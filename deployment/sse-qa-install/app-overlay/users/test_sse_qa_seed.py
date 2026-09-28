from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import OperationalStateVersion
from references.models import DumpPoint, Equipment, RockType
from shifts.models import EmployeeShift
from trips.models import OPEN_TRIP_STATUSES, Trip
from users.forms import is_valid_russian_mobile_phone
from users.management.commands.seed_sse_qa import (
    DRIVER_NAME,
    DRIVER_PHONE,
    EXCAVATOR_NAME,
    EXCAVATOR_PHONE,
    EXCAVATOR_NUMBER,
    ROCK_DENSITY,
    TRUCK_BODY_VOLUME_M3,
    TRUCK_NUMBER,
    Command,
    is_expected_trip_event,
)
from users.models import Employee, EmployeeAccess


@override_settings(
    ALLOWED_HOSTS=["sse-qa.driverform.ru"],
    SSE_PILOT_ENABLED=True,
    SSE_ALLOWED_ROLE_CODES=("driver", "excavator_operator"),
    SSE_QA_DRIVER_PIN="135791",
    SSE_QA_EXCAVATOR_PIN="246802",
)
class SseQaSeedBehaviorTests(TestCase):
    pins = {"driver": "135791", "excavator_operator": "246802"}

    def setUp(self):
        self.database_guard = mock.patch.object(Command, "_validate_database_target")
        self.marker_create = mock.patch.object(Command, "_ensure_marker")
        self.marker_verify = mock.patch.object(Command, "_verify_marker")
        self.database_guard.start()
        self.marker_create.start()
        self.marker_verify.start()
        self.addCleanup(self.database_guard.stop)
        self.addCleanup(self.marker_create.stop)
        self.addCleanup(self.marker_verify.stop)
        call_command("seed_sse_qa", stdout=StringIO())

    def login(self, role_code, phone):
        client = Client(HTTP_HOST="sse-qa.driverform.ru")
        response = client.post(
            reverse("login"),
            {
                "phone": phone,
                "access_code": self.pins[role_code],
                "device_kind": "personal",
                "action": "login",
            },
            HTTP_HOST="sse-qa.driverform.ru",
        )
        self.assertEqual(response.status_code, 302)
        return client

    def test_fixture_uses_valid_accounts_and_resolved_trip_measurements(self):
        driver = Employee.objects.get(full_name=DRIVER_NAME)
        excavator_operator = Employee.objects.get(full_name=EXCAVATOR_NAME)
        self.assertEqual(driver.phone, DRIVER_PHONE)
        self.assertEqual(excavator_operator.phone, EXCAVATOR_PHONE)
        self.assertTrue(is_valid_russian_mobile_phone(driver.phone))
        self.assertTrue(is_valid_russian_mobile_phone(excavator_operator.phone))

        truck = Equipment.objects.select_related("model").get(garage_number=TRUCK_NUMBER)
        excavator = Equipment.objects.select_related("model").get(garage_number=EXCAVATOR_NUMBER)
        rock = RockType.objects.get(name="Порода SSE QA")
        self.assertEqual(truck.equipment_type.name, "Самосвал")
        self.assertEqual(excavator.equipment_type.name, "Экскаватор")
        self.assertIsNotNone(truck.model)
        self.assertIsNotNone(excavator.model)
        self.assertEqual(truck.model.body_volume_m3, TRUCK_BODY_VOLUME_M3)
        self.assertEqual(rock.density, ROCK_DENSITY)

        verify_output = StringIO()
        call_command("seed_sse_qa", verify_only=True, stdout=verify_output)
        self.assertIn("SSE_QA_FIXTURE_OK", verify_output.getvalue())

    def test_both_logins_screens_trip_catchup_and_sse_page_use_exact_trip_version(self):
        driver_client = self.login("driver", DRIVER_PHONE)
        excavator_client = self.login("excavator_operator", EXCAVATOR_PHONE)
        self.assertEqual(
            driver_client.get(reverse("driver_work"), HTTP_HOST="sse-qa.driverform.ru").status_code,
            200,
        )
        self.assertEqual(
            excavator_client.get(reverse("excavator_work"), HTTP_HOST="sse-qa.driverform.ru").status_code,
            200,
        )

        truck = Equipment.objects.get(garage_number=TRUCK_NUMBER)
        excavator = Equipment.objects.get(garage_number=EXCAVATOR_NUMBER)
        rock = RockType.objects.get(name="Порода SSE QA")
        dump_point = DumpPoint.objects.get(name="Отвал SSE QA")
        assignment = HaulAssignment.objects.get(
            truck=truck,
            excavator=excavator,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        )
        before = (
            OperationalStateVersion.objects.filter(key="production")
            .values_list("version", flat=True)
            .first()
            or 0
        )
        response = excavator_client.post(
            reverse("excavator_truck_loaded"),
            data={
                "client_action_id": "sse-qa-runtime-behavior",
                "assignment_id": assignment.id,
                "truck_id": truck.id,
                "excavator_id": excavator.id,
                "dump_point_id": dump_point.id,
                "rock_type_id": rock.id,
                "planned_volume_m3": "40.00",
                "transport_distance_km": "1.00",
            },
            content_type="application/json",
            HTTP_HOST="sse-qa.driverform.ru",
        )
        self.assertEqual(response.status_code, 200, response.content)
        response_payload = response.json()
        trip_id = int(response_payload["trip_id"])
        version = int(response_payload["version"])
        self.assertGreater(version, before)
        trip = Trip.objects.get(pk=trip_id, status__in=OPEN_TRIP_STATUSES)
        self.assertEqual(trip.volume_m3, Decimal("40.00"))
        self.assertEqual(trip.tonnage, Decimal("100.00"))

        catchup = driver_client.get(
            f"/realtime/state/?after={before}&include_events=1",
            HTTP_HOST="sse-qa.driverform.ru",
        )
        self.assertEqual(catchup.status_code, 200, catchup.content)
        catchup_payload = catchup.json()
        self.assertGreaterEqual(int(catchup_payload["version"]), version)
        matching = [
            event for event in catchup_payload["events"]
            if is_expected_trip_event(event, trip_id=trip_id, version=version)
        ]
        self.assertEqual(len(matching), 1)
        self.assertNotIn("event_type", matching[0])

        from core.sse import _read_page

        driver_access = EmployeeAccess.objects.get(
            employee__full_name=DRIVER_NAME,
            role__code="driver",
            is_active=True,
        )
        driver_shift = EmployeeShift.objects.get(
            employee=driver_access.employee,
            workplace_code="driver",
            closed_at__isnull=True,
        )
        action, sse_payload, cursor = _read_page(
            {
                "access": driver_access,
                "role_active": True,
                "has_active_shift": True,
                "background_connection_required": True,
                "worker_equipment_ids": [truck.id],
                "own_equipment_ids": [truck.id],
                "role_app_code": "driver",
                "active_shift_id": str(driver_shift.id),
                "session_revision": "seed-behavior-test",
            },
            before,
            50,
        )
        self.assertEqual(action, "page")
        self.assertGreaterEqual(cursor, version)
        self.assertTrue(
            any(
                is_expected_trip_event(event, trip_id=trip_id, version=version)
                for event in sse_payload["events"]
            )
        )


class SseQaSerializedEventMatcherTests(TestCase):
    def test_matcher_rejects_old_wrong_or_legacy_event_shape(self):
        expected = {
            "version": 12,
            "type": "trip_changed",
            "object_type": "Trip",
            "object_id": "34",
            "payload": {"trip_id": 34},
        }
        self.assertTrue(is_expected_trip_event(expected, trip_id=34, version=12))
        for changed in (
            {**expected, "version": 11},
            {**expected, "object_id": "33"},
            {**expected, "payload": {"trip_id": 33}},
            {**expected, "type": "heartbeat"},
            {key: value for key, value in expected.items() if key != "type"} | {"event_type": "trip_changed"},
        ):
            with self.subTest(event=changed):
                self.assertFalse(is_expected_trip_event(changed, trip_id=34, version=12))
