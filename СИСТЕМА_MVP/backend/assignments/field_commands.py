"""Explicit adapter from signed field commands to the existing tablet command handlers.

Only the server constructs the short-lived request identity, after verifying the
ticket, signature and shift interval. No session is written or issued to the client.
"""

import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpRequest, QueryDict
from django.db.models import Q
from django.urls import get_urlconf, set_urlconf

from core.models import lock_production_state
from downtimes.models import DowntimeEvent
from references.models import DumpPoint, Equipment, RockType
from reports.models import ReportTemplate
from shifts.models import EmployeeShift
from trips.models import OPEN_TRIP_STATUSES, Trip
from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)

COMMANDS = {
    "live.move",
    "live.assign",
    "card.settings",
    "downtime.close",
    "assignment.cancel",
    "trip.cancel",
    "trip.complete",
    "trip.manual",
    "worker_shift.close",
    "report.trip_correct",
    "report.template_save",
}
JSON_COMMANDS = {"live.move", "live.assign", "card.settings", "downtime.close"}
CARD_FIELDS = ("loading_horizon", "loading_block", "rock_type_id", "destinations")
REPORT_FIELDS = (
    "volume_m3",
    "transport_distance_km",
    "loading_horizon",
    "loading_block",
    "downtime_text",
    "note",
    "rock_type_id",
    "actual_dump_point_id",
)


def trusted_request(access, *, payload=None, as_json=False, occurred=None):
    request = HttpRequest()
    request.method = "GET" if payload is None else "POST"
    request.path = request.path_info = "/mining-master/tablet/"
    request.mining_master_tablet_control = True
    request.urlconf = "assignments.tablet_urlconf"
    request.session = {
        "employee_access_id": access.pk,
        ACTIVE_ROLE_SESSION_KEY: access.pk,
        ACTIVE_ROLE_CODE_SESSION_KEY: "mining_master",
        ACTIVE_ROLE_GENERATION_SESSION_KEY: access.last_login_at.isoformat()
        if access.last_login_at
        else "",
    }
    request._messages = FallbackStorage(request)
    # This attribute is never read from HTTP data.
    request.field_verified_occurred_at = occurred
    if payload is not None:
        if as_json:
            request._body = json.dumps(payload, ensure_ascii=False).encode()
            request.content_type = "application/json"
            request.META["CONTENT_TYPE"] = "application/json"
        else:
            request.POST = QueryDict(
                urlencode({k: v for k, v in payload.items() if v is not None}, doseq=True)
            )
    return request


def readonly(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def actual_cards(access, version):
    from trips import views
    from .tablet_views import build_tablet_context, build_tablet_header

    req = trusted_request(access)

    def with_cards(**kwargs):
        kwargs["equipment_card_ids"] = None
        return views.build_dispatcher_dashboard_context(**kwargs)

    old = get_urlconf()
    try:
        set_urlconf(req.urlconf)
        model = views.build_dispatcher_control_read_model(
            req,
            access,
            dashboard_builder=with_cards,
            equipment_shift_is_current=views.mining_master_equipment_shift_is_current,
            dispatcher_header_override=build_tablet_header(req, access),
            context_overrides=build_tablet_context(req, access),
        )
    finally:
        set_urlconf(old)
    cards = {}
    for key, card in model.dispatcher_dashboard["equipment_cards"].items():
        alias = (
            "complex:" + key.removeprefix("complex-equipment-")
            if key.startswith("complex-equipment-")
            else "equipment:" + key
        )
        cards[alias] = {
            "contract": "dispatcher-equipment-detail-v1",
            "card_key": key,
            "version": version,
            "operational_state_version": version,
            "card": readonly(card),
        }
    return cards


def worker_open_trips(shift):
    if not shift.equipment_id:
        return []
    return sorted(
        Trip.objects.filter(status__in=OPEN_TRIP_STATUSES)
        .filter(Q(truck_id=shift.equipment_id) | Q(excavator_id=shift.equipment_id))
        .values_list("pk", flat=True)
    )


def enrich_snapshot(access, snapshot):
    snapshot["cards"] = actual_cards(access, snapshot["server_version"])
    shifts = EmployeeShift.objects.filter(closed_at__isnull=True, equipment__isnull=False)
    snapshot["refs"] = {
        "worker_shifts": [
            {
                "id": shift.pk,
                "employee_id": shift.employee_id,
                "equipment_id": shift.equipment_id,
                "opened_at": shift.opened_at.isoformat(),
                "open_trip_ids": worker_open_trips(shift),
            }
            for shift in shifts
        ],
        "rock_types": list(RockType.objects.filter(is_active=True).values("id", "name")),
        "dump_points": list(DumpPoint.objects.filter(is_active=True).values("id", "name")),
    }
    return snapshot


def decimal_string(value):
    try:
        return "" if value in (None, "") else str(Decimal(str(value).replace(",", ".")).normalize())
    except (InvalidOperation, ValueError):
        return str(value)


def card_base(settings):
    return {
        "loading_horizon": str(settings.get("loading_horizon") or ""),
        "loading_block": str(settings.get("loading_block") or ""),
        "rock_type_id": str(settings.get("rock_type_id") or ""),
        "destinations": sorted(
            (str(d["dump_point_id"]), decimal_string(d.get("transport_distance_km")))
            for d in settings["destinations"]
        ),
    }


def require_target(payload, key):
    from .field_sync import FieldProblem

    n = payload.get(key)
    if isinstance(n, bool) or not str(n).isdigit() or int(n) <= 0:
        raise FieldProblem("target_required", "Не указан действительный номер объекта действия.")
    return int(n)


def require_reference(obj, occurred, time_field=None):
    from .field_sync import FieldProblem

    if (
        not obj
        or time_field
        and getattr(obj, time_field, None)
        and getattr(obj, time_field) > occurred
    ):
        raise FieldProblem(
            "target_changed", "Объект действия изменился или появился позже записи планшета."
        )


def reconcile_local_assignment_tokens(event, payload, current):
    """Resolve a locally chained assignment ID only against signed, accepted history."""
    from core.models import OfflineFieldEvent, OfflineFieldEventStatus

    base = event.get("base_payload")
    if (
        not event.get("depends_on")
        or not isinstance(base, dict)
        or not isinstance(base.get("excavators"), list)
    ):
        return
    targets = {}
    for row in base["excavators"]:
        if not isinstance(row, dict) or not isinstance(row.get("truck_ids"), list):
            return
        for truck_id in row["truck_ids"]:
            targets[str(truck_id)] = row.get("excavator_id")
    scope = OfflineFieldEvent.objects.filter(
        device_id=event["device_id"],
        role_code="mining_master",
        status=OfflineFieldEventStatus.ACCEPTED,
        context_snapshot__local_shift_id=event["local_shift_id"],
        context_snapshot__actor_key=event["actor_key"],
        sequence__lt=event["sequence"],
    )
    needed = {}
    if payload.get("truck_id") is not None and "expected_assignment_state_id" in payload:
        needed[str(payload["truck_id"])] = payload["expected_assignment_state_id"]
    if isinstance(payload.get("expected_assignment_states"), dict):
        needed.update(payload["expected_assignment_states"])
    unresolved = {
        key
        for key, expected in needed.items()
        if str(current.get(key, {}).get("id")) != str(expected)
    }
    if not unresolved:
        return
    frontier = set(event["depends_on"])
    checked_ids = set()
    states = {}
    # Stay inside the signed dependency graph, never select "the last assignment"
    # on the device without proof that this command saw it.
    while frontier and unresolved and len(checked_ids) < 512:
        checked_ids.update(frontier)
        parents = set()
        for prior in scope.filter(event_id__in=frontier).order_by("-sequence"):
            parents.update(prior.depends_on)
            for key, value in prior.result_payload.get("topology_states", {}).items():
                if key in unresolved and value == current.get(key):
                    states[key] = value
                    unresolved.remove(key)
        frontier = parents - checked_ids

    def checked(key, expected):
        actual = current.get(key)
        if actual and states.get(key) == actual and targets.get(key) == actual["target"]:
            return actual["id"]
        return expected

    if "expected_assignment_state_id" in payload and payload.get("truck_id") is not None:
        key = str(payload["truck_id"])
        payload["expected_assignment_state_id"] = checked(
            key, payload["expected_assignment_state_id"]
        )
    if isinstance(payload.get("expected_assignment_states"), dict):
        payload["expected_assignment_states"] = {
            str(key): checked(str(key), expected)
            for key, expected in payload["expected_assignment_states"].items()
        }


def prepare(access, event, occurred):
    from .field_sync import FieldProblem
    from .models import ExcavatorPlacement, HaulAssignment
    from trips import views
    from reports import views as reports

    kind, payload = event["type"], dict(event["payload"])
    base = event.get("base_payload") or {}
    if len(payload) > 160 or not isinstance(base, dict):
        raise FieldProblem("invalid_payload", "Некорректные поля действия.")
    version = lock_production_state().version
    if kind in ("live.move", "live.assign"):
        from .field_plan import _live_state

        current = _live_state()[0]
        reconcile_local_assignment_tokens(event, payload, current["trucks"])
        excavator_id = payload.get("excavator_id")
        desired_zone = (
            payload.get("zone")
            if kind == "live.move"
            else ("active" if payload.get("action") == "assign" else None)
        )
        actual_zone = current["zones"].get(str(excavator_id))
        if desired_zone and desired_zone != actual_zone and excavator_id:
            placement = ExcavatorPlacement.objects.filter(excavator_id=excavator_id).first()
            if placement and placement.changed_at > occurred:
                raise FieldProblem(
                    "zone_changed", "Размещение экскаватора изменено после времени полевой записи."
                )
            if (
                kind == "live.assign"
                and isinstance(base.get("excavators"), list)
                and any(
                    row.get("excavator_id") == excavator_id
                    for row in base["excavators"]
                    if isinstance(row, dict)
                )
            ):
                raise FieldProblem(
                    "zone_changed", "После сохранения черновика комплекс вернули в гараж."
                )
        payload["client_action_id"] = event["id"]
        # Packet IDs are mandatory for legacy commands. Own pending predecessors
        # are resolved explicitly from earlier signed receipts, never guessed.
        return (
            (
                views.dispatcher_move_excavator_view
                if kind == "live.move"
                else views.dispatcher_assign_truck_view
            ),
            (),
            payload,
        )
    if kind == "card.settings":
        equipment_id = require_target(payload, "equipment_id")
        category = payload.get("category", "complex")
        equipment = (
            Equipment.objects.select_for_update()
            .select_related("equipment_type")
            .filter(pk=equipment_id, is_active=True)
            .first()
        )
        require_reference(equipment, occurred)
        settings = views.dispatcher_excavator_settings(
            equipment,
            ExcavatorPlacement.objects.filter(excavator=equipment).first(),
            rock_types=RockType.objects.filter(is_active=True),
            dump_points=DumpPoint.objects.filter(is_active=True),
        )
        if (
            not settings
            or not all(k in base for k in CARD_FIELDS)
            or card_base(settings) != card_base(base)
        ):
            raise FieldProblem(
                "settings_changed", "Настройки экскаватора изменились после чтения планшетом."
            )
        payload["state_version"] = version
        return views.dispatcher_equipment_detail_view, (category, equipment_id), payload
    if kind == "downtime.close":
        pk = require_target(payload, "downtime_id")
        down = DowntimeEvent.objects.select_for_update().filter(pk=pk).first()
        require_reference(down, occurred, "started_at")
        if down.ended_at or base.get("equipment_id") not in (None, down.equipment_id):
            raise FieldProblem(
                "downtime_changed", "Простой уже закрыт или относится к другой машине."
            )
        payload["state_version"] = version
        return views.dispatcher_close_downtime_view, (pk,), payload
    if kind == "worker_shift.close":
        pk = require_target(payload, "shift_id")
        shift = (
            EmployeeShift.objects.select_for_update().filter(pk=pk, equipment__isnull=False).first()
        )
        require_reference(shift, occurred, "opened_at")
        if (
            shift.closed_at
            or base.get("equipment_id") not in (None, shift.equipment_id)
            or base.get("employee_id") not in (None, shift.employee_id)
            or base.get("open_trip_ids") != worker_open_trips(shift)
        ):
            raise FieldProblem(
                "worker_shift_changed", "Состав открытых рейсов или смена сотрудника изменились."
            )
        return views.dispatcher_service_close_shift_view, (pk,), payload
    if kind == "assignment.cancel":
        pk = require_target(payload, "assignment_id")
        obj = HaulAssignment.objects.select_for_update().filter(pk=pk).first()
        require_reference(obj, occurred, "assigned_at")
        if obj.ended_at or base.get("expected_status") not in (None, obj.status):
            raise FieldProblem("assignment_changed", "Назначение уже изменено.")
        return views.dispatcher_cancel_assignment_view, (pk,), payload
    if kind in ("trip.cancel", "trip.complete", "report.trip_correct"):
        pk = require_target(payload, "trip_id")
        trip = Trip.objects.select_for_update().filter(pk=pk).first()
        require_reference(trip, occurred, "loaded_at")
        if base.get("expected_status") not in (None, trip.status):
            raise FieldProblem("trip_changed", "Статус рейса изменился.")
        if kind == "report.trip_correct":
            for k in REPORT_FIELDS:
                if k not in base:
                    raise FieldProblem("base_required", "Нет исходных значений полей отчёта.")
                a, b = getattr(trip, k), base[k]
                equal = (
                    decimal_string(a) == decimal_string(b)
                    if k in ("transport_distance_km", "volume_m3")
                    else str(a or "") == str(b or "")
                )
                if not equal:
                    raise FieldProblem("report_changed", "Рейс изменён после открытия отчёта.")
            return reports.dispatcher_shift_report_view, ("trucks",), payload
        if trip.completed_at or trip.cancelled_at:
            raise FieldProblem("trip_changed", "Рейс уже завершён или отменён.")
        return (
            (
                views.dispatcher_cancel_trip_view
                if kind == "trip.cancel"
                else views.dispatcher_complete_trip_view
            ),
            (pk,),
            payload,
        )
    if kind == "trip.manual":
        pk = require_target(payload, "truck_id")
        return views.dispatcher_manual_trip_view, (pk,), payload
    if kind == "report.template_save":
        if payload.get("template_id"):
            pk = require_target(payload, "template_id")
            template = ReportTemplate.objects.select_for_update().filter(pk=pk).first()
            require_reference(template, occurred)
            expected = {
                "name": template.name,
                "columns": template.columns,
                "group_by": template.group_by,
                "is_active": template.is_active,
            }
            expected.update({f"column_label_{k}": v for k, v in template.column_labels.items()})
            expected.update(template.filters)
            if not all(k in base for k in ("name", "columns", "group_by", "is_active")):
                raise FieldProblem("base_required", "Нет исходного шаблона отчёта.")
            check = set(expected) | {
                k
                for k in base
                if k.startswith("column_label_") or k in reports.REPORT_TEMPLATE_FILTER_FIELDS
            }
            for k in check:
                a, b = expected.get(k, ""), base.get(k, "")
                if k == "is_active":
                    b = b in (True, "true", "on", "1")
                if (
                    k.startswith("column_label_")
                    and b == reports.VOLUME_REPORT_COLUMNS.get(k[13:], ("?",))[0]
                ):
                    b = ""
                if a != b:
                    raise FieldProblem("template_changed", "Шаблон отчёта изменён.")
        return reports.report_template_builder_view, (), payload
    raise FieldProblem("unknown_command", "Эта операция планшета неизвестна.")


def execute(access, event, occurred):
    from .field_sync import FieldProblem

    fn, args, payload = prepare(access, event, occurred)
    req = trusted_request(
        access, payload=payload, as_json=event["type"] in JSON_COMMANDS, occurred=occurred
    )
    if event["type"] == "report.trip_correct":
        req.GET = QueryDict(urlencode({k: payload.get(k, "") for k in ("date", "shift_type")}))
    old = get_urlconf()
    try:
        set_urlconf(req.urlconf)
        response = fn(req, *args)
    finally:
        set_urlconf(old)
    if response.get("Content-Type", "").startswith("application/json"):
        body = json.loads(response.content)
        if response.status_code != 200 or body.get("ok") is False:
            raise FieldProblem(
                str(body.get("error") or "command_rejected"),
                str(body.get("message") or body.get("error") or "Система отклонила действие."),
            )
    elif 300 <= response.status_code < 400:
        notices = list(messages.get_messages(req))
        if not any(n.level == messages.SUCCESS for n in notices) or any(
            n.level >= messages.ERROR for n in notices
        ):
            raise FieldProblem(
                "command_rejected",
                str(notices[-1]) if notices else "Система не подтвердила изменение.",
            )
    else:
        raise FieldProblem("command_rejected", "Система отклонила действие.")
    return {}
