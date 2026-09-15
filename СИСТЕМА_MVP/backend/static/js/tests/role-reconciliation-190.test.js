"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const templates = process.env.ROLE_RECONCILIATION_TEMPLATE_ROOT || path.resolve(__dirname, "../../../templates");
const driver = fs.readFileSync(path.join(templates, "users/driver_shift.html"), "utf8");
const excavator = fs.readFileSync(path.join(templates, "trips/excavator_work.html"), "utf8");
// The existing brace extractor understands comments and quoted braces; use it
// to execute the production functions, never a test copy of their algorithm.
const helperSource = fs.readFileSync(path.join(__dirname, "operational-fragment-runtime.test.js"), "utf8");
const helperStart = helperSource.indexOf("function extractBraceBlock(");
const helperEnd = helperSource.indexOf("\nfunction createExcavatorRefreshRuntime", helperStart);
const helperContext = {assert};
vm.runInNewContext(helperSource.slice(helperStart, helperEnd), helperContext);
const extract = (source, signature) => helperContext.extractBraceBlock(source, signature, signature);

function roleRuntime(role) {
    const driverRole = role === "driver";
    const source = driverRole ? driver : excavator;
    const selector = driverRole ? "[data-driver-shell]" : "[data-eo-shell]";
    let current;
    let requestCount = 0;
    let replacements = 0;
    let restored = 0;
    let version = 101;
    let badRoot = false;
    let bindFailure = false;
    let modal = false;
    let dirty = false;
    let now = 0;
    const pending = [];
    const requestedVersions = [];
    const stored = [];
    const marks = [], proofs = [];
    function shell(loaded = false) {
        return {
            dataset: {activeTab: "manifest", eoActiveTab: "events", driverHasLoadedTrip: String(loaded), driverActiveTripId: loaded ? "810" : "", eoHasPendingFieldEvents: "false"},
            contains: () => false,
            querySelector(q) {
                if (driverRole && q === "[data-driver-shift-close-form]" && dirty) return {dataset: {driverShiftDirty: "true"}};
                if (driverRole && q.includes(".is-touch-armed") && modal) return {};
                if (!driverRole && q === '[data-eo-screen="face"]' && dirty) return {dataset: {eoFaceDirty: "true"}};
                return null;
            },
            querySelectorAll: () => [],
            replaceWith(next) { replacements++; current = next; },
        };
    }
    current = shell();
    const document = {
        hidden: false,
        activeElement: null,
        body: {dataset: {operationalStateVersion: "100"}},
        querySelector(q) {
            if (q === selector) return current;
            if (!driverRole && q.includes(".eo-truck-card.is-pending") && modal) return {};
            return null;
        },
        getElementById: () => null,
        querySelectorAll: () => [],
    };
    const window = {
        sessionStorage: {setItem: (key, value) => stored.push([key, value])},
        AppRealtime: {markApplied: value => marks.push(value), reportTransportSuccess: proof => proofs.push(proof)},
        AppOperationalFragment: {
            request(_role, target) {
                assert.equal(_role, role);
                requestCount++;
                requestedVersions.push(target);
                return new Promise((resolve, reject) => pending.push({resolve, reject}));
            },
            parseRoot: () => badRoot ? null : shell(true),
            captureView: () => ({scroll: ++now}),
            restoreView(_shell, saved) {assert.ok(saved.scroll); restored++;},
        },
        bindDriverMobileShell() {if (bindFailure) throw new Error("DOM init failed");},
        bindMobileShiftScreens() {if (bindFailure) throw new Error("DOM init failed");},
        initExcavatorWorkShell() {},
    };
    const context = {
        window, document, Promise, Array,
        syncDriverTabMarkup: (root, tab) => {root.dataset.activeTab = tab;},
        playDriverDumpPointAlert() {}, playDriverAssignmentAlert() {}, playDriverReleaseOfferCue() {},
        readExcavatorAssignmentSnapshot: () => ({}), syncExcavatorAssignmentSnapshot() {},
        scheduleExcavatorViewportHeightSync() {},
    };
    const functions = driverRole ? [
        extract(source, "function isDriverOperationalRefreshUnsafe(shell)"),
        extract(source, "window.applyOperationalStateRefresh = function (context)"),
    ] : [
        "var excavatorWorkMutationGeneration = 0; var excavatorWorkRefreshRequestGeneration = 0; var excavatorWorkAppliedRequestGeneration = 0;",
        extract(source, "function isExcavatorRefreshUnsafe(options)"),
        extract(source, "function storeExcavatorRealtimeVersion(version)"),
        extract(source, "function refreshExcavatorWorkFromServer(options)"),
        extract(source, "window.applyOperationalStateRefresh = function (context)"),
    ];
    vm.runInNewContext(functions.join(";\n"), context);
    return {
        apply: (target = 101) => window.applyOperationalStateRefresh({version: target, events: [], foregroundReconcile: true}),
        resolve(index = pending.length - 1) {pending[index].resolve({version, html: "<main></main>"});},
        reject(index = pending.length - 1) {const error = new Error("first fragment interrupted"); error.name = "AbortError"; pending[index].reject(error);},
        setVersion(value) {version = value;}, setBadRoot(value) {badRoot = value;}, setBindFailure(value) {bindFailure = value;},
        setModal(value) {modal = value;}, setDirty(value) {dirty = value;},
        setHidden(value) {document.hidden = value;},
        replaceExternally() {current = shell();},
        setTab(value) {current.dataset[driverRole ? "activeTab" : "eoActiveTab"] = value;},
        tab: () => current.dataset[driverRole ? "activeTab" : "eoActiveTab"],
        loaded: () => current.dataset.driverHasLoadedTrip === "true",
        requestCount: () => requestCount, replacements: () => replacements, restored: () => restored,
        applied: () => Number(document.body.dataset.operationalStateVersion),
        requestedVersions, marks, stored, proofs,
    };
}

for (const role of ["driver", "excavator"]) {
    test(`${role}: interrupted first fragment retains applied version and next truth restores loaded state`, async () => {
        const r = roleRuntime(role);
        const first = r.apply(); r.reject();
        assert.equal((await first).deferred, true);
        assert.equal(r.applied(), 100);
        assert.equal(r.replacements(), 0);
        const second = r.apply(); r.resolve();
        assert.equal((await second).applied, true);
        assert.equal(r.applied(), 101);
        assert.equal(r.loaded(), true);
        assert.equal(r.replacements(), 1);
    });
    test(`${role}: concurrent triggers share one fragment owner`, async () => {
        const r = roleRuntime(role);
        const first = r.apply(), second = r.apply();
        assert.equal(r.requestCount(), 1);
        r.resolve();
        assert.equal((await first).applied, true);
        assert.equal((await second).applied, true);
        assert.equal(r.replacements(), 1);
    });
    test(`${role}: wrong selector never acknowledges pending version`, async () => {
        const r = roleRuntime(role); r.setBadRoot(true);
        const apply = r.apply(); r.resolve();
        assert.equal((await apply).deferred, true);
        assert.equal(r.applied(), 100);
    });
    test(`${role}: initialization failure after replacement never acknowledges version`, async () => {
        const r = roleRuntime(role); r.setBindFailure(true);
        const apply = r.apply(); r.resolve();
        assert.equal((await apply).deferred, true);
        assert.equal(r.applied(), 100);
        assert.equal(r.replacements(), 1);
        r.setBindFailure(false);
        const retry = r.apply(); r.resolve();
        assert.equal((await retry).applied, true);
        assert.equal(r.applied(), 101);
    });
    test(`${role}: hidden page, open modal and dirty form defer without GET or replacement`, async () => {
        const r = roleRuntime(role);
        r.setHidden(true); assert.equal((await r.apply()).deferred, true);
        r.setHidden(false); r.setModal(true); assert.equal((await r.apply()).deferred, true);
        r.setModal(false); r.setDirty(true); assert.equal((await r.apply()).deferred, true);
        assert.equal(r.requestCount(), 0);
        r.setDirty(false); const apply = r.apply(); r.resolve();
        assert.equal((await apply).applied, true);
        assert.equal(r.requestCount(), 1);
    });
    test(`${role}: background entered during GET defers and leaves version pending`, async () => {
        const r = roleRuntime(role); const apply = r.apply();
        r.setHidden(true); r.resolve();
        assert.equal((await apply).deferred, true);
        assert.equal(r.applied(), 100);
        assert.equal(r.replacements(), 0);
    });
    test(`${role}: tab selected while GET runs and scroll snapshot survive replacement`, async () => {
        const r = roleRuntime(role); const apply = r.apply();
        r.setTab("shift"); r.resolve(); await apply;
        assert.equal(r.tab(), "shift"); assert.equal(r.restored(), 1);
    });
    test(`${role}: empty event delta still reconciles server truth and acknowledges actual payload version`, async () => {
        const r = roleRuntime(role); r.setVersion(105);
        const apply = r.apply(); r.resolve();
        assert.equal((await apply).version, 105);
        assert.equal(r.requestCount(), 1);
    });
}

test("driver: stale GET cannot overwrite a shell replaced by successful POST", async () => {
    const r = roleRuntime("driver"); const apply = r.apply();
    r.replaceExternally(); r.resolve();
    assert.equal((await apply).deferred, true);
    assert.equal(r.applied(), 100);
    assert.equal(r.replacements(), 0);
});

test("driver: same-owner newer target never claims the earlier fragment reflects that target", async () => {
    const r = roleRuntime("driver");
    const first = r.apply(101), later = r.apply(103);
    r.resolve();
    assert.equal((await first).version, 101);
    assert.equal((await later).version, 101);
    assert.equal(r.requestCount(), 1);
});

test("excavator POST acknowledgement maps server IDs but leaves DOM applied version unchanged", async () => {
    const badge = {dataset: {eoOfflineEventId: "event", eoLocalTripId: "local"}};
    const card = {dataset: {eoOfflineEventId: "event", eoLocalOpenTripId: "local"}};
    const requested = [];
    const document = {body: {dataset: {operationalStateVersion: "100"}}, querySelector: () => badge};
    const window = {AppRealtime: {requestReconcile: (...args) => requested.push(args)}, sessionStorage: {setItem() {}}, setTimeout() {}, clearTimeout() {}};
    const context = {window, document, Promise, freeBucketController: null, cardByTruckId: () => card, downtimeCard: null};
    vm.runInNewContext(extract(excavator, "function storeExcavatorRealtimeVersion(version)") + ";\n" + extract(excavator, "window.confirmExcavatorFieldEvent = function (event, result)"), context);
    await window.confirmExcavatorFieldEvent({event_type: "excavator.trip.loaded", event_id: "event", payload: {truck_id: 8}}, {server_version: 101, server_ids: {trip_id: 810}});
    assert.equal(document.body.dataset.operationalStateVersion, "100");
    assert.equal(badge.dataset.tripId, "810");
    assert.equal(card.dataset.eoOpenTripId, "810");
    assert.equal(badge.dataset.eoLocalTripId, undefined);
    assert.deepEqual(requested, [["offline_excavator_events_confirmed", 101]]);
});

test("driver outbox acknowledgement uses only common reconciliation owner", () => {
    const calls = [];
    const window = {AppRealtime: {requestReconcile: (...args) => calls.push(args)}};
    const context = {window, driverOfflineContext() {}, renderDriverOfflineState() {}, playDriverVoice() {}, showDriverToast() {}};
    vm.runInNewContext(extract(driver, "function driverOfflineBindings()") + "\nvar bindings = driverOfflineBindings();", context);
    context.bindings.onConfirmed({event_type: "driver.downtime.ended"}, {server_version: 109});
    assert.deepEqual(calls, [["driver_offline_event_confirmed", 109]]);
});

for (const role of ["driver", "excavator"]) {
    test(`${role}: outbox count feeds common machine and empty queue never asserts transport success`, () => {
        const label = {textContent: ""}, count = {setAttribute() {}};
        const indicator = {querySelector: q => q.endsWith("label]") ? label : count};
        const current = {
            dataset: {},
            querySelector: q => q === ".driver-online" ? {setAttribute() {assert.fail("Queue renderer must not own connection status");}} : q.endsWith("label]") ? label : count,
        };
        const events = [], requests = [];
        const window = {
            dispatchEvent: event => events.push(event.detail),
            AppRealtime: {requestReconcile: reason => requests.push(reason)},
        };
        const context = {
            window,
            document: {querySelector: q => q === "[data-eo-offline-sync]" ? indicator : current},
            navigator: {onLine: true},
            CustomEvent: function (_type, options) {this.detail = options.detail;},
            driverOfflineEvents: [], applyDriverOfflineProjection() {},
        };
        const source = role === "driver" ? driver : excavator;
        const fn = role === "driver" ? "renderDriverOfflineState" : "renderExcavatorOfflineStatus";
        vm.runInNewContext(extract(source, `function ${fn}(`), context);
        context[fn]({pending: 2, total: 2, events: []});
        assert.equal(window.operationalOutboxPendingCount, 2);
        assert.equal(events[0].pendingCount, 2);
        context[fn]({pending: 0, total: 0, events: []});
        assert.equal(window.operationalOutboxPendingCount, 0);
        assert.equal(events[1].pendingCount, 0);
        assert.equal(label.textContent, "Все действия отправлены");
        assert.equal(requests.length, 1);
    });
}


test("excavator direct fragment reports successful transport only after genuine DOM initialization", async () => {
    const r = roleRuntime("excavator"); r.setBindFailure(true);
    const failed = r.apply(); r.resolve(); await failed;
    assert.equal(r.proofs.length, 0);
    r.setBindFailure(false); const applied = r.apply(); r.resolve(); await applied;
    assert.equal(r.proofs.length, 1);
    assert.equal(r.proofs[0].channel, "fragment");
    assert.equal(r.proofs[0].serverVersion, 101);
});
