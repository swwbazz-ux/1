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
