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

    window.addEventListener("load", retireLegacySettlementPwa);
}());
