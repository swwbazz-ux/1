"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const backendRoot = path.resolve(__dirname, "../../..");
const shiftActionTemplate = fs.readFileSync(
    path.join(backendRoot, "templates/includes/dispatcher_shift_action.html"),
    "utf8",
);
const dialogSource = fs.readFileSync(
    path.join(backendRoot, "templates/includes/tablet-runtime/shared-shift-login.js"),
    "utf8",
);
const realtimeSource = fs.readFileSync(
    path.join(backendRoot, "static/js/dispatcher-realtime-v1.js"),
    "utf8",
);

test("shared shift dialog exposes an idempotent rebind entrypoint", () => {
    assert.match(shiftActionTemplate, /includes\/tablet-runtime\/shared-shift-login\.js/);
    assert.match(dialogSource, /global\.initSharedShiftLogin = initSharedShiftLogin/);
    assert.match(dialogSource, /dataset\.sharedShiftLoginBound/);
    assert.match(dialogSource, /querySelectorAll\("\[data-shared-shift-login-cancel\]"\)/);
    assert.match(dialogSource, /initSharedShiftLogin\(document\)/);
});

test("dispatcher realtime rebinds the shared shift dialog after DOM reconciliation", () => {
    assert.match(
        realtimeSource,
        /typeof hostWindow\.initSharedShiftLogin === "function"[\s\S]+hostWindow\.initSharedShiftLogin\(document\)/,
    );
});

test("tablet entry and next-shift phones use spaces and retain the fixed country code outside the input", () => {
    function phoneInput(tablet, value) {
        const listeners = {};
        return {
            value,
            dataset: {},
            hasAttribute(name) { return tablet && name === "data-tablet-phone"; },
            addEventListener(name, listener) {
                if (!listeners[name]) listeners[name] = [];
                listeners[name].push(listener);
            },
            type(value) {
                this.value = value;
                for (const listener of listeners.input || []) listener();
            },
            inputListenerCount() { return (listeners.input || []).length; },
        };
    }
    const tabletPhone = phoneInput(true, "+7 (999) 123-45-67");
    const dispatcherPhone = phoneInput(false, "89991234567");
    const document = {
        documentElement: {dataset: {}},
        addEventListener() {},
        querySelectorAll(selector) {
            return selector === "[data-shared-shift-phone]" ? [tabletPhone, dispatcherPhone] : [];
        },
    };
    const window = {addEventListener() {}};
    vm.runInNewContext(dialogSource, {window, document});
    assert.equal(tabletPhone.value, "999 123 45 67");
    assert.equal(dispatcherPhone.value, "999-123-45-67");
    for (const [raw, formatted] of [
        ["", ""],
        ["9", "9"],
        ["9991", "999 1"],
        ["9991234", "999 123 4"],
        ["999123456", "999 123 45 6"],
        ["9991234567", "999 123 45 67"],
        ["+7 999 123 45 67", "999 123 45 67"],
        ["8 (999) 123-45-67", "999 123 45 67"],
    ]) {
        tabletPhone.type(raw);
        assert.equal(tabletPhone.value, formatted);
    }
    window.initSharedShiftLogin(document);
    assert.equal(tabletPhone.inputListenerCount(), 1);
    assert.equal(dispatcherPhone.inputListenerCount(), 1);
});

test("tablet keyboard Next validates the phone and Done sends one validated submission", () => {
    const handlers = {};
    function listen(name, handler) {
        const previous = handlers[name];
        handlers[name] = (event) => {
            if (previous) previous(event);
            handler(event);
        };
    }
    const actions = [];
    let posts = 0;
    const document = {
        documentElement: {dataset: {}},
        addEventListener: listen,
        querySelectorAll(selector) { return selector === "form[data-tablet-shift-entry]" ? [form] : []; },
    };
    const window = {
        addEventListener: listen,
        Capacitor: {Plugins: {NativeKeyboard: {setAction({action}) {
            actions.push(action);
            return Promise.resolve();
        }}}},
    };
    const submit = {click() {
        if (!phone.valid || !pin.valid) return;
        const event = {target: form, preventDefault() { this.defaultPrevented = true; }};
        handlers.submit(event);
        if (!event.defaultPrevented) posts++;
    }};
    const form = {
        dataset: {},
        matches(selector) { return selector === "form[data-tablet-shift-entry]"; },
        querySelector(selector) { return selector.startsWith("button") ? submit : pin; },
    };
    function newField(action) {
        return {
            form,
            valid: false,
            getAttribute() { return action; },
            reportValidity() { return this.valid; },
            focus() { document.activeElement = this; handlers.focusin({target: this}); },
        };
    }
    const phone = newField("next");
    const pin = newField("done");
    vm.runInNewContext(dialogSource, {window, document});
    phone.focus();
    handlers["native-ime-action"]({detail: {action: "next"}});
    assert.equal(document.activeElement, phone);
    assert.deepEqual(actions, ["next", "next"]);
    phone.valid = true;
    handlers.keydown({target: phone, key: "Enter", preventDefault() {}});
    assert.equal(document.activeElement, pin);
    pin.valid = true;
    handlers["native-ime-action"]({detail: {action: "next"}});
    assert.equal(posts, 0, "late Next event must not submit");
    handlers["native-ime-action"]({detail: {action: "done"}});
    submit.click();
    assert.equal(posts, 1, "Done and a repeated tap submit only once");
});
