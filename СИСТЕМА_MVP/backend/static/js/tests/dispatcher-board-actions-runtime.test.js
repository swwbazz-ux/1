"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-board-actions-v1.js"),
    "utf8"
);

function flush() {
    return new Promise((resolve) => setImmediate(resolve));
}

function createHarness(overrides = {}) {
    const calls = [];
    const context = {window: {}, Promise};
    vm.runInNewContext(SOURCE, context, {filename: "dispatcher-board-actions-v1.js"});
    const options = {
        showError(error) { calls.push(["showError", error.message]); },
        refreshBoardFromServer() {
            calls.push(["refresh"]);
            return Promise.resolve(true);
        },
        reloadFallback() { calls.push(["reload"]); },
        assignmentState: {
            applyState(response, tile) { calls.push(["applyState", response, tile]); },
            applyStates(response, card) { calls.push(["applyStates", response, card]); },
        },
        mutations: {
            moveTruckToComplex(tile, card) {
                calls.push(["moveTruckToComplex", tile, card]);
                return true;
            },
            moveTruckToGarage(tile) {
                calls.push(["moveTruckToGarage", tile]);
                return true;
            },
            releaseComplexTrucks(card) {
                calls.push(["releaseComplexTrucks", card]);
                return true;
            },
        },
        ...overrides,
    };
    const actions = context.window.createDispatcherBoardActions(options);
    return {actions, calls};
}

test("assign сначала применяет серверный токен и только потом переносит самосвал", () => {
    const {actions, calls} = createHarness();
    const response = {assignment_state_id: 91};
    const truckTile = {id: "truck"};
    const complexCard = {id: "complex"};

    assert.equal(actions.applyTruckAction(response, {type: "assign", truckTile, complexCard}), response);
    assert.deepEqual(calls.map((call) => call[0]), ["applyState", "applyStates", "moveTruckToComplex"]);
});

test("release и release_complex используют свои локальные операции", () => {
    const {actions, calls} = createHarness();
    const truckTile = {id: "truck"};
    const complexCard = {id: "complex"};

    actions.applyTruckAction({}, {type: "release", truckTile});
    actions.applyTruckAction({}, {type: "release_complex", complexCard});

    assert.deepEqual(calls.map((call) => call[0]), [
        "applyState",
        "moveTruckToGarage",
        "applyStates",
        "releaseComplexTrucks",
    ]);
});

test("queued-ответ не применяет токены и не меняет локальный DOM", () => {
    const {actions, calls} = createHarness();
    const response = {queued: true};

    assert.equal(actions.applyTruckAction(response, {
        type: "assign",
        truckTile: {},
        complexCard: {},
    }), response);
    assert.deepEqual(calls, []);
});

test("неудачная локальная операция запрашивает fragment и только при его ошибке перезагружает экран", async () => {
    const calls = [];
    const {actions} = createHarness({
        refreshBoardFromServer() {
            calls.push("refresh");
            return Promise.reject(new Error("offline"));
        },
        reloadFallback() { calls.push("reload"); },
        assignmentState: {applyState() {}, applyStates() {}},
        mutations: {
            moveTruckToComplex() { return false; },
            moveTruckToGarage() { return false; },
            releaseComplexTrucks() { return false; },
        },
    });

    actions.applyTruckAction({}, {type: "assign", truckTile: {}, complexCard: {}});
    await flush();

    assert.deepEqual(calls, ["refresh", "reload"]);
});

test("структурное действие использует fallback только когда fragment не применён", async () => {
    const fallbackCalls = [];
    const refreshResults = [false, true];
    const {actions} = createHarness({
        refreshBoardFromServer() { return Promise.resolve(refreshResults.shift()); },
    });

    const first = {ok: 1};
    const second = {ok: 2};
    assert.equal(await actions.refreshAfterStructuralAction(first, () => fallbackCalls.push("first")), first);
    assert.equal(await actions.refreshAfterStructuralAction(second, () => fallbackCalls.push("second")), second);
    assert.deepEqual(fallbackCalls, ["first"]);
});

test("ошибка structural recovery показывается один раз и не оставляет rejected Promise", async () => {
    const {actions, calls} = createHarness({
        refreshBoardFromServer() { return Promise.reject(new Error("refresh offline")); },
    });

    assert.equal(await actions.handleOptimisticError(new Error("move offline")), null);
    assert.deepEqual(calls, [["showError", "move offline"]]);
});
