"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {createDriverOfflineOutbox, localRepository, backoff} = require("../driver-offline-outbox-v2.js");

function storage() {
    const values = new Map();
    return {
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, String(value)); },
        removeItem(key) { values.delete(key); },
    };
}

function runtime({send, local = storage(), accessId = 7} = {}) {
    return createDriverOfflineOutbox({
        repository: localRepository(local, accessId),
        localStorage: local,
        accessId,
        context: {
            actorId: 11,
            accessId,
            shiftId: 23,
            equipmentId: 58,
            deviceId: "install-uuid-1",
        },
        send: send || (async batch => ({
            results: batch.events.map(event => ({event_id: event.event_id, status: "accepted"})),
        })),
    });
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

test("retry backoff is bounded", () => {
    assert.equal(backoff(1), 5000);
    assert.equal(backoff(20), 300000);
});
