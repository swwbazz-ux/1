(function driverNativePushModule(window, document) {
    "use strict";

    if (window.__driverNativePushBinding) {
        window.__driverNativePushBinding.refresh();
        return;
    }

    var REGISTER_URL = "/driver/push/native/";
    var lastRegisteredKey = "";
    var inFlightKey = "";

    function plugin() {
        return window.Capacitor
            && window.Capacitor.Plugins
            && window.Capacitor.Plugins.NativePush;
    }

    function shell() {
        return document.querySelector("[data-driver-shell]");
    }

    function cookie(name) {
        var prefix = name + "=";
        var parts = String(document.cookie || "").split(";");
        for (var index = 0; index < parts.length; index += 1) {
            var part = parts[index].trim();
            if (part.indexOf(prefix) === 0) return decodeURIComponent(part.slice(prefix.length));
        }
        return "";
    }

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return cookie("csrftoken") || (meta && meta.content) || "";
    }

    function normalizedEnvelope(raw) {
        var value = raw || {};
        var envelope = {
            provider: String(value.provider || "").trim().toLowerCase(),
            token: String(value.token || "").trim(),
            platform: String(value.platform || "").trim().toLowerCase(),
            app_id: String(value.appId || value.app_id || "").trim()
        };
        if (!envelope.token || envelope.provider !== "fcm" || envelope.platform !== "android" || !envelope.app_id) {
            return null;
        }
        return envelope;
    }

    function identityKey(envelope, currentShell) {
        return [
            envelope.provider,
            envelope.token,
            envelope.platform,
            envelope.app_id,
            String(currentShell.dataset.driverAccessId || ""),
            String(currentShell.dataset.driverActorId || ""),
            String(currentShell.dataset.driverAuthGeneration || "")
        ].join("\n");
    }

    function register(raw) {
        var currentShell = shell();
        var envelope = normalizedEnvelope(raw);
        if (!currentShell || !envelope) return Promise.resolve(false);
        if (!currentShell.dataset.driverAccessId || !currentShell.dataset.driverAuthGeneration) {
            return Promise.resolve(false);
        }
        var key = identityKey(envelope, currentShell);
        if (key === lastRegisteredKey || key === inFlightKey) return Promise.resolve(true);
        inFlightKey = key;
        return window.fetch(REGISTER_URL, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify(envelope)
        }).then(function (response) {
            if (!response.ok) return false;
            lastRegisteredKey = key;
            return true;
        }).catch(function () {
            return false;
        }).then(function (registered) {
            if (inFlightKey === key) inFlightKey = "";
            return registered;
        });
    }

    function refresh() {
        var nativePush = plugin();
        if (!nativePush || typeof nativePush.getToken !== "function") {
            return Promise.resolve(false);
        }
        try {
            return Promise.resolve(nativePush.getToken()).then(register).catch(function () { return false; });
        } catch (error) {
            return Promise.resolve(false);
        }
    }

    var binding = {refresh: refresh};
    window.__driverNativePushBinding = binding;

    var nativePush = plugin();
    if (nativePush && typeof nativePush.addListener === "function") {
        try {
            nativePush.addListener("pushToken", function (event) { register(event); });
        } catch (error) {}
    }
    window.addEventListener("online", refresh);
    window.addEventListener("native-connectivity-resume", refresh);
    window.addEventListener("operational-state-refresh-applied", refresh);
    refresh();
})(window, document);
