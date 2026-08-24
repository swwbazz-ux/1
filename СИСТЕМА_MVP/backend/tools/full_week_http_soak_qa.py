#!/usr/bin/env python
"""Локальный HTTP-soak для итоговой недельной QA-базы.

Скрипт не меняет производственные факты: он создаёт только тестовые
авторизованные сессии и параллельно читает realtime/рабочие экраны через
настоящий локальный Django-сервер.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.cookiejar
import json
import math
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


DEFAULT_MARKER = "ТЕСТ_НЕДЕЛЯ_20260727"
DEFAULT_PRODUCTION_DATE = date(2026, 7, 26)
DEFAULT_ARTIFACT_DIR = Path(
    r"C:\Users\swwba\AppData\Local\Temp"
    r"\copper-week-http-soak-20260727"
)
CSRF_RE = re.compile(
    r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
ALLOWED_PORT = 8000


@dataclass(frozen=True)
class Account:
    ordinal: int
    role: str
    host: str
    path: str
    realtime: bool = False

    @property
    def phone(self) -> str:
        return f"+7999200{self.ordinal:04d}"

    @property
    def pin(self) -> str:
        return f"{630000 + self.ordinal:06d}"


@dataclass
class SessionState:
    account: Account
    opener: urllib.request.OpenerDirector
    last_version: int = 0


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_accounts(
    *,
    driver_count: int,
    operator_count: int,
    marker: str,
    production_date: date,
) -> list[Account]:
    marker_query = urllib.parse.quote(marker)
    accounts = [
        Account(
            0,
            "admin",
            "admin.localhost",
            f"/system-admin/employees/?q={marker_query}",
        ),
        Account(1, "oup", "oup.localhost", "/oup/employees/"),
        Account(
            2,
            "deputy_mining_manager",
            "deputy.localhost",
            (
                "/deputy-mining-manager/?role=driver&date="
                f"{production_date.isoformat()}"
            ),
        ),
        Account(
            3,
            "manager",
            "management.localhost",
            f"/reports/management/?date={production_date.isoformat()}",
        ),
        Account(
            4,
            "dispatcher",
            "dispatcher.localhost",
            "/realtime/state/?after={after}&limit=1&include_events=0",
            True,
        ),
        Account(
            5,
            "mining_master",
            "mining-master.localhost",
            "/realtime/state/?after={after}&limit=1&include_events=0",
            True,
        ),
    ]
    accounts.extend(
        Account(
            ordinal,
            "excavator_operator",
            "excavator.localhost",
            "/realtime/state/?after={after}&limit=1&include_events=0",
            True,
        )
        for ordinal in range(12, 12 + operator_count)
    )
    accounts.extend(
        Account(
            ordinal,
            "driver",
            "driver.localhost",
            "/realtime/state/?after={after}&limit=1&include_events=0",
            True,
        )
        for ordinal in range(44, 44 + driver_count)
    )
    return accounts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=ALLOWED_PORT)
    parser.add_argument("--duration-seconds", type=int, default=300)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--page-interval-seconds", type=float, default=30.0)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--startup-jitter-seconds", type=float, default=1.0)
    parser.add_argument("--driver-count", type=int, default=53)
    parser.add_argument("--operator-count", type=int, default=8)
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    parser.add_argument(
        "--production-date",
        type=date.fromisoformat,
        default=DEFAULT_PRODUCTION_DATE,
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
    )
    return parser.parse_args()


def ensure_safe_args(args: argparse.Namespace) -> Path:
    if args.port != ALLOWED_PORT:
        raise RuntimeError(
            f"Разрешён только локальный порт {ALLOWED_PORT}, получен {args.port}."
        )
    if not 30 <= args.duration_seconds <= 900:
        raise RuntimeError("Длительность должна быть от 30 до 900 секунд.")
    if not 0.5 <= args.interval_seconds <= 10:
        raise RuntimeError("Интервал должен быть от 0,5 до 10 секунд.")
    if not 5 <= args.page_interval_seconds <= 120:
        raise RuntimeError(
            "Интервал рабочих страниц должен быть от 5 до 120 секунд."
        )
    if not 2 <= args.timeout_seconds <= 30:
        raise RuntimeError("Таймаут должен быть от 2 до 30 секунд.")
    if not 0 <= args.startup_jitter_seconds <= 5:
        raise RuntimeError(
            "Стартовый разброс должен быть от 0 до 5 секунд."
        )
    if not 1 <= args.driver_count <= 53:
        raise RuntimeError("Число Водителей должно быть от 1 до 53.")
    if not 1 <= args.operator_count <= 8:
        raise RuntimeError("Число Машинистов должно быть от 1 до 8.")
    artifact_dir = args.artifact_dir.resolve()
    if artifact_dir.exists() and any(artifact_dir.iterdir()):
        raise RuntimeError(
            f"Каталог артефактов уже не пуст: {artifact_dir}"
        )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def base_url(account: Account, port: int) -> str:
    if not account.host.endswith(".localhost"):
        raise RuntimeError(f"Запрещён внешний host: {account.host}")
    return f"http://127.0.0.1:{port}"


def host_header(account: Account, port: int) -> str:
    return f"{account.host}:{port}"


def open_request(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    *,
    timeout: float,
) -> tuple[int, bytes, str]:
    with opener.open(request, timeout=timeout) as response:
        return (
            int(response.status),
            response.read(),
            response.geturl(),
        )


def login(
    account: Account,
    *,
    port: int,
    timeout: float,
) -> SessionState:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        NoRedirectHandler(),
    )
    root = f"{base_url(account, port)}/"
    logical_root = f"http://{host_header(account, port)}/"
    get_request = urllib.request.Request(
        root,
        headers={
            "Host": host_header(account, port),
            "User-Agent": "Copper-Week-QA-HTTP-Soak/1.0",
        },
    )
    status, body, _ = open_request(
        opener,
        get_request,
        timeout=timeout,
    )
    if status != 200:
        raise RuntimeError(f"GET login вернул {status}.")
    html = body.decode("utf-8", errors="replace")
    token_match = CSRF_RE.search(html)
    if not token_match:
        raise RuntimeError("CSRF-токен не найден на экране входа.")

    payload = urllib.parse.urlencode(
        {
            "csrfmiddlewaretoken": token_match.group(1),
            "phone": account.phone,
            "access_code": account.pin,
            "device_kind": (
                "shared"
                if account.role in {"dispatcher", "mining_master"}
                else "personal"
            ),
        }
    ).encode("ascii")
    post_request = urllib.request.Request(
        root,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": host_header(account, port),
            "Referer": logical_root,
            "User-Agent": "Copper-Week-QA-HTTP-Soak/1.0",
        },
    )
    try:
        status, body, final_url = open_request(
            opener,
            post_request,
            timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        if exc.code not in {301, 302, 303, 307, 308}:
            raise
        status = int(exc.code)
        body = exc.read()
        final_url = exc.headers.get("Location", "")
    if status not in {200, 301, 302, 303, 307, 308}:
        raise RuntimeError(f"POST login вернул {status}.")
    if status == 200 and urllib.parse.urlparse(final_url).path == "/":
        text = body.decode("utf-8", errors="replace")
        if "Невер" in text or "ошиб" in text.lower():
            raise RuntimeError("Авторизация отклонена.")
    return SessionState(account=account, opener=opener)


def request_once(
    state: SessionState,
    *,
    port: int,
    timeout: float,
    sequence: int,
) -> dict[str, Any]:
    account = state.account
    path = (
        account.path.format(after=state.last_version)
        if account.realtime
        else account.path
    )
    url = f"{base_url(account, port)}{path}"
    started = time.perf_counter()
    status = 0
    body = b""
    error = ""
    version: int | None = None
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": (
                    "application/json"
                    if account.realtime
                    else "text/html,application/xhtml+xml"
                ),
                "Host": host_header(account, port),
                "User-Agent": "Copper-Week-QA-HTTP-Soak/1.0",
                "X-Requested-With": (
                    "XMLHttpRequest" if account.realtime else ""
                ),
            },
        )
        status, body, _ = open_request(
            state.opener,
            request,
            timeout=timeout,
        )
        if account.realtime and status == 200:
            payload = json.loads(body.decode("utf-8"))
            candidate = (
                payload.get("version")
                or payload.get("state_version")
                or payload.get("current_version")
                or 0
            )
            version = int(candidate)
            if version < state.last_version:
                error = (
                    "realtime_version_regressed:"
                    f"{state.last_version}->{version}"
                )
            state.last_version = max(state.last_version, version)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = exc.read()
        error = f"HTTPError:{exc.code}"
    except Exception as exc:  # noqa: BLE001 - ошибка нужна в QA-артефакте.
        error = f"{type(exc).__name__}:{exc}"
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "ordinal": account.ordinal,
        "role": account.role,
        "host": account.host,
        "sequence": sequence,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "bytes": len(body),
        "version": version,
        "error": error,
        "finished_at": datetime.now(UTC).isoformat(),
    }


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * percent))
    return round(ordered[rank - 1], 3)


def run_worker(
    state: SessionState,
    *,
    port: int,
    timeout: float,
    interval: float,
    deadline: float,
    start_barrier: threading.Barrier,
    initial_delay: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start_barrier.wait(timeout=30)
    if initial_delay > 0:
        time.sleep(initial_delay)
    sequence = 0
    next_at = time.monotonic()
    while time.monotonic() < deadline:
        rows.append(
            request_once(
                state,
                port=port,
                timeout=timeout,
                sequence=sequence,
            )
        )
        sequence += 1
        next_at += interval
        pause = next_at - time.monotonic()
        if pause > 0:
            time.sleep(pause)
    return rows


def main() -> int:
    args = parse_args()
    artifact_dir = ensure_safe_args(args)
    accounts = build_accounts(
        driver_count=args.driver_count,
        operator_count=args.operator_count,
        marker=args.marker,
        production_date=args.production_date,
    )
    started_at = datetime.now(UTC)
    login_results: list[dict[str, Any]] = []
    states: list[SessionState] = []

    def login_one(account: Account) -> tuple[Account, SessionState | None, str]:
        try:
            return (
                account,
                login(
                    account,
                    port=args.port,
                    timeout=args.timeout_seconds,
                ),
                "",
            )
        except Exception as exc:  # noqa: BLE001 - ошибка нужна в QA-артефакте.
            return account, None, f"{type(exc).__name__}:{exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        futures = [executor.submit(login_one, account) for account in accounts]
        for future in concurrent.futures.as_completed(futures):
            account, state, error = future.result()
            login_results.append(
                {
                    "ordinal": account.ordinal,
                    "role": account.role,
                    "host": account.host,
                    "passed": state is not None,
                    "error": error,
                }
            )
            if state is not None:
                states.append(state)

    login_results.sort(key=lambda item: item["ordinal"])
    if len(states) != len(accounts):
        report = {
            "status": "FAILED_LOGIN",
            "started_at": started_at.isoformat(),
            "accounts": len(accounts),
            "successful_logins": len(states),
            "login_results": login_results,
        }
        (artifact_dir / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 2

    admin_state = next(
        state for state in states if state.account.role == "admin"
    )
    marker_request = urllib.request.Request(
        (
            f"{base_url(admin_state.account, args.port)}"
            "/system-admin/employees/?q="
            f"{urllib.parse.quote(args.marker)}"
        ),
        headers={
            "Host": host_header(admin_state.account, args.port),
            "User-Agent": "Copper-Week-QA-HTTP-Soak/1.0",
        },
    )
    marker_status, marker_body, _ = open_request(
        admin_state.opener,
        marker_request,
        timeout=args.timeout_seconds,
    )
    marker_found = args.marker.encode("utf-8") in marker_body
    if marker_status != 200 or not marker_found:
        report = {
            "status": "FAILED_DATABASE_PREFLIGHT",
            "started_at": started_at.isoformat(),
            "marker_status": marker_status,
            "marker_found": marker_found,
            "login_results": login_results,
        }
        (artifact_dir / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 3

    states.sort(key=lambda item: item.account.ordinal)
    barrier = threading.Barrier(len(states))
    deadline = time.monotonic() + args.duration_seconds
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(states)
    ) as executor:
        futures = [
            executor.submit(
                run_worker,
                state,
                port=args.port,
                timeout=args.timeout_seconds,
                interval=(
                    args.interval_seconds
                    if state.account.realtime
                    else args.page_interval_seconds
                ),
                deadline=deadline,
                start_barrier=barrier,
                initial_delay=initial_delay,
            )
            for state_index, state in enumerate(states)
            for initial_delay in [
                (
                    state_index
                    * args.startup_jitter_seconds
                    / max(len(states), 1)
                )
            ]
        ]
        for future in concurrent.futures.as_completed(futures):
            rows.extend(future.result())

    rows.sort(
        key=lambda item: (
            item["finished_at"],
            item["ordinal"],
            item["sequence"],
        )
    )
    request_log = artifact_dir / "http_requests.jsonl"
    with request_log.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    elapsed_values = [float(row["elapsed_ms"]) for row in rows]
    realtime_values = [
        float(row["elapsed_ms"])
        for row in rows
        if next(
            item for item in accounts if item.ordinal == row["ordinal"]
        ).realtime
    ]
    page_values = [
        float(row["elapsed_ms"])
        for row in rows
        if not next(
            item for item in accounts if item.ordinal == row["ordinal"]
        ).realtime
    ]
    status_counts: dict[str, int] = {}
    role_counts: dict[str, int] = {}
    for row in rows:
        status_key = str(row["status"])
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        role_counts[row["role"]] = role_counts.get(row["role"], 0) + 1
    errors = [row for row in rows if row["error"]]
    unexpected_statuses = [
        row for row in rows if int(row["status"]) != 200
    ]
    p95 = percentile(elapsed_values, 0.95)
    p99 = percentile(elapsed_values, 0.99)
    realtime_p95 = percentile(realtime_values, 0.95)
    realtime_p99 = percentile(realtime_values, 0.99)
    page_p95 = percentile(page_values, 0.95)
    maximum = round(max(elapsed_values, default=0.0), 3)
    criteria = {
        "all_logins": len(states) == len(accounts),
        "database_marker_found": marker_found,
        "no_request_errors": not errors,
        "all_status_200": not unexpected_statuses,
        "realtime_p95_le_1000_ms": realtime_p95 <= 1000,
        "realtime_p99_le_3000_ms": realtime_p99 <= 3000,
        "pages_p95_le_5000_ms": page_p95 <= 5000,
        "max_lt_timeout": maximum < args.timeout_seconds * 1000,
    }
    finished_at = datetime.now(UTC)
    duration_seconds = (finished_at - started_at).total_seconds()
    summary = {
        "status": "PASS" if all(criteria.values()) else "FAIL",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(duration_seconds, 3),
        "configuration": {
            "sessions": len(states),
            "driver_count": args.driver_count,
            "operator_count": args.operator_count,
            "duration_seconds": args.duration_seconds,
            "interval_seconds": args.interval_seconds,
            "page_interval_seconds": args.page_interval_seconds,
            "timeout_seconds": args.timeout_seconds,
            "startup_jitter_seconds": args.startup_jitter_seconds,
            "target": "http://*.localhost:8000",
            "marker": args.marker,
            "production_date": args.production_date.isoformat(),
            "production_facts_read_only": True,
            "creates_test_sessions": True,
        },
        "accounts_by_role": {
            role: sum(1 for item in accounts if item.role == role)
            for role in sorted({item.role for item in accounts})
        },
        "login_results": login_results,
        "requests": {
            "total": len(rows),
            "status_counts": status_counts,
            "role_counts": role_counts,
            "errors": len(errors),
            "unexpected_statuses": len(unexpected_statuses),
            "requests_per_second": round(
                len(rows) / max(args.duration_seconds, 1),
                3,
            ),
        },
        "latency_ms": {
            "min": round(min(elapsed_values, default=0.0), 3),
            "mean": round(
                statistics.fmean(elapsed_values)
                if elapsed_values
                else 0.0,
                3,
            ),
            "median": round(
                statistics.median(elapsed_values)
                if elapsed_values
                else 0.0,
                3,
            ),
            "p95": p95,
            "p99": p99,
            "max": maximum,
            "realtime_p95": realtime_p95,
            "realtime_p99": realtime_p99,
            "pages_p95": page_p95,
        },
        "criteria": criteria,
        "error_samples": errors[:20],
        "unexpected_status_samples": unexpected_statuses[:20],
    }
    (artifact_dir / "config.json").write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        key: value
                        for key, value in asdict(account).items()
                        if key not in {"phone", "pin"}
                    }
                    for account in accounts
                ],
                "duration_seconds": args.duration_seconds,
                "interval_seconds": args.interval_seconds,
                "page_interval_seconds": args.page_interval_seconds,
                "timeout_seconds": args.timeout_seconds,
                "startup_jitter_seconds": args.startup_jitter_seconds,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (artifact_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
