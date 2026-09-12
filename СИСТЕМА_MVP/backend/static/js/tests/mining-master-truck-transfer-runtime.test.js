"use strict";

/* Механика перестановки самосвала между комплексами у Горного мастера.

   Тест исполняет НАСТОЯЩИЙ обработчик из шаблона в песочнице vm, а не
   сверяет исходник по подстрокам: ломалось именно поведение жеста. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const TEMPLATE_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "..", "templates", "trips", "dispatcher_control.html"),
    "utf8"
);

function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, label + ": сигнатура не найдена.");
    const open = source.indexOf("{", start + signature.length);
    assert.notEqual(open, -1, label + ": не найдена открывающая скобка.");
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
            if (escaped) escaped = false;
            else if (character === "\\") escaped = true;
            else if (character === quote) quote = "";
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
        if (character === "{") depth += 1;
        else if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(label + ": не найдена закрывающая скобка.");
    return "";
}

function readNumericConstant(name) {
    const match = TEMPLATE_SOURCE.match(new RegExp("var\\s+" + name + "\\s*=\\s*(\\d+)\\s*;"));
    assert.notEqual(match, null, "Константа " + name + " не найдена в шаблоне.");
    return Number(match[1]);
}

const START_PX = readNumericConstant("MOBILE_TRUCK_TRANSFER_START_PX");
const HOLD_MS = readNumericConstant("MOBILE_TRUCK_TRANSFER_HOLD_MS");
const DWELL_MS = readNumericConstant("MOBILE_TRUCK_TRANSFER_DWELL_MS");
const MOVE_PX = readNumericConstant("MOBILE_TRUCK_TRANSFER_MOVE_PX");

class ClassListStub {
    constructor(values) {
        this.values = new Set(values || []);
    }
    contains(name) { return this.values.has(name); }
    add() { Array.prototype.forEach.call(arguments, (name) => this.values.add(name)); }
    remove() { Array.prototype.forEach.call(arguments, (name) => this.values.delete(name)); }
    toggle(name, force) { if (force) { this.values.add(name); } else { this.values.delete(name); } }
}

class NodeStub {
    constructor(options) {
        const config = options || {};
        this.dataset = Object.assign({}, config.dataset);
        this.classList = new ClassListStub(config.classNames);
        this.style = {setProperty() {}, removeProperty() {}};
        this.isConnected = true;
        this.listeners = new Map();
        this.textContent = config.textContent || "";
        this.children = [];
    }
    setAttribute() {}
    removeAttribute() {}
    addEventListener(type, listener) {
        const list = this.listeners.get(type) || [];
        list.push(listener);
        this.listeners.set(type, list);
    }
    removeEventListener() {}
    dispatch(type, event) {
        const payload = event || {};
        payload.type = type;
        payload.preventDefault = () => {};
        payload.stopPropagation = () => {};
        for (const listener of this.listeners.get(type) || []) listener.call(this, payload);
        return payload;
    }
    getBoundingClientRect() { return {left: 0, top: 0, width: 40, height: 24}; }
    cloneNode() { return new NodeStub(); }
    querySelectorAll() { return []; }
    querySelector() { return null; }
    closest() { return null; }
    appendChild(node) { this.children.push(node); return node; }
    remove() {}
}

function createHarness() {
    const calls = {begin: 0, complete: [], cancel: 0, pulse: 0};
    const target = new NodeStub({
        dataset: {mmMobileExcavatorId: "7"},
        classNames: ["mm-mobile-complex-card", "is-transfer-target"],
    });
    const mini = new NodeStub({
        dataset: {
            mmMobileHomeTruckId: "101",
            mmMobileHomeTruckNumber: "101",
            mmMobileHomeSourceExcavatorId: "3",
        },
        classNames: ["mm-mobile-truck-mini"],
    });

    let pointerOverTarget = false;
    const body = new NodeStub();
    const documentStub = {
        body: body,
        elementsFromPoint() {
            if (!pointerOverTarget) return [];
            return [{closest: (selector) => (selector.indexOf("is-transfer-target") >= 0 ? target : null)}];
        },
    };
    const windowStub = {
        setTimeout: function () { return setTimeout.apply(null, arguments); },
        clearTimeout: function () { return clearTimeout.apply(null, arguments); },
        navigator: {},
        addEventListener() {},
        removeEventListener() {},
        PointerEvent: function PointerEvent() {},
    };

    const sandbox = {
        document: documentStub,
        window: windowStub,
        setTimeout: windowStub.setTimeout,
        clearTimeout: windowStub.clearTimeout,
        MOBILE_TRUCK_TRANSFER_START_PX: START_PX,
        MOBILE_TRUCK_TRANSFER_HOLD_MS: HOLD_MS,
        MOBILE_TRUCK_TRANSFER_DWELL_MS: DWELL_MS,
        MOBILE_TRUCK_TRANSFER_MOVE_PX: MOVE_PX,
        mobileTruckTransfer: {sourceCard: null},
        isMiningMasterMobileReadonly: () => false,
        beginMobileTruckTransfer: () => { calls.begin += 1; return true; },
        completeMobileTruckTransfer: (card) => { calls.complete.push(card); },
        cancelMobileTruckTransfer: () => { calls.cancel += 1; },
        pulseMiningMasterInfoTarget: () => { calls.pulse += 1; },
        openEquipmentCard: () => {},
        shell: {contains: () => true},
    };
    vm.createContext(sandbox);
    const binder = extractBraceBlock(
        TEMPLATE_SOURCE,
        "function bindMobileTruckTransferDrag(mini)",
        "bindMobileTruckTransferDrag"
    );
    sandbox.__mini = mini;
    vm.runInContext(binder + "\nbindMobileTruckTransferDrag(__mini);", sandbox);

    return {
        calls: calls,
        mini: mini,
        target: target,
        overTarget(value) { pointerOverTarget = value; },
    };
}

function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

test("перестановка начинается при движении вниз, а не только вбок", () => {
    const harness = createHarness();
    harness.mini.dispatch("pointerdown", {pointerId: 1, clientX: 100, clientY: 100, pointerType: "touch"});
    harness.mini.dispatch("pointermove", {pointerId: 1, clientX: 100, clientY: 100 + START_PX + 4});
    assert.equal(
        harness.calls.begin,
        1,
        "Вертикальный жест обязан запускать перестановку: у плитки touch-action: none, прокручивать нечего."
    );
});

test("перестановка начинается по удержанию без движения пальца", async () => {
    const harness = createHarness();
    harness.mini.dispatch("pointerdown", {pointerId: 1, clientX: 100, clientY: 100, pointerType: "touch"});
    await wait(HOLD_MS + 80);
    assert.equal(
        harness.calls.begin,
        1,
        "Удержание должно перестраивать доску до того, как мастер выбрал направление."
    );
});

test("короткое касание не превращается в перестановку", async () => {
    const harness = createHarness();
    harness.mini.dispatch("pointerdown", {pointerId: 1, clientX: 100, clientY: 100, pointerType: "touch"});
    await wait(Math.max(10, HOLD_MS - 140));
    harness.mini.dispatch("pointerup", {pointerId: 1, clientX: 100, clientY: 100});
    assert.equal(harness.calls.begin, 0, "Обычное нажатие не должно превращаться в перестановку.");
    assert.equal(harness.calls.complete.length, 0);
});

test("бросок сквозь доску без выдержки над комплексом не назначает самосвал", () => {
    const harness = createHarness();
    harness.mini.dispatch("pointerdown", {pointerId: 1, clientX: 100, clientY: 100, pointerType: "touch"});
    harness.mini.dispatch("pointermove", {pointerId: 1, clientX: 100 + START_PX + 4, clientY: 100});
    harness.overTarget(true);
    harness.mini.dispatch("pointermove", {pointerId: 1, clientX: 260, clientY: 240});
    harness.mini.dispatch("pointerup", {pointerId: 1, clientX: 260, clientY: 240});
    assert.equal(
        harness.calls.complete.length,
        0,
        "Комплекс, случайно оказавшийся под пальцем при отпускании, не должен получать самосвал."
    );
    assert.equal(harness.calls.cancel, 1, "Жест должен отменяться, а не назначать наугад.");
});

test("после выдержки над комплексом перестановка выполняется", async () => {
    const harness = createHarness();
    harness.mini.dispatch("pointerdown", {pointerId: 1, clientX: 100, clientY: 100, pointerType: "touch"});
    harness.mini.dispatch("pointermove", {pointerId: 1, clientX: 100 + START_PX + 4, clientY: 100});
    harness.overTarget(true);
    harness.mini.dispatch("pointermove", {pointerId: 1, clientX: 260, clientY: 240});
    await wait(DWELL_MS + 80);
    harness.mini.dispatch("pointerup", {pointerId: 1, clientX: 260, clientY: 240});
    assert.equal(harness.calls.complete.length, 1, "Наведённая цель обязана принимать самосвал.");
    assert.equal(harness.calls.complete[0], harness.target);
});

test("короткий путь до подъехавшего комплекса не мешает перестановке", async () => {
    /* При входе в режим перестановки доска перекраивается и соседний комплекс
       подъезжает к пальцу. Прежний порог travelDistance >= 36 отклонял такой
       жест: на Redmi 22011119UY связка K-5 -> K-3 стабильно не срабатывала,
       палец проходил 30 CSS-пикселей при подсвеченной цели. */
    const harness = createHarness();
    harness.mini.dispatch("pointerdown", {pointerId: 1, clientX: 100, clientY: 100, pointerType: "touch"});
    harness.mini.dispatch("pointermove", {pointerId: 1, clientX: 100 + START_PX + 4, clientY: 100});
    harness.overTarget(true);
    harness.mini.dispatch("pointermove", {pointerId: 1, clientX: 150, clientY: 112});
    await wait(DWELL_MS + 80);
    harness.mini.dispatch("pointerup", {pointerId: 1, clientX: 150, clientY: 112});
    assert.equal(
        harness.calls.complete.length,
        1,
        "Наведённая и выдержанная цель обязана принимать самосвал даже на коротком пути."
    );
});

test("удержание без движения не переставляет самосвал, даже если под пальцем оказался комплекс", async () => {
    /* После входа в режим доска перекраивается, и под неподвижным пальцем
       может оказаться соседний комплекс. Отпустить, не двигая, — не жест. */
    const harness = createHarness();
    harness.mini.dispatch("pointerdown", {pointerId: 1, clientX: 100, clientY: 100, pointerType: "touch"});
    await wait(HOLD_MS + 80);
    assert.equal(harness.calls.begin, 1, "Удержание должно включить режим перестановки.");
    harness.overTarget(true);
    harness.mini.dispatch("pointermove", {pointerId: 1, clientX: 103, clientY: 101});
    await wait(DWELL_MS + 80);
    harness.mini.dispatch("pointerup", {pointerId: 1, clientX: 103, clientY: 101});
    assert.equal(harness.calls.complete.length, 0, "Без движения после перестройки перестановки быть не должно.");
    assert.equal(harness.calls.cancel, 1);
});
