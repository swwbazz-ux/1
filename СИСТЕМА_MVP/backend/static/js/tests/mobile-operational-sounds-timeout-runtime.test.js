"use strict";

/* T-016, WP-4 (итерация 2). Сроки ожидания звука.

   В браузере без нативного моста AudioContext может остаться «suspended», а
   resume() — никогда не завершиться (autoplay, headless, звук отключён).
   Раньше play() висел, голосовой handoff не подтверждал версию, и экран
   водителя замирал (T-016, e2e §10.1). Здесь проверяется, что:
   - AudioContext.resume() ограничен сроком и не запускает поздний звук;
   - молчащий мост отвечает reason «bridge_timeout», а не висит;
   - веб-фолбэк не играет поверх замолчавшего моста;
   - быстрый ответ моста не ждёт срока (срок снимается). */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "mobile-operational-sounds-v1.js"),
    "utf8"
);

const DEADLINE_MS = 1500;

function settle() {
    return new Promise((resolve) => setImmediate(resolve));
}

function createRuntime(options) {
    const opts = options || {};
    const timers = new Map();
    let nextTimer = 1;
    let now = 0;
    const played = [];
    const fetches = [];
    const starts = [];
    const resumeCalls = [];
    const lateResume = {resolve: null, reject: null};
    const resumePending = new Promise((resolve, reject) => {
        lateResume.resolve = resolve;
        lateResume.reject = reject;
    });

    const audio = {
        state: opts.audioState || "running",
        destination: {},
        resume() {
            resumeCalls.push(true);
            if (opts.resume === "hang") return resumePending;
            if (opts.resume === "reject") return Promise.reject(new Error("blocked"));
            this.state = "running";
            return Promise.resolve();
        },
        createBufferSource() {
            const source = {
                buffer: null,
                connect() {},
                addEventListener() {},
                start() {
                    starts.push(true);
                },
            };
            return source;
        },
        createGain() {
            return {gain: {value: 1}, connect() {}};
        },
        decodeAudioData(buffer, onSuccess) {
            onSuccess({duration: 1});
        },
    };

    const nativeSound = {};
    if (opts.bridge !== "absent") {
        nativeSound.play = opts.play || function () {
            played.push("native");
            return Promise.resolve({played: true});
        };
        nativeSound.announceOperational = opts.announceOperational || function () {
            return Promise.resolve({announced: true});
        };
        nativeSound.announceDumpPoint = opts.announceDumpPoint || function () {
            return Promise.resolve({announced: true});
        };
        nativeSound.announceEquipment = opts.announceEquipment || function () {
            return Promise.resolve({announced: true});
        };
        nativeSound.announceEquipmentBatch = opts.announceEquipmentBatch || function () {
            return Promise.resolve({announced: true});
        };
    }

    const windowListeners = new Map();
    const window = {
        Capacitor: opts.bridge === "absent" ? undefined : {Plugins: {NativeSound: nativeSound}},
        AudioContext: function FakeAudioContext() {
            return audio;
        },
        addEventListener(name, listener) {
            windowListeners.set(name, listener);
        },
        setTimeout(callback, delay) {
            const id = nextTimer;
            nextTimer += 1;
            timers.set(id, {at: now + Number(delay || 0), callback});
            return id;
        },
        clearTimeout(id) {
            timers.delete(id);
        },
    };

    const document = {
        body: {dataset: {}},
        currentScript: {
            dataset: {
                mobileSoundProfile: "driver",
                mobileSoundBase: "/static/audio/driver/",
            },
        },
        addEventListener() {},
    };

    vm.runInNewContext(SOURCE, {
        window,
        document,
        Promise,
        Number,
        String,
        Object,
        Array,
        JSON,
        Date,
        console,
        fetch(url) {
            fetches.push(String(url));
            if (opts.fetch === "hang") return new Promise(() => {});
            return Promise.resolve({
                ok: true,
                status: 200,
                arrayBuffer() {
                    return Promise.resolve(new ArrayBuffer(8));
                },
            });
        },
    }, {filename: "mobile-operational-sounds-v1.js"});

    return {
        window,
        audio,
        played,
        fetches,
        starts,
        resumeCalls,
        sound: window.MobileOperationalSounds,
        timerCount() {
            return timers.size;
        },
        advance(milliseconds) {
            now += milliseconds;
            let ran = true;
            while (ran) {
                ran = false;
                const due = [...timers.entries()]
                    .filter(([, timer]) => timer.at <= now)
                    .sort((left, right) => left[1].at - right[1].at);
                if (!due.length) break;
                const [id, timer] = due[0];
                timers.delete(id);
                timer.callback();
                ran = true;
            }
        },
        finishResume() {
            lateResume.resolve();
        },
    };
}

test("a suspended AudioContext stops play() within the deadline", async () => {
    const runtime = createRuntime({audioState: "suspended", resume: "hang", bridge: "absent"});
    let settled = false;
    const pending = runtime.sound.play("truck_assigned").then((value) => {
        settled = true;
        return value;
    });
    await settle();

    assert.equal(settled, false, "resume() never settles, so play() must wait for the deadline");
    assert.equal(runtime.audio.state, "suspended");

    runtime.advance(DEADLINE_MS);
    assert.equal(await pending, false, "an unanswered resume() must not hang the voice path");
    assert.equal(runtime.starts.length, 0, "nothing was started");
    assert.deepEqual(runtime.fetches, [], "the asset is not fetched after the deadline");
    assert.equal(runtime.timerCount(), 0, "the deadline timer is cleared");
});

test("a late AudioContext resume does not start a sound nobody waits for", async () => {
    const runtime = createRuntime({audioState: "suspended", resume: "hang", bridge: "absent"});
    const pending = runtime.sound.play("truck_assigned");
    await settle();
    runtime.advance(DEADLINE_MS);
    assert.equal(await pending, false);

    /* Браузер разблокировал звук уже после срока: попытка закрыта. */
    runtime.audio.state = "running";
    runtime.finishResume();
    await settle();

    assert.equal(runtime.starts.length, 0, "a late resume must not play a stale cue");
    assert.deepEqual(runtime.fetches, []);
});

test("a running AudioContext still plays the web cue", async () => {
    const runtime = createRuntime({audioState: "running", bridge: "absent"});

    assert.equal(await runtime.sound.play("action_ok"), true);
    assert.equal(runtime.starts.length, 1);
    assert.equal(runtime.fetches.length, 1);
});

test("a hung native play() answers false instead of a delayed web cue", async () => {
    const runtime = createRuntime({play: () => new Promise(() => {})});
    let settled = false;
    const pending = runtime.sound.play("shift_start").then((value) => {
        settled = true;
        return value;
    });
    await settle();
    assert.equal(settled, false);

    runtime.advance(DEADLINE_MS);
    assert.equal(await pending, false, "an unanswered bridge must not hang the player");
    assert.deepEqual(runtime.resumeCalls, [], "no web fallback for an unanswered bridge");
    assert.deepEqual(runtime.played, []);
});

test("announceOperational answers bridge_timeout when the bridge never settles", async () => {
    const runtime = createRuntime({announceOperational: () => new Promise(() => {})});
    const pending = runtime.sound.announceOperational({
        cue: "truck_assigned",
        voice: "voice_assignment_removed",
        eventVersion: 9,
    });
    await settle();

    runtime.advance(DEADLINE_MS);
    const result = await pending;

    assert.equal(result.supported, true, "the bridge exists, it just has not answered");
    assert.equal(result.announced, false);
    assert.equal(result.reason, "bridge_timeout");
    assert.deepEqual(runtime.resumeCalls, [], "the web fallback must not speak over the bridge");
});

test("announceDumpPoint answers bridge_timeout when the bridge never settles", async () => {
    const runtime = createRuntime({announceDumpPoint: () => new Promise(() => {})});
    const pending = runtime.sound.announceDumpPoint({
        eventVersion: 12,
        tripId: 777,
        dumpPointId: 5,
        dumpPointName: "Склад руды",
    });
    await settle();
    runtime.advance(DEADLINE_MS);

    const result = await pending;
    assert.equal(result.supported, true);
    assert.equal(result.announced, false);
    assert.equal(result.reason, "bridge_timeout");
});

test("announceEquipment answers bridge_timeout and does not fall back to a second bridge", async () => {
    let operationalCalls = 0;
    const runtime = createRuntime({
        announceEquipment: () => new Promise(() => {}),
        announceOperational: () => {
            operationalCalls += 1;
            return Promise.resolve({announced: true});
        },
    });
    const pending = runtime.sound.announceEquipment({
        cue: "truck_assigned",
        action: "excavator_truck_assigned",
        equipmentNumber: "41",
        eventVersion: 14,
    });
    await settle();
    runtime.advance(DEADLINE_MS);

    const result = await pending;
    assert.equal(result.reason, "bridge_timeout");
    assert.equal(
        operationalCalls,
        0,
        "an unanswered bridge must not be replaced by another bridge announcement"
    );
});

test("announceEquipmentBatch answers bridge_timeout when the batch bridge never settles", async () => {
    const runtime = createRuntime({announceEquipmentBatch: () => new Promise(() => {})});
    const pending = runtime.sound.announceEquipmentBatch({
        items: [{
            action: "excavator_truck_assigned",
            equipmentNumber: "42",
            eventVersion: 15,
            operationKey: "excavator-assignment:assign:601:9",
        }],
    });
    await settle();
    runtime.advance(DEADLINE_MS);

    const result = await pending;
    assert.equal(result.reason, "bridge_timeout");
    assert.equal(result.announced, false);
});

test("a responsive bridge is not delayed and leaves no timer behind", async () => {
    const runtime = createRuntime({
        announceOperational: () => Promise.resolve({announced: true}),
    });

    const result = await runtime.sound.announceOperational({
        cue: "truck_assigned",
        voice: "voice_assignment_removed",
    });

    assert.equal(result.announced, true);
    assert.equal(runtime.timerCount(), 0, "the deadline timer is cleared on a fast answer");
});

test("a rejected bridge keeps the previous fallback path", async () => {
    const runtime = createRuntime({
        announceOperational: () => Promise.reject(new Error("old bridge")),
    });

    const result = await runtime.sound.announceOperational({
        cue: "truck_assigned",
        voice: "voice_assignment_removed",
    });

    assert.equal(result.reason, "bridge_error");
    assert.equal(result.supported, true);
    assert.equal(result.announced, true, "the fallback cue covers a rejected bridge as before");
    assert.deepEqual(runtime.played, ["native"], "the direct cue still goes through the player");
});
