(function (window, document) {
    "use strict";

    var labels = {
        unknown: "Проверяем связь…",
        ok: "Связь есть",
        weak: "Переподключение…",
        lost: "Нет связи с сервером",
        recovering: "Восстанавливаем данные"
    };
    var authenticationEnded = false;

    function render() {
        if (!document.body) return;
        var state = document.body.dataset.connectionState || "unknown";
        if (!Object.prototype.hasOwnProperty.call(labels, state)) state = "unknown";
        var label = labels[state];
        Array.prototype.forEach.call(document.querySelectorAll("[data-connection-indicator]"), function (node) {
            node.setAttribute("title", label);
            node.setAttribute("aria-label", label);
            node.setAttribute("data-state-label", label);
            node.setAttribute("role", "status");
            node.setAttribute("aria-live", "polite");
            node.removeAttribute("aria-hidden");
        });
        var banner = document.querySelector("[data-app-realtime-status]");
        if (banner && !authenticationEnded) banner.textContent = label;
    }

    window.addEventListener("operational-state-connection", render);
    window.addEventListener("operational-state-refresh-applied", render);
    window.addEventListener("app-authentication-ended", function () { authenticationEnded = true; });
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", render);
    else render();
})(window, document);
