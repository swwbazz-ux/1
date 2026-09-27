#!/usr/bin/env python3
"""Two real HTTPS/nginx logins using disposable synthetic credentials only."""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import ssl
import stat
import sys
import urllib.parse
import urllib.request
from pathlib import Path


HOST = "sse-qa.driverform.ru"
BASE_URL = f"https://{HOST}"
NETWORK_KEYS = {"basic_auth_password", "driver_pin", "excavator_pin"}


def fail(message: str) -> "NoReturn":
    raise SystemExit("network login smoke failed: " + message)


def read_private_json(path: Path, keys: set[str]) -> dict[str, object]:
    info = path.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        fail(f"private input permissions invalid: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != keys:
        fail(f"private input schema invalid: {path.name}")
    return value


def csrf_cookie(jar: http.cookiejar.CookieJar) -> str:
    for cookie in jar:
        if cookie.name == "csrftoken" and cookie.value:
            return cookie.value
    fail("CSRF cookie missing")


def request(opener, url: str, auth_header: str, data: bytes | None = None):
    headers = {
        "Authorization": auth_header,
        "Host": HOST,
        "Referer": BASE_URL + "/",
        "User-Agent": "sse-qa-disposable-network-smoke/1",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return opener.open(urllib.request.Request(url, data=data, headers=headers), timeout=30)


def login(role: str, phone: str, pin: str, expected_path: str, auth_header: str) -> None:
    jar = http.cookiejar.CookieJar()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=context),
    )
    with request(opener, BASE_URL + "/", auth_header) as response:
        if response.status != 200:
            fail(f"{role} login form HTTP {response.status}")
    payload = urllib.parse.urlencode(
        {
            "csrfmiddlewaretoken": csrf_cookie(jar),
            "phone": phone,
            "access_code": pin,
            "device_kind": "personal",
            "action": "login",
        }
    ).encode("ascii")
    with request(opener, BASE_URL + "/", auth_header, payload) as response:
        if response.status != 200:
            fail(f"{role} login HTTP {response.status}")
    with request(opener, BASE_URL + expected_path, auth_header) as response:
        if response.status != 200 or urllib.parse.urlsplit(response.geturl()).path != expected_path:
            fail(f"{role} screen unavailable or redirected")


def main() -> int:
    if os.geteuid() != 0 or len(sys.argv) != 3:
        fail("root and two private JSON paths required")
    secrets_path = Path(sys.argv[1]).resolve()
    network_path = Path(sys.argv[2]).resolve()
    if network_path != Path("/run/sse-qa-network-smoke.json"):
        fail("network input must use fixed /run path")
    secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
    line = secrets.get("basic_auth_line") if isinstance(secrets, dict) else None
    if not isinstance(line, str) or ":" not in line:
        fail("BasicAuth username unavailable")
    username = line.split(":", 1)[0]
    if not username or any(char in username for char in "\r\n:"):
        fail("BasicAuth username invalid")
    network = read_private_json(network_path, NETWORK_KEYS)
    password = network["basic_auth_password"]
    driver_pin = network["driver_pin"]
    excavator_pin = network["excavator_pin"]
    if not isinstance(password, str) or len(password) < 16:
        fail("BasicAuth password invalid")
    if not all(isinstance(pin, str) and len(pin) == 6 and pin.isdigit() for pin in (driver_pin, excavator_pin)):
        fail("PIN invalid")
    if driver_pin != secrets.get("driver_pin") or excavator_pin != secrets.get("excavator_pin"):
        fail("network and seeded PINs differ")
    raw = f"{username}:{password}".encode("utf-8")
    auth_header = "Basic " + base64.b64encode(raw).decode("ascii")
    login("driver", "+79000000001", driver_pin, "/driver/", auth_header)
    login("excavator", "+79000000002", excavator_pin, "/excavator/work/", auth_header)
    print("SSE_QA_NETWORK_LOGIN_OK logins=2 screens=2 https=1 nginx=1 basic_auth=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
