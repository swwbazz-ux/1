"use strict";
/* T-024, 13.11.2026. Живой тест смены (T-020-live-shift-1): кнопка «Закрыть
   смену» доступна, удержание срабатывает, но end_fuel/end_mileage/
   end_engine_hours пустые и required (users/forms.py:1175-1176) — браузер
   молча не отправлял форму, очередь не создавалась, подпись «ДЕРЖИТЕ»
   залипала. Сервер считает показания необязательными (core/offline_sync.py),
   с показаниями начала офлайн-закрытие проходит (T-020-live-shift-2).
   Здесь проверяется настоящее поведение экрана: bindDriverShiftControls +
   bindDriverShiftHoldAction — предзаполнение полей значениями начала и явный
   отказ вместо молчаливого. */
const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("node:vm");
const {driverScreenSource} = require("./driver-screen-source");

const SCREEN = driverScreenSource();

function block(source, signature) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, signature + " not found");
    let depth = 0;
    let quote = "";
    let escaped = false;
    let lineComment = false;
    let blockComment = false;
    for (let index = source.indexOf("{", start); index < source.length; index += 1) {
        const ch = source[index];
        const next = source[index + 1] || "";
        if (lineComment) { if (ch === "\n") lineComment = false; continue; }
        if (blockComment) { if (ch === "*" && next === "/") { blockComment = false; index += 1; } continue; }
        if (quote) {
            if (escaped) escaped = false;
            else if (ch === "\\") escaped = true;
            else if (ch === quote) quote = "";
            continue;
        }
        if (ch === "/" && next === "/") { lineComment = true; index += 1; continue; }
        if (ch === "/" && next === "*") { blockComment = true; index += 1; continue; }
        if (ch === "'" || ch === '"' || ch === "`") { quote = ch; continue; }
        if (ch === "{") depth += 1;
        if (ch === "}") { depth -= 1; if (depth === 0) return source.slice(start, index + 1); }
    }
    throw new Error("unterminated " + signature);
}

function objectVar(source, name) {
    const start = source.indexOf("var " + name + " = {");
    assert.notEqual(start, -1, "var " + name + " not found");
    const end = source.indexOf("\n};", start);
    assert.notEqual(end, -1, "var " + name + " is not closed");
    return source.slice(start, end + 3);
}

function classList() {
    const values = new Set();
    return {
        add(...names) { names.forEach((name) => values.add(name)); },
        remove(...names) { names.forEach((name) => values.delete(name)); },
        contains(name) { return values.has(name); },
    };
}

class FakeEvent {
    constructor(type, init) {
        this.type = type;
        this.bubbles = Boolean(init && init.bubbles);
    }
}

/* Поле ввода с настоящим аксессором value: код подставляет значение через
   дескриптор прототипа (как браузер) и шлёт событие input. */
class FakeInput {
    constructor(name) {
        this.name = name;
        this.tagName = "INPUT";
        this.validity = {badInput: false};
        this.forcedInvalid = false;
        this.events = [];
        this.listeners = [];
    }
    get value() { return this._value === undefined ? "" : this._value; }
    set value(next) { this._value = String(next === null || next === undefined ? "" : next); }
    checkValidity() { return this.value.trim() !== "" && !this.forcedInvalid; }
    addEventListener(type, callback) { this.listeners.push({type, callback}); }
    dispatchEvent(event) {
        this.events.push(event.type);
        this.listeners.filter((entry) => entry.type === event.type)
            .forEach((entry) => entry.callback(event));
        return true;
    }
    focus(options) { this.focusedWith = options || true; }
}

function screenFixture(options) {
    options = options || {};
    const starts = options.starts || {
        start_fuel: "400",
        start_mileage: "100000",
        start_engine_hours: "5000",
    };
    const inputs = ["end_fuel", "end_mileage", "end_engine_hours"].map((name) => new FakeInput(name));
    const startNodes = Object.keys(starts).map((name) => ({
        dataset: {driverShiftStart: name},
        textContent: String(starts[name]),
    }));
    const label = {textContent: "Закрыть смену"};
    const button = {
        dataset: {},
        disabled: false,
        classList: classList(),
        querySelector(selector) { return selector === "[data-mobile-shift-label]" ? label : null; },
    };
    const submitListeners = [];
    const form = {
        dataset: {},
        classList: classList(),
        submitCount: 0,
        querySelector(selector) {
            const named = /^\[name="([^"]+)"\]$/.exec(selector);
            if (named) return inputs.find((input) => input.name === named[1]) || null;
            const start = /^\[data-driver-shift-start="([^"]+)"\]$/.exec(selector);
            if (start) return startNodes.find((node) => node.dataset.driverShiftStart === start[1]) || null;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-driver-shift-start]") return startNodes;
            if (selector.indexOf("end_fuel") >= 0) return inputs;
            return [];
        },
        addEventListener(type, callback) {
            if (type === "submit") submitListeners.push(callback);
        },
        requestSubmit(submitter) {
            this.submitCount += 1;
            this.lastSubmitter = submitter;
            const event = {
                type: "submit",
                defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; },
            };
            submitListeners.forEach((listener) => listener(event));
            return event;
        },
    };
    const shell = {
        querySelector(selector) {
            if (selector === "[data-driver-shift-close-form]") return form;
            if (selector === "[data-driver-shift-close-button]") return button;
            return null;
        },
        querySelectorAll() { return []; },
    };
    return {form, shell, button, label, inputs, startNodes};
}

function runtime(options) {
    const fixture = screenFixture(options);
    const toasts = [];
    let holdOptions = null;
    let resets = 0;
    const document = {
        readyState: "complete",
        body: {dataset: {}},
        addEventListener() {},
        querySelector() { return null; },
    };
    const windowObject = {
        HTMLElement: function () {},
        HTMLInputElement: FakeInput,
        Event: FakeEvent,
        showDriverToast(message) { toasts.push(message); },
        isAppRoleReadonly() { return false; },
        MobileShiftHold: {
            bind(target, bindOptions) {
                holdOptions = bindOptions;
                target.dataset.mobileShiftHoldBound = "true";
                return {
                    reset() {
                        resets += 1;
                        const label = target.querySelector("[data-mobile-shift-label]");
                        if (label) label.textContent = bindOptions.readyLabel;
                    },
                    setLabel(text) {
                        const label = target.querySelector("[data-mobile-shift-label]");
                        if (label) label.textContent = text;
                    },
                };
            },
        },
    };
    windowObject.window = windowObject;
    windowObject.document = document;
    const context = {
        window: windowObject,
        document,
        shell: fixture.shell,
        Object,
        String,
        Number,
        Math,
        Date,
        Array,
        JSON,
        Promise,
        setTimeout,
        globalThis: windowObject,
    };
    vm.runInNewContext(
        [
            objectVar(SCREEN, "DRIVER_SHIFT_END_READING_LABELS"),
            block(SCREEN, "function driverShiftEndReadingInputs(form)"),
            block(SCREEN, "function driverShiftStartReading(form, name)"),
            block(SCREEN, "function setDriverShiftReadingValue(input, value)"),
            block(SCREEN, "function prefillDriverShiftCloseReadings(form)"),
            block(SCREEN, "function driverShiftCloseReadingProblems(form)"),
            block(SCREEN, "function reportDriverShiftCloseReadings(form)"),
            block(SCREEN, "function bindDriverShiftHoldAction(form, button, options)"),
            block(SCREEN, "function driverRoleIsReadonly()"),
            block(SCREEN, "function bindDriverShiftControls()"),
            "bindDriverShiftControls();",
        ].join("\n"),
        context,
        {filename: "driver-shift-close-readings-runtime"}
    );
    const handle = {
        reset() { resets += 1; },
    };
    return {
        ...fixture,
        toasts,
        context,
        handle,
        resets: () => resets,
        holdOptions: () => holdOptions,
        completeHold() {
            assert.ok(holdOptions, "hold action is not bound");
            holdOptions.onComplete(handle);
        },
        shortPress() {
            assert.ok(holdOptions, "hold action is not bound");
            holdOptions.onShortPress();
        },
    };
}

test("пустая форма закрытия при открытии получает показания начала и пропускает checkValidity", () => {
    const run = runtime();
    assert.deepEqual(run.inputs.map((input) => input.value), ["400", "100000", "5000"]);
    // Каждое поле получило событие input — экран узнал об изменении, как от ввода.
    run.inputs.forEach((input) => assert.deepEqual(input.events, ["input"]));
    assert.equal(run.inputs.every((input) => input.checkValidity()), true);
    assert.equal(run.form.dataset.driverShiftHoldComplete, undefined);
    assert.equal(run.form.submitCount, 0);
});

test("предзаполнение не трогает введённое водителем и добирает пустое поле", () => {
    const run = runtime();
    run.inputs[0].value = "410";
    run.inputs[1].value = "";
    run.context.bindDriverShiftControls();
    assert.deepEqual(run.inputs.map((input) => input.value), ["410", "100000", "5000"]);
});

test("очищенное поле: удержание не отправляет форму, а говорит вслух и сбрасывает «ДЕРЖИТЕ»", () => {
    const run = runtime();
    run.inputs[0].value = "";
    run.completeHold();
    assert.equal(run.form.submitCount, 0, "форма не должна уходить на молчаливую проверку браузера");
    assert.equal(run.toasts.length, 1);
    assert.equal(run.toasts[0], "Заполните для закрытия смены: топливо.");
    assert.equal(run.resets(), 1, "удержание сбрасывается, подпись не залипает");
    assert.equal(run.form.dataset.driverShiftHoldComplete, undefined);
    assert.equal(run.inputs[0].focusedWith && run.inputs[0].focusedWith.preventScroll, true);
});

test("неверное значение поля: сообщение «Проверьте показания», отправки нет", () => {
    const run = runtime();
    run.inputs[2].forcedInvalid = true;
    run.completeHold();
    assert.equal(run.form.submitCount, 0);
    assert.equal(run.toasts[0], "Проверьте показания: моточасы.");
    assert.equal(run.resets(), 1);
});

test("отпускание того же удержания не перекрывает сообщение об отказе", () => {
    const run = runtime();
    run.inputs[0].value = "";
    run.completeHold();
    run.shortPress();
    assert.deepEqual(run.toasts, ["Заполните для закрытия смены: топливо."]);
});

test("заполненные показания: удержание отправляет форму, кнопка уходит в «Закрываем смену»", () => {
    const run = runtime();
    run.completeHold();
    assert.equal(run.form.submitCount, 1);
    assert.equal(run.form.lastSubmitter, run.button);
    assert.equal(run.form.dataset.driverShiftHoldComplete, undefined, "признак удержания снимает обработчик отправки");
    assert.equal(run.button.disabled, true);
    assert.equal(run.label.textContent, "Закрываем смену");
    assert.deepEqual(run.toasts, []);
});

test("без значений начала форма остаётся пустой, но отказ остаётся явным", () => {
    const run = runtime({starts: {start_fuel: "—", start_mileage: "—", start_engine_hours: "—"}});
    assert.deepEqual(run.inputs.map((input) => input.value), ["", "", ""]);
    run.completeHold();
    assert.equal(run.form.submitCount, 0);
    assert.equal(run.toasts[0], "Заполните для закрытия смены: топливо, одометр, моточасы.");
});
