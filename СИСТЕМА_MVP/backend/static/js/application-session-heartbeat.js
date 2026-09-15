(function (window, document) {
    "use strict";

    if (window.AppSessionHeartbeat) return;
    var endpoint = "/session-heartbeat/";
    var intervalMs = 30000;
    var requestTimeoutMs = 8000;
    var timer = 0;
    var timeoutTimer = 0;
    var generation = 0;
    var ownerGeneration = 0;
    var controller = null;
    var pagePaused = document.hidden === true;
    var lastAttemptAt = 0;
    var state = {lastSuccessAtMs: 0};
    window.AppSessionHeartbeat = state;

    function cookie(name) {
        var prefix = name + "=";
        var item = document.cookie.split(";").map(function (part) {
            return part.trim();
        }).find(function (part) {
            return part.startsWith(prefix);
        });
        return item ? decodeURIComponent(item.slice(prefix.length)) : "";
    }

    function clientKind() {
        var body = document.body;
        var userAgent = String(window.navigator && window.navigator.userAgent || "").toLowerCase();
        var isIos = /iphone|ipad|ipod/.test(userAgent);
        var isAndroid = /android/.test(userAgent);
        var isStandalone = Boolean(
            (window.navigator && window.navigator.standalone === true)
            || (window.matchMedia && (
                window.matchMedia("(display-mode: standalone)").matches
                || window.matchMedia("(display-mode: fullscreen)").matches
                || window.matchMedia("(display-mode: minimal-ui)").matches
            ))
        );
        var isSafari = /safari/.test(userAgent)
            && !/(crios|fxios|edgios|opios|chrome|chromium)/.test(userAgent);

        if (body && body.dataset.nativeApp === "true") return "android_apk";
        if (isStandalone && isAndroid) return "android_pwa";
        if (isStandalone && isIos) return "ios_pwa";
        if (isStandalone) return "pwa";
        if (isSafari) return "safari";
        return "browser";
    }

    function active() {
        return !pagePaused && document.hidden !== true && window.navigator.onLine !== false;
    }

    function diagnostic(cause, owner) {
        if (typeof window.CustomEvent !== "function" || typeof window.dispatchEvent !== "function") return;
        window.dispatchEvent(new window.CustomEvent("app:connectiondiagnostic", {detail: {
            owner: "web_heartbeat", cause: cause, generation: owner
        }}));
    }

    function scheduleNext() {
        window.clearTimeout(timer);
        timer = 0;
        if (!active()) return;
        var delay = lastAttemptAt
            ? Math.max(1000, intervalMs - (Date.now() - lastAttemptAt))
            : intervalMs;
        timer = window.setTimeout(function () {
            timer = 0;
            send();
        }, delay);
    }

    function cancelOwner(cause) {
        if (!ownerGeneration) return;
        var previous = ownerGeneration;
        var previousController = controller;
        ownerGeneration = 0;
        controller = null;
        generation += 1;
        window.clearTimeout(timeoutTimer);
        timeoutTimer = 0;
        if (previousController) {
            try { previousController.abort(); } catch (error) {}
        }
        diagnostic(cause, previous);
    }

    function send() {
        if (!active() || ownerGeneration) return;
        // Focus alone cannot turn the 30-second presence clock into another poll.
        // A real pause/resume resets lastAttemptAt and gets one prompt catch-up.
        if (lastAttemptAt && Date.now() - lastAttemptAt < intervalMs) {
            if (!timer) scheduleNext();
            return;
        }
        var csrfToken = cookie("csrftoken") || document.querySelector('meta[name="csrf-token"]')?.content || "";
        if (!csrfToken) { scheduleNext(); return; }
        window.clearTimeout(timer);
        timer = 0;
        lastAttemptAt = Date.now();
        var owner = ++generation;
        ownerGeneration = owner;
        var requestController = window.AbortController ? new window.AbortController() : null;
        controller = requestController;
        var body = new URLSearchParams();
        body.set("path", window.location.pathname);
        body.set("client_kind", clientKind());
        body.set("client_version", document.body?.dataset.nativeClientVersion || "");
        var options = {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "X-CSRFToken": csrfToken,
                "X-Requested-With": "XMLHttpRequest"
            },
            body: body.toString()
        };
        if (requestController) options.signal = requestController.signal;
        timeoutTimer = window.setTimeout(function () {
            if (owner !== ownerGeneration) return;
            cancelOwner("timeout");
            scheduleNext();
        }, requestTimeoutMs);
        var request;
        try { request = window.fetch(endpoint, options); }
        catch (error) { request = Promise.reject(error); }
        Promise.resolve(request).then(function (response) {
            if (owner !== ownerGeneration || !active()) return;
            // The authenticated presence endpoint returns exactly 204. HTML login,
            // redirects, 401/403 and arbitrary JSON are never transport-success proof.
            if (!response || response.status !== 204 || !response.ok || response.redirected) {
                diagnostic(response && response.status ? "http_" + response.status : "invalid_response", owner);
                return;
            }
            state.lastSuccessAtMs = Date.now();
            window.dispatchEvent(new window.CustomEvent("web-heartbeat-success", {detail: {
                source: "application-session-heartbeat", status: 204,
                occurredAtMs: state.lastSuccessAtMs
            }}));
        }).catch(function () {
            if (owner === ownerGeneration && active()) diagnostic("network_error", owner);
        }).finally(function () {
            if (owner !== ownerGeneration) return;
            window.clearTimeout(timeoutTimer);
            timeoutTimer = 0;
            ownerGeneration = 0;
            controller = null;
            scheduleNext();
        });
    }

    function pause() {
        pagePaused = true;
        window.clearTimeout(timer);
        timer = 0;
        cancelOwner("planned_abort");
    }

    function resume() {
        if (document.hidden) return;
        if (pagePaused) lastAttemptAt = 0;
        pagePaused = false;
        send();
    }

    document.addEventListener("visibilitychange", function () {
        if (document.hidden) pause(); else resume();
    });
    document.addEventListener("pause", pause);
    document.addEventListener("resume", resume);
    window.addEventListener("pagehide", pause);
    window.addEventListener("pageshow", resume);
    window.addEventListener("focus", resume);
    window.addEventListener("resume", resume);
    window.addEventListener("native-connectivity-resume", resume);
    window.addEventListener("offline", pause);
    window.addEventListener("online", resume);
    send();
})(window, document);
