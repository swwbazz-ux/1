(function () {
    "use strict";

    var body = document.body;
    if (!body || body.dataset.nativeApp !== "true") {
        return;
    }
    var roleCode = String(body.dataset.appRoleCode || "");
    if (roleCode !== "driver" && roleCode !== "excavator_operator") {
        return;
    }

    var plugin = window.Capacitor
        && window.Capacitor.Plugins
        && window.Capacitor.Plugins.BackgroundConnection;
    if (!plugin || typeof plugin.sync !== "function") {
        return;
    }

    var lastSignature = "";
    var syncScheduled = false;
    var syncInFlight = false;
    var syncAgain = false;
    var lastEvidenceKey = "";
    var lastEvidenceAt = 0;

    function publishTransport(snapshot) {
        var evidence = snapshot && (snapshot.transport || snapshot);
        // Older APKs have lastAliveAt only. They continue lifecycle/outbox sync, without
        // inventing fresh transport evidence from an unversioned historical timestamp.
        if (!evidence || (evidence.status !== "success" && evidence.status !== "failure")) return;
        if (document.hidden || document.visibilityState === "hidden") return;
        var occurredAt = Number(evidence.occurredAtMs);
        var lastSuccessAt = Number(evidence.lastSuccessAtMs);
        var now = Date.now();
        if (!Number.isFinite(occurredAt) || occurredAt <= 0 || occurredAt < lastEvidenceAt
            || occurredAt > now + 1000 || now - occurredAt > 15000
            || !Number.isFinite(lastSuccessAt) || lastSuccessAt < 0 || lastSuccessAt > occurredAt) return;
        if (evidence.status === "success" && (lastSuccessAt <= 0 || now - lastSuccessAt > 15000)) return;
        var key = evidence.status + ":" + occurredAt + ":" + lastSuccessAt;
        if (key === lastEvidenceKey) return;
        var failureCount = Number(evidence.failureCount);
        var serverVersion = Number(evidence.serverVersion);
        var reason = String(evidence.reason || "");
        var allowedReason = /^(heartbeat_success|timeout|network_error|invalid_payload|cookie_timeout|local_processing|authentication_ended|http_[1-5][0-9]{2})$/;
        var detail = {
            status: evidence.status,
            transportState: /^(ok|weak|lost|auth_required)$/.test(String(evidence.transportState || ""))
                ? evidence.transportState : "unknown",
            occurredAtMs: occurredAt,
            lastSuccessAtMs: lastSuccessAt,
            failureCount: Number.isSafeInteger(failureCount) && failureCount >= 0 ? failureCount : 0,
            reason: allowedReason.test(reason) ? reason : "local_processing",
            serverVersion: Number.isSafeInteger(serverVersion) && serverVersion >= 0 ? serverVersion : 0
        };
        lastEvidenceKey = key;
        lastEvidenceAt = occurredAt;
        window.dispatchEvent(new CustomEvent("native-connection-state", {detail: detail}));
    }

    if (typeof plugin.addListener === "function") {
        Promise.resolve(plugin.addListener("connectionState", publishTransport)).catch(function () {});
    }

    function readShiftState() {
        if (roleCode === "driver") {
            var driverShift = document.querySelector("[data-driver-shift-close-form]");
            var driverShell = document.querySelector("[data-driver-shell]");
            return {
                required: !!driverShift,
                shiftId: driverShift ? String(driverShift.dataset.nativeShiftId || "") : "",
                authGeneration: driverShell ? String(driverShell.dataset.driverAuthGeneration || "") : ""
            };
        }
        var excavatorWork = document.querySelector('[data-eo-work-available="true"]');
        var excavatorShell = document.querySelector("[data-eo-shell]");
        return {
            required: !!excavatorWork,
            shiftId: excavatorShell ? String(excavatorShell.dataset.nativeShiftId || "") : ""
        };
    }

    function runSync(reason) {
        var state = readShiftState();
        var signature = (state.required ? "1" : "0") + ":" + state.shiftId + ":" + String(state.authGeneration || "");
        if (syncInFlight) {
            syncAgain = true;
            return;
        }
        if (signature === lastSignature) {
            return;
        }
        syncInFlight = true;
        Promise.resolve(plugin.sync({
            required: state.required,
            shiftId: state.shiftId,
            authGeneration: String(state.authGeneration || ""),
            reason: reason || (state.required ? "shift_active" : "shift_inactive")
        })).then(function (snapshot) {
            publishTransport(snapshot);
            lastSignature = signature;
        }).catch(function () {
            lastSignature = "";
        }).finally(function () {
            syncInFlight = false;
            if (syncAgain) {
                syncAgain = false;
                scheduleSync("dom_changed");
            }
        });
    }

    function scheduleSync(reason) {
        if (syncScheduled) {
            return;
        }
        syncScheduled = true;
        window.setTimeout(function () {
            syncScheduled = false;
            runSync(reason);
        }, 0);
    }

    function stopForLogout() {
        if (typeof plugin.stop !== "function") {
            return Promise.resolve();
        }
        lastSignature = "0:";
        return Promise.resolve(plugin.stop({reason: "logout"})).catch(function () {});
    }

    var observer = new MutationObserver(function () {
        scheduleSync("dom_changed");
    });
    observer.observe(body, {childList: true, subtree: true, attributes: true});

    window.addEventListener("pageshow", function () {
        lastSignature = "";
        scheduleSync("pageshow");
    });
    window.addEventListener("native-connectivity-resume", function () {
        lastSignature = "";
        scheduleSync("native_resume");
    });

    window.NativeBackgroundConnection = {
        sync: function () {
            lastSignature = "";
            scheduleSync("manual_sync");
        },
        stop: stopForLogout,
        getState: function () {
            if (typeof plugin.getState !== "function") return Promise.resolve({});
            return Promise.resolve(plugin.getState()).then(function (snapshot) {
                publishTransport(snapshot);
                return snapshot;
            }).catch(function () { return {}; });
        },
        queueDriverShiftClose: function (payload) {
            if (roleCode !== "driver" || typeof plugin.queueDriverShiftClose !== "function") {
                return Promise.reject(new Error("Фоновая очередь недоступна"));
            }
            return Promise.resolve(plugin.queueDriverShiftClose(payload || {}));
        },
        acknowledgeDriverShiftClose: function (clientActionId) {
            if (roleCode !== "driver" || typeof plugin.acknowledgeDriverShiftClose !== "function") {
                return Promise.resolve({});
            }
            return Promise.resolve(plugin.acknowledgeDriverShiftClose({
                clientActionId: String(clientActionId || "")
            }));
        }
    };
    scheduleSync("initial_render");
})();
