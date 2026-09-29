const test = require("node:test");
const assert = require("node:assert/strict");

/* Смена водителя открывается и закрывается на телефоне без сервера
   (владелец, 30.09.2026). Модуль решает, что показывать, пока сервер не
   догнал, и кладёт открытие/закрытие в общую очередь. */

function storage() {
    const map = new Map();
    return {
        getItem: key => (map.has(key) ? map.get(key) : null),
        setItem: (key, value) => map.set(key, String(value)),
        removeItem: key => map.delete(key),
    };
}

function node(attrs) {
    const el = {
        dataset: Object.assign({}, attrs && attrs.dataset),
        hidden: !!(attrs && attrs.hidden),
        disabled: false,
        value: attrs && attrs.value !== undefined ? attrs.value : "",
        textContent: "",
        children: (attrs && attrs.children) || [],
        classList: {
            items: new Set(),
            add(name) { this.items.add(name); },
            remove(...names) { names.forEach(name => this.items.delete(name)); },
            toggle(name, on) { if (on) this.items.add(name); else this.items.delete(name); },
            contains(name) { return this.items.has(name); },
        },
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = String(value); },
        removeAttribute(name) { delete this.attributes[name]; },
        matches: attrs && attrs.matches || (() => false),
        checkValidity: () => true,
        querySelectorAll(selector) {
            const found = [];
            (function walk(list) {
                list.forEach(child => {
                    if (child.matches(selector)) found.push(child);
                    walk(child.children);
                });
            })(this.children);
            return found;
        },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        closest(selector) { return selector === "[data-driver-shell]" ? el.shell || null : null; },
    };
    return el;
}

function matcher(test) { return selector => test(selector); }

function input(name, value) {
    return node({value, matches: matcher(selector => selector === `[name="${name}"]` || selector === "input[type='number']")});
}

function page({serverShiftId = "", preparedTruckId = "58", version = 10} = {}) {
    const openForm = node({
        dataset: {driverLocalShiftForm: "open"},
        matches: matcher(selector => selector === '[data-driver-local-shift-form="open"]'),
        children: [input("start_fuel", "410"), input("start_mileage", "12000"), input("start_engine_hours", "3000")],
    });
    const shiftIdInput = node({matches: matcher(selector => selector === 'input[name="shift_id"]')});
    const startFuel = node({dataset: {driverShiftStart: "start_fuel"}, matches: matcher(selector => selector === "[data-driver-shift-start]")});
    const closeForm = node({
        dataset: {driverLocalShiftForm: "close"},
        hidden: !serverShiftId,
        matches: matcher(selector => selector === '[data-driver-local-shift-form="close"]'),
        children: [shiftIdInput, startFuel, input("end_fuel", "300"), input("end_mileage", "12040"), input("end_engine_hours", "3008")],
    });
    openForm.hidden = !!serverShiftId;
    const gated = node({matches: matcher(selector => selector === "[data-driver-shift-gated]")});
    const shell = node({
        dataset: {
            driverAccessId: "7",
            driverShiftId: serverShiftId,
            driverServerShiftId: serverShiftId,
            driverCurrentTruckId: preparedTruckId,
            driverPreparedTruckId: serverShiftId ? "" : preparedTruckId,
            driverShiftType: "day",
        },
        children: [openForm, closeForm, gated],
    });
    [openForm, closeForm].forEach(form => { form.shell = shell; });
    const document = {
        body: {dataset: {operationalStateVersion: String(version)}},
        querySelector: selector => (selector === "[data-driver-shell]" ? shell : null),
    };
    return {shell, openForm, closeForm, gated, shiftIdInput, startFuel, document};
}

function load(document) {
    globalThis.window = globalThis;
    globalThis.document = document;
    delete require.cache[require.resolve("../driver-local-shift-v1.js")];
    return require("../driver-local-shift-v1.js");
}

function outbox() {
    const queued = [];
    let sequence = 0;
    return {
        queued,
        enqueue(spec) {
            sequence += 1;
            queued.push(spec);
            return Promise.resolve(Object.assign({occurred_at: "2026-09-30T08:00:0" + sequence + ".000Z", sequence}, spec));
        },
    };
}

test("decision: the phone's open or closed shift wins until the server has seen it", () => {
    const {decide} = load(page().document);
    assert.deepEqual(decide(null, {shiftId: "44", version: 5}), {open: true, shiftRef: "44", local: false, drop: false});
    const localOpen = {status: "open", local_shift_id: "driver-shift-open:a", server_shift_id: null};
    assert.deepEqual(decide(localOpen, {shiftId: "", version: 5}), {open: true, shiftRef: "driver-shift-open:a", local: true, drop: false});
    const confirmedOpen = Object.assign({}, localOpen, {server_shift_id: 45, open_confirmed_version: 20});
    assert.deepEqual(decide(confirmedOpen, {shiftId: "45", version: 21}), {open: true, shiftRef: "45", local: false, drop: false});
    // Старая оболочка из кэша (версия меньше) не может «закрыть» смену телефона.
    assert.deepEqual(decide(confirmedOpen, {shiftId: "", version: 3}), {open: true, shiftRef: "45", local: false, drop: false});
    // Сервер в более новой версии показывает другое (закрыл диспетчер) — сервер прав.
    assert.deepEqual(decide(confirmedOpen, {shiftId: "", version: 25}), {open: false, shiftRef: "", local: false, drop: true});
    const closedPending = {status: "closed", server_shift_id: 44, close_event_id: "driver-shift-close:b"};
    assert.deepEqual(decide(closedPending, {shiftId: "44", version: 30}), {open: false, shiftRef: "", local: false, drop: false});
    const closedConfirmed = Object.assign({}, closedPending, {close_confirmed_version: 31});
    assert.deepEqual(decide(closedConfirmed, {shiftId: "44", version: 12}), {open: false, shiftRef: "", local: false, drop: false});
    assert.deepEqual(decide(closedConfirmed, {shiftId: "46", version: 40}), {open: true, shiftRef: "46", local: false, drop: true});
});

test("opening a shift without a network switches the screen at once and queues driver.shift.opened", async () => {
    const view = page();
    const {createDriverLocalShift} = load(view.document);
    const box = outbox();
    const local = createDriverLocalShift({storage: storage(), outbox: box});
    local.project(view.shell);
    assert.equal(view.shell.dataset.driverShiftOpen, "false");
    assert.equal(view.gated.disabled, true);

    const event = await local.open(view.openForm);

    const spec = box.queued[0];
    assert.equal(spec.event_type, "driver.shift.opened");
    assert.equal(spec.local_shift_id, spec.event_id);
    assert.match(spec.event_id, /^driver-shift-open:/);
    assert.equal(spec.shift_id, null);
    assert.equal(spec.equipment_id, 58);
    assert.deepEqual(spec.depends_on, []);
    assert.equal(spec.payload.truck_id, 58);
    assert.equal(spec.payload.start_mileage, "12000");
    assert.equal(spec.payload.shift_type, "day");
    assert.equal(view.shell.dataset.driverShiftOpen, "true");
    assert.equal(view.shell.dataset.driverShiftId, event.event_id);
    assert.equal(view.shell.dataset.driverLocalShiftId, event.event_id);
    assert.equal(view.gated.disabled, false);
    assert.equal(view.openForm.hidden, true);
    assert.equal(view.closeForm.hidden, false);
    assert.equal(view.shiftIdInput.value, "");
    assert.equal(view.startFuel.textContent, "410");
});

test("closing a phone-only shift shows the next opening at once and the next opening waits for that close", async () => {
    const view = page();
    const {createDriverLocalShift} = load(view.document);
    const box = outbox();
    const local = createDriverLocalShift({storage: storage(), outbox: box});
    local.project(view.shell);
    const opening = await local.open(view.openForm);

    const closing = await local.close(view.closeForm);

    const closeSpec = box.queued[1];
    assert.equal(closeSpec.event_type, "driver.shift.closed");
    assert.equal(closeSpec.shift_id, null);
    assert.equal(closeSpec.local_shift_id, opening.event_id);
    assert.equal(closeSpec.payload.end_mileage, "12040");
    assert.equal(view.shell.dataset.driverShiftOpen, "false");
    assert.equal(view.shell.dataset.driverShiftId, "");
    assert.equal(view.closeForm.hidden, true);
    assert.equal(view.openForm.hidden, false);
    // Показания начала следующей — показания конца предыдущей.
    assert.equal(view.openForm.querySelector('[name="start_mileage"]').value, "12040");

    await local.open(view.openForm);
    assert.deepEqual(box.queued[2].depends_on, [closing.event_id]);
    assert.equal(view.shell.dataset.driverShiftOpen, "true");
});

test("closing waits for every unsent event of its own shift", async () => {
    /* Очередь, копившаяся без связи, отправляла закрытие раньше простоев и выбора
       ковша той же смены — выбор ковша попадал в закрытую смену (стенд, 30.09.2026). */
    const view = page();
    const {createDriverLocalShift} = load(view.document);
    const box = outbox();
    box.pending = () => Promise.resolve([
        {event_id: "downtime-a", event_type: "driver.downtime.started", state: "pending", shift_id: null, local_shift_id: box.queued[0].event_id},
        {event_id: "bucket-a", event_type: "driver.free_bucket.selected", state: "pending", shift_id: null, local_shift_id: box.queued[0].event_id},
        {event_id: "rejected", event_type: "driver.downtime.ended", state: "conflict", shift_id: null, local_shift_id: box.queued[0].event_id},
        {event_id: "other-shift", event_type: "driver.downtime.started", state: "pending", shift_id: 3, local_shift_id: null},
    ]);
    const local = createDriverLocalShift({storage: storage(), outbox: box});
    local.project(view.shell);
    await local.open(view.openForm);

    await local.close(view.closeForm);

    assert.deepEqual(box.queued[1].depends_on, ["downtime-a", "bucket-a"]);
});

test("a finished local close leaves no pending marks on the close form", async () => {
    /* Удержание «Закрыть смену» ставит кнопке is-pending, отправке — признак
       driverInPlacePending; проверка «экран занят» ловит .is-pending в оболочке.
       Местное закрытие экран не подменяет — признаки обязан снять модуль
       (бой v371, 30.09.2026: та же картина у открытия). */
    const view = page();
    const {createDriverLocalShift} = load(view.document);
    const box = outbox();
    const local = createDriverLocalShift({storage: storage(), outbox: box});
    local.project(view.shell);
    await local.open(view.openForm);
    const closeButton = node({matches: matcher(selector => selector === "[data-driver-shift-close-button]")});
    view.closeForm.children.push(closeButton);
    closeButton.classList.add("is-pending");
    closeButton.disabled = true;
    view.closeForm.dataset.driverInPlacePending = "true";
    view.openForm.dataset.driverShiftOpeningPending = "true";

    await local.close(view.closeForm);

    assert.equal(closeButton.classList.contains("is-pending"), false);
    assert.equal(closeButton.disabled, false);
    assert.equal(view.closeForm.dataset.driverInPlacePending, "false");
    assert.equal(view.openForm.dataset.driverShiftOpeningPending, "false");
});

test("server confirmation of the opening gives later actions the real shift id", async () => {
    const view = page();
    const {createDriverLocalShift} = load(view.document);
    const box = outbox();
    const local = createDriverLocalShift({storage: storage(), outbox: box});
    local.project(view.shell);
    const opening = await local.open(view.openForm);

    assert.equal(local.onConfirmed(opening, {server_ids: {shift_id: 91}, version: 50}), true);

    assert.equal(view.shell.dataset.driverShiftId, "91");
    assert.equal(view.shell.dataset.driverLocalShiftId, "");
    assert.equal(view.shiftIdInput.value, "91");
    // Свежая отрисовка сервера показывает ту же смену — состояние телефона сохраняется
    // для следующего запуска из кэша без сети.
    view.shell.dataset.driverServerShiftId = "91";
    local.project(view.shell);
    assert.equal(local.state(view.shell).server_shift_id, 91);
});

test("closing a shift the server opened earlier references its server id", async () => {
    const view = page({serverShiftId: "44"});
    const {createDriverLocalShift} = load(view.document);
    const box = outbox();
    const local = createDriverLocalShift({storage: storage(), outbox: box});
    local.project(view.shell);
    assert.equal(view.shell.dataset.driverShiftOpen, "true");

    await local.close(view.closeForm);

    assert.equal(box.queued[0].shift_id, 44);
    assert.equal(box.queued[0].local_shift_id, null);
    assert.equal(view.shell.dataset.driverShiftOpen, "false");
    // Экран сервера до отправки закрытия всё ещё рисует смену 44 открытой — телефон прав.
    local.project(view.shell);
    assert.equal(view.shell.dataset.driverShiftOpen, "false");
});

/* Матрица без сети B3a (30.09.2026): смена закрывалась на телефоне при идущем
   простое, и новая местная смена наследовала «простой уже идёт» — кнопки
   причин молчали. Простой заканчивается закрытием смены (так же считает
   сервер): телефон ставит его завершение в очередь раньше самого закрытия. */
test("closing a shift with a running downtime queues its end before the close", async () => {
    const view = page({serverShiftId: "27"});
    const card = node({
        dataset: {driverActiveDowntimeId: "local:driver-downtime-start-1"},
        matches: matcher(selector => selector === "[data-driver-active-downtime-id]"),
    });
    view.shell.children.push(card);
    const {createDriverLocalShift} = load(view.document);
    const outboxModule = require("../driver-offline-outbox-v2.js");
    globalThis.createDriverDowntimeEndEvent = outboxModule.createDriverDowntimeEndEvent;
    const box = outbox();
    const queuedView = () => box.queued.map((event) => ({
        event_id: event.event_id, event_type: event.event_type, state: "pending", shift_id: 27, local_shift_id: null,
    }));
    box.pending = () => Promise.resolve([
        {event_id: "driver-downtime-start-1", event_type: "driver.downtime.started", state: "pending", shift_id: 27, local_shift_id: null},
    ].concat(queuedView()));
    const local = createDriverLocalShift({storage: storage(), outbox: box});
    local.project(view.shell);

    await local.close(view.closeForm);

    assert.deepEqual(box.queued.map((event) => event.event_type), ["driver.downtime.ended", "driver.shift.closed"]);
    assert.equal(box.queued[0].local_downtime_id, "driver-downtime-start-1");
    assert.deepEqual(box.queued[0].depends_on, ["driver-downtime-start-1"]);
    assert.ok(box.queued[1].depends_on.includes(box.queued[0].event_id), "the close waits for the downtime end");
    delete globalThis.createDriverDowntimeEndEvent;
});

test("closing a shift ends a running downtime the server already confirmed by its server id", async () => {
    const view = page({serverShiftId: "27"});
    view.shell.children.push(node({
        dataset: {driverActiveDowntimeId: "102"},
        matches: matcher(selector => selector === "[data-driver-active-downtime-id]"),
    }));
    const {createDriverLocalShift} = load(view.document);
    globalThis.createDriverDowntimeEndEvent = require("../driver-offline-outbox-v2.js").createDriverDowntimeEndEvent;
    const box = outbox();
    box.pending = () => Promise.resolve([]);
    const local = createDriverLocalShift({storage: storage(), outbox: box});
    local.project(view.shell);

    await local.close(view.closeForm);

    assert.equal(box.queued[0].event_type, "driver.downtime.ended");
    assert.equal(box.queued[0].payload.downtime_id, 102);
    assert.deepEqual(box.queued[0].depends_on, []);
    assert.equal(box.queued[1].event_type, "driver.shift.closed");
    delete globalThis.createDriverDowntimeEndEvent;
});
