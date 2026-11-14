"use strict";

/*
 * Контракт голосовой квитанции — вариант A (решение руководителя T-014).
 *
 * Курсор доставки realtime подтверждается текущей квитанцией роли:
 *   - роль вернула `{accepted: true}` из `window.handleOperationalStateSignals`;
 *   - экскаваторный guard ставит `accepted` по `announced === true`
 *     (или `reason === "already_announced"`) от `MobileOperationalSounds`;
 *   - поле `processed` из нативного моста НЕ является квитанцией и не двигает
 *     курсор.
 *
 * Контракт материала «durable processed из ledger» сознательно не принимается.
 * Остаточный риск описан в отчёте T-014-realtime-2.
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const jsRoot = path.resolve(__dirname, "..");
const backendRoot = path.resolve(jsRoot, "..", "..");
const realtimeSource = fs.readFileSync(path.join(jsRoot, "realtime-client.js"), "utf8");
const soundsSource = fs.readFileSync(path.join(jsRoot, "mobile-operational-sounds-v1.js"), "utf8");
const excavatorTemplate = fs.readFileSync(
    path.join(backendRoot, "templates", "trips", "excavator_work.html"),
    "utf8"
);
const voiceStart = excavatorTemplate.indexOf("/* EXCAVATOR_VOICE_GUARD_START */");
const voiceEnd = excavatorTemplate.indexOf("function readExcavatorViewportMetrics", voiceStart);
assert.notEqual(voiceStart, -1);
assert.notEqual(voiceEnd, -1);
const excavatorVoiceSource = excavatorTemplate.slice(voiceStart, voiceEnd);

function eventTarget() {
    const listeners = new Map();
    return {
        addEventListener(name, listener) {
            listeners.set(name, [...(listeners.get(name) || []), listener]);
        },
        dispatchEvent(event) {
            (listeners.get(event.type) || []).slice().forEach((listener) => listener.call(this, event));
        },
    };
}

function storage() {
    const values = new Map();
    return {
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, String(value)); },
        removeItem(key) { values.delete(key); },
    };
}

function classList() {
    const values = new Set();
    return {
        add(...items) { items.forEach((item) => values.add(item)); },
        remove(...items) { items.forEach((item) => values.delete(item)); },
        contains(item) { return values.has(item); },
        toggle(item, enabled) { if (enabled) values.add(item); else values.delete(item); },
    };
}

function statePayload(version, events = []) {
    return {
        version,
        relevant: events.length > 0,
        events,
        events_truncated: false,
        role_active: true,
        role_app_code: "excavator_operator",
        session_revision: "session-1",
        worker_equipment_ids: [9],
        journal_epoch: "epoch-1",
        context: {
            cursor_scope: "access-2|excavator|equipment-9",
            role_app_code: "excavator_operator",
            worker_equipment_ids: [9],
            session_revision: "session-1",
        },
        journal: {epoch: "epoch-1", min_version: version > 0 ? 1 : 0, max_version: version},
    };
}

function assignmentEvent(version, assignmentId, truckId) {
    return {
        version,
        type: "assignment_changed",
        object_type: "HaulAssignment",
        object_id: String(assignmentId),
        payload: {
            action: "assignment_applied",
            truck_ids: [truckId],
            truck_number: String(truckId),
            target_excavator_id: 9,
            excavator_ids: [9],
        },
    };
}

function releaseEvent(version, assignmentId, truckId) {
    return {
        version,
        type: "assignment_changed",
        object_type: "HaulAssignment",
        object_id: String(assignmentId),
        payload: {
            action: "release_applied",
            truck_ids: [truckId],
            truck_number: String(truckId),
            target_excavator_id: null,
            excavator_ids: [9],
        },
    };
}

function createRuntime({
    batchBridge,
    equipmentBridge,
    operationalBridge,
    sharedStorage = storage(),
    initialVersion = 7,
    sseEnabled = true,
}) {
    let now = 1_000_000;
    let nextTimer = 0;
    const timers = new Map();
    const intervals = new Map();
    const requests = [];
    const sources = [];
    const batchCalls = [];
    const itemCalls = [];
    const shell = {
        dataset: {eoCurrentExcavatorId: "9", eoShiftExcavatorId: ""},
        querySelectorAll() { return []; },
    };
    const body = {
        dataset: {
            appRoleCode: "excavator_operator",
            operationalStateVersion: String(initialVersion),
            nativeApp: "true",
        },
        classList: classList(),
    };
    const document = Object.assign(eventTarget(), {
        body,
        currentScript: {dataset: {
            mobileSoundProfile: "excavator",
            mobileSoundBase: "/static/audio/excavator/",
            connectionLossStableMs: "30000",
            connectionRecoveryStableMs: "30000",
            connectionAlertCooldownMs: "300000",
        }},
        hidden: false,
        activeElement: null,
        hasFocus: () => true,
        querySelector(selector) { return selector === "[data-eo-shell]" ? shell : null; },
    });
    class FakeEventSource {
        constructor(url) {
            this.url = url;
            this.listeners = new Map();
            this.closed = false;
            sources.push(this);
        }
        addEventListener(name, listener) { this.listeners.set(name, listener); }
        emit(name, data) {
            const event = {data: JSON.stringify(data)};
            if (name === "open" && this.onopen) this.onopen(event);
            else if (name === "error" && this.onerror) this.onerror(event);
            else if (this.listeners.has(name)) this.listeners.get(name)(event);
        }
        close() { this.closed = true; }
    }
    const navigator = {onLine: true, vibrate() {}};
    const nativeSound = {
        announceEquipmentBatch(details) {
            batchCalls.push(JSON.parse(JSON.stringify(details)));
            return Promise.resolve(batchBridge(details));
        },
        announceEquipment(details) {
            itemCalls.push(JSON.parse(JSON.stringify(details)));
            return Promise.resolve(
                equipmentBridge
                    ? equipmentBridge(details)
                    : {announced: false, processed: false, reason: "in_progress"}
            );
        },
        announceOperational() {
            return Promise.resolve(
                operationalBridge
                    ? operationalBridge()
                    : {announced: false, reason: "not_announced"}
            );
        },
        play() { return Promise.resolve({played: true}); },
    };
    const window = Object.assign(eventTarget(), {
        document,
        navigator,
        EventSource: FakeEventSource,
        AbortController,
        Capacitor: {Plugins: {NativeSound: nativeSound}},
        location: {
            pathname: "/excavator/work/",
            origin: "https://driver.sse-pilot.local",
            href: "https://driver.sse-pilot.local/excavator/work/",
            reload() {},
            replace() {},
        },
        localStorage: sharedStorage,
        sessionStorage: storage(),
        AppRealtimeConfig: {
            stateUrl: "/realtime/state/",
            sseUrl: "/realtime/stream/",
            sseEnabled,
            sseHeartbeatTimeoutMs: 30000,
            initialVersion,
            pollTimeoutMs: 8000,
            workPollIntervalMs: 2000,
            truthRefreshIntervalMs: 20000,
            sseTruthRefreshIntervalMs: 60000,
            screens: [{
                name: "excavator", role: "excavator_operator", path: "^/excavator/", mode: "custom",
                customRefresh: true, foregroundReconcile: true, pollIntervalMs: 2000,
            }],
        },
        setTimeout(callback, delay) { timers.set(++nextTimer, {callback, delay}); return nextTimer; },
        clearTimeout(id) { timers.delete(id); },
        setInterval(callback, delay) { intervals.set(++nextTimer, {callback, delay}); return nextTimer; },
        clearInterval(id) { intervals.delete(id); },
        fetch(url, init) {
            return new Promise((resolve, reject) => {
                const request = {url: String(url), init, resolve, reject};
                if (init && init.signal) {
                    init.signal.addEventListener("abort", () => {
                        const error = new Error("aborted");
                        error.name = "AbortError";
                        reject(error);
                    });
                }
                requests.push(request);
            });
        },
        applyOperationalStateRefresh(context) {
            return Promise.resolve({applied: true, snapshotVersion: context.version});
        },
        playExcavatorEquipmentVoice() {
            throw new Error("real batch wrapper must own the integration path");
        },
    });
    class FakeDate extends Date { static now() { return now; } }
    class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } }
    const context = {
        window, document, navigator, EventSource: FakeEventSource, AbortController,
        CustomEvent, Date: FakeDate, URL, Promise, Object, Array, Number, String, JSON, console,
        fetch: window.fetch,
    };
    vm.runInNewContext(soundsSource, context, {filename: "mobile-operational-sounds-v1.js"});
    vm.runInNewContext(excavatorVoiceSource, context, {filename: "excavator_work.html#voice-guard"});
    vm.runInNewContext(realtimeSource, context, {filename: "realtime-client.js"});
    document.dispatchEvent({type: "DOMContentLoaded"});

    return {
        window, document, requests, sources, batchCalls, itemCalls,
        advance(ms) { now += ms; },
        runDelay(delay) {
            for (const [id, timer] of [...timers]) {
                if (timer.delay === delay) {
                    timers.delete(id);
                    timer.callback();
                }
            }
        },
        success(index, payload) {
            requests[index].resolve({
                status: 200,
                ok: true,
                redirected: false,
                headers: {get: () => "application/json"},
                json: () => Promise.resolve(payload),
            });
        },
    };
}

async function settle() {
    for (let index = 0; index < 30; index += 1) await Promise.resolve();
}

test("announced=true from the native batch is the receipt that advances the shared cursor", async () => {
    const runtime = createRuntime({
        batchBridge() {
            return {announced: true, processed: true, reason: "queued"};
        },
    });
    runtime.success(0, statePayload(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("operational", {
        ...statePayload(9, [assignmentEvent(8, 601, 61)]),
        caught_up: true,
    });
    runtime.runDelay(150);
    await settle();

    assert.equal(runtime.batchCalls.length, 1, "one batch call owns the assignment set");
    assert.deepEqual(
        runtime.batchCalls[0].items.map((item) => ({
            action: item.action,
            equipmentNumber: item.equipmentNumber,
            operationKey: item.operationKey,
        })),
        [{
            action: "excavator_truck_assigned",
            equipmentNumber: "61",
            operationKey: "excavator-assignment:assign:601:9",
        }]
    );
    assert.equal(runtime.itemCalls.length, 0, "announced batch must not fall back to a single item");

    const state = runtime.window.AppRealtime.getDebugState();
    assert.equal(state.appliedVersion, 9, "announced=true must confirm the shared cursor");
    assert.equal(state.pendingVersion, null);
});

test("processed=true without announced is not a receipt and keeps the cursor pending", async () => {
    let attempts = 0;
    const runtime = createRuntime({
        sseEnabled: false,
        batchBridge() {
            attempts += 1;
            if (attempts === 1) {
                // Нативный мост отвечает «работа принята в ledger», но голос
                // ещё не озвучен: по варианту A это НЕ квитанция.
                return {announced: false, processed: true, reason: "in_progress"};
            }
            return {announced: true, processed: true, reason: "queued"};
        },
    });
    runtime.success(0, {...statePayload(8), relevant: true, events: [assignmentEvent(8, 601, 61)]});
    await settle();

    let state = runtime.window.AppRealtime.getDebugState();
    assert.equal(attempts, 1);
    assert.equal(state.appliedVersion, 7, "processed=true must not move the delivery cursor");
    assert.equal(state.domAppliedVersion, 8, "the screen may already contain current truth");
    assert.equal(state.pendingVersion, 8);
    assert.equal(state.pendingEvents.length, 1);

    runtime.advance(2000);
    runtime.runDelay(2000);
    await settle();

    state = runtime.window.AppRealtime.getDebugState();
    assert.equal(attempts, 2);
    assert.equal(state.appliedVersion, 8);
    assert.equal(state.pendingVersion, null);
});

test("fresh HTML V9 replays stored V8 and lets the actual V9 removal suppress it", async () => {
    const sharedStorage = storage();
    const first = createRuntime({
        sharedStorage,
        sseEnabled: true,
        batchBridge() {
            return {announced: false, processed: false, reason: "in_progress"};
        },
    });
    first.success(0, statePayload(8, [assignmentEvent(8, 601, 61)]));
    await settle();
    assert.equal(first.window.AppRealtime.getDebugState().appliedVersion, 7);
    assert.equal(first.window.AppRealtime.getDebugState().pendingVersion, 8);

    const second = createRuntime({
        sharedStorage,
        initialVersion: 9,
        sseEnabled: true,
        batchBridge() {
            return {announced: true, processed: true, reason: "queued"};
        },
    });
    second.success(0, statePayload(9));
    await settle();
    second.runDelay(0);
    assert.equal(second.requests.length, 2, "restart replays from stored applied=7, not fresh HTML=9");
    assert.match(second.requests[1].url, /[?&]after=7(?:&|$)/);
    second.success(1, statePayload(9, [
        assignmentEvent(8, 601, 61),
        releaseEvent(9, 601, 61),
    ]));
    await settle();

    assert.equal(second.batchCalls.length, 1);
    assert.deepEqual(
        second.batchCalls[0].items.map((item) => ({action: item.action, operationKey: item.operationKey})),
        [{
            action: "excavator_truck_removed",
            operationKey: "excavator-assignment:remove:601:9",
        }]
    );
    assert.equal(second.window.AppRealtime.getDebugState().appliedVersion, 9);
    assert.equal(second.window.AppRealtime.getDebugState().pendingVersion, null);
});
