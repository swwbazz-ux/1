"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-equipment-card-trigger-v1.js"),
    "utf8"
);

class ClassListStub {
    constructor(classNames = []) {
        this.values = new Set(classNames);
    }

    contains(className) {
        return this.values.has(className);
    }
}

class ElementStub {
    constructor({cardId = "", classNames = []} = {}) {
        this.dataset = {};
        if (cardId) this.dataset.equipmentCardId = cardId;
        this.classList = new ClassListStub(classNames);
        this.listeners = new Map();
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    dispatch(type, values = {}) {
        const event = {
            type,
            key: values.key || "",
            target: values.target || this,
            defaultPrevented: false,
            propagationStopped: false,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this.propagationStopped = true; },
        };
        (this.listeners.get(type) || []).forEach((listener) => listener(event));
        return event;
    }

    closest() {
        return null;
    }
}

function createHarness(openEquipmentCard) {
    const context = {window: {}};
    vm.runInNewContext(SOURCE, context, {filename: "dispatcher-equipment-card-trigger-v1.js"});
    return context.window.createDispatcherEquipmentCardTrigger({openEquipmentCard});
}

test("повторная привязка не дублирует открытие карточки", () => {
    const opened = [];
    const trigger = createHarness((cardId, node) => {
        opened.push({cardId, node});
        return true;
    });
    const node = new ElementStub({cardId: "truck-17", classNames: ["dispatcher-truck-tile"]});

    trigger.bind(node);
    trigger.bind(node);
    const event = node.dispatch("click");

    assert.equal(opened.length, 1);
    assert.equal(opened[0].cardId, "truck-17");
    assert.equal(opened[0].node, node);
    assert.equal(event.defaultPrevented, true);
    assert.equal(event.propagationStopped, true);
    assert.equal(node.dataset.cardBound, "true");
});

test("placeholder и вложенный элемент управления не открывают карточку", () => {
    let openCount = 0;
    const trigger = createHarness(() => {
        openCount += 1;
        return true;
    });
    const placeholder = new ElementStub({cardId: "truck-empty", classNames: ["is-placeholder"]});
    const regular = new ElementStub({cardId: "truck-5"});
    const nestedControl = {closest() { return {}; }};

    trigger.bind(placeholder);
    trigger.bind(regular);
    placeholder.dispatch("click");
    regular.dispatch("click", {target: nestedControl});

    assert.equal(openCount, 0);
});

test("Enter и пробел открывают карточку, а отказ detail-модуля не поглощает событие", () => {
    const opened = [];
    const trigger = createHarness((cardId) => {
        opened.push(cardId);
        return cardId !== "blocked";
    });
    const active = new ElementStub({cardId: "excavator-82"});
    const blocked = new ElementStub({cardId: "blocked"});

    trigger.bind(active);
    trigger.bind(blocked);
    const enterEvent = active.dispatch("keydown", {key: "Enter"});
    const spaceEvent = active.dispatch("keydown", {key: " "});
    const ignoredEvent = active.dispatch("keydown", {key: "Escape"});
    const blockedEvent = blocked.dispatch("keydown", {key: "Enter"});

    assert.deepEqual(opened, ["excavator-82", "excavator-82", "blocked"]);
    assert.equal(enterEvent.defaultPrevented, true);
    assert.equal(spaceEvent.defaultPrevented, true);
    assert.equal(ignoredEvent.defaultPrevented, false);
    assert.equal(blockedEvent.defaultPrevented, false);
});
