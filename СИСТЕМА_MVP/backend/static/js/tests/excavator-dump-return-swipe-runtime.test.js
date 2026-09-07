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

test("gesture, fallback queue and realtime safety contracts stay wired", () => {
    assert.match(templateSource, /target\.addEventListener\("pointerup"[\s\S]*returnLastTruckFromDump\(target\)/);
    assert.match(templateSource, /window\.setTimeout\(function \(\) \{[\s\S]*openDumpQueueModal\(target\)[\s\S]*\}, 560\)/);
    assert.match(templateSource, /\.eo-dashboard-unload-card\.is-return-swiping/);
    assert.match(templateSource, /\.eo-dashboard-unload-card\.is-return-pending/);
    assert.match(shiftCss, /touch-action: pan-x !important;/);
    assert.match(shiftCss, /\[data-eo-last-sent-truck="true"\]/);
    assert.match(shiftCss, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.is-returned-from-dump/);
});
