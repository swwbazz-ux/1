"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "excavator-sounds-v1.js"),
    "utf8"
);

function createRuntime() {
    const windowListeners = new Map();
    const played = [];
    const document = {
        body: {dataset: {}},
        currentScript: {dataset: {excavatorSoundBase: "/static/audio/excavator/"}},
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
    }, {filename: "excavator-sounds-v1.js"});
    return {document, played, window, windowListeners};
}

test("native app receives the exact event name and the full approved sound map", async () => {
    const runtime = createRuntime();

    assert.deepEqual(
        Array.from(Object.keys(runtime.window.ExcavatorSounds.files)),
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
    assert.equal(await runtime.window.ExcavatorSounds.play("shift_start"), true);
    assert.deepEqual(runtime.played, ["shift_start"]);
    assert.equal(await runtime.window.ExcavatorSounds.play("unknown"), false);
    assert.deepEqual(runtime.played, ["shift_start"]);
});

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
