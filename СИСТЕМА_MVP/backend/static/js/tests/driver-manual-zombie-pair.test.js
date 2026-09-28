"use strict";
/* Боевой баг (стенд 28.09.2026, телефон водителя): в очереди застряла пара
   «ручная погрузка под свободный ковш + её отмена». Приём ковша отменили, сервер
   на погрузку вечно отвечает retry free_bucket_acceptance_pending (336 повторов),
   отмена ждёт её как зависимость. Последствия:
   1) синий индикатор навсегда (в очереди всегда есть неотправленное);
   2) каждая новая ручная погрузка исчезала с круга через ~1 с после того, как
      сервер её принял: принятая уходит из очереди, «последней» становится
      зомби-погрузка, её отмена «выигрывает» — круг пуст, а сервер держит рейс
      открытым, следующая погрузка получает stale_driver_manual_load.
   Правка: (а) открытый ручной рейс сервера главнее отмены, которая к нему не
   относится; (б) неотправленная погрузка и её же неотправленная отмена снимаются
   с очереди локально — намерение водителя «погрузки нет». */
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const engine = require(path.join(BACKEND, "static", "js", "driver-manual-excavator-workspace-v1.js"));
const {
    createDriverOfflineOutbox,
    createDriverManualLoadEvent,
    createDriverManualLoadCancelledEvent,
    localRepository,
} = require(path.join(BACKEND, "static", "js", "driver-offline-outbox-v2.js"));

function makeNode() {
    const classes = new Set();
    const children = {};
    return {
        dataset: {},
        hidden: false,
        disabled: false,
        textContent: "",
        title: "",
        style: {setProperty() {}, removeProperty() {}},
        classList: {
            add(...names) { names.forEach((n) => classes.add(n)); },
            remove(...names) { names.forEach((n) => classes.delete(n)); },
            toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
            contains(name) { return classes.has(name); }
        },
        setAttribute(name, value) { this["attr:" + name] = value; },
        getAttribute(name) { return this["attr:" + name]; },
        removeAttribute(name) { delete this["attr:" + name]; },
        querySelector(selector) { return children[selector] || null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        __children: children
    };
}

function workspaceWithServerTrip(tripId) {
    const shell = {dataset: {
        driverActiveTripOrigin: "driver_manual",
        driverActiveTripId: String(tripId),
        driverActiveTripLoadedAt: "2026-09-27T20:13:02.000Z",
        driverActualDumpPointId: "1",
        driverActualDumpPointName: "ККД"
    }};
    const timer = makeNode();
    ["label", "state", "destination", "value"].forEach((part) => {
        timer.__children["[data-driver-manual-trip-timer-" + part + "]"] = makeNode();
    });
    const pointOpen = makeNode();
    pointOpen.__children["[data-driver-manual-point-label]"] = makeNode();
    pointOpen.__children["[data-driver-manual-point-hint]"] = makeNode();
    const children = {
        "[data-driver-manual-source]": makeNode(),
        "[data-driver-manual-result]": makeNode(),
        "[data-driver-manual-dismiss-rejected]": makeNode(),
        "[data-driver-manual-point-notice]": makeNode(),
        "[data-driver-manual-point-open]": pointOpen,
        "[data-driver-manual-trip-timer]": timer
    };
    return {
        dataset: {},
        closest() { return shell; },
        querySelector(selector) { return children[selector] || null; },
        querySelectorAll() { return []; },
        style: {setProperty() {}, removeProperty() {}}
    };
}

// Зомби-пара: погрузка seq 29 в retry, её отмена seq 30 ждёт зависимость.
const zombieLoad = {
    event_type: "driver.trip.loaded",
    event_id: "driver-manual-load:zombie",
    local_trip_id: "driver-manual-load:zombie",
    trip_id: null,
    state: "pending",
    sequence: 29,
    attempt_count: 336,
    last_error: {code: "free_bucket_acceptance_pending"},
    occurred_at: "2026-09-27T19:38:02.237Z",
    payload: {dump_point_id: 1, assigned_dump_point_id: 1},
    context_snapshot: {selected_dump_point_id: 1, selected_dump_point_name: "ККД"}
};
const zombieCancel = {
    event_type: "driver.trip.loaded.cancelled",
    event_id: "driver-manual-load-cancel:zombie",
    local_trip_id: "driver-manual-load:zombie",
    trip_id: null,
    state: "pending",
    sequence: 30,
    depends_on: ["driver-manual-load:zombie"],
    last_error: {code: "dependency_pending"},
    occurred_at: "2026-09-27T19:38:20.297Z",
    payload: {dump_point_id: 1},
    context_snapshot: {}
};

test("a stuck load+cancel pair does not hide the server's open manual trip", () => {
    const workspace = workspaceWithServerTrip(90);
    // Новая погрузка принята сервером и уже ушла из очереди: остались только зомби.
    const projection = engine.renderProjection(workspace, [zombieLoad, zombieCancel], null);
    try {
        assert.equal(projection && projection.state, "confirmed", "круг показывает рейс сервера, а не отмену чужой погрузки");
        assert.equal(projection.trip_id, 90);
        assert.equal(engine.activeManualPointId(), "1", "точка рейса сервера остаётся на круге");
    } finally {
        engine.stopTripTimer(workspace);
    }
});

test("a cancel that really targets the server trip still wins", () => {
    const workspace = workspaceWithServerTrip(90);
    const ownCancel = Object.assign({}, zombieCancel, {
        event_id: "driver-manual-load-cancel:own",
        local_trip_id: null,
        trip_id: 90,
        depends_on: [],
        last_error: null
    });
    const projection = engine.renderProjection(workspace, [ownCancel], null);
    engine.stopTripTimer(workspace);
    assert.equal(projection && projection.state, "cancelling");
    assert.equal(engine.activeManualPointId(), "");
});

function storage() {
    const values = new Map();
    return {
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, String(value)); },
        removeItem(key) { values.delete(key); },
    };
}

function outbox({send, local = storage()} = {}) {
    return createDriverOfflineOutbox({
        repository: localRepository(local, 7),
        localStorage: local,
        accessId: 7,
        context: {actorId: 11, accessId: 7, shiftId: 23, equipmentId: 58, deviceId: "install-uuid-1"},
        send: send || (async (batch) => ({
            results: batch.events.map((event) => ({event_id: event.event_id, status: "accepted"})),
        })),
    });
}

function loadSpec(eventId, extra) {
    return Object.assign(createDriverManualLoadEvent({
        eventId,
        truckId: 58,
        excavatorId: 55,
        dumpPointId: 1,
        rockTypeId: 4,
        acceptanceLocalId: "driver-free-bucket-select:gone",
    }), extra || {});
}

function cancelSpec(eventId, loadEventId) {
    return createDriverManualLoadCancelledEvent({
        eventId,
        localTripId: loadEventId,
        loadEventId,
        truckId: 58,
        excavatorId: 55,
        dumpPointId: 1,
        events: [],
    });
}

test("a load the server keeps rejecting with retry and its own cancel leave the queue without being sent", async () => {
    const sent = [];
    const box = outbox({
        send: async (batch) => {
            sent.push(...batch.events.map((event) => event.event_id));
            return {results: batch.events.map((event) => ({
                event_id: event.event_id,
                status: event.event_type === "driver.trip.loaded" ? "retry" : "accepted",
                retryable: true,
                code: event.event_type === "driver.trip.loaded" ? "free_bucket_acceptance_pending" : "",
            }))};
        },
    });
    await box.enqueue(loadSpec("driver-manual-load:z"));
    await box.flush();                     // сервер ответил retry — рейса на сервере нет
    await box.enqueue(cancelSpec("driver-manual-load-cancel:z", "driver-manual-load:z"));
    const pending = await box.pending();
    assert.deepEqual(pending.map((event) => event.event_id), [], "пара снята локально");
    assert.deepEqual(sent, ["driver-manual-load:z"], "отмена не отправлялась");
    assert.equal(global.driverOfflineAnnihilatedPairs.at(-1).load_event_id, "driver-manual-load:z");
    assert.equal(global.driverOfflineAnnihilatedPairs.at(-1).load_last_error, "free_bucket_acceptance_pending");
});

test("a load and its cancel created offline on this page are annihilated before any send", async () => {
    const sent = [];
    const box = outbox({send: async (batch) => { sent.push(...batch.events.map((e) => e.event_id)); throw new Error("offline"); }});
    await box.enqueue(loadSpec("driver-manual-load:o"));
    await box.enqueue(cancelSpec("driver-manual-load-cancel:o", "driver-manual-load:o"));
    assert.deepEqual((await box.pending()).map((event) => event.event_id), []);
    assert.deepEqual(sent, []);
});

test("a load the server may already hold keeps its cancel in the queue", async () => {
    // Сетевой сбой — не доказательство, что сервер не создал рейс: отмену надо доставить.
    let fail = true;
    const box = outbox({send: async (batch) => {
        if (fail) throw new Error("offline");
        return {results: batch.events.map((event) => ({event_id: event.event_id, status: "accepted"}))};
    }});
    await box.enqueue(loadSpec("driver-manual-load:n"));
    await box.flush();                     // попытка была, ответа нет
    await box.enqueue(cancelSpec("driver-manual-load-cancel:n", "driver-manual-load:n"));
    assert.deepEqual(
        (await box.pending()).map((event) => event.event_id),
        ["driver-manual-load:n", "driver-manual-load-cancel:n"]
    );
    fail = false;
    await box.flush();
    assert.deepEqual(await box.pending(), []);
});

test("a load restored from storage with no attempts is not annihilated by a fresh page", async () => {
    // Запись без попыток из ДРУГОЙ сессии может быть в отправке у другой вкладки.
    const local = storage();
    const first = outbox({local, send: async () => { throw new Error("offline"); }});
    await first.enqueue(loadSpec("driver-manual-load:s"));
    const key = "driver-offline-events-v2:7";
    const items = JSON.parse(local.getItem(key) || "[]");
    assert.equal(items.length, 1);
    items.forEach((item) => { item.created_session = "page-other"; });
    local.setItem(key, JSON.stringify(items));
    const second = outbox({local, send: async () => { throw new Error("offline"); }});
    await second.enqueue(cancelSpec("driver-manual-load-cancel:s", "driver-manual-load:s"));
    assert.equal((await second.pending()).length, 2);
});
