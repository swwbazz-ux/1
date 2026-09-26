"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const DND_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-board-dnd-v1.js"),
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
}


class ElementStub {
    constructor({dataset = {}, classNames = []} = {}) {
        this.dataset = {...dataset};
        this.classList = new ClassListStub(classNames);
        this.listeners = new Map();
        this.selectorOne = new Map();
        this.selectorAll = new Map();
        this.parentNode = null;
        this.removed = false;
        this.className = "";
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    listenerCount(type) {
        return (this.listeners.get(type) || []).length;
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

    querySelector(selector) {
        return this.selectorOne.get(selector) || null;
    }

    querySelectorAll(selector) {
        return this.selectorAll.get(selector) || [];
    }

    setOne(selector, value) {
        this.selectorOne.set(selector, value);
        return this;
    }

    setAll(selector, values) {
        this.selectorAll.set(selector, values);
        return this;
    }

    cloneNode() {
        return new ElementStub();
    }

    remove() {
        this.removed = true;
    }
}


class DocumentStub {
    constructor() {
        this.selectorAll = new Map();
        this.nodes = [];
        this.body = {
            appended: [],
            appendChild: (node) => {
                node.parentNode = this.body;
                this.body.appended.push(node);
            },
        };
    }

    setAll(selector, nodes) {
        this.selectorAll.set(selector, nodes);
        nodes.forEach((node) => {
            if (!this.nodes.includes(node)) this.nodes.push(node);
        });
    }

    querySelectorAll(selector) {
        if (selector === ".dispatcher-drop-target") {
            return this.nodes.filter((node) => node.classList.contains("dispatcher-drop-target"));
        }
        return this.selectorAll.get(selector) || [];
    }
}


function dataTransfer() {
    const values = new Map();
    return {
        effectAllowed: "",
        dragImage: null,
        setData(key, value) {
            values.set(key, value);
        },
        setDragImage(node, x, y) {
            this.dragImage = {node, x, y};
        },
        getData(key) {
            return values.get(key);
        },
    };
}


async function flush() {
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    await Promise.resolve();
}


function plain(value) {
    return JSON.parse(JSON.stringify(value));
}


function createHarness({shiftOpen = true, postResult = {}, autoConfirm = true} = {}) {
    const documentStub = new DocumentStub();
    const board = new ElementStub();
    const posts = [];
    const truckActions = [];
    const structuralCalls = [];
    const confirmations = [];
    const errors = [];
    const order = [];
    const context = {
        Promise,
        document: documentStub,
        window: {},
    };
    vm.runInNewContext(DND_SOURCE, context, {
        filename: "dispatcher-board-dnd-v1.js",
    });
    const dnd = context.window.createDispatcherBoardDnD({
        document: documentStub,
        post(url, payload, options) {
            posts.push({url, payload, options});
            if (postResult instanceof Error) return Promise.reject(postResult);
            return Promise.resolve(postResult);
        },
        moveExcavatorUrl: "/dispatcher/excavator/move/",
        assignTruckUrl: "/dispatcher/assign-truck/",
        getShiftOpen() {
            return shiftOpen;
        },
        getBoard() {
            return board;
        },
        getAssignmentStateId(tile) {
            return tile.dataset.haulAssignmentStateId || "0";
        },
        collectComplexAssignmentStates(card) {
            const states = {};
            card.querySelectorAll("[data-complex-truck='true']").forEach((truck) => {
                states[truck.dataset.equipmentId] = truck.dataset.haulAssignmentStateId || "0";
            });
            return states;
        },
        applyHaulAssignmentStates(response, card) {
            order.push("apply");
            const states = response.assignment_state_ids || {};
            card.querySelectorAll("[data-complex-truck='true']").forEach((truck) => {
                if (Object.prototype.hasOwnProperty.call(states, truck.dataset.equipmentId)) {
                    truck.dataset.haulAssignmentStateId = String(states[truck.dataset.equipmentId]);
                }
            });
        },
        applyDesktopTruckAction(response, action) {
            truckActions.push({response, action});
            return response;
        },
        refreshDesktopBoardAfterStructuralAction(response, fallback) {
            structuralCalls.push(response);
            if (typeof fallback === "function") fallback();
            return Promise.resolve(response);
        },
        activateDesktopComplexFromExcavatorTile(tile, zone) {
            order.push("activate:" + tile.dataset.equipmentId + ":" + (zone.dataset.zoneId || ""));
        },
        moveDesktopComplexToExcavatorGarage(card) {
            const trucks = card.querySelectorAll("[data-complex-truck='true']");
            order.push("move:" + card.dataset.equipmentId + ":" + trucks[0].dataset.haulAssignmentStateId);
        },
        handleStructuralError(error) {
            errors.push("structural:" + error.message);
        },
        showError(error) {
            errors.push("normal:" + error.message);
        },
        confirmDanger(options) {
            confirmations.push(options);
            if (autoConfirm) options.action();
        },
    });
    return {
        dnd,
        documentStub,
        board,
        posts,
        truckActions,
        structuralCalls,
        confirmations,
        errors,
        order,
    };
}


function bind(harness, {drags = [], complexDrops = [], excavatorGarageDrops = [], truckGarageDrops = []}) {
    harness.documentStub.setAll("[data-dispatcher-drag]", drags);
    harness.documentStub.setAll("[data-dispatcher-drop='complex']", complexDrops);
    harness.documentStub.setAll("[data-dispatcher-drop='excavator-garage']", excavatorGarageDrops);
    harness.documentStub.setAll("[data-dispatcher-drop='truck-garage']", truckGarageDrops);
    harness.dnd.bind();
}


test("rebind не дублирует обработчик и самосвал назначается одним точным POST", async () => {
    const truck = new ElementStub({
        dataset: {dispatcherDrag: "truck", equipmentId: "10", equipmentName: "10", haulAssignmentStateId: "41"},
    });
    const complex = new ElementStub({
        dataset: {dispatcherDrop: "complex", equipmentId: "3", zoneId: "K-3"},
    });
    const harness = createHarness({postResult: {assignment_state_id: 77}});

    bind(harness, {drags: [truck], complexDrops: [complex]});
    bind(harness, {drags: [truck], complexDrops: [complex]});
    truck.dispatch("dragstart", {dataTransfer: dataTransfer()});
    complex.dispatch("drop");
    await flush();

    assert.equal(truck.listenerCount("dragstart"), 1);
    assert.equal(complex.listenerCount("drop"), 1);
    assert.equal(harness.posts.length, 1);
    assert.equal(harness.posts[0].url, "/dispatcher/assign-truck/");
    assert.deepEqual(plain(harness.posts[0].payload), {
        action: "assign",
        truck_id: "10",
        excavator_id: "3",
        expected_assignment_state_id: "41",
    });
    assert.equal(harness.truckActions.length, 1);
    assert.equal(harness.truckActions[0].action.type, "assign");
    assert.equal(harness.truckActions[0].action.truckTile, truck);
});


test("самосвал из комплекса возвращается в гараж с текущим токеном", async () => {
    const truck = new ElementStub({
        dataset: {
            dispatcherDrag: "truck",
            complexTruck: "true",
            equipmentId: "11",
            equipmentName: "11",
            haulAssignmentStateId: "53",
        },
    });
    const garage = new ElementStub({dataset: {dispatcherDrop: "truck-garage"}});
    const harness = createHarness({postResult: {assignment_state_id: 54}});

    bind(harness, {drags: [truck], truckGarageDrops: [garage]});
    truck.dispatch("dragstart", {dataTransfer: dataTransfer()});
    garage.dispatch("drop");
    await flush();

    assert.equal(harness.confirmations.length, 0);
    assert.equal(harness.posts.length, 1);
    assert.deepEqual(plain(harness.posts[0].payload), {
        action: "release",
        truck_id: "11",
        expected_assignment_state_id: "53",
    });
    assert.equal(harness.truckActions[0].action.type, "release");
    assert.equal(harness.truckActions[0].action.truckTile, truck);
});


test("экскаватор из гаража активируется структурной командой без offline queue", async () => {
    const excavator = new ElementStub({
        dataset: {dispatcherDrag: "excavator", equipmentId: "8", equipmentName: "8"},
    });
    const complex = new ElementStub({
        dataset: {dispatcherDrop: "complex", zoneId: "K-4"},
    });
    const harness = createHarness({postResult: {ok: true}});

    bind(harness, {drags: [excavator], complexDrops: [complex]});
    excavator.dispatch("dragstart", {dataTransfer: dataTransfer()});
    complex.dispatch("drop");
    await flush();

    assert.equal(harness.posts.length, 1);
    assert.equal(harness.posts[0].url, "/dispatcher/excavator/move/");
    assert.deepEqual(plain(harness.posts[0].payload), {
        excavator_id: "8",
        zone: "active",
        expected_zone: "inactive",
    });
    assert.deepEqual(plain(harness.posts[0].options), {queueOnNetworkFailure: false});
    assert.equal(harness.structuralCalls.length, 1);
    assert.deepEqual(harness.order, ["activate:8:K-4"]);
});


test("расформирование требует подтверждения и обновляет токены до local fallback", async () => {
    const truck = new ElementStub({
        dataset: {complexTruck: "true", equipmentId: "17", haulAssignmentStateId: "41"},
    });
    const complex = new ElementStub({
        dataset: {dispatcherDrag: "complex", equipmentId: "3", equipmentName: "3"},
    }).setAll("[data-complex-truck='true']", [truck]);
    const garage = new ElementStub({dataset: {dispatcherDrop: "excavator-garage"}});
    const harness = createHarness({
        autoConfirm: false,
        postResult: {assignment_state_ids: {17: 99}},
    });

    bind(harness, {drags: [complex], excavatorGarageDrops: [garage]});
    complex.dispatch("dragstart", {dataTransfer: dataTransfer()});
    garage.dispatch("drop");

    assert.equal(harness.confirmations.length, 1);
    assert.equal(harness.posts.length, 0, "без подтверждения опасная команда не уходит");

    harness.confirmations[0].action();
    await flush();

    assert.equal(harness.posts.length, 1);
    assert.deepEqual(plain(harness.posts[0].payload), {
        excavator_id: "3",
        zone: "inactive",
        expected_zone: "active",
        expected_assignment_states: {17: "41"},
    });
    assert.deepEqual(plain(harness.posts[0].options), {queueOnNetworkFailure: false});
    assert.deepEqual(harness.order, ["apply", "move:3:99"]);
});


test("снятие всего комплекса требует подтверждения и отправляет release_complex", async () => {
    const truck = new ElementStub({
        dataset: {complexTruck: "true", equipmentId: "19", haulAssignmentStateId: "60"},
    });
    const complex = new ElementStub({
        dataset: {dispatcherDrag: "complex", equipmentId: "5", equipmentName: "5"},
    }).setAll("[data-complex-truck='true']", [truck]);
    const garage = new ElementStub({dataset: {dispatcherDrop: "truck-garage"}});
    const harness = createHarness({autoConfirm: false, postResult: {assignment_state_ids: {19: 61}}});

    bind(harness, {drags: [complex], truckGarageDrops: [garage]});
    complex.dispatch("dragstart", {dataTransfer: dataTransfer()});
    garage.dispatch("drop");
    assert.equal(harness.posts.length, 0);
    assert.equal(harness.confirmations.length, 1);

    harness.confirmations[0].action();
    await flush();

    assert.deepEqual(plain(harness.posts[0].payload), {
        action: "release_complex",
        excavator_id: "5",
        expected_assignment_states: {19: "60"},
    });
    assert.deepEqual(plain(harness.posts[0].options), {queueOnNetworkFailure: false});
    assert.equal(harness.truckActions[0].action.type, "release_complex");
    assert.equal(harness.truckActions[0].action.complexCard, complex);
});


test("drag-session чистит ghost и подсветку, а закрытая смена не начинает действие", () => {
    const progress = new ElementStub();
    const complex = new ElementStub({
        dataset: {dispatcherDrag: "complex", equipmentId: "3", equipmentName: "3"},
    }).setOne(".equipment-progress-complex", progress);
    const garage = new ElementStub({dataset: {dispatcherDrop: "excavator-garage"}});
    const harness = createHarness();

    bind(harness, {drags: [complex], excavatorGarageDrops: [garage]});
    const transfer = dataTransfer();
    complex.dispatch("dragstart", {dataTransfer: transfer});
    garage.dispatch("dragover");

    assert.equal(complex.classList.contains("dispatcher-dragging"), true);
    assert.equal(harness.board.classList.contains("is-complex-dragging"), true);
    assert.equal(garage.classList.contains("dispatcher-drop-target"), true);
    assert.equal(harness.documentStub.body.appended.length, 1);
    assert.ok(transfer.dragImage);

    complex.dispatch("dragend");
    assert.equal(complex.classList.contains("dispatcher-dragging"), false);
    assert.equal(harness.board.classList.contains("is-complex-dragging"), false);
    assert.equal(garage.classList.contains("dispatcher-drop-target"), false);
    assert.equal(harness.documentStub.body.appended[0].removed, true);

    const closedTruck = new ElementStub({
        dataset: {dispatcherDrag: "truck", equipmentId: "10", equipmentName: "10"},
    });
    const closedHarness = createHarness({shiftOpen: false});
    bind(closedHarness, {drags: [closedTruck]});
    const start = closedTruck.dispatch("dragstart", {dataTransfer: dataTransfer()});
    assert.equal(start.defaultPrevented, true);
    assert.equal(closedHarness.posts.length, 0);
});


test("сброс после замены fragment не даёт старой плитке выполнить drop", () => {
    const staleTruck = new ElementStub({
        dataset: {dispatcherDrag: "truck", equipmentId: "10", equipmentName: "10", haulAssignmentStateId: "41"},
    });
    const staleComplex = new ElementStub({
        dataset: {dispatcherDrop: "complex", equipmentId: "3"},
    });
    const freshTruck = new ElementStub({
        dataset: {dispatcherDrag: "truck", equipmentId: "11", equipmentName: "11", haulAssignmentStateId: "42"},
    });
    const freshComplex = new ElementStub({
        dataset: {dispatcherDrop: "complex", equipmentId: "4"},
    });
    const harness = createHarness();

    bind(harness, {drags: [staleTruck], complexDrops: [staleComplex]});
    staleTruck.dispatch("dragstart", {dataTransfer: dataTransfer()});
    harness.dnd.resetSession();

    bind(harness, {drags: [freshTruck], complexDrops: [freshComplex]});
    freshComplex.dispatch("drop");
    assert.equal(harness.posts.length, 0);

    freshTruck.dispatch("dragstart", {dataTransfer: dataTransfer()});
    freshComplex.dispatch("drop");
    assert.equal(harness.posts.length, 1);
    assert.deepEqual(plain(harness.posts[0].payload), {
        action: "assign",
        truck_id: "11",
        excavator_id: "4",
        expected_assignment_state_id: "42",
    });
});


test("ошибка POST останавливает local action и проходит через верный recovery callback", async () => {
    const truck = new ElementStub({
        dataset: {dispatcherDrag: "truck", equipmentId: "10", equipmentName: "10", haulAssignmentStateId: "41"},
    });
    const complex = new ElementStub({
        dataset: {dispatcherDrop: "complex", equipmentId: "3"},
    });
    const harness = createHarness({postResult: new Error("offline")});

    bind(harness, {drags: [truck], complexDrops: [complex]});
    truck.dispatch("dragstart", {dataTransfer: dataTransfer()});
    complex.dispatch("drop");
    await flush();

    assert.deepEqual(harness.errors, ["normal:offline"]);
    assert.equal(harness.truckActions.length, 0);
});
