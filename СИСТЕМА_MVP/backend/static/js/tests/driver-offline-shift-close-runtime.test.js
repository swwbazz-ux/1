"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const template = fs.readFileSync(
    path.resolve(__dirname, "../../../templates/users/driver_shift.html"),
    "utf8"
);

function markedSource(startMarker, endMarker) {
    const start = template.indexOf(startMarker);
    const end = template.indexOf(endMarker, start + startMarker.length);
    assert.notEqual(start, -1);
    assert.notEqual(end, -1);
    return template.slice(start + startMarker.length, end);
}

class MemoryStorage {
    constructor() {
        this.values = new Map();
    }
    getItem(key) {
        return this.values.has(key) ? this.values.get(key) : null;
    }
    setItem(key, value) {
        this.values.set(key, String(value));
    }
    removeItem(key) {
        this.values.delete(key);
    }
}

function classList() {
    const values = new Set();
    return {
        add(...names) { names.forEach((name) => values.add(name)); },
        remove(...names) { names.forEach((name) => values.delete(name)); },
        contains(name) { return values.has(name); },
    };
}

function createForm() {
    const fields = new Map([
        ["client_action_id", {value: "driver-close-offline-1", disabled: false}],
        ["end_fuel", {value: "90", disabled: false}],
        ["end_mileage", {value: "2600", disabled: false}],
        ["end_engine_hours", {value: "712", disabled: false}],
    ]);
    const panel = {hidden: true};
    const controls = [...fields.values(), {disabled: false}];
    return {
        dataset: {nativeShiftId: "17"},
        classList: classList(),
        isConnected: true,
        querySelector(selector) {
            const match = selector.match(/^\[name="([^"]+)"\]$/);
            if (match) return fields.get(match[1]) || null;
            if (selector === "[data-driver-shift-sync-pending]") return panel;
            return null;
        },
        querySelectorAll(selector) {
            return selector === "input, button, select, textarea" ? controls : [];
        },
        panel,
        fields,
        controls,
    };
}

function createRuntime({networkResult = false} = {}) {
    const localStorage = new MemoryStorage();
    const nativeCalls = [];
    const toasts = [];
    const listeners = new Map();
    const form = createForm();
    const body = {dataset: {}};
    const native = {
        queueDriverShiftClose(payload) {
            nativeCalls.push({method: "queue", payload: {...payload}});
            return Promise.resolve({pendingDriverShiftClose: payload});
        },
        acknowledgeDriverShiftClose(clientActionId) {
            nativeCalls.push({method: "ack", clientActionId});
            return Promise.resolve({pendingDriverShiftClose: null});
        },
        getState() {
            nativeCalls.push({method: "state"});
            return Promise.resolve({pendingDriverShiftClose: null});
        },
    };
    const window = {
        localStorage,
        NativeBackgroundConnection: native,
        submitDriverFormInPlace(submittedForm, options) {
            nativeCalls.push({method: "network", submittedForm, options});
            return Promise.resolve(networkResult);
        },
        showDriverToast(message, tone) {
            toasts.push({message, tone});
        },
        addEventListener(name, callback) {
            listeners.set(name, callback);
        },
    };
    const document = {
        body,
        querySelector(selector) {
            return selector === "[data-driver-shift-close-form]" ? form : null;
        },
    };
    vm.runInNewContext(
        markedSource("/* DRIVER_SHIFT_CLOSE_OUTBOX_START */", "/* DRIVER_SHIFT_CLOSE_OUTBOX_END */"),
        {window, document, navigator: {onLine: false}, Date, JSON, Promise, Error}
    );
    return {window, body, form, localStorage, nativeCalls, toasts, listeners};
}

test("offline driver shift close is persisted before network and leaves a calm local closed state", async () => {
    const runtime = createRuntime({networkResult: false});

    const accepted = await runtime.window.DriverShiftCloseOutbox.submit(runtime.form);

    assert.equal(accepted, true);
    assert.equal(runtime.nativeCalls[0].method, "queue");
    assert.equal(runtime.nativeCalls[1].method, "network");
    assert.equal(runtime.nativeCalls.some((call) => call.method === "ack"), false);
    assert.equal(runtime.form.classList.contains("is-sync-pending"), true);
    assert.equal(runtime.form.panel.hidden, false);
    assert.equal(runtime.body.dataset.driverShiftClosePending, "true");
    assert.equal(runtime.form.controls.every((control) => control.disabled), true);
    assert.match(runtime.localStorage.getItem("driver-shift-close-pending:v1"), /driver-close-offline-1/);
    assert.deepEqual(runtime.toasts, []);
});

test("server-confirmed close clears both browser and native outboxes", async () => {
    const runtime = createRuntime({networkResult: true});

    const accepted = await runtime.window.DriverShiftCloseOutbox.submit(runtime.form);

    assert.equal(accepted, true);
    assert.equal(runtime.localStorage.getItem("driver-shift-close-pending:v1"), null);
    assert.equal(runtime.nativeCalls.at(-1).method, "ack");
    assert.equal(runtime.nativeCalls.at(-1).clientActionId, "driver-close-offline-1");
    assert.equal(runtime.form.classList.contains("is-sync-pending"), false);
});

test("Driver never navigates the WebView to an HTTP error fallback", () => {
    const start = template.indexOf("window.submitDriverFormInPlace = function (form, options)");
    const end = template.indexOf("/* DRIVER_SHIFT_CLOSE_OUTBOX_START */", start);
    const submitSource = template.slice(start, end);

    assert.doesNotMatch(submitSource, /form\.submit\s*\(/);
    assert.match(submitSource, /return Promise\.resolve\(false\)/);
    assert.match(template, /position:\s*fixed;[\s\S]*?top:\s*50%;[\s\S]*?transform:\s*translate\(-50%, -50%\)/);
    assert.match(template, /Смена закрыта на телефоне/);
    assert.match(template, /name="shift_id" value="\{\{ open_shift\.id \}\}"/);
});
