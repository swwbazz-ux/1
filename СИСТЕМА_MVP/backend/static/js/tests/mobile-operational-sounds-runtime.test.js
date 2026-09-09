"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "mobile-operational-sounds-v1.js"),
    "utf8"
);

function createRuntime(profile = "excavator") {
    const windowListeners = new Map();
    const played = [];
    const document = {
        body: {dataset: {}},
        currentScript: {dataset: {
            mobileSoundProfile: profile,
            mobileSoundBase: `/static/audio/${profile}/`,
        }},
        addEventListener() {},
    };
    const window = {
        Capacitor: {
            Plugins: {
                NativeSound: {
                    play(options) {
                        played.push(options.name);
                        return Promise.resolve({played: true});
                    },
                },
            },
        },
        addEventListener(name, listener) {
            windowListeners.set(name, listener);
        },
    };
    vm.runInNewContext(SOURCE, {
        document,
        fetch() {
            throw new Error("Native playback must not fetch web assets");
        },
        Promise,
        window,
    }, {filename: "mobile-operational-sounds-v1.js"});
    return {document, played, window, windowListeners};
}

for (const profile of ["excavator", "driver"]) {
test(`${profile} native app receives the exact event name and full sound map`, async () => {
    const runtime = createRuntime(profile);

    assert.deepEqual(
        Array.from(Object.keys(runtime.window.MobileOperationalSounds.files)),
        [
            "truck_assigned",
            "action_ok",
            "action_error",
            "connection_lost",
            "connection_restored",
            "shift_start",
            "shift_end",
            "assignment_notice",
            "action_success_notice",
            "assignment_removed_notice",
            "shift_notice",
            "action_failed_notice",
            "connection_lost_notice",
            "connection_restored_notice",
        ]
    );
    assert.equal(runtime.window.MobileOperationalSounds.profile, profile);
    assert.equal(
        runtime.window.MobileOperationalSounds.files.shift_start,
        `${profile}_shift_start.wav`
    );
    assert.equal(await runtime.window.MobileOperationalSounds.play("shift_start"), true);
    assert.deepEqual(runtime.played, ["shift_start"]);
    assert.equal(await runtime.window.MobileOperationalSounds.play("assignment_removed_notice"), true);
    assert.deepEqual(runtime.played, ["shift_start", "assignment_removed_notice"]);
    assert.equal(await runtime.window.MobileOperationalSounds.play("unknown"), false);
    assert.deepEqual(runtime.played, ["shift_start", "assignment_removed_notice"]);
});
}

test("connection sounds fire only on a real lost transition and its recovery", () => {
    const runtime = createRuntime();
    const listener = runtime.windowListeners.get("operational-state-connection");
    assert.equal(typeof listener, "function");

    runtime.document.body.dataset.connectionState = "weak";
    listener();
    assert.deepEqual(runtime.played, []);

    runtime.document.body.dataset.connectionState = "lost";
    listener();
    listener();
    assert.deepEqual(runtime.played, ["connection_lost_notice"]);

    runtime.document.body.dataset.connectionState = "ok";
    listener();
    assert.deepEqual(runtime.played, ["connection_lost_notice", "connection_restored_notice"]);
});

test("connection transitions use one native signal-and-voice sequence", () => {
    const runtime = createRuntime();
    const calls = [];
    runtime.window.Capacitor.Plugins.NativeSound.announceOperational = (details) => {
        calls.push(details);
        return Promise.resolve({announced: true});
    };
    const listener = runtime.windowListeners.get("operational-state-connection");

    runtime.document.body.dataset.connectionState = "weak";
    listener();
    runtime.document.body.dataset.connectionState = "lost";
    listener();
    listener();
    runtime.document.body.dataset.connectionState = "ok";
    listener();

    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
        {cue: "connection_lost", voice: "voice_connection_lost", cueResolved: false, eventVersion: 0, eventKey: ""},
        {cue: "connection_restored", voice: "voice_connection_restored", cueResolved: false, eventVersion: 0, eventKey: ""},
    ]);
});

test("Excavator assignment batches reach the native bridge with exact operation keys", async () => {
    const runtime = createRuntime();
    const calls = [];
    runtime.window.Capacitor.Plugins.NativeSound.announceEquipmentBatch = (details) => {
        calls.push(details);
        return Promise.resolve({announced: true});
    };

    const result = await runtime.window.MobileOperationalSounds.announceEquipmentBatch({
        cue: "truck_assigned",
        items: [{
            action: "excavator_truck_assigned",
            equipmentNumber: "41",
            fallbackVoice: "voice_truck_assigned",
            eventVersion: 120,
            operationKey: "excavator-assignment:assign:501:9",
        }],
    });

    assert.equal(result.announced, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].items[0].operationKey, "excavator-assignment:assign:501:9");
    assert.equal(calls[0].items[0].eventVersion, 120);
});

test("an already announced Excavator batch never falls through to another cue", async () => {
    const runtime = createRuntime();
    runtime.window.Capacitor.Plugins.NativeSound.announceEquipmentBatch = () => (
        Promise.resolve({announced: false, reason: "already_announced"})
    );

    const result = await runtime.window.MobileOperationalSounds.announceEquipmentBatch({
        items: [{
            action: "excavator_truck_removed",
            equipmentNumber: "42",
            fallbackVoice: "voice_truck_removed",
            eventVersion: 121,
            operationKey: "excavator-assignment:remove:502:9",
        }],
    });

    assert.equal(result.reason, "already_announced");
    assert.deepEqual(runtime.played, []);
});

test("a synchronous batch bridge failure falls back without rejecting", async () => {
    const runtime = createRuntime();
    runtime.window.Capacitor.Plugins.NativeSound.announceEquipmentBatch = () => {
        throw new Error("bridge unavailable");
    };

    const result = await runtime.window.MobileOperationalSounds.announceEquipmentBatch({
        items: [{
            action: "excavator_truck_assigned",
            equipmentNumber: "43",
            fallbackVoice: "voice_truck_assigned",
            eventVersion: 122,
            operationKey: "excavator-assignment:assign:503:9",
        }],
    });

    assert.equal(result.announced, true);
    assert.deepEqual(runtime.played, ["assignment_notice"]);
});

test("native bridge keeps the legacy event contract while web fallback chooses the semantic cue", async () => {
    const runtime = createRuntime("driver");
    const calls = [];
    runtime.window.Capacitor.Plugins.NativeSound.announceOperational = (details) => {
        calls.push(details);
        return Promise.resolve({announced: true});
    };

    const announced = await runtime.window.MobileOperationalSounds.announceOperational({
        cue: "truck_assigned",
        voice: "voice_assignment_removed",
        eventVersion: 133,
        eventKey: "driver_assignment",
    });

    assert.equal(announced.announced, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].cue, "truck_assigned");
    assert.equal(calls[0].voice, "voice_assignment_removed");
    assert.equal(calls[0].eventVersion, 133);
    assert.equal(calls[0].eventKey, "driver_assignment");

    runtime.window.Capacitor.Plugins.NativeSound.announceOperational = () => Promise.reject(new Error("old bridge"));
    const fallback = await runtime.window.MobileOperationalSounds.announceOperational({
        cue: "truck_assigned",
        voice: "voice_assignment_removed",
    });

    assert.equal(fallback.announced, true);
    assert.deepEqual(runtime.played, ["assignment_removed_notice"]);
});

test("equipment removal fallback uses its distinct removal cue", async () => {
    const runtime = createRuntime("excavator");
    runtime.window.Capacitor.Plugins.NativeSound.announceEquipment = () => Promise.reject(new Error("old bridge"));
    runtime.window.Capacitor.Plugins.NativeSound.announceOperational = () => Promise.reject(new Error("old bridge"));

    const result = await runtime.window.MobileOperationalSounds.announceEquipment({
        cue: "truck_assigned",
        action: "excavator_truck_removed",
        equipmentNumber: "41",
        fallbackVoice: "voice_truck_removed",
    });

    assert.equal(result.announced, true);
    assert.deepEqual(runtime.played, ["assignment_removed_notice"]);
});

test("a mixed Excavator batch keeps the generic assignment cue in its fallback", async () => {
    for (const items of [[
        {
            action: "excavator_truck_removed",
            equipmentNumber: "41",
            fallbackVoice: "voice_truck_removed",
            eventVersion: 141,
            operationKey: "excavator-assignment:remove:501:9",
        },
        {
            action: "excavator_truck_assigned",
            equipmentNumber: "42",
            fallbackVoice: "voice_truck_assigned",
            eventVersion: 142,
            operationKey: "excavator-assignment:assign:502:9",
        },
    ], [
        {
            action: "excavator_truck_sent",
            equipmentNumber: "43",
            fallbackVoice: "voice_truck_sent",
            eventVersion: 143,
            operationKey: "excavator-assignment:sent:503:9",
        },
        {
            action: "excavator_truck_assigned",
            equipmentNumber: "44",
            fallbackVoice: "voice_truck_assigned",
            eventVersion: 144,
            operationKey: "excavator-assignment:assign:504:9",
        },
    ]]) {
        const runtime = createRuntime("excavator");
        runtime.window.Capacitor.Plugins.NativeSound.announceEquipmentBatch = () => {
            throw new Error("batch bridge unavailable");
        };
        runtime.window.Capacitor.Plugins.NativeSound.announceEquipment = () => {
            throw new Error("equipment bridge unavailable");
        };
        runtime.window.Capacitor.Plugins.NativeSound.announceOperational = () => {
            throw new Error("operational bridge unavailable");
        };

        const result = await runtime.window.MobileOperationalSounds.announceEquipmentBatch({items});
        assert.equal(result.announced, true);
        assert.deepEqual(runtime.played, ["assignment_notice"]);
    }
});
