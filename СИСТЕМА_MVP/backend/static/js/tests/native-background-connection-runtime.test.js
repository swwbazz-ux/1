const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
    path.resolve(__dirname, "../native-background-connection-v1.js"),
    "utf8"
);
const baseTemplate = fs.readFileSync(
    path.resolve(__dirname, "../../../templates/base.html"),
    "utf8"
);
const driverTemplate = fs.readFileSync(
    path.resolve(__dirname, "../../../templates/users/driver_shift.html"),
    "utf8"
);
const excavatorTemplate = fs.readFileSync(
    path.resolve(__dirname, "../../../templates/trips/excavator_work.html"),
    "utf8"
);

function createRuntime(roleCode, options = {}) {
    const calls = [];
    const events = [];
    let nativeListener = null;
    let snapshot = options.snapshot || {};
    const listeners = new Map();
    const timers = [];
    let shiftActive = false;
    let observerCallback = null;

    const plugin = {
        sync(options) {
            calls.push({method: "sync", options: {...options}});
            return Promise.resolve({...snapshot, desired: options.required});
        },
        stop(options) {
            calls.push({method: "stop", options: {...options}});
            return Promise.resolve({desired: false});
        },
        getState() {
            calls.push({method: "getState"});
            return Promise.resolve({...snapshot, pendingDriverShiftClose: null});
        },
        queueDriverShiftClose(options) {
            calls.push({method: "queueDriverShiftClose", options: {...options}});
            return Promise.resolve({pendingDriverShiftClose: {...options}});
        },
        acknowledgeDriverShiftClose(options) {
            calls.push({method: "acknowledgeDriverShiftClose", options: {...options}});
            return Promise.resolve({pendingDriverShiftClose: null});
        },
    };
    if (options.newBridge) {
        plugin.addListener = (name, callback) => { nativeListener = callback; return Promise.resolve({remove() {}}); };
    }
    const body = {
        dataset: {
            nativeApp: "true",
            appRoleCode: roleCode,
        },
    };
    const document = {
        body,
        querySelector(selector) {
            if (roleCode === "driver" && selector === "[data-driver-shell]") {
                return {dataset: {driverAuthGeneration: "driver-auth-7"}};
            }
            if (!shiftActive) return null;
            if (roleCode === "driver" && selector === "[data-driver-shift-close-form]") {
                return {dataset: {nativeShiftId: "driver-17"}};
            }
            if (roleCode === "excavator_operator" && selector === '[data-eo-work-available="true"]') {
                return {dataset: {}};
            }
            if (roleCode === "excavator_operator" && selector === "[data-eo-shell]") {
                return {dataset: {nativeShiftId: "excavator-23"}};
            }
            return null;
        },
        addEventListener(name, callback) {
            listeners.set(`document:${name}`, callback);
        },
    };
    const window = {
        Capacitor: {Plugins: {BackgroundConnection: plugin}},
        dispatchEvent(event) { events.push(event); },
        addEventListener(name, callback) {
            listeners.set(`window:${name}`, callback);
        },
        setTimeout(callback) {
            timers.push(callback);
            return timers.length;
        },
    };
    class MutationObserver {
        constructor(callback) {
            observerCallback = callback;
        }
        observe() {}
    }
    const context = vm.createContext({
        document,
        window,
        MutationObserver,
        Promise,
        CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init.detail; } },
        Date: {now() { return 1_000_000; }},
    });
    vm.runInContext(source, context);

    async function flush() {
        while (timers.length) timers.shift()();
        await Promise.resolve();
        await Promise.resolve();
        while (timers.length) timers.shift()();
        await Promise.resolve();
        await Promise.resolve();
    }

    return {
        calls,
        events,
        setHidden(value) { document.hidden = value; document.visibilityState = value ? "hidden" : "visible"; },
        setSnapshot(value) { snapshot = value; },
        emitNative(value) { if (nativeListener) nativeListener(value); },
        async flush() { await flush(); },
        async setShiftActive(value) {
            shiftActive = value;
            observerCallback();
            await flush();
        },
        async resume() {
            listeners.get("window:native-connectivity-resume")();
            await flush();
        },
        stopForLogout() {
            return context.window.NativeBackgroundConnection.stop();
        },
        connection: context.window.NativeBackgroundConnection,
    };
}

for (const roleCode of ["driver", "excavator_operator"]) {
    test(`${roleCode} native bridge follows server-rendered shift state`, async () => {
        const runtime = createRuntime(roleCode);
        await runtime.flush();
        assert.deepEqual(runtime.calls[0], {
            method: "sync",
            options: {
                required: false,
                shiftId: "",
                authGeneration: roleCode === "driver" ? "driver-auth-7" : "",
                reason: "initial_render",
            },
        });

        await runtime.setShiftActive(true);
        const active = runtime.calls.at(-1);
        assert.equal(active.method, "sync");
        assert.equal(active.options.required, true);
        assert.match(active.options.shiftId, roleCode === "driver" ? /driver-17/ : /excavator-23/);

        await runtime.setShiftActive(false);
        assert.equal(runtime.calls.at(-1).options.required, false);
    });
}

test("native bridge stops immediately on logout and rechecks after resume", async () => {
    const runtime = createRuntime("driver");
    await runtime.flush();
    await runtime.stopForLogout();
    assert.deepEqual(runtime.calls.at(-1), {method: "stop", options: {reason: "logout"}});

    const beforeResume = runtime.calls.length;
    await runtime.resume();
    assert.equal(runtime.calls.length, beforeResume + 1);
    assert.equal(runtime.calls.at(-1).method, "sync");
    assert.match(baseTemplate, /navigateAfterNativeConnectionStop\(link\.href\)/);
    assert.match(
        driverTemplate,
        /navigateAfterNativeConnectionStop\(logoutButton\.dataset\.driverLogoutUrl\)/
    );
    assert.match(excavatorTemplate, /navigateAfterNativeConnectionStop\(logoutUrl\)/);
    assert.match(driverTemplate, /NativeBackgroundConnection\.stop\(\)/);
    assert.match(excavatorTemplate, /NativeBackgroundConnection\.stop\(\)/);
});

test("browser and unrelated native roles never touch the Android plugin", () => {
    assert.match(source, /body\.dataset\.nativeApp !== "true"/);
    assert.match(source, /roleCode !== "driver" && roleCode !== "excavator_operator"/);
    assert.match(source, /\[data-driver-shift-close-form\]/);
    assert.match(source, /\[data-eo-work-available="true"\]/);
});

test("driver bridge exposes the durable shift-close outbox without widening other roles", async () => {
    const runtime = createRuntime("driver");
    await runtime.flush();
    const payload = {
        shiftId: "17",
        clientActionId: "driver-close-17",
        endFuel: "90",
        endMileage: "2600",
        endEngineHours: "712",
    };

    await runtime.connection.queueDriverShiftClose(payload);
    await runtime.connection.getState();
    await runtime.connection.acknowledgeDriverShiftClose(payload.clientActionId);

    assert.deepEqual(runtime.calls.slice(-3), [
        {method: "queueDriverShiftClose", options: payload},
        {method: "getState"},
        {
            method: "acknowledgeDriverShiftClose",
            options: {clientActionId: payload.clientActionId},
        },
    ]);

    const excavator = createRuntime("excavator_operator");
    await excavator.flush();
    await assert.rejects(
        excavator.connection.queueDriverShiftClose(payload),
        /Фоновая очередь недоступна/
    );
});

function nativeSuccess(overrides = {}) {
    return {
        status: "success", transportState: "ok", lastSuccessAtMs: 999_900,
        occurredAtMs: 999_900, failureCount: 0, serverVersion: 17,
        reason: "heartbeat_success", ...overrides,
    };
}

for (const roleCode of ["driver", "excavator_operator"]) {
    test(`${roleCode} forwards fresh native evidence once across listener and snapshot`, async () => {
        const evidence = nativeSuccess();
        const runtime = createRuntime(roleCode, {newBridge: true, snapshot: {transport: evidence}});
        await runtime.flush();
        assert.equal(runtime.events.length, 1);
        assert.equal(runtime.events[0].type, "native-connection-state");
        assert.equal(runtime.events[0].detail.serverVersion, 17);
        const callsBefore = runtime.calls.length;
        runtime.emitNative(evidence);
        runtime.emitNative(evidence);
        assert.equal(runtime.events.length, 1);
        assert.equal(runtime.calls.length, callsBefore, "native evidence must not initiate another native HTTP loop");
        await runtime.connection.getState();
        assert.equal(runtime.events.length, 1);
        runtime.emitNative(nativeSuccess({lastSuccessAtMs: 1_000_000, occurredAtMs: 1_000_000, serverVersion: 18}));
        assert.equal(runtime.events.length, 2);
    });
}

test("native bridge rejects stale future out-of-order and invalid evidence", async () => {
    const runtime = createRuntime("driver", {newBridge: true});
    await runtime.flush();
    runtime.emitNative(nativeSuccess({occurredAtMs: 980_000, lastSuccessAtMs: 980_000}));
    runtime.emitNative(nativeSuccess({occurredAtMs: 1_002_000, lastSuccessAtMs: 1_002_000}));
    runtime.emitNative(nativeSuccess({occurredAtMs: NaN}));
    runtime.emitNative(nativeSuccess({lastSuccessAtMs: 1_000_100}));
    assert.equal(runtime.events.length, 0);
    runtime.emitNative(nativeSuccess());
    runtime.emitNative(nativeSuccess({occurredAtMs: 999_800, lastSuccessAtMs: 999_800}));
    assert.equal(runtime.events.length, 1);
});

test("old APK lastAliveAt remains lifecycle-compatible without invented heartbeat", async () => {
    const runtime = createRuntime("excavator_operator", {snapshot: {lastAliveAt: 999_900}});
    await runtime.flush();
    await runtime.connection.getState();
    await runtime.setShiftActive(true);
    assert.equal(runtime.events.length, 0);
    assert.equal(runtime.calls.at(-1).options.required, true);
});

test("background evidence is applied once from a fresh resume snapshot", async () => {
    const runtime = createRuntime("driver", {newBridge: true});
    await runtime.flush();
    runtime.setHidden(true);
    runtime.emitNative(nativeSuccess());
    assert.equal(runtime.events.length, 0);
    runtime.setSnapshot({transport: nativeSuccess()});
    runtime.setHidden(false);
    await runtime.resume();
    assert.equal(runtime.events.length, 1);
});

test("failure evidence is sanitized and never leaks native error text or queue content", async () => {
    const runtime = createRuntime("driver", {newBridge: true});
    await runtime.flush();
    runtime.emitNative(nativeSuccess({status: "failure", transportState: "weak", failureCount: 1,
        reason: "https://example.invalid/?token=secret", cookie: "secret", pendingDriverShiftClose: {endFuel: "90"}}));
    assert.equal(runtime.events[0].detail.status, "failure");
    assert.equal(runtime.events[0].detail.failureCount, 1);
    assert.equal(runtime.events[0].detail.reason, "local_processing");
    assert.equal(JSON.stringify(runtime.events[0].detail).includes("secret"), false);
    assert.equal(JSON.stringify(runtime.events[0].detail).includes("endFuel"), false);
});
