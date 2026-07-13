(function () {
    "use strict";

    if (!("serviceWorker" in navigator)) return;

    var WORKER_URL = "/deputy-mining-manager-sw.js";
    var PWA_SCOPE = "/deputy-mining-manager/";
    var registrationRef = null;

    function removeCachedPlanningDocuments() {
        if (!("caches" in window)) return Promise.resolve();
        return caches.keys().then(function (cacheNames) {
            return Promise.all(cacheNames.map(function (cacheName) {
                return caches.open(cacheName).then(function (cache) {
                    return cache.keys().then(function (requests) {
                        return Promise.all(requests.filter(function (request) {
                            var url = new URL(request.url);
                            return url.origin === window.location.origin &&
                                url.pathname.indexOf(PWA_SCOPE) === 0;
                        }).map(function (request) {
                            return cache.delete(request);
                        }));
                    });
                });
            }));
        }).catch(function () {});
    }

    function activateWaitingWorker(registration) {
        if (!registration || !registration.waiting) return;
        registration.waiting.postMessage({ type: "SKIP_WAITING" });
    }

    function watchRegistration(registration) {
        registrationRef = registration;
        activateWaitingWorker(registration);

        registration.addEventListener("updatefound", function () {
            var installing = registration.installing;
            if (!installing) return;
            installing.addEventListener("statechange", function () {
                if (installing.state === "installed" && navigator.serviceWorker.controller) {
                    activateWaitingWorker(registration);
                }
            });
        });
    }

    function requestWorkerUpdate() {
        if (!registrationRef) return;
        registrationRef.update().catch(function () {});
    }

    navigator.serviceWorker.register(WORKER_URL, { scope: PWA_SCOPE })
        .then(function (registration) {
            watchRegistration(registration);
            requestWorkerUpdate();
        })
        .catch(function () {});

    removeCachedPlanningDocuments();
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) requestWorkerUpdate();
    });
    window.addEventListener("focus", requestWorkerUpdate);
    window.addEventListener("online", requestWorkerUpdate);
})();
