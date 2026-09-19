"use strict";

const assert = require("node:assert/strict");
const {driverScreenSource} = require("./driver-screen-source");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const driverTemplatePath = path.resolve(
    __dirname,
    "..",
    "..",
    "..",
    "templates",
    "users",
    "driver_shift.html"
);
const excavatorTemplatePath = path.resolve(
    __dirname,
    "..",
    "..",
    "..",
    "templates",
    "trips",
    "excavator_work.html"
);


function extractMarkedSource(source, startMarker, endMarker, label) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert.notEqual(start, -1, `${label} start marker was not found.`);
    assert.notEqual(end, -1, `${label} end marker was not found.`);
    return source.slice(start + startMarker.length, end);
}


function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${label} signature was not found.`);
    const openBrace = source.indexOf("{", start + signature.length);
    assert.notEqual(openBrace, -1, `${label} opening brace was not found.`);

    let depth = 0;
    for (let index = openBrace; index < source.length; index += 1) {
        if (source[index] === "{") depth += 1;
        if (source[index] === "}") depth -= 1;
        if (depth === 0) {
            return source.slice(start, index + 1);
        }
    }
    assert.fail(`${label} closing brace was not found.`);
}


class MemoryStorage {
    constructor() {
        this.values = new Map();
    }

    get length() {
        return this.values.size;
    }

    key(index) {
        return Array.from(this.values.keys())[index] || null;
    }

    getItem(key) {
        return this.values.has(String(key)) ? this.values.get(String(key)) : null;
    }

    setItem(key, value) {
        this.values.set(String(key), String(value));
    }

    removeItem(key) {
        this.values.delete(String(key));
    }
}


class EventTargetStub {
    constructor() {
        this.listeners = new Map();
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    removeEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        this.listeners.set(
            type,
            listeners.filter((candidate) => candidate !== listener)
        );
    }

    dispatchEvent(event) {
        (this.listeners.get(event.type) || []).slice().forEach((listener) => {
            listener(event);
        });
    }
}


function loadDriverUnloadRecoveryRuntime() {
    const template = driverScreenSource();
    const source = extractMarkedSource(
        template,
        "/* DRIVER_UNLOAD_RECOVERY_START */",
        "/* DRIVER_UNLOAD_RECOVERY_END */",
        "Driver unload recovery"
    );
    const runtimeWindow = new EventTargetStub();
    vm.runInNewContext(source, {window: runtimeWindow});
    assert.equal(typeof runtimeWindow.createDriverUnloadRecovery, "function");
    return {template, runtimeWindow};
}


async function executeDriverSharedUnloadSubmit(template, recovery, pathLabel, submissions) {
    const submitSource = extractBraceBlock(
        template,
        "function submitDriverUnloadOnce()",
        "Driver shared unload submit"
    );
    const classNames = new Set(["is-loaded"]);
    const holdForm = {
        dataset: {driverTripId: "42"}, isConnected: true,
        querySelector() { return {value: recovery.ensureActionId()}; },
    };
    const holdButton = {
        classList: {
            add(...names) {
                names.forEach((name) => classNames.add(name));
            },
            remove(...names) {
                names.forEach((name) => classNames.delete(name));
            },
        },
        dataset: {},
        disabled: false,
    };
    const executable = {};

    vm.runInNewContext(
        `
        (function () {
            var unloadSubmissionPending = false;
            var unloadRecovery = context.recovery;
            var holdForm = context.holdForm;
            var holdButton = context.holdButton;
            var dialLabel = null;
            var shell = {dataset: {driverHasLoadedTrip: "true"}};
            var driverOfflineEvents = [];
            var driverOfflineOutbox = context.outbox;
            function driverOfflineContext() {return {shiftId: 3, equipmentId: 4};}
            function applyDriverOfflineProjection() {}
            function driverRoleIsReadonly() { return false; }
            function showDriverToast() {}
            function renderDriverDialLabel() {}
            function scheduleDriverDialLabelFit() {}
            ${submitSource}
            context.executable.submit = submitDriverUnloadOnce;
        })();
        `,
        {
            context: {
                executable,
                holdButton,
                holdForm,
                recovery,
                outbox: {
                    pending() {return Promise.resolve([]);},
                    enqueue(event) {
                        submissions.push({actionId: event.event_id, form: holdForm, path: pathLabel});
                        assert.equal(event.event_type, "driver.trip.unloaded");
                        assert.equal(event.trip_id, "42");
                        return Promise.resolve(event);
                    },
                    flush() {return Promise.resolve([]);},
                },
            },
            window: {
                submitDriverFormInPlace() {
                    assert.fail("Unload must enter the durable outbox before any send");
                },
            },
        },
        {filename: "templates/users/driver_shift.html#shared-unload-submit"}
    );

    assert.equal(executable.submit(), true);
    assert.equal(holdForm.dataset.driverUnloadSubmitting, "true");
    assert.equal(holdForm.dataset.holdComplete, "true");
    assert.equal(holdButton.disabled, true);
    assert.equal(classNames.has("is-pending"), true);
    await flushPromiseChain();
}


test("production Driver unload recovery keeps one action id across hold, one-tap, lost response, reload, BFCache and realtime shell replacement", async () => {
    const {template, runtimeWindow} = loadDriverUnloadRecoveryRuntime();
    const storage = new MemoryStorage();
    let generated = 0;
    let recovered = 0;
    const submissions = [];
    const generateActionId = () => {
        generated += 1;
        return "trip-unloaded-stable-action";
    };

    const firstInput = {value: ""};
    const firstRuntime = runtimeWindow.createDriverUnloadRecovery({
        storage,
        eventTarget: runtimeWindow,
        tripId: "42",
        input: firstInput,
        generateActionId,
    });
    const firstActionId = firstRuntime.ensureActionId();
    assert.equal(firstActionId, "trip-unloaded-stable-action");
    assert.equal(storage.getItem("driver-trip-unloaded:42"), firstActionId);

    await executeDriverSharedUnloadSubmit(
        template,
        firstRuntime,
        "one-tap-before-lost-response",
        submissions
    );

    // The server may have committed while the HTTP response was lost.
    // A reload creates a new input and a new production recovery instance.
    firstRuntime.destroy();
    const reloadInput = {value: ""};
    const reloadRuntime = runtimeWindow.createDriverUnloadRecovery({
        storage,
        eventTarget: runtimeWindow,
        tripId: "42",
        input: reloadInput,
        generateActionId,
        onRecover() {
            recovered += 1;
        },
    });
    assert.equal(reloadRuntime.ensureActionId(), firstActionId);
    assert.equal(reloadInput.value, firstActionId);
    assert.equal(generated, 1);

    runtimeWindow.dispatchEvent({type: "pageshow", persisted: true});
    assert.equal(recovered, 1);
    assert.equal(reloadRuntime.ensureActionId(), firstActionId);

    await executeDriverSharedUnloadSubmit(
        template,
        reloadRuntime,
        "hold-after-reload-and-bfcache",
        submissions
    );

    // Realtime replaces the Driver shell without reloading the JavaScript file.
    reloadRuntime.destroy();
    const realtimeInput = {value: ""};
    const realtimeRuntime = runtimeWindow.createDriverUnloadRecovery({
        storage,
        eventTarget: runtimeWindow,
        tripId: "42",
        input: realtimeInput,
        generateActionId,
    });
    assert.equal(realtimeRuntime.ensureActionId(), firstActionId);
    assert.equal(generated, 1);

    await executeDriverSharedUnloadSubmit(
        template,
        realtimeRuntime,
        "one-tap-after-realtime-rebind",
        submissions
    );
    assert.deepEqual(
        submissions.map(({actionId, path}) => ({actionId, path})),
        [
            {actionId: firstActionId, path: "one-tap-before-lost-response"},
            {actionId: firstActionId, path: "hold-after-reload-and-bfcache"},
            {actionId: firstActionId, path: "one-tap-after-realtime-rebind"},
        ]
    );
    assert.equal(generated, 1);

    // A server-confirmed shell without an open trip is authoritative and clears
    // every retained unload action from this tab.
    realtimeRuntime.destroy();
    runtimeWindow.createDriverUnloadRecovery({
        storage,
        eventTarget: runtimeWindow,
        tripId: "",
        input: null,
        generateActionId,
    });
    assert.equal(storage.length, 0);

    assert.match(template, /data-driver-trip-id="\{\{ active_trip\.id \}\}"/);
    assert.equal(
        (template.match(/unloadRecovery\.ensureActionId\(\)/g) || []).length,
        1,
        "The shared production submit must prepare the retained ID exactly once."
    );
    assert.match(
        template,
        /onComplete:\s*function\s*\(\)\s*\{[^}]*if\s*\(!submitDriverUnloadOnce\(\)\)/,
        "The normal hold completion must route through the shared unload submit."
    );
    assert.match(
        template,
        /onOneTap:\s*function\s*\(\)\s*\{\s*return submitDriverUnloadOnce\(\);\s*\}/,
        "The waiting-unload one-tap path must route through the same submit."
    );
});


function flushPromiseChain() {
    return new Promise((resolve) => setImmediate(resolve));
}

function createExcavatorLoadFixture({loseFirstResponse = false, completed = false} = {}) {
    const template = fs.readFileSync(excavatorTemplatePath, "utf8");
    const createOutbox = require("../excavator-field-outbox-v1.js");
    const local = new MemoryStorage();
    const serverActions = new Set(), requests = [], reconcile = [];
    let serverTripCount = 0, idCount = 0, fragmentCount = 0, failFragment = false;
    let pendingCount = 0, restoredCards = 0;
    const classNames = new Set();
    const card = {
        dataset: {truckId: "7", assignmentId: "8", eoCanLoad: "1", eoEquipmentState: "assigned"},
        className: "eo-truck-card", hidden: false,
        classList: {add: (...keys) => keys.forEach(key => classNames.add(key)), remove: (...keys) => keys.forEach(key => classNames.delete(key))},
        querySelector: () => null,
    };
    const badge = {dataset: {}};
    const dumpTarget = {dataset: {eoDumpTarget: "9", eoDumpName: "ККД"}, querySelector: () => null};
    const inputs = new Map([["select[name='rock_type']", "4"], ["input[name='loading_horizon']", "125"],
        ["input[name='loading_block']", "6"], ["input[name='planned_volume_m3']", "25"], ["input[name='note']", ""]]);
    function makeShell(terminal = false) {
        return {
            dataset: {eoCurrentExcavatorId: "3", eoAccessId: "7", eoEmployeeId: "17", nativeShiftId: "11", eoActiveTab: "trucks", eoHasPendingFieldEvents: "false"},
            terminal,
            contains: () => false,
            querySelector: selector => inputs.has(selector) ? {value: inputs.get(selector)} : null,
            querySelectorAll: () => [],
            replaceWith(next) {currentShell = next;},
        };
    }
    const initialShell = makeShell();
    let currentShell = initialShell;
    const document = {
        hidden: false, activeElement: null,
        body: {dataset: {operationalStateVersion: "100"}},
        querySelector(selector) {
            if (selector === "[data-eo-shell]") return currentShell;
            if (selector.includes("[data-eo-offline-event-id=")) return currentShell === initialShell ? badge : null;
            return null;
        },
        querySelectorAll: () => [], getElementById: () => null,
    };
    const window = {
        sessionStorage: new MemoryStorage(),
        AppRealtime: {requestReconcile: (reason, version) => reconcile.push({reason, version})},
        AppOperationalFragment: {
            request(_role, version) {
                fragmentCount++;
                if (failFragment) return Promise.reject(Object.assign(new Error("fragment interrupted"), {name: "AbortError"}));
                return Promise.resolve({version: Math.max(101, version), html: "<main data-eo-shell></main>"});
            },
            parseRoot: () => makeShell(completed),
        },
        initExcavatorWorkShell() {}, bindMobileShiftScreens() {},
    };
    const context = {
        window, document, navigator: {onLine: true}, Promise, shell: initialShell,
        freeBucketController: null, downtimeCard: null, downtimeInput: {value: ""}, transportDistanceInput: {value: "3.5"},
        canTruckLoad: () => true, truckLoadBlockReason: () => "",
        snapshotTruckCard: () => ({className: card.className}),
        applyTruckPending(node) {node.dataset.eoEquipmentState = "loaded_waiting_unload";},
        addPendingTruckBadge: () => badge,
        confirmTruckLoaded(node) {node.dataset.eoEquipmentState = "loaded_waiting_unload"; node.classList.remove("is-pending");},
        restoreTruckCard() {restoredCards++;},
        playExcavatorEquipmentVoice() {}, playExcavatorVoice() {}, playExcavatorSound() {},
        markLastDumpTarget() {}, showExcavatorNotice() {},
        newExcavatorFieldId: prefix => prefix + "-" + (++idCount), excavatorInstallDeviceId: () => "device-A",
        legacyExcavatorFieldSequence: () => 0,
        cardByTruckId: () => card,
        readExcavatorAssignmentSnapshot: () => ({}), syncExcavatorAssignmentSnapshot() {}, scheduleExcavatorViewportHeightSync() {},
    };
    function newOutbox() {
        return createOutbox({
            localStorage: local, queueKey: "exact-live-fixture",
            onChange(summary) {pendingCount = summary.pending; currentShell.dataset.eoHasPendingFieldEvents = pendingCount ? "true" : "false";},
            onConfirmed(event, result) {return window.confirmExcavatorFieldEvent(event, result);},
            send: async events => {
                requests.push(events);
                for (const event of events) {
                    if (!serverActions.has(event.event_id)) {serverActions.add(event.event_id); serverTripCount++;}
                }
                if (loseFirstResponse && requests.length === 1) throw new Error("HTTP response lost after server commit");
                return {results: events.map(event => ({event_id: event.event_id, status: "deduplicated",
                    server_version: 101, server_ids: {trip_id: 501}, trip_status: completed ? "completed" : "loaded_waiting_unload"}))};
            },
        });
    }
    const outbox = newOutbox();
    context.fieldOutbox = outbox;
    const declarations = [
        "var excavatorWorkMutationGeneration = 0, excavatorWorkRefreshRequestGeneration = 0, excavatorWorkAppliedRequestGeneration = 0;",
        ...[
            "function invalidateExcavatorWorkRefresh()", "function isExcavatorRefreshUnsafe(options)",
            "function storeExcavatorRealtimeVersion(version)", "function refreshExcavatorWorkFromServer(options)",
            "window.applyOperationalStateRefresh = function (context)",
            "window.confirmExcavatorFieldEvent = function (event, result)",
            "function postTruckLoaded(card, dumpTarget)",
        ].map(signature => extractBraceBlock(template, signature, signature)),
    ];
    vm.runInNewContext(declarations.join(";\n"), context);
    return {
        load: () => context.postTruckLoaded(card, dumpTarget), outbox, restartOutbox: newOutbox,
        refresh: () => window.applyOperationalStateRefresh({version: 101}),
        failRefresh(value) {failFragment = value;},
        requests, reconcile, card, badge, document,
        serverTripCount: () => serverTripCount, serverActionCount: () => serverActions.size,
        generated: () => idCount, restoredCards: () => restoredCards,
        fragmentCount: () => fragmentCount, terminal: () => currentShell.terminal,
    };
}

test("production Excavator load survives a lost response and a restarted durable outbox retries the identical event", async () => {
    const r = createExcavatorLoadFixture({loseFirstResponse: true});
    r.load(); await flushPromiseChain();
    assert.equal(r.requests.length, 1);
    assert.equal(r.serverTripCount(), 1);
    assert.equal((await r.outbox.pending()).length, 1);
    assert.equal(r.card.dataset.eoEquipmentState, "loaded_waiting_unload");
    assert.equal(r.restoredCards(), 0, "A committed durable load must not restore an actionable truck after a transport failure");
    const event = r.requests[0][0];
    assert.equal(event.event_type, "excavator.trip.loaded");
    assert.equal(event.payload.assignment_id, "8");
    assert.equal(event.payload.dump_point_id, "9");

    const restarted = r.restartOutbox();
    await restarted.ready(); await restarted.retryNow(); await flushPromiseChain();
    assert.equal(r.requests.length, 2);
    assert.deepEqual(r.requests[1][0], event, "Retry keeps ID, fact time, sequence, dependencies and immutable payload");
    assert.equal(r.serverTripCount(), 1); assert.equal(r.serverActionCount(), 1);
    assert.equal(r.generated(), 2, "One event ID and one local trip ID are allocated only once");
    assert.deepEqual(await restarted.pending(), []);
    assert.equal(r.badge.dataset.tripId, "501");
    assert.equal(r.card.dataset.eoOpenTripId, "501");
    assert.equal(r.reconcile.length, 1);
    assert.equal(r.reconcile[0].version, 101);
    assert.equal(r.document.body.dataset.operationalStateVersion, "100", "ACK is not DOM proof");
    assert.equal((await r.refresh()).applied, true);
    assert.equal(r.document.body.dataset.operationalStateVersion, "101");
});

test("production Excavator late deduplicated completed load reconciles from server and never revives the optimistic trip", async () => {
    const r = createExcavatorLoadFixture({completed: true});
    r.load(); await flushPromiseChain();
    assert.equal(r.requests.length, 1); assert.equal(r.serverTripCount(), 1);
    assert.deepEqual(await r.outbox.pending(), []);
    assert.equal(r.reconcile.length, 1);
    assert.equal(r.reconcile[0].reason, "offline_excavator_events_confirmed");
    r.failRefresh(true);
    assert.equal((await r.refresh()).deferred, true);
    assert.equal(r.document.body.dataset.operationalStateVersion, "100");
    r.failRefresh(false);
    assert.equal((await r.refresh()).applied, true);
    assert.equal(r.terminal(), true);
    assert.equal(r.document.body.dataset.operationalStateVersion, "101");
    assert.equal(r.fragmentCount(), 2);
    assert.equal(r.requests.length, 1, "Reconciliation never issues a second production load");
    assert.equal(r.serverTripCount(), 1);
});
