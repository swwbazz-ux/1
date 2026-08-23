"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const RUNTIME_SOURCE = fs.readFileSync(
    path.resolve(
        __dirname,
        "..",
        "dispatcher-control-v1.js"
    ),
    "utf8"
);
const TEMPLATE_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "..", "templates", "trips", "dispatcher_control.html"),
    "utf8"
);


function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${label} signature was not found.`);
    const open = source.indexOf("{", start + signature.length);
    assert.notEqual(open, -1, `${label} opening brace was not found.`);

    let depth = 0;
    let quote = "";
    let escaped = false;
    let lineComment = false;
    let blockComment = false;

    for (let index = open; index < source.length; index += 1) {
        const character = source[index];
        const next = source[index + 1] || "";
        if (lineComment) {
            if (character === "\n") lineComment = false;
            continue;
        }
        if (blockComment) {
            if (character === "*" && next === "/") {
                blockComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (escaped) {
                escaped = false;
            } else if (character === "\\") {
                escaped = true;
            } else if (character === quote) {
                quote = "";
            }
            continue;
        }
        if (character === "/" && next === "/") {
            lineComment = true;
            index += 1;
            continue;
        }
        if (character === "/" && next === "*") {
            blockComment = true;
            index += 1;
            continue;
        }
        if (character === "'" || character === '"' || character === "`") {
            quote = character;
            continue;
        }
        if (character === "{") {
            depth += 1;
        } else if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`${label} closing brace was not found.`);
}


class ClassListStub {
    constructor(classNames = []) {
        this.values = new Set(classNames);
    }

    contains(className) {
        return this.values.has(className);
    }

    add(...classNames) {
        classNames.forEach((className) => this.values.add(className));
    }

    remove(...classNames) {
        classNames.forEach((className) => this.values.delete(className));
    }
}


class ElementStub {
    constructor({dataset = {}, classNames = []} = {}) {
        this.dataset = {...dataset};
        this.classList = new ClassListStub(classNames);
        this.listeners = new Map();
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    dispatch(type, event = {}) {
        event.type = type;
        event.defaultPrevented = false;
        event.preventDefault = () => {
            event.defaultPrevented = true;
        };
        event.stopPropagation = () => {};
        for (const listener of this.listeners.get(type) || []) {
            listener.call(this, event);
        }
        return event;
    }

    querySelector() {
        return null;
    }
}


function createRuntime(initialShiftOpen, freshShiftOpen) {
    const tile = new ElementStub({
        dataset: {
            dispatcherDrag: "truck",
            equipmentId: "12",
            equipmentName: "Самосвал 12",
        },
    });
    const zone = new ElementStub({
        dataset: {
            dispatcherDrop: "complex",
            equipmentId: "34",
        },
    });
    const board = new ElementStub();
    const freshBoard = new ElementStub({
        dataset: {
            dispatcherShiftOpen: freshShiftOpen ? "true" : "false",
        },
        classNames: freshShiftOpen ? [] : ["is-readonly"],
    });
    const documentStub = {
        body: {
            appendChild() {},
        },
        querySelector(selector) {
            if (selector === ".dispatcher-board") return board;
            if (selector === "[data-dispatcher-excavator-garage]") return null;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-dispatcher-drag]") return [tile];
            if (selector === "[data-equipment-card-id]") return [];
            if (selector === "[data-dispatcher-drop='complex']") return [zone];
            return [];
        },
    };
    const context = {
        console,
        document: documentStub,
        Promise,
        fetchCount: 0,
    };
    const syncSource = extractBraceBlock(
        RUNTIME_SOURCE,
        "function syncDispatcherShiftRuntime(freshBoard)",
        "Dispatcher shift runtime sync"
    );
    const bindDragSource = extractBraceBlock(
        RUNTIME_SOURCE,
        "function bindDragTile(tile)",
        "Dispatcher drag source bind"
    );
    const bindDropSource = extractBraceBlock(
        RUNTIME_SOURCE,
        "function bindDispatcherComplexDrop(zone)",
        "Dispatcher complex drop bind"
    );
    const bindAllSource = extractBraceBlock(
        RUNTIME_SOURCE,
        "function bindDispatcherDesktopInteractions()",
        "Dispatcher desktop interactions bind"
    );
    vm.runInNewContext(
        `
        var dispatcherShiftOpen = ${initialShiftOpen ? "true" : "false"};
        var draggedTile = null;
        var board = null;
        var excavatorGarage = null;
        var dragGhost = null;
        var dispatcherAssignTruckUrl = "/dispatcher/assign-truck/";
        function clearDragGhost() {}
        function bindEquipmentCardTrigger() {}
        function normalizeComplexGrid() {}
        function refreshExcavatorGarage() {}
        function refreshTruckGarage() {}
        function refreshAllComplexTruckRacks() {}
        function bindDispatcherExcavatorGarageDrop() {}
        function bindDispatcherTruckGarageDrop() {}
        function dispatcherPost() {
            fetchCount += 1;
            return Promise.resolve({});
        }
        function applyDesktopTruckAction(response) { return response; }
        function showDispatcherDnDError(error) { throw error; }
        ${syncSource}
        ${bindDragSource}
        ${bindDropSource}
        ${bindAllSource}
        `,
        context,
        {filename: "templates/trips/dispatcher_control.html#dispatcher-shift-runtime"}
    );
    context.syncDispatcherShiftRuntime(freshBoard);
    context.bindDispatcherDesktopInteractions();
    return {context, tile, zone};
}


function dragEvent() {
    return {
        dataTransfer: {
            effectAllowed: "",
            setData() {},
            setDragImage() {},
        },
    };
}


test("closed to open fragment enables dispatcher drag-and-drop without reload", async () => {
    const {context, tile, zone} = createRuntime(false, true);

    const dragStart = tile.dispatch("dragstart", dragEvent());
    zone.dispatch("drop");
    await Promise.resolve();

    assert.equal(context.dispatcherShiftOpen, true);
    assert.equal(dragStart.defaultPrevented, false);
    assert.equal(context.fetchCount, 1);
});


test("open to closed fragment blocks dispatcher drag-and-drop with fetch count zero", async () => {
    const {context, tile, zone} = createRuntime(true, false);

    const dragStart = tile.dispatch("dragstart", dragEvent());
    zone.dispatch("drop");
    await Promise.resolve();

    assert.equal(context.dispatcherShiftOpen, false);
    assert.equal(dragStart.defaultPrevented, true);
    assert.equal(context.fetchCount, 0);
});


test("fragment refresh synchronizes shift runtime before replacement and rebind", () => {
    const refreshSource = extractBraceBlock(
        RUNTIME_SOURCE,
        "function refreshDispatcherDesktopBoardFromServer(options)",
        "Dispatcher fragment refresh"
    );
    const parseIndex = refreshSource.indexOf("AppOperationalFragment.parseRoot");
    const syncIndex = refreshSource.indexOf("syncDispatcherShiftRuntime(freshBoard)");
    const reconcileIndex = refreshSource.indexOf("reconcileDispatcherDesktopBoard(currentBoard, freshBoard)");
    const fallbackIndex = refreshSource.indexOf("currentBoard.replaceWith(freshBoard)");
    const bindIndex = refreshSource.indexOf("bindDispatcherDesktopInteractions()");

    assert.ok(parseIndex >= 0);
    assert.ok(syncIndex > parseIndex);
    assert.ok(reconcileIndex > syncIndex);
    assert.ok(fallbackIndex > reconcileIndex, "whole-board replacement is fallback-only");
    assert.ok(bindIndex > fallbackIndex);
    assert.match(
        TEMPLATE_SOURCE,
        /data-dispatcher-shift-open="\{% if dispatcher_header\.active_shift %\}true/
    );
});

test("dispatcher fragment reconciliation is keyed by equipment and complex identity", () => {
    const reconcileSource = extractBraceBlock(
        RUNTIME_SOURCE,
        "function reconcileDispatcherDesktopBoard(currentBoard, freshBoard)",
        "Dispatcher keyed board reconciliation"
    );

    assert.match(reconcileSource, /\.dispatcher-excavators/);
    assert.match(reconcileSource, /\.dispatcher-zone-grid/);
    assert.match(reconcileSource, /\.dispatcher-trucks/);
    assert.match(reconcileSource, /key:\s*"equipmentId"/);
    assert.match(reconcileSource, /key:\s*"zoneId"/);
    assert.doesNotMatch(reconcileSource, /currentBoard\.replaceWith/);
});

test("one changed truck replaces only that keyed tile", () => {
    const helperNames = [
        "function dispatcherNodeMarkup(node)",
        "function syncDispatcherNodeAttributes(currentNode, freshNode)",
        "function dispatcherItemKey(node, keyName)",
        "function reconcileDispatcherKeyedRegion(currentBoard, freshBoard, definition)",
        "function reconcileDispatcherDesktopBoard(currentBoard, freshBoard)",
    ];
    const helpers = helperNames.map((signature) => (
        extractBraceBlock(RUNTIME_SOURCE, signature, signature)
    )).join("\n");
    function node(markup, dataset = {}) {
        return {
            outerHTML: markup,
            dataset: {...dataset},
            attributes: [],
            replacements: [],
            children: {},
            lists: {},
            hasAttribute() { return false; },
            removeAttribute() {},
            setAttribute() {},
            querySelector(selector) { return this.children[selector] || null; },
            querySelectorAll(selector) { return this.lists[selector] || []; },
            replaceWith(replacement) { this.replacements.push(replacement); },
        };
    }
    const definitions = [
        [".dispatcher-excavators", ".dispatcher-equipment-tile[data-equipment-id]", "equipmentId", "7"],
        [".dispatcher-zone-grid", ".dispatcher-complex-card[data-zone-id]", "zoneId", "2"],
        [".dispatcher-trucks", ".dispatcher-truck-tile[data-equipment-id]", "equipmentId", "15"],
    ];
    const currentBoard = node("board-old");
    const freshBoard = node("board-new");
    currentBoard.children[".dispatcher-topbar"] = node("topbar");
    freshBoard.children[".dispatcher-topbar"] = node("topbar");
    let changedTruck = null;
    definitions.forEach(([regionSelector, itemSelector, keyName, key]) => {
        const currentRegion = node(`${regionSelector}-old`);
        const freshRegion = node(`${regionSelector}-new`);
        const currentItem = node(
            regionSelector === ".dispatcher-trucks" ? "truck-old" : `${regionSelector}-same`,
            {[keyName]: key}
        );
        const freshItem = node(
            regionSelector === ".dispatcher-trucks" ? "truck-new" : `${regionSelector}-same`,
            {[keyName]: key}
        );
        currentRegion.lists[itemSelector] = [currentItem];
        freshRegion.lists[itemSelector] = [freshItem];
        currentBoard.children[regionSelector] = currentRegion;
        freshBoard.children[regionSelector] = freshRegion;
        if (regionSelector === ".dispatcher-trucks") changedTruck = currentItem;
    });
    const context = {Array};
    vm.runInNewContext(helpers, context);

    const result = context.reconcileDispatcherDesktopBoard(currentBoard, freshBoard);

    assert.equal(result, currentBoard);
    assert.equal(changedTruck.replacements.length, 1);
    assert.equal(currentBoard.replacements.length, 0);
    assert.equal(currentBoard.children[".dispatcher-topbar"].replacements.length, 0);
});
