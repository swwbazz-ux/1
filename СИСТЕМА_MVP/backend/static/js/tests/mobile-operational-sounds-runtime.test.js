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
const EXCAVATOR_TEMPLATE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "..", "templates", "trips", "excavator_work.html"),
    "utf8"
);
const DRIVER_TEMPLATE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "..", "templates", "users", "driver_shift.html"),
    "utf8"
);

function createStorage(values = new Map()) {
    return {
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, String(value)); },
        removeItem(key) { values.delete(key); },
        values,
    };
}

function createRuntime(profile = "excavator", options = {}) {
    const windowListeners = new Map();
    const played = [];
    const timers = new Map();
    const storage = options.storage || createStorage();
    let now = options.now || 1_000_000;
    let timerId = 0;
    const document = {
        body: {dataset: {}},
        currentScript: {dataset: {
            mobileSoundProfile: profile,
            mobileSoundBase: `/static/audio/${profile}/`,
            connectionLossStableMs: profile === "excavator" ? "30000" : "0",
            connectionRecoveryStableMs: profile === "excavator" ? "30000" : "0",
            connectionAlertCooldownMs: profile === "excavator" ? "300000" : "0",
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
        localStorage: storage,
        setTimeout(callback, delay) {
            timerId += 1;
            timers.set(timerId, {at: now + Number(delay || 0), callback});
            return timerId;
        },
        clearTimeout(id) {
            timers.delete(id);
        },
    };
    class FakeDate extends Date {
        static now() { return now; }
    }
    vm.runInNewContext(SOURCE, {
        Date: FakeDate,
        document,
        fetch() {
            throw new Error("Native playback must not fetch web assets");
        },
        Promise,
        window,
    }, {filename: "mobile-operational-sounds-v1.js"});
    return {
        document, played, storage, window, windowListeners,
        advance(milliseconds) {
            now += milliseconds;
            let ran = true;
            while (ran) {
                ran = false;
                for (const [id, timer] of [...timers].sort((left, right) => left[1].at - right[1].at)) {
                    if (timer.at > now) continue;
                    timers.delete(id);
                    timer.callback();
                    ran = true;
                    break;
                }
            }
        },
        connection(state) {
            document.body.dataset.connectionState = state;
            windowListeners.get("operational-state-connection")();
        },
    };
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

test("Excavator template enables the bounded connection voice policy", () => {
    assert.match(EXCAVATOR_TEMPLATE, /data-connection-loss-stable-ms="30000"/);
    assert.match(EXCAVATOR_TEMPLATE, /data-connection-recovery-stable-ms="30000"/);
    assert.match(EXCAVATOR_TEMPLATE, /data-connection-alert-cooldown-ms="300000"/);
});

/* Водитель 24.09.2026: у него этих порогов не было вовсе, а без них выдержка и
   пауза равны нулю — «связь потеряна» звучало с первого же неудачного опроса и
   повторялось без ограничения. В карьере это давало поток ложных оповещений
   при исправном интернете. */
test("Driver template enables the same bounded connection voice policy", () => {
    assert.match(DRIVER_TEMPLATE, /data-connection-loss-stable-ms="30000"/);
    assert.match(DRIVER_TEMPLATE, /data-connection-recovery-stable-ms="30000"/);
    assert.match(DRIVER_TEMPLATE, /data-connection-alert-cooldown-ms="300000"/);
});

test("Excavator connection voice requires sustained loss and sustained recovery", () => {
    const runtime = createRuntime();
    assert.equal(typeof runtime.windowListeners.get("operational-state-connection"), "function");

    runtime.connection("weak");
    assert.deepEqual(runtime.played, []);

    runtime.connection("lost");
    runtime.advance(29_999);
    assert.deepEqual(runtime.played, []);
    runtime.advance(1);
    assert.deepEqual(runtime.played, ["connection_lost_notice"]);

    runtime.connection("ok");
    runtime.advance(29_999);
    assert.deepEqual(runtime.played, ["connection_lost_notice"]);
    runtime.advance(1);
    assert.deepEqual(runtime.played, ["connection_lost_notice", "connection_restored_notice"]);
});

test("Excavator connection transitions use one native signal-and-voice sequence after dwell", () => {
    const runtime = createRuntime();
    const calls = [];
    runtime.window.Capacitor.Plugins.NativeSound.announceOperational = (details) => {
        calls.push(details);
        return Promise.resolve({announced: true});
    };
    runtime.connection("weak");
    runtime.connection("lost");
    runtime.advance(30_000);
    runtime.connection("ok");
    runtime.advance(30_000);

    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
        {cue: "connection_lost", voice: "voice_connection_lost", cueResolved: false, eventVersion: 0, eventKey: ""},
        {cue: "connection_restored", voice: "voice_connection_restored", cueResolved: false, eventVersion: 0, eventKey: ""},
    ]);
});

test("Excavator recovery dwell resets on recovering, weak, or renewed lost states", () => {
    const runtime = createRuntime();
    for (const state of ["unknown", "weak", "recovering", "ok"]) {
        runtime.connection(state);
    }
    runtime.advance(30_000);
    assert.deepEqual(runtime.played, []);
    runtime.connection("lost");
    runtime.advance(30_000);
    assert.deepEqual(runtime.played, ["connection_lost_notice"]);
    runtime.connection("ok");
    runtime.advance(20_000);
    runtime.connection("weak");
    runtime.advance(20_000);
    runtime.connection("ok");
    runtime.advance(29_999);
    assert.deepEqual(runtime.played, ["connection_lost_notice"]);
    runtime.advance(1);
    assert.deepEqual(runtime.played, ["connection_lost_notice", "connection_restored_notice"]);
});

test("a routine recovering blip does not postpone the restored announcement", () => {
    // Снято с живого телефона 24.09.2026: после возврата связи состояние
    // мелькает ok → recovering → ok примерно раз в двадцать секунд. Это
    // обычная сверка версии, а не обрыв. Пока отсчёт сбрасывался на ней,
    // возврат связи не озвучивался вообще, происшествие оставалось открытым,
    // и следующая потеря тоже уходила в тишину.
    const runtime = createRuntime();
    runtime.connection("lost");
    runtime.advance(30_000);
    assert.deepEqual(runtime.played, ["connection_lost_notice"]);
    runtime.connection("ok");
    for (let index = 0; index < 3; index += 1) {
        runtime.advance(20_000);
        runtime.connection("recovering");
        runtime.connection("ok");
    }
    assert.deepEqual(
        runtime.played,
        ["connection_lost_notice", "connection_restored_notice"]
    );
});

test("first confirmed lost state is announced even without an earlier successful response", () => {
    const runtime = createRuntime("driver");
    runtime.connection("lost");
    assert.deepEqual(runtime.played, ["connection_lost_notice"]);
});

test("brief Excavator flaps stay silent and do not arm a restored announcement", () => {
    const runtime = createRuntime();
    for (let index = 0; index < 4; index += 1) {
        runtime.connection("lost");
        runtime.advance(10_000);
        runtime.connection("ok");
        runtime.advance(10_000);
    }
    assert.deepEqual(runtime.played, []);
});

test("Excavator loss voice has a persisted five-minute cooldown across reload", () => {
    const storage = createStorage();
    const first = createRuntime("excavator", {storage});
    first.connection("lost");
    first.advance(30_000);
    assert.deepEqual(first.played, ["connection_lost_notice"]);

    const reloaded = createRuntime("excavator", {storage, now: 1_060_000});
    reloaded.connection("lost");
    reloaded.advance(30_000);
    reloaded.connection("ok");
    reloaded.advance(30_000);
    assert.deepEqual(reloaded.played, ["connection_restored_notice"]);

    const secondIncident = createRuntime("excavator", {storage, now: 1_120_000});
    secondIncident.connection("lost");
    secondIncident.advance(30_000);
    secondIncident.connection("ok");
    secondIncident.advance(30_000);
    assert.deepEqual(secondIncident.played, []);
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

/* Проверка гипотезы 24.09.2026: не сбрасывает ли мигание состояния выдержку
   перед объявлением потери. Таймер снимается на ЛЮБОМ не-lost состоянии, и на
   возврате в lost начинается заново — значит дребезг откладывает голос. */
test("a flapping connection restarts the loss dwell instead of accumulating it", () => {
    const runtime = createRuntime();

    runtime.connection("lost");
    runtime.advance(20_000);
    runtime.connection("weak");     // связь частично отвечает — выдержка снята
    runtime.advance(1_000);
    runtime.connection("lost");     // снова потеря — отсчёт начинается с нуля
    runtime.advance(29_999);
    assert.deepEqual(runtime.played, [], "первые 30 с после ВОЗВРАТА в потерю голос молчит");
    runtime.advance(1);
    assert.deepEqual(
        runtime.played,
        ["connection_lost_notice"],
        "после полных 30 с непрерывной потери голос обязан прозвучать"
    );
});

test("an outage that never lets up still announces once the dwell passes", () => {
    const runtime = createRuntime();
    runtime.connection("lost");
    runtime.advance(30_000);
    assert.deepEqual(runtime.played, ["connection_lost_notice"], "непрерывная потеря объявляется");
});
