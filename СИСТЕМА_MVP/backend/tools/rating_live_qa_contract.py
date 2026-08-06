"""Fail-closed JSON contract shared by the local live-rating QA harness."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4


LIVE_STATE_SCHEMA = "driver-rating-qa-live-state"
LIVE_STATE_SCHEMA_VERSION = 1
LIVE_MANIFEST_SCHEMA = "driver-rating-qa-live-run"
LIVE_MANIFEST_SCHEMA_VERSION = 1
LIVE_STATE_FILENAME = "live_state.json"
LIVE_MANIFEST_FILENAME = "run_manifest.json"
LIVE_RUN_ID_ENV = "RATING_TV_QA_LIVE_RUN_ID"
LIVE_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_LIVE_STATE_BYTES = 256 * 1024
MAX_LIVE_STATE_AGE_SECONDS = 120
VALID_SHIFT_TYPES = frozenset({"day", "night"})
VALID_PLACEHOLDER_STATUSES = frozenset({"withheld", "not_observed"})
LIVE_STATE_KEYS = frozenset({
    "schema",
    "schema_version",
    "synthetic",
    "official",
    "official_rating_eligible",
    "run_id",
    "site_code",
    "rating_period_id",
    "watch_composition_id",
    "step",
    "virtual_at",
    "shift_type",
    "placeholders",
})
PLACEHOLDER_KEYS = frozenset({
    "employee_id",
    "status",
    "reasons",
})

_FORBIDDEN_KEYS = frozenset({
    "blocks",
    "final_score",
    "kpi",
    "place",
    "score",
    "shift_score",
    "weights",
})


class RatingLiveQAContractError(RuntimeError):
    """A local QA state failed a safety or information-boundary check."""


def _aware_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RatingLiveQAContractError(
            "virtual_at должен быть непустой ISO datetime."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RatingLiveQAContractError(
            "virtual_at должен быть корректным ISO datetime."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RatingLiveQAContractError(
            "virtual_at должен содержать часовой пояс."
        )
    return parsed


def _reject_rating_fields(
    value: Any,
    *,
    path: str = "$",
    reject_fingerprints: bool = True,
) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if (
                normalized in _FORBIDDEN_KEYS
                or normalized in {"places", "scores"}
                or normalized.endswith("_place")
                or normalized.endswith("_score")
                or normalized.endswith("_scores")
                or (
                    reject_fingerprints
                    and "fingerprint" in normalized
                )
            ):
                raise RatingLiveQAContractError(
                    f"QA sidecar не может содержать поле рейтинга {path}.{key}."
                )
            _reject_rating_fields(
                nested,
                path=f"{path}.{key}",
                reject_fingerprints=reject_fingerprints,
            )
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_rating_fields(
                nested,
                path=f"{path}[{index}]",
                reject_fingerprints=reject_fingerprints,
            )


def validate_live_run_id(
    run_id: Any,
    *,
    configured_run_id: Any,
) -> str:
    normalized = str(run_id or "")
    configured = str(configured_run_id or "")
    if not configured:
        raise RatingLiveQAContractError(
            f"{LIVE_RUN_ID_ENV} не задан; live-QA выключен."
        )
    if LIVE_RUN_ID_PATTERN.fullmatch(normalized) is None:
        raise RatingLiveQAContractError(
            "run_id должен соответствовать "
            "[A-Za-z0-9][A-Za-z0-9._-]{0,63}."
        )
    if not normalized or normalized != configured:
        raise RatingLiveQAContractError(
            "run_id не совпадает с явно разрешённым live-QA запуском."
        )
    return normalized


def build_placeholders(
    *,
    expected_employee_ids: Sequence[int],
    observed_employee_ids: Sequence[int],
    closed_employee_ids: Sequence[int],
    withheld_reasons: Mapping[str, int] | None,
) -> list[dict[str, Any]]:
    expected = {int(value) for value in expected_employee_ids}
    observed = {int(value) for value in observed_employee_ids}
    closed = {int(value) for value in closed_employee_ids}
    if not observed.issubset(expected) or not closed.issubset(expected):
        raise RatingLiveQAContractError(
            "Наблюдаемые или закрытые смены вышли за состав QA-группы."
        )
    global_reasons = sorted(
        str(reason).strip()
        for reason, count in (withheld_reasons or {}).items()
        if str(reason).strip() and int(count or 0) > 0
    )
    placeholders = []
    for employee_id in sorted(expected - observed):
        if employee_id in closed:
            status = "withheld"
            reasons = global_reasons or ["withheld_by_formula"]
        else:
            status = "not_observed"
            reasons = ["no_closed_shift"]
        placeholders.append({
            "employee_id": employee_id,
            "status": status,
            "reasons": reasons,
        })
    return placeholders


def validate_live_state(
    payload: Any,
    *,
    configured_run_id: Any,
) -> bytes:
    if not isinstance(payload, dict):
        raise RatingLiveQAContractError(
            "QA sidecar должен быть JSON-объектом."
        )
    if set(payload) != LIVE_STATE_KEYS:
        raise RatingLiveQAContractError(
            "QA sidecar содержит неверный набор полей."
        )
    validate_live_run_id(
        payload.get("run_id"),
        configured_run_id=configured_run_id,
    )
    required_flags = {
        "synthetic": True,
        "official": False,
        "official_rating_eligible": False,
    }
    if (
        payload.get("schema") != LIVE_STATE_SCHEMA
        or payload.get("schema_version") != LIVE_STATE_SCHEMA_VERSION
    ):
        raise RatingLiveQAContractError(
            "Неверная схема live-QA sidecar."
        )
    if any(payload.get(key) is not expected for key, expected in required_flags.items()):
        raise RatingLiveQAContractError(
            "Live-QA sidecar обязан оставаться синтетическим и неофициальным."
        )
    if not isinstance(payload.get("site_code"), str) or not payload["site_code"].strip():
        raise RatingLiveQAContractError("site_code не задан.")
    for identifier_key in (
        "rating_period_id",
        "watch_composition_id",
    ):
        identifier = payload.get(identifier_key)
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or identifier <= 0
        ):
            raise RatingLiveQAContractError(
                f"{identifier_key} должен быть положительным числом."
            )
    step = payload.get("step")
    if isinstance(step, bool) or not isinstance(step, int) or step < 0:
        raise RatingLiveQAContractError(
            "step должен быть целым неотрицательным числом."
        )
    _aware_datetime(payload.get("virtual_at"))
    if payload.get("shift_type") not in VALID_SHIFT_TYPES:
        raise RatingLiveQAContractError(
            "shift_type должен быть day или night."
        )
    placeholders = payload.get("placeholders")
    if not isinstance(placeholders, list):
        raise RatingLiveQAContractError(
            "placeholders должен быть массивом."
        )
    employee_ids = set()
    for placeholder in placeholders:
        if (
            not isinstance(placeholder, dict)
            or set(placeholder) != PLACEHOLDER_KEYS
        ):
            raise RatingLiveQAContractError(
                "Каждый placeholder должен иметь точную схему."
            )
        employee_id = placeholder.get("employee_id")
        if (
            isinstance(employee_id, bool)
            or not isinstance(employee_id, int)
            or employee_id <= 0
            or employee_id in employee_ids
        ):
            raise RatingLiveQAContractError(
                "employee_id placeholder должен быть уникальным положительным числом."
            )
        employee_ids.add(employee_id)
        if placeholder.get("status") not in VALID_PLACEHOLDER_STATUSES:
            raise RatingLiveQAContractError(
                "Недопустимый status placeholder."
            )
        reasons = placeholder.get("reasons")
        if (
            not isinstance(reasons, list)
            or not reasons
            or len(reasons) > 20
            or any(
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason.strip()) > 160
                for reason in reasons
            )
        ):
            raise RatingLiveQAContractError(
                "reasons placeholder должен содержать непустые строки."
            )
    _reject_rating_fields(payload, reject_fingerprints=True)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_LIVE_STATE_BYTES:
        raise RatingLiveQAContractError(
            "Live-QA sidecar превышает 256 KiB."
        )
    return encoded


def atomic_write_live_state(
    path: Path,
    payload: dict[str, Any],
    *,
    configured_run_id: str,
) -> None:
    encoded = validate_live_state(
        payload,
        configured_run_id=configured_run_id,
    )
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_live_manifest(
    path: Path,
    payload: dict[str, Any],
    *,
    configured_run_id: str,
) -> None:
    validate_live_run_id(
        payload.get("run_id"),
        configured_run_id=configured_run_id,
    )
    if (
        payload.get("schema") != LIVE_MANIFEST_SCHEMA
        or payload.get("schema_version") != LIVE_MANIFEST_SCHEMA_VERSION
        or payload.get("synthetic") is not True
        or payload.get("official") is not False
        or payload.get("official_rating_eligible") is not False
    ):
        raise RatingLiveQAContractError(
            "Неверная схема или классификация live-QA manifest."
        )
    _reject_rating_fields(payload, reject_fingerprints=False)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_LIVE_STATE_BYTES:
        raise RatingLiveQAContractError(
            "Live-QA manifest превышает 256 KiB."
        )
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
