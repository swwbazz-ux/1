#!/usr/bin/env python
"""Read-only traffic measurement for the ready-core role PWA applications.

The script targets only a local Django server on port 8000 and uses the
isolated weekly QA accounts created by ``full_week_load_qa.py``. It never
prints or stores access codes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


ALLOWED_PORT = 8000
DEFAULT_ARTIFACT_DIR = Path(
    r"C:\Users\swwba\AppData\Local\Temp"
    r"\copper-pwa-traffic-audit-20260727"
)
CSRF_RE = re.compile(
    r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoleTarget:
    ordinal: int
    role: str
    host: str
    start_url: str
    manifest_url: str
    service_worker_url: str
    realtime: bool
    simultaneous_sessions: int
    poll_interval_seconds: int | None

    @property
    def phone(self) -> str:
        return f"+7999200{self.ordinal:04d}"

    @property
    def pin(self) -> str:
        return f"{630000 + self.ordinal:06d}"


READY_ROLES = (
    RoleTarget(
        0,
        "admin",
        "admin.localhost",
        "/system-admin/",
        "/system-admin.webmanifest",
        "/system-admin-sw.js",
        True,
        1,
        15,
    ),
    RoleTarget(
        1,
        "oup",
        "oup.localhost",
        "/oup/employees/",
        "/oup.webmanifest",
        "/oup-sw.js",
        False,
        1,
        None,
    ),
    RoleTarget(
        2,
        "deputy_mining_manager",
        "deputy.localhost",
        "/deputy-mining-manager/",
        "/deputy-mining-manager.webmanifest",
        "/deputy-mining-manager-sw.js",
        False,
        1,
        None,
    ),
    RoleTarget(
        4,
        "dispatcher",
        "dispatcher.localhost",
        "/dispatcher/control/",
        "/dispatcher.webmanifest",
        "/dispatcher-sw.js",
        True,
        1,
        5,
    ),
    RoleTarget(
        5,
        "mining_master",
        "mining-master.localhost",
        "/mining-master/assignments/",
        "/mining-master-manifest.webmanifest",
        "/mining-master-sw.js",
        True,
        1,
        5,
    ),
    RoleTarget(
        12,
        "excavator_operator",
        "excavator.localhost",
        "/excavator/work/",
        "/excavator.webmanifest",
        "/excavator-sw.js",
        True,
        8,
        5,
    ),
    RoleTarget(
        44,
        "driver",
        "driver.localhost",
        "/driver/",
        "/driver.webmanifest",
        "/driver-sw.js",
        True,
        53,
        5,
    ),
    RoleTarget(
        3,
        "manager",
        "management.localhost",
        "/reports/management/?date=2026-07-26",
        "/management.webmanifest",
        "/management-sw.js",
        True,
        1,
        15,
    ),
)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        candidate = ""
        if tag in {"script", "img", "source"}:
            candidate = values.get("src") or ""
        elif tag == "link":
            candidate = values.get("href") or ""
        if candidate:
            self.urls.append(candidate)


@dataclass
class MeasuredResponse:
    category: str
    role: str
    path: str
    status: int
    elapsed_ms: float
    body_bytes: int
    gzip_bytes: int
    response_header_bytes: int
    content_type: str
    content_encoding: str
    cache_control: str
    etag: str
    last_modified: str
    sha256: str
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=ALLOWED_PORT)
    parser.add_argument("--timeout-seconds", type=float, default=12.0)
    parser.add_argument("--realtime-polls", type=int, default=12)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
    )
    return parser.parse_args()


def ensure_safe_args(args: argparse.Namespace) -> Path:
    if args.port != ALLOWED_PORT:
        raise RuntimeError(
            f"Only local port {ALLOWED_PORT} is allowed, got {args.port}."
        )
    if not 2 <= args.timeout_seconds <= 30:
        raise RuntimeError("Timeout must be between 2 and 30 seconds.")
    if not 3 <= args.realtime_polls <= 60:
        raise RuntimeError("Realtime poll count must be between 3 and 60.")
    artifact_dir = args.artifact_dir.resolve()
    if artifact_dir.exists() and any(artifact_dir.iterdir()):
        raise RuntimeError(f"Artifact directory is not empty: {artifact_dir}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def host_header(role: RoleTarget, port: int) -> str:
    return f"{role.host}:{port}"


def response_header_size(status: int, headers: Any) -> int:
    total = len(f"HTTP/1.1 {status}\r\n".encode("ascii"))
    for key, value in headers.items():
        total += len(f"{key}: {value}\r\n".encode("utf-8"))
    return total + 2


def gzip_size(body: bytes) -> int:
    if not body:
        return 0
    return len(gzip.compress(body, compresslevel=6, mtime=0))


def measure_request(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    *,
    role: RoleTarget,
    category: str,
    path: str,
    timeout: float,
) -> tuple[MeasuredResponse, bytes, Any]:
    started = time.perf_counter()
    status = 0
    body = b""
    headers: Any = {}
    error = ""
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            headers = response.headers
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        headers = exc.headers
        body = exc.read()
        if status not in {301, 302, 303, 304, 307, 308}:
            error = f"HTTPError:{status}"
    except Exception as exc:  # noqa: BLE001 - audit must record all failures.
        error = f"{type(exc).__name__}:{exc}"
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    result = MeasuredResponse(
        category=category,
        role=role.role,
        path=path,
        status=status,
        elapsed_ms=elapsed_ms,
        body_bytes=len(body),
        gzip_bytes=gzip_size(body),
        response_header_bytes=response_header_size(status, headers),
        content_type=str(headers.get("Content-Type", "")),
        content_encoding=str(headers.get("Content-Encoding", "")),
        cache_control=str(headers.get("Cache-Control", "")),
        etag=str(headers.get("ETag", "")),
        last_modified=str(headers.get("Last-Modified", "")),
        sha256=hashlib.sha256(body).hexdigest() if body else "",
        error=error,
    )
    return result, body, headers


def new_session() -> tuple[
    urllib.request.OpenerDirector,
    http.cookiejar.CookieJar,
]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        NoRedirectHandler(),
    )
    return opener, cookie_jar


def login(
    role: RoleTarget,
    *,
    port: int,
    timeout: float,
) -> tuple[
    urllib.request.OpenerDirector,
    http.cookiejar.CookieJar,
    list[MeasuredResponse],
]:
    opener, cookie_jar = new_session()
    root = f"{base_url(port)}/"
    common = {
        "Host": host_header(role, port),
        "User-Agent": "Copper-PWA-Traffic-Audit/1.0",
    }
    get_result, body, _ = measure_request(
        opener,
        urllib.request.Request(root, headers=common),
        role=role,
        category="login_get",
        path="/",
        timeout=timeout,
    )
    token = CSRF_RE.search(body.decode("utf-8", errors="replace"))
    if get_result.status != 200 or not token:
        raise RuntimeError(
            f"{role.role}: login form unavailable ({get_result.status})."
        )
    form = urllib.parse.urlencode(
        {
            "csrfmiddlewaretoken": token.group(1),
            "phone": role.phone,
            "access_code": role.pin,
            "device_kind": (
                "shared"
                if role.role in {"dispatcher", "mining_master"}
                else "personal"
            ),
        }
    ).encode("ascii")
    post_result, _, _ = measure_request(
        opener,
        urllib.request.Request(
            root,
            data=form,
            headers={
                **common,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"http://{host_header(role, port)}/",
            },
        ),
        role=role,
        category="login_post",
        path="/",
        timeout=timeout,
    )
    if post_result.status not in {301, 302, 303, 307, 308}:
        raise RuntimeError(
            f"{role.role}: login rejected ({post_result.status})."
        )
    return opener, cookie_jar, [get_result, post_result]


def normalize_asset_urls(html: bytes, role: RoleTarget) -> list[str]:
    parser = AssetParser()
    parser.feed(html.decode("utf-8", errors="replace"))
    raw_urls = [
        *parser.urls,
        role.manifest_url,
        role.service_worker_url,
    ]
    result: list[str] = []
    for raw in raw_urls:
        if not raw or raw.startswith(("data:", "blob:", "javascript:")):
            continue
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme or parsed.netloc:
            continue
        path = urllib.parse.urlunparse(("", "", parsed.path, "", parsed.query, ""))
        if not path.startswith("/"):
            path = "/" + path.lstrip("/")
        if path not in result:
            result.append(path)
    return result


def build_get(
    role: RoleTarget,
    *,
    port: int,
    path: str,
    accept: str,
    conditional_headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    return urllib.request.Request(
        f"{base_url(port)}{path}",
        headers={
            "Host": host_header(role, port),
            "Accept": accept,
            "User-Agent": "Copper-PWA-Traffic-Audit/1.0",
            **(conditional_headers or {}),
        },
    )


def audit_role(
    role: RoleTarget,
    *,
    port: int,
    timeout: float,
    realtime_polls: int,
) -> dict[str, Any]:
    opener, cookie_jar, rows = login(role, port=port, timeout=timeout)
    cold_page, cold_html, _ = measure_request(
        opener,
        build_get(
            role,
            port=port,
            path=role.start_url,
            accept="text/html,application/xhtml+xml",
        ),
        role=role,
        category="cold_page",
        path=role.start_url,
        timeout=timeout,
    )
    rows.append(cold_page)
    if cold_page.status != 200:
        raise RuntimeError(
            f"{role.role}: start page returned {cold_page.status}."
        )

    cold_assets: dict[str, tuple[MeasuredResponse, Any]] = {}
    for path in normalize_asset_urls(cold_html, role):
        result, _, headers = measure_request(
            opener,
            build_get(role, port=port, path=path, accept="*/*"),
            role=role,
            category="cold_asset",
            path=path,
            timeout=timeout,
        )
        rows.append(result)
        cold_assets[path] = (result, headers)

    # Cross a second boundary to catch any regression back to unstable
    # second-resolution URLs while preserving the stable release contract.
    time.sleep(1.05)
    warm_page, warm_html, _ = measure_request(
        opener,
        build_get(
            role,
            port=port,
            path=role.start_url,
            accept="text/html,application/xhtml+xml",
        ),
        role=role,
        category="warm_page",
        path=role.start_url,
        timeout=timeout,
    )
    rows.append(warm_page)
    warm_urls = normalize_asset_urls(warm_html, role)
    for path in warm_urls:
        result, _, _ = measure_request(
            opener,
            build_get(role, port=port, path=path, accept="*/*"),
            role=role,
            category="warm_asset_unconditional",
            path=path,
            timeout=timeout,
        )
        rows.append(result)

    for path, (_, headers) in cold_assets.items():
        conditional: dict[str, str] = {}
        etag = str(headers.get("ETag", ""))
        last_modified = str(headers.get("Last-Modified", ""))
        if etag:
            conditional["If-None-Match"] = etag
        if last_modified:
            conditional["If-Modified-Since"] = last_modified
        if not conditional:
            continue
        result, _, _ = measure_request(
            opener,
            build_get(
                role,
                port=port,
                path=path,
                accept="*/*",
                conditional_headers=conditional,
            ),
            role=role,
            category="warm_asset_conditional",
            path=path,
            timeout=timeout,
        )
        rows.append(result)

    realtime_rows: list[MeasuredResponse] = []
    if role.realtime:
        version = 0
        for _ in range(realtime_polls):
            path = (
                f"/realtime/state/?after={version}&include_events=0"
                if version
                else "/realtime/state/?include_events=0"
            )
            result, body, _ = measure_request(
                opener,
                build_get(
                    role,
                    port=port,
                    path=path,
                    accept="application/json",
                ),
                role=role,
                category="realtime_idle",
                path=path,
                timeout=timeout,
            )
            rows.append(result)
            realtime_rows.append(result)
            if result.status == 200:
                payload = json.loads(body.decode("utf-8"))
                version = max(version, int(payload.get("version") or 0))

    cold_urls = set(cold_assets)
    warm_url_set = set(warm_urls)
    changed_urls = sorted(
        path
        for path in cold_urls.symmetric_difference(warm_url_set)
        if path.startswith("/static/")
    )
    return {
        "role": role.role,
        "host": role.host,
        "simultaneous_sessions": role.simultaneous_sessions,
        "realtime": role.realtime,
        "poll_interval_seconds": role.poll_interval_seconds,
        "cookie_count": len(list(cookie_jar)),
        "cold_asset_count": len(cold_urls),
        "warm_asset_count": len(warm_url_set),
        "changed_static_urls_between_navigations": changed_urls,
        "rows": [asdict(row) for row in rows],
        "realtime_body_mean": (
            round(
                sum(row.body_bytes for row in realtime_rows)
                / len(realtime_rows),
                2,
            )
            if realtime_rows
            else 0
        ),
        "realtime_gzip_mean": (
            round(
                sum(row.gzip_bytes for row in realtime_rows)
                / len(realtime_rows),
                2,
            )
            if realtime_rows
            else 0
        ),
    }


def sum_category(role_result: dict[str, Any], category: str, field: str) -> int:
    return int(
        sum(
            int(row[field])
            for row in role_result["rows"]
            if row["category"] == category and row["status"] in {200, 304}
        )
    )


def build_summary(role_results: list[dict[str, Any]]) -> dict[str, Any]:
    role_summary = []
    shift_seconds = 12 * 60 * 60
    body_projection = 0.0
    gzip_projection = 0.0
    realtime_requests = 0
    for result in role_results:
        sessions = int(result["simultaneous_sessions"])
        projected_requests = 0
        poll_interval_seconds = result.get("poll_interval_seconds")
        if result["realtime"]:
            if not poll_interval_seconds or int(poll_interval_seconds) <= 0:
                raise ValueError(
                    f"{result['role']}: realtime poll interval is not defined."
                )
            projected_requests = round(
                sessions * shift_seconds / int(poll_interval_seconds)
            )
            body_projection += (
                float(result["realtime_body_mean"])
                * projected_requests
            )
            gzip_projection += (
                float(result["realtime_gzip_mean"])
                * projected_requests
            )
            realtime_requests += projected_requests
        role_summary.append(
            {
                "role": result["role"],
                "simultaneous_sessions": sessions,
                "realtime": result["realtime"],
                "poll_interval_seconds": poll_interval_seconds,
                "projected_realtime_requests_per_12h_shift": projected_requests,
                "cold_page_body_bytes": sum_category(
                    result, "cold_page", "body_bytes"
                ),
                "cold_assets_body_bytes": sum_category(
                    result, "cold_asset", "body_bytes"
                ),
                "warm_page_body_bytes": sum_category(
                    result, "warm_page", "body_bytes"
                ),
                "warm_assets_unconditional_body_bytes": sum_category(
                    result,
                    "warm_asset_unconditional",
                    "body_bytes",
                ),
                "warm_assets_conditional_body_bytes": sum_category(
                    result,
                    "warm_asset_conditional",
                    "body_bytes",
                ),
                "realtime_body_mean": result["realtime_body_mean"],
                "realtime_gzip_mean": result["realtime_gzip_mean"],
                "changed_static_urls_between_navigations": result[
                    "changed_static_urls_between_navigations"
                ],
            }
        )
    return {
        "roles": role_summary,
        "ready_role_count": len(role_results),
        "simultaneous_sessions": sum(
            int(item["simultaneous_sessions"]) for item in role_results
        ),
        "simultaneous_realtime_sessions": sum(
            int(item["simultaneous_sessions"])
            for item in role_results
            if item["realtime"]
        ),
        "realtime_requests_per_12h_shift": realtime_requests,
        "realtime_body_bytes_per_12h_shift": round(body_projection),
        "realtime_gzip_estimate_bytes_per_12h_shift": round(gzip_projection),
        "realtime_projection_assumes_visible_active_windows": True,
    }


def main() -> int:
    args = parse_args()
    artifact_dir = ensure_safe_args(args)
    started_at = datetime.now(UTC)
    role_results = []
    errors = []
    for role in READY_ROLES:
        try:
            role_results.append(
                audit_role(
                    role,
                    port=args.port,
                    timeout=args.timeout_seconds,
                    realtime_polls=args.realtime_polls,
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep complete audit evidence.
            errors.append({"role": role.role, "error": f"{type(exc).__name__}:{exc}"})

    summary = build_summary(role_results)
    summary.update(
        {
            "status": "PASS" if not errors and len(role_results) == len(READY_ROLES) else "FAIL",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "target": "http://*.localhost:8000",
            "production_mutated": False,
            "ready_roles_expected": len(READY_ROLES),
            "errors": errors,
        }
    )
    (artifact_dir / "traffic_detail.json").write_text(
        json.dumps(role_results, ensure_ascii=False, indent=2),
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
