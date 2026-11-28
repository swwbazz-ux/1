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
    /* Подписи «Начало» внутри формы закрытия: из них берётся предзаполнение
       (mobile_shift_screen.html:53,57,61). */
    const startNodes = ["start_fuel", "start_mileage", "start_engine_hours"].map((name) => ({
        dataset: {driverShiftStart: name},
        textContent: "—",
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
            const named = /^\[name=["']([^"']+)["']\]$/.exec(selector);
            if (named) return inputs.find((input) => input.name === named[1]) || null;
            const start = /^\[data-driver-shift-start=["']([^"']+)["']\]$/.exec(selector);
            if (start) return startNodes.find((node) => node.dataset.driverShiftStart === start[1]) || null;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-driver-shift-start]") return startNodes;
            return [];
        },
        closest(selector) { return selector === "[data-driver-shell]" ? getShell() : null; },
        contains(node) { return inputs.includes(node); },
    };
    return {form, inputs, button, startNodes};
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
    /* Настоящие показания закрытия смены: предзаполнение и снятие прошлых
       значений (driver-shift-close-v1.js). Ветка driver-local-shift-v1.js,
       которая их зовёт, иначе оставалась бы без теста (QA T-025, 8.4). */
    const readingHelpers = [
        block(SCREEN, "var DRIVER_SHIFT_END_READING_LABELS = {"),
        block(SCREEN, "function driverShiftEndReadingInputs(form)"),
        block(SCREEN, "function driverShiftStartReading(form, name)"),
        block(SCREEN, "function setDriverShiftReadingValue(input, value)"),
        block(SCREEN, "function prefillDriverShiftCloseReadings(form)"),
        block(SCREEN, "function clearDriverShiftCloseReadings(form)"),
        "window.prefillDriverShiftCloseReadings = prefillDriverShiftCloseReadings;",
        "window.clearDriverShiftCloseReadings = clearDriverShiftCloseReadings;",
    ].join("\n");
    vm.runInNewContext(
        `${HOLD_SOURCE}\n${holdAdapter}\n${openingBinding}\n${refreshGuard}\n${typingCheck}\n${readingHelpers}\n${LOCAL_SHIFT_SOURCE}`,
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
        project: () => windowObject.DriverLocalShift.project(shell),
        /* Как bindDriverShiftControls при загрузке страницы (driver-shift-v1.js). */
        prefillCloseReadings: () => windowObject.prefillDriverShiftCloseReadings(close.form),
        type: (values) => inputs.forEach((input, index) => {
            input.value = values[index];
            input.dispatch("input");
        }),
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

/* QA T-025, 8.2. Форма закрытия не пересоздаётся между сменами: после
   «закрыл → открыл» в ней оставались показания прошлой смены, а подписи
   «Начало» уже новые. Сервер ловит уменьшение одометра/моточасов, а
   устаревшее топливо прошло бы молча. Новая местная смена должна получить
   свои показания начала. */
test("second local shift in a row: close form shows its own start readings, not the previous end", async () => {
    const run = setup({withCloseForm: true});

    run.type(["400", "100000", "5000"]);
    run.completeHold();
    await run.form.lastOpen;
    assert.equal(run.close.form.hidden, false);
    assert.deepEqual(run.close.inputs.map((input) => input.value), ["400", "100000", "5000"]);

    run.close.inputs.forEach((input, index) => { input.value = ["380", "100120", "5010"][index]; });
    await run.closeShift();
    assert.equal(run.close.form.hidden, true);

    /* Вторая смена: водитель сам вписал новые показания при открытии. */
    run.type(["450", "100200", "5020"]);
    run.completeHold();
    await run.form.lastOpen;
    assert.equal(run.close.form.hidden, false);
    assert.deepEqual(
        run.close.startNodes.map((node) => node.textContent),
        ["450", "100200", "5020"],
        "подписи «Начало» новой смены"
    );
    assert.deepEqual(
        run.close.inputs.map((input) => input.value),
        ["450", "100200", "5020"],
        "поля закрытия получили показания начала новой смены, а не прошлой"
    );

    await run.closeShift();
    const lastClose = run.queued.filter((event) => event.event_type === "driver.shift.closed").pop();
    assert.equal(lastClose.payload.end_fuel, "450");
    assert.equal(lastClose.payload.end_mileage, "100200");
    assert.equal(lastClose.payload.end_engine_hours, "5020");
});

/* Требование карточки: не стирать ввод, пока форма закрытия текущей смены
   открыта/редактируется. Повторное применение состояния той же смены
   (фрагмент, подтверждение сервера) не трогает ни введённое, ни очищенное. */
test("close form keeps what the driver typed while the same shift is open", async () => {
    const run = setup({withCloseForm: true});

    run.type(["400", "100000", "5000"]);
    run.completeHold();
    await run.form.lastOpen;
    assert.deepEqual(run.close.inputs.map((input) => input.value), ["400", "100000", "5000"]);

    run.close.inputs[0].value = "470";
    run.close.inputs[1].value = "";
    run.project();
    run.project();
    assert.deepEqual(
        run.close.inputs.map((input) => input.value),
        ["470", "", "5000"],
        "повторное применение той же смены не затирает и не добирает поля"
    );
});

/* QA T-025, итерация 2 (блокер). Реальная цепочка на стенде: страница открыта
   с СЕРВЕРНОЙ сменой (21/19), водитель офлайн закрывает её, затем офлайн
   открывает новую смену с изменёнными показаниями начала. Форма закрытия
   серверной смены была предзаполнена при загрузке (bindDriverShiftControls),
   и первая местная смена обязана получить свои показания, а не серверные. */
test("server shift → offline close → local open: close form gets the new shift readings", async () => {
    const run = setup({withCloseForm: true});
    /* Разметка сервера: смена 19 открыта, подписи «Начало» — из неё. */
    run.shell.dataset.driverShiftId = "19";
    run.shell.dataset.driverServerShiftId = "19";
    const serverStarts = {start_fuel: "400", start_mileage: "100000", start_engine_hours: "5000"};
    run.close.startNodes.forEach((node) => {
        node.textContent = serverStarts[node.dataset.driverShiftStart];
    });
    run.project();
    run.prefillCloseReadings();
    assert.equal(run.shell.dataset.driverShiftId, "19");
    assert.deepEqual(run.close.inputs.map((input) => input.value), ["400", "100000", "5000"]);

    /* Офлайн-закрытие серверной смены 19 (показания те же, что на экране). */
    await run.closeShift();
    assert.equal(run.close.form.hidden, true);

    /* Новая местная смена: водитель изменил показания при открытии. */
    run.type(["450", "100200", "5020"]);
    run.completeHold();
    await run.form.lastOpen;

    assert.equal(run.close.form.hidden, false);
    assert.deepEqual(
        run.close.startNodes.map((node) => node.textContent),
        ["450", "100200", "5020"],
        "подписи «Начало» новой смены"
    );
    assert.deepEqual(
        run.close.inputs.map((input) => input.value),
        ["450", "100200", "5020"],
        "первая местная смена после серверной не оставляет показания серверной"
    );

    await run.closeShift();
    const lastClose = run.queued.filter((event) => event.event_type === "driver.shift.closed").pop();
    assert.equal(lastClose.payload.end_fuel, "450");
    assert.equal(lastClose.payload.end_mileage, "100200");
    assert.equal(lastClose.payload.end_engine_hours, "5020");
});
