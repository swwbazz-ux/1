(function (window, document) {
    "use strict";

    /* Отправка падений на сервер.

       Ошибка на телефоне сотрудника раньше не попадала никуда: человек видел
       сломанный экран, а система об этом не знала. При полусотне людей это
       значит, что большую часть проблем не увидеть, пока кто-нибудь не напишет
       сам — а пишут далеко не все.

       Отправляем только то, что нужно для разбора: что упало, где, на какой
       версии. Ничего из введённого человеком сюда не попадает. */

    var URL = "/client-error/";
    /* Одна и та же поломка повторяется десятки раз за минуту: шлём по разу. */
    var seen = {};
    var MAX_PER_SCREEN = 5;
    var sent = 0;

    function screenName() {
        var body = document.body;
        if (!body) return "";
        return body.dataset.realtimeScreen || body.dataset.appRoleCode || window.location.pathname;
    }

    function report(data) {
        if (sent >= MAX_PER_SCREEN) return;
        var key = (data.message || "") + "|" + (data.source || "") + "|" + (data.line || "");
        if (seen[key]) return;
        seen[key] = true;
        sent += 1;

        var payload = JSON.stringify({
            message: data.message || "",
            source: (data.source || "") + (data.line ? ":" + data.line : ""),
            stack: data.stack || "",
            screen: screenName(),
            role: (document.body && document.body.dataset.appRoleCode) || "",
            appVersion: (document.body && document.body.dataset.appShellVersion) || ""
        });

        /* sendBeacon доносит отчёт, даже если страница в этот момент закрывается. */
        try {
            if (window.navigator && window.navigator.sendBeacon) {
                var blob = new window.Blob([payload], {type: "application/json"});
                if (window.navigator.sendBeacon(URL, blob)) return;
            }
        } catch (error) {}

        try {
            window.fetch(URL, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                keepalive: true,
                headers: {"Content-Type": "application/json"},
                body: payload
            }).catch(function () {});
        } catch (error) {}
    }

    window.addEventListener("error", function (event) {
        if (!event) return;
        /* Не догрузился файл или картинка — это не падение скрипта. */
        if (event.target && event.target !== window && event.target.tagName) {
            report({
                message: "Не загрузился ресурс: " + (event.target.currentSrc || event.target.src || event.target.href || event.target.tagName),
                source: "resource"
            });
            return;
        }
        report({
            message: event.message || "Ошибка скрипта",
            source: event.filename || "",
            line: event.lineno || "",
            stack: event.error && event.error.stack ? String(event.error.stack) : ""
        });
    }, true);

    window.addEventListener("unhandledrejection", function (event) {
        var reason = event && event.reason;
        report({
            message: "Необработанный сбой: " + (reason && reason.message ? reason.message : String(reason)),
            stack: reason && reason.stack ? String(reason.stack) : ""
        });
    });
})(window, document);
