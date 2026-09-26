"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const MUTATIONS_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-board-mutations-v1.js"),
    "utf8"
);
const DND_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-board-dnd-v1.js"),
    "utf8"
);
const BOARD_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-board-v1.js"),
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
    return attributeName.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}


function nodeMatches(node, selector) {
    if (!node) return false;
    if (selector.includes(":not(.is-assigned)") && node.classList.contains("is-assigned")) return false;
    if (selector.startsWith(".")) {
        const classes = selector.split(":")[0].split(".").filter(Boolean);
        return classes.every((className) => node.classList.contains(className));
    }
    const attributes = [...selector.matchAll(/\[data-([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'))?\]/g)];
    if (!attributes.length) return false;
    return attributes.every((match) => {
        const value = node.dataset[dataKey(match[1])];
        const expected = match[2] ?? match[3];
        return expected === undefined ? value !== undefined : value === expected;
    });
}


class ElementStub {
    constructor({dataset = {}, classNames = [], textContent = ""} = {}) {
        this.dataset = {...dataset};
        this.classList = new ClassListStub(classNames);
        this.style = new StyleStub();
        this.attributes = new Map();
        this.children = [];
        this.parentNode = null;
        this.removed = false;
        this.innerHTML = "";
        this.textContent = textContent;
        this.insertedHtml = [];
    }

    get className() {
        return [...this.classList.values].join(" ");
    }

    set className(value) {
        this.classList = new ClassListStub(String(value || "").split(/\s+/).filter(Boolean));
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
        if (name.startsWith("data-")) this.dataset[dataKey(name)] = String(value);
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    hasAttribute(name) {
        return this.attributes.has(name);
    }

    removeAttribute(name) {
        this.attributes.delete(name);
        if (name.startsWith("data-")) delete this.dataset[dataKey(name)];
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

    closest(selector) {
        let node = this;
        while (node) {
            if (nodeMatches(node, selector)) return node;
            node = node.parentNode;
        }
        return null;
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    querySelectorAll(selector) {
        const results = [];
        const visit = (node) => {
            node.children.forEach((child) => {
                if (nodeMatches(child, selector)) results.push(child);
                visit(child);
            });
        };
        visit(this);
        return results;
    }

    insertAdjacentHTML(position, html) {
        this.insertedHtml.push({position, html});
    }
}


class DocumentStub {
    constructor() {
        this.nodes = [];
        this.body = new ElementStub();
        this.body.classList = new ClassListStub(["mining-master-mobile-screen"]);
    }

    register(...nodes) {
        nodes.forEach((node) => {
            if (!this.nodes.includes(node)) this.nodes.push(node);
        });
    }

    createElement() {
        const node = new ElementStub();
        this.register(node);
        return node;
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    querySelectorAll(selector) {
        if (selector.startsWith('[data-dispatcher-drag="truck"]')) {
            const idMatch = selector.match(/data-equipment-id="([^"]+)"/);
            return this.nodes.filter((node) => !node.removed &&
                node.dataset.dispatcherDrag === "truck" &&
                node.dataset.equipmentId !== undefined &&
                (!idMatch || node.dataset.equipmentId === idMatch[1]));
        }
        return this.nodes.filter((node) => !node.removed && nodeMatches(node, selector));
    }
}


const PLAN_KEYS = [
    "planStatus", "planPercent", "planLoopPercent", "planCompletedLoops", "planProgressPhase",
    "planMode", "planValue", "planFact", "planUnit", "planGroup",
];


function makePlanDataset(extra = {}) {
    return {
        planStatus: "warning",
        planPercent: "43",
        planLoopPercent: "29",
        planCompletedLoops: "2",
        planProgressPhase: "load",
        planMode: "shift",
        planValue: "100",
        planFact: "43",
        planUnit: "т",
        planGroup: "смена",
        ...extra,
    };
}


function createHarness() {
    const documentStub = new DocumentStub();
    const calls = {
        drag: [],
        card: [],
        integrity: 0,
        truckGarage: 0,
        excavatorGarage: 0,
        racks: 0,
        rack: 0,
        grid: 0,
    };
    const context = {document: documentStub, window: {}};
    vm.runInNewContext(MUTATIONS_SOURCE, context, {
        filename: "dispatcher-board-mutations-v1.js",
    });
    const mutations = context.window.createDispatcherBoardMutations({
        document: documentStub,
        equipmentStateClass(state) {
            return "status-" + state;
        },
        equipmentStateLabel(state) {
            return "state:" + state;
        },
        neutralEquipmentIcon() {
            return "/neutral.png";
        },
        setNodeEquipmentState(node, state) {
            node.dataset.equipmentState = state;
        },
        bindDragTile(node) {
            calls.drag.push(node);
        },
        bindEquipmentCard(node) {
            calls.card.push(node);
        },
        refreshIntegrity() {
            calls.integrity += 1;
        },
        refreshTruckGarage() {
            calls.truckGarage += 1;
        },
        refreshExcavatorGarage() {
            calls.excavatorGarage += 1;
        },
        refreshComplexTruckRacks() {
            calls.racks += 1;
        },
        refreshComplexTruckRack() {
            calls.rack += 1;
        },
        normalizeComplexGrid() {
            calls.grid += 1;
        },
    });
    return {documentStub, calls, mutations};
}


test("перемещение самосвала сохраняет его план и токен, меняя только размещение", () => {
    const {documentStub, calls, mutations} = createHarness();
    const garage = new ElementStub({classNames: ["dispatcher-trucks"]});
    const placeholder = new ElementStub({classNames: ["dispatcher-truck-tile", "is-placeholder"]});
    const truck = new ElementStub({
        dataset: makePlanDataset({
            dispatcherDrag: "truck",
            garageItem: "truck",
            equipmentId: "17",
            haulAssignmentStateId: "99",
        }),
        classNames: ["dispatcher-equipment-tile", "is-plan-overrun"],
    });
    truck.style.setProperty("--tile-progress", "43%");
    truck.style.setProperty("--tile-total-progress", "80%");
    const complex = new ElementStub({
        dataset: {zoneId: "K-1"},
        classNames: ["dispatcher-complex-card"],
    });
    const rack = new ElementStub({classNames: ["complex-assigned-trucks"]});
    const empty = new ElementStub({classNames: ["complex-truck-empty"]});
    garage.appendChild(truck);
    garage.appendChild(placeholder);
    rack.appendChild(empty);
    complex.appendChild(rack);
    documentStub.register(garage, placeholder, truck, complex, rack, empty);

    assert.equal(mutations.moveTruckToComplex(truck, complex), true);
    assert.equal(rack.children.includes(truck), true);
    assert.equal(truck.dataset.complexTruck, "true");
    assert.equal(truck.dataset.assignedZone, "K-1");
    assert.equal(truck.dataset.garageItem, undefined);
    assert.equal(truck.dataset.haulAssignmentStateId, "99");
    PLAN_KEYS.forEach((key) => assert.equal(truck.dataset[key], makePlanDataset()[key]));
    assert.equal(truck.style.getPropertyValue("--tile-progress"), "43%");
    assert.equal(truck.style.getPropertyValue("--tile-total-progress"), "80%");
    assert.equal(truck.classList.contains("is-plan-overrun"), true);

    assert.equal(mutations.moveTruckToGarage(truck), true);
    assert.equal(garage.children[0], truck);
    assert.equal(truck.dataset.garageItem, "truck");
    assert.equal(truck.dataset.complexTruck, undefined);
    assert.equal(truck.dataset.assignedZone, undefined);
    assert.equal(truck.dataset.haulAssignmentStateId, "99");
    PLAN_KEYS.forEach((key) => assert.equal(truck.dataset[key], makePlanDataset()[key]));
    assert.equal(calls.drag.filter((node) => node === truck).length, 2);
    assert.equal(calls.card.filter((node) => node === truck).length, 2);
    assert.equal(calls.integrity, 2);
});


test("сверка дублей оставляет одну плитку самосвала", () => {
    const {documentStub, mutations} = createHarness();
    const kept = new ElementStub({dataset: {dispatcherDrag: "truck", equipmentId: "17"}});
    const duplicate = new ElementStub({dataset: {dispatcherDrag: "truck", equipmentId: "17"}});
    documentStub.register(kept, duplicate);

    mutations.reconcileTruckUniqueness();

    assert.equal(kept.removed, false);
    assert.equal(duplicate.removed, true);
});


test("расформирование очищает только плановую оболочку комплекса, а самосвал сохраняет токен", () => {
    const {documentStub, calls, mutations} = createHarness();
    const truckGarage = new ElementStub({classNames: ["dispatcher-trucks"]});
    const excavatorGarage = new ElementStub({classNames: ["dispatcher-excavators"]});
    const excavatorPlaceholder = new ElementStub({classNames: ["dispatcher-excavator-garage-tile", "is-placeholder"]});
    const complex = new ElementStub({
        dataset: makePlanDataset({
            dispatcherDrag: "complex",
            dispatcherDrop: "complex",
            zoneId: "K-3",
            zoneLabel: "K-3",
            equipmentId: "82",
            equipmentCardId: "82",
            equipmentName: "ЭКГ-5А",
            equipmentState: "assigned",
            excavatorSlot: "3",
            placementZone: "active",
        }),
        classNames: ["dispatcher-complex-card", "status-green", "is-plan-overrun"],
    });
    complex.style.setProperty("--complex-progress", "43%");
    complex.style.setProperty("--complex-total-progress", "80%");
    complex.setAttribute("role", "button");
    complex.setAttribute("draggable", "true");
    const rack = new ElementStub({classNames: ["complex-assigned-trucks"]});
    const truck = new ElementStub({
        dataset: makePlanDataset({
            dispatcherDrag: "truck",
            complexTruck: "true",
            equipmentId: "17",
            haulAssignmentStateId: "101",
        }),
        classNames: ["complex-truck-tile", "is-plan-overrun"],
    });
    rack.appendChild(truck);
    complex.appendChild(rack);
    excavatorGarage.appendChild(excavatorPlaceholder);
    documentStub.register(truckGarage, excavatorGarage, excavatorPlaceholder, complex, rack, truck);

    assert.equal(mutations.moveComplexToExcavatorGarage(complex), true);
    const garageTile = excavatorGarage.children[0];
    assert.equal(garageTile.dataset.equipmentId, "82");
    assert.equal(garageTile.classList.contains("is-sync-pending"), true);
    PLAN_KEYS.forEach((key) => assert.equal(garageTile.dataset[key], undefined));
    assert.equal(truckGarage.children.includes(truck), true);
    assert.equal(truck.dataset.haulAssignmentStateId, "101");
    PLAN_KEYS.forEach((key) => assert.equal(truck.dataset[key], makePlanDataset()[key]));
    assert.equal(complex.classList.contains("status-empty"), true);
    assert.equal(complex.dataset.dispatcherDrop, "complex");
    assert.equal(complex.dataset.zoneId, "K-3");
    assert.equal(complex.dataset.placementZone, undefined);
    PLAN_KEYS.forEach((key) => assert.equal(complex.dataset[key], undefined));
    assert.equal(complex.style.getPropertyValue("--complex-progress"), "0%");
    assert.equal(complex.style.getPropertyValue("--complex-total-progress"), "0%");
    assert.equal(complex.classList.contains("is-plan-overrun"), false);
    assert.equal(complex.hasAttribute("role"), false);
    assert.equal(calls.drag.includes(garageTile), true);
    assert.equal(calls.card.includes(garageTile), true);
});


test("активация из гаража создаёт только ожидающий планless-комплекс", () => {
    const {documentStub, calls, mutations} = createHarness();
    const garageTile = new ElementStub({
        dataset: {equipmentId: "82", equipmentCardId: "82", equipmentName: "ЭКГ-5А", excavatorSlot: "3"},
        classNames: ["dispatcher-excavator-garage-tile"],
    });
    const garage = new ElementStub({classNames: ["dispatcher-excavators"]});
    garage.appendChild(garageTile);
    const target = new ElementStub({
        dataset: makePlanDataset({zoneId: "K-3", zoneLabel: "K-3", placementZone: "stale"}),
        classNames: ["dispatcher-complex-card", "status-empty", "is-plan-overrun"],
    });
    target.style.setProperty("--complex-progress", "50%");
    target.style.setProperty("--complex-total-progress", "50%");
    const rack = new ElementStub({classNames: ["complex-assigned-trucks"]});
    target.appendChild(rack);
    documentStub.register(garage, garageTile, target, rack);

    assert.equal(mutations.activateComplexFromExcavatorTile(garageTile, target), true);
    assert.equal(garageTile.removed, true);
    assert.equal(target.dataset.equipmentId, "82");
    assert.equal(target.dataset.dispatcherDrag, "complex");
    assert.equal(target.dataset.placementZone, undefined);
    PLAN_KEYS.forEach((key) => assert.equal(target.dataset[key], undefined));
    assert.equal(target.style.getPropertyValue("--complex-progress"), "0%");
    assert.equal(target.style.getPropertyValue("--complex-total-progress"), "0%");
    assert.equal(target.classList.contains("is-sync-pending"), true);
    assert.equal(target.classList.contains("is-plan-overrun"), false);
    assert.equal(calls.drag.includes(target), true);
    assert.equal(calls.card.includes(target), true);
});


test("доска собирается только с выделенным модулем локальных перестановок", () => {
    const documentStub = {
        body: {classList: new ClassListStub()},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        createElement() { return new ElementStub(); },
    };
    const context = {document: documentStub, window: {}};
    vm.runInNewContext(DND_SOURCE, context, {filename: "dispatcher-board-dnd-v1.js"});
    vm.runInNewContext(MUTATIONS_SOURCE, context, {filename: "dispatcher-board-mutations-v1.js"});
    vm.runInNewContext(BOARD_SOURCE, context, {filename: "dispatcher-board-v1.js"});

    const board = context.window.createDispatcherBoard({
        post() { return Promise.resolve({}); },
        getShiftOpen() { return false; },
        refreshBoardFromServer() { return Promise.resolve(true); },
        reloadFallback() {},
        openEquipmentCard() { return false; },
        equipmentStateClass() { return ""; },
        equipmentStateLabel() { return ""; },
        neutralEquipmentIcon() { return ""; },
        setNodeEquipmentState() {},
    });

    assert.equal(typeof board.bindInteractions, "function");
    assert.equal(typeof board.refreshIntegrity, "function");
});
