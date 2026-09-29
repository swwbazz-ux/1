"use strict";
/* Путёвка водителя из событий телефона (владелец, 30.09.2026: «путёвка должна
   наполняться рейсами и простоями независимо от интернета и сервера»).
   Сценарий приёмки координатора: два рейса и простой без сети → перезапуск
   приложения без сети → путёвка та же → сервер вернулся → те же строки, без
   дублей, с серверными номерами. Плюс отклонённые строки с пометкой (не
   удаляются сами), время сервера только при доказанной поправке часов,
   граница смены, идущий простой, разгрузка рейса машиниста без сети. */
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const {createDriverManifestLocal} = require(path.resolve(__dirname, "..", "driver-manifest-local-v1.js"));

const ACCESS = "7";
const LOCAL_SHIFT = "driver-shift-open:aaa";
const T0 = Date.parse("2026-09-30T00:00:00.000Z"); // 10:00 по Владивостоку

function at(minutes) { return new Date(T0 + minutes * 60000).toISOString(); }

function memoryStorage() {
    const values = new Map();
    return {
        getItem: (key) => (values.has(key) ? values.get(key) : null),
        setItem: (key, value) => values.set(key, String(value)),
        removeItem: (key) => values.delete(key),
        values,
    };
}

let sequence = 0;
function event(type, minutes, extra) {
    sequence += 1;
    return Object.assign({
        event_id: type.replace(/\./g, "-") + ":" + sequence,
        event_type: type,
        access_id: ACCESS,
        sequence,
        occurred_at: at(minutes),
        shift_id: null,
        local_shift_id: LOCAL_SHIFT,
        trip_id: null,
        local_trip_id: null,
        local_downtime_id: null,
        payload: {},
        context_snapshot: {},
        state: "pending",
    }, extra || {});
}

function manualLoad(minutes, point, pointName) {
    const e = event("driver.trip.loaded", minutes, {
        payload: {excavator_id: 54, dump_point_id: point, manual_control: true},
        context_snapshot: {excavator_label: "ЭКС-1", selected_dump_point_name: pointName},
    });
    e.local_trip_id = e.event_id;
    return e;
}
function manualComplete(load, minutes) {
    return event("driver.trip.manual_completed", minutes, {local_trip_id: load.local_trip_id, depends_on: [load.event_id]});
}
function downtimeStart(minutes, reasonId) {
    const e = event("driver.downtime.started", minutes, {payload: {reason_id: reasonId}});
    e.local_downtime_id = e.event_id;
    return e;
}
function downtimeEnd(start, minutes) {
    return event("driver.downtime.ended", minutes, {local_downtime_id: start.event_id, payload: {local_downtime_id: start.event_id}});
}

function node() { return {innerHTML: ""}; }

function shellFor({shiftId = LOCAL_SHIFT, open = true, page = null} = {}) {
    const trips = node();
    const downtimes = node();
    const timeline = node();
    const panel = {
        dataset: {},
        querySelector(selector) {
            if (selector === "[data-driver-report-trip-scroll]") return trips;
            if (selector === "[data-driver-report-downtime-scroll]") return downtimes;
            if (selector === "[data-driver-timeline]") return timeline;
            return null;
        },
    };
    const fact = {textContent: ""};
    const shell = {
        dataset: {driverAccessId: ACCESS, driverShiftOpen: open ? "true" : "false", driverShiftId: shiftId, driverShiftType: "day"},
        querySelector(selector) {
            if (selector === '[data-driver-tab-panel="manifest"]') return panel;
            if (selector === "#driver-manifest-data") return page ? {textContent: JSON.stringify(page)} : null;
            if (selector.indexOf("data-driver-downtime-reason-id") >= 0) {
                return {dataset: {driverReasonLabel: selector.indexOf('"13"') >= 0 ? "Заправка" : "ОФР"}};
            }
            return null;
        },
        querySelectorAll(selector) { return selector === "[data-driver-shift-fact]" ? [fact] : []; },
    };
    return {shell, panel, trips, downtimes, timeline, fact};
}

function controller(storage, nowMinutes) {
    return createDriverManifestLocal({storage, now: () => T0 + nowMinutes * 60000});
}

function tripRows(view) {
    return [...view.trips.innerHTML.matchAll(/data-excavator="([^"]*)" data-dump-point="([^"]*)" data-count="(\d+)"/g)]
        .map((m) => `${m[1]}|${m[2]}|${m[3]}`);
}
function timelineIds(view, kind) {
    const attr = kind === "trip" ? "data-driver-trip-id" : "data-driver-downtime-id";
    return [...view.timeline.innerHTML.matchAll(new RegExp(attr + '="(\\d+)"', "g"))].map((m) => Number(m[1]));
}
function timelineRows(view) {
    return [...view.timeline.innerHTML.matchAll(/<strong>([^<]*)<\/strong>/g)].map((m) => m[1]);
}

function offlineScenario() {
    sequence = 0;
    const opened = event("driver.shift.opened", 0, {event_id: LOCAL_SHIFT, local_shift_id: LOCAL_SHIFT});
    const load1 = manualLoad(5, 1, "ККД");
    const done1 = manualComplete(load1, 20);
    const load2 = manualLoad(25, 3, "Отвал");
    const done2 = manualComplete(load2, 40);
    const start = downtimeStart(45, 13);
    const end = downtimeEnd(start, 55);
    return {opened, load1, done1, load2, done2, start, end, all: [opened, load1, done1, load2, done2, start, end]};
}

test("two trips and a downtime recorded offline fill the manifest, survive a restart and do not double when the server returns", () => {
    const storage = memoryStorage();
    const s = offlineScenario();

    // Без сети: события очереди.
    const first = controller(storage, 60);
    first.observe(s.all);
    const offline = shellFor();
    first.render(offline.shell);
    assert.deepEqual(tripRows(offline), ["ЭКС-1|ККД|1", "ЭКС-1|Отвал|1"]);
    assert.equal(offline.panel.dataset.driverReportTripTotal, "2");
    assert.match(offline.downtimes.innerHTML, /data-reason="Заправка" data-duration="10 мин\."/);
    assert.equal(offline.fact.textContent, "2 шт.");

    // Перезапуск без сети: новый экземпляр, страница из кэша без данных этой смены.
    const restarted = controller(storage, 61);
    const afterRestart = shellFor();
    restarted.render(afterRestart.shell);
    assert.deepEqual(tripRows(afterRestart), tripRows(offline));
    assert.equal(afterRestart.timeline.innerHTML, offline.timeline.innerHTML);

    // Сервер вернулся: подтверждения с серверными номерами...
    const back = controller(storage, 70);
    back.confirmed(s.opened, {server_ids: {shift_id: 31}});
    back.confirmed(s.load1, {server_ids: {trip_id: 501, shift_id: 31}});
    back.confirmed(s.done1, {server_ids: {trip_id: 501}});
    back.confirmed(s.load2, {server_ids: {trip_id: 502, shift_id: 31}});
    back.confirmed(s.done2, {server_ids: {trip_id: 502}});
    back.confirmed(s.start, {server_ids: {downtime_event_id: 91}});
    back.confirmed(s.end, {server_ids: {downtime_event_id: 91}});
    // ...и свежая страница сервера с теми же рейсами и простоем.
    const page = {
        shift: {id: 31, local_id: LOCAL_SHIFT, opened_at: at(0), closed_at: null, truck_id: 10, shift_type: "day"},
        utc_offset_minutes: 600,
        trips: [
            {id: 501, local_ids: [s.load1.event_id, s.done1.event_id], status: "completed", excavator_id: 54, excavator: "1", dump_point_id: 1, dump_point: "ККД", loaded_at: at(5), completed_at: at(20), load_time_source: "driver_device", unload_time_source: "driver_device"},
            {id: 502, local_ids: [s.load2.event_id, s.done2.event_id], status: "completed", excavator_id: 54, excavator: "1", dump_point_id: 3, dump_point: "Отвал", loaded_at: at(25), completed_at: at(40), load_time_source: "driver_device", unload_time_source: "driver_device"},
        ],
        downtimes: [{id: 91, local_ids: [s.start.event_id], reason_id: 13, reason: "Заправка", started_at: at(45), ended_at: at(55)}],
        labels: {excavators: {"54": "1"}, reasons: {"13": "Заправка"}},
    };
    const online = shellFor({shiftId: "31", page});
    back.render(online.shell);
    assert.deepEqual(tripRows(online), ["1|ККД|1", "1|Отвал|1"], "same rows, server labels, no duplicates");
    assert.equal(online.panel.dataset.driverReportTripTotal, "2");
    assert.deepEqual(timelineIds(online, "trip"), [501, 502]);
    assert.deepEqual(timelineIds(online, "downtime"), [91, 91]);
    assert.equal(timelineRows(online).length, 4, "two trips + downtime start/end, nothing twice");
});

test("a row the server rejected stays in the manifest with a mark and is not purged after a day", () => {
    const storage = memoryStorage();
    sequence = 0;
    const load = manualLoad(5, 1, "ККД");
    const done = manualComplete(load, 15);
    const first = controller(storage, 20);
    first.observe([load, done]);
    first.review(load, {status: "conflict", code: "stale_driver_manual_load", message: "Отклонено"});

    const later = controller(storage, 20 + 2 * 24 * 60);
    const view = shellFor();
    later.render(view.shell);
    assert.deepEqual(tripRows(view), ["ЭКС-1|ККД|1"]);
    assert.match(view.trips.innerHTML, /data-driver-report-rejected="trips"[^>]*><span>Из них не принято сервером<\/span><strong>1</);
    assert.match(view.timeline.innerHTML, /is-rejected[\s\S]*не принято сервером/);
});

test("row time is the press time; the server time only when the server proved the phone clock wrong", () => {
    const storage = memoryStorage();
    sequence = 0;
    const load = manualLoad(5, 1, "ККД");
    const done = manualComplete(load, 15);
    const c = controller(storage, 30);
    c.observe([load, done]);
    c.confirmed(load, {server_ids: {trip_id: 7}});
    c.confirmed(done, {server_ids: {trip_id: 7}, device_clock_adjusted: true, effective_occurred_at: at(16)});
    const view = shellFor({page: {shift: {id: 1, local_id: LOCAL_SHIFT, opened_at: at(0)}, utc_offset_minutes: 600, trips: [], downtimes: [], labels: {}}});
    c.render(view.shell);
    assert.match(view.timeline.innerHTML, /<time>10:05<\/time><strong>Рейс 01 · ЭКС-1 → ККД<\/strong><span>10:05–10:16 · время сервера<\/span>/);
});

test("events of another shift never enter this shift's manifest", () => {
    const storage = memoryStorage();
    sequence = 0;
    const mine = manualLoad(5, 1, "ККД");
    const mineDone = manualComplete(mine, 10);
    const other = manualLoad(6, 3, "Отвал");
    other.local_shift_id = null;
    other.shift_id = 12;
    const otherDone = manualComplete(other, 9);
    otherDone.local_shift_id = null;
    otherDone.shift_id = 12;
    const c = controller(storage, 20);
    c.observe([mine, mineDone, other, otherDone]);
    const view = shellFor();
    c.render(view.shell);
    assert.deepEqual(tripRows(view), ["ЭКС-1|ККД|1"]);
});

test("a running downtime counts up to now and the next reason closes the previous one", () => {
    const storage = memoryStorage();
    sequence = 0;
    const first = downtimeStart(0, 12);
    const second = downtimeStart(10, 13);
    const c = controller(storage, 40);
    c.observe([first, second]);
    const view = shellFor();
    c.render(view.shell);
    assert.match(view.downtimes.innerHTML, /data-reason="ОФР" data-duration="10 мин\."/);
    assert.match(view.downtimes.innerHTML, /data-reason="Заправка" data-duration="30 мин\."/);
    assert.match(view.downtimes.innerHTML, /Всего простоев<\/span><strong>40 мин\.</);
});

test("an excavator trip unloaded offline is counted once, before and after the server confirms it", () => {
    const storage = memoryStorage();
    sequence = 0;
    const page = {
        shift: {id: 31, local_id: "", opened_at: at(0), closed_at: null},
        utc_offset_minutes: 600,
        trips: [{id: 700, local_ids: [], status: "loaded_waiting_unload", excavator_id: 54, excavator: "1", dump_point_id: 1, dump_point: "ККД", loaded_at: at(10), completed_at: null, load_time_source: "excavator_device", unload_time_source: "unknown"}],
        downtimes: [],
        labels: {excavators: {"54": "1"}},
    };
    const unload = event("driver.trip.unloaded", 30, {local_shift_id: null, shift_id: 31, trip_id: 700, payload: {trip_id: 700}});
    const c = controller(storage, 31);
    c.observe([unload]);
    const offline = shellFor({shiftId: "31", page});
    c.render(offline.shell);
    assert.deepEqual(tripRows(offline), ["1|ККД|1"]);

    c.confirmed(unload, {server_ids: {trip_id: 700}});
    const confirmedPage = Object.assign({}, page, {trips: [Object.assign({}, page.trips[0], {status: "completed", completed_at: at(30), local_ids: [unload.event_id]})]});
    const online = shellFor({shiftId: "31", page: confirmedPage});
    c.render(online.shell);
    assert.deepEqual(tripRows(online), ["1|ККД|1"]);
    assert.equal(timelineRows(online).length, 1);
});

test("the journal of a shift closed more than 7 days ago is dropped", () => {
    const storage = memoryStorage();
    sequence = 0;
    const load = manualLoad(5, 1, "ККД");
    const closed = event("driver.shift.closed", 60);
    const c = controller(storage, 70);
    c.observe([load, closed]);
    assert.equal(c.journal(ACCESS).shifts.length, 1);
    const later = controller(storage, 60 + 8 * 24 * 60);
    later.observe([event("driver.downtime.started", 60 + 8 * 24 * 60, {local_shift_id: "driver-shift-open:new", payload: {reason_id: 12}})]);
    const shifts = later.journal(ACCESS).shifts;
    assert.equal(shifts.length, 1);
    assert.ok(shifts[0].aliases.includes("l:driver-shift-open:new"));
});
