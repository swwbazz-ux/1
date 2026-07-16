(function () {
    "use strict";

    if (!("serviceWorker" in navigator)) return;

    var workerMeta = document.querySelector('meta[name="role-app-service-worker"]');
    var scopeMeta = document.querySelector('meta[name="role-app-scope"]');
    if (!workerMeta || !scopeMeta) return;

    var workerUrl = workerMeta.content;
    var scope = scopeMeta.content;
    if (!workerUrl || !scope) return;

    window.addEventListener("load", function () {
        navigator.serviceWorker.register(workerUrl, { scope: scope }).then(function (registration) {
            registration.update().catch(function () {});
            if (registration.waiting) registration.waiting.postMessage({ type: "SKIP_WAITING" });
        }).catch(function () {});
    });
}());
