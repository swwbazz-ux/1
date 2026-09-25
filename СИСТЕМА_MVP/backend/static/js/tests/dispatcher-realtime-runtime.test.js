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

function classList() {
    const values = new Set();
    return {
        add(value) { values.add(value); },
        remove(value) { values.delete(value); },
        contains(value) { return values.has(value); },
    };
}

function boardNode() {
    return {
        dataset: {},
        classList: classList(),
        outerHTML: '<section class="dispatcher-board"></section>',
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

function createHarness({payload = null} = {}) {
    const currentBoard = boardNode();
    const freshBoard = boardNode();
    freshBoard.dataset.dispatcherShiftOpen = "true";
    const storage = new Map();
    const calls = {
        request: [],
        syncShift: 0,
        bind: 0,
        integrity: 0,
        indicator: 0,
    };
    const body = {
        dataset: {operationalStateVersion: "10"},
        classList: classList(),
    };
    const document = {
        body,
        activeElement: null,
        querySelector(selector) {
            if (selector === ".dispatcher-board") return currentBoard;
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
                return Promise.resolve(payload || {html: "<section></section>"});
            },
            parseRoot() { return freshBoard; },
        },
    };
    vm.runInNewContext(SOURCE, {window, console}, {filename: "dispatcher-realtime-v1.js"});
    const runtime = window.createDispatcherRealtime({
        transport: {
            getQueueState() { return {isFlushing: false, pendingCount: 0}; },
            readQueue() { return []; },
            scheduleFlush() {},
        },
        syncShiftRuntime() { calls.syncShift += 1; },
        getEquipmentCards() { return {}; },
        setEquipmentCards() {},
        getDetailLayer() { return null; },
        openEquipmentCard() {},
        bindBoardInteractions() { calls.bind += 1; },
        refreshBoardIntegrity() { calls.integrity += 1; },
        updateSyncIndicator() { calls.indicator += 1; },
    });
    return {runtime, window, body, currentBoard, freshBoard, storage, calls};
}

test("irrelevant realtime version is stored without fetching a dispatcher fragment", async () => {
    const harness = createHarness();

    const result = await harness.runtime.applyOperationalStateRefresh({version: 11, events: []});

    assert.equal(result.applied, true);
    assert.equal(harness.calls.request.length, 0);
    assert.equal(harness.body.dataset.operationalStateVersion, "11");
    assert.equal(harness.storage.get("operational-state-version"), "11");
});

test("truncated realtime history uses full-board fallback and restores runtime hooks", async () => {
    const harness = createHarness({payload: {html: "<section></section>", equipment_cards: {7: {id: 7}}}});

    const result = await harness.runtime.applyOperationalStateRefresh({
        version: 200,
        events: [{type: "assignment_changed"}],
        eventsTruncated: true,
    });

    assert.equal(result.applied, true);
    assert.deepEqual(harness.calls.request, [{screen: "dispatcher", version: 200}]);
    assert.equal(harness.currentBoard.replacedWith, harness.freshBoard);
    assert.equal(harness.calls.syncShift, 1);
    assert.equal(harness.calls.bind, 1);
    assert.equal(harness.calls.integrity, 1);
    assert.equal(harness.calls.indicator, 1);
    assert.equal(harness.body.dataset.operationalStateVersion, "200");
});
