"use strict";

const assert = require("node:assert/strict");
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
    const template = fs.readFileSync(driverTemplatePath, "utf8");
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


test("production Driver unload recovery keeps one action id across lost response, reload, BFCache and realtime shell replacement", () => {
    const {template, runtimeWindow} = loadDriverUnloadRecoveryRuntime();
    const storage = new MemoryStorage();
    let generated = 0;
    let recovered = 0;
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
        2,
        "Both production unload submission paths must prepare the retained ID."
    );
});


function extractExcavatorTruckLoadedHandler() {
    const template = fs.readFileSync(excavatorTemplatePath, "utf8");
    const startMarker = "    function postTruckLoaded(card, dumpTarget) {";
    const endMarker = "    function clearDropReady() {";
    const start = template.indexOf(startMarker);
    const end = template.indexOf(endMarker, start);
    assert.notEqual(start, -1, "Production postTruckLoaded handler was not found.");
    assert.notEqual(end, -1, "Production postTruckLoaded boundary was not found.");
    return template.slice(start, end);
}


function flushPromiseChain() {
    return new Promise((resolve) => setImmediate(resolve));
}


test("production Excavator load handler retries a lost successful response with the exact same action id", async () => {
    const requests = [];
    const serverActions = new Set();
    let serverTripCount = 0;
    let restoredCards = 0;
    let confirmedCards = 0;
    let removedBadges = 0;
    const card = {
        dataset: {
            truckId: "7",
            eoCanLoad: "1",
        },
    };
    const dumpTarget = {dataset: {eoDumpTarget: "9"}};
    const inputValues = new Map([
        ["select[name='rock_type']", "4"],
        ["input[name='loading_horizon']", "125"],
        ["input[name='loading_block']", "6"],
        ["input[name='planned_volume_m3']", "25"],
        ["input[name='transport_distance_km']", "3.5"],
        ["input[name='note']", ""],
    ]);
    const shell = {
        dataset: {eoCurrentExcavatorId: "3"},
        querySelector(selector) {
            return inputValues.has(selector) ? {value: inputValues.get(selector)} : null;
        },
    };
    const runtimeWindow = {
        alert() {},
        AppRealtime: {wake() {}},
    };
    let generated = 0;

    function fakeFetch(_url, options) {
        const payload = JSON.parse(options.body);
        requests.push(payload);
        if (!serverActions.has(payload.client_action_id)) {
            serverActions.add(payload.client_action_id);
            serverTripCount += 1;
        }
        if (requests.length === 1) {
            return Promise.reject(new Error("HTTP response was lost after commit"));
        }
        return Promise.resolve({
            ok: true,
            json() {
                return Promise.resolve({
                    ok: true,
                    deduplicated: true,
                    trip_id: 501,
                    dump_point: "ККД",
                    status_label: "на разгрузку",
                });
            },
        });
    }

    const executable = {};
    vm.runInNewContext(
        `
        (function () {
            var shell = context.shell;
            var truckLoadedUrl = "/excavator/truck-loaded/";
            var csrfToken = "csrf";
            var downtimeInput = {value: ""};
            function canTruckLoad() { return true; }
            function truckLoadBlockReason() { return ""; }
            function snapshotTruckCard() { return {snapshot: true}; }
            function markLastDumpTarget() {}
            function applyTruckPending() {}
            function addPendingTruckBadge() {
                return {remove: function () { context.removedBadges(); }};
            }
            function pendingBadgeList() { return []; }
            function ensureMutedPendingBadge() {}
            function restoreTruckCard() { context.restoredCards(); }
            function confirmTruckLoaded() { context.confirmedCards(); }
            function updatePendingTruckBadgeTrip() {}
            function applyActiveDowntime() {}
            function clearActiveDowntime() {}
            function playExcavatorSound() { return Promise.resolve(true); }
            function showExcavatorNotice() {}
            function generateClientActionId() {
                return context.generateClientActionId();
            }
            ${extractExcavatorTruckLoadedHandler()}
            context.executable.handler = postTruckLoaded;
        })();
        `,
        {
            window: runtimeWindow,
            fetch: fakeFetch,
            context: {
                executable,
                shell,
                generateClientActionId() {
                    generated += 1;
                    return "truck-loaded-stable-action";
                },
                restoredCards() {
                    restoredCards += 1;
                },
                confirmedCards() {
                    confirmedCards += 1;
                },
                removedBadges() {
                    removedBadges += 1;
                },
            },
        }
    );

    executable.handler(card, dumpTarget);
    await flushPromiseChain();
    assert.equal(requests.length, 1);
    assert.equal(card.dataset.eoLoadActionId, "truck-loaded-stable-action");
    assert.equal(restoredCards, 1);

    executable.handler(card, dumpTarget);
    await flushPromiseChain();
    assert.equal(requests.length, 2);
    assert.equal(requests[0].client_action_id, requests[1].client_action_id);
    assert.equal(serverTripCount, 1);
    assert.equal(serverActions.size, 1);
    assert.equal(generated, 1);
    assert.equal(confirmedCards, 1);
    assert.equal(removedBadges, 1);
    assert.equal(card.dataset.eoLoadActionId, undefined);
    assert.equal(card.dataset.eoLoadActionKey, undefined);
});


test("production Excavator late deduplicated load rolls back optimistic state and refreshes from server", async () => {
    const requests = [];
    const wakeReasons = [];
    const refreshRequests = [];
    let restoredCards = 0;
    let confirmedCards = 0;
    let removedBadges = 0;
    let updatedBadges = 0;
    let alerts = 0;
    const card = {
        dataset: {
            truckId: "7",
            eoCanLoad: "1",
            eoEquipmentState: "assigned",
        },
    };
    const dumpTarget = {dataset: {eoDumpTarget: "9"}};
    const inputValues = new Map([
        ["select[name='rock_type']", "4"],
        ["input[name='loading_horizon']", "125"],
        ["input[name='loading_block']", "6"],
        ["input[name='planned_volume_m3']", "25"],
        ["input[name='transport_distance_km']", "3.5"],
        ["input[name='note']", ""],
    ]);
    const shell = {
        dataset: {eoCurrentExcavatorId: "3"},
        querySelector(selector) {
            return inputValues.has(selector) ? {value: inputValues.get(selector)} : null;
        },
    };
    const runtimeWindow = {
        alert() {
            alerts += 1;
        },
        AppRealtime: {
            wake(reason) {
                wakeReasons.push(reason);
            },
        },
    };

    function fakeFetch(_url, options) {
        requests.push(JSON.parse(options.body));
        return Promise.resolve({
            ok: true,
            json() {
                return Promise.resolve({
                    ok: true,
                    deduplicated: true,
                    trip_id: 501,
                    status: "completed",
                    status_label: "Выполнен",
                    refresh_required: true,
                });
            },
        });
    }

    const executable = {};
    vm.runInNewContext(
        `
        (function () {
            var shell = context.shell;
            var truckLoadedUrl = "/excavator/truck-loaded/";
            var csrfToken = "csrf";
            var downtimeInput = {value: ""};
            function canTruckLoad() { return true; }
            function truckLoadBlockReason() { return ""; }
            function snapshotTruckCard(card) {
                return {equipmentState: card.dataset.eoEquipmentState};
            }
            function markLastDumpTarget() {}
            function applyTruckPending(card) {
                card.dataset.eoEquipmentState = "loaded_waiting_unload";
            }
            function addPendingTruckBadge() {
                return {remove: function () { context.removedBadges(); }};
            }
            function pendingBadgeList() { return []; }
            function ensureMutedPendingBadge() {}
            function restoreTruckCard(card, snapshot) {
                card.dataset.eoEquipmentState = snapshot.equipmentState;
                context.restoredCards();
            }
            function confirmTruckLoaded(card) {
                card.dataset.eoEquipmentState = "loaded_waiting_unload";
                context.confirmedCards();
            }
            function updatePendingTruckBadgeTrip() {
                context.updatedBadges();
            }
            function applyActiveDowntime() {}
            function clearActiveDowntime() {}
            function playExcavatorSound() { return Promise.resolve(true); }
            function showExcavatorNotice() {}
            function generateClientActionId() {
                return "truck-loaded-terminal-action";
            }
            function refreshExcavatorWorkFromServer(options) {
                context.refreshRequests(options);
                return Promise.resolve(true);
            }
            ${extractExcavatorTruckLoadedHandler()}
            context.executable.handler = postTruckLoaded;
        })();
        `,
        {
            window: runtimeWindow,
            fetch: fakeFetch,
            context: {
                executable,
                shell,
                restoredCards() {
                    restoredCards += 1;
                },
                confirmedCards() {
                    confirmedCards += 1;
                },
                removedBadges() {
                    removedBadges += 1;
                },
                updatedBadges() {
                    updatedBadges += 1;
                },
                refreshRequests(options) {
                    refreshRequests.push(options);
                },
            },
        }
    );

    executable.handler(card, dumpTarget);
    await flushPromiseChain();
    await flushPromiseChain();

    assert.equal(requests.length, 1);
    assert.equal(card.dataset.eoEquipmentState, "assigned");
    assert.equal(restoredCards, 1);
    assert.equal(confirmedCards, 0);
    assert.equal(removedBadges, 1);
    assert.equal(updatedBadges, 0);
    assert.equal(alerts, 0);
    assert.deepEqual(wakeReasons, []);
    assert.equal(refreshRequests.length, 1);
    assert.equal(refreshRequests[0].preserveTab, true);
    assert.equal(card.dataset.eoLoadActionId, undefined);
    assert.equal(card.dataset.eoLoadActionKey, undefined);
});
