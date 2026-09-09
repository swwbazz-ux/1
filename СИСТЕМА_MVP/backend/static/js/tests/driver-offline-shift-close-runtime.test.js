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

function jsonResponse(status, data) {
    return {
        status,
        ok: status >= 200 && status < 300,
        text() { return Promise.resolve(JSON.stringify(data)); },
    };
}

function createRuntime({responses = [], initialStorage = null, nativeState = null, online = true} = {}) {
    const localStorage = new MemoryStorage(initialStorage);
    const nativeCalls = [];
    const fetchCalls = [];
    const toasts = [];
    const listeners = new Map();
    const form = createForm();
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
    }
    const location = {
        href: "/driver/?tab=shift",
        assigned: [],
        assign(target) { this.assigned.push(target); },
        reload() { this.reloaded = true; },
    };
    const window = {
        localStorage,
        NativeBackgroundConnection: native,
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
    };
    const document = {
        body,
        createElement(tagName) { return node(tagName); },
        querySelector(selector) {
            return selector === "[data-driver-shift-close-form]" ? form : null;
        },
    };
    vm.runInNewContext(
        markedSource("/* DRIVER_SHIFT_CLOSE_OUTBOX_START */", "/* DRIVER_SHIFT_CLOSE_OUTBOX_END */"),
        {window, document, navigator: {onLine: online}, Date, JSON, Promise, Error, Object, Array}
    );
    return {window, body, form, localStorage, nativeCalls, fetchCalls, toasts, listeners};
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
