"use strict";

const assert = require("node:assert/strict");
const {driverScreenSource} = require("./driver-screen-source");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const template = driverScreenSource();
const nativePlugin = fs.readFileSync(
    path.resolve(__dirname, "../../../../../mobile/capacitor-shell/android/app/src/main/java/ru/copperresources/mobile/BackgroundConnectionPlugin.java"),
    "utf8"
);
const nativeService = fs.readFileSync(
    path.resolve(__dirname, "../../../../../mobile/capacitor-shell/android/app/src/main/java/ru/copperresources/mobile/ConnectivityForegroundService.java"),
    "utf8"
);
const nativePending = fs.readFileSync(
    path.resolve(__dirname, "../../../../../mobile/capacitor-shell/android/app/src/main/java/ru/copperresources/mobile/PendingDriverShiftClose.java"),
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
    constructor(initialValue = null) {
        this.values = new Map();
        if (initialValue) this.values.set("driver-shift-close-pending:v1", initialValue);
    }
    getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
    setItem(key, value) { this.values.set(key, String(value)); }
    removeItem(key) { this.values.delete(key); }
}

function classList() {
    const values = new Set();
    return {
        add(...names) { names.forEach((name) => values.add(name)); },
        remove(...names) { names.forEach((name) => values.delete(name)); },
        contains(name) { return values.has(name); },
    };
}

function node(tagName = "div") {
    const children = [];
    return {
        tagName: tagName.toUpperCase(),
        dataset: {},
        classList: classList(),
        hidden: false,
        disabled: false,
        textContent: "",
        children,
        appendChild(child) { children.push(child); return child; },
        removeChild(child) { children.splice(children.indexOf(child), 1); },
        get firstChild() { return children[0] || null; },
        focus() { this.focused = true; },
    };
}

function createForm() {
    const field = (value) => Object.assign(node("input"), {
        value,
        listeners: new Map(),
        addEventListener(name, callback) { this.listeners.set(name, callback); },
    });
    const fields = new Map([
        ["client_action_id", field("driver-close-17")],
        ["shift_id", field("17")],
        ["end_fuel", field("90")],
        ["end_mileage", field("2600")],
        ["end_engine_hours", field("712")],
        ["reading_confirmation_token", field("")],
    ]);
    const panel = Object.assign(node(), {hidden: true});
    const closeLabel = node("span");
    const closeButton = Object.assign(node("button"), {
        querySelector(selector) {
            return selector === "[data-mobile-shift-label]" ? closeLabel : null;
        },
    });
    const modal = Object.assign(node(), {hidden: true});
    const title = node("h2");
    const message = node("p");
    const warningList = node("ul");
    const back = node("button");
    const accept = node("button");
    const modalNodes = new Map([
        ["[data-driver-reading-confirmation-title]", title],
        ["[data-driver-reading-confirmation-message]", message],
        ["[data-driver-reading-confirmation-warnings]", warningList],
        ["[data-driver-reading-confirmation-back]", back],
        ["[data-driver-reading-confirmation-accept]", accept],
    ]);
    modal.querySelector = (selector) => modalNodes.get(selector) || null;
    const controls = [...fields.values(), closeButton, back, accept];
    return {
        dataset: {nativeShiftId: "17", driverInPlacePending: "false"},
        classList: classList(),
        isConnected: true,
        getAttribute(name) { return name === "action" ? "/driver/shift/close/" : null; },
        querySelector(selector) {
            const match = selector.match(/^\[name=['"]([^'"]+)['"]\]$/);
            if (match) return fields.get(match[1]) || null;
            if (selector === "[data-driver-shift-sync-pending]") return panel;
            if (selector === "[data-driver-shift-close-button]") return closeButton;
            if (selector === "[data-driver-reading-confirmation]") return modal;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "input, button, select, textarea") return controls;
            if (selector.includes("end_fuel")) {
                return [fields.get("end_fuel"), fields.get("end_mileage"), fields.get("end_engine_hours")];
            }
            return [];
        },
        panel,
        fields,
        controls,
        modal,
        warningList,
        back,
        accept,
    };
}

function jsonResponse(status, data, extra = {}) {
    return {
        status,
        ok: status >= 200 && status < 300,
        text() { return Promise.resolve(JSON.stringify(data)); },
        ...extra,
    };
}

function createRuntime({responses = [], initialStorage = null, nativeState = null, online = true, nativeAvailable = true, authGeneration = "auth-1"} = {}) {
    const localStorage = new MemoryStorage(initialStorage);
    const nativeCalls = [];
    const fetchCalls = [];
    const toasts = [];
    const listeners = new Map();
    const timers = [];
    const form = createForm();
    const shell = {dataset: {driverAuthGeneration: authGeneration}};
    const body = {dataset: {}, classList: classList()};
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
            return Promise.resolve({pendingDriverShiftClose: nativeState});
        },
    };
    class FakeFormData {
        constructor(submittedForm) {
            this.values = Object.fromEntries(
                [...submittedForm.fields].map(([name, input]) => [name, String(input.value || "")])
            );
        }
        set(name, value) { this.values[name] = String(value); }
    }
    const location = {
        origin: "https://driverform.ru",
        href: "/driver/?tab=shift",
        assigned: [],
        assign(target) { this.assigned.push(target); },
        reload() { this.reloaded = true; },
    };
    const window = {
        localStorage,
        NativeBackgroundConnection: nativeAvailable ? native : null,
        FormData: FakeFormData,
        location,
        fetch(url, options) {
            fetchCalls.push({url, options, values: {...options.body.values}});
            const response = responses.shift();
            if (response instanceof Error) return Promise.reject(response);
            return Promise.resolve(response || jsonResponse(200, {ok: true, redirect_url: "/driver/?tab=manifest"}));
        },
        showDriverToast(text, tone) { toasts.push({text, tone}); },
        addEventListener(name, callback) { listeners.set(name, callback); },
        setTimeout(callback, delay) { timers.push({callback, delay}); return timers.length; },
        clearTimeout() {},
    };
    const document = {
        body,
        createElement(tagName) { return node(tagName); },
        querySelector(selector) {
            if (selector === "[data-driver-shell]") return shell;
            return selector === "[data-driver-shift-close-form]" ? form : null;
        },
    };
    vm.runInNewContext(
        markedSource("/* DRIVER_SHIFT_CLOSE_OUTBOX_START */", "/* DRIVER_SHIFT_CLOSE_OUTBOX_END */"),
        {window, document, navigator: {onLine: online}, Date, JSON, Promise, Error, Object, Array, Number, Math, URL}
    );
    return {window, body, form, localStorage, nativeCalls, fetchCalls, toasts, listeners, timers};
}

const warningResponse = () => jsonResponse(422, {
    ok: false,
    confirmation_required: true,
    confirmation_token: "signed-token:1:abc",
    has_active_shift: true,
    error: "Проверьте подозрительные показания.",
    warnings: [{
        code: "mileage_delta_high",
        field: "end_mileage",
        title: "Пробег больше 250 км",
        message: "На начало 2 500 км; введено 2 900 км; разница 400 км.",
    }],
});

test("HTTP 422 shows the warning and never pretends that the network is lost", async () => {
    const runtime = createRuntime({responses: [warningResponse()]});

    const accepted = await runtime.window.DriverShiftCloseOutbox.submit(runtime.form);

    assert.equal(accepted, true);
    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.nativeCalls.some((call) => call.method === "queue"), false);
    assert.equal(runtime.form.classList.contains("is-sync-pending"), false);
    assert.equal(runtime.form.panel.hidden, true);
    assert.equal(runtime.form.modal.hidden, false);
    assert.equal(runtime.form.accept.hidden, false);
    assert.equal(runtime.form.warningList.children.length, 1);
    assert.equal(runtime.form.warningList.children[0].dataset.warningCode, "mileage_delta_high");
    assert.equal(runtime.form.fields.get("end_mileage").value, "2600");
    assert.match(runtime.localStorage.getItem("driver-shift-close-pending:v1"), /signed-token/);
});

test("a genuine fetch failure queues the exact readings for native background delivery", async () => {
    const runtime = createRuntime({responses: [new TypeError("Failed to fetch")]});

    const accepted = await runtime.window.DriverShiftCloseOutbox.submit(runtime.form);

    assert.equal(accepted, true);
    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.nativeCalls.filter((call) => call.method === "queue").length, 1);
    assert.equal(runtime.form.classList.contains("is-sync-pending"), true);
    assert.equal(runtime.form.panel.hidden, false);
    assert.equal(runtime.body.dataset.driverShiftClosePending, "true");
    assert.match(runtime.localStorage.getItem("driver-shift-close-pending:v1"), /driver-close-17/);
    const stored = JSON.parse(runtime.localStorage.getItem("driver-shift-close-pending:v1"));
    assert.equal(runtime.fetchCalls[0].values.occurred_at, new Date(stored.createdAt).toISOString());
    const queued = runtime.nativeCalls.find((call) => call.method === "queue").payload;
    assert.equal(queued.createdAt, stored.createdAt);
    assert.equal(queued.state, "retry");
    assert.equal(queued.retryAttempts, 1);
});

test("HTTP 5xx remains retryable across restart and drains after bounded backoff", async () => {
    const first = createRuntime({responses: [jsonResponse(503, {ok: false, error: "temporary"})], nativeAvailable: false});

    await first.window.DriverShiftCloseOutbox.submit(first.form);

    const queued = JSON.parse(first.localStorage.getItem("driver-shift-close-pending:v1"));
    assert.equal(queued.state, "retry");
    assert.equal(queued.requiresAttention, false);
    assert.equal(queued.retryAttempts, 1);
    assert.ok(queued.nextAttemptAt > queued.createdAt);
    assert.ok(queued.nextAttemptAt - queued.createdAt <= 60000);
    queued.nextAttemptAt = 0;

    const restarted = createRuntime({
        initialStorage: JSON.stringify(queued),
        responses: [jsonResponse(200, {ok: true, status: "applied"})],
        nativeAvailable: false,
    });
    await restarted.window.DriverShiftCloseOutbox.restore(restarted.form);

    assert.equal(restarted.fetchCalls.length, 1);
    assert.equal(restarted.localStorage.getItem("driver-shift-close-pending:v1"), null);
});

test("401 waits for a new authenticated generation and resumes after login", async () => {
    const first = createRuntime({responses: [jsonResponse(401, {ok: false})], nativeAvailable: false, authGeneration: "auth-old"});
    await first.window.DriverShiftCloseOutbox.submit(first.form);
    const stored = first.localStorage.getItem("driver-shift-close-pending:v1");
    const blocked = JSON.parse(stored);
    assert.equal(blocked.state, "auth_required");
    assert.equal(blocked.requiresAttention, false);
    assert.equal(blocked.blockedAuthGeneration, "auth-old");

    const sameSession = createRuntime({initialStorage: stored, nativeAvailable: false, authGeneration: "auth-old"});
    await sameSession.window.DriverShiftCloseOutbox.restore(sameSession.form);
    assert.equal(sameSession.fetchCalls.length, 0);

    const freshSession = createRuntime({
        initialStorage: stored,
        responses: [jsonResponse(200, {ok: true, status: "already_applied"})],
        nativeAvailable: false,
        authGeneration: "auth-new",
    });
    await freshSession.window.DriverShiftCloseOutbox.restore(freshSession.form);
    assert.equal(freshSession.fetchCalls.length, 1);
    assert.equal(freshSession.localStorage.getItem("driver-shift-close-pending:v1"), null);
});

test("403 and login redirects are classified as authentication, not validation", async () => {
    for (const response of [
        jsonResponse(403, {ok: false}),
        jsonResponse(200, {}, {redirected: true, url: "https://driverform.ru/"}),
    ]) {
        const runtime = createRuntime({responses: [response], nativeAvailable: false});
        await runtime.window.DriverShiftCloseOutbox.submit(runtime.form);
        const stored = JSON.parse(runtime.localStorage.getItem("driver-shift-close-pending:v1"));
        assert.equal(stored.state, "auth_required");
        assert.equal(runtime.form.modal.hidden, true);
    }
});

test("web retry preserves the original offline shift-close occurred_at", async () => {
    const createdAt = Date.parse("2026-09-13T04:05:06.789Z");
    const pending = JSON.stringify({
        shiftId: "17",
        clientActionId: "driver-close-old",
        endFuel: "90",
        endMileage: "2600",
        endEngineHours: "712",
        confirmationToken: "",
        state: "queued",
        createdAt,
    });
    const runtime = createRuntime({initialStorage: pending, nativeState: null, online: true, nativeAvailable: false});

    await runtime.window.DriverShiftCloseOutbox.restore(runtime.form);

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.fetchCalls[0].values.occurred_at, "2026-09-13T04:05:06.789Z");
});

test("confirmation sends the signed token and clears both outboxes", async () => {
    const runtime = createRuntime({responses: [warningResponse(), jsonResponse(200, {
        ok: true,
        status: "applied",
        redirect_url: "/driver/?tab=manifest",
    })]});
    await runtime.window.DriverShiftCloseOutbox.submit(runtime.form);

    const confirmed = await runtime.form.accept.onclick();

    assert.equal(confirmed, true);
    assert.equal(runtime.fetchCalls.length, 2);
    assert.equal(runtime.fetchCalls[1].values.reading_confirmation_token, "signed-token:1:abc");
    assert.equal(runtime.nativeCalls.some((call) => call.method === "queue"), false);
    assert.deepEqual(runtime.window.location.assigned, ["/driver/?tab=manifest"]);
    assert.equal(runtime.localStorage.getItem("driver-shift-close-pending:v1"), null);
});

test("editing any reading invalidates the old confirmation and restores the form", async () => {
    const runtime = createRuntime({responses: [warningResponse()]});
    await runtime.window.DriverShiftCloseOutbox.submit(runtime.form);
    runtime.window.DriverShiftCloseOutbox.bindInvalidation(runtime.form);
    runtime.form.fields.get("reading_confirmation_token").value = "signed-token:1:abc";

    runtime.form.fields.get("end_fuel").listeners.get("input")();

    assert.equal(runtime.form.fields.get("reading_confirmation_token").value, "");
    assert.equal(runtime.form.modal.hidden, true);
    assert.equal(runtime.localStorage.getItem("driver-shift-close-pending:v1"), null);
});

test("a warning survives app restart without retrying the rejected request", async () => {
    const first = createRuntime({responses: [warningResponse()]});
    await first.window.DriverShiftCloseOutbox.submit(first.form);
    const stored = first.localStorage.getItem("driver-shift-close-pending:v1");
    const restarted = createRuntime({initialStorage: stored, nativeState: null});

    const restored = await restarted.window.DriverShiftCloseOutbox.restore(restarted.form);

    assert.equal(restored, true);
    assert.equal(restarted.fetchCalls.length, 0);
    assert.equal(restarted.nativeCalls.some((call) => call.method === "queue"), false);
    assert.equal(restarted.form.modal.hidden, false);
    assert.equal(restarted.form.fields.get("reading_confirmation_token").value, "signed-token:1:abc");
});

test("hard HTTP validation stays editable and never enters the offline queue", async () => {
    const runtime = createRuntime({responses: [jsonResponse(422, {
        ok: false,
        confirmation_required: false,
        has_active_shift: true,
        error: "Укажите топливо.",
        field_errors: {end_fuel: ["Обязательное поле."]},
    })]});

    await runtime.window.DriverShiftCloseOutbox.submit(runtime.form);

    assert.equal(runtime.form.accept.hidden, true);
    assert.equal(runtime.form.modal.hidden, false);
    assert.equal(runtime.form.back.textContent, "Отменить отправку и вернуться к вводу");
    assert.equal(runtime.form.controls.every((control) => control.disabled === false), true);
    assert.equal(runtime.nativeCalls.some((call) => call.method === "queue"), false);
    assert.equal(runtime.form.classList.contains("is-sync-pending"), false);
});

test("Driver never navigates the WebView to a raw HTTP error page", () => {
    const start = template.indexOf("window.submitDriverFormInPlace = function (form, options)");
    const end = template.indexOf("/* DRIVER_SHIFT_CLOSE_OUTBOX_START */", start);
    const submitSource = template.slice(start, end);

    assert.doesNotMatch(submitSource, /form\.submit\s*\(/);
    assert.match(submitSource, /return Promise\.resolve\(false\)/);
    assert.match(template, /confirmation_required/);
    assert.match(template, /reading_confirmation_token/);
    assert.match(template, /Вернуться и проверить/);
    assert.match(template, /Всё верно — закрыть смену/);
});

test("native foreground close posts the persisted createdAt as occurred_at", () => {
    assert.match(nativePlugin, /numericLong\(call\.getData\(\)\.opt\("createdAt"\)\)/);
    assert.match(nativeService, /formField\("occurred_at", PendingDriverShiftClose\.occurredAtIso\(pending\.createdAt\)\)/);
    assert.match(nativeService, /statusCode == 401 \|\| result\.statusCode == 403/);
    assert.match(nativeService, /PendingDriverShiftClose\.markAuthRequired/);
    assert.match(nativeService, /PendingDriverShiftClose\.markRetry/);
    assert.match(nativePending, /STATE_AUTH_REQUIRED/);
    assert.match(nativePending, /next_attempt_at/);
    assert.match(nativePlugin, /resumeAfterAuthentication\(getContext\(\), authGeneration\)/);
});
