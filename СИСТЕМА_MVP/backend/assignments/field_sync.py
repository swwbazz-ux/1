"""Optional durable exchange for the mining master's APK outbox."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from django.core import signing
from django.core.exceptions import ValidationError
from . import field_commands, field_signing
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.http import JsonResponse, Http404
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.db_locks import lock_idempotency_key
from core.models import (
    OfflineFieldEvent,
    OfflineFieldEventConflict,
    OfflineFieldEventStatus,
    lock_production_state,
    bump_operational_state,
)
from references.models import Equipment
from shifts.models import EmployeeShift
from shifts.services import lock_active_employee_for_shift
from users.active_role import role_session_state
from users.forms import is_valid_russian_mobile_phone, normalize_phone
from users.mining_master_tablet_auth import _eligible, _version
from users.models import AdminActionLog, Employee, EmployeeAccess

from .field_plan import PlanningError, _intents, _live_state, _merge, _payload
from .models import ExcavatorPlacement
from .services import HaulAssignmentStateConflict, schedule_haul_assignment, schedule_haul_release
from .views import get_shift_type_for_now, mining_master_shift_queryset


ROLE = "mining_master"
PROTOCOL = "master-field/1"
SALT = "mining-master-field-ticket-v1"
EVENT_PREFIX = "master.field."
EVENT_TYPES = {"shift.open", "shift.close", "draft.save", "live.save", "draft.apply"}
TICKET_AGE = 7 * 24 * 60 * 60
IDENTITY_FIELDS = (
    "id",
    "format_version",
    "type",
    "device_id",
    "sequence",
    "occurred_at",
    "actor_key",
    "actor_id",
    "actor_access_id",
    "actor_generation",
    "local_shift_id",
    "server_shift_id",
    "payload",
    "base_payload",
)


class FieldProblem(Exception):
    def __init__(self, code, message, *, status="conflict"):
        super().__init__(message)
        self.code = code
        self.status = status


def _uuid(value):
    try:
        parsed = UUID(str(value))
        return parsed.version == 4 and str(parsed) == value
    except (TypeError, ValueError, AttributeError):
        return False


def _generation(access):
    return access.last_login_at.isoformat() if access.last_login_at else ""


def _actor(access):
    key = "phone:" + hashlib.sha256(normalize_phone(access.employee.phone).encode()).hexdigest()
    return {
        "key": key,
        "id": access.employee_id,
        "access_id": access.pk,
        "role": ROLE,
        "verified": True,
        "full_name": access.employee.full_name,
        "generation": _generation(access),
    }


def _alias(generation):
    return "session:" + hashlib.sha256(generation.encode()).hexdigest()


def _own_shift(access):
    return mining_master_shift_queryset().filter(employee_id=access.employee_id).first()


def _shared_draft(live):
    latest = (
        OfflineFieldEvent.objects.filter(
            role_code=ROLE,
            status=OfflineFieldEventStatus.ACCEPTED,
            event_type__in=[EVENT_PREFIX + "draft.save", EVENT_PREFIX + "draft.apply"],
        )
        .order_by("-id")
        .first()
    )
    if latest and "draft_payload" in latest.result_payload:
        return {"revision": latest.pk, "payload": latest.result_payload["draft_payload"]}
    return {"revision": 0, "payload": live}


def _snapshot(access, device, *, live=None, v2=False):
    current = live or _live_state()
    shift = _own_shift(access)
    local_id = None
    if shift:
        binding = (
            OfflineFieldEvent.objects.filter(
                actor_id=access.employee_id,
                access_id=access.pk,
                role_code=ROLE,
                device_id=device,
                shift=shift,
                status=OfflineFieldEventStatus.ACCEPTED,
            )
            .exclude(context_snapshot__local_shift_id="")
            .order_by("sequence")
            .first()
        )
        local_id = binding.context_snapshot.get("local_shift_id") if binding else None
        local_id = local_id or str(
            UUID(
                hex=hashlib.sha256(f"{device}:master-shift:{shift.pk}".encode()).hexdigest()[:32],
                version=4,
            )
        )
    version = lock_production_state().version
    snapshot = {
        "actor": _actor(access),
        "server_version": version,
        "inventory": current[2],
        "live": current[1],
        "draft": _shared_draft(current[1]),
        "shift": {
            "is_open": bool(shift),
            "server_id": shift.pk if shift else None,
            "local_shift_id": local_id,
            "opened_at": shift.opened_at.isoformat() if shift else None,
        },
    }

    if v2:
        snapshot["live_tokens"] = {"trucks": current[0]["trucks"], "zones": current[0]["zones"]}
        return field_commands.enrich_snapshot(access, snapshot)
    return snapshot


def _reply(event, status, *, code="", message=""):
    return {
        "id": event.get("id"),
        "actor_key": event.get("actor_key"),
        "actor_id": event.get("actor_id"),
        "actor_access_id": event.get("actor_access_id"),
        "status": status,
        "code": code,
        "message": message,
    }


def _validate_event(raw, *, v2=False):
    if not isinstance(raw, dict):
        raise FieldProblem("invalid_event", "Не удалось прочитать действие планшета.")
    occurred = parse_datetime(str(raw.get("occurred_at", "")))
    if (
        not _uuid(raw.get("id"))
        or not _uuid(raw.get("local_shift_id"))
        or not _uuid(raw.get("device_id"))
        or type(raw.get("sequence")) is not int
        or not 1 <= raw["sequence"] <= 9007199254740991
        or type(raw.get("format_version")) is not int
        or raw["format_version"] != (2 if v2 else 1)
        or not isinstance(raw.get("type"), str)
        or raw["type"]
        not in (EVENT_TYPES | field_commands.COMMANDS | {"conflict.resolve"} if v2 else EVENT_TYPES)
        or not occurred
        or timezone.is_naive(occurred)
        or not isinstance(raw.get("payload"), dict)
    ):
        raise FieldProblem("invalid_event", "Номер, время или формат действия не подтверждены.")
    if occurred > timezone.now() + timedelta(minutes=5):
        raise FieldProblem(
            "device_clock_ahead", "Время планшета опережает сервер. Нужна сверка времени."
        )
    if v2:
        _v2_dependencies(raw)
    identity = {key: raw.get(key) for key in IDENTITY_FIELDS}
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return occurred, fingerprint


def _ticket_access(ticket, event, *, key=None):
    try:
        proof = signing.loads(ticket, salt=SALT, max_age=None if key else TICKET_AGE)
    except (signing.BadSignature, TypeError):
        raise FieldProblem(
            "ticket_invalid", "Подтвердите личный вход мастера при наличии связи.", status="auth"
        )
    if (key and proof.get("key_sha256") != key.fingerprint) or (
        not key and (proof.get("key_sha256") or field_signing.registration(event["device_id"]))
    ):
        raise FieldProblem(
            "signed_device_required",
            "Допуск должен соответствовать зарегистрированному ключу планшета.",
            status="auth",
        )
    if key:
        at = parse_datetime(event["occurred_at"])
        until = parse_datetime(str(proof.get("grant_until", "")))
        since = parse_datetime(str(proof.get("grant_from", "")))
        if not until or not since or not at or at > until or at < since - timedelta(minutes=5):
            raise FieldProblem(
                "grant_expired",
                "Время действия выходит за срок проверенного доступа.",
                status="auth",
            )
    if proof.get("device_id") != event.get("device_id"):
        raise FieldProblem("device_mismatch", "Допуск выдан другому планшету.", status="auth")
    candidate = (
        EmployeeAccess.objects.select_related(
            "employee", "role", "employee__contractor_organization"
        )
        .filter(pk=proof.get("access_id"))
        .first()
    )
    if not candidate:
        raise FieldProblem("access_revoked", "Доступ сотрудника недоступен.", status="auth")
    Employee.objects.select_for_update().get(pk=candidate.employee_id)
    access = (
        EmployeeAccess.objects.select_for_update(of=("self",))
        .select_related(
            "employee",
            "role",
            "employee__contractor_organization",
        )
        .get(pk=candidate.pk)
    )
    if not _eligible(access) or proof.get("access_version") != _version(access):
        raise FieldProblem(
            "access_revoked", "Доступ сотрудника изменён. Нужна проверка входа.", status="auth"
        )
    canonical = _actor(access)
    generation = str(event.get("actor_generation") or "")
    legacy = (
        bool(generation)
        and event.get("actor_key") == _alias(generation)
        and generation == proof.get("generation") == _generation(access)
    )
    if event.get("actor_key") != canonical["key"] and not legacy:
        raise FieldProblem(
            "actor_mismatch", "Допуск не подтверждает автора этого действия.", status="auth"
        )
    if generation != proof.get("generation"):
        raise FieldProblem(
            "generation_mismatch",
            "Действие относится к другому подтверждённому входу.",
            status="auth",
        )
    # Legacy personally verified entries had no numeric IDs. The signed scope is mandatory.
    if event.get("actor_id") not in (None, access.employee_id) or event.get(
        "actor_access_id"
    ) not in (None, access.pk):
        raise FieldProblem(
            "actor_mismatch", "Номера сотрудника и допуска не совпадают.", status="auth"
        )
    if any(
        event.get(key) is not None and type(event[key]) is not int
        for key in ("actor_id", "actor_access_id")
    ):
        raise FieldProblem(
            "actor_mismatch", "Формат номера сотрудника не подтверждён.", status="auth"
        )
    return access, proof


def _read_grant(access, proof):
    until = parse_datetime(str(proof.get("grant_until", "")))
    return bool(
        until and timezone.now() <= until and proof.get("generation") == _generation(access)
    )


def _conflict_attempt(existing, access, event, fingerprint, code):
    OfflineFieldEventConflict.objects.create(
        existing_event=existing,
        attempted_event_id=event["id"],
        actor=access.employee,
        access=access,
        role_code=ROLE,
        device_id=event["device_id"],
        fingerprint=fingerprint,
        code=code,
        submitted_event={key: event.get(key) for key in IDENTITY_FIELDS},
    )


def _shift_for_event(access, proof, event, occurred):
    binding = (
        OfflineFieldEvent.objects.select_for_update(of=("self",))
        .filter(
            actor_id=access.employee_id,
            access_id=access.pk,
            role_code=ROLE,
            device_id=event["device_id"],
            context_snapshot__local_shift_id=event["local_shift_id"],
            status=OfflineFieldEventStatus.ACCEPTED,
            shift__isnull=False,
        )
        .order_by("sequence")
        .first()
    )
    shift_id = binding.shift_id if binding else proof.get("shift_id")
    if not shift_id:
        raise FieldProblem(
            "shift_not_received",
            "Сначала должна подтвердиться открытая на планшете смена.",
            status="retry",
        )
    if event.get("server_shift_id") and event["server_shift_id"] != shift_id:
        raise FieldProblem("shift_mismatch", "Действие относится к другой смене.")
    shift = (
        EmployeeShift.objects.select_for_update()
        .filter(
            pk=shift_id,
            employee_id=access.employee_id,
            workplace_code=ROLE,
            equipment__isnull=True,
        )
        .first()
    )
    if not shift or occurred < shift.opened_at:
        raise FieldProblem(
            "shift_mismatch", "Время действия не относится к подтверждённой смене мастера."
        )
    if shift.closed_at:
        raise FieldProblem(
            "shift_closed", "Эта смена уже закрыта. Позднее действие сохранено для сверки."
        )
    other_binding = (
        OfflineFieldEvent.objects.filter(
            actor_id=access.employee_id,
            access_id=access.pk,
            device_id=event["device_id"],
            role_code=ROLE,
            shift=shift,
            status=OfflineFieldEventStatus.ACCEPTED,
        )
        .exclude(context_snapshot__local_shift_id=event["local_shift_id"])
        .exists()
    )
    if not binding and other_binding:
        raise FieldProblem("shift_mismatch", "Смена уже связана с другим локальным номером.")
    return shift


def _plan_draft(base, desired, current):
    if not isinstance(base, dict):
        raise FieldProblem(
            "base_required", "Для сверки расстановки нужна исходная версия планшета."
        )
    # Historical equipment may have become inactive. Validate its identity first,
    # then let the intent merge reject only attempted changes to unavailable items.
    ids = set()
    for value in (base, desired):
        if isinstance(value, dict) and isinstance(value.get("excavators"), list):
            for row in value["excavators"]:
                if isinstance(row, dict):
                    if (
                        type(row.get("excavator_id")) is not int
                        or not isinstance(row.get("truck_ids"), list)
                        or any(type(key) is not int for key in row["truck_ids"])
                    ):
                        raise FieldProblem(
                            "invalid_payload", "Не удалось прочитать номера техники."
                        )
                    ids.add(row["excavator_id"])
                    ids.update(row["truck_ids"])
    equipment = list(Equipment.objects.filter(pk__in=ids).select_related("equipment_type"))
    catalog = {
        "excavators": [
            {"id": item.pk, "number": item.garage_number}
            for item in equipment
            if "экскаватор" in item.equipment_type.name.lower()
        ],
        "trucks": [
            {"id": item.pk, "number": item.garage_number}
            for item in equipment
            if "самосвал" in item.equipment_type.name.lower()
        ],
    }
    base = _payload(base, catalog)
    desired = _payload(desired, catalog)
    base_targets = {
        truck_id: row["excavator_id"] for row in base["excavators"] for truck_id in row["truck_ids"]
    }
    active = {row["excavator_id"] for row in base["excavators"]}
    baseline = {
        "zones": {
            str(item["id"]): ExcavatorPlacement.Zone.ACTIVE
            if item["id"] in active
            else ExcavatorPlacement.Zone.INACTIVE
            for item in catalog["excavators"]
        },
        "trucks": {
            str(item["id"]): {"id": 0, "target": base_targets.get(item["id"])}
            for item in catalog["trucks"]
        },
        "excavator_ids": [item["id"] for item in catalog["excavators"]],
        "truck_ids": [item["id"] for item in catalog["trucks"]],
        "numbers": {str(item.pk): item.garage_number for item in equipment},
    }
    draft = SimpleNamespace(baseline=baseline, payload=desired, revision=0)
    merged, conflicts = _merge(draft, current)
    if conflicts:
        raise FieldProblem("base_conflict", " ".join(item["message"] for item in conflicts))
    return draft, merged


def _apply_live(access, event, current):
    draft, merged = _plan_draft(event.get("base_payload"), event["payload"], current)
    truck_intents, zone_intents = _intents(draft)
    for target in truck_intents.values():
        if target is not None:
            zone_intents.setdefault(target, True)
    ids = set(truck_intents) | set(zone_intents)
    ids.update(key for key in truck_intents.values() if key is not None)
    equipment = {
        item.pk: item
        for item in Equipment.objects.select_for_update().filter(pk__in=ids).order_by("pk")
    }
    now = (
        parse_datetime(event["occurred_at"]) if event.get("format_version") == 2 else timezone.now()
    )
    changed = False
    for truck_id, target in sorted(truck_intents.items()):
        actual = current[0]["trucks"][str(truck_id)]
        if actual["target"] == target:
            continue
        kwargs = {
            "truck": equipment[truck_id],
            "assigned_by": access.employee,
            "now": now,
            "expected_state_id": actual["id"],
        }
        if event.get("format_version") == 2:
            kwargs["recorded_at"] = now
        if target is None:
            schedule_haul_release(**kwargs)
        else:
            schedule_haul_assignment(excavator=equipment[target], **kwargs)
        changed = True
    for excavator_id, enabled in sorted(zone_intents.items()):
        if excavator_id not in current[0]["excavator_ids"]:
            continue
        zone = ExcavatorPlacement.Zone.ACTIVE if enabled else ExcavatorPlacement.Zone.INACTIVE
        if current[0]["zones"][str(excavator_id)] == zone:
            continue
        placement, _ = ExcavatorPlacement.objects.get_or_create(excavator_id=excavator_id)
        placement.zone = zone
        placement.changed_by = access.employee
        placement.save(update_fields=["zone", "changed_by", "changed_at"])
        if event.get("format_version") == 2:
            ExcavatorPlacement.objects.filter(pk=placement.pk).update(changed_at=now)
        changed = True
    return _live_state()[1], changed


def _process(access, proof, event, occurred):
    if event["type"] == "conflict.resolve":
        return _resolve(access, event)
    if event["type"] == "shift.open":
        if (event.get("format_version") != 2 and event["id"] != event["local_shift_id"]) or (
            OfflineFieldEvent.objects.filter(
                device_id=event["device_id"],
                role_code=ROLE,
                event_type=EVENT_PREFIX + "shift.open",
                status=OfflineFieldEventStatus.ACCEPTED,
                context_snapshot__local_shift_id=event["local_shift_id"],
            ).exists()
        ):
            raise FieldProblem(
                "shift_mismatch", "Открытие смены должно сохранить свой исходный номер."
            )
        lock_active_employee_for_shift(access.employee, role_code=ROLE)
        if (
            EmployeeShift.objects.filter(
                employee_id=access.employee_id, closed_at__isnull=True
            ).exists()
            or mining_master_shift_queryset().exists()
        ):
            raise FieldProblem(
                "shift_occupied", "На сервере уже открыта смена. Нужна сверка пересменки."
            )
        if (
            EmployeeShift.objects.filter(workplace_code=ROLE)
            .filter(Q(opened_at__gt=occurred) | Q(closed_at__gt=occurred))
            .exists()
            or EmployeeShift.objects.filter(
                employee_id=access.employee_id, closed_at__gt=occurred
            ).exists()
        ):
            raise FieldProblem(
                "late_shift", "После этой смены уже началась другая. История сохранена для сверки."
            )
        shift = EmployeeShift.objects.create(
            employee=access.employee,
            opened_by=access.employee,
            workplace_code=ROLE,
            shift_type=get_shift_type_for_now(occurred),
            opened_at=occurred,
        )
        return shift, {}
    shift = _shift_for_event(access, proof, event, occurred)
    if event["type"] in field_commands.COMMANDS:
        return shift, field_commands.execute(access, event, occurred)
    if event["type"] == "shift.close":
        if OfflineFieldEvent.objects.filter(
            shift=shift, status=OfflineFieldEventStatus.ACCEPTED, occurred_at__gt=occurred
        ).exists():
            raise FieldProblem(
                "time_order", "Завершение смены предшествует уже принятым действиям."
            )
        shift.closed_at = occurred
        shift.closed_by = access.employee
        shift.save(update_fields=["closed_at", "closed_by"])
        return shift, {}
    current = _live_state()
    if event["type"] == "draft.save":
        shared = _shared_draft(current[1])["payload"]
        targets = {
            truck_id: row["excavator_id"]
            for row in shared["excavators"]
            for truck_id in row["truck_ids"]
        }
        active = {row["excavator_id"] for row in shared["excavators"]}
        draft_live = (dict(current[0]), shared, current[2])
        draft_live[0]["trucks"] = {
            str(item["id"]): {"id": 0, "target": targets.get(item["id"])}
            for item in current[2]["trucks"]
        }
        draft_live[0]["zones"] = {
            str(item["id"]): ExcavatorPlacement.Zone.ACTIVE
            if item["id"] in active
            else ExcavatorPlacement.Zone.INACTIVE
            for item in current[2]["excavators"]
        }
        _, merged = _plan_draft(event.get("base_payload"), event["payload"], draft_live)
        return shift, {"draft_payload": merged}
    live, changed = _apply_live(access, event, current)
    extra = {"topology_changed": changed}
    if event["type"] == "draft.apply":
        extra["draft_payload"] = live
    return shift, extra


def _v2_dependencies(event):
    deps = event.get("depends_on")
    if (
        not isinstance(deps, list)
        or len(deps) > 64
        or len(set(str(v) for v in deps)) != len(deps)
        or any(not _uuid(key) or key == event["id"] for key in deps)
    ):
        raise FieldProblem("invalid_dependencies", "Неверные зависимости полевого действия.")


def _accepted_or_resolved(row):
    return row.status == OfflineFieldEventStatus.ACCEPTED or (
        row.status == OfflineFieldEventStatus.CONFLICT
        and bool(row.result_payload.get("resolved_by"))
    )


def _check_order(event):
    if event["type"] == "conflict.resolve":
        return
    key = field_signing.registration(event["device_id"])
    if key.result_payload.get("contiguous_sequence", 0) < event["sequence"] - 1:
        raise FieldProblem(
            "previous_event_required",
            "Сначала нужно доставить или сверить предыдущее действие планшета.",
            status="retry",
        )
    previous = OfflineFieldEvent.objects.filter(
        role_code=ROLE, device_id=event["device_id"], sequence=event["sequence"] - 1
    ).first()
    if not previous or not _accepted_or_resolved(previous):
        raise FieldProblem(
            "previous_event_required",
            "Сначала нужно доставить или сверить предыдущее действие планшета.",
            status="retry",
        )

    for key in event["depends_on"]:
        prior = OfflineFieldEvent.objects.filter(
            event_id=key, role_code=ROLE, device_id=event["device_id"]
        ).first()
        if not prior or prior.sequence >= event["sequence"] or not _accepted_or_resolved(prior):
            raise FieldProblem(
                "dependency_required",
                "Ожидается подтверждение связанного действия.",
                status="retry",
            )
    if (
        previous.event_type != field_signing.ENROLLMENT
        and previous.event_type != EVENT_PREFIX + "conflict.resolve"
        and previous.occurred_at > parse_datetime(event["occurred_at"])
    ):
        raise FieldProblem(
            "device_time_order", "Часы устройства переведены назад. Требуется сверка."
        )


def _advance_sequence(key):
    # A resolution can arrive ahead of its sequence. Advance only the confirmed
    # contiguous prefix. The per-device advisory lock is held until commit.
    position = key.result_payload.get("contiguous_sequence", 0)
    for row in OfflineFieldEvent.objects.filter(
        device_id=key.device_id, role_code=ROLE, sequence__gt=position
    ).order_by("sequence"):
        if row.sequence != position + 1 or not _accepted_or_resolved(row):
            break
        position = row.sequence
    if key.result_payload.get("contiguous_sequence", 0) != position:
        key.result_payload = {**key.result_payload, "contiguous_sequence": position}
        key.save(update_fields=["result_payload", "updated_at"])


def _resolve(access, event):
    data = event["payload"]
    if not _uuid(data.get("event_id")) or data.get("decision") != "accept_server":
        raise FieldProblem("invalid_resolution", "Не выбран конфликт и способ сверки.")
    prior = (
        OfflineFieldEvent.objects.select_for_update()
        .filter(
            event_id=data["event_id"],
            actor=access.employee,
            access=access,
            role_code=ROLE,
            device_id=event["device_id"],
            status=OfflineFieldEventStatus.CONFLICT,
            sequence__lt=event["sequence"],
        )
        .first()
    )
    if not prior:
        raise FieldProblem("resolution_target", "Указанное действие не находится на сверке.")
    if not prior.result_payload.get("resolved_by"):
        prior.result_payload = {**prior.result_payload, "resolved_by": event["id"]}
        prior.save(update_fields=["result_payload", "updated_at"])
    return None, {"resolved_event_id": prior.event_id}


@transaction.atomic
def _receive(ticket, event, *, key=None, signed_hash=None, envelope=None):
    v2 = key is not None
    occurred, fingerprint = _validate_event(event, v2=v2)
    if v2:
        fingerprint = signed_hash
        lock_idempotency_key("master-device-sequence", event["device_id"])
    access, proof = _ticket_access(ticket, event, key=key)
    lock_idempotency_key("offline_field_event", event["id"])
    existing = OfflineFieldEvent.objects.select_for_update().filter(event_id=event["id"]).first()
    if existing:
        if (
            existing.actor_id != access.employee_id
            or existing.access_id != access.pk
            or existing.device_id != event["device_id"]
            or existing.role_code != ROLE
            or existing.fingerprint != fingerprint
        ):
            _conflict_attempt(existing, access, event, fingerprint, "event_id_reused")
            return _reply(
                event,
                "conflict",
                code="event_id_reused",
                message="Номер действия уже использован с другими данными.",
            )
        if existing.status != OfflineFieldEventStatus.RETRY:
            reply = {**existing.result_payload, "deduplicated": True}
            if v2 and _read_grant(access, proof):
                reply["snapshot"] = _snapshot(access, event["device_id"], v2=True)
            return reply
    lock_idempotency_key(
        "master_field_sequence", f"{access.employee_id}:{event['device_id']}:{event['sequence']}"
    )
    sequence_scope = {} if v2 else {"actor_id": access.employee_id}
    duplicate = (
        OfflineFieldEvent.objects.filter(
            **sequence_scope,
            role_code=ROLE,
            device_id=event["device_id"],
            sequence=event["sequence"],
        )
        .exclude(event_id=event["id"])
        .first()
    )
    if duplicate:
        _conflict_attempt(duplicate, access, event, fingerprint, "sequence_reused")
        return _reply(
            event,
            "conflict",
            code="sequence_reused",
            message="Этот номер очереди уже принадлежит другому действию.",
        )
    receipt = existing or OfflineFieldEvent.objects.create(
        event_id=event["id"],
        event_type=EVENT_PREFIX + event["type"],
        actor=access.employee,
        access=access,
        role_code=ROLE,
        device_id=event["device_id"],
        sequence=event["sequence"],
        occurred_at=occurred,
        fingerprint=fingerprint,
        payload=event["payload"],
        format_version=2 if v2 else 1,
        depends_on=event.get("depends_on", []) if v2 else [],
        context_snapshot={
            "local_shift_id": event["local_shift_id"],
            "actor_key": event["actor_key"],
            "actor_generation": event["actor_generation"],
            "base_payload": event.get("base_payload"),
            **(
                {
                    "signed_bytes": envelope["signed_bytes"],
                    "signature": envelope["signature"],
                    "key_sha256": key.fingerprint,
                }
                if v2
                else {}
            ),
        },
    )
    state_version = lock_production_state().version
    try:
        if v2:
            _check_order(event)
        if (
            not v2
            and OfflineFieldEvent.objects.filter(
                actor_id=access.employee_id,
                role_code=ROLE,
                device_id=event["device_id"],
                sequence__gt=event["sequence"],
                status=OfflineFieldEventStatus.ACCEPTED,
            ).exists()
        ):
            raise FieldProblem(
                "event_out_of_order",
                "Более позднее действие уже принято. Раннее сохранено для сверки.",
            )
        if (
            not v2
            and OfflineFieldEvent.objects.filter(
                actor_id=access.employee_id,
                role_code=ROLE,
                device_id=event["device_id"],
                sequence__lt=event["sequence"],
            )
            .exclude(status=OfflineFieldEventStatus.ACCEPTED)
            .exists()
        ):
            raise FieldProblem(
                "predecessor_pending",
                "Сначала нужно подтвердить предыдущее действие.",
                status="retry",
            )
        with transaction.atomic():
            topology = v2 and event["type"] in {
                "live.assign",
                "live.move",
                "draft.apply",
                "live.save",
                "assignment.cancel",
            }
            before = _live_state()[0]["trucks"] if topology else {}
            shift, extra = _process(access, proof, event, occurred)
            if topology:
                after = _live_state()[0]["trucks"]
                extra["topology_states"] = {
                    key: val for key, val in after.items() if before.get(key) != val
                }
            result = _reply(event, "applied")
            receipt.shift = shift
            receipt.status = OfflineFieldEventStatus.ACCEPTED
            receipt.retryable = False
            receipt.error_code = receipt.error_message = ""
            receipt.result_payload = {**result, **extra}
            receipt.save(
                update_fields=[
                    "shift",
                    "status",
                    "retryable",
                    "error_code",
                    "error_message",
                    "result_payload",
                    "updated_at",
                ]
            )
            if extra.get("topology_changed"):
                AdminActionLog.objects.create(
                    actor=access.employee,
                    action="Расстановка из полевого планшета",
                    action_code="master_field_" + event["type"].replace(".", "_"),
                    object_type="OfflineFieldEvent",
                    object_id=str(receipt.pk),
                    object_repr=f"Смена {shift.pk}, действие {event['id']}",
                    old_value=json.dumps(event.get("base_payload"), ensure_ascii=False),
                    new_value=json.dumps(event["payload"], ensure_ascii=False),
                )
            if v2:
                _advance_sequence(field_signing.registration(event["device_id"]))
                if (
                    lock_production_state().version == state_version
                    and event["type"] != "conflict.resolve"
                ):
                    bump_operational_state(
                        "MasterField:" + event["type"],
                        event_type="master_field_changed",
                        payload={
                            "event_id": event["id"],
                            "actor_id": access.employee_id,
                            "action": event["type"],
                        },
                    )
            if "resolved_event_id" in extra:
                result["resolved_event_id"] = extra["resolved_event_id"]
            if not v2 or _read_grant(access, proof):
                result["snapshot"] = _snapshot(access, event["device_id"], v2=v2)
            if not v2:
                receipt.result_payload = {**result, **extra}
                receipt.save(update_fields=["result_payload", "updated_at"])
    except (
        FieldProblem,
        PlanningError,
        HaulAssignmentStateConflict,
        ValidationError,
        Http404,
        IntegrityError,
    ) as error:
        status = error.status if isinstance(error, FieldProblem) else "conflict"
        code = getattr(error, "code", None) or "base_conflict"
        message = (
            "Объект изменён или больше недоступен."
            if isinstance(error, (IntegrityError, Http404))
            else str(error)
        )
        result = _reply(event, status, code=code, message=message)
        if v2 and _read_grant(access, proof):
            result["snapshot"] = _snapshot(access, event["device_id"], v2=True)
        receipt.status = (
            OfflineFieldEventStatus.RETRY if status == "retry" else OfflineFieldEventStatus.CONFLICT
        )
        receipt.retryable = status == "retry"
        receipt.error_code, receipt.error_message = code, str(error)
        receipt.result_payload = {k: v for k, v in result.items() if k != "snapshot"}
        receipt.save(
            update_fields=[
                "status",
                "retryable",
                "error_code",
                "error_message",
                "result_payload",
                "updated_at",
            ]
        )
    return result


def _capability(request, device, *, v2, enroll_key=None):
    state = role_session_state(request)
    access = state.get("access")
    if not _uuid(device):
        return JsonResponse({"code": "device_required"}, status=400)
    if (
        not access
        or not state.get("is_active")
        or not _eligible(access)
        or not is_valid_russian_mobile_phone(access.employee.phone)
        or getattr(request, "observer_mode", False)
    ):
        return JsonResponse({"code": "authentication_required"}, status=403)
    with transaction.atomic():
        Employee.objects.select_for_update().get(pk=access.employee_id)
        access = (
            EmployeeAccess.objects.select_for_update(of=("self",))
            .select_related("employee", "role", "employee__contractor_organization")
            .get(pk=access.pk)
        )
        if (
            not _eligible(access)
            or not role_session_state(request, access)["is_active"]
            or not is_valid_russian_mobile_phone(access.employee.phone)
        ):
            return JsonResponse({"code": "authentication_required"}, status=403)
        key = (
            field_signing.enroll(access, device, enroll_key)
            if enroll_key is not None
            else field_signing.registration(device)
        )
        if v2 and not key:
            return JsonResponse({"code": "enrollment_required"}, status=409)
        lock_production_state()
        snapshot = _snapshot(access, device, v2=v2)
        claims = {
            "access_id": access.pk,
            "device_id": device,
            "access_version": _version(access),
            "generation": _generation(access),
            "shift_id": snapshot["shift"]["server_id"],
        }
        if v2:
            claims["key_sha256"] = key.fingerprint
            claims["grant_from"] = max(
                key.received_at, access.last_login_at or key.received_at
            ).isoformat()
            claims["grant_until"] = (timezone.now() + timedelta(days=30)).isoformat()
        return JsonResponse(
            {
                "protocol": field_signing.PROTOCOL if v2 else PROTOCOL,
                "snapshot": snapshot,
                "ticket": signing.dumps(claims, salt=SALT),
                "actor_aliases": [_alias(_generation(access))] if _generation(access) else [],
                **({"public_key": key.payload, "grant_until": claims["grant_until"]} if v2 else {}),
            }
        )


CHALLENGE_SALT = "master-field-read-challenge-v2"
CHALLENGE_TTL = 120


def _challenge(request, device):
    if not _uuid(device):
        return JsonResponse({"code": "device_required"}, status=400)
    nonce = str(uuid4())
    sealed = signing.dumps({"device_id": device, "nonce": nonce}, salt=CHALLENGE_SALT)
    return JsonResponse(
        {
            "protocol": field_signing.PROTOCOL,
            "challenge": sealed,
            "server_time": timezone.now().isoformat(),
            "csrf_token": get_token(request),
        }
    )


@transaction.atomic
def _pull(ticket, event, key):
    if (
        event.get("type") != "snapshot.read"
        or event.get("format_version") != 2
        or not _uuid(event.get("id"))
    ):
        raise FieldProblem("invalid_read_request", "Неверный вид запроса состояния.")
    try:
        challenge = signing.loads(
            event.get("challenge"), salt=CHALLENGE_SALT, max_age=CHALLENGE_TTL
        )
    except (TypeError, signing.BadSignature):
        raise FieldProblem(
            "read_challenge_expired", "Запросите свежую проверку времени сервера.", status="auth"
        )
    now = timezone.now()
    occurred = parse_datetime(str(event.get("occurred_at") or ""))
    if (
        challenge.get("device_id") != event["device_id"]
        or not _uuid(challenge.get("nonce"))
        or not occurred
        or timezone.is_naive(occurred)
        or abs((now - occurred).total_seconds()) > CHALLENGE_TTL + 5
    ):
        raise FieldProblem(
            "read_challenge_mismatch",
            "Проверка устройства или времени запроса не пройдена.",
            status="auth",
        )
    access, proof = _ticket_access(ticket, event, key=key)
    if not _read_grant(access, proof):
        raise FieldProblem(
            "read_grant_expired",
            "Для чтения состояния нужен действующий допуск этого сотрудника.",
            status="auth",
        )
    key = OfflineFieldEvent.objects.select_for_update().get(pk=key.pk)
    fresh = {
        nonce: at
        for nonce, at in key.context_snapshot.get("used_read_challenges", {}).items()
        if at >= now.timestamp() - CHALLENGE_TTL - 5
    }
    nonce = challenge["nonce"]
    if nonce in fresh:
        raise FieldProblem(
            "read_challenge_replayed", "Этот запрос состояния уже использован.", status="auth"
        )
    if len(fresh) > 512:
        raise FieldProblem("read_throttled", "Повторите сверку состояния позже.", status="retry")
    fresh[nonce] = now.timestamp()
    key.context_snapshot = {**key.context_snapshot, "used_read_challenges": fresh}
    key.save(update_fields=["context_snapshot", "updated_at"])
    return JsonResponse(
        {
            "protocol": field_signing.PROTOCOL,
            "snapshot": _snapshot(access, event["device_id"], v2=True),
        }
    )


@never_cache
@require_http_methods(["GET", "POST"])
def field_sync_view(request):
    event = {}
    try:
        if request.method == "GET":
            get_token(request)
            if (
                request.GET.get("protocol") == field_signing.PROTOCOL
                and request.GET.get("challenge") == "1"
            ):
                return _challenge(request, request.GET.get("device_id", ""))
            return _capability(
                request,
                request.GET.get("device_id", ""),
                v2=request.GET.get("protocol") == field_signing.PROTOCOL,
            )
        if len(request.body) > field_signing.MAX_BYTES * 4:
            return JsonResponse({"code": "envelope_too_large"}, status=413)
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError()
        if data.get("protocol") == field_signing.PROTOCOL:
            if data.get("op") == "enroll":
                if not isinstance(data.get("public_key"), dict):
                    raise ValueError()
                return _capability(
                    request, data.get("device_id", ""), v2=True, enroll_key=data["public_key"]
                )
            event, key, digest = field_signing.checked_event(data)
            if data.get("op") == "snapshot":
                return _pull(data.get("ticket"), event, key)
            return JsonResponse(
                _receive(data.get("ticket"), event, key=key, signed_hash=digest, envelope=data)
            )
        event = data.get("event", {})
        return JsonResponse(_receive(data.get("ticket"), event))
    except field_signing.EnvelopeError as error:
        return JsonResponse(
            {
                "status": "auth" if error.status in (403, 409) else "conflict",
                "code": error.code,
                "message": str(error),
            },
            status=error.status,
        )
    except (ValueError, AttributeError, UnicodeDecodeError, RecursionError):
        return JsonResponse({"status": "conflict", "code": "invalid_request"}, status=400)
    except FieldProblem as error:
        event = event if isinstance(event, dict) else {}
        return JsonResponse(
            _reply(event, error.status, code=error.code, message=str(error)),
            status=403 if error.status == "auth" else 400,
        )
