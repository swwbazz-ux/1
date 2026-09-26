"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const RUNTIME = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-haul-assignment-state-v1.js"),
    "utf8"
);
const BOARD = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-board-v1.js"),
    "utf8"
);
const CONTROL = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-control-v1.js"),
    "utf8"
);

function createStateRuntime() {
    const context = {};
    context.window = context;
    vm.runInNewContext(RUNTIME, context, {
        filename: "dispatcher-haul-assignment-state-v1.js",
    });
    return context.createDispatcherHaulAssignmentState();
}

test("версии назначений нормализуются и массово собираются без потери нуля", () => {
    const state = createStateRuntime();
    assert.equal(state.getStateId({dataset: {haulAssignmentStateId: "17"}}), "17");
    assert.equal(state.getStateId({dataset: {haulAssignmentStateId: "0007"}}), "0007");
    assert.equal(state.getStateId({dataset: {haulAssignmentStateId: "-1"}}), "0");
    assert.equal(state.getStateId({dataset: {haulAssignmentStateId: ""}}), "0");
    assert.equal(state.getStateId(null), "0");

    const desktopTruck = {dataset: {equipmentId: "17", haulAssignmentStateId: "8"}};
    const mobileTruck = {dataset: {mmMobileHomeTruckId: "18", haulAssignmentStateId: "0"}};
    const states = state.collectComplexStates({
        querySelectorAll(selector) {
            assert.match(selector, /data-complex-truck/);
            assert.match(selector, /data-mm-mobile-home-truck-id/);
            return [desktopTruck, mobileTruck];
        },
    });
    assert.equal(states["17"], "8");
    assert.equal(states["18"], "0");
    assert.deepEqual(Object.keys(states).sort(), ["17", "18"]);
});

test("ответ сервера применяет только свои версии и отличает ноль от отсутствующего поля", () => {
    const state = createStateRuntime();
    const singleTruck = {dataset: {haulAssignmentStateId: "41"}};
    state.applyState({assignment_state_id: 0}, singleTruck);
    assert.equal(singleTruck.dataset.haulAssignmentStateId, "0");
    state.applyState({}, singleTruck);
    assert.equal(singleTruck.dataset.haulAssignmentStateId, "0");

    const desktopTruck = {dataset: {equipmentId: "17", haulAssignmentStateId: "old"}};
    const mobileTruck = {dataset: {mmMobileHomeTruckId: "18", haulAssignmentStateId: "old"}};
    const unknownTruck = {dataset: {equipmentId: "19", haulAssignmentStateId: "keep"}};
    const assignmentStateIds = Object.create({19: 777});
    assignmentStateIds["17"] = 99;
    assignmentStateIds["18"] = 0;
    state.applyStates({assignment_state_ids: assignmentStateIds}, {
        querySelectorAll() {
            return [desktopTruck, mobileTruck, unknownTruck];
        },
    });

    assert.equal(desktopTruck.dataset.haulAssignmentStateId, "99");
    assert.equal(mobileTruck.dataset.haulAssignmentStateId, "0");
    assert.equal(unknownTruck.dataset.haulAssignmentStateId, "keep");
});

test("доска получает только готовый state-adapter и не дублирует его правила", () => {
    assert.match(RUNTIME, /function createDispatcherHaulAssignmentState\(\)/);
    assert.match(RUNTIME, /global\.createDispatcherHaulAssignmentState = createDispatcherHaulAssignmentState;/);
    assert.doesNotMatch(RUNTIME, /dispatcherPost|refreshBoardFromServer|dragstart/);
    assert.match(BOARD, /var dispatcherHaulAssignmentState = options\.assignmentState \|\| \{\};/);
    assert.doesNotMatch(BOARD, /function haulAssignmentStateId\(/);
    assert.match(CONTROL, /window\.createDispatcherHaulAssignmentState\(\)/);
    assert.match(CONTROL, /assignmentState: dispatcherHaulAssignmentState/);
});
