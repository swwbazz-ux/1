"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
    createDriverOfflineOutbox,
    createDriverManualLoadEvent,
    createDriverManualLoadCancelledEvent,
    createDriverManualCompletedEvent,
    createDriverPointChangeEvent,
    createDriverFreeBucketSelectedEvent,
    createDriverFreeBucketCancelledEvent,
    isDriverSyncAuthResponse,
    createDriverDowntimeEndEvent,
    selectDriverDowntimeProjection,
    localRepository,
    backoff,
} = require("../driver-offline-outbox-v2.js");

test("manual-load cancellation keeps an exact trip reference and the latest dependency", () => {
    const serverCancel = createDriverManualLoadCancelledEvent({
        eventId: "manual-cancel-server",
        tripId: 81,
        truckId: 58,
        excavatorId: 9,
        dumpPointId: 4,
        events: [{
            event_id: "point-change-1",
            event_type: "driver.trip.dump_point_changed",
            trip_id: 81,
            sequence: 7,
            state: "pending",
        }],
    });
    assert.equal(serverCancel.event_type, "driver.trip.loaded.cancelled");
    assert.equal(serverCancel.trip_id, 81);
    assert.equal(serverCancel.local_trip_id, null);
    assert.deepEqual(serverCancel.depends_on, ["point-change-1"]);

    const localCancel = createDriverManualLoadCancelledEvent({
        eventId: "manual-cancel-local",
        localTripId: "manual-load-local",
        loadEventId: "manual-load-local",
        truckId: 58,
        excavatorId: 9,
        dumpPointId: 4,
        events: [{
            event_id: "point-change-local",
            event_type: "driver.trip.dump_point_changed",
            local_trip_id: "manual-load-local",
            sequence: 8,
            state: "pending",
        }],
    });
    assert.equal(localCancel.trip_id, null);
    assert.equal(localCancel.local_trip_id, "manual-load-local");
    assert.deepEqual(localCancel.depends_on, ["point-change-local", "manual-load-local"]);
    assert.throws(
        () => createDriverManualLoadCancelledEvent({tripId: 81, localTripId: "manual-load-local"}),
        /offline_manual_cancel_identity_invalid/
    );
});

test("downward swipe completes the exact local or confirmed manual trip through the existing queue", () => {
    const confirmed = createDriverManualCompletedEvent({
        eventId: "manual-end-confirmed",
        tripId: 81,
        truckId: 58,
        excavatorId: 9,
        dumpPointId: 4,
        events: [{
            event_id: "point-change-confirmed",
            event_type: "driver.trip.dump_point_changed",
            trip_id: 81,
            sequence: 7,
            state: "pending",
        }],
        contextSnapshot: {source: "driver_manual", action: "manual_completed"},
    });
    assert.equal(confirmed.event_type, "driver.trip.manual_completed");
    assert.equal(confirmed.trip_id, 81);
    assert.equal(confirmed.local_trip_id, null);
    assert.deepEqual(confirmed.depends_on, ["point-change-confirmed"]);
    assert.equal(confirmed.payload.manual_control, true);

    const local = createDriverManualCompletedEvent({
        eventId: "manual-end-local",
        localTripId: "manual-load-local",
        loadEventId: "manual-load-local",
        truckId: 58,
        excavatorId: 9,
        dumpPointId: 4,
        events: [],
        contextSnapshot: {source: "driver_manual", action: "manual_completed"},
    });
    assert.equal(local.trip_id, null);
    assert.equal(local.local_trip_id, "manual-load-local");
    assert.deepEqual(local.depends_on, ["manual-load-local"]);
    assert.throws(
        () => createDriverManualCompletedEvent({tripId: 81, localTripId: "manual-load-local"}),
        /offline_manual_complete_identity_invalid/
    );
});

function manualLoad(overrides = {}) {
    return createDriverManualLoadEvent({
        eventId: "manual-load-1",
        occurredAt: "2026-09-21T01:02:03.000Z",
        truckId: 58,
        excavatorId: 9,
        dumpPointId: 4,
        rockTypeId: 3,
        assignmentId: 71,
        placementId: 22,
        placementUpdatedAt: "2026-09-21T00:00:00Z",
        loadingHorizon: "15",
        loadingBlock: "55",
        transportDistanceKm: "4.2",
        contextSnapshot: {
            source: "driver_manual",
            excavator_id: 9,
            rock_type_id: 3,
            dump_points: [{id: 4, name: "СКЛАД 2.1"}],
            selected_dump_point_id: 4,
        },
        ...overrides,
    });
}

test("manual load uses stable local identity and an immutable Driver snapshot", async () => {
    const spec = manualLoad();
    assert.equal(spec.event_type, "driver.trip.loaded");
    assert.equal(spec.local_trip_id, spec.event_id);
    assert.equal(spec.trip_id, null);
    assert.equal(spec.payload.manual_control, true);
    assert.equal(spec.payload.assignment_id, 71);
    assert.equal(spec.payload.free_bucket_acceptance_id, null);

    const box = runtime({send: async () => { throw new Error("offline"); }});
    const saved = await box.enqueue(spec);
    spec.context_snapshot.dump_points[0].name = "ПОДМЕНА";
    assert.equal(saved.context_snapshot.dump_points[0].name, "СКЛАД 2.1");
    assert.equal(saved.actor_id, 11);
    assert.equal(saved.role_code, "driver");
    assert.equal(saved.equipment_id, 58);
    assert.deepEqual(manualLoad({eventId: "manual-load-2", dependsOn: ["manual-load-1"]}).depends_on, ["manual-load-1"]);
});

test("manual load rejects missing or ambiguous authority and a foreign truck", async () => {
    assert.throws(() => manualLoad({assignmentId: null}), /offline_manual_trip_authority_ambiguous/);
    assert.throws(
        () => manualLoad({acceptanceId: 91}),
        /offline_manual_trip_authority_ambiguous/
    );
    const box = runtime();
    await assert.rejects(box.enqueue(manualLoad({truckId: 99})), /offline_manual_trip_truck_mismatch/);
});

test("point correction can depend on a not-yet-confirmed manual trip", async () => {
    const box = runtime({send: async () => { throw new Error("offline"); }});
    const load = await box.enqueue(manualLoad());
    const point = await box.enqueue(createDriverPointChangeEvent({
        eventId: "manual-point-1",
        occurredAt: "2026-09-21T01:03:00.000Z",
        localTripId: load.local_trip_id,
        loadEventId: load.event_id,
        pointId: 5,
        currentPointId: 4,
        pointName: "ККД",
    }));
    assert.equal(point.trip_id, null);
    assert.equal(point.local_trip_id, load.local_trip_id);
    assert.deepEqual(point.depends_on, [load.event_id]);
});

test("manual trip confirmation survives restart with the server mapping", async () => {
    const local = storage();
    const send = async batch => ({results: batch.events.map(event => ({
        event_id: event.event_id,
        status: "accepted",
        server_received_at: "2026-09-21T01:02:05.000Z",
        server_ids: {trip_id: 451, shift_id: 23},
        trip_origin: "driver_manual",
        version: 812,
    }))});
    const first = runtime({local, send});
    await first.enqueue(manualLoad());
    await first.flush();
    const restarted = runtime({local, send});
    const receipt = await restarted.getManualTripProjectionReceipt(23, 58);
    assert.equal(receipt.event_id, "manual-load-1");
    assert.equal(receipt.server_ids.trip_id, 451);
    assert.equal(receipt.trip_origin, "driver_manual");
    assert.equal(receipt.version, 812);
    assert.equal((await restarted.pending()).length, 0);
});

test("terminal downtime events never replace the authoritative active downtime", () => {
    const projection = selectDriverDowntimeProjection([
        {
            event_id: "pending-start",
            event_type: "driver.downtime.started",
            sequence: 10,
            state: "pending",
            payload: {reason_id: 4},
        },
        {
            event_id: "rejected-switch",
            event_type: "driver.downtime.started",
            sequence: 11,
            state: "conflict",
            payload: {reason_id: 8},
        },
    ]);

    assert.equal(projection.event_id, "pending-start");
    assert.equal(selectDriverDowntimeProjection([
        {
            event_id: "only-terminal-start",
            event_type: "driver.downtime.started",
            sequence: 12,
            state: "invalid",
            payload: {reason_id: 9},
        },
    ]), null);
});

test("restart recovers only clock conflict and its dependency chain with immutable identity", async () => {
    const local = storage();
    const deviceStart = "2036-09-23T10:00:00.000Z";
    const deviceEnd = "2036-09-23T10:01:00.000Z";
    const rejected = runtime({
        local,
        send: async batch => ({
            results: batch.events.map(event => ({
                event_id: event.event_id,
                status: "conflict",
                code: event.event_id === "clock-parent" ? "device_clock_ahead" : "dependency_rejected",
                message: event.event_id === "clock-parent"
                    ? "Часы устройства опережают сервер"
                    : "Предыдущее событие требует сверки",
            })),
        }),
    });
    await rejected.enqueue({
        event_id: "clock-parent",
        event_type: "driver.downtime.started",
        occurred_at: deviceStart,
        payload: {reason_id: 9},
    });
    await rejected.enqueue({
        event_id: "clock-child",
        event_type: "driver.downtime.ended",
        occurred_at: deviceEnd,
        local_downtime_id: "clock-parent",
        depends_on: ["clock-parent"],
        payload: {local_downtime_id: "clock-parent"},
    });
    await rejected.flush();
    assert.deepEqual((await rejected.pending()).map(event => event.state), ["conflict", "conflict"]);

    const delivered = [];
    const restarted = runtime({
        local,
        send: async batch => {
            delivered.push(structuredClone(batch.events));
            return {
                results: batch.events.map(event => ({
                    event_id: event.event_id,
                    status: "accepted",
                    effective_occurred_at: "2026-09-23T00:00:00.000Z",
                    device_occurred_at: event.occurred_at,
                    time_source: "server_receipt",
                })),
            };
        },
    });
    await restarted.initialize();

    assert.equal(delivered.length, 1);
    assert.deepEqual(delivered[0].map(event => event.event_id), ["clock-parent", "clock-child"]);
    assert.deepEqual(delivered[0].map(event => event.sequence), [1, 2]);
    assert.deepEqual(delivered[0].map(event => event.occurred_at), [deviceStart, deviceEnd]);
    assert.deepEqual(delivered[0][1].depends_on, ["clock-parent"]);
    assert.equal((await restarted.pending()).length, 0);
});

test("restart resends a manual load refused as free_bucket_not_available and its chain", async () => {
    const local = storage();
    const loadPayload = {
        manual_control: true, truck_id: 58, excavator_id: 7, dump_point_id: 3, rock_type_id: 2,
        assignment_id: null, free_bucket_acceptance_id: null, free_bucket_acceptance_local_id: "fb-select-1",
    };
    const rejected = runtime({
        local,
        send: async batch => ({
            results: batch.events.map(event => ({
                event_id: event.event_id,
                status: "conflict",
                code: event.event_id === "fb-load" ? "free_bucket_not_available" : "dependency_rejected",
                message: event.event_id === "fb-load"
                    ? "Свободный ковш уже отменён или закрыт."
                    : "Предыдущее событие требует сверки или отклонено.",
            })),
        }),
    });
    await rejected.enqueue({
        event_id: "fb-load",
        event_type: "driver.trip.loaded",
        occurred_at: "2026-09-28T13:00:00.000Z",
        local_trip_id: "fb-load",
        payload: loadPayload,
    });
    await rejected.enqueue({
        event_id: "fb-complete",
        event_type: "driver.trip.manual_completed",
        occurred_at: "2026-09-28T13:05:00.000Z",
        local_trip_id: "fb-load",
        depends_on: ["fb-load"],
        payload: {manual_control: true, truck_id: 58, excavator_id: 7, dump_point_id: 3},
    });
    await rejected.flush();
    assert.deepEqual((await rejected.pending()).map(event => event.state), ["conflict", "conflict"]);

    const delivered = [];
    const restarted = runtime({
        local,
        send: async batch => {
            delivered.push(batch.events.map(event => event.event_id));
            return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
        },
    });
    await restarted.initialize();

    assert.deepEqual(delivered.flat(), ["fb-load", "fb-complete"]);
    assert.equal((await restarted.pending()).length, 0);
});

test("restart resends a free-bucket selection refused as free_bucket_request_stale and its cancel", async () => {
    const local = storage();
    const rejected = runtime({
        local,
        send: async batch => ({
            results: batch.events.map(event => ({
                event_id: event.event_id,
                status: "conflict",
                code: event.event_id === "fb-select" ? "free_bucket_request_stale" : "dependency_rejected",
                message: "Отклонено.",
            })),
        }),
    });
    await rejected.enqueue({
        event_id: "fb-select",
        event_type: "driver.free_bucket.selected",
        occurred_at: "2026-09-30T07:21:58.000Z",
        payload: {truck_id: 58, excavator_id: 7},
    });
    await rejected.enqueue({
        event_id: "fb-cancel",
        event_type: "driver.free_bucket.cancelled",
        occurred_at: "2026-09-30T07:21:59.000Z",
        depends_on: ["fb-select"],
        payload: {free_bucket_acceptance_id: null, free_bucket_acceptance_local_id: "fb-select"},
    });
    await rejected.flush();
    assert.deepEqual((await rejected.pending()).map(event => event.state), ["conflict", "conflict"]);

    const delivered = [];
    const restarted = runtime({
        local,
        send: async batch => {
            delivered.push(batch.events.map(event => event.event_id));
            return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
        },
    });
    await restarted.initialize();

    assert.deepEqual(delivered.flat(), ["fb-select", "fb-cancel"]);
    assert.equal((await restarted.pending()).length, 0);
});

test("restart resends bucket refusals the v375 server accepts: point outside the snapshot, another excavator", async () => {
    const local = storage();
    const rejected = runtime({
        local,
        send: async batch => ({
            results: batch.events.map(event => ({
                event_id: event.event_id,
                status: "conflict",
                code: event.event_id === "fb-select-2" ? "free_bucket_target_changed" : "free_bucket_work_context_changed",
                message: "Отклонено.",
            })),
        }),
    });
    await rejected.enqueue({
        event_id: "fb-select-2",
        event_type: "driver.free_bucket.selected",
        occurred_at: "2026-10-01T08:00:00.000Z",
        payload: {truck_id: 58, excavator_id: 9},
    });
    await rejected.enqueue({
        event_id: "fb-load-2",
        event_type: "driver.trip.loaded",
        occurred_at: "2026-10-01T08:01:00.000Z",
        local_trip_id: "fb-load-2",
        payload: {
            manual_control: true, truck_id: 58, excavator_id: 9, dump_point_id: 3, rock_type_id: 2,
            assignment_id: null, free_bucket_acceptance_id: null, free_bucket_acceptance_local_id: "fb-select-2",
        },
    });
    await rejected.flush();
    assert.deepEqual((await rejected.pending()).map(event => event.state), ["conflict", "conflict"]);

    const delivered = [];
    const restarted = runtime({
        local,
        send: async batch => {
            delivered.push(batch.events.map(event => event.event_id));
            return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
        },
    });
    await restarted.initialize();
    assert.deepEqual(delivered.flat(), ["fb-select-2", "fb-load-2"]);
    assert.equal((await restarted.pending()).length, 0);
});

test("restart never retries a real domain conflict", async () => {
    const local = storage();
    const rejected = runtime({
        local,
        send: async batch => ({
            results: batch.events.map(event => ({
                event_id: event.event_id,
                status: "conflict",
                code: "equipment_context_changed",
                message: "Техника в событии не совпадает со сменой",
            })),
        }),
    });
    await rejected.enqueue({
        event_id: "real-domain-conflict",
        event_type: "driver.downtime.started",
        occurred_at: "2036-09-23T10:00:00.000Z",
        payload: {reason_id: 9},
    });
    await rejected.flush();

    let sends = 0;
    const restarted = runtime({local, send: async () => { sends += 1; return {results: []}; }});
    await restarted.initialize();

    const events = await restarted.pending();
    assert.equal(sends, 0);
    assert.equal(events.length, 1);
    assert.equal(events[0].state, "conflict");
    assert.equal(events[0].last_error.code, "equipment_context_changed");
});

test("confirmed downtime close receipt survives queue removal and restart", async () => {
    const local = storage();
    const send = async batch => ({
        results: batch.events.map((event, index) => ({
            event_id: event.event_id,
            status: "accepted",
            server_received_at: `2026-09-17T00:00:0${index + 1}Z`,
            server_ids: {downtime_event_id: 701, shift_id: 23},
        })),
    });
    const first = runtime({local, send});
    await first.enqueue({
        event_id: "downtime-receipt-start",
        event_type: "driver.downtime.started",
        occurred_at: "2026-09-17T00:00:00Z",
        payload: {reason_id: 9},
    });
    await first.flush();

    const second = runtime({local, send});
    await second.enqueue(createDriverDowntimeEndEvent({
        eventId: "downtime-receipt-end",
        occurredAt: "2026-09-17T00:01:00Z",
        serverId: 701,
        contextSnapshot: {
            downtime_projection: {
                shift_total_seconds: 60,
                active_elapsed_seconds: 60,
                reason_totals: {9: 60},
            },
        },
    }));
    await second.flush();

    const restarted = runtime({local, send});
    const receipt = await restarted.getDowntimeProjectionReceipt(23, 58);
    assert.equal((await restarted.pending()).length, 0);
    assert.equal(receipt.event_type, "driver.downtime.ended");
    assert.equal(receipt.event_id, "downtime-receipt-end");
    assert.equal(receipt.server_ids.downtime_event_id, 701);
    assert.equal(receipt.projection.shift_total_seconds, 60);
    assert.deepEqual(receipt.projection.reason_totals, {9: 60});
});

function storage() {
    const values = new Map();
    return {
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, String(value)); },
        removeItem(key) { values.delete(key); },
    };
}

function runtime({send, local = storage(), accessId = 7, context, batchSize, onState, onConfirmed, onReview, indexedDB, requestTimeoutMs} = {}) {
    return createDriverOfflineOutbox({
        repository: indexedDB ? undefined : localRepository(local, accessId),
        indexedDB,
        localStorage: local,
        accessId,
        context: context || {
            actorId: 11,
            accessId,
            shiftId: 23,
            equipmentId: 58,
            deviceId: "install-uuid-1",
        },
        batchSize,
        requestTimeoutMs,
        onState,
        onConfirmed,
        onReview,
        send: send || (async batch => ({
            results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"})),
        })),
    });
}

test("a projection callback failure does not turn a durable enqueue into a storage error", async () => {
    let stateCalls = 0;
    const box = runtime({
        onState() {
            stateCalls += 1;
            throw new Error("broken_projection");
        },
        send: async () => { throw new Error("offline"); },
    });
    const saved = await box.enqueue({
        event_id: "durable-despite-projection",
        event_type: "driver.trip.unloaded",
        trip_id: 91,
        payload: {trip_id: 91},
    });
    assert.equal(saved.event_id, "durable-despite-projection");
    assert.equal((await box.pending()).length, 1);
    assert.equal(stateCalls, 1);
});

const fakeIndexedDB = require("./helpers/transactional-indexeddb.js");

test("recovered Driver IndexedDB merges fallback facts with its own queue and preserves order", async () => {
    const local = storage();
    const indexedDB = fakeIndexedDB();
    const primary = runtime({local, indexedDB});
    const before = await primary.enqueue({event_id: "primary-kept", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    const fallback = runtime({local});
    const offline = await fallback.enqueue({event_id: "fallback-kept", event_type: "driver.trip.unloaded", trip_id: 2, payload: {trip_id: 2}});
    assert.ok(offline.sequence > before.sequence);
    const recovered = runtime({local, indexedDB});
    const events = await recovered.pending();
    assert.deepEqual(events.map(event => event.event_id), ["primary-kept", "fallback-kept"]);
    assert.deepEqual(events[1], offline);
    assert.deepEqual(JSON.parse(local.getItem("driver-offline-events-v2:7")), []);
    const next = await recovered.enqueue({event_id: "after-recovery", event_type: "driver.trip.unloaded", trip_id: 3, payload: {trip_id: 3}});
    assert.ok(next.sequence > offline.sequence);
});

test("Driver fallback ACK archive and metadata survive recovery with an empty delivery queue", async () => {
    const local = storage();
    const fallback = runtime({local, send: async batch => ({results: batch.events.map(event => ({
        event_id: event.event_id, status: "accepted", server_ids: {trip_id: 501},
    }))})});
    const original = await fallback.enqueue(manualLoad());
    await fallback.flush();
    const meta = JSON.parse(local.getItem("driver-offline-events-v2:7:meta"));
    meta["sequence:driver:11:install-uuid-1"] = 205;
    meta["sequence:7"] = 205;
    local.setItem("driver-offline-events-v2:7:meta", JSON.stringify(meta));
    const recovered = runtime({local, indexedDB: fakeIndexedDB()});
    assert.deepEqual(await recovered.pending(), []);
    const [source] = await recovered.journal();
    assert.equal(source.event_id, original.event_id);
    assert.equal(source.state, "confirmed");
    assert.equal(source.server_result.server_ids.trip_id, 501);
    assert.deepEqual(await recovered.getServerMapping(original.event_id), {trip_id: 501});
    assert.equal((await recovered.getManualTripProjectionReceipt(23, 58)).event_id, original.event_id);
    const next = await recovered.enqueue({event_id: "next-after-ack", event_type: "driver.trip.unloaded", trip_id: 501, payload: {trip_id: 501}});
    assert.equal(next.sequence, 206);
    assert.equal(next.device_id, original.device_id);
});

test("Driver migration keeps the primary cleared-trip tombstone instead of reviving an older fallback load", async () => {
    const indexedDB = fakeIndexedDB();
    const send = async batch => ({results: batch.events.map(event => ({
        event_id: event.event_id, status: "accepted", server_ids: {trip_id: 501},
    }))});
    const primary = runtime({local: storage(), indexedDB, send});
    await primary.enqueue(manualLoad());
    await primary.flush();
    await primary.enqueue({event_id: "primary-unloaded", event_type: "driver.trip.unloaded", trip_id: 501, payload: {trip_id: 501}});
    await primary.flush();
    assert.equal(await primary.getManualTripProjectionReceipt(23, 58), null);
    const local = storage();
    const fallback = runtime({local, send});
    await fallback.enqueue(manualLoad());
    await fallback.flush();
    assert.equal((await fallback.getManualTripProjectionReceipt(23, 58)).event_type, "driver.trip.loaded");
    const recovered = runtime({local, indexedDB, send});
    assert.equal(await recovered.getManualTripProjectionReceipt(23, 58), null);
    assert.equal((await recovered.journal()).length, 2);
    assert.deepEqual(await recovered.pending(), []);
});

test("Driver migration quota leaves raw fallback recoverable and never switches away from primary facts", async () => {
    const local = storage();
    let quota = false;
    const indexedDB = fakeIndexedDB({failPut: (store, event) => {
        if (quota && store === "events" && event.event_id === "fallback-quota") throw new Error("migration quota");
    }});
    const primary = runtime({local, indexedDB});
    await primary.enqueue({event_id: "primary-visible", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    const fallback = runtime({local});
    await fallback.enqueue({event_id: "fallback-quota", event_type: "driver.trip.unloaded", trip_id: 2, payload: {trip_id: 2}});
    const raw = local.getItem("driver-offline-events-v2:7");
    quota = true;
    let state;
    const failed = runtime({local, indexedDB, onState: value => { state = value; }});
    await failed.publish();
    assert.equal(state.storage, "indexedDB");
    assert.equal(state.migration_error, "migration quota");
    assert.deepEqual(state.events.map(event => event.event_id), ["primary-visible"]);
    assert.equal(local.getItem("driver-offline-events-v2:7"), raw);
    quota = false;
    const restarted = runtime({local, indexedDB});
    assert.deepEqual((await restarted.pending()).map(event => event.event_id), ["primary-visible", "fallback-quota"]);
});

test("Driver migration never overwrites a different immutable event with the same ID", async () => {
    const local = storage();
    const indexedDB = fakeIndexedDB();
    const primary = runtime({local, indexedDB});
    await primary.enqueue({event_id: "id-collision", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    const fallback = runtime({local});
    await fallback.enqueue({event_id: "id-collision", event_type: "driver.trip.unloaded", trip_id: 2, payload: {trip_id: 2}});
    const raw = local.getItem("driver-offline-events-v2:7");
    let state;
    const recovered = runtime({local, indexedDB, onState: value => { state = value; }});
    await recovered.publish();
    assert.equal(state.events[0].trip_id, 1);
    assert.equal(state.migration_error, "offline_migration_identity_conflict");
    assert.equal(local.getItem("driver-offline-events-v2:7"), raw);
});

test("corrupt Driver fallback queue or metadata stays raw while the recovered primary remains usable", async () => {
    for (const suffix of ["", ":meta"]) {
        const indexedDB = fakeIndexedDB();
        const primary = runtime({local: storage(), indexedDB});
        await primary.enqueue({event_id: "primary-valid", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
        const local = storage();
        const key = "driver-offline-events-v2:7" + suffix;
        local.setItem(key, "{legacy raw");
        let state;
        const recovered = runtime({local, indexedDB, onState: value => { state = value; }});
        await recovered.publish();
        assert.equal(state.storage, "indexedDB");
        assert.equal(state.events[0].event_id, "primary-valid");
        assert.ok(state.migration_error);
        assert.equal(local.getItem(key), "{legacy raw");
    }
});

test("Driver migration cleanup preserves a new fallback fact appended while IndexedDB was writing", async () => {
    const local = storage();
    const key = "driver-offline-events-v2:7";
    const fallback = runtime({local});
    const original = await fallback.enqueue({event_id: "importing", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    const concurrent = {...original, event_id: "appended-during-import", trip_id: 2, payload: {trip_id: 2}, sequence: original.sequence + 1};
    let appended = false;
    const indexedDB = fakeIndexedDB({failPut: (store, event) => {
        if (!appended && store === "events" && event.event_id === original.event_id) {
            appended = true;
            local.setItem(key, JSON.stringify([...JSON.parse(local.getItem(key)), concurrent]));
        }
    }});
    const recovered = runtime({local, indexedDB});
    assert.equal((await recovered.pending())[0].event_id, original.event_id);
    assert.deepEqual((await recovered.journal()).map(event => event.event_id), [original.event_id, concurrent.event_id]);
    assert.deepEqual(JSON.parse(local.getItem(key)), []);
    const next = runtime({local, indexedDB});
    assert.deepEqual((await next.pending()).map(event => event.event_id), [original.event_id, concurrent.event_id]);
});

test("Driver legacy identity receipts without pending records keep immutable duplicate protection after migration", async () => {
    const local = storage();
    const old = runtime({local});
    const spec = {event_id: "old-accepted", event_type: "driver.trip.unloaded", trip_id: 9, occurred_at: "2026-10-01T10:00:00Z", payload: {trip_id: 9}};
    await old.enqueue(spec);
    await old.flush();
    // Старый клиент до долговечного журнала удалял принятый исходник из events.
    local.setItem("driver-offline-events-v2:7", "[]");
    const recovered = runtime({local, indexedDB: fakeIndexedDB()});
    assert.equal((await recovered.enqueue(spec)).state, "confirmed");
    await assert.rejects(recovered.enqueue({...spec, trip_id: 10, payload: {trip_id: 10}}), /offline_event_id_reused/);
    assert.deepEqual(await recovered.pending(), []);
});

test("event is durably written with full driver context before send", async () => {
    const calls = [];
    const box = runtime({send: async batch => { calls.push(batch); throw new Error("offline"); }});
    const event = await box.enqueue({
        event_id: "unload-1",
        event_type: "driver.trip.unloaded",
        trip_id: 91,
        occurred_at: "2026-09-13T08:11:12.000Z",
        payload: {trip_id: 91},
    });
    assert.equal(calls.length, 0);
    assert.equal(event.format_version, 1);
    assert.equal(event.actor_id, 11);
    assert.equal(event.access_id, 7);
    assert.equal(event.role_code, "driver");
    assert.equal(event.device_id, "install-uuid-1");
    assert.equal(event.shift_id, 23);
    assert.equal(event.equipment_id, 58);
    assert.equal(event.trip_id, 91);
    assert.equal(event.sequence, 1);
    assert.equal((await box.pending()).length, 1);
    await box.flush();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].protocol_version, 1);
    assert.equal(calls[0].format_version, 1);
    assert.equal(calls[0].role_code, "driver");
    assert.equal(calls[0].device_id, "install-uuid-1");
    assert.equal(calls[0].events[0].occurred_at, "2026-09-13T08:11:12.000Z");
});

test("only exact accepted and deduplicated acknowledgements remove events", async () => {
    let response = {results: [
        {event_id: "unload-1", status: "accepted"},
        {event_id: "point-1", status: "deduplicated"},
    ]};
    const box = runtime({send: async () => response});
    await box.enqueue({event_id: "unload-1", event_type: "driver.trip.unloaded", trip_id: 91, payload: {trip_id: 91}});
    await box.enqueue({event_id: "point-1", event_type: "driver.trip.dump_point_changed", trip_id: 92, payload: {dump_point_id: 4}});
    await box.flush();
    assert.deepEqual(await box.pending(), []);
});

test("partial response retains the unacknowledged event with retry metadata", async () => {
    const box = runtime({send: async () => ({results: [{event_id: "first", status: "accepted"}]})});
    await box.enqueue({event_id: "first", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    await box.enqueue({event_id: "second", event_type: "driver.trip.unloaded", trip_id: 2, payload: {trip_id: 2}});
    await box.flush();
    const remaining = await box.pending();
    assert.equal(remaining.length, 1);
    assert.equal(remaining[0].event_id, "second");
    assert.equal(remaining[0].last_error.code, "missing_ack");
    assert.equal(remaining[0].attempt_count, 1);
});

test("conflict auth and invalid records remain for reconciliation and are not retried", async () => {
    let calls = 0;
    const statuses = ["conflict", "auth_required", "invalid"];
    const box = runtime({send: async batch => {
        calls += 1;
        return {results: batch.events.map((event, index) => ({event_id: event.event_id, status: statuses[index], message: "review"}))};
    }});
    for (let index = 0; index < statuses.length; index += 1) {
        await box.enqueue({event_id: "event-" + index, event_type: "driver.downtime.started", payload: {reason_id: index + 1}});
    }
    await box.flush();
    await box.flush();
    assert.equal(calls, 1);
    assert.deepEqual((await box.pending()).map(event => event.state), statuses);
});

test("parallel flush calls share one request and preserve order and dependency", async () => {
    let release;
    let calls = 0;
    const box = runtime({send: batch => {
        calls += 1;
        assert.deepEqual(batch.events.map(event => event.event_id), ["start", "stop"]);
        assert.deepEqual(batch.events[1].depends_on, ["start"]);
        return new Promise(resolve => { release = () => resolve({results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))}); });
    }});
    await box.enqueue({event_id: "start", event_type: "driver.downtime.started", payload: {reason_id: 3}});
    await box.enqueue({event_id: "stop", event_type: "driver.downtime.ended", depends_on: ["start"], payload: {local_downtime_id: "start"}});
    const first = box.flush();
    const second = box.flush();
    await new Promise(resolve => setImmediate(resolve));
    release();
    await Promise.all([first, second]);
    assert.equal(calls, 1);
});

test("a due terminal action pulls its locally pending dependency into the same batch", async () => {
    const batches = [];
    let call = 0;
    const box = runtime({send: async batch => {
        batches.push(batch.events.map(event => event.event_id));
        call += 1;
        if (call === 1) {
            return {results: [{
                event_id: "point-before-unload",
                status: "retry",
                code: "temporary",
                message: "retry parent",
            }]};
        }
        return {results: batch.events.map(event => ({
            event_id: event.event_id,
            status: "accepted",
        }))};
    }});
    await box.enqueue({
        event_id: "point-before-unload",
        event_type: "driver.trip.dump_point_changed",
        trip_id: 91,
        payload: {dump_point_id: 4},
    });
    await box.flush();
    await box.enqueue({
        event_id: "unload-after-point",
        event_type: "driver.trip.unloaded",
        trip_id: 91,
        depends_on: ["point-before-unload"],
        payload: {trip_id: 91},
    });
    await box.flush();

    assert.deepEqual(batches, [
        ["point-before-unload"],
        ["point-before-unload", "unload-after-point"],
    ]);
    assert.deepEqual(await box.pending(), []);
});

test("parallel gestures receive a stable monotonic order", async () => {
    const box = runtime();
    await Promise.all([
        box.enqueue({event_id: "one", event_type: "driver.trip.dump_point_changed", trip_id: 9, payload: {dump_point_id: 1}}),
        box.enqueue({event_id: "two", event_type: "driver.trip.dump_point_changed", trip_id: 9, payload: {dump_point_id: 2}}),
    ]);
    assert.deepEqual((await box.pending()).map(event => event.sequence), [1, 2]);
});

test("replacing Driver access does not reuse a device sequence", async () => {
    const local = storage();
    const first = runtime({local, accessId: 7});
    const firstEvent = await first.enqueue({
        event_id: "before-access-replacement",
        event_type: "driver.downtime.started",
        payload: {reason_id: 1},
    });
    const second = runtime({local, accessId: 8, context: {
        actorId: 11,
        accessId: 8,
        shiftId: 23,
        equipmentId: 58,
        deviceId: "install-uuid-1",
    }});
    const secondEvent = await second.enqueue({
        event_id: "after-access-replacement",
        event_type: "driver.downtime.started",
        payload: {reason_id: 1},
    });
    assert.equal(firstEvent.sequence, 1);
    assert.equal(secondEvent.sequence, 2);
});

test("legacy unload queue migrates without changing event identity or fact time", async () => {
    const local = storage();
    local.setItem("driver-unload-outbox-v1:7", JSON.stringify([{
        trip_id: "91",
        client_action_id: "legacy-unload-91",
        occurred_at: "2026-09-11T10:00:00Z",
    }]));
    const box = runtime({local, send: async () => { throw new Error("offline"); }});
    await box.initialize();
    const [event] = await box.pending();
    assert.equal(event.event_id, "legacy-unload-91");
    assert.equal(event.trip_id, 91);
    assert.equal(event.occurred_at, "2026-09-11T10:00:00Z");
    assert.equal(local.getItem("driver-unload-outbox-v1:7"), null);
});

test("same event id cannot be silently reused with different content", async () => {
    const box = runtime();
    await box.enqueue({event_id: "same", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    await assert.rejects(
        box.enqueue({event_id: "same", event_type: "driver.trip.unloaded", trip_id: 2, payload: {trip_id: 2}}),
        /offline_event_id_reused/
    );
});

test("acknowledged identity tombstone prevents incompatible id reuse", async () => {
    const box = runtime();
    const original = {
        event_id: "acked-id",
        event_type: "driver.trip.unloaded",
        trip_id: 1,
        occurred_at: "2026-09-13T11:00:00Z",
        payload: {trip_id: 1},
    };
    await box.enqueue(original);
    await box.flush();
    const duplicate = await box.enqueue(original);
    assert.equal(duplicate.state, "confirmed");
    assert.equal((await box.pending()).length, 0);
    await assert.rejects(box.enqueue({...original, trip_id: 2, payload: {trip_id: 2}}), /offline_event_id_reused/);
});

test("retry backoff is bounded to 5-10s while online, not the old 5-minute cap", () => {
    // Раньше пауза между повторами росла до 5 минут (backoff(20) === 300000) —
    // на нестабильной, но живой связи водитель мог не увидеть подтверждение
    // своих действий у машиниста/на пульте минутами (26.09.2026).
    assert.equal(backoff(1), 5000);
    assert.equal(backoff(20), 8000);
});

test("regaining connectivity retries immediately instead of waiting out a stale backoff", async () => {
    // Раньше 'online' просто звал flush(), а flush() пропускает события, чьё
    // next_retry_at ещё не наступило — назначенное офлайн-попытками на минуты
    // вперёд время пережидалось полностью, хотя связь уже вернулась.
    let attempts = 0;
    const box = runtime({
        send: async () => {
            attempts += 1;
            if (attempts === 1) throw new Error("offline");
            return {results: [{event_id: "reconnect-1", status: "accepted"}]};
        },
    });
    await box.enqueue({
        event_id: "reconnect-1",
        event_type: "driver.trip.unloaded",
        trip_id: 1,
        payload: {trip_id: 1},
    });
    await box.flush();
    assert.equal(attempts, 1);
    const [scheduled] = await box.pending();
    assert.ok(Number(scheduled.next_retry_at) > Date.now());

    await box.retryNow();

    assert.equal(attempts, 2);
    assert.equal((await box.pending()).length, 0);
});

test("accepted callback runs only after durable removal and published zero state", async () => {
    const timeline = [];
    let callbackResolve;
    const callbackDone = new Promise(resolve => { callbackResolve = resolve; });
    let box;
    box = runtime({
        onState: state => timeline.push("state:" + state.pending),
        onConfirmed: async () => {
            timeline.push("confirmed:" + (await box.pending()).length);
            callbackResolve();
        },
    });
    await box.enqueue({event_id: "confirmed-1", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    await box.flush();
    await callbackDone;
    const confirmedIndex = timeline.indexOf("confirmed:0");
    assert.ok(confirmedIndex > timeline.lastIndexOf("state:1"));
    assert.ok(timeline.slice(0, confirmedIndex).includes("state:0"));
});

test("shell replacement rebinds callbacks on the existing durable runtime", async () => {
    let oldCalls = 0;
    let newCalls = 0;
    const box = runtime({onConfirmed: () => { oldCalls += 1; }});
    await box.setBindings({onConfirmed: () => { newCalls += 1; }});
    await box.enqueue({event_id: "rebind-1", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    await box.flush();
    assert.equal(oldCalls, 0);
    assert.equal(newCalls, 1);
});

test("more than one batch drains without another lifecycle signal", async () => {
    const batchSizes = [];
    const box = runtime({
        batchSize: 20,
        send: async batch => {
            batchSizes.push(batch.events.length);
            return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
        },
    });
    for (let index = 0; index < 45; index += 1) {
        await box.enqueue({event_id: "bulk-" + index, event_type: "driver.downtime.started", payload: {reason_id: 1}});
    }
    await box.flush();
    assert.deepEqual(batchSizes, [20, 20, 5]);
    assert.equal((await box.pending()).length, 0);
});

test("batch envelope keeps the immutable device identity of every event", async () => {
    const batches = [];
    const box = runtime({send: async batch => {
        batches.push({device: batch.device_id, eventDevices: batch.events.map(event => event.device_id)});
        return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
    }});
    await box.enqueue({event_id: "device-a-event", event_type: "driver.downtime.started", device_id: "device-a", payload: {reason_id: 1}});
    await box.enqueue({event_id: "device-b-event", event_type: "driver.downtime.started", device_id: "device-b", payload: {reason_id: 1}});
    await box.flush();
    assert.deepEqual(batches, [
        {device: "device-a", eventDevices: ["device-a"]},
        {device: "device-b", eventDevices: ["device-b"]},
    ]);
});

test("event enqueued during an in-flight send is drained by the same flush", async () => {
    let releaseFirst;
    const sent = [];
    const box = runtime({send: batch => {
        sent.push(batch.events.map(event => event.event_id));
        if (sent.length === 1) {
            return new Promise(resolve => {
                releaseFirst = () => resolve({results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))});
            });
        }
        return Promise.resolve({results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))});
    }});
    await box.enqueue({event_id: "flight-1", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    const flushing = box.flush();
    await new Promise(resolve => setImmediate(resolve));
    await box.enqueue({event_id: "flight-2", event_type: "driver.trip.unloaded", trip_id: 2, payload: {trip_id: 2}});
    releaseFirst();
    await flushing;
    assert.deepEqual(sent, [["flight-1"], ["flight-2"]]);
});

test("backoff and terminal review do not head-of-line block an independent event", async () => {
    const sent = [];
    const box = runtime({send: async batch => {
        sent.push(batch.events.map(event => event.event_id));
        if (sent.length === 1) {
            return {results: [
                {event_id: "retry-head", status: "retry"},
                {event_id: "review-head", status: "conflict"},
            ]};
        }
        return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
    }});
    await box.enqueue({event_id: "retry-head", event_type: "driver.downtime.started", payload: {reason_id: 1}});
    await box.enqueue({event_id: "review-head", event_type: "driver.downtime.started", payload: {reason_id: 2}});
    await box.flush();
    await box.enqueue({event_id: "independent", event_type: "driver.downtime.started", payload: {reason_id: 3}});
    await box.flush();
    assert.deepEqual(sent, [["retry-head", "review-head"], ["independent"]]);
    assert.deepEqual((await box.pending()).map(event => event.state), ["pending", "conflict"]);
});

test("auth-required events resume only under a fresh authenticated generation", async () => {
    let generation = "login-1";
    let sends = 0;
    const context = () => ({actorId: 11, accessId: 7, shiftId: 23, equipmentId: 58, deviceId: "install-uuid-1", authGeneration: generation});
    const box = runtime({
        context,
        send: async batch => {
            sends += 1;
            return {results: batch.events.map(event => ({event_id: event.event_id, status: sends === 1 ? "auth_required" : "accepted"}))};
        },
    });
    await box.enqueue({event_id: "auth-1", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
    await box.flush();
    await box.resumeAuthRequired("login-1");
    await box.flush();
    assert.equal(sends, 1);
    generation = "login-2";
    await box.setBindings({context});
    await box.resumeAuthRequired(generation);
    await box.flush();
    assert.equal(sends, 2);
    assert.equal((await box.pending()).length, 0);
});

test("immutable event identity includes context references time dependencies and payload", async () => {
    const original = {
        event_id: "immutable",
        event_type: "driver.trip.dump_point_changed",
        actor_id: 11,
        access_id: 7,
        device_id: "device-a",
        shift_id: 23,
        equipment_id: 58,
        trip_id: 91,
        local_trip_id: "local-trip",
        occurred_at: "2026-09-13T10:00:00Z",
        depends_on: ["before"],
        payload: {dump_point_id: 4, expected_actual_dump_point_id: 3},
    };
    const sameBox = runtime();
    await sameBox.enqueue(original);
    const same = await sameBox.enqueue({...original, payload: {expected_actual_dump_point_id: 3, dump_point_id: 4}});
    assert.equal(same.sequence, 1);
    const mutations = [
        {actor_id: 12}, {device_id: "device-b"}, {shift_id: 24}, {equipment_id: 59},
        {trip_id: 92}, {local_trip_id: "other-local"}, {occurred_at: "2026-09-13T10:00:01Z"},
        {depends_on: ["other"]}, {payload: {dump_point_id: 5, expected_actual_dump_point_id: 3}},
    ];
    for (const mutation of mutations) {
        const box = runtime();
        await box.enqueue(original);
        await assert.rejects(box.enqueue({...original, ...mutation}), /offline_event_id_reused/);
    }
});

test("downtime start acknowledgement keeps the exact server mapping for close", async () => {
    const box = runtime({send: async batch => ({results: batch.events.map(event => ({
        event_id: event.event_id,
        status: "accepted",
        server_ids: {downtime_event_id: 501},
    }))})});
    await box.enqueue({event_id: "down-start", event_type: "driver.downtime.started", payload: {reason_id: 4}});
    await box.flush();
    assert.deepEqual(await box.getServerMapping("down-start"), {downtime_event_id: 501});
    const serverClose = createDriverDowntimeEndEvent({eventId: "close-server", serverId: 501, occurredAt: "2026-09-13T11:30:00Z"});
    assert.deepEqual(serverClose.depends_on, []);
    assert.deepEqual(serverClose.payload, {downtime_id: 501, local_downtime_id: null});
    const localClose = createDriverDowntimeEndEvent({eventId: "close-local", pendingStartId: "down-local", occurredAt: "2026-09-13T11:31:00Z"});
    assert.deepEqual(localClose.depends_on, ["down-local"]);
    assert.deepEqual(localClose.payload, {downtime_id: null, local_downtime_id: "down-local"});
});

test("offline downtime start and close share the exact local id before sync", async () => {
    let wire;
    const box = runtime({send: async batch => {
        wire = batch.events;
        return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
    }});
    const start = await box.enqueue({
        event_id: "local-start-1",
        event_type: "driver.downtime.started",
        payload: {reason_id: 4},
    });
    const closeSpec = createDriverDowntimeEndEvent({
        eventId: "local-close-1",
        pendingStartId: start.event_id,
        occurredAt: "2026-09-13T11:31:00Z",
    });
    await box.enqueue(closeSpec);
    await box.flush();
    assert.equal(wire[0].local_downtime_id, "local-start-1");
    assert.equal(wire[1].local_downtime_id, "local-start-1");
    assert.deepEqual(wire[1].depends_on, ["local-start-1"]);
    assert.equal(wire[1].payload.local_downtime_id, "local-start-1");
});

test("accepted downtime start maps a quick close to the exact server id", async () => {
    const batches = [];
    const box = runtime({send: async batch => {
        batches.push(batch.events);
        return {results: batch.events.map(event => ({
            event_id: event.event_id,
            status: "accepted",
            server_ids: event.event_type === "driver.downtime.started" ? {downtime_event_id: 702} : {},
        }))};
    }});
    await box.enqueue({event_id: "quick-start", event_type: "driver.downtime.started", payload: {reason_id: 4}});
    await box.flush();
    const mapping = await box.getServerMapping("quick-start");
    await box.enqueue(createDriverDowntimeEndEvent({eventId: "quick-close", serverId: mapping.downtime_event_id}));
    await box.flush();
    const close = batches[1][0];
    assert.equal(close.payload.downtime_id, 702);
    assert.equal(close.payload.local_downtime_id, null);
    assert.equal(close.local_downtime_id, null);
    assert.deepEqual(close.depends_on, []);
});

test("A to B to A point changes always get new ids CAS state and dependencies", () => {
    const first = createDriverPointChangeEvent({tripId: 91, pointId: 2, currentPointId: 1, events: []});
    const second = createDriverPointChangeEvent({tripId: 91, pointId: 1, currentPointId: 1, events: [{...first, state: "pending"}]});
    assert.notEqual(first.event_id, second.event_id);
    assert.deepEqual(first.depends_on, []);
    assert.equal(first.payload.expected_actual_dump_point_id, 1);
    assert.deepEqual(second.depends_on, [first.event_id]);
    assert.equal(second.payload.expected_actual_dump_point_id, 2);
});

test("free bucket selection captures the exact truck excavator and catalog snapshot", async () => {
    const box = runtime({send: async () => { throw new Error("offline"); }});
    const contextSnapshot = {
        excavator_id: 17,
        excavator_label: "ЭКГ-17",
        loading_horizon: "Горизонт 210",
        actor_id: 999,
        access_id: 999,
        role_code: "dispatcher",
    };
    const selected = createDriverFreeBucketSelectedEvent({
        eventId: "driver-free-select-1",
        truckId: 58,
        excavatorId: 17,
        catalogVersion: 41,
        catalogGeneratedAt: "2026-09-14T10:00:00Z",
        contextSnapshot,
        occurredAt: "2026-09-14T10:01:00Z",
    });
    contextSnapshot.excavator_label = "mutated-after-build";
    const stored = await box.enqueue(selected);
    assert.equal(stored.event_type, "driver.free_bucket.selected");
    assert.equal(stored.trip_id, null);
    assert.deepEqual(stored.depends_on, []);
    assert.deepEqual(stored.payload, {
        truck_id: 58,
        excavator_id: 17,
        catalog_version: 41,
        catalog_generated_at: "2026-09-14T10:00:00Z",
    });
    assert.deepEqual(stored.context_snapshot, {
        excavator_id: 17,
        excavator_label: "ЭКГ-17",
        loading_horizon: "Горизонт 210",
        actor_id: 11,
        access_id: 7,
        role_code: "driver",
    });
    assert.equal(stored.actor_id, 11);
    assert.equal(stored.access_id, 7);
    assert.equal(stored.role_code, "driver");
    assert.equal(stored.occurred_at, "2026-09-14T10:01:00Z");
    assert.equal((await box.pending()).length, 1);
    await assert.rejects(
        box.enqueue({...selected, context_snapshot: {...selected.context_snapshot, excavator_label: "other"}}),
        /offline_event_id_reused/
    );
});

test("free bucket cancellation depends only on an unresolved local selection", () => {
    const local = createDriverFreeBucketCancelledEvent({
        eventId: "driver-free-cancel-local",
        localAcceptanceId: "driver-free-select-1",
    });
    assert.deepEqual(local.depends_on, ["driver-free-select-1"]);
    assert.deepEqual(local.payload, {
        free_bucket_acceptance_id: null,
        free_bucket_acceptance_local_id: "driver-free-select-1",
    });

    const confirmed = createDriverFreeBucketCancelledEvent({
        eventId: "driver-free-cancel-server",
        acceptanceId: 701,
        localAcceptanceId: "driver-free-select-origin-device",
    });
    assert.deepEqual(confirmed.depends_on, []);
    assert.deepEqual(confirmed.payload, {
        free_bucket_acceptance_id: 701,
        free_bucket_acceptance_local_id: null,
    });
});

test("actual IndexedDB repository path survives a new runtime instance", async () => {
    const indexedDB = fakeIndexedDB();
    const first = runtime({indexedDB, send: async () => { throw new Error("offline"); }});
    await first.enqueue({event_id: "idb-1", event_type: "driver.trip.unloaded", trip_id: 91, payload: {trip_id: 91}});
    await first.flush();
    const second = runtime({indexedDB, send: async () => { throw new Error("offline"); }});
    const [restored] = await second.pending();
    assert.equal(restored.event_id, "idb-1");
    assert.equal(restored.attempt_count, 1);
});

test("a shift opened on the phone without a network is queued and later actions reference it", async () => {
    /* Владелец, 30.09.2026: смена открывается по нажатию, без ожидания сервера.
       Открытие — событие очереди; действия в этой смене несут local_shift_id и
       зависят от открытия, пока у смены нет серверного ID. */
    const sent = [];
    const context = {actorId: 11, accessId: 7, shiftId: "", localShiftId: "", equipmentId: 58, deviceId: "install-uuid-1"};
    const box = runtime({
        context,
        send: async batch => { sent.push(batch); throw new Error("offline"); },
    });
    const opening = await box.enqueue({
        event_id: "driver-shift-open:one",
        event_type: "driver.shift.opened",
        local_shift_id: "driver-shift-open:one",
        payload: {truck_id: 58, start_fuel: "410", start_mileage: "12000", start_engine_hours: "3000"},
    });
    assert.equal(opening.shift_id, null);
    assert.equal(opening.local_shift_id, "driver-shift-open:one");
    assert.deepEqual(opening.depends_on, []);

    context.shiftId = "driver-shift-open:one";
    context.localShiftId = "driver-shift-open:one";
    const downtime = await box.enqueue({
        event_id: "downtime-in-local-shift",
        event_type: "driver.downtime.started",
        payload: {reason_id: 3},
    });
    assert.equal(downtime.shift_id, null);
    assert.equal(downtime.local_shift_id, "driver-shift-open:one");
    assert.deepEqual(downtime.depends_on, ["driver-shift-open:one"]);
    assert.equal(downtime.payload.local_shift_id, "driver-shift-open:one");

    context.shiftId = "44";
    context.localShiftId = "";
    const later = await box.enqueue({
        event_id: "downtime-after-server-id",
        event_type: "driver.downtime.started",
        payload: {reason_id: 3},
    });
    assert.equal(later.shift_id, 44);
    assert.equal(later.local_shift_id, null);
    assert.deepEqual(later.depends_on, []);
    assert.equal(later.payload.local_shift_id, undefined);
});

test("a shift opening must name itself as the local shift and its own truck", async () => {
    const box = runtime({context: {actorId: 11, accessId: 7, shiftId: "", equipmentId: 58, deviceId: "install-uuid-1"}});
    await assert.rejects(
        box.enqueue({
            event_id: "driver-shift-open:x", event_type: "driver.shift.opened",
            local_shift_id: "driver-shift-open:other", payload: {truck_id: 58},
        }),
        /offline_shift_open_identity_invalid/
    );
    await assert.rejects(
        box.enqueue({
            event_id: "driver-shift-open:y", event_type: "driver.shift.opened",
            local_shift_id: "driver-shift-open:y", payload: {truck_id: 59},
        }),
        /offline_shift_open_truck_mismatch/
    );
    await assert.rejects(
        box.enqueue({event_id: "no-shift", event_type: "driver.downtime.started", payload: {reason_id: 1}}),
        /offline_event_context_incomplete/
    );
});

test("local guards reject ungranted or incomplete driver actions before storage", async () => {
    const box = runtime();
    await assert.rejects(
        box.enqueue({event_id: "open", event_type: "driver.shift.reopened", payload: {}}),
        /offline_event_type_not_permitted/
    );
    await assert.rejects(
        box.enqueue({event_id: "trip", event_type: "driver.trip.unloaded", payload: {trip_id: 1}}),
        /offline_trip_required/
    );
    await assert.rejects(
        box.enqueue({event_id: "reason", event_type: "driver.downtime.started", payload: {}}),
        /offline_downtime_reason_required/
    );
    await assert.rejects(
        box.enqueue({event_id: "access", event_type: "driver.downtime.started", access_id: 8, payload: {reason_id: 1}}),
        /offline_event_access_mismatch/
    );
    await assert.rejects(
        box.enqueue({event_id: "free-incomplete", event_type: "driver.free_bucket.selected", payload: {truck_id: 58}}),
        /offline_free_bucket_context_incomplete/
    );
    await assert.rejects(
        box.enqueue({event_id: "free-wrong-truck", event_type: "driver.free_bucket.selected", payload: {truck_id: 59, excavator_id: 17}}),
        /offline_free_bucket_truck_mismatch/
    );
    await assert.rejects(
        box.enqueue({event_id: "free-cancel-unbound", event_type: "driver.free_bucket.cancelled", payload: {free_bucket_acceptance_local_id: "selection-1"}}),
        /offline_free_bucket_acceptance_required/
    );
    await assert.rejects(
        box.enqueue({event_id: "free-cancel-server-dependent", event_type: "driver.free_bucket.cancelled", depends_on: ["other-device"], payload: {free_bucket_acceptance_id: 701}}),
        /offline_free_bucket_server_reference_dependency/
    );
    await assert.rejects(
        box.enqueue({event_id: "free-cancel-ambiguous", event_type: "driver.free_bucket.cancelled", payload: {free_bucket_acceptance_id: 701, free_bucket_acceptance_local_id: "selection-1"}}),
        /offline_free_bucket_acceptance_ambiguous/
    );
    assert.equal((await box.pending()).length, 0);
});

test("closing a downtime is always queued, even with no reference to its start yet", async () => {
    // Раньше здесь требовали ссылку на начало простоя до постановки закрытия в
    // очередь — водитель мог остаться без возможности завершить простой на
    // нестабильной связи. Телефон обязан принять закрытие всегда; сервер сам
    // свяжет его с началом, когда оно синхронизуется.
    const box = runtime();
    const queued = await box.enqueue({
        event_id: "end-no-reference",
        event_type: "driver.downtime.ended",
        payload: {}
    });
    assert.equal(queued.event_id, "end-no-reference");
    assert.equal((await box.pending()).length, 1);
});

test("auth classifier recognizes status redirect to root and login HTML", () => {
    const headers = type => ({get() { return type; }});
    assert.equal(isDriverSyncAuthResponse({status: 401}, "", "https://driverform.ru/driver/"), true);
    assert.equal(isDriverSyncAuthResponse({status: 200, redirected: true, url: "https://driverform.ru/", headers: headers("text/html")}, "", "https://driverform.ru/driver/"), true);
    assert.equal(isDriverSyncAuthResponse({status: 200, redirected: false, url: "https://driverform.ru/offline-events/sync/", headers: headers("text/html")}, '<main data-mobile-role-login></main>', "https://driverform.ru/driver/"), true);
    assert.equal(isDriverSyncAuthResponse({status: 200, redirected: false, url: "https://driverform.ru/offline-events/sync/", headers: headers("application/json")}, '{"ok":true}', "https://driverform.ru/driver/"), false);
});

test("old terminal records leave the delivery indicator but remain in the durable journal", async () => {
    /* 20.09.2026: на боевом телефоне лежали восемь отклонённых стартов простоя
       трёхдневной давности — подпись связи вечно показывала «Не подтверждено». */
    const local = storage();
    const conflictSend = async batch => ({
        results: batch.events.map(event => ({event_id: event.event_id, status: "conflict", message: "review"})),
    });
    const box = createDriverOfflineOutbox({
        repository: localRepository(local, 7), localStorage: local, accessId: 7,
        context: {actorId: 11, accessId: 7, shiftId: 23, equipmentId: 58, deviceId: "install-uuid-1"},
        send: conflictSend, reviewRetentionMs: 30,
    });
    await box.enqueue({event_id: "old-conflict", event_type: "driver.downtime.started", payload: {reason_id: 1}});
    await box.flush();
    assert.deepEqual((await box.pending()).map(event => event.state), ["conflict"]);
    await new Promise(resolve => setTimeout(resolve, 60));
    await box.enqueue({event_id: "fresh-conflict", event_type: "driver.downtime.started", payload: {reason_id: 2}});
    await box.flush();
    assert.deepEqual((await box.pending()).map(event => event.event_id), ["fresh-conflict"], "старая запись убрана, свежая осталась");
    const restarted = runtime({local});
    assert.deepEqual((await restarted.journal()).map(event => event.event_id), ["old-conflict", "fresh-conflict"]);
    assert.equal((await restarted.journal())[0].payload.reason_id, 1);
});

test("accepted source and full ACK survive restart before a manifest callback or server snapshot", async () => {
    for (const indexedDB of [undefined, fakeIndexedDB()]) {
        const local = storage();
        const result = {status: "accepted", server_ids: {trip_id: 802}, server_received_at: "2026-10-04T10:00:00Z"};
        const first = runtime({local, indexedDB, send: async batch => ({results: batch.events.map(event => ({event_id: event.event_id, ...result}))})});
        const source = await first.enqueue(manualLoad());
        await first.flush();
        assert.deepEqual(await first.pending(), []);
        const restarted = runtime({local, indexedDB});
        const [archived] = await restarted.journal();
        assert.equal(archived.state, "confirmed");
        for (const field of ["event_id", "actor_id", "access_id", "occurred_at", "payload", "depends_on", "context_snapshot"]) {
            assert.deepEqual(archived[field], source[field], field);
        }
        assert.equal(archived.server_result.server_ids.trip_id, 802);
        let published;
        await restarted.setBindings({onState: state => { published = state; }});
        assert.equal(published.pending, 0);
        assert.equal(published.journalEvents[0].event_id, source.event_id);
    }
});

test("archive quota failure leaves the exact accepted source queued and suppresses the success callback", async () => {
    const local = storage();
    const setItem = local.setItem;
    let quota = false;
    local.setItem = (key, value) => {
        if (quota && key === "driver-offline-events-v2:7" && JSON.parse(value).some(event => event.state === "confirmed")) throw new Error("quota");
        setItem(key, value);
    };
    let confirmed = 0;
    const box = runtime({local, onConfirmed: () => { confirmed += 1; }});
    const source = await box.enqueue(manualLoad());
    quota = true;
    await assert.rejects(box.flush(), /quota/);
    assert.equal(confirmed, 0);
    assert.equal((await box.pending())[0].state, "pending");
    assert.deepEqual((await box.journal())[0].payload, source.payload);
    quota = false;
    await box.flush();
    assert.equal((await box.journal())[0].state, "confirmed");
    assert.equal(confirmed, 1);
});

test("persistent ACK quota uses the bounded retry timer instead of a hot send loop", async () => {
    const oldSetTimeout = globalThis.setTimeout;
    const oldClearTimeout = globalThis.clearTimeout;
    const timers = new Map();
    let time = 0;
    let token = 0;
    globalThis.setTimeout = (callback, delay) => {
        const handle = {id: ++token, unref() {}};
        timers.set(handle, {callback, at: time + delay});
        return handle;
    };
    globalThis.clearTimeout = handle => timers.delete(handle);
    const advance = async milliseconds => {
        time += milliseconds;
        for (const [handle, timer] of [...timers]) {
            if (timer.at <= time) { timers.delete(handle); timer.callback(); }
        }
        await new Promise(resolve => setImmediate(resolve));
    };
    try {
        const local = storage();
        const setItem = local.setItem;
        let quota = false;
        local.setItem = (key, value) => {
            if (quota && key === "driver-offline-events-v2:7" && JSON.parse(value).some(event => event.state === "confirmed")) throw new Error("quota");
            return setItem(key, value);
        };
        let sends = 0;
        const oldAccepted = Number(globalThis.driverOutboxAcceptedCount) || 0;
        const box = runtime({local, send: async batch => {
            sends += 1;
            return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
        }});
        await box.enqueue(manualLoad());
        quota = true;
        await assert.rejects(box.flush(), /quota/);
        await advance(4999);
        assert.equal(sends, 1);
        assert.equal(Number(globalThis.driverOutboxAcceptedCount) || 0, oldAccepted);
        await advance(1);
        assert.equal(sends, 2);
        await advance(7999);
        assert.equal(sends, 2);
        quota = false;
        await advance(1);
        assert.equal(sends, 3);
        assert.equal((await box.journal())[0].state, "confirmed");
        assert.equal(globalThis.driverOutboxAcceptedCount, oldAccepted + 1);
    } finally {
        globalThis.setTimeout = oldSetTimeout;
        globalThis.clearTimeout = oldClearTimeout;
    }
});

test("locally cancelled unsent pair keeps both sources and cannot send its surviving half after restart", async () => {
    const local = storage();
    const repository = localRepository(local, 7);
    const put = repository.put;
    repository.put = async event => {
        if (event.event_id === "cancel-durable-pair" && event.state === "cancelled_locally") throw new Error("quota while archiving second half");
        return put(event);
    };
    const first = createDriverOfflineOutbox({repository, accessId: 7,
        context: {actorId: 11, accessId: 7, shiftId: 23, equipmentId: 58, deviceId: "install-uuid-1"},
        send: async () => { throw new Error("must not send"); }});
    const load = await first.enqueue(manualLoad());
    await first.enqueue(createDriverManualLoadCancelledEvent({eventId: "cancel-durable-pair", localTripId: load.event_id,
        loadEventId: load.event_id, truckId: 58, excavatorId: 9, dumpPointId: 4}));
    assert.deepEqual(await first.pending(), []);
    assert.equal((await first.journal()).length, 2);
    let sends = 0;
    const restarted = runtime({local, send: async () => { sends += 1; return {}; }});
    await restarted.initialize();
    assert.equal(sends, 0);
    assert.deepEqual((await restarted.journal()).map(event => event.state), ["cancelled_locally", "cancelled_locally"]);
});

test("response headers with a stalled body time out, release flush and ignore a late ACK", async () => {
    let finishBody;
    let firstSignal;
    let count = 0;
    const box = runtime({requestTimeoutMs: 15, send: (batch, request) => {
        count += 1;
        if (count > 1) return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
        firstSignal = request.signal;
        return Promise.resolve({text: () => new Promise(resolve => { finishBody = resolve; })})
            .then(response => response.text()).then(JSON.parse);
    }});
    await box.enqueue(manualLoad());
    await box.flush();
    assert.equal(firstSignal.aborted, true);
    assert.equal((await box.pending())[0].last_error.code, "network");
    finishBody(JSON.stringify({results: [{event_id: "manual-load-1", status: "conflict", code: "stale response"}]}));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal((await box.pending())[0].state, "pending");
    await box.retryNow();
    assert.equal(count, 2);
    assert.deepEqual(await box.pending(), []);
    assert.equal((await box.journal())[0].state, "confirmed");
});

test("request timeout without AbortController still allows a new local gesture and a later flush", async () => {
    const oldAbortController = globalThis.AbortController;
    globalThis.AbortController = undefined;
    try {
        let calls = 0;
        let started;
        const start = new Promise(resolve => { started = resolve; });
        const box = runtime({requestTimeoutMs: 15, send: batch => {
            calls += 1;
            if (calls === 1) { started(); return new Promise(() => {}); }
            return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
        }});
        await box.enqueue({event_id: "deadline-a", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
        const flush = box.flush();
        await start;
        await box.enqueue({event_id: "deadline-b", event_type: "driver.trip.unloaded", trip_id: 2, payload: {trip_id: 2}});
        await flush;
        assert.deepEqual(await box.pending(), []);
        assert.equal((await box.journal()).length, 2);
    } finally { globalThis.AbortController = oldAbortController; }
});

/* Часы водителя 24.09.2026: телефон с вручную отведёнными назад часами писал в
   базу своё время — простой «шёл» полчаса в ту же секунду, как его начали, а
   переключение причины и вовсе отклонялось как «раньше начала». Событие,
   ушедшее сразу после создания, теперь помечается для сервера: телефон не мог
   быть офлайн эти секунды, значит его час просто неверен. */
test("an event sent right away is marked so the server can use its own receipt time", async () => {
    const sent = [];
    const box = runtime({send: async (body) => {
        sent.push(body);
        return {results: body.events.map((event) => ({event_id: event.event_id, status: "accepted"}))};
    }});
    await box.enqueue(manualLoad());
    await box.flush();

    assert.equal(sent.length, 1);
    const wire = sent[0].events[0];
    assert.equal(wire.sent_live, true, "флаг ставится на верхнем уровне события");
    assert.equal(wire.payload.sent_live, undefined, "в payload флага нет: payload входит в отпечаток события");
    assert.equal(wire.created_session, undefined, "служебные поля очереди на сервер не уходят");
    assert.equal(wire.created_mono, undefined);
});


test("every server acceptance bumps the counter the screen refresh checks (matrix C1b)", async () => {
    globalThis.driverOutboxAcceptedCount = 0;
    const box = runtime({
        send: async batch => ({
            results: batch.events.map(event => ({
                event_id: event.event_id,
                status: event.event_id === "c1b-refused" ? "conflict" : "accepted",
                code: event.event_id === "c1b-refused" ? "equipment_context_changed" : undefined,
            })),
        }),
    });
    await box.enqueue({
        event_id: "c1b-accepted",
        event_type: "driver.downtime.started",
        occurred_at: "2026-10-01T08:48:28.237Z",
        payload: {reason_id: 9},
    });
    await box.enqueue({
        event_id: "c1b-refused",
        event_type: "driver.downtime.started",
        occurred_at: "2026-10-01T08:48:30.000Z",
        payload: {reason_id: 9},
    });
    await box.flush();
    assert.equal(globalThis.driverOutboxAcceptedCount, 1);
    delete globalThis.driverOutboxAcceptedCount;
});

test("after a network drop the queue leaves strictly by sequence (battle 02.10, seq 232)", async () => {
    // Отмена ковша ждала бэкоффа после обрыва, а свежие события ушли раньше
    // неё. Теперь всё, что ждёт только из-за сети, едет в той же пачке по порядку.
    const local = storage();
    let online = false;
    const batches = [];
    const box = runtime({
        local,
        send: async batch => {
            if (!online) throw new TypeError("Failed to fetch");
            batches.push(batch.events.map(event => event.event_id));
            return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
        },
    });
    await box.enqueue({
        event_id: "v376-cancel-first",
        event_type: "driver.downtime.started",
        occurred_at: "2026-10-01T23:35:39.000Z",
        payload: {reason_id: 9},
    });
    await box.flush();
    const waiting = (await box.pending()).find(event => event.event_id === "v376-cancel-first");
    assert.equal(waiting.last_error.code, "network");
    assert.ok(Number(waiting.next_retry_at) > Date.now());

    online = true;
    await box.enqueue({
        event_id: "v376-later",
        event_type: "driver.downtime.started",
        occurred_at: "2026-10-01T23:36:51.000Z",
        payload: {reason_id: 10},
    });
    await box.flush();

    assert.deepEqual(batches, [["v376-cancel-first", "v376-later"]]);
    assert.equal((await box.pending()).length, 0);
});

test("a server-assigned wait does not hold the rest of the queue", async () => {
    const local = storage();
    const batches = [];
    let first = true;
    const box = runtime({
        local,
        send: async batch => {
            batches.push(batch.events.map(event => event.event_id));
            return {results: batch.events.map(event => (
                first && event.event_id === "v376-server-wait"
                    ? {event_id: event.event_id, status: "retry", code: "dependency_pending", message: "Ждём."}
                    : {event_id: event.event_id, status: "accepted"}
            ))};
        },
    });
    await box.enqueue({
        event_id: "v376-server-wait",
        event_type: "driver.downtime.started",
        occurred_at: "2026-10-01T23:40:00.000Z",
        payload: {reason_id: 9},
    });
    await box.flush();
    first = false;
    await box.enqueue({
        event_id: "v376-after-server-wait",
        event_type: "driver.downtime.started",
        occurred_at: "2026-10-01T23:41:00.000Z",
        payload: {reason_id: 10},
    });
    await box.flush();
    assert.deepEqual(batches, [["v376-server-wait"], ["v376-after-server-wait"]]);
});

test("restart resends v376 refusals: load during downtime, changed face, cancel of a cancelled bucket, buried next trip", async () => {
    const local = storage();
    const codes = {
        "v376-load-downtime": "equipment_downtime_active",
        "v376-load-face": "manual_work_context_changed",
        "v376-cancel": "free_bucket_not_cancellable",
        "v376-next-load": "dependency_rejected",
    };
    const rejected = runtime({
        local,
        send: async batch => ({
            results: batch.events.map(event => ({
                event_id: event.event_id, status: "conflict", code: codes[event.event_id], message: "Отклонено.",
            })),
        }),
    });
    const manualPayload = {
        manual_control: true, truck_id: 58, excavator_id: 9, dump_point_id: 3, rock_type_id: 2,
        assignment_id: 7, free_bucket_acceptance_id: null, free_bucket_acceptance_local_id: null,
    };
    await rejected.enqueue({
        event_id: "v376-load-downtime", event_type: "driver.trip.loaded",
        occurred_at: "2026-10-01T23:36:56.000Z", local_trip_id: "v376-load-downtime", payload: manualPayload,
    });
    await rejected.enqueue({
        event_id: "v376-load-face", event_type: "driver.trip.loaded",
        occurred_at: "2026-10-01T23:37:56.000Z", local_trip_id: "v376-load-face", payload: manualPayload,
    });
    await rejected.enqueue({
        event_id: "v376-cancel", event_type: "driver.free_bucket.cancelled",
        occurred_at: "2026-10-01T23:38:00.000Z", payload: {free_bucket_acceptance_id: 631},
    });
    await rejected.enqueue({
        event_id: "v376-next-load", event_type: "driver.trip.loaded",
        occurred_at: "2026-10-01T23:39:04.000Z", local_trip_id: "v376-next-load", payload: manualPayload,
    });
    await rejected.flush();
    assert.deepEqual((await rejected.pending()).map(event => event.state), ["conflict", "conflict", "conflict", "conflict"]);

    const delivered = [];
    const restarted = runtime({
        local,
        send: async batch => {
            delivered.push(...batch.events.map(event => event.event_id));
            return {results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"}))};
        },
    });
    await restarted.initialize();
    assert.deepEqual(delivered, ["v376-load-downtime", "v376-load-face", "v376-cancel", "v376-next-load"]);
    assert.equal((await restarted.pending()).length, 0);
});
