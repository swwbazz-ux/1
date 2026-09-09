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
        ]
    );
    assert.equal(runtime.window.MobileOperationalSounds.profile, profile);
    assert.equal(
        runtime.window.MobileOperationalSounds.files.shift_start,
        `${profile}_shift_start.wav`
    );
    assert.equal(await runtime.window.MobileOperationalSounds.play("shift_start"), true);
    assert.deepEqual(runtime.played, ["shift_start"]);
    assert.equal(await runtime.window.MobileOperationalSounds.play("unknown"), false);
    assert.deepEqual(runtime.played, ["shift_start"]);
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
    assert.deepEqual(runtime.played, ["connection_lost"]);

    runtime.document.body.dataset.connectionState = "ok";
    listener();
    assert.deepEqual(runtime.played, ["connection_lost", "connection_restored"]);
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
    assert.deepEqual(runtime.played, ["truck_assigned"]);
});
