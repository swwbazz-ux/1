"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

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
    const source = extractBraceBlock(templateSource, "function isDumpReturnSwipe", "swipe classifier");
    const context = {};
    vm.runInNewContext(`${source}; this.classify = isDumpReturnSwipe;`, context);

    assert.equal(context.classify(0, -47), false);
    assert.equal(context.classify(0, -48), true);
    assert.equal(context.classify(20, -56), true);
    assert.equal(context.classify(50, -56), false);
    assert.equal(context.classify(0, 80), false);
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
    const releaseSource = extractBraceBlock(
        templateSource,
        "function setDumpReturnReleaseVector",
        "dump return release vector"
    );
    const clearSource = extractBraceBlock(
        templateSource,
        "function clearDumpReturnReleaseVector",
        "dump return release cleanup"
    );
    const reboundSource = extractBraceBlock(
        templateSource,
        "function playDumpReturnRebound",
        "dump return rebound"
    );
    const classes = new Set(["is-return-rebounding"]);
    const properties = new Map();
    const timers = [];
    let layoutReads = 0;
    const target = {
        classList: {
            add(value) { classes.add(value); },
            remove(value) { classes.delete(value); },
        },
        style: {
            setProperty(name, value) { properties.set(name, value); },
            removeProperty(name) { properties.delete(name); },
        },
        get offsetWidth() {
            layoutReads += 1;
            return 120;
        },
    };
    const context = {
        window: {
            setTimeout(callback, delay) {
                timers.push({callback, delay});
            },
        },
    };
    vm.runInNewContext(
        `${releaseSource}\n${clearSource}\n${reboundSource}; this.run = playDumpReturnRebound;`,
        context
    );

    context.run(target, {
        elasticX: 28,
        elasticY: -48,
        elasticTilt: 2.5,
        elasticStretchX: 1.04,
        elasticStretchY: 1.09,
    });
    assert.equal(layoutReads, 1);
    assert.equal(classes.has("is-return-rebounding"), true);
    assert.equal(timers.length, 1);
    assert.equal(timers[0].delay, 860);
    assert.equal(properties.get("--eo-return-release-x"), "28.00px");
    assert.equal(properties.get("--eo-return-release-y"), "-48.00px");
    assert.equal(properties.get("--eo-return-bounce-1-y"), "13.44px");

    timers[0].callback();
    assert.equal(classes.has("is-return-rebounding"), false);
    assert.equal(properties.has("--eo-return-release-x"), false);
});

test("the whole dump zone follows the finger with visible rubber resistance in every direction", () => {
    const rubberSource = extractBraceBlock(
        templateSource,
        "function rubberBandDumpReturnOffset",
        "rubber-band offset"
    );
    const updateSource = extractBraceBlock(
        templateSource,
        "function updateDumpReturnElastic",
        "elastic dump update"
    );
    const properties = new Map();
    const target = {
        style: {
            setProperty(name, value) { properties.set(name, value); },
        },
    };
    const context = {Math, Number};
    vm.runInNewContext(
        `${rubberSource}\n${updateSource}; this.update = updateDumpReturnElastic;`,
        context
    );

    const swipe = {deltaX: 64, deltaY: -64};
    context.update(target, swipe);
    assert.ok(swipe.elasticX > 35, "horizontal zone travel must be clearly visible");
    assert.ok(swipe.elasticY < -48, "upward zone travel must be clearly visible");
    assert.equal(properties.get("--eo-return-origin-x"), "0%");
    assert.equal(properties.get("--eo-return-origin-y"), "100%");
    assert.match(properties.get("--eo-return-anchor-x"), /^-\d/);
    assert.match(properties.get("--eo-return-anchor-y"), /^\d/);

    const oppositeSwipe = {deltaX: -48, deltaY: 48};
    context.update(target, oppositeSwipe);
    assert.ok(oppositeSwipe.elasticX < -25);
    assert.ok(oppositeSwipe.elasticY > 35);
    assert.equal(properties.get("--eo-return-origin-x"), "100%");
    assert.equal(properties.get("--eo-return-origin-y"), "0%");
});

test("gesture, fallback queue and realtime safety contracts stay wired", () => {
    assert.match(templateSource, /target\.addEventListener\("pointerup"[\s\S]*returnLastTruckFromDump\(target\)/);
    assert.match(templateSource, /target\.addEventListener\("pointerup"[\s\S]*playDumpReturnRebound\(target, releasedSwipe\)[\s\S]*returnLastTruckFromDump\(target\)/);
    assert.match(templateSource, /window\.setTimeout\(function \(\) \{[\s\S]*openDumpQueueModal\(target\)[\s\S]*\}, 560\)/);
    assert.match(templateSource, /\.eo-dashboard-unload-card\.is-return-swiping/);
    assert.match(shiftCss, /\.eo-dashboard-unload-card\.is-return-swiping[\s\S]*--eo-return-drag-x[\s\S]*--eo-return-drag-y[\s\S]*scaleX\(var\(--eo-return-stretch-x\)\)/);
    assert.match(shiftCss, /\.eo-dashboard-unload-card\.is-return-rebounding[\s\S]*animation: eo-dump-return-rebound \.82s/);
    assert.match(shiftCss, /@keyframes eo-dump-return-rebound[\s\S]*--eo-return-bounce-1-x[\s\S]*--eo-return-bounce-2-x[\s\S]*--eo-return-bounce-3-x[\s\S]*--eo-return-bounce-4-x/);
    assert.match(templateSource, /\.eo-dashboard-unload-card\.is-return-pending/);
    assert.match(shiftCss, /data-eo-has-pending-trucks="true"[\s\S]*touch-action: none !important;/);
    assert.match(shiftCss, /\[data-eo-last-sent-truck="true"\]/);
    const reducedMotionBlock = shiftCss.slice(
        shiftCss.indexOf("@media (prefers-reduced-motion: reduce)"),
        shiftCss.indexOf("@media (max-width: 480px)")
    );
    assert.doesNotMatch(reducedMotionBlock, /\.is-return-swiping/);
    assert.doesNotMatch(reducedMotionBlock, /\.is-return-rebounding/);
});
