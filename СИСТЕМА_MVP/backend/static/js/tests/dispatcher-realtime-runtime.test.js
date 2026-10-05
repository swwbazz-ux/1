"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-realtime-v1.js"),
    "utf8"
);
const RECONCILER_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-fragment-reconciler-v1.js"),
    "utf8"
);

function classList() {
    const values = new Set();
    return {
        add(value) { values.add(value); },
        remove(value) { values.delete(value); },
        contains(value) { return values.has(value); },
    };
}

function boardNode(marker = "") {
    return {
        dataset: {testBoard: marker},
        classList: classList(),
        outerHTML: '<section class="dispatcher-board" data-test-board="' + marker + '"></section>',
        attributes: [],
        replacedWith: null,
        getClientRects() { return [{}]; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        replaceWith(node) { this.replacedWith = node; },
        hasAttribute() { return false; },
        removeAttribute() {},
        setAttribute() {},
    };
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return {promise, resolve, reject};
}

function createHarness({payload = null, unsafeDrag = false, deferredRequests = false} = {}) {
    const currentBoard = boardNode();
    const freshBoard = boardNode();
    const oldBoard = boardNode("old");
    const newBoard = boardNode("new");
    const newerBoard = boardNode("newer");
    freshBoard.dataset.dispatcherShiftOpen = "true";
    let activeBoard = currentBoard;
    const pendingRequests = [];
    const storage = new Map();
    const calls = {
        request: [],
        syncShift: 0,
        reset: 0,
        bind: 0,
        integrity: 0,
        indicator: 0,
        lifecycle: [],
    };
    [currentBoard, freshBoard, oldBoard, newBoard, newerBoard].forEach((board) => {
        board.replaceWith = function (node) {
            calls.lifecycle.push("replace");
            this.replacedWith = node;
            activeBoard = node;
        };
    });
    const body = {
        dataset: {operationalStateVersion: "10"},
        classList: classList(),
    };
    const document = {
        body,
        activeElement: null,
        querySelector(selector) {
            if (selector === ".dispatcher-board") return activeBoard;
            if (unsafeDrag && selector.includes(".dispatcher-dragging")) return {};
            return null;
        },
    };
    const window = {
        document,
        navigator: {onLine: true},
        sessionStorage: {
            getItem(key) { return storage.get(key) || null; },
            setItem(key, value) { storage.set(key, value); },
        },
        getComputedStyle() { return {display: "block", visibility: "visible"}; },
        scrollX: 0,
        scrollY: 0,
        scrollTo() {},
        AppOperationalFragment: {
            request(screen, version) {
                calls.request.push({screen, version});
                if (deferredRequests) {
                    const request = deferred();
                    pendingRequests.push(request);
                    return request.promise;
                }
                return Promise.resolve(payload || {html: "<section></section>", version: 10});
            },
            parseRoot(html) {
                if (html === "old") return oldBoard;
                if (html === "new") return newBoard;
                if (html === "newer") return newerBoard;
                return freshBoard;
            },
        },
    };
    const context = {window, console};
    vm.runInNewContext(RECONCILER_SOURCE, context, {filename: "dispatcher-fragment-reconciler-v1.js"});
    vm.runInNewContext(SOURCE, context, {filename: "dispatcher-realtime-v1.js"});
    const runtime = window.createDispatcherRealtime({
        transport: {
            boardRefreshToken() { return "unchanged"; },
            getQueueState() { return {isFlushing: false, pendingCount: 0}; },
            readQueue() { return []; },
            scheduleFlush() {},
        },
        syncShiftRuntime() {
            calls.syncShift += 1;
            calls.lifecycle.push("sync");
        },
        getEquipmentCards() { return {}; },
        setEquipmentCards() {},
        getDetailLayer() { return null; },
        openEquipmentCard() {},
        resetBoardDragSession() {
            calls.reset += 1;
            calls.lifecycle.push("reset");
        },
        bindBoardInteractions() {
            calls.bind += 1;
            calls.lifecycle.push("bind");
        },
        refreshBoardIntegrity() { calls.integrity += 1; },
        updateSyncIndicator() { calls.indicator += 1; },
    });
    return {
        runtime,
        window,
        body,
        currentBoard,
        freshBoard,
        oldBoard,
        newBoard,
        newerBoard,
        activeBoard: () => activeBoard,
        resolveRequest(index, nextPayload) {
            pendingRequests[index].resolve(nextPayload);
        },
        storage,
        calls,
    };
}

test("irrelevant realtime version is stored without fetching a dispatcher fragment", async () => {
    const harness = createHarness();

    const result = await harness.runtime.applyOperationalStateRefresh({version: 11, events: []});

    assert.equal(result.applied, true);
    assert.equal(harness.calls.request.length, 0);
    assert.equal(harness.body.dataset.operationalStateVersion, "11");
    assert.equal(harness.storage.get("operational-state-version"), "11");
});

test("активный drag откладывает fragment и не сбрасывает текущую сессию", async () => {
    const harness = createHarness({unsafeDrag: true});

    const result = await harness.runtime.applyOperationalStateRefresh({
        version: 11,
        events: [{type: "assignment_changed"}],
    });

    assert.equal(result.deferred, true);
    assert.equal(result.reason, "dispatcher_busy");
    assert.equal(harness.calls.request.length, 0);
    assert.equal(harness.calls.reset, 0);
    assert.equal(harness.calls.bind, 0);
});

test("assignment_changed always refreshes the authoritative dispatcher board", async () => {
    const harness = createHarness({
        payload: {html: "<section></section>", version: 11, equipment_cards: {}},
    });

    assert.equal(typeof harness.runtime.markLocalAssignmentApplied, "undefined");
    const result = await harness.runtime.applyOperationalStateRefresh({
        version: 11,
        events: [{
            type: "assignment_changed",
            payload: {truck_ids: ["other-truck"], target_excavator_id: "other-excavator"},
        }],
    });

    assert.equal(result.applied, true);
    assert.deepEqual(harness.calls.request, [{screen: "dispatcher", version: 11}]);
    assert.equal(harness.currentBoard.replacedWith, harness.freshBoard);
    assert.equal(harness.body.dataset.operationalStateVersion, "11");
});

test("truncated realtime history uses full-board fallback and restores runtime hooks", async () => {
    const harness = createHarness({payload: {html: "<section></section>", version: 200, equipment_cards: {7: {id: 7}}}});

    const result = await harness.runtime.applyOperationalStateRefresh({
        version: 200,
        events: [{type: "assignment_changed"}],
        eventsTruncated: true,
    });

    assert.equal(result.applied, true);
    assert.deepEqual(harness.calls.request, [{screen: "dispatcher", version: 200}]);
    assert.equal(harness.currentBoard.replacedWith, harness.freshBoard);
    assert.equal(harness.calls.syncShift, 1);
    assert.equal(harness.calls.reset, 1);
    assert.equal(harness.calls.bind, 1);
    assert.deepEqual(harness.calls.lifecycle.slice(0, 4), ["sync", "replace", "reset", "bind"]);
    assert.equal(harness.calls.integrity, 1);
    assert.equal(harness.calls.indicator, 1);
    assert.equal(harness.body.dataset.operationalStateVersion, "200");
});

test("late stale fragment cannot overwrite a newer dispatcher board", async () => {
    const harness = createHarness({deferredRequests: true});

    const recovery = harness.runtime.refreshBoardFromServer({forceFullBoard: true});
    const realtime = harness.runtime.applyOperationalStateRefresh({
        version: 102,
        events: [{type: "assignment_changed"}],
        eventsTruncated: true,
    });

    assert.deepEqual(harness.calls.request, [
        {screen: "dispatcher", version: 0},
        {screen: "dispatcher", version: 102},
    ]);

    harness.resolveRequest(1, {html: "new", version: 104, equipment_cards: {}});
    const realtimeResult = await realtime;
    assert.equal(realtimeResult.applied, true);
    assert.equal(realtimeResult.version, 104);
    assert.equal(harness.activeBoard(), harness.newBoard);
    assert.equal(harness.body.dataset.operationalStateVersion, "104");
    assert.equal(harness.storage.get("operational-state-version"), "104");
    assert.equal(harness.calls.syncShift, 1);
    assert.equal(harness.calls.reset, 1);
    assert.equal(harness.calls.bind, 1);
    assert.equal(harness.calls.integrity, 1);
    assert.equal(harness.calls.indicator, 1);

    harness.resolveRequest(0, {html: "old", version: 101, equipment_cards: {}});
    assert.equal(await recovery, true);
    assert.equal(harness.activeBoard(), harness.newBoard);
    assert.equal(harness.calls.syncShift, 1);
    assert.equal(harness.calls.reset, 1);
    assert.equal(harness.calls.bind, 1);
    assert.equal(harness.calls.integrity, 1);
    assert.equal(harness.calls.indicator, 1);
});

test("a higher fragment version wins even when its request started earlier", async () => {
    const harness = createHarness({deferredRequests: true});

    const recovery = harness.runtime.refreshBoardFromServer({forceFullBoard: true});
    const realtime = harness.runtime.applyOperationalStateRefresh({
        version: 102,
        events: [{type: "assignment_changed"}],
        eventsTruncated: true,
    });

    harness.resolveRequest(1, {html: "new", version: 102, equipment_cards: {}});
    const realtimeResult = await realtime;
    assert.equal(realtimeResult.applied, true);
    assert.equal(realtimeResult.version, 102);
    assert.equal(harness.activeBoard(), harness.newBoard);

    harness.resolveRequest(0, {html: "newer", version: 104, equipment_cards: {}});
    assert.equal(await recovery, true);
    assert.equal(harness.activeBoard(), harness.newerBoard);
    assert.equal(harness.body.dataset.operationalStateVersion, "104");
    assert.equal(harness.storage.get("operational-state-version"), "104");
    assert.equal(harness.calls.syncShift, 2);
    assert.equal(harness.calls.reset, 2);
    assert.equal(harness.calls.bind, 2);
});

test("obsolete fragment is acknowledged without touching a board that already covers it", async () => {
    const harness = createHarness({payload: {html: "old", version: 9, equipment_cards: {}}});

    assert.equal(await harness.runtime.refreshBoardFromServer({forceFullBoard: true}), true);
    assert.equal(harness.activeBoard(), harness.currentBoard);
    assert.equal(harness.body.dataset.operationalStateVersion, "10");
    assert.equal(harness.calls.syncShift, 0);
    assert.equal(harness.calls.reset, 0);
    assert.equal(harness.calls.bind, 0);
});
