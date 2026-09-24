const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {DISPATCHER_STYLE_FILES, dispatcherStyleSource} = require("./dispatcher-style-source");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const source = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-sounds-v1.js"),
    "utf8"
);

class EventTargetMock {
    constructor() {
        this.listeners = new Map();
    }

    addEventListener(name, listener) {
        if (!this.listeners.has(name)) this.listeners.set(name, []);
        this.listeners.get(name).push(listener);
    }

    dispatchEvent(event) {
        (this.listeners.get(event.type) || []).slice().forEach((listener) => listener(event));
        return true;
    }
}

function createRuntime(storedValue = null) {
    const window = new EventTargetMock();
    const document = new EventTargetMock();
    const storage = new Map();
    if (storedValue !== null) storage.set("dispatcher-sound-enabled-v1", storedValue);
    const tones = [];
    const mutedMark = {style: {display: ""}};
    const button = {
        attributes: {},
        dataset: {},
        title: "",
        setAttribute(name, value) {
            this.attributes[name] = String(value);
        },
        querySelector(selector) {
            return selector === "[data-dispatcher-sound-muted-mark]" ? mutedMark : null;
        }
    };

    class AudioContextMock {
        constructor() {
            this.state = "suspended";
            this.currentTime = 1;
            this.destination = {};
        }

        resume() {
            this.state = "running";
            return Promise.resolve();
        }

        createOscillator() {
            const record = {frequency: null, startedAt: null, stoppedAt: null, type: ""};
            tones.push(record);
            const oscillator = {
                frequency: {setValueAtTime(value) { record.frequency = value; }},
                connect() {},
                start(value) { record.startedAt = value; },
                stop(value) { record.stoppedAt = value; }
            };
            Object.defineProperty(oscillator, "type", {
                get() { return record.type; },
                set(value) { record.type = value; }
            });
            return oscillator;
        }

        createGain() {
            const record = tones[tones.length - 1];
            return {
                gain: {
                    setValueAtTime() {},
                    exponentialRampToValueAtTime(value) {
                        record.peakGain = Math.max(Number(record.peakGain || 0), Number(value || 0));
                    }
                },
                connect() {}
            };
        }
    }

    document.body = {dataset: {connectionState: "unknown"}};
    document.querySelectorAll = (selector) => selector === "[data-dispatcher-sound-toggle]" ? [button] : [];
    window.document = document;
    window.window = window;
    window.AudioContext = AudioContextMock;
    window.localStorage = {
        getItem(key) { return storage.has(key) ? storage.get(key) : null; },
        setItem(key, value) { storage.set(key, String(value)); }
    };
    window.CustomEvent = class CustomEvent {
        constructor(type, options = {}) {
            this.type = type;
            this.detail = options.detail || {};
        }
    };

    const context = vm.createContext({
        window,
        document,
        CustomEvent: window.CustomEvent,
        Date,
        Promise,
        Object,
        Array,
        String,
        Number,
        Math,
        console
    });
    vm.runInContext(source, context, {filename: "dispatcher-sounds-v1.js"});
    return {window, document, storage, tones, button, mutedMark, context};
}

async function settle() {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
}

test("new operational events sound once per stable event version", async () => {
    const runtime = createRuntime();
    runtime.document.dispatchEvent({type: "pointerdown"});
    await settle();
    runtime.window.dispatchEvent(new runtime.window.CustomEvent("operational-state-refresh-applied", {
        detail: {
            role: "dispatcher",
            version: 12,
            events: [{version: 11, type: "trip_changed"}, {version: 12, type: "downtime_changed"}]
        }
    }));
    await settle();
    assert.equal(runtime.tones.length, 3);
    assert.ok(runtime.tones.every((tone) => tone.type === "triangle"));
    assert.ok(runtime.tones.every((tone) => tone.peakGain >= 0.18));
    assert.ok(
        runtime.tones.at(-1).stoppedAt - runtime.tones[0].startedAt >= 0.8,
        "attention cue must remain audible for roughly a second"
    );

    runtime.window.dispatchEvent(new runtime.window.CustomEvent("operational-state-refresh-applied", {
        detail: {role: "dispatcher", version: 12, events: [{version: 12, type: "downtime_changed"}]}
    }));
    await settle();
    assert.equal(runtime.tones.length, 3, "the repeated server event must stay silent");

    runtime.window.dispatchEvent(new runtime.window.CustomEvent("operational-state-refresh-applied", {
        detail: {role: "dispatcher", version: 13, events: [{version: 13, type: "assignment_changed"}]}
    }));
    await settle();
    assert.equal(runtime.tones.length, 6);
});

test("connection loss and recovery have distinct transition-only cues", async () => {
    const runtime = createRuntime();
    runtime.document.dispatchEvent({type: "pointerdown"});
    await settle();
    const connection = (state, previousState) => runtime.window.dispatchEvent(
        new runtime.window.CustomEvent("operational-state-connection", {
            detail: {role: "dispatcher", state, previousState}
        })
    );
    connection("weak", "unknown");
    connection("lost", "weak");
    await settle();
    assert.equal(runtime.tones.length, 3);
    connection("lost", "lost");
    await settle();
    assert.equal(runtime.tones.length, 3);
    connection("recovering", "lost");
    await settle();
    assert.equal(runtime.tones.length, 6);
    connection("ok", "recovering");
    await settle();
    assert.equal(runtime.tones.length, 6);
});

test("action errors are audible but an immediate duplicate is suppressed", async () => {
    const runtime = createRuntime();
    runtime.document.dispatchEvent({type: "pointerdown"});
    await settle();
    const event = new runtime.window.CustomEvent("dispatcher-action-error", {
        detail: {code: "state_conflict", message: "Состояние изменилось"}
    });
    runtime.window.dispatchEvent(event);
    runtime.window.dispatchEvent(event);
    await settle();
    assert.equal(runtime.tones.length, 3);
});

test("the persistent header toggle mutes and re-enables cues", async () => {
    const runtime = createRuntime();
    const click = () => runtime.document.dispatchEvent({
        type: "click",
        preventDefault() {},
        target: {closest() { return runtime.button; }}
    });
    assert.equal(runtime.button.attributes["aria-pressed"], "true");
    click();
    await settle();
    assert.equal(runtime.window.DispatcherSounds.isEnabled(), false);
    assert.equal(runtime.storage.get("dispatcher-sound-enabled-v1"), "false");
    assert.equal(runtime.mutedMark.style.display, "");
    await runtime.window.DispatcherSounds.play("attention");
    assert.equal(runtime.tones.length, 0);

    click();
    await settle();
    assert.equal(runtime.window.DispatcherSounds.isEnabled(), true);
    assert.equal(runtime.storage.get("dispatcher-sound-enabled-v1"), "true");
    assert.equal(runtime.mutedMark.style.display, "none");
    assert.equal(runtime.tones.length, 2, "enabling sound must give immediate feedback");
});

test("loading the module twice does not duplicate global listeners", async () => {
    const runtime = createRuntime();
    const before = runtime.window.listeners.get("operational-state-refresh-applied").length;
    vm.runInContext(source, runtime.context, {filename: "dispatcher-sounds-v1.js#second-load"});
    assert.equal(runtime.window.listeners.get("operational-state-refresh-applied").length, before);
});

test("dispatcher shell wires and precaches the isolated sound module", () => {
    const template = fs.readFileSync(
        path.join(BACKEND, "templates", "trips", "dispatcher_control.html"),
        "utf8"
    );
    const header = fs.readFileSync(
        path.join(BACKEND, "templates", "includes", "dispatcher_header.html"),
        "utf8"
    );
    const views = fs.readFileSync(path.join(BACKEND, "trips", "views.py"), "utf8");
    const control = fs.readFileSync(
        path.join(BACKEND, "static", "js", "dispatcher-control-v1.js"),
        "utf8"
    );
    const css = dispatcherStyleSource();
    let previousStyle = -1;
    for (const file of DISPATCHER_STYLE_FILES) {
        const currentStyle = template.indexOf(`css/${file}`);
        assert.ok(currentStyle > previousStyle, `${file} must keep dispatcher cascade order`);
        previousStyle = currentStyle;
    }
    assert.match(template, /dispatcher-sounds-v1\.js[^\n]+dispatcher-desktop-shell-v142/);
    assert.match(template, /dispatcher-transport-v1\.js[^\n]+dispatcher-desktop-shell-v142/);
    assert.match(template, /dispatcher-detail-v1\.js[^\n]+dispatcher-desktop-shell-v142/);
    assert.match(template, /dispatcher-board-v1\.js[^\n]+dispatcher-desktop-shell-v142/);
    assert.match(template, /dispatcher-realtime-v1\.js[^\n]+dispatcher-desktop-shell-v142/);
    assert.match(header, /data-dispatcher-sound-toggle/);
    assert.match(views, /dispatcher-desktop-shell-v142/);
    assert.match(views, /\/static\/js\/dispatcher-sounds-v1\.js/);
    assert.match(control, /new CustomEvent\("dispatcher-action-error"/);
    assert.match(css, /@media \(max-width: 1180px\)[\s\S]+?\.dispatcher-command-utility\s*\{[\s\S]+?grid-template-columns:\s*repeat\(5,/);
});
