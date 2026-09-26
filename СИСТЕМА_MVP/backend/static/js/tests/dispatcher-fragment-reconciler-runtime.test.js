"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-fragment-reconciler-v1.js"),
    "utf8"
);

function createRuntime(options = {}) {
    const window = {
        document: options.document,
        scrollX: options.scrollX || 0,
        scrollY: options.scrollY || 0,
        scrollTo: options.scrollTo || function () {},
    };
    vm.runInNewContext(SOURCE, {window, console}, {filename: "dispatcher-fragment-reconciler-v1.js"});
    return window.createDispatcherFragmentReconciler({window, ...options});
}

test("fragment reconciler восстанавливает прокрутку доски и открытую detail-карточку", () => {
    const currentRegion = {scrollTop: 27, scrollLeft: 4};
    const freshRegion = {scrollTop: 0, scrollLeft: 0};
    const currentBoard = {
        querySelector(selector) {
            return selector === ".dispatcher-left" ? currentRegion : null;
        },
    };
    const freshBoard = {
        querySelector(selector) {
            return selector === ".dispatcher-left" ? freshRegion : null;
        },
    };
    const panel = {scrollTop: 63};
    const detailLayer = {
        hidden: false,
        dataset: {gdActiveCardId: "7"},
        querySelector(selector) {
            return selector === ".mm-equipment-detail-panel" ? panel : null;
        },
    };
    const scrollCalls = [];
    const openCalls = [];
    const runtime = createRuntime({
        document: {querySelector() { return null; }},
        scrollX: 11,
        scrollY: 19,
        scrollTo(x, y) { scrollCalls.push([x, y]); },
        getDetailLayer() { return detailLayer; },
        getEquipmentCards() { return {7: {id: 7}}; },
        openEquipmentCard(id) { openCalls.push(id); },
    });

    const state = runtime.captureState(currentBoard);
    panel.scrollTop = 0;
    runtime.restoreState(freshBoard, state);

    assert.equal(freshRegion.scrollTop, 27);
    assert.equal(freshRegion.scrollLeft, 4);
    assert.deepEqual(scrollCalls, [[11, 19]]);
    assert.deepEqual(openCalls, ["7"]);
    assert.equal(panel.scrollTop, 63);
});

test("неполный fragment не считается пригодным для точечной сверки", () => {
    const board = {
        querySelector() { return null; },
        querySelectorAll() { return []; },
    };
    const runtime = createRuntime({
        document: {querySelector() { return null; }},
    });

    assert.equal(runtime.reconcileBoard(board, board), null);
});
