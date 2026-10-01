"use strict";
/* Бой v371, Infinix владельца (30.09.2026): после «Начать смену» настоящим
   удержанием экран больше не обновлялся с сервера — в журнале каждую секунду
   driver-refresh-deferred … busy=opening_form, синий индикатор, пустая путёвка.
   Удержание ставит форме открытия признак отправки, ввод показаний — признак
   набора; раньше их снимала полная подмена экрана ответом сервера, а местное
   открытие экран не подменяет. На стенде не всплыло: форму отправляли
   requestSubmit() без удержания и заполняли поля без события input.
   Здесь — настоящий путь: bindDriverShiftOpeningForm + bindDriverShiftHoldAction
   (onComplete удержания), события input на полях, DriverLocalShift.open. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const {driverScreenSource} = require("./driver-screen-source");

const SCREEN = driverScreenSource();
const HOLD_SOURCE = fs.readFileSync(path.resolve(__dirname, "..", "mobile-shift-unified-v1.js"), "utf8");
const LOCAL_SHIFT_SOURCE = fs.readFileSync(path.resolve(__dirname, "..", "driver-local-shift-v1.js"), "utf8");

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

class ClassList {
    constructor() { this.values = new Set(); }
    add(...names) { names.forEach((name) => this.values.add(name)); }
    remove(...names) { names.forEach((name) => this.values.delete(name)); }
    toggle(name, on) { if (on) this.values.add(name); else this.values.delete(name); }
    contains(name) { return this.values.has(name); }
}

function closeFormFixture(getShell) {
    const inputs = ["end_fuel", "end_mileage", "end_engine_hours"].map((name) => ({
        name,
        value: "",
        tagName: "INPUT",
        addEventListener() {},
        checkValidity() { return this.value.trim() !== ""; },
    }));
    const label = {textContent: "Закрыть смену"};
    const button = {
        disabled: false,
        classList: new ClassList(),
        querySelector(selector) { return selector === "[data-mobile-shift-label]" ? label : null; },
    };
    const shiftInput = {value: ""};
    const form = {
        dataset: {driverLocalShiftForm: "close"},
        hidden: true,
        classList: new ClassList(),
        querySelector(selector) {
            if (selector === "[data-driver-shift-close-button]") return button;
            if (selector === 'input[name="shift_id"]') return shiftInput;
            return inputs.find((input) => selector === `[name="${input.name}"]`) || null;
        },
        querySelectorAll() { return []; },
        closest(selector) { return selector === "[data-driver-shell]" ? getShell() : null; },
        contains(node) { return inputs.includes(node); },
    };
    return {form, inputs, button};
}

function setup(options = {}) {
    const inputs = ["start_fuel", "start_mileage", "start_engine_hours"].map((name) => {
        const listeners = {};
        return {
            name,
            value: "",
            tagName: "INPUT",
            addEventListener(type, callback) { listeners[type] = callback; },
            dispatch(type) { if (listeners[type]) listeners[type]({type, target: this}); },
            checkValidity() { return this.value.trim() !== ""; },
        };
    });
    const label = {textContent: "Начать смену"};
    const button = {
        dataset: {},
        disabled: false,
        classList: new ClassList(),
        style: {setProperty() {}},
        addEventListener() {},
        getAttribute() { return null; },
        setAttribute() {},
        querySelector(selector) { return selector === "[data-mobile-shift-label]" ? label : null; },
    };
    const submitListeners = [];
    let shell;
    // Обе формы смены на одной странице — как в шаблоне: вторая скрыта.
    const close = options.withCloseForm ? closeFormFixture(() => shell) : null;
    const form = {
        dataset: {driverLocalShiftForm: "open"},
        hidden: false,
        querySelector(selector) {
            if (selector === "[data-driver-shift-open-button]") return button;
            const named = inputs.find((input) => selector === `[name="${input.name}"]`);
            return named || null;
        },
        querySelectorAll(selector) { return selector === "input[type='number']" ? inputs : []; },
        addEventListener(type, callback) { if (type === "submit") submitListeners.push(callback); },
        closest(selector) { return selector === "[data-driver-shell]" ? shell : null; },
        requestSubmit() {
            const event = {type: "submit", defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
            submitListeners.forEach((listener) => listener(event));
            return event;
        },
    };
    shell = {
        dataset: {
            driverAccessId: "7",
            driverShiftId: "",
            driverServerShiftId: "",
            driverPreparedTruckId: "58",
            driverCurrentTruckId: "58",
            driverShiftType: "day",
        },
        querySelector(selector) {
            if (selector === ".driver-shift-opening-form") return form;
            if (selector === '[data-driver-local-shift-form="open"]') return form;
            if (close && selector === '[data-driver-local-shift-form="close"]') return close.form;
            if (close && selector === "[data-driver-shift-close-form]") return close.form;
            if (selector === "[data-driver-shift-open-button]") return button;
            if (selector.includes(".is-pending") && button.classList.contains("is-pending")) return button;
            return null;
        },
        querySelectorAll() { return []; },
        contains(node) { return inputs.includes(node); },
    };
    const storage = new Map();
    const queued = [];
    const document = {
        readyState: "loading",
        addEventListener() {},
        documentElement: {dataset: {}, classList: new ClassList()},
        hidden: false,
        activeElement: null,
        body: {dataset: {operationalStateVersion: "10"}},
        querySelector(selector) { return selector === "[data-driver-shell]" ? shell : null; },
    };
    const windowObject = {
        AppRealtime: {wake() {}},
        showDriverToast() {},
        localStorage: {
            getItem: (key) => (storage.has(key) ? storage.get(key) : null),
            setItem: (key, value) => storage.set(key, String(value)),
            removeItem: (key) => storage.delete(key),
        },
        driverOfflineOutbox: {
            pending() { return Promise.resolve([]); },
            enqueue(spec) {
                queued.push(spec);
                return Promise.resolve(Object.assign({occurred_at: "2026-09-30T08:00:00.000Z"}, spec));
            },
        },
        setTimeout,
    };
    windowObject.window = windowObject;
    windowObject.document = document;
    const context = {window: windowObject, document, Promise, Array, Number, String, Object, JSON, Date, Math, setTimeout, globalThis: windowObject};
    const holdAdapter = block(SCREEN, "function bindDriverShiftHoldAction(form, button, options)");
    const openingBinding = block(SCREEN, "function bindDriverShiftOpeningForm(shell)");
    const refreshGuard = block(SCREEN, "function isDriverOperationalRefreshUnsafe(shell)");
    const typingCheck = block(SCREEN, "function driverIsTypingIntoForm(shell)");
    vm.runInNewContext(
        `${HOLD_SOURCE}\n${holdAdapter}\n${openingBinding}\n${refreshGuard}\n${typingCheck}\n${LOCAL_SHIFT_SOURCE}`,
        context,
        {filename: "driver-local-shift-hold-runtime"}
    );
    /* Удержание в браузере завершается вызовом onComplete, который передаёт
       адаптер bindDriverShiftHoldAction: берём его ровно оттуда. */
    let holdOptions = null;
    windowObject.MobileShiftHold.bind = (target, options) => {
        holdOptions = options;
        target.dataset.mobileShiftHoldBound = "true";
        return {};
    };
    // Обработчик отправки экрана водителя: местное открытие смены.
    submitListeners.push((event) => {
        if (event.defaultPrevented) return;
        form.lastOpen = windowObject.DriverLocalShift.open(form);
    });
    context.bindDriverShiftOpeningForm(shell);
    return {
        form, button, label, inputs, shell, queued, context, close,
        completeHold: () => holdOptions.onComplete(),
        closeShift: () => windowObject.DriverLocalShift.close(close.form),
    };
}

test("a real hold-and-open leaves no busy marks on the opening form, so server refreshes resume", async () => {
    const run = setup();
    run.inputs.forEach((input, index) => {
        input.value = ["410", "12000", "3000"][index];
        input.dispatch("input");
    });
    assert.equal(run.form.dataset.driverShiftOpeningDirty, "true");

    run.completeHold();

    assert.equal(run.form.dataset.driverShiftOpeningPending, "true");
    assert.equal(run.button.classList.contains("is-pending"), true);
    assert.equal(run.label.textContent, "Открываем смену");
    assert.equal(run.context.isDriverOperationalRefreshUnsafe(run.shell), true);

    await run.form.lastOpen;

    assert.equal(run.queued.length, 1);
    assert.equal(run.queued[0].event_type, "driver.shift.opened");
    assert.equal(run.shell.dataset.driverShiftOpen, "true");
    assert.equal(run.form.dataset.driverShiftOpeningPending, "false");
    assert.equal(run.form.dataset.driverShiftOpeningDirty, "false");
    assert.equal(run.form.dataset.driverInPlacePending, "false");
    assert.equal(run.button.classList.contains("is-pending"), false);
    assert.equal(run.label.textContent, "Начать смену");
    assert.equal(run.context.driverIsTypingIntoForm(run.shell), false);
    assert.equal(run.context.isDriverOperationalRefreshUnsafe(run.shell), false);
});

/* Бой v372, Infinix (30.09.2026): цикл без сети «открыть → рейс → закрыть →
   открыть новую → сеть» — очередь ушла, а экран снова каждую секунду
   откладывал обновление с busy=opening_form. На стенде не повторялось: там
   показания вводили до удержания. На телефоне клавиатура Android после
   удержания присылает поздний input уже в скрытую форму открытия, и сверка
   со старыми серверными показаниями ставила «в наборе» навсегда. Полный цикл
   на одной странице, обе формы смены — как в шаблоне. */
test("open → close → open on one page: a late keyboard input into the hidden opening form never holds the screen", async () => {
    const run = setup({withCloseForm: true});
    const type = (values) => run.inputs.forEach((input, index) => {
        input.value = values[index];
        input.dispatch("input");
    });

    type(["410", "12000", "3000"]);
    run.completeHold();
    await run.form.lastOpen;
    assert.equal(run.form.hidden, true);
    assert.equal(run.close.form.hidden, false);
    // Поздний input клавиатуры в уже скрытую форму открытия.
    run.inputs[0].value = "411";
    run.inputs[0].dispatch("input");
    assert.equal(run.form.dataset.driverShiftOpeningDirty, "false");
    assert.equal(run.context.isDriverOperationalRefreshUnsafe(run.shell), false);

    run.close.inputs.forEach((input, index) => { input.value = ["380", "12090", "3008"][index]; });
    await run.closeShift();
    assert.equal(run.form.hidden, false);
    assert.equal(run.close.form.hidden, true);
    assert.deepEqual(run.inputs.map((input) => input.value), ["380", "12090", "3008"]);
    // Показания подставил телефон, водитель их не менял: форма не «в наборе».
    run.inputs[1].dispatch("input");
    assert.equal(run.form.dataset.driverShiftOpeningDirty, "false");
    assert.equal(run.context.isDriverOperationalRefreshUnsafe(run.shell), false);

    run.completeHold();
    assert.equal(run.context.isDriverOperationalRefreshUnsafe(run.shell), true);
    await run.form.lastOpen;
    assert.equal(run.queued.map((event) => event.event_type).join(","), "driver.shift.opened,driver.shift.closed,driver.shift.opened");
    assert.equal(run.form.hidden, true);
    run.inputs[2].value = "3009";
    run.inputs[2].dispatch("input");
    assert.equal(run.form.dataset.driverShiftOpeningDirty, "false");
    assert.equal(run.form.dataset.driverShiftOpeningPending, "false");
    assert.equal(run.context.isDriverOperationalRefreshUnsafe(run.shell), false);
    assert.equal(run.context.driverIsTypingIntoForm(run.shell), false);
});

test("a hidden opening form still carrying stale busy marks does not hold the screen", () => {
    const run = setup({withCloseForm: true});
    run.form.hidden = true;
    run.form.dataset.driverShiftOpeningDirty = "true";
    run.form.dataset.driverShiftOpeningPending = "true";
    assert.equal(run.context.isDriverOperationalRefreshUnsafe(run.shell), false);
    assert.equal(run.context.driverIsTypingIntoForm(run.shell), false);
    run.form.hidden = false;
    assert.equal(run.context.isDriverOperationalRefreshUnsafe(run.shell), true);
});
