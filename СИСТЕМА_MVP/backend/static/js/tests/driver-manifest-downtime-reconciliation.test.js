"use strict";

/* Регрессии клиентской путёвки: сервер закрывает ожидание погрузкой, а телефон
   не получает отдельного события downtime.ended. Даты, имена и ID синтетические. */
const test = require("node:test");
const assert = require("node:assert/strict");
const {replay, merge, buildReport, createDriverManifestLocal} = require("../driver-manifest-local-v1.js");

const BASE = Date.parse("2026-01-01T00:00:00Z");
const at = (seconds) => new Date(BASE + seconds * 1000).toISOString();
const WAIT = "Ожидание погрузки";
const UNLOAD = "Ожидание разгрузки";
const FUEL = "Заправка";

function start(id, seconds, reason = 9, serverId = null) {
    return {
        event_id: "start-" + id, event_type: "driver.downtime.started", sequence: seconds + 1,
        occurred_at: at(seconds), local_downtime_id: "start-" + id,
        reason_id: reason, reason_label: reason === 9 ? WAIT : reason === 10 ? UNLOAD : FUEL,
        status: "confirmed", server_downtime_id: serverId,
    };
}

function end(id, seconds, serverId) {
    return {
        event_id: "end-" + id, event_type: "driver.downtime.ended", sequence: seconds + 1,
        occurred_at: at(seconds), local_downtime_id: "start-" + id,
        downtime_id: serverId, status: "pending",
    };
}

function serverRow(id, started, ended, reason = 9, localIds = ["start-" + id]) {
    return {
        id, local_ids: localIds, reason_id: reason,
        reason: reason === 9 ? WAIT : reason === 10 ? UNLOAD : FUEL,
        started_at: at(started), ended_at: ended === null ? null : at(ended),
    };
}

function report(rows, closed = 40000) {
    return buildReport(rows, {opened_at: at(0), closed_at: closed === null ? null : at(closed), now: BASE + 42000 * 1000}, 0);
}

function loadingSeconds(rows) {
    return report(rows).downtimes.find((row) => row.reason === WAIT)?.seconds || 0;
}

function shiftPayload(downtimes, closed = null) {
    return {
        shift: {id: 77, local_id: "", truck_id: 88, opened_at: at(0), closed_at: closed === null ? null : at(closed)},
        trips: [], downtimes,
        labels: {reasons: {"9": WAIT, "10": UNLOAD, "8": FUEL}, reason_flows: {"9": "waiting_loading", "10": "waiting_unload", "8": ""}},
        utc_offset_minutes: 0,
    };
}

function incidentFixture() {
    // Восемь ожиданий имеют продолжительности 184+155+235+624+573+63+517+970.
    const spans = [
        [101, 0, 184, 9], [102, 1807, 1962, 9], [103, 5261, 5496, 9],
        [104, 8946, 9570, 9], [105, 10457, 10746, 10],
        [106, 12918, 13491, 9], [107, 16934, 17946, 8],
        [108, 27186, 27249, 9], [109, 28203, 28381, 10],
        [110, 30999, 31516, 9], [111, 32290, 32600, 10], [112, 33184, 34154, 9],
    ];
    const log = spans.flatMap(([id, from, to, reason]) => reason === 9
        ? [start(id, from, reason, id)]
        : [start(id, from, reason, id), end(id, to, id)]);
    return {log, server: shiftPayload(spans.map((span) => serverRow(...span)), 38553)};
}

test("repeated loading waits closed by excavator loads reconcile to 3321 seconds, not 18765", () => {
    const {log, server} = incidentFixture();
    const rows = merge(replay(log, server), server);
    assert.equal(loadingSeconds(rows), 3321);
    assert.equal(loadingSeconds(merge(replay(log), server)), 3321, "standalone replay followed by merge is also safe");
    assert.equal(rows.downtimes.filter((row) => row.reason === WAIT).length, 8);
    assert.equal(report(rows).downtimes.find((row) => row.reason === UNLOAD).seconds, 777);
    assert.equal(report(rows).downtimes.find((row) => row.reason === FUEL).seconds, 1012);
});

test("a closed server interval cannot be extended by a later local reason or stale phone clock", () => {
    const log = [start(1, 0, 9, 1), start(2, 12000, 8, 2)];
    const server = shiftPayload([serverRow(1, 20, 320), serverRow(2, 12000, 12100, 8)]);
    const row = merge(replay(log, server), server).downtimes.find((item) => item.downtime_id === 1);
    assert.equal(row.started_at, at(20));
    assert.equal(row.ended_at, at(320));
    assert.equal(loadingSeconds({trips: [], downtimes: [row]}), 300);
});

test("different start IDs of the same reason remain separate waiting intervals", () => {
    const rows = replay([start(1, 0), start(2, 600)]).downtimes;
    assert.equal(rows.length, 2);
    assert.equal(rows[0].ended_at, at(600));
    assert.deepEqual(rows.map((row) => row.local_downtime_id), ["start-1", "start-2"]);
});

test("repeated starts confirmed as one unchanged server downtime do not duplicate it", () => {
    const log = [start(1, 0, 9, 10), start(2, 60, 9, 10), end(1, 300, 10)];
    const server = shiftPayload([serverRow(10, 0, 300, 9, ["start-1", "start-2"])]);
    const rows = merge(replay(log, server), server);
    assert.equal(rows.downtimes.length, 1);
    assert.equal(loadingSeconds(rows), 300);
});

test("server local IDs alone prove two unchanged starts belong to one downtime", () => {
    const log = [start(1, 0), start(2, 60), end(2, 300, null)];
    const server = shiftPayload([serverRow(10, 0, 300, 9, ["start-1", "start-2"])]);
    assert.equal(merge(replay(log, server), server).downtimes.length, 1);
    assert.equal(loadingSeconds(merge(replay(log, server), server)), 300);
    assert.equal(loadingSeconds(merge(replay(log), server)), 300);
});

test("incoming excavator loading closes a phone-only loading wait at the trip load time", () => {
    const log = [start(1, 100)];
    const server = shiftPayload([]);
    server.trips = [{
        id: 90, local_ids: [], status: "completed", excavator: "Тест", dump_point: "Тест",
        loaded_at: at(400), completed_at: at(900),
    }];
    const rows = merge(replay(log, server), server);
    assert.equal(rows.downtimes[0].ended_at, at(400));
    assert.equal(loadingSeconds(rows), 300);
});

test("an excavator load before the local waiting start cannot close a later wait", () => {
    const log = [start(1, 500)];
    const server = shiftPayload([]);
    server.trips = [{
        id: 90, local_ids: [], status: "completed", excavator: "Тест", dump_point: "Тест",
        loaded_at: at(400), completed_at: at(900),
    }];
    const rows = merge(replay(log, server), server);
    assert.equal(rows.downtimes[0].ended_at, null);
});

test("cancelled or rejected loads cannot implicitly close a waiting interval", () => {
    for (const status of ["cancelled", "rejected"]) {
        const log = [start(1, 100)];
        const server = shiftPayload([]);
        if (status === "cancelled") {
            server.trips = [{
                id: 90, local_ids: [], status, excavator: "Тест", dump_point: "Тест",
                loaded_at: at(400), completed_at: null,
            }];
        } else {
            log.push({event_id: "rejected-load", event_type: "driver.trip.loaded", local_trip_id: "rejected-load",
                sequence: 401, occurred_at: at(400), status: "rejected", reject: {code: "needs_review"}});
        }
        assert.equal(merge(replay(log, server), server).downtimes[0].ended_at, null, status);
    }
});

test("closed-shift downtime is capped at shift close and never grows with the phone clock", () => {
    const rows = {trips: [], downtimes: [{reason: WAIT, started_at: at(100), ended_at: null}]};
    const window = {opened_at: at(0), closed_at: at(400), now: BASE + 1000000};
    assert.equal(buildReport(rows, window, 0).downtimes[0].seconds, 300);
    window.now = BASE + 7 * 86400000;
    assert.equal(buildReport(rows, window, 0).downtimes[0].seconds, 300);
});

test("a pending explicit local end is shown immediately while the cached server row is open", () => {
    const log = [start(1, 0, 9, 1), end(1, 300, 1)];
    const server = shiftPayload([serverRow(1, 0, null)]);
    const rows = merge(replay(log, server), server);
    assert.equal(rows.downtimes.length, 1);
    assert.equal(rows.downtimes[0].ended_at, at(300));
    assert.equal(loadingSeconds(rows), 300);
});

test("confirmed server closure wins over a pending local end even when the latter is later", () => {
    const log = [start(1, 0, 9, 1), end(1, 1200, 1)];
    const server = shiftPayload([serverRow(1, 0, 300)]);
    assert.equal(loadingSeconds(merge(replay(log, server), server)), 300);
});

test("a confirmed close can target its downtime using only the acknowledgement server ID", () => {
    const close = end(1, 300, null);
    close.local_downtime_id = "";
    close.server_downtime_id = 10;
    close.status = "confirmed";
    const rows = replay([start(1, 0, 9, 10), close]).downtimes;
    assert.equal(rows.length, 1);
    assert.equal(rows[0].ended_at, at(300));
});

function storage() {
    const data = new Map();
    return {getItem: (key) => data.get(key) || null, setItem: (key, value) => data.set(key, String(value))};
}

function view(page, open = true) {
    const targets = new Map();
    const panel = {
        dataset: {}, getAttribute: () => JSON.stringify(page),
        querySelector: (selector) => {
            if (!targets.has(selector)) targets.set(selector, {innerHTML: ""});
            return targets.get(selector);
        },
    };
    return {shell: {
        dataset: {driverAccessId: "5", driverShiftId: "77", driverShiftOpen: String(open), driverShiftType: "day"},
        querySelector: (selector) => selector === '[data-driver-tab-panel="manifest"]' ? panel : null,
        querySelectorAll: () => [],
    }, panel, targets};
}

function eventForController(entry) {
    return {
        ...entry, access_id: "5", shift_id: 77, state: entry.status === "confirmed" ? "confirmed" : "pending",
        payload: {reason_id: entry.reason_id, downtime_id: entry.downtime_id, local_downtime_id: entry.local_downtime_id},
    };
}

test("fresh closed server evidence survives restart and cannot be replaced by a lower-version open snapshot", () => {
    const previousDocument = globalThis.document;
    const store = storage();
    try {
        globalThis.document = {body: {dataset: {operationalStateVersion: "20"}}};
        const closed = shiftPayload([serverRow(1, 0, 300)], 3600);
        const first = createDriverManifestLocal({storage: store, now: () => BASE + 4200 * 1000});
        const fresh = view(closed, false);
        first.render(fresh.shell);
        assert.equal(loadingSeconds(first.model(fresh.shell).rows), 300);

        globalThis.document.body.dataset.operationalStateVersion = "19";
        const restarted = createDriverManifestLocal({storage: store, now: () => BASE + 8000 * 1000});
        const stale = view(shiftPayload([serverRow(1, 0, null)]), false);
        restarted.render(stale.shell);
        assert.equal(loadingSeconds(restarted.model(stale.shell).rows), 300);
        assert.equal(stale.panel.dataset.driverReportDowntimeTotal, "5 мин.");
    } finally {
        globalThis.document = previousDocument;
    }
});

test("an equal-version cached snapshot cannot reopen already confirmed server downtime", () => {
    const previousDocument = globalThis.document;
    const store = storage();
    try {
        globalThis.document = {body: {dataset: {operationalStateVersion: "20"}}};
        const first = createDriverManifestLocal({storage: store, now: () => BASE + 4200 * 1000});
        first.render(view(shiftPayload([serverRow(1, 0, 300)], 3600), false).shell);
        const restarted = createDriverManifestLocal({storage: store, now: () => BASE + 8000 * 1000});
        const stale = view(shiftPayload([serverRow(1, 0, null)]), false);
        assert.equal(loadingSeconds(restarted.model(stale.shell).rows), 300);
    } finally {
        globalThis.document = previousDocument;
    }
});

test("offline starts and explicit ends survive restart and reconcile without duplicates", () => {
    const store = storage();
    const log = [start(1, 0), end(1, 120, null), start(2, 600), end(2, 780, null)];
    const first = createDriverManifestLocal({storage: store, now: () => BASE + 900000});
    first.observe(log.map(eventForController));
    const page = shiftPayload([]);
    const offline = view(page);
    first.render(offline.shell);
    assert.equal(loadingSeconds(first.model(offline.shell).rows), 300);
    const restarted = createDriverManifestLocal({storage: store, now: () => BASE + 1000000});
    const after = view(page);
    restarted.render(after.shell);
    assert.equal(loadingSeconds(restarted.model(after.shell).rows), 300);
    const online = view(shiftPayload([serverRow(1, 0, 120), serverRow(2, 600, 780)]));
    restarted.render(online.shell);
    assert.equal(restarted.model(online.shell).rows.downtimes.length, 2);
    assert.equal(loadingSeconds(restarted.model(online.shell).rows), 300);
});

test("rejected local downtime remains visible with a review mark after server reconciliation", () => {
    const log = [start(1, 0), end(1, 120, null)];
    log[0].status = "rejected";
    log[0].reject = {code: "needs_review", message: "Нужна сверка"};
    const rows = merge(replay(log), shiftPayload([]));
    assert.equal(rows.downtimes.length, 1);
    assert.equal(rows.downtimes[0].rejected, true);
    assert.equal(report(rows).downtimeRejected, 1);
    assert.match(report(rows).timeline.find((row) => row.kind === "downtime-start").meta, /не принято сервером/);
    assert.equal(report(rows).downtimeTotal, 0, "unaccepted evidence cannot inflate operational totals");
});

test("a rejected close cannot stop a still-open accepted server downtime", () => {
    const close = end(1, 300, 1);
    close.status = "rejected";
    const server = shiftPayload([serverRow(1, 0, null)]);
    const rows = merge(replay([start(1, 0, 9, 1), close], server), server);
    assert.equal(rows.downtimes.find((row) => row.downtime_id === 1).ended_at, null);
    assert.equal(report(rows).downtimeTotal, 40000, "accepted server interval remains countable");
});

test("a rejected close cannot remove accepted closed server duration from the totals", () => {
    const close = end(1, 1200, 1);
    close.status = "rejected";
    const server = shiftPayload([serverRow(1, 0, 300)]);
    const rows = merge(replay([start(1, 0, 9, 1), close], server), server);
    assert.equal(report(rows).downtimeTotal, 300);
});

test("a rejected next start cannot close the previous accepted local interval", () => {
    const rejected = start(2, 300, 8);
    rejected.status = "rejected";
    const rows = replay([start(1, 0), rejected]).downtimes;
    assert.equal(rows.find((row) => row.local_downtime_id === "start-1").ended_at, null);
});
