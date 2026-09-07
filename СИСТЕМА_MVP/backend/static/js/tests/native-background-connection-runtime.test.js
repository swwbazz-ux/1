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

function createRuntime(roleCode) {
    const calls = [];
    const listeners = new Map();
    const timers = [];
    let shiftActive = false;
    let observerCallback = null;

    const plugin = {
        sync(options) {
            calls.push({method: "sync", options: {...options}});
            return Promise.resolve({desired: options.required});
        },
        stop(options) {
            calls.push({method: "stop", options: {...options}});
            return Promise.resolve({desired: false});
        },
    };
    const body = {
        dataset: {
            nativeApp: "true",
            appRoleCode: roleCode,
        },
    };
    const document = {
        body,
        querySelector(selector) {
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
    };
}

for (const roleCode of ["driver", "excavator_operator"]) {
    test(`${roleCode} native bridge follows server-rendered shift state`, async () => {
        const runtime = createRuntime(roleCode);
        await runtime.flush();
        assert.deepEqual(runtime.calls[0], {
            method: "sync",
            options: {required: false, shiftId: "", reason: "initial_render"},
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
    assert.match(driverTemplate, /NativeBackgroundConnection\.stop\(\)[\s\S]*?driverLogoutUrl/);
    assert.match(excavatorTemplate, /NativeBackgroundConnection\.stop\(\)[\s\S]*?eoLogoutUrl/);
});

test("browser and unrelated native roles never touch the Android plugin", () => {
    assert.match(source, /body\.dataset\.nativeApp !== "true"/);
    assert.match(source, /roleCode !== "driver" && roleCode !== "excavator_operator"/);
    assert.match(source, /\[data-driver-shift-close-form\]/);
    assert.match(source, /\[data-eo-work-available="true"\]/);
});
