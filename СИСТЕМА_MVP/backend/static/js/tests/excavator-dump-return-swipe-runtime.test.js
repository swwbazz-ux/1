"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const sharedReturn = require(path.join(__dirname, "..", "excavator-dump-return-swipe-v1.js"));

const backendRoot = path.resolve(__dirname, "..", "..", "..");
const templateSource = fs.readFileSync(
    path.join(backendRoot, "templates", "trips", "excavator_work.html"),
    "utf8"
).replace(/\r\n?/g, "\n");
const shiftCss = fs.readFileSync(
    path.join(backendRoot, "static", "css", "excavator-work-v55-shift.css"),
    "utf8"
).replace(/\r\n?/g, "\n");

function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `Missing ${label}`);
    const braceStart = source.indexOf("{", start);
    let depth = 0;
    let quote = "";
    let escaped = false;
    for (let index = braceStart; index < source.length; index += 1) {
        const character = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (character === "\\") escaped = true;
            else if (character === quote) quote = "";
            continue;
        }
        if (character === '"' || character === "'" || character === "`") {
            quote = character;
            continue;
        }
        if (character === "{") depth += 1;
        if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    throw new Error(`Unclosed ${label}`);
}

test("direct dump return requires a confident upward swipe", () => {
    assert.equal(sharedReturn.isDumpReturnSwipe(0, -47), false);
    assert.equal(sharedReturn.isDumpReturnSwipe(0, -48), true);
    assert.equal(sharedReturn.isDumpReturnSwipe(20, -56), true);
    assert.equal(sharedReturn.isDumpReturnSwipe(50, -56), false);
    assert.equal(sharedReturn.isDumpReturnSwipe(0, 80), false);
});

test("Driver completion requires the same confident gesture downward", () => {
    assert.equal(sharedReturn.isDumpCompleteSwipe(0, 47), false);
    assert.equal(sharedReturn.isDumpCompleteSwipe(0, 48), true);
    assert.equal(sharedReturn.isDumpCompleteSwipe(20, 56), true);
    assert.equal(sharedReturn.isDumpCompleteSwipe(50, 56), false);
    assert.equal(sharedReturn.isDumpCompleteSwipe(0, -80), false);
});

test("direct return resolves the explicitly marked newest truck and submits once", async () => {
    const latestSource = extractBraceBlock(
        templateSource,
        "function latestPendingTruckBadge",
        "latest pending truck selector"
    );
    const returnSource = extractBraceBlock(
        templateSource,
        "function returnLastTruckFromDump",
        "direct return action"
    );
    const olderBadge = {id: "older", classList: {add() {}, remove() {}}};
    const newestBadge = {id: "newest", classList: {add() {}, remove() {}}};
    const classes = new Set();
    const calls = [];
    const dumpTarget = {
        classList: {
            add(value) { classes.add(value); },
            remove(value) { classes.delete(value); },
            contains(value) { return classes.has(value); },
        },
        querySelector(selector) {
            if (selector.includes('data-eo-last-sent-truck="true"')) return newestBadge;
            if (selector === "[data-eo-queue-truck]") return olderBadge;
            return null;
        },
    };
    const context = {
        Promise,
        playExcavatorSound() {},
        showExcavatorNotice() {},
        queueItemFromBadge(badge) { return {badge}; },
        cancelLoadedTripFromQueue(item, options) {
            calls.push({item, options});
            return Promise.resolve(true);
        },
    };
    vm.runInNewContext(
        `${latestSource}\n${returnSource}; this.run = returnLastTruckFromDump;`,
        context
    );

    assert.equal(await context.run(dumpTarget), true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].item.badge, newestBadge);
    assert.equal(calls[0].options.actionOwner, newestBadge);
    assert.equal(calls[0].options.direct, true);
    assert.equal(classes.has("is-return-pending"), false);
});

test("successful swipe restarts and clears the dump-card rebound", () => {
    const classes = new Set(["is-return-rebounding"]);
    const timers = [];
    let layoutReads = 0;
    const target = {
        classList: {
            add(value) { classes.add(value); },
            remove(value) { classes.delete(value); },
        },
        style: {
            setProperty() {},
            removeProperty() {},
        },
        get offsetWidth() {
            layoutReads += 1;
            return 120;
        },
    };
    const timerRoot = {
        setTimeout(callback, delay) {
            timers.push({callback, delay});
        },
    };
    sharedReturn.playDumpReturnRebound(target, {
        elasticX: 12,
        elasticY: -64,
        elasticTilt: 2,
        elasticStretchX: 1.02,
        elasticStretchY: 1.04,
    }, timerRoot);
    assert.equal(layoutReads, 1);
    assert.equal(classes.has("is-return-rebounding"), true);
    assert.equal(timers.length, 1);
    assert.equal(timers[0].delay, 860);

    timers[0].callback();
    assert.equal(classes.has("is-return-rebounding"), false);
});

test("gesture, fallback queue and realtime safety contracts stay wired", () => {
    assert.match(templateSource, /excavator-dump-return-swipe-v1\.js/);
    assert.match(templateSource, /ExcavatorDumpReturnSwipe\.attach\(\{[\s\S]*holdMs: 560/);
    assert.match(templateSource, /onHold: openDumpQueueModal/);
    assert.match(templateSource, /onReturn: returnLastTruckFromDump/);
    assert.doesNotMatch(templateSource, /function isDumpReturnSwipe/);
    assert.match(templateSource, /\.eo-dashboard-unload-card\.is-return-swiping/);
    assert.match(shiftCss, /\.eo-dashboard-unload-card\.is-return-swiping[\s\S]*translate3d\(var\(--eo-return-drag-x\), var\(--eo-return-drag-y\), 0\)/);
    assert.match(shiftCss, /\.eo-dashboard-unload-card\.is-return-rebounding[\s\S]*animation: eo-dump-return-rebound \.82s/);
    assert.match(shiftCss, /@keyframes eo-dump-return-rebound[\s\S]*--eo-return-bounce-1-y[\s\S]*--eo-return-bounce-2-y[\s\S]*--eo-return-bounce-3-y[\s\S]*--eo-return-bounce-4-y/);
    assert.match(templateSource, /is-return-pending/);
    assert.match(shiftCss, /touch-action: pan-x !important;/);
    assert.match(shiftCss, /\[data-eo-last-sent-truck="true"\]/);
    assert.match(shiftCss, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.is-return-rebounding[\s\S]*animation: none !important/);
});
