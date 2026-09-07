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

    function readShiftState() {
        if (roleCode === "driver") {
            var driverShift = document.querySelector("[data-driver-shift-close-form]");
            return {
                required: !!driverShift,
                shiftId: driverShift ? String(driverShift.dataset.nativeShiftId || "") : ""
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
        var signature = (state.required ? "1" : "0") + ":" + state.shiftId;
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
            reason: reason || (state.required ? "shift_active" : "shift_inactive")
        })).then(function () {
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
        stop: stopForLogout
    };
    scheduleSync("initial_render");
})();
