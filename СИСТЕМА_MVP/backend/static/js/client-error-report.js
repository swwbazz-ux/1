(function (window, document) {
    "use strict";

    var REPORT_URL = "/client-error/";
    var seen = Object.create(null);
    var sent = 0;
    var recentConnections = [];
    var connectionWindowAt = 0;
    var connectionSent = 0;
    var connectionSeen = Object.create(null);
    var connectionIncidentActive = false;
    var states = ["unknown", "ok", "weak", "lost", "recovering"];
    var causes = ["planned_abort", "background_pause", "timeout", "network_error",
        "http_error", "invalid_payload", "offline_event", "online_event", "poll_stuck",
        "silent_timeout", "initial_timeout", "poll_success", "heartbeat_success",
        "native_heartbeat", "reconcile_pending", "reconcile_success", "reconcile_error",
        "outbox_pending", "outbox_applied", "resume", "auth_required", "state_change",
        "initial", "transport_success", "transport_recovered", "native_heartbeat_success",
        "web_heartbeat_success", "fragment_success",
        "dom_applied", "fragment_refresh", "fragment_failed", "custom_handler_missing",
        "custom_refresh_not_applied", "stale_fragment_version", "role_state_apply_failed",
        "offline", "outbox_state", "outbox_drained", "periodic_server_truth", "explicit_reconcile",
        "post_ack", "driver_refresh_failed", "excavator_refresh_failed"];

    function bodyData() { return document.body && document.body.dataset || {}; }
    function code(value, limit) {
        return String(value || "").replace(/[^a-zA-Z0-9_.:-]/g, "").slice(0, limit || 64);
    }
    function pathOnly(value) {
        if (!value) return "";
        try {
            var parsed = new window.URL(String(value), window.location.href);
            if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return "[resource]";
            return parsed.pathname.slice(0, 240);
        } catch (error) { return "[resource]"; }
    }
    function safeText(value, limit) {
        return String(value || "")
            .replace(/(?:https?:\/\/|\/)[^\s<>"')\]}]+/g, function (url) {
                return pathOnly(url);
            })
            .replace(/\bBearer\s+[a-zA-Z0-9_.~+\/-]+/gi, "Bearer [redacted]")
            .replace(/((?:["']?)(?:pin|password|access_code|token|secret|cookie|csrfmiddlewaretoken|csrftoken)(?:["']?)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)/gi, "$1[redacted]")
            .slice(0, limit);
    }
    function screenName() {
        var data = bodyData();
        return code(data.realtimeScreen || data.appRoleCode) || pathOnly(window.location.pathname);
    }
    function send(data) {
        var body = bodyData();
        var payload = JSON.stringify({
            message: safeText(data.message, 500),
            source: safeText(data.source, 300),
            stack: data.connection ? JSON.stringify(data.connection) : safeText(data.stack, 2000),
            screen: screenName(), role: code(body.appRoleCode), appVersion: code(body.appShellVersion)
        });
        try {
            if (window.navigator && window.navigator.sendBeacon) {
                var blob = new window.Blob([payload], {type: "application/json"});
                if (window.navigator.sendBeacon(REPORT_URL, blob)) return;
            }
        } catch (error) {}
        try {
            window.fetch(REPORT_URL, {
                method: "POST", credentials: "same-origin", cache: "no-store", keepalive: true,
                headers: {"Content-Type": "application/json"}, body: payload
            }).catch(function () {});
        } catch (error) {}
    }
    function report(data) {
        var key = safeText(data.message, 500) + "|" + safeText(data.source, 300);
        if (sent >= 5 || seen[key]) return;
        seen[key] = true;
        sent += 1;
        send(data);
    }
    function plannedAbort(error) {
        return !!(error && error.name === "AbortError" && error.__appPlannedAbort === true);
    }
    function number(value, maximum) {
        var parsed = Number(value);
        return isFinite(parsed) && parsed >= 0 ? Math.min(Math.floor(parsed), maximum) : 0;
    }
    function state(value) { return states.indexOf(value) >= 0 ? value : "unknown"; }
    function recordConnection(input) {
        input = input || {};
        var body = bodyData();
        var httpFailure = /^http_([1-5][0-9]{2})$/.exec(input.cause || "");
        var detail = {
            role: code(body.appRoleCode), appVersion: code(body.appShellVersion),
            clientKind: body.nativeApp === "true" ? "android_apk" : code(body.clientKind || body.appClientKind || "web"),
            nativeVersion: code(body.nativeClientVersion),
            cause: httpFailure ? "http_error" : (causes.indexOf(input.cause) >= 0 ? input.cause : "state_change"),
            owner: ["realtime_poll", "fragment", "native_heartbeat", "web_heartbeat", "outbox", "watchdog", "lifecycle"].indexOf(input.owner) >= 0 ? input.owner : "realtime_poll",
            generation: number(input.generation, Number.MAX_SAFE_INTEGER),
            failureCount: number(input.failureCount, 10000),
            lastSuccessAt: number(input.lastSuccessAt, Number.MAX_SAFE_INTEGER),
            durationMs: number(input.durationMs, 86400000),
            nativeHeartbeatFresh: input.nativeHeartbeatFresh === true,
            pendingOutboxCount: number(input.pendingOutboxCount, 100000),
            fromState: state(input.fromState), toState: state(input.toState),
            httpStatus: number(httpFailure ? httpFailure[1] : input.httpStatus, 599)
        };
        recentConnections.push(detail);
        if (recentConnections.length > 24) recentConnections.shift();
        // Плановая отмена и уход в фон полезны при разборе, но не являются падением.
        if (detail.cause === "planned_abort" || detail.cause === "background_pause") return;
        var badState = function (value) { return value === "weak" || value === "lost"; };
        var incident = connectionIncidentActive || badState(detail.fromState) || badState(detail.toState);
        connectionIncidentActive = incident && detail.toState !== "ok";
        // Штатные periodic/outbox reconciliation остаются в памяти: они не являются
        // ошибкой и не должны расходовать бюджет следующего реального инцидента.
        if (!incident) return;
        // Успешные повторные heartbeat не создают поток записей в журнале ошибок.
        if (detail.fromState === detail.toState) return;
        var now = Date.now();
        if (!connectionWindowAt || now - connectionWindowAt >= 60000) {
            connectionWindowAt = now;
            connectionSent = 0;
            connectionSeen = Object.create(null);
        }
        var key = detail.fromState + ":" + detail.toState + ":" + detail.cause;
        if (connectionSent >= 6 || connectionSeen[key]) return;
        connectionSeen[key] = true;
        connectionSent += 1;
        send({message: "Связь: " + detail.fromState + " → " + detail.toState + " (" + detail.cause + ")",
            source: "connection", connection: detail});
    }

    window.AppClientDiagnostics = {
        getRecentConnections: function () {
            return recentConnections.map(function (item) { return Object.assign({}, item); });
        }
    };
    window.addEventListener("app:connectiondiagnostic", function (event) {
        recordConnection(event && event.detail);
    });
    window.addEventListener("error", function (event) {
        if (!event) return;
        // Один resource error не доказывает плановую отмену. Сохраняем именно DOM target.
        if (event.target && event.target !== window && event.target.tagName) {
            var tag = code(event.target.tagName, 24).toUpperCase();
            var path = pathOnly(event.target.currentSrc || event.target.src || event.target.href);
            report({message: "Не загрузился ресурс: " + tag + " " + path, source: "resource:" + tag,
                stack: JSON.stringify({tag: tag, path: path})});
            return;
        }
        if (plannedAbort(event.error)) return;
        report({message: event.message || "Ошибка скрипта",
            source: pathOnly(event.filename) + (event.lineno ? ":" + number(event.lineno, 10000000) : ""),
            stack: event.error && event.error.stack || ""});
    }, true);
    window.addEventListener("unhandledrejection", function (event) {
        var reason = event && event.reason;
        if (plannedAbort(reason)) return;
        report({message: "Необработанный сбой: " + (reason && reason.message ? reason.message : String(reason)),
            stack: reason && reason.stack || ""});
    });
})(window, document);
