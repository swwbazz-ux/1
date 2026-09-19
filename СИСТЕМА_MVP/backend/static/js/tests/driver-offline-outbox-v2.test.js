"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
    createDriverOfflineOutbox,
    createDriverPointChangeEvent,
    createDriverFreeBucketSelectedEvent,
    createDriverFreeBucketCancelledEvent,
    isDriverSyncAuthResponse,
    createDriverDowntimeEndEvent,
    selectDriverDowntimeProjection,
    localRepository,
    backoff,
} = require("../driver-offline-outbox-v2.js");

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

function runtime({send, local = storage(), accessId = 7, context, batchSize, onState, onConfirmed, onReview, indexedDB} = {}) {
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

function fakeIndexedDB() {
    const stores = new Map();
    function storeApi(name, transaction) {
        if (!stores.has(name)) stores.set(name, new Map());
        const values = stores.get(name);
        function request(operation) {
            const result = {onsuccess: null, onerror: null};
            queueMicrotask(() => {
                try {
                    result.result = operation();
                    if (result.onsuccess) result.onsuccess();
                    queueMicrotask(() => transaction.oncomplete && transaction.oncomplete());
                } catch (error) {
                    transaction.error = error;
                    if (result.onerror) result.onerror();
                    if (transaction.onerror) transaction.onerror();
                }
            });
            return result;
        }
        return {
            createIndex() {},
            getAll() { return request(() => Array.from(values.values()).map(value => structuredClone(value))); },
            get(key) { return request(() => values.has(key) ? structuredClone(values.get(key)) : undefined); },
            put(value, key) {
                return request(() => {
                    const resolved = key === undefined ? value.event_id : key;
                    values.set(resolved, structuredClone(value));
                    return resolved;
                });
            },
            delete(key) { return request(() => values.delete(key)); },
        };
    }
    const db = {
        objectStoreNames: {contains(name) { return stores.has(name); }},
        createObjectStore(name) {
            stores.set(name, new Map());
            return {createIndex() {}};
        },
        transaction(name) {
            const transaction = {error: null};
            transaction.objectStore = () => storeApi(name, transaction);
            return transaction;
        },
    };
    return {
        open() {
            const request = {result: db};
            queueMicrotask(() => {
                if (request.onupgradeneeded) request.onupgradeneeded();
                if (request.onsuccess) request.onsuccess();
            });
            return request;
        },
    };
}

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

test("parallel gestures receive a stable monotonic order", async () => {
    const box = runtime();
    await Promise.all([
        box.enqueue({event_id: "one", event_type: "driver.trip.dump_point_changed", trip_id: 9, payload: {dump_point_id: 1}}),
        box.enqueue({event_id: "two", event_type: "driver.trip.dump_point_changed", trip_id: 9, payload: {dump_point_id: 2}}),
    ]);
    assert.deepEqual((await box.pending()).map(event => event.sequence), [1, 2]);
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

test("retry backoff is bounded", () => {
    assert.equal(backoff(1), 5000);
    assert.equal(backoff(20), 300000);
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

test("local guards reject ungranted or incomplete driver actions before storage", async () => {
    const box = runtime();
    await assert.rejects(
        box.enqueue({event_id: "open", event_type: "driver.shift.opened", payload: {}}),
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
        box.enqueue({event_id: "end", event_type: "driver.downtime.ended", payload: {local_downtime_id: "start"}}),
        /offline_downtime_reference_required/
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

test("auth classifier recognizes status redirect to root and login HTML", () => {
    const headers = type => ({get() { return type; }});
    assert.equal(isDriverSyncAuthResponse({status: 401}, "", "https://driverform.ru/driver/"), true);
    assert.equal(isDriverSyncAuthResponse({status: 200, redirected: true, url: "https://driverform.ru/", headers: headers("text/html")}, "", "https://driverform.ru/driver/"), true);
    assert.equal(isDriverSyncAuthResponse({status: 200, redirected: false, url: "https://driverform.ru/offline-events/sync/", headers: headers("text/html")}, '<main data-mobile-role-login></main>', "https://driverform.ru/driver/"), true);
    assert.equal(isDriverSyncAuthResponse({status: 200, redirected: false, url: "https://driverform.ru/offline-events/sync/", headers: headers("application/json")}, '{"ok":true}', "https://driverform.ru/driver/"), false);
});
