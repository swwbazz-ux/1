"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const sourcePath = path.resolve(__dirname, "../realtime-client.js");
const source = fs.readFileSync(sourcePath, "utf8");

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

function createRuntime({
    sseEnabled = true,
    refresh,
    signalHandler,
    sharedStorage,
    initialVersion = 7,
    nativeSync,
} = {}) {
    let now = 1_000_000;
    let nextTimer = 0;
    const timers = new Map();
    const intervals = new Map();
    const requests = [];
    const sources = [];
    const signals = [];
    let reloads = 0;
    const body = {
        dataset: {appRoleCode: "driver", operationalStateVersion: String(initialVersion)},
        classList: classList(),
    };
    const document = Object.assign(eventTarget(), {
        body,
        hidden: false,
        activeElement: null,
        hasFocus: () => true,
        querySelector: () => null,
    });
    class FakeEventSource {
        constructor(url) {
            this.url = url;
            this.closed = false;
            this.listeners = new Map();
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
    const navigator = {onLine: true};
    const window = Object.assign(eventTarget(), {
        document,
        navigator,
        EventSource: FakeEventSource,
        AbortController,
        location: {
            pathname: "/driver/shift/",
            origin: "https://driver.sse-pilot.local",
            href: "https://driver.sse-pilot.local/driver/shift/",
            reload() { reloads += 1; },
            replace() {},
        },
        localStorage: sharedStorage || storage(),
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
                name: "driver", role: "driver", path: "^/driver/", mode: "custom",
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
                if (init.signal) {
                    init.signal.addEventListener("abort", () => {
                        const error = new Error("aborted");
                        error.name = "AbortError";
                        reject(error);
                    });
                }
                requests.push(request);
            });
        },
        applyOperationalStateRefresh: refresh || ((context) => Promise.resolve({applied: true, version: context.version})),
        handleOperationalStateSignals(context) {
            signals.push(context);
            return signalHandler
                ? signalHandler(context)
                : Promise.resolve({accepted: true});
        },
    });
    if (nativeSync) {
        window.NativeBackgroundConnection = {syncOperationalState: nativeSync};
    }
    class FakeDate extends Date { static now() { return now; } }
    class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } }
    vm.runInNewContext(source, {
        window, document, navigator, EventSource: FakeEventSource, AbortController,
        CustomEvent, Date: FakeDate, URL, console,
    }, {filename: sourcePath});
    document.dispatchEvent({type: "DOMContentLoaded"});

    return {
        window, document, requests, sources, signals,
        get reloads() { return reloads; },
        advance(ms) { now += ms; },
        watchdog() { intervals.forEach((timer) => timer.callback()); },
        runDelay(delay) {
            for (const [id, timer] of [...timers]) {
                if (timer.delay === delay) {
                    timers.delete(id);
                    timer.callback();
                }
            }
        },
        runAllTimers(max = 40) {
            let ran = 0;
            while (ran < max) {
                const entry = [...timers.entries()][0];
                if (!entry) break;
                timers.delete(entry[0]);
                entry[1].callback();
                ran += 1;
            }
            return ran;
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
    for (let index = 0; index < 20; index += 1) await Promise.resolve();
}

function context(version, scope = "access-1|driver|truck-1", epoch = "epoch-1") {
    return {
        version,
        relevant: false,
        events: [],
        events_truncated: false,
        role_active: true,
        role_app_code: "driver",
        session_revision: "session-1",
        worker_equipment_ids: [1],
        journal_epoch: epoch,
        context: {cursor_scope: scope, role_app_code: "driver", worker_equipment_ids: [1], session_revision: "session-1"},
        journal: {epoch, min_version: version > 0 ? 1 : 0, max_version: version},
    };
}

function assignmentEvent(version = 8, assignmentId = "44") {
    return {
        version,
        type: "assignment_changed",
        object_type: "HaulAssignment",
        object_id: assignmentId,
        payload: {action: "assignment_pending", target_excavator_number: "02"},
    };
}

test("feature flag is off-compatible and never constructs EventSource", async () => {
    const runtime = createRuntime({sseEnabled: false});
    runtime.success(0, context(7));
    await settle();
    assert.equal(runtime.sources.length, 0);
    assert.equal(runtime.window.AppRealtime.getDebugState().sseState, "disabled");
});

test("SSE handoff waits for validated synchronized data, not TCP open", async () => {
    const runtime = createRuntime();
    runtime.success(0, context(7));
    await settle();
    assert.equal(runtime.sources.length, 1);
    const stream = runtime.sources[0];
    assert.match(stream.url, /[?&]applied=7(?:&|$)/);
    assert.match(stream.url, /[?&]after=7(?:&|$)/);
    stream.emit("open", {});
    assert.equal(runtime.window.AppRealtime.getDebugState().ssePrimaryActive, false);
    assert.equal(runtime.document.body.dataset.realtimeTransport, "sse-syncing");
    stream.emit("heartbeat", context(7));
    assert.equal(runtime.window.AppRealtime.getDebugState().ssePrimaryActive, true);
    assert.equal(runtime.document.body.dataset.realtimeTransport, "sse");
});

test("paged catch-up coalesces load plus later cancellation before one refresh/signal", async () => {
    let refreshCount = 0;
    let refreshContext = null;
    const runtime = createRuntime({refresh(ctx) {
        refreshCount += 1;
        refreshContext = ctx;
        return Promise.resolve({applied: true, version: ctx.version});
    }});
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("operational", {
        ...context(8), relevant: true, caught_up: false,
        journal: {epoch: "epoch-1", min_version: 1, max_version: 9},
        events: [{version: 8, type: "trip_changed", payload: {action: "truck_loaded", trip_id: 51, dump_point_id: 3}}],
    });
    assert.equal(runtime.signals.length, 0);
    assert.equal(refreshCount, 0);
    stream.emit("operational", {
        ...context(9), relevant: true, caught_up: true,
        events: [{version: 9, type: "trip_changed", payload: {action: "truck_loaded_cancel", trip_id: 51}}],
    });
    assert.equal(runtime.signals.length, 0);
    runtime.runDelay(150);
    await settle();
    assert.equal(runtime.signals.length, 1);
    assert.deepEqual(
        Array.from(runtime.signals[0].events, (item) => String(item.payload.action)),
        ["truck_loaded", "truck_loaded_cancel"]
    );
    assert.equal(refreshCount, 1);
    assert.equal(refreshContext.snapshotRequired, true);
    assert.equal(refreshContext.signalsAfterApply, true);
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 9);
});

test("SSE reset forwards snapshot_url and reconnects strictly after the returned snapshot version", async () => {
    const refreshes = [];
    const runtime = createRuntime({refresh(ctx) {
        refreshes.push(ctx);
        return Promise.resolve({applied: true, version: 12, snapshotVersion: 12});
    }});
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("reset", {
        ...context(10),
        reason: "journal_gap",
        snapshot_required: true,
        snapshot_url: "/driver/?_operational_snapshot=1",
    });
    await settle();

    assert.equal(refreshes.length, 1);
    assert.equal(refreshes[0].snapshotRequired, true);
    assert.equal(refreshes[0].snapshotUrl, "/driver/?_operational_snapshot=1");
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 12);
    assert.equal(runtime.sources.length, 2);
    assert.match(runtime.sources[1].url, /[?&]applied=12(?:&|$)/);
    assert.doesNotMatch(runtime.sources[1].url, /[?&]applied=10(?:&|$)/);
});

test("snapshot S above page H waits for H+1..S cancellation before voice arbitration", async () => {
    const refreshes = [];
    const resolvers = [];
    const runtime = createRuntime({refresh(ctx) {
        refreshes.push(ctx);
        return new Promise((resolve) => resolvers.push(resolve));
    }});
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("operational", {
        ...context(8), relevant: true, caught_up: true,
        events: [{
            version: 8, type: "assignment_changed", object_type: "HaulAssignment", object_id: "44",
            payload: {action: "assignment_pending", target_excavator_number: "02"},
        }],
    });
    runtime.runDelay(150);
    await settle();
    assert.equal(refreshes.length, 1);
    assert.equal(runtime.signals.length, 0, "voice must wait for truth");
    assert.equal(refreshes[0].isCurrentSnapshot(9), false);
    resolvers[0]({applied: true, version: 9, snapshotVersion: 9});
    await settle();
    assert.equal(runtime.signals.length, 0, "a snapshot ahead of the stream must stay silent");
    assert.equal(
        runtime.window.AppRealtime.getDebugState().appliedVersion,
        7,
        "received page H is not a durable delivery commit before current voice arbitration"
    );
    assert.equal(runtime.window.AppRealtime.getDebugState().pendingVersion, 9);
    assert.equal(refreshes.length, 1, "the client must wait for the missing SSE range");

    stream.emit("operational", {
        ...context(9), relevant: true, caught_up: true,
        events: [{
            version: 9, type: "assignment_changed", object_type: "HaulAssignment", object_id: "44",
            payload: {action: "pending_cancelled"},
        }],
    });
    runtime.runDelay(150);
    await settle();
    assert.equal(refreshes.length, 2);
    assert.equal(refreshes[1].isCurrentSnapshot(9), true);
    resolvers[1]({applied: true, version: 9, snapshotVersion: 9});
    await settle();
    assert.equal(runtime.signals.length, 1);
    assert.deepEqual(
        Array.from(runtime.signals[0].events, (event) => String(event.payload.action)),
        ["assignment_pending", "pending_cancelled"]
    );
});

test("native high-water is only a wake hint and addressed poll starts after applied", async () => {
    const runtime = createRuntime({sseEnabled: false});
    runtime.success(0, context(7));
    await settle();
    runtime.window.dispatchEvent({type: "native-connection-state", detail: {
        status: "success", lastSuccessAtMs: 1_000_000, occurredAtMs: 1_000_000, serverVersion: 20,
    }});
    runtime.runDelay(0);
    assert.equal(runtime.requests.length, 2);
    assert.match(runtime.requests[1].url, /[?&]after=7(?:&|$)/);
    assert.equal(runtime.window.AppRealtime.getDebugState().wakeHintVersion, 20);
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 7);
});

test("native authoritative reset reloads visible WebView even when cursor moves down", async () => {
    const runtime = createRuntime({sseEnabled: false});
    runtime.success(0, context(7));
    await settle();
    runtime.window.dispatchEvent({type: "native-connection-state", detail: {
        status: "success",
        lastSuccessAtMs: 1_000_000,
        occurredAtMs: 1_000_000,
        serverVersion: 0,
        operationalResetRequired: true,
        operationalResetReason: "cursor_ahead",
        operationalResetToVersion: 0,
    }});
    assert.equal(runtime.reloads, 1);
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 7);
});

test("SSE error returns to existing poll fallback and reconnect state", async () => {
    const runtime = createRuntime();
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("heartbeat", context(7));
    assert.equal(runtime.window.AppRealtime.getDebugState().ssePrimaryActive, true);
    stream.emit("error", {});
    assert.equal(stream.closed, true);
    assert.equal(runtime.window.AppRealtime.getDebugState().ssePrimaryActive, false);
    assert.equal(runtime.window.AppRealtime.getDebugState().sseState, "fallback");
    assert.equal(runtime.requests.length, 2);
});

test("missing heartbeat is treated as hung stream and falls back to poll", async () => {
    const runtime = createRuntime();
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("heartbeat", context(7));
    runtime.advance(30001);
    runtime.watchdog();
    assert.equal(stream.closed, true);
    assert.equal(runtime.window.AppRealtime.getDebugState().ssePrimaryActive, false);
    assert.equal(runtime.requests.length, 2);
});

test("poll fallback retains cursor and persisted event until durable voice handoff succeeds", async () => {
    let attempts = 0;
    const runtime = createRuntime({
        sseEnabled: false,
        signalHandler() {
            attempts += 1;
            return Promise.resolve({accepted: attempts > 1});
        },
    });
    runtime.success(0, {...context(8), relevant: true, events: [assignmentEvent()]});
    await settle();

    let state = runtime.window.AppRealtime.getDebugState();
    assert.equal(state.appliedVersion, 7, "failed bridge/queue must not advance shared cursor");
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
    assert.equal(runtime.signals.length, 2);
});

test("destroyed WebView restores the unconfirmed voice batch before cursor commit", async () => {
    const sharedStorage = storage();
    const never = new Promise(() => {});
    const first = createRuntime({
        sseEnabled: false,
        sharedStorage,
        signalHandler() { return never; },
    });
    first.success(0, {...context(8), relevant: true, events: [assignmentEvent()]});
    await settle();
    assert.equal(first.window.AppRealtime.getDebugState().appliedVersion, 7);
    assert.equal(first.window.AppRealtime.getDebugState().pendingEvents.length, 1);

    const second = createRuntime({
        sseEnabled: false,
        sharedStorage,
        signalHandler() { return Promise.resolve({accepted: true}); },
    });
    second.success(0, context(8));
    await settle();
    assert.equal(second.signals.length, 1);
    assert.equal(second.signals[0].events[0].object_id, "44");
    assert.equal(second.window.AppRealtime.getDebugState().appliedVersion, 8);
    assert.equal(second.window.AppRealtime.getDebugState().domAppliedVersion, 8);
});

test("persistent delivery cursor never substitutes for the cached DOM snapshot", async () => {
    const sharedStorage = storage();
    const key = "operational-delivery-v1:"
        + encodeURIComponent("access-1|driver|truck-1") + ":"
        + encodeURIComponent("epoch-1");
    sharedStorage.setItem(key, JSON.stringify({
        contextKey: "access-1|driver|truck-1",
        journalEpoch: "epoch-1",
        appliedVersion: 10,
        pendingVersion: 0,
    }));
    const refreshes = [];
    const runtime = createRuntime({
        sharedStorage,
        refresh(ctx) {
            refreshes.push(ctx);
            return Promise.resolve({applied: true, snapshotVersion: 10});
        },
    });
    runtime.success(0, context(10));
    await settle();

    assert.equal(refreshes.length, 1, "cached DOM 7 must fetch/apply snapshot 10");
    assert.equal(runtime.signals.length, 0, "persistently accepted voice must not replay");
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 10);
    assert.equal(runtime.window.AppRealtime.getDebugState().domAppliedVersion, 10);
    assert.match(runtime.sources[0].url, /[?&]applied=10(?:&|$)/);
});

for (const resetTarget of [3, 0]) {
    test(`trusted SSE reset applies authoritative lower cursor 7→${resetTarget}`, async () => {
        const nativeCalls = [];
        const runtime = createRuntime({
            nativeSync(payload) {
                nativeCalls.push(payload);
                return Promise.resolve({resetAccepted: payload.authoritativeReset === true});
            },
            refresh(ctx) {
                return Promise.resolve({applied: true, snapshotVersion: resetTarget, version: resetTarget});
            },
        });
        runtime.success(0, context(7));
        await settle();
        const stream = runtime.sources[0];
        stream.emit("open", {});
        stream.emit("reset", {...context(resetTarget), reason: "cursor_ahead"});
        assert.equal(
            nativeCalls.some((call) => call.authoritativeReset === true),
            false,
            "native resetApplied is a final commit and must not run before the handoff microtask"
        );
        await settle();

        const state = runtime.window.AppRealtime.getDebugState();
        assert.equal(state.appliedVersion, resetTarget);
        assert.equal(state.domAppliedVersion, resetTarget);
        assert.ok(nativeCalls.some((call) => (
            call.authoritativeReset === true && call.resetVersion === resetTarget
        )));
        assert.match(runtime.sources.at(-1).url, new RegExp(`[?&]applied=${resetTarget}(?:&|$)`));
    });
}

test("reset invalidates an older in-flight fragment and accepts the new epoch snapshot", async () => {
    const refreshes = [];
    const resolvers = [];
    const runtime = createRuntime({
        nativeSync(payload) { return Promise.resolve({resetAccepted: payload.authoritativeReset === true}); },
        refresh(ctx) {
            refreshes.push(ctx);
            return new Promise((resolve) => resolvers.push(resolve));
        },
    });
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("operational", {
        ...context(8), relevant: true, caught_up: true, events: [assignmentEvent()],
    });
    runtime.runDelay(150);
    await settle();
    assert.equal(refreshes.length, 1);

    stream.emit("reset", {...context(0, "access-1|driver|truck-1", "epoch-2"), reason: "journal_epoch"});
    await settle();
    assert.equal(refreshes.length, 2);
    resolvers[0]({applied: true, snapshotVersion: 8});
    await settle();
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 0);
    resolvers[1]({applied: true, snapshotVersion: 0});
    await settle();
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 0);
    assert.equal(runtime.window.AppRealtime.getDebugState().journalEpoch, "epoch-2");
});

test("stale reset for another context cannot lower the active cursor", async () => {
    const runtime = createRuntime();
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("reset", {...context(0, "access-2|driver|truck-9"), reason: "cursor_ahead"});
    await settle();
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 7);
    assert.equal(runtime.window.AppRealtime.getDebugState().deliveryContextKey, "access-1|driver|truck-1");
});

test("R3 A: V9 survives while the accepted V8 voice handoff is awaiting", async () => {
    let resolveFirst;
    const handedOff = [];
    const runtime = createRuntime({
        signalHandler(details) {
            handedOff.push(Array.from(details.events, (event) => Number(event.version)));
            if (handedOff.length === 1) {
                return new Promise((resolve) => { resolveFirst = resolve; });
            }
            return Promise.resolve({accepted: true});
        },
    });
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("operational", {
        ...context(8), relevant: true, caught_up: true, events: [assignmentEvent(8, "44")],
    });
    runtime.runDelay(150);
    await settle();
    stream.emit("operational", {
        ...context(9), relevant: true, caught_up: true, events: [assignmentEvent(9, "45")],
    });
    runtime.runDelay(150);
    await settle();
    assert.deepEqual(
        Array.from(runtime.window.AppRealtime.getDebugState().pendingEvents, (event) => Number(event.version)),
        [8, 9]
    );

    resolveFirst({accepted: true});
    await settle();
    let state = runtime.window.AppRealtime.getDebugState();
    assert.equal(state.appliedVersion, 8);
    assert.equal(state.pendingVersion, 9);
    assert.deepEqual(Array.from(state.pendingEvents, (event) => Number(event.version)), [9]);

    runtime.runDelay(0);
    await settle();
    state = runtime.window.AppRealtime.getDebugState();
    assert.equal(state.appliedVersion, 9, JSON.stringify({state, handedOff}));
    assert.deepEqual(handedOff, [[8], [9]]);
});

test("R3 A: rejected V8 handoff retains V8 plus its later cancellation V9", async () => {
    let resolveFirst;
    const handedOff = [];
    const runtime = createRuntime({
        signalHandler(details) {
            handedOff.push(Array.from(details.events, (event) => String(event.payload.action)));
            if (handedOff.length === 1) {
                return new Promise((resolve) => { resolveFirst = resolve; });
            }
            return Promise.resolve({accepted: true});
        },
    });
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("operational", {
        ...context(8), relevant: true, caught_up: true, events: [assignmentEvent(8, "44")],
    });
    runtime.runDelay(150);
    await settle();
    stream.emit("operational", {
        ...context(9), relevant: true, caught_up: true,
        events: [{...assignmentEvent(9, "44"), payload: {action: "pending_cancelled"}}],
    });
    runtime.runDelay(150);
    await settle();

    resolveFirst({accepted: false, reason: "in_progress"});
    await settle();
    let state = runtime.window.AppRealtime.getDebugState();
    assert.equal(state.appliedVersion, 7);
    assert.equal(state.pendingVersion, 9);
    assert.deepEqual(Array.from(state.pendingEvents, (event) => Number(event.version)), [8, 9]);

    runtime.advance(2000);
    runtime.runDelay(2000);
    await settle();
    state = runtime.window.AppRealtime.getDebugState();
    assert.equal(state.appliedVersion, 9, JSON.stringify({state, handedOff}));
    assert.deepEqual(handedOff, [["assignment_pending"], ["assignment_pending", "pending_cancelled"]]);
});

test("R3 B: fresh HTML V8 cannot erase the persisted unconfirmed V8 voice", async () => {
    const sharedStorage = storage();
    const first = createRuntime({
        sseEnabled: false,
        sharedStorage,
        signalHandler() { return new Promise(() => {}); },
    });
    first.success(0, {...context(8), relevant: true, events: [assignmentEvent(8, "44")]});
    await settle();
    assert.equal(first.window.AppRealtime.getDebugState().appliedVersion, 7);

    const second = createRuntime({
        sseEnabled: false,
        initialVersion: 8,
        sharedStorage,
        signalHandler() { return Promise.resolve({accepted: true}); },
    });
    second.success(0, context(8));
    await settle();
    second.runDelay(0);
    assert.equal(second.requests.length, 2, "restart must replay from stored applied=7, not fresh HTML=8");
    assert.match(second.requests[1].url, /[?&]after=7(?:&|$)/);
    second.success(1, {...context(8), relevant: true, events: [assignmentEvent(8, "44")]});
    await settle();
    assert.equal(second.signals.length, 1);
    assert.equal(second.signals[0].events[0].object_id, "44");
    assert.equal(second.window.AppRealtime.getDebugState().appliedVersion, 8);
});

for (const htmlVersion of [7, 8]) {
    for (const sseEnabled of [false, true]) {
        test(`R3 B matrix: pending V8 survives HTML V${htmlVersion} with SSE ${sseEnabled ? "on" : "off"}`, async () => {
            const sharedStorage = storage();
            const first = createRuntime({
                sseEnabled,
                sharedStorage,
                signalHandler() { return new Promise(() => {}); },
            });
            first.success(0, {...context(8), relevant: true, events: [assignmentEvent(8, "44")]});
            await settle();
            assert.equal(first.window.AppRealtime.getDebugState().appliedVersion, 7);

            const second = createRuntime({
                sseEnabled,
                initialVersion: htmlVersion,
                sharedStorage,
                signalHandler() { return Promise.resolve({accepted: true}); },
            });
            if (htmlVersion === 7) {
                second.success(0, {...context(8), relevant: true, events: [assignmentEvent(8, "44")]});
            } else {
                second.success(0, context(8));
                await settle();
                second.runDelay(0);
                assert.equal(second.requests.length, 2);
                assert.match(second.requests[1].url, /[?&]after=7(?:&|$)/);
                second.success(1, {...context(8), relevant: true, events: [assignmentEvent(8, "44")]});
            }
            await settle();

            assert.equal(second.signals.length, 1);
            assert.equal(second.signals[0].events[0].object_id, "44");
            assert.equal(second.window.AppRealtime.getDebugState().appliedVersion, 8);
            assert.equal(second.window.AppRealtime.getDebugState().pendingVersion, null);
        });
    }
}

test("R3 C: snapshotAhead keeps delivery cursor before H until voice arbitration", async () => {
    const refreshes = [];
    const resolvers = [];
    const nativeCalls = [];
    const runtime = createRuntime({
        nativeSync(details) {
            nativeCalls.push({...details});
            return Promise.resolve({});
        },
        refresh(details) {
            refreshes.push(details);
            return new Promise((resolve) => resolvers.push(resolve));
        },
    });
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("operational", {
        ...context(8), relevant: true, caught_up: true, events: [assignmentEvent(8, "44")],
    });
    runtime.runDelay(150);
    await settle();
    resolvers[0]({applied: true, snapshotVersion: 9});
    await settle();

    const state = runtime.window.AppRealtime.getDebugState();
    assert.equal(state.appliedVersion, 7);
    assert.equal(state.domAppliedVersion, 9);
    assert.equal(state.pendingVersion, 9);
    assert.equal(runtime.signals.length, 0);
    assert.equal(nativeCalls.at(-1).appliedVersion, 7);
    assert.equal(nativeCalls.at(-1).pendingVersion, 9);
});

test("T-016 §10.3: SSE re-arms after a pause even when /realtime/state/ is unavailable", async () => {
    const runtime = createRuntime();
    runtime.success(0, context(7));
    await settle();
    const first = runtime.sources[0];
    first.emit("open", {});
    first.emit("heartbeat", context(7));
    assert.equal(runtime.document.body.dataset.realtimeTransport, "sse");
    assert.equal(runtime.window.AppRealtime.getDebugState().ssePrimaryActive, true);

    // Уход в фон закрывает поток без переподключения; возврат в активное
    // состояние запускает опрос, но /realtime/state/ недоступен.
    runtime.window.dispatchEvent({type: "blur"});
    assert.equal(runtime.window.AppRealtime.getDebugState().sseState, "standby");
    runtime.window.dispatchEvent({type: "focus"});
    await settle();
    for (let index = 1; index < runtime.requests.length; index += 1) runtime.fail(index);
    await settle();

    assert.equal(runtime.sources.length, 1, "stream must still be closed right after the failed poll");
    runtime.advance(2000);
    runtime.watchdog();
    await settle();
    runtime.runAllTimers(12);
    await settle();

    assert.ok(
        runtime.sources.length >= 2,
        "SSE was not re-armed while /realtime/state/ stayed unavailable"
    );
    const revived = runtime.sources.at(-1);
    revived.emit("open", {});
    revived.emit("operational", {
        ...context(9), relevant: false, caught_up: true, high_water_version: 9, events: [],
    });
    runtime.runDelay(150);
    await settle();

    const state = runtime.window.AppRealtime.getDebugState();
    assert.equal(runtime.document.body.dataset.realtimeTransport, "sse");
    assert.equal(state.ssePrimaryActive, true);
    assert.equal(state.appliedVersion, 9, "cursor must catch up to the server high-water after reconnect");
});

test("T-016 §10.1: a voice handoff that never resolves cannot freeze the screen or cursor", async () => {
    let acceptVoice = false;
    let calls = 0;
    const degraded = [];
    const runtime = createRuntime({
        signalHandler() {
            calls += 1;
            return acceptVoice ? Promise.resolve({accepted: true}) : new Promise(() => {});
        },
    });
    runtime.window.addEventListener("operational-voice-handoff-degraded", (event) => {
        degraded.push(event.detail);
    });
    runtime.success(0, context(7));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("heartbeat", context(7));
    stream.emit("operational", {
        ...context(8), relevant: true, caught_up: true, events: [assignmentEvent(8, "44")],
    });
    runtime.runDelay(150);
    await settle();
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 7);
    assert.equal(runtime.window.AppRealtime.getDebugState().pendingVersion, 8);

    for (let tick = 0; tick < 12 && runtime.window.AppRealtime.getDebugState().appliedVersion < 8; tick += 1) {
        runtime.advance(6000);
        runtime.runAllTimers(8);
        await settle();
    }

    let state = runtime.window.AppRealtime.getDebugState();
    assert.equal(state.appliedVersion, 8, JSON.stringify({state, calls}));
    assert.equal(state.domAppliedVersion, 8);
    assert.equal(state.pendingVersion, null);
    assert.equal(calls, 1, "a timed-out handoff must be reused, not re-dispatched on every retry");
    assert.equal(degraded.length, 1, "one diagnostic event per degraded batch");
    assert.equal(degraded[0].reason, "voice_handoff_failed");
    assert.equal(degraded[0].version, 8);
    const applied = [];
    runtime.window.addEventListener("operational-state-refresh-applied", (event) => applied.push(event.detail));
    assert.equal(state.deferredVoiceVersion, 8, "unconfirmed voice work must be kept for the next snapshot");

    // Следующее событие: озвучка снова работает, отложенная работа уходит один раз.
    acceptVoice = true;
    stream.emit("operational", {
        ...context(9), relevant: true, caught_up: true, events: [assignmentEvent(9, "45")],
    });
    runtime.runDelay(150);
    await settle();
    state = runtime.window.AppRealtime.getDebugState();
    assert.equal(state.appliedVersion, 9);
    assert.equal(state.deferredVoiceVersion, 0, "deferred batch must be flushed exactly once");
    const versionsWithV8 = () => runtime.signals.filter(
        (signal) => signal.events.some((event) => Number(event.version) === 8)
    ).length;
    assert.equal(
        versionsWithV8(),
        2,
        `the original attempt plus exactly one deferred retry, saw ${JSON.stringify(runtime.signals.map((signal) => signal.events.map((event) => Number(event.version))))}`
    );

    // Следующий период истины не повторяет ни отложенную, ни уже принятую работу.
    for (let step = 0; step < 6; step += 1) {
        runtime.advance(10000);
        stream.emit("heartbeat", context(9));
        runtime.watchdog();
        await settle();
        runtime.runAllTimers(4);
        await settle();
    }
    assert.equal(versionsWithV8(), 2, "deferred voice must not be replayed on every refresh");
});

test("T-016 §8.2: the 60s truth refresh is fragment-only, does not poll state and repeats no voice", async () => {
    const refreshes = [];
    const runtime = createRuntime({
        refresh(ctx) {
            refreshes.push(ctx);
            return Promise.resolve({applied: true, version: ctx.version});
        },
    });
    runtime.success(0, context(9));
    await settle();
    const stream = runtime.sources[0];
    stream.emit("open", {});
    stream.emit("heartbeat", context(9));
    runtime.runDelay(150);
    await settle();
    assert.equal(runtime.document.body.dataset.realtimeTransport, "sse");
    const requestsBefore = runtime.requests.length;

    // Здоровый поток: heartbeat каждые 10 с, 60 с без бизнес-событий.
    for (let step = 0; step < 6; step += 1) {
        runtime.advance(10000);
        stream.emit("heartbeat", context(9));
        runtime.watchdog();
        await settle();
        runtime.runAllTimers(4);
        await settle();
    }

    assert.equal(runtime.requests.length, requestsBefore, "no /realtime/state/ request while SSE is healthy");
    assert.equal(runtime.signals.length, 0, "the periodic truth refresh must not repeat voice work");
    assert.ok(refreshes.length >= 1, "the 60s truth refresh must still re-read the screen fragment");
    assert.equal(runtime.window.AppRealtime.getDebugState().appliedVersion, 9);
});
