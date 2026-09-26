"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const LAYOUT_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-board-layout-v1.js"),
    "utf8"
);

class ClassListStub {
    constructor(classNames = []) {
        this.values = new Set(classNames);
    }

    add(...classNames) {
        classNames.forEach((className) => this.values.add(className));
    }

    remove(...classNames) {
        classNames.forEach((className) => this.values.delete(className));
    }

    contains(className) {
        return this.values.has(className);
    }

    toggle(className, force) {
        const enabled = force === undefined ? !this.values.has(className) : Boolean(force);
        if (enabled) this.values.add(className);
        else this.values.delete(className);
        return enabled;
    }
}

class StyleStub {
    constructor() {
        this.values = new Map();
    }

    setProperty(name, value) {
        this.values.set(name, value);
    }

    getPropertyValue(name) {
        return this.values.get(name) || "";
    }
}

function dataKey(attributeName) {
    return String(attributeName).replace(/^data-/, "").replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

function nodeMatches(node, selector) {
    if (!node) return false;
    if (selector.includes(":not(.is-assigned)") && node.classList.contains("is-assigned")) return false;
    if (selector.includes(":not(.is-placeholder)") && node.classList.contains("is-placeholder")) return false;
    const plainSelector = selector.replace(/:not\([^)]*\)/g, "");
    if (plainSelector.startsWith(".")) {
        const classes = plainSelector.split(".").filter(Boolean);
        return classes.every((className) => node.classList.contains(className));
    }
    const attributes = [...plainSelector.matchAll(/\[data-([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'))?\]/g)];
    if (!attributes.length) return false;
    return attributes.every((match) => {
        const expected = match[2] ?? match[3];
        const value = node.dataset[dataKey(match[1])];
        return expected === undefined ? value !== undefined : value === expected;
    });
}

class ElementStub {
    constructor({dataset = {}, classNames = [], textContent = ""} = {}) {
        this.dataset = {...dataset};
        this.classList = new ClassListStub(classNames);
        this.style = new StyleStub();
        this.children = [];
        this.parentNode = null;
        this.removed = false;
        this.textContent = textContent;
        this.innerHTML = "";
    }

    get className() {
        return [...this.classList.values].join(" ");
    }

    set className(value) {
        this.classList = new ClassListStub(String(value || "").split(/\s+/).filter(Boolean));
    }

    setAttribute(name, value) {
        if (name.startsWith("data-")) this.dataset[dataKey(name)] = String(value);
    }

    appendChild(node) {
        if (node.parentNode) node.parentNode.removeChild(node);
        node.parentNode = this;
        this.children.push(node);
        return node;
    }

    insertBefore(node, referenceNode) {
        if (node.parentNode) node.parentNode.removeChild(node);
        node.parentNode = this;
        const index = this.children.indexOf(referenceNode);
        if (index === -1) this.children.push(node);
        else this.children.splice(index, 0, node);
        return node;
    }

    removeChild(node) {
        const index = this.children.indexOf(node);
        if (index !== -1) this.children.splice(index, 1);
        node.parentNode = null;
    }

    remove() {
        if (this.parentNode) this.parentNode.removeChild(this);
        this.removed = true;
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    querySelectorAll(selector) {
        const result = [];
        const visit = (node) => {
            node.children.forEach((child) => {
                if (!child.removed && nodeMatches(child, selector)) result.push(child);
                visit(child);
            });
        };
        visit(this);
        return result;
    }
}

class DocumentStub {
    constructor() {
        this.activeRoot = null;
    }

    setActiveRoot(node) {
        this.activeRoot = node;
    }

    createElement() {
        return new ElementStub();
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    querySelectorAll(selector) {
        if (!this.activeRoot) return [];
        const result = [];
        const visit = (node) => {
            if (!node.removed && nodeMatches(node, selector)) result.push(node);
            node.children.forEach(visit);
        };
        visit(this.activeRoot);
        return result;
    }
}

function makeTruck(number, options = {}) {
    return new ElementStub({
        dataset: {
            garageItem: "truck",
            equipmentSort: String(number),
            equipmentName: options.name || "Самосвал " + number,
        },
        classNames: ["dispatcher-truck-tile"].concat(options.assigned ? ["is-assigned"] : []),
        textContent: options.name || "Самосвал " + number,
    });
}

function makeBoardFixture() {
    const board = new ElementStub({classNames: ["dispatcher-board"]});
    const truckGarage = new ElementStub({classNames: ["dispatcher-trucks"]});
    const excavatorGarage = new ElementStub({dataset: {dispatcherExcavatorGarage: ""}});
    const excavatorList = new ElementStub({classNames: ["dispatcher-excavators"]});
    const grid = new ElementStub({classNames: ["dispatcher-zone-grid"]});
    excavatorGarage.appendChild(excavatorList);
    board.appendChild(truckGarage);
    board.appendChild(excavatorGarage);
    board.appendChild(grid);
    return {board, truckGarage, excavatorGarage, excavatorList, grid};
}

function createHarness() {
    const document = new DocumentStub();
    const context = {window: {}, document};
    vm.runInNewContext(LAYOUT_SOURCE, context, {filename: "dispatcher-board-layout-v1.js"});
    const layout = context.window.createDispatcherBoardLayout({document});
    return {
        document,
        layout,
        activate(fixture) {
            document.setActiveRoot(fixture.board);
        },
    };
}

test("сортировка сохраняет плитки и ставит технику перед пустыми ячейками", () => {
    const {layout} = createHarness();
    const rack = new ElementStub();
    const truckTen = makeTruck(10, {name: "Яма"});
    const truckTwoBeta = makeTruck(2, {name: "Бета"});
    const truckTwoAlpha = makeTruck(2, {name: "Альфа"});
    const placeholder = new ElementStub({classNames: ["dispatcher-truck-tile", "is-placeholder"]});
    const empty = new ElementStub({classNames: ["complex-truck-empty"]});
    [truckTen, placeholder, truckTwoBeta, empty, truckTwoAlpha].forEach((node) => rack.appendChild(node));

    layout.sortEquipmentList(rack, ".dispatcher-truck-tile:not(.is-placeholder)");

    assert.deepEqual(rack.children, [truckTwoAlpha, truckTwoBeta, truckTen, placeholder, empty]);
    assert.equal(rack.children[0], truckTwoAlpha);
    assert.equal(rack.children[2], truckTen);
});

test("гараж самосвалов считает только свободную технику и создаёт нужные пустые места", () => {
    const harness = createHarness();
    const fixture = makeBoardFixture();
    harness.activate(fixture);
    const freeTrucks = Array.from({length: 13}, (_, index) => makeTruck(index + 1));
    const assignedTruck = makeTruck(99, {assigned: true});
    const oldPlaceholder = new ElementStub({classNames: ["dispatcher-truck-tile", "is-placeholder"]});
    [...freeTrucks, assignedTruck, oldPlaceholder].forEach((node) => fixture.truckGarage.appendChild(node));

    harness.layout.refreshTruckGarage();

    assert.equal(oldPlaceholder.removed, true);
    assert.equal(fixture.board.style.getPropertyValue("--truck-garage-columns"), "2");
    assert.equal(fixture.board.style.getPropertyValue("--truck-garage-scrollbar-w"), "0px");
    assert.equal(fixture.board.classList.contains("is-truck-garage-empty"), false);
    assert.equal(fixture.truckGarage.querySelectorAll(".dispatcher-truck-tile.is-placeholder").length, 11);

    Array.from({length: 24}, (_, index) => makeTruck(index + 101)).forEach((truck) => fixture.truckGarage.appendChild(truck));
    harness.layout.refreshTruckGarage();
    assert.equal(fixture.board.style.getPropertyValue("--truck-garage-columns"), "3");
    assert.equal(fixture.board.style.getPropertyValue("--truck-garage-scrollbar-w"), "14px");

    fixture.truckGarage.querySelectorAll("[data-garage-item='truck']:not(.is-assigned)").forEach((truck) => truck.remove());
    harness.layout.refreshTruckGarage();
    assert.equal(fixture.board.classList.contains("is-truck-garage-empty"), true);
});

test("гараж экскаваторов сортирует свободные плитки и не считает назначенные", () => {
    const harness = createHarness();
    const fixture = makeBoardFixture();
    harness.activate(fixture);
    const assigned = new ElementStub({
        dataset: {garageItem: "excavator", excavatorSlot: "1"},
        classNames: ["dispatcher-excavator-garage-tile", "is-assigned"],
    });
    const fifth = new ElementStub({
        dataset: {garageItem: "excavator", excavatorSlot: "5"},
        classNames: ["dispatcher-excavator-garage-tile"],
    });
    const second = new ElementStub({
        dataset: {garageItem: "excavator", excavatorSlot: "2"},
        classNames: ["dispatcher-excavator-garage-tile"],
    });
    [assigned, fifth, second].forEach((node) => fixture.excavatorList.appendChild(node));

    harness.layout.refreshExcavatorGarage();

    assert.deepEqual(fixture.excavatorList.children, [assigned, second, fifth]);
    assert.equal(fixture.board.classList.contains("is-excavator-garage-empty"), false);
    second.remove();
    fifth.remove();
    harness.layout.refreshExcavatorGarage();
    assert.equal(fixture.board.classList.contains("is-excavator-garage-empty"), true);
});

test("сетка комплексов ставит риск выше нормы, а пустые карточки — в конец", () => {
    const harness = createHarness();
    const fixture = makeBoardFixture();
    harness.activate(fixture);
    const card = (slot, className) => new ElementStub({
        dataset: {excavatorSlot: String(slot)},
        classNames: ["dispatcher-complex-card", className],
    });
    const empty = card(1, "status-empty");
    const green = card(4, "status-green");
    const other = card(6, "status-gray");
    const blue = card(3, "status-blue");
    const yellow = card(2, "status-yellow");
    const orange = card(5, "status-orange");
    const red = card(7, "status-red");
    [empty, green, other, blue, yellow, orange, red].forEach((node) => fixture.grid.appendChild(node));

    harness.layout.normalizeComplexGrid();

    assert.deepEqual(fixture.grid.children, [red, orange, yellow, blue, green, other, empty]);
});

test("после fragment-замены layout работает с новой доской", () => {
    const harness = createHarness();
    const oldFixture = makeBoardFixture();
    oldFixture.truckGarage.appendChild(makeTruck(1));
    harness.activate(oldFixture);
    harness.layout.refreshTruckGarage();
    assert.equal(oldFixture.board.style.getPropertyValue("--truck-garage-columns"), "1");

    const newFixture = makeBoardFixture();
    Array.from({length: 13}, (_, index) => makeTruck(index + 1)).forEach((truck) => newFixture.truckGarage.appendChild(truck));
    harness.activate(newFixture);
    harness.layout.refreshTruckGarage();

    assert.equal(newFixture.board.style.getPropertyValue("--truck-garage-columns"), "2");
    assert.equal(oldFixture.board.style.getPropertyValue("--truck-garage-columns"), "1");
});
