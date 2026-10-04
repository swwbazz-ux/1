"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {createDriverOfflineOutbox} = require("../driver-offline-outbox-v2.js");
const createExcavatorOutbox = require("../excavator-field-outbox-v1.js");

function memoryStorage() {
    const data = new Map();
    return {getItem: key => data.get(key) || null, setItem: (key, value) => data.set(key, String(value))};
}

function actualSender(role, fetch) {
    const backend = path.resolve(__dirname, "../../..");
    const file = role === "driver" ? "static/js/driver-shift-v1.js" : "templates/trips/excavator_work.html";
    const source = fs.readFileSync(path.join(backend, file), "utf8");
    const signature = role === "driver" ? "send: function (batch, request)" : "send: function (events, request)";
    const start = source.indexOf(signature);
    assert.ok(start >= 0);
    const endMarker = role === "driver" ? "\n            },\n            onState:" : "\n        }\n    });";
    const end = source.indexOf(endMarker, start);
    assert.ok(end > start);
    const body = source.slice(start + "send: ".length, end) + "\n}";
    const shell = {dataset: {eoOfflineSyncUrl: "/offline-events/sync/"}, querySelector: () => ({value: "test-token"})};
    return vm.runInNewContext("(" + body + ")", {
        fetch,
        window: {AbortController, setTimeout, clearTimeout, location: {href: "https://driver.test/driver/"}},
        AbortController, Promise, JSON,
        document: {querySelector: () => shell},
        deviceId: "test-device",
        csrf: {content: "test-token"},
    });
}

for (const role of ["driver", "excavator"]) {
    test(role + " actual screen sender stays bounded through body parsing and receives the outbox abort signal", async () => {
        let signal;
        let bodyStarted = false;
        const send = actualSender(role, async (url, request) => {
            signal = request.signal;
            const body = () => { bodyStarted = true; return new Promise(() => {}); };
            return {status: 200, ok: true, text: body, json: body};
        });
        const localStorage = memoryStorage();
        const box = role === "driver" ? createDriverOfflineOutbox({
            accessId: 7, localStorage, send, requestTimeoutMs: 15,
            context: {actorId: 11, accessId: 7, shiftId: 23, equipmentId: 58, deviceId: "test-device"},
        }) : createExcavatorOutbox({queueKey: "deadline-screen", localStorage, send, requestTimeoutMs: 15});
        if (role === "driver") {
            await box.enqueue({event_id: "body-stalled", event_type: "driver.trip.unloaded", trip_id: 1, payload: {trip_id: 1}});
        } else {
            await box.queue({event_id: "body-stalled", event_type: "excavator.trip.loaded", sequence: 1,
                payload: {truck_id: 58, excavator_id: 5, dump_point_id: 2}});
        }
        await box.flush();
        assert.equal(bodyStarted, true);
        assert.equal(signal.aborted, true);
        const [event] = await box.pending();
        assert.equal(event.event_id, "body-stalled");
        assert.equal(event.state || event.sync_state, "pending");
    });
}
