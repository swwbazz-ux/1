(function () {
    "use strict";

    // This document is supplied by the APK at the workplace origin, outside
    // the server page. Only replace the role's PWA cache and registration.
    async function refresh() {
        if (navigator.serviceWorker) {
            const registrations = await navigator.serviceWorker.getRegistrations();
            await Promise.all(registrations.filter(registration =>
                [registration.active, registration.waiting, registration.installing].some(worker =>
                    worker && new URL(worker.scriptURL).pathname === "/mining-master-sw.js"
                )
            ).map(registration => registration.unregister()));
        }
        if (window.caches) {
            const names = await caches.keys();
            await Promise.all(names.filter(name => /^mining-master-mobile-shell-v[0-9]+$/.test(name))
                .map(name => caches.delete(name)));
        }
    }

    Promise.race([refresh(), new Promise(resolve => setTimeout(resolve, 2500))])
        .catch(() => {})
        .finally(() => location.replace(document.body.dataset.startUrl));
})();
