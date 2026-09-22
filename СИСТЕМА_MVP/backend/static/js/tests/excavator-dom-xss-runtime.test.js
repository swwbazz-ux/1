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

test("Excavator dump queue keeps an untrusted truck number as text", () => {
    const source = extractBraceBlock(
        templateSource,
        "function queueItemFromBadge",
        "dump queue item builder"
    );
    const document = {
        createElement(tagName) {
            return {
                tagName,
                dataset: {},
                children: [],
                addEventListener() {},
                appendChild(child) { this.children.push(child); },
            };
        },
    };
    const maliciousNumber = '<img src=x onerror="globalThis.compromised=true">';
    const context = {
        document,
        truckGreenIcon: "/static/img/truck-green.png",
        beginQueueDrag() {},
    };

    vm.runInNewContext(`${source}; this.build = queueItemFromBadge;`, context);
    const item = context.build({
        dataset: {truckId: "17", tripId: "23", eoTruckNumber: maliciousNumber},
        textContent: "fallback",
    }, {dataset: {eoDumpTarget: "5"}});

    assert.equal(item.children.length, 2);
    assert.equal(item.children[0].tagName, "img");
    assert.equal(item.children[1].tagName, "strong");
    assert.equal(item.children[1].textContent, maliciousNumber);
    assert.equal(Object.hasOwn(item, "innerHTML"), false);
    assert.equal(context.compromised, undefined);
});
