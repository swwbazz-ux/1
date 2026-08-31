"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const REALTIME_CLIENT_PATH = path.resolve(__dirname, "..", "realtime-client.js");
const REALTIME_CLIENT_SOURCE = fs.readFileSync(REALTIME_CLIENT_PATH, "utf8");
const BASE_TEMPLATE_PATH = path.resolve(__dirname, "..", "..", "..", "templates", "base.html");
const BASE_TEMPLATE_SOURCE = fs.readFileSync(BASE_TEMPLATE_PATH, "utf8");
const MOBILE_QUEUE_KEY = "realtime-auth-runtime-queue";

class FakeClassList {
    constructor() {
        this.values = new Set();
    }

    add(...names) {
        names.forEach((name) => this.values.add(name));
    }

    remove(...names) {
        names.forEach((name) => this.values.delete(name));
    }

    contains(name) {
        return this.values.has(name);
    }

    toggle(name, force) {
        const enabled = typeof force === "boolean" ? force : !this.values.has(name);
        if (enabled) {
            this.values.add(name);
        } else {
            this.values.delete(name);
        }
        return enabled;
    }
}

function createEventTarget() {
    const listeners = new Map();
    return {
        addEventListener(type, listener) {
            const registered = listeners.get(type) || [];
            registered.push(listener);
            listeners.set(type, registered);
        },
        removeEventListener(type, listener) {
            const registered = listeners.get(type) || [];
            listeners.set(type, registered.filter((candidate) => candidate !== listener));
        },
        dispatchEvent(event) {
            if (!event || !event.type) {
                throw new TypeError("Event type is required");
            }
            const registered = (listeners.get(event.type) || []).slice();
            registered.forEach((listener) => listener.call(this, event));
            return true;
        },
    };
}

function createStorage(initialValues) {
    const values = new Map(
        Object.entries(initialValues || {}).map(([key, value]) => [key, String(value)])
    );
    return {
        getItem(key) {
            return values.has(String(key)) ? values.get(String(key)) : null;
        },
        setItem(key, value) {
            values.set(String(key), String(value));
        },
        removeItem(key) {
            values.delete(String(key));
        },
        clear() {
            values.clear();
        },
    };
}

function response(status, payload) {
    return {
        status,
        ok: status >= 200 && status < 300,
        json() {
            return Promise.resolve(payload);
        },
    };
}

function createRuntime(options) {
    const runtimeOptions = options || {};
    const windowTarget = createEventTarget();
    const documentTarget = createEventTarget();
    const redirects = [];
    const reloads = [];
    const fetchCalls = [];
    const readonlyCalls = [];
    const activeRoleEvents = [];
    const refreshAppliedEvents = [];
    const refreshDeferredEvents = [];
    const refreshSkippedEvents = [];
    const intervals = new Map();
    const timeouts = new Map();
    let nextTimerId = 1;
    let currentHref = runtimeOptions.currentHref || "http://driver.localhost/driver/";

    const location = {
        get href() {
            return currentHref;
        },
        set href(value) {
            const nextHref = new URL(String(value), currentHref).href;
            redirects.push({method: "href", value: nextHref});
            currentHref = nextHref;
        },
        get origin() {
            return new URL(currentHref).origin;
        },
        get pathname() {
            return new URL(currentHref).pathname;
        },
        assign(value) {
            const nextHref = new URL(String(value), currentHref).href;
            redirects.push({method: "assign", value: nextHref});
            currentHref = nextHref;
        },
        replace(value) {
            const nextHref = new URL(String(value), currentHref).href;
            redirects.push({method: "replace", value: nextHref});
            currentHref = nextHref;
        },
        reload() {
            reloads.push(true);
        },
    };

    const body = {
        dataset: {
            roleAccessActive: "true",
            operationalStateVersion: "7",
            appRoleCode: runtimeOptions.appRoleCode || "driver",
        },
        classList: new FakeClassList(),
    };
    const document = Object.assign(documentTarget, {
        body,
        activeElement: null,
        hidden: runtimeOptions.hidden === true,
        hasFocus() {
            return runtimeOptions.focused !== false;
        },
        querySelector() {
            return null;
        },
    });
    const localStorage = createStorage({
        [MOBILE_QUEUE_KEY]: JSON.stringify([{id: "pending-action"}]),
    });
    const sessionStorage = createStorage();
    const navigator = {onLine: runtimeOptions.online !== false};

    const fetchResponses = Array.isArray(runtimeOptions.fetchResponses)
        ? runtimeOptions.fetchResponses.slice()
        : [];
    const fetchImplementation = runtimeOptions.fetch || function () {
        if (!fetchResponses.length) {
            throw new Error("Unexpected fetch");
        }
        const nextResponse = fetchResponses.shift();
        return Promise.resolve(
            typeof nextResponse === "function" ? nextResponse() : nextResponse
        );
    };

    const window = Object.assign(windowTarget, {
        AppRealtimeConfig: {
            stateUrl: "/api/operational-state/version/",
            initialVersion: typeof runtimeOptions.initialVersion === "number"
                ? runtimeOptions.initialVersion
                : 7,
            workPollIntervalMs: typeof runtimeOptions.workPollIntervalMs === "number"
                ? runtimeOptions.workPollIntervalMs
                : 5000,
            observerPollIntervalMs: typeof runtimeOptions.observerPollIntervalMs === "number"
                ? runtimeOptions.observerPollIntervalMs
                : 15000,
            idleDelayMs: typeof runtimeOptions.idleDelayMs === "number"
                ? runtimeOptions.idleDelayMs
                : 1,
            pollTimeoutMs: 8000,
            maxSilentMs: 7000,
            mobileQueueKey: MOBILE_QUEUE_KEY,
            screens: runtimeOptions.screens || [{
                name: "driver",
                role: "driver",
                mode: "custom",
                path: "^/driver/?$",
                customRefresh: true,
            }],
            customRefreshPaths: ["^/driver/?$"],
        },
        location,
        localStorage,
        sessionStorage,
        navigator,
        AbortController,
        fetch(url, fetchOptions) {
            fetchCalls.push({url, options: fetchOptions});
            return fetchImplementation(url, fetchOptions);
        },
        applyAppRoleReadonlyState(isActive) {
            readonlyCalls.push(isActive);
        },
        requestAnimationFrame(callback) {
            callback();
            return 0;
        },
        setTimeout(callback, delay) {
            const timerId = nextTimerId++;
            timeouts.set(timerId, {callback, delay: Number(delay || 0)});
            return timerId;
        },
        clearTimeout(timerId) {
            timeouts.delete(timerId);
        },
        setInterval(callback, delay) {
            const timerId = nextTimerId++;
            intervals.set(timerId, {callback, delay: Number(delay || 0)});
            return timerId;
        },
        clearInterval(timerId) {
            intervals.delete(timerId);
        },
    });
    if (typeof runtimeOptions.applyOperationalStateRefresh === "function") {
        window.applyOperationalStateRefresh = runtimeOptions.applyOperationalStateRefresh;
    }
    document.location = location;
    window.window = window;
    window.document = document;

    window.addEventListener("active-role-state-changed", (event) => {
        activeRoleEvents.push(event.detail);
    });
    window.addEventListener("operational-state-refresh-applied", (event) => {
        refreshAppliedEvents.push(event.detail);
    });
    window.addEventListener("operational-state-refresh-deferred", (event) => {
        refreshDeferredEvents.push(event.detail);
    });
    window.addEventListener("operational-state-refresh-skipped", (event) => {
        refreshSkippedEvents.push(event.detail);
    });

    class FakeCustomEvent {
        constructor(type, init) {
            this.type = type;
            this.detail = init && init.detail ? init.detail : {};
        }
    }

    const context = vm.createContext({
        window,
        document,
        navigator,
        CustomEvent: FakeCustomEvent,
        AbortController,
        URL,
        Promise,
        Error,
        Date,
        JSON,
        Math,
        Number,
        Object,
        Array,
        RegExp,
        String,
        Boolean,
        console,
    });
    vm.runInContext(REALTIME_CLIENT_SOURCE, context, {
        filename: REALTIME_CLIENT_PATH,
    });
    document.dispatchEvent({type: "DOMContentLoaded"});

    return {
        window,
        document,
        navigator,
        localStorage,
        sessionStorage,
        fetchCalls,
        readonlyCalls,
        activeRoleEvents,
        refreshAppliedEvents,
        refreshDeferredEvents,
        refreshSkippedEvents,
        redirects,
        reloads,
        runIntervals() {
            Array.from(intervals.values()).forEach(({callback}) => callback());
        },
        intervalDelays() {
            return Array.from(intervals.values()).map(({delay}) => delay);
        },
        timeoutDelays() {
            return Array.from(timeouts.values()).map(({delay}) => delay);
        },
        flushZeroTimers() {
            let ranTimer = true;
            while (ranTimer) {
                ranTimer = false;
                for (const [timerId, timer] of Array.from(timeouts.entries())) {
                    if (timer.delay !== 0) {
                        continue;
                    }
                    timeouts.delete(timerId);
                    timer.callback();
                    ranTimer = true;
                }
            }
        },
        runAllTimeouts() {
            for (const [timerId, timer] of Array.from(timeouts.entries())) {
                timeouts.delete(timerId);
                timer.callback();
            }
        },
        runTimeoutsUpTo(maxDelay) {
            for (const [timerId, timer] of Array.from(timeouts.entries())) {
                if (timer.delay > maxDelay) {
                    continue;
                }
                timeouts.delete(timerId);
                timer.callback();
            }
        },
        runOneTimeoutByDelay(targetDelay) {
            for (const [timerId, timer] of Array.from(timeouts.entries())) {
                if (timer.delay !== targetDelay) {
                    continue;
                }
                timeouts.delete(timerId);
                timer.callback();
                return true;
            }
            return false;
        },
    };
}

async function settlePromises() {
    for (let round = 0; round < 12; round += 1) {
        await Promise.resolve();
    }
}

function readQueue(runtime) {
    const rawQueue = runtime.localStorage.getItem(MOBILE_QUEUE_KEY);
    if (rawQueue === null) {
        return [];
    }
    return JSON.parse(rawQueue);
}

test("first realtime 401 terminates auth once and permanently stops this client", async () => {
    const runtime = createRuntime({
        fetch() {
            return Promise.resolve(response(401));
        },
    });
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 1, "DOMContentLoaded must perform the first poll");

    runtime.window.AppRealtime.poll({force: true});
    runtime.window.dispatchEvent({type: "focus"});
    runtime.window.dispatchEvent({type: "pageshow", persisted: true});
    runtime.runIntervals();
    runtime.flushZeroTimers();
    await settlePromises();

    assert.deepEqual(
        runtime.readonlyCalls,
        [false],
        "the first 401 must switch the open screen to read-only exactly once"
    );
    assert.equal(
        runtime.window.AppRealtime.getDebugState().authEnded,
        true,
        "the realtime client must remember that authentication has ended"
    );
    assert.equal(runtime.document.body.dataset.roleAccessActive, "false");
    assert.deepEqual(
        runtime.activeRoleEvents.map((event) => ({
            active: Boolean(event.active),
            activeRoleCode: String(event.activeRoleCode || ""),
            changedAt: String(event.changedAt || ""),
        })),
        [{
            active: false,
            activeRoleCode: "",
            changedAt: "",
        }],
        "one inactive-role event must reset already-running holds"
    );
    assert.deepEqual(readQueue(runtime), [], "a stale mobile mutation queue must be cleared");
    assert.equal(runtime.redirects.length, 1, "login redirect must be scheduled only once");
    assert.equal(new URL(runtime.redirects[0].value).pathname, "/");
    assert.equal(runtime.reloads.length, 0, "auth termination must not reload the stale PWA");
    assert.equal(
        runtime.fetchCalls.length,
        1,
        "poll, interval, focus and pageshow must never fetch again after auth termination"
    );
});

test("HTTP 500 is a connection failure, not authentication termination", async () => {
    const runtime = createRuntime({
        fetchResponses: [
            response(500),
            response(200, {version: 7, role_active: true}),
        ],
    });
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.window.AppRealtime.getDebugState().consecutiveFailures, 1);
    assert.notEqual(runtime.window.AppRealtime.getDebugState().authEnded, true);
    assert.deepEqual(runtime.readonlyCalls, []);
    assert.deepEqual(runtime.activeRoleEvents, []);
    assert.deepEqual(readQueue(runtime), [{id: "pending-action"}]);
    assert.equal(runtime.redirects.length, 0);

    runtime.window.AppRealtime.poll({force: true});
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 2, "polling must remain available after HTTP 500");
    assert.equal(runtime.redirects.length, 0);
});

test("server restart recovery applies a higher version through custom refresh", async () => {
    const customRefreshCalls = [];
    const runtime = createRuntime({
        idleDelayMs: -1,
        fetchResponses: [
            response(500),
            response(200, {
                version: 8,
                role_active: true,
                relevant: true,
                events_truncated: false,
                events: [{
                    version: 8,
                    type: "trip_changed",
                    payload: {truck_id: 10, excavator_id: 20},
                }],
            }),
        ],
        applyOperationalStateRefresh(context) {
            customRefreshCalls.push(context);
            return Promise.resolve({applied: true});
        },
    });
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.document.body.classList.contains("is-realtime-stale"), true);
    assert.equal(runtime.window.AppRealtime.getDebugState().currentVersion, 7);

    runtime.window.AppRealtime.poll({force: true});
    await settlePromises();

    const debugState = runtime.window.AppRealtime.getDebugState();
    assert.equal(runtime.fetchCalls.length, 2);
    assert.equal(customRefreshCalls.length, 1);
    assert.equal(customRefreshCalls[0].previousVersion, 7);
    assert.equal(customRefreshCalls[0].version, 8);
    assert.equal(customRefreshCalls[0].events.length, 1);
    assert.equal(customRefreshCalls[0].events[0].type, "trip_changed");
    assert.equal(customRefreshCalls[0].eventsTruncated, false);
    assert.equal(debugState.currentVersion, 8);
    assert.equal(debugState.pendingVersion, null);
    assert.equal(debugState.consecutiveFailures, 0);
    assert.equal(debugState.authEnded, false);
    assert.equal(runtime.sessionStorage.getItem("operational-state-version"), "8");
    assert.equal(runtime.document.body.classList.contains("is-realtime-stale"), false);
    assert.equal(runtime.refreshAppliedEvents.length, 1);
    assert.equal(runtime.refreshSkippedEvents.length, 0);
    assert.equal(runtime.reloads.length, 0);
});

test("irrelevant delta advances version without events GET, custom refresh or HTML reload", async () => {
    const customRefreshCalls = [];
    const runtime = createRuntime({
        idleDelayMs: -1,
        fetchResponses: [
            response(200, {
                version: 8,
                role_active: true,
                relevant: false,
                events: [],
                events_truncated: false,
            }),
        ],
        applyOperationalStateRefresh(context) {
            customRefreshCalls.push(context);
            return Promise.resolve({applied: true});
        },
    });

    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(
        new URL(runtime.fetchCalls[0].url).searchParams.get("include_events"),
        "1",
        "the light state request must already carry the server-filtered delta"
    );
    assert.equal(
        new URL(runtime.fetchCalls[0].url).searchParams.get("role_app_code"),
        "driver",
        "the poll must request the contract of the visible role application"
    );
    assert.equal(customRefreshCalls.length, 0);
    assert.equal(runtime.reloads.length, 0);
    assert.equal(runtime.window.AppRealtime.getDebugState().currentVersion, 8);
    assert.equal(runtime.sessionStorage.getItem("operational-state-version"), "8");
});

test("custom refresh false keeps the version pending until a retry applies it", async () => {
    const customRefreshCalls = [];
    const runtime = createRuntime({
        idleDelayMs: -1,
        fetchResponses: [
            response(200, {version: 8, role_active: true}),
        ],
        applyOperationalStateRefresh(context) {
            customRefreshCalls.push(context);
            if (customRefreshCalls.length === 1) {
                return Promise.resolve(false);
            }
            return Promise.resolve({applied: true});
        },
    });
    await settlePromises();

    let debugState = runtime.window.AppRealtime.getDebugState();
    assert.equal(customRefreshCalls.length, 1);
    assert.equal(debugState.currentVersion, 7);
    assert.equal(debugState.pendingVersion, 8);
    assert.equal(debugState.applyingUpdate, false);
    assert.equal(runtime.sessionStorage.getItem("operational-state-version"), null);
    assert.equal(runtime.refreshDeferredEvents.length, 1);
    assert.equal(
        runtime.refreshDeferredEvents[0].reason,
        "custom_refresh_not_applied"
    );
    assert.equal(runtime.refreshAppliedEvents.length, 0);
    assert.equal(runtime.refreshSkippedEvents.length, 0);
    assert.equal(runtime.reloads.length, 0);

    runtime.runTimeoutsUpTo(2000);
    await settlePromises();

    debugState = runtime.window.AppRealtime.getDebugState();
    assert.equal(customRefreshCalls.length, 2);
    assert.equal(debugState.currentVersion, 8);
    assert.equal(debugState.pendingVersion, null);
    assert.equal(debugState.applyingUpdate, false);
    assert.equal(runtime.sessionStorage.getItem("operational-state-version"), "8");
    assert.equal(runtime.refreshAppliedEvents.length, 1);
    assert.equal(runtime.refreshSkippedEvents.length, 0);
    assert.equal(runtime.reloads.length, 0);
});

test("hidden tab pauses failed HTML refresh retries until one visible catch-up", async () => {
    const customRefreshCalls = [];
    const relevantPayload = {
        version: 8,
        role_active: true,
        relevant: true,
        events_truncated: false,
        events: [{
            version: 8,
            type: "trip_changed",
            payload: {truck_id: 10},
        }],
    };
    const runtime = createRuntime({
        idleDelayMs: -1,
        fetchResponses: [
            response(200, relevantPayload),
            response(200, relevantPayload),
        ],
        applyOperationalStateRefresh(context) {
            customRefreshCalls.push(context);
            if (customRefreshCalls.length === 1) {
                return Promise.resolve(false);
            }
            return Promise.resolve({applied: true});
        },
    });
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(customRefreshCalls.length, 1);
    assert.equal(runtime.window.AppRealtime.getDebugState().pendingVersion, 8);

    runtime.document.hidden = true;
    runtime.document.dispatchEvent({type: "visibilitychange"});
    runtime.runAllTimeouts();
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 1, "hidden state must not poll again");
    assert.equal(
        customRefreshCalls.length,
        1,
        "hidden state must not retry the full-HTML refresh handler"
    );
    assert.equal(runtime.window.AppRealtime.getDebugState().pendingVersion, 8);

    runtime.document.hidden = false;
    runtime.document.dispatchEvent({type: "visibilitychange"});
    runtime.flushZeroTimers();
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 2, "visibility recovery performs one state catch-up");
    assert.equal(customRefreshCalls.length, 2, "the relevant catch-up retries once");
    assert.equal(runtime.window.AppRealtime.getDebugState().pendingVersion, null);
    assert.equal(runtime.window.AppRealtime.getDebugState().currentVersion, 8);
});

test("system admin uses the generic safe reload after server recovery", async () => {
    const systemAdminConfigLine = BASE_TEMPLATE_SOURCE
        .split(/\r?\n/)
        .find((line) => line.includes('name: "system-admin"'));
    const customRefreshPathsBlock = BASE_TEMPLATE_SOURCE.match(
        /customRefreshPaths:\s*\[([\s\S]*?)\]/
    );

    assert.ok(systemAdminConfigLine, "system-admin realtime screen config must exist");
    assert.doesNotMatch(systemAdminConfigLine, /customRefresh:\s*true/);
    assert.ok(customRefreshPathsBlock, "customRefreshPaths config must exist");
    assert.doesNotMatch(customRefreshPathsBlock[1], /\^\/system-admin\//);

    const runtime = createRuntime({
        currentHref: "http://admin.localhost/system-admin/",
        idleDelayMs: -1,
        screens: [{
            name: "system-admin",
            role: "system_admin",
            mode: "observer",
            path: "^/system-admin/",
        }],
        fetchResponses: [
            response(200, {version: 8, role_active: true}),
        ],
    });
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.reloads.length, 0, "pending mobile work must defer a full reload");
    runtime.localStorage.removeItem(MOBILE_QUEUE_KEY);
    runtime.runTimeoutsUpTo(2000);
    await settlePromises();

    assert.equal(runtime.reloads.length, 1);
    assert.equal(runtime.sessionStorage.getItem("operational-state-version"), "8");
    assert.equal(runtime.refreshSkippedEvents.length, 0);
    assert.equal(runtime.window.AppRealtime.getDebugState().authEnded, false);
});

test("offline state is recoverable and does not terminate authentication", async () => {
    const runtime = createRuntime({
        online: false,
        fetchResponses: [
            response(200, {version: 7, role_active: true}),
        ],
    });
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 0);
    assert.equal(runtime.window.AppRealtime.getDebugState().consecutiveFailures, 1);
    assert.notEqual(runtime.window.AppRealtime.getDebugState().authEnded, true);
    assert.deepEqual(runtime.readonlyCalls, []);
    assert.deepEqual(runtime.activeRoleEvents, []);
    assert.deepEqual(readQueue(runtime), [{id: "pending-action"}]);
    assert.equal(runtime.redirects.length, 0);

    runtime.navigator.onLine = true;
    runtime.window.AppRealtime.poll({force: true});
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 1, "polling must resume when connectivity returns");
    assert.equal(runtime.redirects.length, 0);
});

test("work and observer screens use 5s and 15s polling instead of the global second", async () => {
    assert.doesNotMatch(BASE_TEMPLATE_SOURCE, /pollIntervalMs:\s*1000/);
    assert.match(BASE_TEMPLATE_SOURCE, /workPollIntervalMs:\s*5000/);
    assert.match(BASE_TEMPLATE_SOURCE, /observerPollIntervalMs:\s*15000/);

    const worker = createRuntime({
        fetch() {
            return Promise.resolve(response(200, {version: 7, role_active: true, relevant: false}));
        },
    });
    const observer = createRuntime({
        currentHref: "http://admin.localhost/system-admin/",
        screens: [{
            name: "system-admin",
            role: "system_admin",
            mode: "observer",
            path: "^/system-admin/",
        }],
        fetch() {
            return Promise.resolve(response(200, {version: 7, role_active: true, relevant: false}));
        },
    });
    const manager = createRuntime({
        currentHref: "http://management.localhost/reports/management/dynamics/",
        screens: [{
            name: "management-dynamics",
            role: "management",
            mode: "custom",
            path: "^/reports/management/dynamics/?$",
            customRefresh: true,
        }],
        fetch() {
            return Promise.resolve(response(200, {version: 7, role_active: true, relevant: false}));
        },
    });
    await settlePromises();

    assert.equal(worker.window.AppRealtime.getDebugState().pollIntervalMs, 5000);
    assert.equal(observer.window.AppRealtime.getDebugState().pollIntervalMs, 15000);
    assert.equal(manager.window.AppRealtime.getDebugState().pollIntervalMs, 15000);
    assert.equal(worker.intervalDelays().includes(1000), false);
    assert.equal(observer.intervalDelays().includes(1000), false);
});

test("Mining Master runs twelve visible hours with only the shared five-second realtime timer", async () => {
    const twelveHoursInFiveSecondTicks = (12 * 60 * 60 * 1000) / 5000;
    const runtime = createRuntime({
        currentHref: "http://mining-master.localhost/mining-master/assignments/",
        idleDelayMs: -1,
        screens: [{
            name: "mining-master",
            role: "mining_master",
            mode: "custom",
            path: "^/mining-master/assignments/?$",
            customRefresh: true,
        }],
        fetch() {
            return Promise.resolve(response(200, {
                version: 7,
                role_active: true,
                relevant: false,
                events: [],
                events_truncated: false,
            }));
        },
        applyOperationalStateRefresh() {
            assert.fail("A stable version must not request an operational fragment.");
        },
    });
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 1, "the visible screen performs one initial state GET");
    assert.equal(
        runtime.timeoutDelays().includes(10000),
        false,
        "the runtime must not schedule a second ten-second state timer"
    );

    for (let tick = 0; tick < twelveHoursInFiveSecondTicks; tick += 1) {
        assert.equal(
            runtime.runOneTimeoutByDelay(5000),
            true,
            `shared realtime tick ${tick + 1} must stay scheduled`
        );
        await settlePromises();
    }

    assert.equal(
        runtime.fetchCalls.length,
        twelveHoursInFiveSecondTicks + 1,
        "twelve hours must contain only the initial GET and shared five-second realtime GETs"
    );
    assert.equal(runtime.timeoutDelays().includes(10000), false);
});

test("Mining Master loads one JSON fragment for a relevant event and none for an irrelevant event", async () => {
    const fragmentRefreshes = [];
    const runtime = createRuntime({
        currentHref: "http://mining-master.localhost/mining-master/assignments/",
        idleDelayMs: -1,
        screens: [{
            name: "mining-master",
            role: "mining_master",
            mode: "custom",
            path: "^/mining-master/assignments/?$",
            customRefresh: true,
        }],
        fetchResponses: [
            response(200, {
                version: 8,
                role_active: true,
                relevant: false,
                events: [],
                events_truncated: false,
            }),
            response(200, {
                version: 9,
                role_active: true,
                relevant: true,
                events: [{
                    version: 9,
                    type: "assignment_changed",
                    payload: {excavator_id: 20, truck_id: 10},
                }],
                events_truncated: false,
            }),
        ],
        applyOperationalStateRefresh(context) {
            fragmentRefreshes.push(context);
            return Promise.resolve({applied: true});
        },
    });
    await settlePromises();

    assert.equal(
        fragmentRefreshes.length,
        0,
        "a server-filtered irrelevant delta must not invoke the JSON fragment handler"
    );

    runtime.window.AppRealtime.poll({force: true});
    await settlePromises();

    assert.equal(fragmentRefreshes.length, 1);
    assert.equal(fragmentRefreshes[0].version, 9);
    assert.equal(fragmentRefreshes[0].events.length, 1);
    assert.equal(fragmentRefreshes[0].events[0].type, "assignment_changed");
    assert.equal(runtime.reloads.length, 0, "a relevant event is applied without a full HTML reload");
});

test("Mining Master coalesces focus, visibility and online catch-up into one state request", async () => {
    const runtime = createRuntime({
        currentHref: "http://mining-master.localhost/mining-master/assignments/",
        screens: [{
            name: "mining-master",
            role: "mining_master",
            mode: "custom",
            path: "^/mining-master/assignments/?$",
            customRefresh: true,
        }],
        fetch() {
            return Promise.resolve(response(200, {
                version: 7,
                role_active: true,
                relevant: false,
                events: [],
                events_truncated: false,
            }));
        },
    });
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 1);

    runtime.window.dispatchEvent({type: "focus"});
    runtime.document.dispatchEvent({type: "visibilitychange"});
    runtime.window.dispatchEvent({type: "online"});

    assert.equal(
        runtime.fetchCalls.length,
        1,
        "the burst must stay queued until the shared zero-delay wake runs"
    );
    runtime.flushZeroTimers();
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 2, "the three recovery signals must coalesce into one GET");
    runtime.flushZeroTimers();
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 2, "the wake must not leave a polling storm behind");
});

test("hidden tab performs no initial or timer-driven realtime requests", async () => {
    const runtime = createRuntime({
        hidden: true,
        fetch() {
            return Promise.resolve(response(200, {version: 7, role_active: true, relevant: false}));
        },
    });
    await settlePromises();

    runtime.runIntervals();
    runtime.runAllTimeouts();
    await settlePromises();

    assert.equal(runtime.fetchCalls.length, 0);
});

test("blur pauses polling and focus performs one immediate catch-up request", async () => {
    const runtime = createRuntime({
        fetch() {
            return Promise.resolve(response(200, {version: 7, role_active: true, relevant: false}));
        },
    });
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 1);

    runtime.window.dispatchEvent({type: "blur"});
    runtime.runIntervals();
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 1);

    runtime.window.dispatchEvent({type: "focus"});
    runtime.flushZeroTimers();
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 2);
});

test("native startup reveal wakes a rendered WebView before DOM focus is reported", async () => {
    const runtime = createRuntime({
        focused: false,
        fetch() {
            return Promise.resolve(response(200, {
                version: 7,
                role_active: true,
                relevant: false,
            }));
        },
    });
    await settlePromises();

    assert.equal(
        runtime.fetchCalls.length,
        0,
        "a WebView covered by the native startup layer must not poll before it is revealed"
    );
    assert.equal(runtime.window.AppRealtime.getDebugState().pageActive, false);

    runtime.window.dispatchEvent({type: "native-connectivity-resume"});
    runtime.flushZeroTimers();
    await settlePromises();

    assert.equal(
        runtime.fetchCalls.length,
        1,
        "the native reveal signal must trigger exactly one immediate catch-up"
    );
    assert.equal(runtime.window.AppRealtime.getDebugState().pageActive, true);
});

test("forced catch-up ignores the aborted older response without clearing the new poll", async () => {
    const resolvers = [];
    const runtime = createRuntime({
        fetch() {
            return new Promise((resolve) => {
                resolvers.push(resolve);
            });
        },
    });
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 1);

    runtime.window.AppRealtime.poll({force: true});
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 2);

    resolvers[0](response(200, {
        version: 99,
        role_active: true,
        relevant: false,
    }));
    await settlePromises();
    assert.equal(runtime.window.AppRealtime.getDebugState().pollInFlight, true);
    assert.equal(runtime.window.AppRealtime.getDebugState().currentVersion, 7);

    runtime.window.AppRealtime.poll();
    assert.equal(runtime.fetchCalls.length, 2, "the new in-flight request must stay protected");

    resolvers[1](response(200, {
        version: 8,
        role_active: true,
        relevant: false,
    }));
    await settlePromises();
    assert.equal(runtime.window.AppRealtime.getDebugState().pollInFlight, false);
    assert.equal(runtime.window.AppRealtime.getDebugState().currentVersion, 8);
});

test("active to hidden aborts the current poll and visible resumes exactly once", async () => {
    const resolvers = [];
    const runtime = createRuntime({
        fetch() {
            return new Promise((resolve) => {
                resolvers.push(resolve);
            });
        },
    });
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 1);

    runtime.document.hidden = true;
    runtime.document.dispatchEvent({type: "visibilitychange"});
    runtime.runAllTimeouts();
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.window.AppRealtime.getDebugState().pageActive, false);

    runtime.document.hidden = false;
    runtime.document.dispatchEvent({type: "visibilitychange"});
    runtime.flushZeroTimers();
    await settlePromises();
    assert.equal(runtime.fetchCalls.length, 2);

    resolvers[0](response(200, {
        version: 99,
        role_active: true,
        relevant: false,
    }));
    resolvers[1](response(200, {
        version: 8,
        role_active: true,
        relevant: false,
    }));
    await settlePromises();
    assert.equal(runtime.window.AppRealtime.getDebugState().currentVersion, 8);
});
