import base64
import json
from datetime import timedelta
from uuid import uuid4

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from django.test import TestCase
from django.utils import timezone

from core.models import OfflineFieldEvent
from references.models import Equipment, EquipmentType, DumpPoint, RockType
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role
from .field_signing import PROTOCOL, ENROLLMENT
from .models import HaulAssignment, ExcavatorPlacement
from . import test_field_sync as legacy_tests


def enc(v):
    return base64.urlsafe_b64encode(v).decode().rstrip("=")


class MasterFieldV2Tests(TestCase):
    login = legacy_tests.MasterFieldSyncTests.login

    def setUp(self):
        self.role, _ = Role.objects.get_or_create(
            code="mining_master", defaults={"name": "Горный мастер"}
        )
        self.employee = Employee.objects.create(
            full_name="Мастер тестовый", phone="+79991234567", status=Employee.Status.ACTIVE
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.role,
            access_code="111222",
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.truck = Equipment.objects.create(
            equipment_type=EquipmentType.objects.create(name="Самосвал"), garage_number="31"
        )
        self.exc = Equipment.objects.create(
            equipment_type=EquipmentType.objects.create(name="Экскаватор"), garage_number="1"
        )
        self.device = str(uuid4())
        self.url = "/mining-master/tablet/field-sync/"
        self.local = str(uuid4())
        self.signer = ec.generate_private_key(ec.SECP256R1())
        self.login(self.access)
        self.grant = self.enroll(self.signer).json()
        self.now = timezone.now() - timedelta(seconds=30)

    def enroll(self, signer):
        pt = signer.public_key().public_numbers()
        key = {
            "kty": "EC",
            "crv": "P-256",
            "x": enc(pt.x.to_bytes(32, "big")),
            "y": enc(pt.y.to_bytes(32, "big")),
        }
        return self.client.post(
            self.url,
            {"op": "enroll", "protocol": PROTOCOL, "device_id": self.device, "public_key": key},
            content_type="application/json",
        )

    def event(self, kind, seq, payload=None, base=None, deps=None):
        actor = self.grant["snapshot"]["actor"]
        return {
            "id": str(uuid4()),
            "type": kind,
            "sequence": seq,
            "device_id": self.device,
            "format_version": 2,
            "occurred_at": (self.now + timedelta(seconds=seq)).isoformat(),
            "actor_key": actor["key"],
            "actor_id": actor["id"],
            "actor_access_id": actor["access_id"],
            "actor_generation": actor["generation"],
            "local_shift_id": self.local,
            "server_shift_id": None,
            "depends_on": deps or [],
            "payload": payload or {},
            "base_payload": base,
        }

    def envelope(self, e):
        raw = json.dumps(e, separators=(",", ":"), ensure_ascii=False).encode()
        der = self.signer.sign(raw, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return {
            "event": e,
            "protocol": PROTOCOL,
            "ticket": self.grant["ticket"],
            "signed_bytes": enc(raw),
            "signature": enc(r.to_bytes(32, "big") + s.to_bytes(32, "big")),
        }

    def post(self, e):
        return self.client.post(self.url, self.envelope(e), content_type="application/json").json()

    def open(self):
        e = self.event("shift.open", 1)
        r = self.post(e)
        self.assertEqual(r["status"], "applied", r.get("message"))
        return e

    def test_pinned_registration_and_no_unsigned_downgrade(self):
        self.assertEqual(self.grant["protocol"], PROTOCOL)
        self.assertTrue(self.grant["grant_until"])
        self.assertTrue(self.grant["snapshot"]["cards"])
        self.assertEqual(self.enroll(self.signer).status_code, 200)
        self.assertEqual(self.enroll(ec.generate_private_key(ec.SECP256R1())).status_code, 409)
        self.assertEqual(OfflineFieldEvent.objects.filter(event_type=ENROLLMENT).count(), 1)
        e = self.event("shift.open", 1)
        e["format_version"] = 1
        r = self.client.post(
            self.url, {"event": e, "ticket": self.grant["ticket"]}, content_type="application/json"
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(EmployeeShift.objects.exists())

    def test_tamper_and_device_key_mismatch_do_not_change_business_state(self):
        e = self.event("shift.open", 1)
        body = self.envelope(e)
        body["event"]["occurred_at"] = timezone.now().isoformat()
        self.assertEqual(
            self.client.post(self.url, body, content_type="application/json").status_code, 400
        )
        self.signer = ec.generate_private_key(ec.SECP256R1())
        body = self.envelope(e)
        self.assertEqual(
            self.client.post(self.url, body, content_type="application/json").status_code, 403
        )
        self.assertFalse(EmployeeShift.objects.exists())

    def test_fifo_retry_and_signed_conflict_resolution(self):
        opened = self.open()
        close = self.event("shift.close", 3)
        self.assertEqual(self.post(close)["code"], "previous_event_required")
        bad = self.event(
            "live.assign",
            2,
            {
                "action": "assign",
                "truck_id": self.truck.pk,
                "excavator_id": self.exc.pk,
                "expected_assignment_state_id": 456,
            },
        )
        r = self.post(bad)
        self.assertEqual(r["status"], "conflict", r)
        resolution = self.event(
            "conflict.resolve", 4, {"decision": "accept_server", "event_id": bad["id"]}
        )
        r = self.post(resolution)
        self.assertEqual(r["status"], "applied", r)
        self.assertEqual(r["resolved_event_id"], bad["id"])
        self.assertEqual(self.post(close)["status"], "applied")
        self.assertEqual(EmployeeShift.objects.get().closed_at, self.now + timedelta(seconds=3))
        self.assertTrue(self.post(opened)["deduplicated"])
        self.assertFalse(HaulAssignment.objects.exists())

    def test_assign_release_and_no_duplicate_after_lost_ack(self):
        first = self.open()
        e = self.event(
            "live.assign",
            2,
            {
                "action": "assign",
                "truck_id": self.truck.pk,
                "excavator_id": self.exc.pk,
                "expected_assignment_state_id": 0,
            },
            deps=[first["id"]],
        )
        r = self.post(e)
        self.assertEqual(r["status"], "applied", r.get("message"))
        assignment = HaulAssignment.objects.get(truck=self.truck)
        self.assertEqual(assignment.assigned_at, self.now + timedelta(seconds=2))
        self.assertTrue(self.post(e)["deduplicated"])
        r = self.post(
            self.event(
                "live.assign",
                3,
                {
                    "action": "release",
                    "truck_id": self.truck.pk,
                    "expected_assignment_state_id": assignment.pk,
                },
                deps=[e["id"]],
            )
        )
        self.assertEqual(r["status"], "applied", r.get("message"))
        receipt = OfflineFieldEvent.objects.get(event_id=e["id"])
        self.assertIn("signature", receipt.context_snapshot)
        self.assertLess(receipt.occurred_at, receipt.received_at)

    def test_card_changes_preserve_concurrent_fields(self):
        self.open()
        rock = RockType.objects.create(name="Руда")
        point = DumpPoint.objects.create(name="Пункт")
        p = {
            "category": "complex",
            "equipment_id": self.exc.pk,
            "loading_horizon": "120",
            "loading_block": "17",
            "rock_type_id": rock.pk,
            "destinations": [{"dump_point_id": point.pk, "transport_distance_km": "2.50"}],
        }
        base = {
            "loading_horizon": "",
            "loading_block": "",
            "rock_type_id": None,
            "destinations": [],
        }
        r = self.post(self.event("card.settings", 2, p, base))
        self.assertEqual(r["status"], "applied", r.get("message"))
        self.assertEqual(ExcavatorPlacement.objects.get(excavator=self.exc).loading_block, "17")
        r = self.post(self.event("card.settings", 3, {**p, "loading_block": "18"}, base))
        self.assertEqual(r["code"], "settings_changed")
        self.assertEqual(ExcavatorPlacement.objects.get(excavator=self.exc).loading_block, "17")

    def test_snapshot_and_ack_under_other_master_session(self):
        self.open()
        person = Employee.objects.create(
            full_name="Мастер два", phone="+79991110000", status=Employee.Status.ACTIVE
        )
        access = EmployeeAccess.objects.create(
            employee=person,
            role=self.role,
            access_code="112244",
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.login(access)
        r = self.post(self.event("shift.close", 2))
        self.assertEqual(r["status"], "applied", r)
        self.assertEqual(r["snapshot"]["actor"]["id"], self.employee.pk)
        live = self.client.get(self.url, {"device_id": self.device, "protocol": PROTOCOL}).json()
        self.assertEqual(live["snapshot"]["actor"]["id"], person.pk)
        self.assertFalse(EmployeeShift.objects.filter(employee=person).exists())

    def test_group_move_disband_and_chained_local_tokens(self):
        first = self.open()
        add = self.event(
            "live.move",
            2,
            {"excavator_id": self.exc.pk, "expected_zone": "inactive", "zone": "active"},
            deps=[first["id"]],
        )
        result = self.post(add)
        self.assertEqual(result["status"], "applied", result.get("message"))
        assign = self.event(
            "live.assign",
            3,
            {
                "action": "assign",
                "truck_id": self.truck.pk,
                "excavator_id": self.exc.pk,
                "expected_assignment_state_id": 0,
            },
            deps=[add["id"]],
        )
        self.assertEqual(self.post(assign)["status"], "applied")
        token = HaulAssignment.objects.get(truck=self.truck).pk
        base = {"excavators": [{"excavator_id": self.exc.pk, "truck_ids": [self.truck.pk]}]}
        disband = self.event(
            "live.move",
            4,
            {
                "excavator_id": self.exc.pk,
                "expected_zone": "active",
                "zone": "inactive",
                "expected_assignment_states": {str(self.truck.pk): 0},
            },
            base=base,
            deps=[assign["id"]],
        )
        r = self.post(disband)
        self.assertEqual(r["status"], "applied", r.get("message"))
        self.assertEqual(ExcavatorPlacement.objects.get(excavator=self.exc).zone, "inactive")
        self.assertNotEqual(r["snapshot"]["live_tokens"]["trucks"][str(self.truck.pk)]["id"], token)
        self.assertTrue(self.post(disband)["deduplicated"])

    def test_downtime_closes_at_device_time_with_one_receipt(self):
        from downtimes.models import DowntimeEvent, DowntimeReason

        self.open()
        reason = DowntimeReason.objects.create(
            name="Причина QA v2", equipment_type=self.truck.equipment_type
        )
        down = DowntimeEvent.objects.create(
            equipment=self.truck, reason=reason, started_at=self.now - timedelta(minutes=1)
        )
        e = self.event(
            "downtime.close", 2, {"downtime_id": down.pk}, base={"equipment_id": self.truck.pk}
        )
        r = self.post(e)
        self.assertEqual(r["status"], "applied", r.get("message"))
        down.refresh_from_db()
        self.assertEqual(down.ended_at, self.now + timedelta(seconds=2))
        self.assertEqual(self.post(e)["snapshot"]["actor"]["id"], self.employee.pk)

    def test_trip_cancel_and_complete_timestamps(self):
        from trips.models import Trip, TripStatus

        self.open()
        worker = Employee.objects.create(full_name="Водитель", status=Employee.Status.ACTIVE)
        driver = EmployeeShift.objects.create(
            employee=worker,
            shift_type="day",
            workplace_code="driver",
            equipment=self.truck,
            opened_at=self.now - timedelta(minutes=2),
        )
        rock = RockType.objects.create(name="Руда")
        point = DumpPoint.objects.create(name="Пункт")

        def trip():
            return Trip.objects.create(
                excavator=self.exc,
                truck=self.truck,
                rock_type=rock,
                dump_point=point,
                assigned_dump_point=point,
                actual_dump_point=point,
                volume_m3="47",
                tonnage="117.5",
                loaded_at=self.now - timedelta(minutes=1),
                status=TripStatus.LOADED_WAITING_UNLOAD,
            )

        a = trip()
        e = self.event("trip.cancel", 2, {"trip_id": a.pk, "reason": "Ошибка назначения"})
        r = self.post(e)
        self.assertEqual(r["status"], "applied", r.get("message"))
        a.refresh_from_db()
        self.assertEqual(a.cancelled_at, self.now + timedelta(seconds=2))
        b = trip()
        r = self.post(
            self.event("trip.complete", 3, {"trip_id": b.pk, "reason": "Подтверждено водителем"})
        )
        self.assertEqual(r["status"], "applied", r.get("message"))
        b.refresh_from_db()
        self.assertEqual(b.completed_at, self.now + timedelta(seconds=3))
        event = self.event(
            "worker_shift.close",
            4,
            {"shift_id": driver.pk, "reason": "Смена окончена", "close_kind": "coordinated"},
            base={"open_trip_ids": [], "equipment_id": self.truck.pk, "employee_id": worker.pk},
        )
        r = self.post(event)
        self.assertEqual(r["status"], "applied", r.get("message"))
        driver.refresh_from_db()
        self.assertEqual(driver.closed_at, self.now + timedelta(seconds=4))

    def test_template_save_and_stale_edit(self):
        from reports.models import ReportTemplate
        from reports.views import VOLUME_REPORT_COLUMNS

        self.open()
        columns = list(VOLUME_REPORT_COLUMNS)[:2]
        data = {"name": "Полевой тест", "columns": columns, "is_active": "on", "group_by": ""}
        e = self.event("report.template_save", 2, data)
        r = self.post(e)
        self.assertEqual(r["status"], "applied", r.get("message"))
        obj = ReportTemplate.objects.get(name=data["name"])
        obj.name = "Серверная правка"
        obj.save()
        r = self.post(
            self.event(
                "report.template_save",
                3,
                {**data, "name": "Новая версия", "template_id": obj.pk},
                base={**data},
            )
        )
        self.assertEqual(r["code"], "template_changed")
        obj.refresh_from_db()
        self.assertEqual(obj.name, "Серверная правка")

    def test_predecessor_cannot_rebase_over_external_reassignment(self):
        from .services import schedule_haul_assignment

        self.open()
        first = self.event(
            "live.assign",
            2,
            {
                "action": "assign",
                "truck_id": self.truck.pk,
                "excavator_id": self.exc.pk,
                "expected_assignment_state_id": 0,
            },
        )
        self.assertEqual(self.post(first)["status"], "applied")
        current = HaulAssignment.objects.get(truck=self.truck)
        other = Equipment.objects.create(equipment_type=self.exc.equipment_type, garage_number="2")
        schedule_haul_assignment(
            truck=self.truck,
            excavator=other,
            assigned_by=self.employee,
            expected_state_id=current.pk,
        )
        base = {"excavators": [{"excavator_id": self.exc.pk, "truck_ids": [self.truck.pk]}]}
        second = self.event(
            "live.assign",
            3,
            {"action": "release", "truck_id": self.truck.pk, "expected_assignment_state_id": 0},
            base=base,
            deps=[first["id"]],
        )
        r = self.post(second)
        self.assertEqual(r["status"], "conflict")

    def test_manual_trip_correction_and_assignment_cancel(self):
        from references.models import EquipmentModel
        from trips.models import Trip
        from core.production_time import production_shift_context

        self.open()
        worker = Employee.objects.create(full_name="Водитель теста", status=Employee.Status.ACTIVE)
        EmployeeShift.objects.create(
            employee=worker,
            shift_type="day",
            workplace_code="driver",
            equipment=self.truck,
            opened_at=self.now - timedelta(minutes=4),
        )
        self.truck.model = EquipmentModel.objects.create(
            name="Тестовая модель",
            equipment_type=self.truck.equipment_type,
            body_volume_m3="44",
            payload_tons="100",
        )
        self.truck.save(update_fields=["model"])
        rock = RockType.objects.create(name="Руда QA", density="2.4", loosening_factor="1.2")
        point = DumpPoint.objects.create(name="Точка QA")
        a = self.event(
            "live.assign",
            2,
            {
                "action": "assign",
                "truck_id": self.truck.pk,
                "excavator_id": self.exc.pk,
                "expected_assignment_state_id": 0,
            },
        )
        self.assertEqual(self.post(a)["status"], "applied")
        job = self.event(
            "trip.manual",
            3,
            {
                "truck_id": self.truck.pk,
                "excavator_id": self.exc.pk,
                "dump_point_id": point.pk,
                "rock_type_id": rock.pk,
                "trips_count": 1,
                "reason": "Подтверждение водителя",
            },
            deps=[a["id"]],
        )
        r = self.post(job)
        self.assertEqual(r["status"], "applied", r.get("message"))
        trip = Trip.objects.get()
        self.assertEqual(trip.completed_at, self.now + timedelta(seconds=3))
        self.assertTrue(self.post(job)["deduplicated"])
        self.assertEqual(Trip.objects.count(), 1)
        span = production_shift_context(trip.completed_at)
        fields = (
            "volume_m3",
            "transport_distance_km",
            "loading_horizon",
            "loading_block",
            "downtime_text",
            "note",
            "rock_type_id",
            "actual_dump_point_id",
        )
        before = {k: str(getattr(trip, k) or "") for k in fields}
        correction = {
            **before,
            "trip_id": trip.pk,
            "shift_type": span.shift_type,
            "date": span.production_date.isoformat(),
            "note": "Уточнение по рации",
            "correction_reason": "Сверка",
        }
        event = self.event("report.trip_correct", 4, correction, base=before, deps=[job["id"]])
        r = self.post(event)
        self.assertEqual(r["status"], "applied", r.get("message"))
        trip.refresh_from_db()
        self.assertEqual(trip.note, "Уточнение по рации")
        assignment = HaulAssignment.objects.get(truck=self.truck)
        event = self.event(
            "assignment.cancel", 5, {"assignment_id": assignment.pk, "reason": "Проверка"}
        )
        r = self.post(event)
        self.assertEqual(r["status"], "applied", r.get("message"))
        assignment.refresh_from_db()
        self.assertEqual(assignment.ended_at, self.now + timedelta(seconds=5))

    def test_ack_replay_reports_latest_shift_without_switching_cookie(self):
        first = self.open()
        self.assertEqual(self.post(self.event("shift.close", 2))["status"], "applied")
        repeat = self.post(first)
        self.assertTrue(repeat["deduplicated"])
        self.assertFalse(repeat["snapshot"]["shift"]["is_open"])

    def test_foreign_key_cannot_resolve_another_device_or_actor_conflict(self):
        self.open()
        r = self.post(
            self.event(
                "conflict.resolve", 2, {"decision": "accept_server", "event_id": str(uuid4())}
            )
        )
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["code"], "resolution_target")

    def test_signed_pull_without_cookie_requires_recent_single_use_server_nonce(self):
        from django.test import Client

        anonymous = Client(enforce_csrf_checks=True)
        gate = anonymous.get(
            self.url, {"device_id": self.device, "protocol": PROTOCOL, "challenge": "1"}
        ).json()
        self.assertNotIn("snapshot", gate)
        self.assertTrue(gate["csrf_token"])
        actor = self.grant["snapshot"]["actor"]
        evt = {
            "format_version": 2,
            "id": str(uuid4()),
            "device_id": self.device,
            "type": "snapshot.read",
            "challenge": gate["challenge"],
            "occurred_at": gate["server_time"],
            "actor_id": actor["id"],
            "actor_access_id": actor["access_id"],
            "actor_key": actor["key"],
            "actor_generation": actor["generation"],
        }
        body = {**self.envelope(evt), "op": "snapshot"}
        refused = anonymous.post(self.url, body, content_type="application/json")
        self.assertEqual(refused.status_code, 403)
        ack = anonymous.post(
            self.url, body, content_type="application/json", HTTP_X_CSRFTOKEN=gate["csrf_token"]
        )
        self.assertEqual(ack.status_code, 200, ack.content[:200])
        self.assertEqual(ack.json()["snapshot"]["actor"]["id"], self.employee.pk)
        again = anonymous.post(
            self.url, body, content_type="application/json", HTTP_X_CSRFTOKEN=gate["csrf_token"]
        )
        self.assertEqual(again.json()["code"], "read_challenge_replayed")
        self.assertNotIn("snapshot", again.json())
        self.assertEqual(OfflineFieldEvent.objects.exclude(event_type=ENROLLMENT).count(), 0)

    def test_old_ticket_can_replay_past_record_but_cannot_read_new_state(self):
        from django.core import signing
        from .field_sync import SALT

        proof = signing.loads(self.grant["ticket"], salt=SALT)
        proof["grant_from"] = (self.now - timedelta(hours=2)).isoformat()
        proof["grant_until"] = (timezone.now() - timedelta(seconds=2)).isoformat()
        self.grant["ticket"] = signing.dumps(proof, salt=SALT)
        r = self.post(self.event("shift.open", 1))
        self.assertEqual(r["status"], "applied")
        self.assertNotIn("snapshot", r)
        event = self.event("shift.close", 2)
        event["occurred_at"] = timezone.now().isoformat()
        r = self.post(event)
        self.assertEqual(r["status"], "auth")
        self.assertFalse(EmployeeShift.objects.get().closed_at)

    def test_signed_pull_cannot_exchange_owner_or_device(self):
        gate = self.client.get(
            self.url, {"device_id": str(uuid4()), "protocol": PROTOCOL, "challenge": "1"}
        ).json()
        evt = self.event("snapshot.read", 1)
        evt["occurred_at"] = gate["server_time"]
        evt["challenge"] = gate["challenge"]
        result = self.client.post(
            self.url, {**self.envelope(evt), "op": "snapshot"}, content_type="application/json"
        )
        self.assertEqual(result.json()["code"], "read_challenge_mismatch")
        self.assertNotIn("snapshot", result.json())
        gate = self.client.get(
            self.url, {"device_id": self.device, "protocol": PROTOCOL, "challenge": "1"}
        ).json()
        evt["challenge"] = gate["challenge"]
        evt["actor_id"] = self.employee.pk + 1
        result = self.client.post(
            self.url, {**self.envelope(evt), "op": "snapshot"}, content_type="application/json"
        )
        self.assertEqual(result.json()["code"], "actor_mismatch")
        self.assertNotIn("snapshot", result.json())

    def report_read(self, path, client=None, event_patch=None):
        client = client or self.client
        gate = client.get(
            self.url, {"device_id": self.device, "protocol": PROTOCOL, "challenge": "1"}
        ).json()
        event = self.event("report.read", 1)
        event.update(path=path, occurred_at=gate["server_time"], challenge=gate["challenge"])
        event.update(event_patch or {})
        body = {**self.envelope(event), "op": "report"}
        token = {"HTTP_X_CSRFTOKEN": gate["csrf_token"]}
        response = client.post(self.url, body, content_type="application/json", **token)
        return response, body, token

    def portrait_read(self, client, **extra):
        gate = client.get(
            self.url,
            {
                "device_id": self.device,
                "protocol": PROTOCOL,
                "challenge": "1",
            },
        ).json()
        actor = self.grant["snapshot"]["actor"]
        event = {
            "id": str(uuid4()),
            "type": "portrait.read",
            "format_version": 2,
            "device_id": self.device,
            "actor_id": actor["id"],
            "actor_access_id": actor["access_id"],
            "actor_key": actor["key"],
            "actor_generation": actor["generation"],
            "occurred_at": gate["server_time"],
            "challenge": gate["challenge"],
            **extra,
        }
        body = {**self.envelope(event), "op": "portrait"}
        response = client.post(
            self.url, body, content_type="application/json", HTTP_X_CSRFTOKEN=gate["csrf_token"]
        )
        return response, body, gate["csrf_token"]

    def test_personal_portraits_follow_the_signed_owner_and_never_the_web_cookie(self):
        from io import BytesIO
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from django.core.files.base import ContentFile
        from django.test import Client
        from PIL import Image

        other = Employee.objects.create(
            full_name="Другой мастер", phone="+79991234568", status=Employee.Status.ACTIVE
        )
        other_access = EmployeeAccess.objects.create(
            employee=other,
            role=self.role,
            access_code="333333",
            status=EmployeeAccess.Status.ACTIVATED,
        )
        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            for employee, color in [(self.employee, "red"), (other, "blue")]:
                source = BytesIO()
                Image.new("RGB", (320, 80), color).save(source, format="PNG")
                employee.photo.save("profile.png", ContentFile(source.getvalue()))
            with patch.object(
                self.employee.photo.storage,
                "open",
                side_effect=AssertionError("snapshot read the file"),
            ):
                snapshot = self.client.get(
                    self.url, {"device_id": self.device, "protocol": PROTOCOL}
                ).json()["snapshot"]
            version = snapshot["portrait_revision"]
            self.assertRegex(version, "^[0-9a-f]{64}$")
            self.login(other_access)
            # The normal browser session is B's. The signed portrait request is for A.
            red, body, csrf = self.portrait_read(self.client)
            self.assertEqual(red.status_code, 200)
            self.assertIn("no-store", red["Cache-Control"])
            picture = red.json()["portrait"]
            self.assertEqual(
                (picture["employee_id"], picture["access_id"], picture["revision"]),
                (self.employee.pk, self.access.pk, version),
            )
            self.assertTrue(picture["data"].startswith("data:image/jpeg;base64,"))
            red_bytes = base64.b64decode(picture["data"].split(",")[1], validate=True)
            self.assertLessEqual(len(red_bytes), 64 * 1024)
            with Image.open(BytesIO(red_bytes)) as photo:
                self.assertEqual(photo.size, (160, 40))
                self.assertGreater(photo.getpixel((0, 0))[0], 245)
            again = self.client.post(
                self.url, body, content_type="application/json", HTTP_X_CSRFTOKEN=csrf
            )
            self.assertEqual(again.json()["code"], "read_challenge_replayed")
            self.grant = self.enroll(self.signer).json()
            blue, _, _ = self.portrait_read(Client(enforce_csrf_checks=True))
            self.assertEqual(blue.status_code, 200)
            picture = blue.json()["portrait"]
            self.assertEqual(
                (picture["employee_id"], picture["access_id"]), (other.pk, other_access.pk)
            )
            self.assertNotEqual(picture["revision"], version)
            with Image.open(BytesIO(base64.b64decode(picture["data"].split(",")[1]))) as photo:
                self.assertGreater(photo.getpixel((0, 0))[2], 245)
            self.assertFalse(OfflineFieldEvent.objects.exclude(event_type=ENROLLMENT).exists())

    def test_portrait_deletion_storage_failure_and_replacement_are_distinct(self):
        from io import BytesIO
        from tempfile import TemporaryDirectory

        from django.core.files.base import ContentFile
        from django.test import Client
        from PIL import Image

        no_cookie = Client(enforce_csrf_checks=True)
        self.assertEqual(self.grant["snapshot"]["portrait_revision"], "")
        no_photo, _, _ = self.portrait_read(no_cookie)
        self.assertEqual(
            no_photo.json()["portrait"],
            {
                "employee_id": self.employee.pk,
                "access_id": self.access.pk,
                "data": None,
                "revision": "",
            },
        )
        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            self.employee.photo.save("bad.png", ContentFile(b"truncated picture"))
            old = self.client.get(
                self.url, {"device_id": self.device, "protocol": PROTOCOL}
            ).json()["snapshot"]["portrait_revision"]
            for attempt in range(2):
                if attempt:
                    self.employee.photo.storage.delete(self.employee.photo.name)
                failure, _, _ = self.portrait_read(no_cookie)
                self.assertEqual((failure.status_code, failure.json()["status"]), (503, "retry"))
                self.assertNotIn("portrait", failure.json())
            source = BytesIO()
            Image.new("RGB", (20, 20)).save(source, "PNG")
            # A saved replacement may reuse the filename of a removed file.
            self.employee.photo.save("bad.png", ContentFile(source.getvalue()))
            replacement, _, _ = self.portrait_read(no_cookie)
            self.assertNotEqual(replacement.json()["portrait"]["revision"], old)
            self.employee.photo.delete(save=True)
            gone, _, _ = self.portrait_read(no_cookie)
            self.assertEqual(
                (gone.json()["portrait"]["revision"], gone.json()["portrait"]["data"]), ("", None)
            )

    def test_portrait_revision_is_stable_during_normal_shift_login_and_polling(self):
        self.employee.photo = "employee_photos/stable.jpg"
        self.employee.save(update_fields=["photo", "updated_at"])

        def current_revision():
            return self.client.get(
                self.url, {"device_id": self.device, "protocol": PROTOCOL}
            ).json()["snapshot"]["portrait_revision"]

        initial = current_revision()
        self.assertTrue(initial)
        self.open()
        heartbeat = self.client.post("/session-heartbeat/", {"path": "/mining-master/tablet/"})
        self.assertEqual(heartbeat.status_code, 204)
        self.assertEqual(current_revision(), initial)
        self.assertEqual(self.post(self.event("shift.close", 2))["status"], "applied")
        self.assertEqual(current_revision(), initial)
        login = self.client.post(
            "/mining-master/tablet/login/",
            {"phone": self.employee.phone, "access_code": "111222", "intent": "login"},
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(current_revision(), initial)

    def test_portrait_requires_own_current_access_and_cannot_select_another_file(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        wrong_id, _, _ = self.portrait_read(client, actor_id=self.employee.pk + 1)
        self.assertEqual(wrong_id.json()["code"], "actor_mismatch")
        for field in [
            {"path": "/media/employee_photos/other.jpeg"},
            {"url": "https://example.invalid"},
            {"employee_id": self.employee.pk + 1},
        ]:
            with self.subTest(field=field):
                result, _, _ = self.portrait_read(client, **field)
                self.assertEqual(
                    (result.status_code, result.json()["code"]), (400, "invalid_portrait_request")
                )
                self.assertNotIn("portrait", result.json())
        self.access.status = EmployeeAccess.Status.BLOCKED
        self.access.save(update_fields=["status"])
        blocked, _, _ = self.portrait_read(client)
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (403, "access_revoked"))
        self.assertNotIn("portrait", blocked.json())

    def test_read_original_report_period_and_export_after_session_loss(self):
        from io import BytesIO
        from django.test import Client
        from openpyxl import load_workbook

        client = Client(enforce_csrf_checks=True)
        path = "/mining-master/tablet/dispatcher/shift-reports/trucks/"
        filters = "?date=2026-10-17&shift_type=night"
        html, body, headers = self.report_read(path + filters, client)
        self.assertEqual(html.status_code, 200, html.content[:240])
        self.assertEqual(html["X-Copper-Report-Read"], "1")
        self.assertContains(html, "2026-10-17")
        self.assertContains(html, "/mining-master/tablet/")
        self.assertNotIn("sessionid", client.cookies)
        again = client.post(self.url, body, content_type="application/json", **headers)
        self.assertEqual(again.json()["code"], "read_challenge_replayed")
        xlsx, _, _ = self.report_read(path + "export/" + filters, client)
        self.assertEqual(xlsx.status_code, 200, xlsx.content[:200])
        self.assertIn("2026-10-17_night.xlsx", xlsx["Content-Disposition"])
        self.assertTrue(load_workbook(BytesIO(xlsx.content), read_only=True).sheetnames)
        self.assertEqual(OfflineFieldEvent.objects.exclude(event_type=ENROLLMENT).count(), 0)

    def test_report_cookie_does_not_switch_signed_employee_or_privileges(self):
        from io import BytesIO
        from django.test import Client
        from openpyxl import load_workbook

        other = Employee.objects.create(
            full_name="Чужая сессия", phone="+79994445566", status=Employee.Status.ACTIVE
        )
        access = EmployeeAccess.objects.create(
            employee=other,
            role=self.role,
            status=EmployeeAccess.Status.ACTIVATED,
            access_code="998877",
        )
        self.login(access)
        path = "/mining-master/tablet/reports/volume/export/"
        response, _, _ = self.report_read(path)
        self.assertEqual(response.status_code, 200, response.content[:200])
        rows = list(load_workbook(BytesIO(response.content), read_only=True).active.values)
        self.assertIn(self.employee.full_name, [cell for row in rows for cell in row])
        self.assertNotIn(other.full_name, [cell for row in rows for cell in row])
        self.assertEqual(self.client.session["employee_access_id"], access.pk)
        resp, _, _ = self.report_read(path, Client(), {"actor_id": other.pk})
        self.assertEqual(resp.json()["code"], "actor_mismatch")
        self.access.status = EmployeeAccess.Status.BLOCKED
        self.access.save(update_fields=["status"])
        resp, _, _ = self.report_read(path)
        self.assertEqual(resp.json()["code"], "access_revoked")

    def test_report_allowlist_scope_and_limits(self):
        from unittest.mock import patch

        path = "/mining-master/tablet/dispatcher/shift-log/"
        for wrong in (
            "/admin/",
            "//example.com" + path,
            path + "#x",
            path.replace("shift-log/", "shift-log/../transport/"),
            "/mining-master/tablet/logout/",
            "/mining-master/tablet/field-sync/",
        ):
            with self.subTest(wrong=wrong):
                result, _, _ = self.report_read(wrong)
                self.assertEqual(result.json()["code"], "report_not_allowed")
        for wrong in (
            "?next=/admin/",
            "?date=2026-99-99",
            "?template=NaN",
            "?status=a%0D%0Ab",
            "?_operational_fragment=dispatcher-shift-report",
            "?" + "&".join(["date="] * 49),
        ):
            with self.subTest(wrong=wrong):
                result, _, _ = self.report_read(path + wrong)
                self.assertEqual(result.json()["code"], "report_filters_invalid")
        result, _, _ = self.report_read(path, event_patch={"type": "snapshot.read"})
        self.assertEqual(result.json()["code"], "invalid_read_request")
        with patch("assignments.field_reports.MAX_REPORT_BYTES", 16):
            result, _, _ = self.report_read(path)
        self.assertEqual(result.status_code, 413)
        self.assertEqual(result.json()["code"], "report_too_large")

    def test_report_whitelist_matches_original_tablet_permissions(self):
        from django.urls import reverse
        from .tablet_urls import REPORT_NAMES

        for name in sorted(REPORT_NAMES):
            with self.subTest(name=name):
                url = reverse("mining_master_tablet_" + name) + "?date=2026-10-17"
                original = self.client.get(url)
                signed, _, _ = self.report_read(url)
                if original.status_code == 200:
                    self.assertEqual(signed.status_code, 200, signed.content[:200])
                    self.assertEqual(signed["Content-Type"], original["Content-Type"])
                else:
                    self.assertEqual(signed.status_code, 403, signed.content[:200])
                    self.assertEqual(signed.json()["code"], "report_not_allowed")
