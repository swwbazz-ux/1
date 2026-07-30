#!/usr/bin/env python
"""Run an accelerated, materialized driver-rating scenario in an isolated DB.

One simulation day contains two complete 12-hour production shifts: day and
night. The script never estimates an open shift. Every visible change follows
the production chain: HTTP events, EmployeeShift closure, on-commit passport
capture, materialized refresh, then the materialized reader.
"""

from __future__ import annotations

import argparse
import copy
import os
import stat
import tempfile
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.db import connection, transaction  # noqa: E402

from assignments.models import WorkShiftType  # noqa: E402
from reports.driver_rating_materialization import (  # noqa: E402
    get_materialized_driver_rating_period,
    refresh_driver_rating_group,
)
from reports.driver_rating_scope_membership import (  # noqa: E402
    discover_driver_rating_group_scope,
)
from reports import driver_shift_passport_snapshots as passport_service  # noqa: E402
from reports.driver_shift_passport_snapshots import (  # noqa: E402
    enqueue_driver_shift_passport_rebuild,
    process_driver_shift_passport_request,
)
from reports.models import (  # noqa: E402
    DriverRatingPeriodMaterializedSnapshot,
    DriverShiftPassportCaptureRequest,
    DriverShiftPassportRequestStatus,
    DriverShiftPassportSnapshot,
    DriverShiftPassportTrigger,
    RatingPeriod,
)
from shifts.models import EmployeeShift, WatchPeriod  # noqa: E402
from tools.generate_driver_rating_30d_qa import (  # noqa: E402
    DAY_BRIGADE,
    DEFAULT_START_DATE,
    EXPECTED_DRIVER_COUNT,
    NIGHT_BRIGADE,
    Rating30dConfig,
    Rating30dOnboarding,
    Rating30dQAError,
    Rating30dRunner,
    Rating30dScope,
    ensure_new_artifact_directory,
)
from tools.full_week_qa import ReferenceCatalog, local_dt  # noqa: E402
from tools.prepare_rating_30d_qa_database import (  # noqa: E402
    business_table_counts,
    protected_business_models,
)
from tools.rating_live_qa_contract import (  # noqa: E402
    LIVE_MANIFEST_FILENAME,
    LIVE_MANIFEST_SCHEMA,
    LIVE_MANIFEST_SCHEMA_VERSION,
    LIVE_RUN_ID_ENV,
    LIVE_STATE_FILENAME,
    LIVE_STATE_SCHEMA,
    LIVE_STATE_SCHEMA_VERSION,
    RatingLiveQAContractError,
    atomic_write_live_manifest,
    atomic_write_live_state,
    build_placeholders,
    validate_live_run_id,
)
from trips.models import OPEN_TRIP_STATUSES, Trip  # noqa: E402
from users.models import Employee, WatchComposition  # noqa: E402


TARGET_DB_ENGINE = "django.db.backends.postgresql"
TARGET_DB_NAME = "copper_rating_live_qa_20260730"
TARGET_DB_USER = "copper_rating_live_qa_runner"
TARGET_DB_HOST = "127.0.0.1"
TARGET_DB_PORT = "55436"

DEFAULT_MARKER = "ТЕСТ_РЕЙТИНГ_LIVE_20260730"
ALLOWED_ARTIFACT_ROOT = (
    Path(tempfile.gettempdir())
    / "copper-rating-live-qa-20260730"
)
DEFAULT_STEP_SECONDS = 12
MIN_STEP_SECONDS = 10
MAX_STEP_SECONDS = 15
DEFAULT_VIEWER_DELAY_SECONDS = 30
MIN_VIEWER_DELAY_SECONDS = 0
MAX_VIEWER_DELAY_SECONDS = 300
MIN_SIMULATION_DAYS = 1
MAX_SIMULATION_DAYS = 30
CHECKPOINT_24H_FILENAME = "checkpoint_24h.json"
LIVE_STATE_HEARTBEAT_SECONDS = 30


class RatingLiveQAError(Rating30dQAError):
    """The live scenario stopped without weakening a safety invariant."""


@dataclass(frozen=True)
class MaterializedSnapshotState:
    snapshot_id: int
    revision: int
    scope_fingerprint: str
    source_fingerprint: str
    shift_score_fingerprint: str
    payload_fingerprint: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class MaterializedRefreshEvidence:
    baseline: MaterializedSnapshotState | None
    current: MaterializedSnapshotState
    statuses: tuple[str, str, str]
    changed: tuple[bool, bool, bool]

    def manifest_payload(self) -> dict[str, Any]:
        return {
            "baseline_revision": (
                self.baseline.revision
                if self.baseline is not None
                else 0
            ),
            "revision": self.current.revision,
            "statuses": list(self.statuses),
            "changed": list(self.changed),
            "scope_fingerprint": self.current.scope_fingerprint,
            "source_fingerprint": self.current.source_fingerprint,
            "shift_score_fingerprint": (
                self.current.shift_score_fingerprint
            ),
            "payload_fingerprint": self.current.payload_fingerprint,
        }


class LiveStateHeartbeat:
    """Keep the last complete sidecar fresh while a synthetic shift runs."""

    def __init__(
        self,
        *,
        path: Path,
        configured_run_id: str,
        interval_seconds: float = LIVE_STATE_HEARTBEAT_SECONDS,
        writer: Callable[..., None] = atomic_write_live_state,
    ):
        if interval_seconds <= 0:
            raise ValueError("Heartbeat interval must be positive.")
        self.path = path
        self.configured_run_id = configured_run_id
        self.interval_seconds = interval_seconds
        self.writer = writer
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._payload: dict[str, Any] | None = None
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._last_published_step = -1

    def _write_current(self) -> None:
        with self._lock:
            if self._error is not None:
                raise RatingLiveQAError(
                    "Не удалось обновить heartbeat live-QA sidecar."
                ) from self._error
            if self._payload is None:
                return
            try:
                self.writer(
                    self.path,
                    self._payload,
                    configured_run_id=self.configured_run_id,
                )
            except Exception as error:
                self._error = error
                self._stop_event.set()
                raise

    def publish(self, payload: dict[str, Any]) -> None:
        with self._lock:
            if self._error is not None:
                raise RatingLiveQAError(
                    "Не удалось обновить heartbeat live-QA sidecar."
                ) from self._error
            step = payload.get("step")
            if (
                isinstance(step, bool)
                or not isinstance(step, int)
                or step <= self._last_published_step
            ):
                raise RatingLiveQAError(
                    "Новый live-QA frame должен иметь строго больший step."
                )
            self._payload = copy.deepcopy(payload)
            try:
                self.writer(
                    self.path,
                    self._payload,
                    configured_run_id=self.configured_run_id,
                )
            except Exception as error:
                self._error = error
                self._stop_event.set()
                raise
            self._last_published_step = step

    def begin_frame(self) -> None:
        """Remove the old sidecar before any materialized state can change."""

        with self._lock:
            previous_error = self._error
            self._payload = None
            try:
                self.path.unlink(missing_ok=True)
            except Exception as error:
                self._error = error
                self._stop_event.set()
                raise RatingLiveQAError(
                    "Не удалось удалить устаревший live-QA sidecar."
                ) from error
            if previous_error is not None:
                self._stop_event.set()
                raise RatingLiveQAError(
                    "Не удалось приостановить heartbeat live-QA sidecar."
                ) from previous_error

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self._write_current()
            except Exception:
                return

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Heartbeat already started.")
        self._thread = threading.Thread(
            target=self._run,
            name="driver-rating-live-qa-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def raise_if_failed(self) -> None:
        with self._lock:
            if self._error is not None:
                raise RatingLiveQAError(
                    "Не удалось обновить heartbeat live-QA sidecar."
                ) from self._error

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def configured_database_identity(
    database: dict[str, Any],
) -> tuple[str, ...]:
    return (
        str(database.get("ENGINE") or ""),
        str(database.get("NAME") or ""),
        str(database.get("USER") or ""),
        str(database.get("HOST") or ""),
        str(database.get("PORT") or ""),
    )


def validate_configured_database(database: dict[str, Any]) -> None:
    actual = configured_database_identity(database)
    expected = (
        TARGET_DB_ENGINE,
        TARGET_DB_NAME,
        TARGET_DB_USER,
        TARGET_DB_HOST,
        TARGET_DB_PORT,
    )
    if actual != expected:
        raise RatingLiveQAError(
            "Защитная остановка: live-QA разрешён только для отдельной "
            f"PostgreSQL-БД {TARGET_DB_NAME}@{TARGET_DB_HOST}:"
            f"{TARGET_DB_PORT}, роль={TARGET_DB_USER}; получено "
            f"identity={actual!r}."
        )


def validate_simulation_days(value: int) -> int:
    if (
        isinstance(value, bool)
        or not MIN_SIMULATION_DAYS <= int(value) <= MAX_SIMULATION_DAYS
    ):
        raise RatingLiveQAError(
            "simulation-days должен быть от 1 до 30 календарных дней "
            "(2–60 закрытых дневных/ночных смен)."
        )
    return int(value)


def validate_step_seconds(value: int) -> int:
    if (
        isinstance(value, bool)
        or not MIN_STEP_SECONDS <= int(value) <= MAX_STEP_SECONDS
    ):
        raise RatingLiveQAError(
            "step-seconds должен быть от 10 до 15 секунд."
        )
    return int(value)


def validate_viewer_delay_seconds(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_VIEWER_DELAY_SECONDS
        <= value
        <= MAX_VIEWER_DELAY_SECONDS
    ):
        raise RatingLiveQAError(
            "viewer-delay-seconds должен быть целым числом "
            "от 0 до 300 секунд."
        )
    return value


def open_live_viewer_window(
    *,
    heartbeat: LiveStateHeartbeat,
    state: dict[str, Any],
    viewer_delay_seconds: int,
    write_manifest: Callable[[], None],
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Publish step zero and wait once before the first simulated shift."""

    delay_seconds = validate_viewer_delay_seconds(
        viewer_delay_seconds
    )
    heartbeat.publish(state)
    heartbeat.start()
    write_manifest()
    sleeper(delay_seconds)


def viewer_window_manifest_metadata(
    *,
    state: dict[str, Any],
    viewer_delay_seconds: int,
) -> dict[str, Any]:
    delay_seconds = validate_viewer_delay_seconds(
        viewer_delay_seconds
    )
    if (
        state.get("step") != 0
        or not isinstance(state.get("virtual_at"), str)
        or not state["virtual_at"]
    ):
        raise RatingLiveQAError(
            "Окно подключения разрешено только для стартового кадра step=0."
        )
    return {
        "viewer_delay_seconds": delay_seconds,
        "initial_status": {
            "phase": "viewer_window",
            "step": 0,
            "virtual_at": state["virtual_at"],
        },
    }


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first in second.parents
        or second in first.parents
    )


def _path_is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _assert_plain_artifact_path(
    *paths: Path,
) -> None:
    checked: set[Path] = set()
    for path in paths:
        for component in (path, *path.parents):
            if component in checked:
                continue
            checked.add(component)
            is_junction = getattr(
                component,
                "is_junction",
                lambda: False,
            )()
            if (
                component.is_symlink()
                or is_junction
                or _path_is_reparse_point(component)
            ):
                raise RatingLiveQAError(
                    "Symlink/junction/reparse запрещён в пути "
                    f"live-QA артефактов: {component}."
                )


def _default_protected_artifact_roots() -> tuple[Path, ...]:
    roots = {
        BACKEND_DIR,
        BACKEND_DIR.parent,
        BACKEND_DIR.parents[1],
    }
    configured_base_dir = getattr(settings, "BASE_DIR", None)
    if configured_base_dir:
        roots.add(Path(configured_base_dir))
    return tuple(sorted(roots, key=str))


def resolve_live_artifact_directory(
    *,
    marker: str,
    run_id: str,
    artifact_dir: Path | None,
    allowed_root: Path | None = None,
    protected_roots: tuple[Path, ...] | None = None,
) -> Path:
    """Return the only directory this live-QA run may create."""

    if marker != DEFAULT_MARKER:
        raise RatingLiveQAError(
            "Live-QA marker должен точно совпадать с DEFAULT_MARKER."
        )
    root = Path(allowed_root or ALLOWED_ARTIFACT_ROOT)
    if not root.is_absolute():
        raise RatingLiveQAError(
            "Разрешённый корень live-QA артефактов должен быть абсолютным."
        )
    marker_directory = root / DEFAULT_MARKER
    expected = marker_directory / run_id
    requested = expected if artifact_dir is None else Path(artifact_dir)
    if (
        not requested.is_absolute()
        or requested != expected
    ):
        raise RatingLiveQAError(
            "Каталог live-QA артефактов должен точно совпадать с "
            f"{expected}."
        )

    _assert_plain_artifact_path(
        root,
        marker_directory,
        expected,
        requested,
    )

    resolved_root = root.resolve(strict=False)
    resolved_expected = expected.resolve(strict=False)
    resolved_requested = requested.resolve(strict=False)
    try:
        resolved_expected.relative_to(resolved_root)
    except ValueError as error:
        raise RatingLiveQAError(
            "Каталог live-QA вышел за разрешённый временный корень."
        ) from error
    if resolved_requested != resolved_expected:
        raise RatingLiveQAError(
            "Разрешён только канонический каталог текущего live-QA запуска."
        )

    protected = (
        _default_protected_artifact_roots()
        if protected_roots is None
        else protected_roots
    )
    for protected_root in protected:
        resolved_protected = Path(protected_root).resolve(strict=False)
        if _paths_overlap(resolved_expected, resolved_protected):
            raise RatingLiveQAError(
                "Каталог live-QA пересекается с backend/workspace: "
                f"{resolved_protected}."
            )
    return resolved_expected


def continuous_execution_contract(
    simulation_days: int,
) -> dict[str, Any]:
    simulation_days = validate_simulation_days(simulation_days)
    return {
        "mode": "single_process_from_empty_database",
        "cross_process_resume": False,
        "checkpoint_after_shift": 2,
        "continues_after_24h_in_same_process": simulation_days >= 2,
        "target_calendar_days": simulation_days,
        "target_closed_shift_count": simulation_days * 2,
    }


def verify_live_database(*, require_empty: bool) -> dict[str, Any]:
    validate_configured_database(settings.DATABASES["default"])
    if connection.vendor != "postgresql":
        raise RatingLiveQAError(
            "Фактическое соединение live-QA не является PostgreSQL."
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "select current_database(), inet_server_addr()::text, "
            "inet_server_port()::text, current_user"
        )
        actual_name, actual_host, actual_port, actual_user = cursor.fetchone()
    actual = (
        str(actual_name or ""),
        str(actual_host or ""),
        str(actual_port or ""),
        str(actual_user or ""),
    )
    expected = (
        TARGET_DB_NAME,
        TARGET_DB_HOST,
        TARGET_DB_PORT,
        TARGET_DB_USER,
    )
    if (
        actual[0] != expected[0]
        or actual[1] not in {expected[1], f"{expected[1]}/32"}
        or actual[2:] != expected[2:]
    ):
        raise RatingLiveQAError(
            "Фактическая PostgreSQL identity не совпадает с live-QA: "
            f"ожидалось {expected!r}, получено {actual!r}."
        )

    counts = business_table_counts(protected_business_models())
    nonempty = {
        label: count
        for label, count in counts.items()
        if count
    }
    if require_empty and nonempty:
        raise RatingLiveQAError(
            "Live-QA не очищает и не дополняет непустую БД; найдены "
            f"бизнес-строки: {dict(list(nonempty.items())[:12])!r}."
        )
    return {
        "engine": TARGET_DB_ENGINE,
        "name": actual[0],
        "user": actual[3],
        "host": actual[1],
        "port": actual[2],
        "protected_business_rows": sum(counts.values()),
    }


def create_live_scope(
    config: Rating30dConfig,
    onboarding: Rating30dOnboarding,
) -> Rating30dScope:
    composition = WatchComposition.objects.create(
        code="qa-rating-live-20260730",
        name="ТЕСТОВЫЙ СОСТАВ LIVE-РЕЙТИНГА 20260730",
        is_active=True,
    )
    day_employee_ids = tuple(sorted(
        member.employee_id
        for member in onboarding.drivers_by_brigade[DAY_BRIGADE].values()
    ))
    night_employee_ids = tuple(sorted(
        member.employee_id
        for member in onboarding.drivers_by_brigade[NIGHT_BRIGADE].values()
    ))
    if (
        len(day_employee_ids) != EXPECTED_DRIVER_COUNT
        or len(night_employee_ids) != EXPECTED_DRIVER_COUNT
        or set(day_employee_ids) & set(night_employee_ids)
    ):
        raise RatingLiveQAError(
            "Live-QA ожидает две непересекающиеся группы по 53 водителя."
        )
    driver_ids = (*day_employee_ids, *night_employee_ids)
    if (
        Employee.objects.filter(pk__in=driver_ids).update(
            watch_composition=composition
        )
        != EXPECTED_DRIVER_COUNT * 2
    ):
        raise RatingLiveQAError(
            "Не удалось привязать к тестовому составу ровно 106 водителей."
        )

    watch_period = WatchPeriod.objects.create(
        name=(
            "ТЕСТОВАЯ LIVE-ВАХТА РЕЙТИНГА "
            f"{config.start_date:%d.%m.%Y}–{config.end_date:%d.%m.%Y}"
        ),
        watch_composition=composition,
        starts_on=config.start_date,
        ends_on=config.end_date,
        is_active=True,
    )
    rating_period = RatingPeriod.objects.create(
        name=(
            "ТЕСТОВЫЙ LIVE-ПЕРИОД РЕЙТИНГА "
            f"{config.start_date:%d.%m.%Y}–{config.end_date:%d.%m.%Y}"
        ),
        starts_on=config.start_date,
        ends_before=config.ends_before,
        comment=(
            f"{config.marker}: ускоренный синтетический live-QA; "
            "не использовать для KPI или премирования."
        ),
        is_active=True,
    )
    return Rating30dScope(
        composition=composition,
        watch_period=watch_period,
        rating_period=rating_period,
        day_employee_ids=day_employee_ids,
        night_employee_ids=night_employee_ids,
    )


class RatingLiveRunner(Rating30dRunner):
    """Incremental shift runner with explicit carryover reconciliation."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.carryover_origin_shift_id: int | None = None
        self.last_reconciled_shift_type: str | None = None
        self.delayed_passport_employee_id: int | None = None
        self.delayed_passport_request_id: int | None = None

    def carry_truck_for_boundary(self, shift_index: int):
        # A one-day smoke intentionally leaves the final carryover visible.
        # Runs of two or more days close the last planned boundary.
        if (
            self.config.day_count >= 2
            and shift_index == self.config.total_shift_count - 1
        ):
            return None
        return super().carry_truck_for_boundary(shift_index)

    @staticmethod
    def _rebuild_reconciled_passport(shift_id: int) -> str:
        shift = EmployeeShift.objects.get(pk=shift_id)
        with transaction.atomic():
            capture_request = enqueue_driver_shift_passport_rebuild(
                shift=shift,
                trigger=DriverShiftPassportTrigger.SOURCE_RECONCILE,
                schedule_on_commit=False,
            )
        snapshot = process_driver_shift_passport_request(
            capture_request.pk
        )
        if snapshot is None:
            raise RatingLiveQAError(
                f"Повторный паспорт смены {shift_id} не сформирован."
            )
        return shift.shift_type

    def _select_delayed_passport_employee_id(self) -> int:
        carry_truck = self.carry_truck_for_boundary(0)
        carry_truck_id = getattr(carry_truck, "id", None)
        for truck_id, member in sorted(
            self.onboarding.drivers_by_brigade[DAY_BRIGADE].items()
        ):
            if truck_id != carry_truck_id:
                return int(member.employee_id)
        raise RatingLiveQAError(
            "Не удалось выбрать дневного водителя вне carryover для "
            "проверки PENDING-паспорта."
        )

    def _validate_first_shift_passport_requests(
        self,
        *,
        expect_pending: bool,
    ) -> None:
        rows = list(
            DriverShiftPassportCaptureRequest.objects
            .filter(
                shift__watch_period=self.scope.watch_period,
                shift__workplace_code="driver",
                shift__shift_type=WorkShiftType.SHIFT_1,
                shift__closed_at__isnull=False,
            )
            .values(
                "id",
                "shift_id",
                "shift__employee_id",
                "status",
                "snapshot_id",
            )
        )
        target_rows = [
            row
            for row in rows
            if int(row["id"]) == self.delayed_passport_request_id
            and int(row["shift__employee_id"])
            == self.delayed_passport_employee_id
        ]
        if (
            len(rows) != EXPECTED_DRIVER_COUNT
            or len({int(row["shift_id"]) for row in rows})
            != EXPECTED_DRIVER_COUNT
            or len(target_rows) != 1
        ):
            raise RatingLiveQAError(
                "Первая дневная смена должна создать ровно 53 "
                "однозначных outbox-запроса, включая один target."
            )
        target = target_rows[0]
        other_rows = [
            row
            for row in rows
            if int(row["id"]) != self.delayed_passport_request_id
        ]
        if any(
            row["status"]
            != DriverShiftPassportRequestStatus.COMPLETED
            or row["snapshot_id"] is None
            for row in other_rows
        ):
            raise RatingLiveQAError(
                "Не все остальные паспорта первой смены COMPLETED."
            )
        if expect_pending:
            if (
                target["status"]
                != DriverShiftPassportRequestStatus.PENDING
                or target["snapshot_id"] is not None
                or sum(
                    row["status"]
                    == DriverShiftPassportRequestStatus.PENDING
                    for row in rows
                )
                != 1
            ):
                raise RatingLiveQAError(
                    "До process должен существовать ровно один реальный "
                    "PENDING target без snapshot."
                )
        elif (
            target["status"]
            != DriverShiftPassportRequestStatus.COMPLETED
            or target["snapshot_id"] is None
            or any(
                row["status"]
                != DriverShiftPassportRequestStatus.COMPLETED
                for row in rows
            )
        ):
            raise RatingLiveQAError(
                "После process все 53 паспорта должны быть COMPLETED."
            )

    def _run_first_shift_with_delayed_passport(self, shift_index: int):
        target_employee_id = self._select_delayed_passport_employee_id()
        original_processor = (
            passport_service.safe_process_driver_shift_passport_request
        )

        def delayed_processor(request_id):
            capture_request = (
                DriverShiftPassportCaptureRequest.objects
                .select_related("shift")
                .get(pk=request_id)
            )
            if capture_request.shift.employee_id != target_employee_id:
                return original_processor(request_id)
            if (
                self.delayed_passport_request_id is not None
                and self.delayed_passport_request_id
                != capture_request.pk
            ):
                raise RatingLiveQAError(
                    "Для одного withheld-водителя создано несколько "
                    "PENDING-запросов паспорта."
                )
            self.delayed_passport_employee_id = target_employee_id
            self.delayed_passport_request_id = capture_request.pk
            return None

        with patch.object(
            passport_service,
            "safe_process_driver_shift_passport_request",
            side_effect=delayed_processor,
        ):
            result = super().run_shift_step(shift_index)
        capture_request = self.get_delayed_passport_request()
        if (
            capture_request.status
            != DriverShiftPassportRequestStatus.PENDING
            or capture_request.snapshot_id is not None
        ):
            raise RatingLiveQAError(
                "Отложенный паспорт не остался в реальном статусе PENDING."
            )
        self._validate_first_shift_passport_requests(
            expect_pending=True,
        )
        return result

    def get_delayed_passport_request(
        self,
    ) -> DriverShiftPassportCaptureRequest:
        if self.delayed_passport_request_id is None:
            raise RatingLiveQAError(
                "HTTP-закрытие первой смены не создало отложенный "
                "outbox-запрос паспорта."
            )
        return (
            DriverShiftPassportCaptureRequest.objects
            .select_related("shift", "snapshot")
            .get(pk=self.delayed_passport_request_id)
        )

    def process_delayed_passport(
        self,
    ) -> DriverShiftPassportCaptureRequest:
        capture_request = self.get_delayed_passport_request()
        if (
            capture_request.status
            != DriverShiftPassportRequestStatus.PENDING
            or capture_request.snapshot_id is not None
        ):
            raise RatingLiveQAError(
                "Перед обработкой отложенный паспорт уже изменил статус."
            )
        snapshot = process_driver_shift_passport_request(
            capture_request.pk
        )
        capture_request.refresh_from_db()
        if (
            snapshot is None
            or capture_request.status
            != DriverShiftPassportRequestStatus.COMPLETED
            or capture_request.snapshot_id != snapshot.pk
        ):
            raise RatingLiveQAError(
                "Отложенный паспорт не перешёл PENDING → COMPLETED."
            )
        self._validate_first_shift_passport_requests(
            expect_pending=False,
        )
        return capture_request

    def run_shift_step(self, shift_index: int):
        reconciled_shift_id = self.carryover_origin_shift_id
        if shift_index == 0:
            result = self._run_first_shift_with_delayed_passport(
                shift_index
            )
        else:
            result = super().run_shift_step(shift_index)
        self.last_reconciled_shift_type = None
        if reconciled_shift_id is not None:
            self.last_reconciled_shift_type = (
                self._rebuild_reconciled_passport(reconciled_shift_id)
            )

        self.carryover_origin_shift_id = None
        if self.carryover_trip_id and self.carryover_truck_id:
            current_shift = (
                EmployeeShift.objects
                .filter(
                    watch_period=self.scope.watch_period,
                    workplace_code="driver",
                    shift_type=self.shift_type_for_index(shift_index),
                    equipment_id=self.carryover_truck_id,
                    closed_at__isnull=False,
                )
                .order_by("-closed_at", "-id")
                .first()
            )
            if current_shift is None:
                raise RatingLiveQAError(
                    "Не найдена закрытая смена-источник переходящего рейса."
                )
            self.carryover_origin_shift_id = current_shift.id
        return result


def read_materialized_snapshot_state(
    rating_period: RatingPeriod,
    watch_composition: WatchComposition,
    *,
    shift_type: str,
    scope_code: str,
) -> MaterializedSnapshotState | None:
    snapshot = (
        DriverRatingPeriodMaterializedSnapshot.objects
        .filter(
            scope_code=scope_code,
            rating_period=rating_period,
            watch_composition=watch_composition,
            shift_type=shift_type,
        )
        .first()
    )
    if snapshot is None:
        return None
    return MaterializedSnapshotState(
        snapshot_id=int(snapshot.pk),
        revision=int(snapshot.revision),
        scope_fingerprint=str(snapshot.scope_fingerprint or ""),
        source_fingerprint=str(snapshot.source_fingerprint or ""),
        shift_score_fingerprint=str(
            snapshot.shift_score_fingerprint or ""
        ),
        payload_fingerprint=str(snapshot.payload_fingerprint or ""),
        payload=copy.deepcopy(snapshot.payload),
    )


def _validate_snapshot_state(
    snapshot: MaterializedSnapshotState,
    *,
    label: str,
) -> None:
    fingerprints = (
        snapshot.scope_fingerprint,
        snapshot.source_fingerprint,
        snapshot.shift_score_fingerprint,
        snapshot.payload_fingerprint,
    )
    if (
        snapshot.snapshot_id <= 0
        or snapshot.revision <= 0
        or any(not value for value in fingerprints)
        or not isinstance(snapshot.payload, dict)
    ):
        raise RatingLiveQAError(
            f"{label}: materialized snapshot неполон или имеет revision=0."
        )
    if (
        passport_service._fingerprint(snapshot.payload)
        != snapshot.payload_fingerprint
        or str(snapshot.payload.get("source_fingerprint") or "")
        != snapshot.source_fingerprint
        or str(
            snapshot.payload.get("shift_score_fingerprint") or ""
        )
        != snapshot.shift_score_fingerprint
    ):
        raise RatingLiveQAError(
            f"{label}: row/payload fingerprints materialized snapshot "
            "не совпали."
        )


def _validate_refresh_phase(
    result: Any,
    *,
    expected_status: str,
    expected_changed: bool,
    label: str,
) -> None:
    if (
        getattr(result, "status", None) != expected_status
        or bool(getattr(result, "changed", None))
        is not expected_changed
        or int(getattr(result, "revision", 0) or 0) <= 0
    ):
        raise RatingLiveQAError(
            f"{label}: materialized refresh должен быть "
            f"{expected_status}/changed={expected_changed}, получено "
            f"status={getattr(result, 'status', None)!r}, "
            f"changed={getattr(result, 'changed', None)!r}, "
            f"revision={getattr(result, 'revision', None)!r}."
        )


def _refresh_result_invariant(
    initial: Any,
    repeated: Any,
    third: Any,
    *,
    baseline: MaterializedSnapshotState | None,
    initial_snapshot: MaterializedSnapshotState,
    repeated_snapshot: MaterializedSnapshotState,
    third_snapshot: MaterializedSnapshotState,
    require_shift_score_change: bool,
) -> MaterializedRefreshEvidence:
    if baseline is not None:
        _validate_snapshot_state(baseline, label="baseline")
    for label, snapshot in (
        ("initial", initial_snapshot),
        ("repeated", repeated_snapshot),
        ("third", third_snapshot),
    ):
        _validate_snapshot_state(snapshot, label=label)

    previous_revision = baseline.revision if baseline is not None else 0
    expected_revision = previous_revision + 1
    if (
        int(initial.revision) != expected_revision
        or initial_snapshot.revision != expected_revision
        or int(initial.snapshot_id or 0)
        != initial_snapshot.snapshot_id
    ):
        raise RatingLiveQAError(
            "Первый refresh не опубликовал ровно следующую revision: "
            f"baseline={previous_revision}, result={initial.revision}, "
            f"row={initial_snapshot.revision}."
        )
    if baseline is not None:
        if (
            initial_snapshot.scope_fingerprint
            != baseline.scope_fingerprint
            or initial_snapshot.source_fingerprint
            == baseline.source_fingerprint
            or initial_snapshot.payload_fingerprint
            == baseline.payload_fingerprint
        ):
            raise RatingLiveQAError(
                "Новый источник не изменил source/payload fingerprint "
                "или изменил scope fingerprint."
            )
        if (
            require_shift_score_change
            and initial_snapshot.shift_score_fingerprint
            == baseline.shift_score_fingerprint
        ):
            raise RatingLiveQAError(
                "Переход withheld → processed не изменил "
                "shift_score_fingerprint."
            )

    for result, snapshot, label in (
        (repeated, repeated_snapshot, "repeated"),
        (third, third_snapshot, "third"),
    ):
        if (
            int(result.revision) != expected_revision
            or int(result.snapshot_id or 0) != initial_snapshot.snapshot_id
            or snapshot != initial_snapshot
        ):
            raise RatingLiveQAError(
                f"{label}: повторный refresh изменил revision, "
                "fingerprints или JSON без новых источников."
            )
    return MaterializedRefreshEvidence(
        baseline=baseline,
        current=initial_snapshot,
        statuses=(
            str(initial.status),
            str(repeated.status),
            str(third.status),
        ),
        changed=(
            bool(initial.changed),
            bool(repeated.changed),
            bool(third.changed),
        ),
    )


def refresh_materialized_strict(
    *,
    rating_period: RatingPeriod,
    watch_composition: WatchComposition,
    shift_type: str,
    site_code: str,
    baseline_snapshot: MaterializedSnapshotState | None,
    require_shift_score_change: bool = False,
    refresh: Callable[..., Any] | None = None,
    snapshot_reader: Callable[..., MaterializedSnapshotState | None]
    | None = None,
) -> MaterializedRefreshEvidence:
    refresh = refresh or refresh_driver_rating_group
    snapshot_reader = (
        snapshot_reader or read_materialized_snapshot_state
    )
    refresh_kwargs = {
        "shift_type": shift_type,
        "scope_code": site_code,
    }
    initial = refresh(
        rating_period,
        watch_composition,
        **refresh_kwargs,
    )
    _validate_refresh_phase(
        initial,
        expected_status="published",
        expected_changed=True,
        label="initial",
    )
    initial_snapshot = snapshot_reader(
        rating_period,
        watch_composition,
        **refresh_kwargs,
    )
    if initial_snapshot is None:
        raise RatingLiveQAError(
            "После published refresh materialized row отсутствует."
        )

    repeated = refresh(
        rating_period,
        watch_composition,
        **refresh_kwargs,
    )
    _validate_refresh_phase(
        repeated,
        expected_status="verified",
        expected_changed=False,
        label="repeated",
    )
    repeated_snapshot = snapshot_reader(
        rating_period,
        watch_composition,
        **refresh_kwargs,
    )
    if repeated_snapshot is None:
        raise RatingLiveQAError(
            "После repeated refresh materialized row отсутствует."
        )

    third = refresh(
        rating_period,
        watch_composition,
        **refresh_kwargs,
    )
    _validate_refresh_phase(
        third,
        expected_status="verified",
        expected_changed=False,
        label="third",
    )
    third_snapshot = snapshot_reader(
        rating_period,
        watch_composition,
        **refresh_kwargs,
    )
    if third_snapshot is None:
        raise RatingLiveQAError(
            "После third refresh materialized row отсутствует."
        )
    return _refresh_result_invariant(
        initial,
        repeated,
        third,
        baseline=baseline_snapshot,
        initial_snapshot=initial_snapshot,
        repeated_snapshot=repeated_snapshot,
        third_snapshot=third_snapshot,
        require_shift_score_change=require_shift_score_change,
    )


def refresh_and_read_materialized(
    *,
    rating_period: RatingPeriod,
    watch_composition: WatchComposition,
    shift_type: str,
    site_code: str,
    baseline_snapshot: MaterializedSnapshotState | None,
    require_shift_score_change: bool = False,
    refresh: Callable[..., Any] | None = None,
    snapshot_reader: Callable[..., MaterializedSnapshotState | None]
    | None = None,
    discover: Callable[..., Any] | None = None,
    reader: Callable[..., dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, Any], Any, MaterializedRefreshEvidence]:
    discover = discover or discover_driver_rating_group_scope
    reader = reader or get_materialized_driver_rating_period
    evidence = refresh_materialized_strict(
        rating_period=rating_period,
        watch_composition=watch_composition,
        shift_type=shift_type,
        site_code=site_code,
        baseline_snapshot=baseline_snapshot,
        require_shift_score_change=require_shift_score_change,
        refresh=refresh,
        snapshot_reader=snapshot_reader,
    )
    group_scope = discover(
        rating_period,
        watch_composition,
        shift_type=shift_type,
    )
    payload = reader(
        rating_period,
        watch_composition,
        shift_type=shift_type,
        allowed_employee_ids=group_scope.allowed_employee_ids,
        expected_employee_ids=group_scope.expected_employee_ids,
        scope_code=site_code,
    )
    if (
        int(payload.get("snapshot_revision") or 0)
        != evidence.current.revision
        or str(payload.get("source_fingerprint") or "")
        != evidence.current.source_fingerprint
        or str(payload.get("shift_score_fingerprint") or "")
        != evidence.current.shift_score_fingerprint
    ):
        raise RatingLiveQAError(
            "Materialized reader вернул другую revision или fingerprints."
        )
    return evidence.current, payload, group_scope, evidence


def validate_materialized_payload(
    payload: dict[str, Any],
    *,
    shift_type: str,
    expected_employee_ids: tuple[int, ...],
) -> tuple[int, ...]:
    if (
        payload.get("official") is not False
        or payload.get("official_rating_eligible") is not False
        or payload.get("shift_type") != shift_type
    ):
        raise RatingLiveQAError(
            "Materialized reader вернул неверную группу или официальный результат."
        )
    try:
        distance_weight = Decimal(
            str((payload.get("distance_metrics") or {}).get("weight"))
        )
    except (InvalidOperation, TypeError) as error:
        raise RatingLiveQAError(
            "В materialized payload отсутствует нулевой вес м³·км/т·км."
        ) from error
    if distance_weight != 0:
        raise RatingLiveQAError(
            "м³·км/т·км получили ненулевой вес в live-QA."
        )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RatingLiveQAError(
            "Materialized payload не содержит массив entries."
        )
    observed = []
    place_by_score: dict[str, int] = {}
    for entry in entries:
        try:
            employee_id = int(entry["employee_id"])
            place = int(entry["place"])
            score = str(entry["score"])
        except (KeyError, TypeError, ValueError) as error:
            raise RatingLiveQAError(
                "Materialized entry не соответствует контракту."
            ) from error
        observed.append(employee_id)
        known_place = place_by_score.setdefault(score, place)
        if known_place != place:
            raise RatingLiveQAError(
                "Одинаковые баллы получили разные места."
            )
    if (
        len(observed) != len(set(observed))
        or not set(observed).issubset(set(expected_employee_ids))
    ):
        raise RatingLiveQAError(
            "Materialized entries вышли за фиксированную QA-группу."
        )
    return tuple(sorted(observed))


def materialized_place_map(
    payload: dict[str, Any],
) -> dict[int, int]:
    result = {}
    for entry in payload.get("entries") or []:
        try:
            employee_id = int(entry["employee_id"])
            result[employee_id] = int(entry["place"])
        except (KeyError, TypeError, ValueError) as error:
            raise RatingLiveQAError(
                "Не удалось прочитать места materialized entries."
            ) from error
    return result


def verify_place_movement(
    *,
    shift_type: str,
    previous: dict[int, int],
    current: dict[int, int],
) -> dict[str, Any]:
    common_employee_ids = sorted(set(previous) & set(current))
    changed_employee_ids = [
        employee_id
        for employee_id in common_employee_ids
        if previous[employee_id] != current[employee_id]
    ]
    if not changed_employee_ids:
        raise RatingLiveQAError(
            f"После 48h группа {shift_type} не показала реального "
            "изменения мест."
        )
    return {
        "shift_type": shift_type,
        "common_employee_count": len(common_employee_ids),
        "changed_employee_count": len(changed_employee_ids),
        "changed_employee_ids": changed_employee_ids,
    }


def materialized_tie_evidence(
    payloads: tuple[dict[str, Any], ...],
) -> dict[str, int]:
    tie_group_count = 0
    tied_employee_count = 0
    for payload in payloads:
        groups: dict[tuple[str, int], list[int]] = {}
        for entry in payload.get("entries") or []:
            try:
                key = (str(entry["score"]), int(entry["place"]))
                employee_id = int(entry["employee_id"])
            except (KeyError, TypeError, ValueError) as error:
                raise RatingLiveQAError(
                    "Не удалось проверить ничьи materialized entries."
                ) from error
            groups.setdefault(key, []).append(employee_id)
        tied_groups = [
            employee_ids
            for employee_ids in groups.values()
            if len(employee_ids) > 1
        ]
        tie_group_count += len(tied_groups)
        tied_employee_count += sum(map(len, tied_groups))
    if tie_group_count == 0:
        raise RatingLiveQAError(
            "После 48h не сформировалась ни одна реальная группа ничьей."
        )
    return {
        "tie_group_count": tie_group_count,
        "tied_employee_count": tied_employee_count,
    }


def verify_48h_rating_dynamics(
    *,
    baseline_places: dict[str, dict[int, int]],
    current_payloads: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    movements = {}
    for shift_type in (
        WorkShiftType.SHIFT_1,
        WorkShiftType.SHIFT_2,
    ):
        if (
            shift_type not in baseline_places
            or shift_type not in current_payloads
        ):
            raise RatingLiveQAError(
                "Для 48h проверки не хватает дневной или ночной группы."
            )
        movements[shift_type] = verify_place_movement(
            shift_type=shift_type,
            previous=baseline_places[shift_type],
            current=materialized_place_map(
                current_payloads[shift_type]
            ),
        )
    return {
        "passed": True,
        "movements": movements,
        "ties": materialized_tie_evidence(tuple(
            current_payloads[shift_type]
            for shift_type in (
                WorkShiftType.SHIFT_1,
                WorkShiftType.SHIFT_2,
            )
        )),
    }


def _closed_group_state(
    *,
    scope: Rating30dScope,
    shift_type: str,
    expected_closed_shift_count: int,
    allowed_pending_request_id: int | None = None,
) -> tuple[tuple[int, ...], int, int]:
    shifts = EmployeeShift.objects.filter(
        watch_period=scope.watch_period,
        workplace_code="driver",
        shift_type=shift_type,
        closed_at__isnull=False,
    )
    shift_ids = tuple(shifts.values_list("id", flat=True))
    closed_employee_ids = tuple(
        shifts.values_list("employee_id", flat=True)
    )
    passport_shift_ids = set(
        DriverShiftPassportSnapshot.objects
        .filter(shift_id__in=shift_ids)
        .values_list("shift_id", flat=True)
    )
    incomplete_requests = list(
        DriverShiftPassportCaptureRequest.objects
        .filter(
            shift_id__in=shift_ids,
            status__in=(
                DriverShiftPassportRequestStatus.PENDING,
                DriverShiftPassportRequestStatus.PROCESSING,
                DriverShiftPassportRequestStatus.FAILED,
            ),
        )
        .values("id", "shift_id", "status")
    )
    missing_snapshot_shift_ids = set(shift_ids) - passport_shift_ids
    if allowed_pending_request_id is None:
        allowed_pending = not incomplete_requests
        expected_passport_count = expected_closed_shift_count
        allowed_missing_shift_ids: set[int] = set()
    else:
        allowed_rows = [
            row
            for row in incomplete_requests
            if int(row["id"]) == allowed_pending_request_id
            and row["status"]
            == DriverShiftPassportRequestStatus.PENDING
        ]
        allowed_pending = (
            len(incomplete_requests) == 1
            and len(allowed_rows) == 1
        )
        allowed_missing_shift_ids = {
            int(allowed_rows[0]["shift_id"])
        } if allowed_rows else set()
        expected_passport_count = expected_closed_shift_count - 1
    if (
        len(shift_ids) != expected_closed_shift_count
        or len(passport_shift_ids) != expected_passport_count
        or not allowed_pending
        or missing_snapshot_shift_ids != allowed_missing_shift_ids
    ):
        raise RatingLiveQAError(
            "Закрытые смены и обработанные паспорта не совпали: "
            f"closed={len(shift_ids)}, passports={len(passport_shift_ids)}, "
            f"incomplete_requests={incomplete_requests!r}, "
            f"missing_snapshots={sorted(missing_snapshot_shift_ids)!r}, "
            f"expected={expected_closed_shift_count}."
        )
    return (
        tuple(sorted(set(closed_employee_ids))),
        len(shift_ids),
        len(passport_shift_ids),
    )


def build_live_state(
    *,
    run_id: str,
    site_code: str,
    rating_period_id: int,
    watch_composition_id: int,
    step: int,
    virtual_at: datetime,
    shift_type: str,
    expected_employee_ids: tuple[int, ...],
    observed_employee_ids: tuple[int, ...],
    closed_employee_ids: tuple[int, ...],
    summary: dict[str, Any],
) -> dict[str, Any]:
    placeholders = build_placeholders(
        expected_employee_ids=expected_employee_ids,
        observed_employee_ids=observed_employee_ids,
        closed_employee_ids=closed_employee_ids,
        withheld_reasons=summary.get("withheld_reasons") or {},
    )
    if len(observed_employee_ids) + len(placeholders) != EXPECTED_DRIVER_COUNT:
        raise RatingLiveQAError(
            "Entries и placeholders не образуют полную группу из 53 водителей."
        )
    return {
        "schema": LIVE_STATE_SCHEMA,
        "schema_version": LIVE_STATE_SCHEMA_VERSION,
        "synthetic": True,
        "official": False,
        "official_rating_eligible": False,
        "run_id": run_id,
        "site_code": site_code,
        "rating_period_id": int(rating_period_id),
        "watch_composition_id": int(watch_composition_id),
        "step": step,
        "virtual_at": virtual_at.isoformat(),
        "shift_type": shift_type,
        "placeholders": placeholders,
    }


def validate_delayed_passport_lifecycle_frame(
    *,
    lifecycle: str,
    target_employee_id: int,
    observed_employee_ids: tuple[int, ...],
    frame: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    placeholders = frame.get("placeholders")
    if not isinstance(placeholders, list):
        raise RatingLiveQAError(
            "Lifecycle frame не содержит placeholders."
        )
    withheld_reasons = {
        str(reason): int(count or 0)
        for reason, count in (
            summary.get("withheld_reasons") or {}
        ).items()
        if int(count or 0) > 0
    }
    withheld_shift_count = int(
        summary.get("withheld_shift_count", -1)
    )
    if lifecycle == "passport_pending":
        expected_placeholder = {
            "employee_id": target_employee_id,
            "status": "withheld",
            "reasons": ["passport_coverage_incomplete"],
        }
        if (
            len(observed_employee_ids)
            != EXPECTED_DRIVER_COUNT - 1
            or target_employee_id in observed_employee_ids
            or placeholders != [expected_placeholder]
            or withheld_shift_count != 1
            or withheld_reasons
            != {"passport_coverage_incomplete": 1}
        ):
            raise RatingLiveQAError(
                "PENDING-frame должен содержать ровно 52 entries и "
                "единственный passport_coverage_incomplete target."
            )
        return
    if lifecycle == "passport_processed":
        if (
            len(observed_employee_ids) != EXPECTED_DRIVER_COUNT
            or target_employee_id not in observed_employee_ids
            or placeholders
            or withheld_shift_count != 0
            or withheld_reasons
        ):
            raise RatingLiveQAError(
                "COMPLETED-frame должен содержать ровно 53 entries "
                "без placeholders и withheld-причин."
            )
        return
    raise RatingLiveQAError(
        f"Неизвестный passport lifecycle frame: {lifecycle!r}."
    )


def initial_live_state(
    *,
    run_id: str,
    site_code: str,
    rating_period_id: int,
    watch_composition_id: int,
    virtual_at: datetime,
    expected_employee_ids: tuple[int, ...],
) -> dict[str, Any]:
    return build_live_state(
        run_id=run_id,
        site_code=site_code,
        rating_period_id=rating_period_id,
        watch_composition_id=watch_composition_id,
        step=0,
        virtual_at=virtual_at,
        shift_type=WorkShiftType.SHIFT_1,
        expected_employee_ids=expected_employee_ids,
        observed_employee_ids=(),
        closed_employee_ids=(),
        summary={"withheld_reasons": {}},
    )


def _read_checkpoint_group(
    *,
    scope: Rating30dScope,
    shift_type: str,
    site_code: str,
) -> dict[str, Any]:
    expected_employee_ids = (
        scope.day_employee_ids
        if shift_type == WorkShiftType.SHIFT_1
        else scope.night_employee_ids
    )
    group_scope = discover_driver_rating_group_scope(
        scope.rating_period,
        scope.composition,
        shift_type=shift_type,
    )
    payload = get_materialized_driver_rating_period(
        scope.rating_period,
        scope.composition,
        shift_type=shift_type,
        allowed_employee_ids=group_scope.allowed_employee_ids,
        expected_employee_ids=group_scope.expected_employee_ids,
        scope_code=site_code,
    )
    observed_employee_ids = validate_materialized_payload(
        payload,
        shift_type=shift_type,
        expected_employee_ids=expected_employee_ids,
    )
    (
        closed_employee_ids,
        closed_shift_count,
        passport_shift_count,
    ) = _closed_group_state(
        scope=scope,
        shift_type=shift_type,
        expected_closed_shift_count=EXPECTED_DRIVER_COUNT,
    )
    placeholders = build_placeholders(
        expected_employee_ids=expected_employee_ids,
        observed_employee_ids=observed_employee_ids,
        closed_employee_ids=closed_employee_ids,
        withheld_reasons=(
            (payload.get("summary") or {}).get("withheld_reasons") or {}
        ),
    )
    displayed_row_count = len(observed_employee_ids) + len(placeholders)
    if displayed_row_count != EXPECTED_DRIVER_COUNT:
        raise RatingLiveQAError(
            f"24h checkpoint {shift_type} содержит "
            f"{displayed_row_count} строк вместо 53."
        )
    snapshot = DriverRatingPeriodMaterializedSnapshot.objects.get(
        scope_code=site_code,
        rating_period=scope.rating_period,
        watch_composition=scope.composition,
        shift_type=shift_type,
    )
    return {
        "closed_shift_count": closed_shift_count,
        "passport_shift_count": passport_shift_count,
        "materialized_revision": int(snapshot.revision),
        "materialized_row_count": len(observed_employee_ids),
        "placeholder_count": len(placeholders),
        "displayed_row_count": displayed_row_count,
    }


def verify_24h_checkpoint(
    *,
    run_id: str,
    site_code: str,
    scope: Rating30dScope,
) -> dict[str, Any]:
    groups = {
        shift_type: _read_checkpoint_group(
            scope=scope,
            shift_type=shift_type,
            site_code=site_code,
        )
        for shift_type in (
            WorkShiftType.SHIFT_1,
            WorkShiftType.SHIFT_2,
        )
    }
    open_shift_count = EmployeeShift.objects.filter(
        closed_at__isnull=True
    ).count()
    open_trip_count = Trip.objects.filter(
        status__in=OPEN_TRIP_STATUSES
    ).count()
    if open_shift_count or open_trip_count not in {0, 1}:
        raise RatingLiveQAError(
            "24h checkpoint не прошёл: "
            f"open_shifts={open_shift_count}, "
            f"open_trips={open_trip_count}."
        )
    return {
        "schema": LIVE_MANIFEST_SCHEMA,
        "schema_version": LIVE_MANIFEST_SCHEMA_VERSION,
        "synthetic": True,
        "official": False,
        "official_rating_eligible": False,
        "run_id": run_id,
        "checkpoint": "24h_day_and_night",
        "passed": True,
        "completed_shift_count": 2,
        "groups": groups,
        "open_shift_count": open_shift_count,
        "open_trip_count": open_trip_count,
        "carryover_contract": (
            "At most one open carryover trip is allowed at 24h."
        ),
    }


def _refresh_reconciled_group(
    *,
    scope: Rating30dScope,
    shift_type: str | None,
    site_code: str,
    baseline_snapshot: MaterializedSnapshotState | None,
) -> MaterializedRefreshEvidence | None:
    if shift_type is None:
        return None
    return refresh_materialized_strict(
        rating_period=scope.rating_period,
        watch_composition=scope.composition,
        shift_type=shift_type,
        site_code=site_code,
        baseline_snapshot=baseline_snapshot,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ускоренно выполнить 1–30 синтетических календарных дней "
            "(2–60 закрытых дневных/ночных смен) через materialized рейтинг. "
            "Каждый запуск начинается с пустой БД; N>=2 проходит 24h "
            "checkpoint и продолжается в том же процессе, без повторного "
            "запуска поверх суточной БД."
        )
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--simulation-days",
        type=int,
        default=1,
        help="1 календарный день = дневная + ночная закрытые смены.",
    )
    parser.add_argument(
        "--step-seconds",
        type=int,
        default=DEFAULT_STEP_SECONDS,
    )
    parser.add_argument(
        "--viewer-delay-seconds",
        type=int,
        default=DEFAULT_VIEWER_DELAY_SECONDS,
        help=(
            "Однократная пауза после публикации стартового кадра "
            "и до первой моделируемой смены (0–300 секунд)."
        ),
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=DEFAULT_START_DATE,
    )
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started_at = time.perf_counter()
    artifact_directory_ready = False
    artifact_dir: Path | None = None
    heartbeat = None
    configured_run_id = str(os.environ.get(LIVE_RUN_ID_ENV) or "")
    try:
        run_id = validate_live_run_id(
            args.run_id,
            configured_run_id=configured_run_id,
        )
        simulation_days = validate_simulation_days(
            args.simulation_days
        )
        step_seconds = validate_step_seconds(args.step_seconds)
        viewer_delay_seconds = validate_viewer_delay_seconds(
            args.viewer_delay_seconds
        )
        validate_configured_database(settings.DATABASES["default"])
        database_identity = verify_live_database(require_empty=True)
        marker = str(args.marker)
        artifact_dir = resolve_live_artifact_directory(
            marker=marker,
            run_id=run_id,
            artifact_dir=args.artifact_dir,
        )
        artifact_dir = ensure_new_artifact_directory(
            artifact_dir
        )
        artifact_directory_ready = True
        config = Rating30dConfig(
            run_id=run_id,
            marker=marker,
            start_date=args.start_date,
            artifact_dir=artifact_dir,
            day_count=simulation_days,
        )
        catalog = ReferenceCatalog(config)
        onboarding = Rating30dOnboarding(config, catalog).run()
        scope = create_live_scope(config, onboarding)
        runner = RatingLiveRunner(
            config,
            catalog,
            onboarding,
            scope,
        )
        site_code = str(
            getattr(settings, "PORTAL_SITE_CODE", "") or ""
        ).strip()
        if not site_code:
            raise RatingLiveQAError("PORTAL_SITE_CODE не задан.")

        state_path = artifact_dir / LIVE_STATE_FILENAME
        manifest_path = artifact_dir / LIVE_MANIFEST_FILENAME
        state = initial_live_state(
            run_id=run_id,
            site_code=site_code,
            rating_period_id=scope.rating_period.id,
            watch_composition_id=scope.composition.id,
            virtual_at=local_dt(config.start_date, 7, 5),
            expected_employee_ids=scope.day_employee_ids,
        )
        heartbeat = LiveStateHeartbeat(
            path=state_path,
            configured_run_id=configured_run_id,
        )
        manifest = {
            "schema": LIVE_MANIFEST_SCHEMA,
            "schema_version": LIVE_MANIFEST_SCHEMA_VERSION,
            "synthetic": True,
            "official": False,
            "official_rating_eligible": False,
            "run_id": run_id,
            "site_code": site_code,
            "simulation_days": simulation_days,
            "simulation_shift_count": config.total_shift_count,
            "step_seconds": step_seconds,
            **viewer_window_manifest_metadata(
                state=state,
                viewer_delay_seconds=viewer_delay_seconds,
            ),
            "execution": continuous_execution_contract(
                simulation_days
            ),
            "database": database_identity,
            "scope": {
                "rating_period_id": scope.rating_period.id,
                "watch_period_id": scope.watch_period.id,
                "watch_composition_id": scope.composition.id,
                "day_employee_count": len(scope.day_employee_ids),
                "night_employee_count": len(scope.night_employee_ids),
            },
            "limitations": {
                "open_shift_estimates": False,
                "downtime_kpi_coverage": False,
                "downtime_note": (
                    "Первые сутки проверяют цепочку, но не доказывают "
                    "полноту KPI простоев."
                ),
                "one_day_carryover_may_remain_open": (
                    simulation_days == 1
                ),
            },
            "steps": [],
            "complete": False,
        }
        print(
            "RATING_LIVE_QA_VIEWER_WINDOW "
            f"seconds={viewer_delay_seconds} step=0",
            flush=True,
        )
        open_live_viewer_window(
            heartbeat=heartbeat,
            state=state,
            viewer_delay_seconds=viewer_delay_seconds,
            write_manifest=lambda: atomic_write_live_manifest(
                manifest_path,
                manifest,
                configured_run_id=configured_run_id,
            ),
        )

        live_step = 0
        baseline_places: dict[str, dict[int, int]] = {}
        latest_payloads: dict[str, dict[str, Any]] = {}

        def publish_frame(
            *,
            result: Any,
            payload: dict[str, Any],
            evidence: MaterializedRefreshEvidence,
            reconciliation_evidence: (
                MaterializedRefreshEvidence | None
            ),
            shift_type: str,
            expected_employee_ids: tuple[int, ...],
            observed_employee_ids: tuple[int, ...],
            closed_employee_ids: tuple[int, ...],
            closed_shift_count: int,
            passport_shift_count: int,
            virtual_at: datetime,
            lifecycle: str,
            expected_withheld_employee_id: int | None = None,
            expected_processed_employee_id: int | None = None,
        ) -> dict[str, Any]:
            nonlocal live_step
            live_step += 1
            frame = build_live_state(
                run_id=run_id,
                site_code=site_code,
                rating_period_id=scope.rating_period.id,
                watch_composition_id=scope.composition.id,
                step=live_step,
                virtual_at=virtual_at,
                shift_type=shift_type,
                expected_employee_ids=expected_employee_ids,
                observed_employee_ids=observed_employee_ids,
                closed_employee_ids=closed_employee_ids,
                summary=payload.get("summary") or {},
            )
            if expected_withheld_employee_id is not None:
                validate_delayed_passport_lifecycle_frame(
                    lifecycle=lifecycle,
                    target_employee_id=(
                        expected_withheld_employee_id
                    ),
                    observed_employee_ids=observed_employee_ids,
                    frame=frame,
                    summary=payload.get("summary") or {},
                )
            if expected_processed_employee_id is not None:
                validate_delayed_passport_lifecycle_frame(
                    lifecycle=lifecycle,
                    target_employee_id=(
                        expected_processed_employee_id
                    ),
                    observed_employee_ids=observed_employee_ids,
                    frame=frame,
                    summary=payload.get("summary") or {},
                )

            heartbeat.publish(frame)
            step_manifest = {
                "step": live_step,
                "virtual_at": frame["virtual_at"],
                "shift_type": shift_type,
                "lifecycle": lifecycle,
                "revision": evidence.current.revision,
                "refresh": evidence.manifest_payload(),
                "counts": {
                    "cohort_employee_count": EXPECTED_DRIVER_COUNT,
                    "observed_employee_count": len(
                        observed_employee_ids
                    ),
                    "placeholder_count": len(
                        frame["placeholders"]
                    ),
                    "closed_shift_count": closed_shift_count,
                    "passport_shift_count": passport_shift_count,
                },
                "carryover_open": bool(runner.carryover_trip_id),
                "shift": asdict(result),
            }
            if reconciliation_evidence is not None:
                step_manifest["reconciliation_refresh"] = (
                    reconciliation_evidence.manifest_payload()
                )
            manifest["steps"].append(step_manifest)
            atomic_write_live_manifest(
                manifest_path,
                manifest,
                configured_run_id=configured_run_id,
            )
            return frame

        for day_index in range(simulation_days):
            runner.publish_daily_plans(day_index)
            for shift_in_day in range(2):
                shift_index = day_index * 2 + shift_in_day
                shift_type = runner.shift_type_for_index(shift_index)
                current_baseline = read_materialized_snapshot_state(
                    scope.rating_period,
                    scope.composition,
                    shift_type=shift_type,
                    scope_code=site_code,
                )
                expected_reconciled_shift_type = None
                reconciled_baseline = None
                if runner.carryover_origin_shift_id is not None:
                    expected_reconciled_shift_type = (
                        EmployeeShift.objects
                        .only("shift_type")
                        .get(pk=runner.carryover_origin_shift_id)
                        .shift_type
                    )
                    reconciled_baseline = (
                        read_materialized_snapshot_state(
                            scope.rating_period,
                            scope.composition,
                            shift_type=(
                                expected_reconciled_shift_type
                            ),
                            scope_code=site_code,
                        )
                    )

                heartbeat.begin_frame()
                result = runner.run_shift_step(shift_index)
                if (
                    runner.last_reconciled_shift_type
                    != expected_reconciled_shift_type
                ):
                    raise RatingLiveQAError(
                        "Тип пересчитанной carryover-смены изменился "
                        "между baseline и rebuild."
                    )
                reconciliation_evidence = _refresh_reconciled_group(
                    scope=scope,
                    shift_type=runner.last_reconciled_shift_type,
                    site_code=site_code,
                    baseline_snapshot=reconciled_baseline,
                )
                expected_employee_ids = (
                    scope.day_employee_ids
                    if shift_type == WorkShiftType.SHIFT_1
                    else scope.night_employee_ids
                )
                (
                    refresh_result,
                    payload,
                    group_scope,
                    refresh_evidence,
                ) = (
                    refresh_and_read_materialized(
                        rating_period=scope.rating_period,
                        watch_composition=scope.composition,
                        shift_type=shift_type,
                        site_code=site_code,
                        baseline_snapshot=current_baseline,
                    )
                )
                if (
                    tuple(sorted(group_scope.expected_employee_ids))
                    != expected_employee_ids
                ):
                    raise RatingLiveQAError(
                        "Materializer обнаружил другой состав QA-группы."
                    )
                observed_employee_ids = validate_materialized_payload(
                    payload,
                    shift_type=shift_type,
                    expected_employee_ids=expected_employee_ids,
                )
                expected_closed_shift_count = (
                    EXPECTED_DRIVER_COUNT * (day_index + 1)
                )
                allowed_pending_request_id = (
                    runner.delayed_passport_request_id
                    if shift_index == 0
                    else None
                )
                (
                    closed_employee_ids,
                    closed_shift_count,
                    passport_shift_count,
                ) = _closed_group_state(
                    scope=scope,
                    shift_type=shift_type,
                    expected_closed_shift_count=(
                        expected_closed_shift_count
                    ),
                    allowed_pending_request_id=(
                        allowed_pending_request_id
                    ),
                )
                _, close_time = runner.shift_bounds(shift_index)
                if shift_index != 0:
                    latest_payloads[shift_type] = payload
                    if shift_index == 1:
                        baseline_places[shift_type] = (
                            materialized_place_map(payload)
                        )
                    if shift_index == 3:
                        manifest["checkpoint_48h_dynamics"] = (
                            verify_48h_rating_dynamics(
                                baseline_places=baseline_places,
                                current_payloads=latest_payloads,
                            )
                        )
                state = publish_frame(
                    result=result,
                    payload=payload,
                    evidence=refresh_evidence,
                    reconciliation_evidence=(
                        reconciliation_evidence
                    ),
                    shift_type=shift_type,
                    expected_employee_ids=expected_employee_ids,
                    observed_employee_ids=observed_employee_ids,
                    closed_employee_ids=closed_employee_ids,
                    closed_shift_count=closed_shift_count,
                    passport_shift_count=passport_shift_count,
                    virtual_at=close_time + timedelta(minutes=1),
                    lifecycle=(
                        "passport_pending"
                        if shift_index == 0
                        else "shift_closed"
                    ),
                    expected_withheld_employee_id=(
                        runner.delayed_passport_employee_id
                        if shift_index == 0
                        else None
                    ),
                )

                if shift_index == 0:
                    heartbeat.raise_if_failed()
                    time.sleep(step_seconds)
                    heartbeat.begin_frame()
                    processed_baseline = refresh_evidence.current
                    runner.process_delayed_passport()
                    (
                        refresh_result,
                        payload,
                        group_scope,
                        refresh_evidence,
                    ) = refresh_and_read_materialized(
                        rating_period=scope.rating_period,
                        watch_composition=scope.composition,
                        shift_type=shift_type,
                        site_code=site_code,
                        baseline_snapshot=processed_baseline,
                        require_shift_score_change=True,
                    )
                    if (
                        tuple(sorted(
                            group_scope.expected_employee_ids
                        ))
                        != expected_employee_ids
                    ):
                        raise RatingLiveQAError(
                            "После COMPLETED изменился состав QA-группы."
                        )
                    observed_employee_ids = (
                        validate_materialized_payload(
                            payload,
                            shift_type=shift_type,
                            expected_employee_ids=(
                                expected_employee_ids
                            ),
                        )
                    )
                    (
                        closed_employee_ids,
                        closed_shift_count,
                        passport_shift_count,
                    ) = _closed_group_state(
                        scope=scope,
                        shift_type=shift_type,
                        expected_closed_shift_count=(
                            expected_closed_shift_count
                        ),
                    )
                    state = publish_frame(
                        result=result,
                        payload=payload,
                        evidence=refresh_evidence,
                        reconciliation_evidence=None,
                        shift_type=shift_type,
                        expected_employee_ids=(
                            expected_employee_ids
                        ),
                        observed_employee_ids=(
                            observed_employee_ids
                        ),
                        closed_employee_ids=closed_employee_ids,
                        closed_shift_count=closed_shift_count,
                        passport_shift_count=passport_shift_count,
                        virtual_at=(
                            close_time + timedelta(minutes=2)
                        ),
                        lifecycle="passport_processed",
                        expected_processed_employee_id=(
                            runner.delayed_passport_employee_id
                        ),
                    )
                    latest_payloads[shift_type] = payload
                    baseline_places[shift_type] = (
                        materialized_place_map(payload)
                    )
                if shift_index == 1:
                    checkpoint_24h = verify_24h_checkpoint(
                        run_id=run_id,
                        site_code=site_code,
                        scope=scope,
                    )
                    manifest["checkpoint_24h"] = checkpoint_24h
                    atomic_write_live_manifest(
                        artifact_dir / CHECKPOINT_24H_FILENAME,
                        checkpoint_24h,
                        configured_run_id=configured_run_id,
                    )
                    atomic_write_live_manifest(
                        manifest_path,
                        manifest,
                        configured_run_id=configured_run_id,
                    )
                heartbeat.raise_if_failed()
                print(
                    "RATING_LIVE_QA_STEP "
                    f"{live_step}/{config.total_shift_count + 1} "
                    f"{shift_type} revision={refresh_result.revision} "
                    f"rows={len(observed_employee_ids)} "
                    f"placeholders={len(state['placeholders'])}",
                    flush=True,
                )
                if shift_index + 1 < config.total_shift_count:
                    time.sleep(step_seconds)

        final_open_trips = Trip.objects.filter(
            status__in=OPEN_TRIP_STATUSES
        ).count()
        if simulation_days == 1:
            if final_open_trips not in {0, 1}:
                raise RatingLiveQAError(
                    "После суточного smoke допустим максимум один carryover."
                )
        elif final_open_trips:
            raise RatingLiveQAError(
                "После 48+ виртуальных часов остался открытый рейс."
            )
        if EmployeeShift.objects.filter(
            closed_at__isnull=True
        ).exists():
            raise RatingLiveQAError(
                "После live-QA остались открытые смены."
            )
        manifest["complete"] = True
        manifest["duration_seconds"] = round(
            time.perf_counter() - started_at,
            3,
        )
        manifest["final_open_trip_count"] = final_open_trips
        atomic_write_live_manifest(
            manifest_path,
            manifest,
            configured_run_id=configured_run_id,
        )
        print(
            "DRIVER_RATING_LIVE_QA_COMPLETE "
            f"days={simulation_days} shifts={config.total_shift_count} "
            f"open_trips={final_open_trips}",
            flush=True,
        )
        return 0
    except Exception as error:
        if heartbeat is not None:
            try:
                heartbeat.begin_frame()
            except Exception:
                pass
        failure = {
            "schema": LIVE_MANIFEST_SCHEMA,
            "schema_version": LIVE_MANIFEST_SCHEMA_VERSION,
            "synthetic": True,
            "official": False,
            "official_rating_eligible": False,
            "run_id": str(args.run_id or "").strip(),
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "complete": False,
            "duration_seconds": round(
                time.perf_counter() - started_at,
                3,
            ),
        }
        if (
            artifact_directory_ready
            and artifact_dir is not None
            and configured_run_id
            and failure["run_id"] == configured_run_id
        ):
            try:
                atomic_write_live_manifest(
                    artifact_dir / "failure.json",
                    failure,
                    configured_run_id=configured_run_id,
                )
            except RatingLiveQAContractError:
                pass
        print(
            "DRIVER_RATING_LIVE_QA_FAILED "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        if heartbeat is not None:
            heartbeat.stop()


if __name__ == "__main__":
    raise SystemExit(main())
