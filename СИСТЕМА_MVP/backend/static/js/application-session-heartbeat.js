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

    function send() {
        if (document.hidden) return;
        var csrfToken = cookie("csrftoken") || document.querySelector('meta[name="csrf-token"]')?.content || "";
        if (!csrfToken) return;
        var body = new URLSearchParams();
        body.set("path", window.location.pathname);
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
