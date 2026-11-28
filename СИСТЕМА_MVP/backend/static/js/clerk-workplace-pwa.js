(function () {
    "use strict";

    var LEGACY_SCOPE_PATH = "/settlement/";
    var LEGACY_WORKER_PATH = "/settlement/sw.js";
    var LEGACY_CACHE_PREFIX = "settlement-clerk-shell-";

    function pathname(value) {
        try {
            return new URL(value, window.location.origin).pathname;
        } catch (error) {
            return "";
        }
    }

    function isLegacyRegistration(registration) {
        var registrationScope = registration ? pathname(registration.scope) : "";
        if (
            registrationScope !== LEGACY_SCOPE_PATH
            && registrationScope !== "/"
        ) {
            return false;
        }
        return [registration.active, registration.waiting, registration.installing]
            .some(function (worker) {
                return worker && pathname(worker.scriptURL) === LEGACY_WORKER_PATH;
            });
    }

    function retireLegacyRegistration() {
        var serviceWorker = navigator.serviceWorker;
        if (!serviceWorker || !serviceWorker.getRegistrations) {
            return Promise.resolve();
        }
        return serviceWorker.getRegistrations().then(function (registrations) {
            return Promise.all(
                registrations
                    .filter(isLegacyRegistration)
                    .map(function (registration) {
                        return registration.unregister();
                    })
            );
        });
    }

    function clearLegacyCaches() {
        if (!("caches" in window)) return Promise.resolve();
        return window.caches.keys().then(function (keys) {
            return Promise.all(
                keys
                    .filter(function (key) {
                        return key.startsWith(LEGACY_CACHE_PREFIX);
                    })
                    .map(function (key) {
                        return window.caches.delete(key);
                    })
            );
        });
    }

    function retireLegacySettlementPwa() {
        return Promise.all([
            retireLegacyRegistration(),
            clearLegacyCaches()
        ]).catch(function () {});
    }

    function initWorkplaceTheme() {
        var shell = document.querySelector(".clerk-workplace-shell");
        var toggle = shell && shell.querySelector("[data-admin-theme-toggle]");
        if (!toggle) return;

        function applyTheme(isNight) {
            shell.classList.toggle("admin-night", isNight);
            shell.classList.toggle("admin-day", !isNight);
            toggle.dataset.themeIcon = isNight ? "moon" : "sun";
            toggle.title = isNight ? "Включить дневную тему" : "Включить ночную тему";
            toggle.setAttribute("aria-label", toggle.title);
        }

        var savedTheme;
        try { savedTheme = window.localStorage.getItem("clerk-theme"); } catch (error) {}
        applyTheme(savedTheme === "night");
        toggle.addEventListener("click", function () {
            var isNight = !shell.classList.contains("admin-night");
            applyTheme(isNight);
            try { window.localStorage.setItem("clerk-theme", isNight ? "night" : "day"); } catch (error) {}
        });
    }

    function initConnectionIndicator() {
        function setState(state) {
            if (document.body.dataset.connectionState === state) return;
            document.body.dataset.connectionState = state;
            window.dispatchEvent(new CustomEvent("operational-state-connection", {
                detail: {state: state}
            }));
        }

        window.addEventListener("web-heartbeat-success", function (event) {
            var detail = event.detail || {};
            if (detail.source === "application-session-heartbeat" && detail.status === 204) {
                setState("ok");
            }
        });
        window.addEventListener("app:connectiondiagnostic", function (event) {
            var detail = event.detail || {};
            if (detail.owner !== "web_heartbeat" || detail.cause === "planned_abort") return;
            setState(detail.cause === "network_error" || detail.cause === "timeout" ? "lost" : "weak");
        });
        window.addEventListener("offline", function () { setState("lost"); });
        window.addEventListener("online", function () { setState("weak"); });
        if (navigator.onLine === false) setState("lost");
        else if (window.AppSessionHeartbeat && window.AppSessionHeartbeat.lastSuccessAtMs > 0) setState("ok");
    }

    window.addEventListener("DOMContentLoaded", initWorkplaceTheme);
    window.addEventListener("DOMContentLoaded", initConnectionIndicator);
    window.addEventListener("load", retireLegacySettlementPwa);
}());
