"""Replay an allowlisted tablet report GET with the identity of its signed read grant."""

from datetime import date
from urllib.parse import parse_qsl, urlsplit

from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpResponse, QueryDict
from django.urls import resolve, Resolver404

from .field_commands import trusted_request
from .field_signing import EnvelopeError

MAX_REPORT_BYTES = 8 * 1024 * 1024
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FILTERS = {
    "date",
    "date_from",
    "date_to",
    "shift_type",
    "status",
    "event_type",
    "critical",
    "equipment",
    "reason",
    "granularity",
    "chart_mode",
    "excavators",
    "edit",
    "template",
    "group_by",
    "loading_shift_type",
    "unloading_shift_type",
    "carryover",
    "truck",
    "excavator",
    "rock_type",
    "dump_point",
}
IDS = {
    "edit",
    "template",
    "equipment",
    "reason",
    "excavators",
    "truck",
    "excavator",
    "rock_type",
    "dump_point",
}


def _report_path(value):
    # The tablet resolver also includes login and POST-capable actions. Only its named
    # report aliases may be read here. The signed string is never sent to the network.
    from .tablet_urls import REPORT_NAMES

    if not isinstance(value, str) or len(value.encode()) > 4608 or any(ord(c) < 32 for c in value):
        raise EnvelopeError("report_not_allowed", "Указан недоступный адрес отчёта.", 403)
    target = urlsplit(value)
    if (
        target.netloc
        or target.scheme
        or target.fragment
        or "#" in value
        or "\\" in value
        or "%" in target.path
        or not target.path.startswith("/mining-master/tablet/")
    ):
        raise EnvelopeError("report_not_allowed", "Доступны только отчёты планшета.", 403)
    try:
        match = resolve(target.path, urlconf="assignments.tablet_urls")
    except Resolver404:
        raise EnvelopeError("report_not_allowed", "Такой отчёт на планшете недоступен.", 403)
    name = (match.url_name or "").removeprefix("mining_master_tablet_")
    if name not in REPORT_NAMES:
        raise EnvelopeError("report_not_allowed", "Доступны только отчёты планшета.", 403)
    try:
        if len(target.query.encode()) > 4096:
            raise ValueError()
        pairs = parse_qsl(target.query, keep_blank_values=True, max_num_fields=48, errors="strict")
        for key, val in pairs:
            if key not in FILTERS or len(val) > 128 or any(ord(c) < 32 for c in val):
                raise ValueError()
            if key in IDS and val and (not val.isascii() or not val.isdecimal() or len(val) > 18):
                raise ValueError()
            if key in {"date", "date_from", "date_to"} and val:
                if len(val) != 10 or not 1900 <= date.fromisoformat(val).year <= 2200:
                    raise ValueError()
    except (ValueError, UnicodeError):
        raise EnvelopeError("report_filters_invalid", "Проверьте параметры и даты отчёта.")
    return target, match, name.endswith("_export")


def render_report(access, path, csrf_secret):
    target, match, export = _report_path(path)
    request = trusted_request(access)
    session = SessionStore()
    session.update(request.session)
    request.session = session
    request.user = AnonymousUser()
    request.path = request.path_info = target.path
    request.resolver_match = match
    request.GET = QueryDict(target.query)
    request.META.update(QUERY_STRING=target.query, SERVER_NAME="localhost", SERVER_PORT="80")
    if csrf_secret:
        request.META["CSRF_COOKIE"] = csrf_secret
    original = match.func(request, *match.args, **match.kwargs)
    if original.status_code != 200 or original.streaming:
        raise EnvelopeError("report_not_allowed", "Нет доступа к этому отчёту.", 403)
    content_type = original.get("Content-Type", "")
    if content_type.partition(";")[0] != (XLSX if export else "text/html"):
        raise EnvelopeError("report_not_allowed", "Неожиданный вид ответа отчёта.", 403)
    if len(original.content) > MAX_REPORT_BYTES:
        raise EnvelopeError(
            "report_too_large", "Отчёт слишком велик. Выберите меньший период.", 413
        )
    # Never forward session cookies, redirect targets, or headers from the synthetic GET.
    response = HttpResponse(original.content, content_type=content_type)
    response["X-Copper-Report-Read"] = "1"
    if export and original.has_header("Content-Disposition"):
        response["Content-Disposition"] = original["Content-Disposition"]
    return response
