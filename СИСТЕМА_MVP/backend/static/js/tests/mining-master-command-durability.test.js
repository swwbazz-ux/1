"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const template = fs.readFileSync(path.resolve(__dirname, "../../../templates/trips/dispatcher_control.html"), "utf8");
const fillCode = template.slice(template.indexOf("        function confirmMobileFillScreen("), template.indexOf("        function moveMobileHomeTruckLocal("));
const transferCode = template.slice(template.indexOf("        function completeMobileTruckTransfer("), template.indexOf("        if (mobileTruckTransferCancel)", template.indexOf("        function completeMobileTruckTransfer(")));
const classes = {remove() {}, add() {}, contains() { return false; }};
function fillRuntime(post) {
    const events = [];
    const assigned = {dataset: {mmMobileAssignedTruckId: "7", mmMobileFillAdded: "true"}};
    const released = {dataset: {mmMobileFillTruckId: "8", mmMobileFillWasAssigned: "true"}};
    const screen = {
        dataset: {}, classList: classes,
        querySelector: () => ({dataset: {mmMobileFillExcavatorId: "9"}, classList: classes, style: {removeProperty() {}}}),
        querySelectorAll: (selector) => selector.startsWith("[data-mm-mobile-assigned")
            ? (assigned.dataset.mmMobileFillAdded === "true" ? [assigned] : [])
            : (released.dataset.mmMobileFillWasAssigned === "true" ? [released] : []),
    };
    const context = vm.createContext({
        Promise, Array, isMiningMasterMobileReadonly: () => false,
        dispatcherAssignTruckUrl: "/assign/", dispatcherPostQueued: post,
        haulAssignmentStateId: () => "11", showDispatcherDnDError: (error) => events.push(error.message),
        syncMobileHomeComplexFromFill: () => events.push("home"), closeDetails: () => events.push("close"),
        updateMobileHomeComplexGridLayout() {}, scheduleMobileLocalLayoutReconcile() {},
        window: {requestAnimationFrame: (fn) => fn()},
    });
    vm.runInContext(fillCode, context);
    return {context, events, screen, assigned, released};
}

test("master fill confirms the home board only after all commands are saved", async () => {
    const resolvers = [];
    const r = fillRuntime(() => new Promise((resolve) => resolvers.push(resolve)));
    const saved = r.context.confirmMobileFillScreen(r.screen);
    assert.deepEqual(r.events, []);
    resolvers.forEach((resolve) => resolve({queued: true}));
    await saved;
    assert.deepEqual(r.events, ["home", "close"]);
});

test("partial quota keeps unsaved fill intent and does not repeat commands already recorded", async () => {
    const calls = [];
    let failRelease = true;
    const r = fillRuntime((url, payload) => {
        calls.push(payload.action);
        return failRelease && payload.action === "release" ? Promise.reject(new Error("quota")) : Promise.resolve({queued: true});
    });
    await r.context.confirmMobileFillScreen(r.screen);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(r.events, ["quota"]);
    assert.equal(r.assigned.dataset.mmMobileFillAdded, undefined);
    assert.equal(r.released.dataset.mmMobileFillWasAssigned, "true");
    failRelease = false;
    await r.context.confirmMobileFillScreen(r.screen);
    assert.deepEqual(calls, ["assign", "release", "release"]);
    assert.deepEqual(r.events, ["quota", "home", "close"]);
});

test("master transfer does not show completion or move the truck before the command is saved", async () => {
    const events = [];
    let resolveSave;
    const transfer = {truckId: "7", sourceExcavatorId: "8", assignmentStateId: "11", sourceLabel: "К-1", truckNumber: "101", homeMini: {}};
    const target = {classList: classes, dataset: {mmMobileExcavatorId: "9"}, querySelector: () => ({textContent: "К-2", classList: classes, dataset: {}})};
    const context = vm.createContext({
        Promise, mobileTruckTransfer: transfer, home: {}, CSS: {escape: (v) => v},
        dispatcherAssignTruckUrl: "/assign/", dispatcherPostQueued: () => new Promise((resolve) => { resolveSave = resolve; }),
        moveMobileHomeTruckLocal: () => { events.push("move"); const undo = () => events.push("undo"); undo.commit = () => events.push("commit"); return undo; },
        finishMobileTruckTransferUi: () => events.push("finish"), showDispatcherDnDError: () => events.push("error"),
        pulseMiningMasterInfoTarget() {},
    });
    vm.runInContext(transferCode, context);
    const saved = context.completeMobileTruckTransfer(target);
    assert.deepEqual(events, []);
    resolveSave({queued: true});
    await saved;
    assert.deepEqual(events, ["move", "finish", "commit"]);
});
