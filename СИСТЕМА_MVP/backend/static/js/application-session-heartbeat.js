(function (window, document) {
    "use strict";

    var endpoint = "/session-heartbeat/";
    var intervalMs = 30000;
    var timer = 0;

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

    function send() {
        if (document.hidden) return;
        var csrfToken = cookie("csrftoken") || document.querySelector('meta[name="csrf-token"]')?.content || "";
        if (!csrfToken) return;
        var body = new URLSearchParams();
        body.set("path", window.location.pathname);
        body.set("client_kind", clientKind());
        body.set("client_version", document.body?.dataset.nativeClientVersion || "");
        window.fetch(endpoint, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            keepalive: true,
            headers: {
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "X-CSRFToken": csrfToken,
                "X-Requested-With": "XMLHttpRequest"
            },
            body: body.toString()
        }).catch(function () {});
    }

    function schedule() {
        window.clearInterval(timer);
        send();
        timer = window.setInterval(send, intervalMs);
    }

    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) schedule();
    });
    window.addEventListener("focus", send);
    schedule();
})(window, document);
