"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const TEMPLATE_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "..", "templates", "trips", "dispatcher_control.html"),
    "utf8"
);
const CSS_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "css", "dispatcher-control-v1.css"),
    "utf8"
);

function extractBraceBlock(source, signature) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${signature} was not found`);
    const open = source.indexOf("{", start + signature.length);
    let depth = 0;
    for (let index = open; index < source.length; index += 1) {
        if (source[index] === "{") depth += 1;
        if (source[index] === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`${signature} closing brace was not found`);
}

function runRackLayout({tileCount, rackWidth, rackHeight, garageWidth = 82, garageHeight = 70}) {
    const values = new Map();
    const tiles = Array.from({length: tileCount}, () => ({}));
    const rack = {
        clientWidth: rackWidth,
        clientHeight: rackHeight,
        classList: {add() {}, remove() {}},
        querySelector(selector) {
            return selector === ".complex-truck-empty" ? null : null;
        },
        querySelectorAll(selector) {
            return selector === ".complex-truck-tile" ? tiles : [];
        },
        getBoundingClientRect() {
            return {width: rackWidth, height: rackHeight};
        },
        style: {
            setProperty(name, value) {
                values.set(name, value);
            },
        },
    };
    const garageTile = {
        getBoundingClientRect() {
            return {width: garageWidth, height: garageHeight};
        },
    };
    const context = {
        Math,
        sortDesktopEquipmentList() {},
        document: {
            createElement() {
                throw new Error("empty marker must not be created for a populated rack");
            },
            documentElement: {},
            querySelector() {
                return {};
            },
            querySelectorAll(selector) {
                return selector === "[data-garage-item='truck']" ? [garageTile] : [];
            },
        },
        getComputedStyle() {
            return {
                getPropertyValue(name) {
                    return name === "--gd-truck-slot-w" ? String(garageWidth) : String(garageHeight);
                },
            };
        },
    };
    vm.runInNewContext(`${extractBraceBlock(TEMPLATE_SOURCE, "function refreshComplexTruckRack(rack)")}; this.refreshComplexTruckRack = refreshComplexTruckRack;`, context);
    context.refreshComplexTruckRack(rack);
    return values;
}

test("complex rack fits two rows inside the available card height", () => {
    const values = runRackLayout({tileCount: 7, rackWidth: 520, rackHeight: 100});
    assert.equal(values.get("--complex-truck-cols"), "6");
    assert.equal(values.get("--complex-truck-rows"), "2");
    const height = Number.parseInt(values.get("--complex-truck-h"), 10);
    const gap = Number.parseInt(values.get("--complex-truck-gap"), 10);
    assert.ok((height * 2) + gap <= 100, "two tile rows must stay inside their rack");
});

test("complex rack fits three rows inside the available card height", () => {
    const values = runRackLayout({tileCount: 18, rackWidth: 520, rackHeight: 112});
    assert.equal(values.get("--complex-truck-cols"), "6");
    assert.equal(values.get("--complex-truck-rows"), "3");
    const height = Number.parseInt(values.get("--complex-truck-h"), 10);
    const gap = Number.parseInt(values.get("--complex-truck-gap"), 10);
    assert.ok((height * 3) + (gap * 2) <= 112, "three tile rows must stay inside their rack");
});

test("desktop grid and status chips respond to available width", () => {
    assert.match(CSS_SOURCE, /repeat\(auto-fit,\s*minmax\(min\(460px,\s*100%\),\s*1fr\)\)/);
    assert.match(CSS_SOURCE, /min-width:\s*min\(120px,\s*100%\)/);
    assert.match(CSS_SOURCE, /grid-auto-rows:\s*minmax\(clamp\(190px,\s*21vh,\s*220px\),\s*auto\)/);
    assert.match(CSS_SOURCE, /dispatcher-complex-card \.complex-assigned-trucks[\s\S]*?overflow:\s*hidden/);
    assert.match(CSS_SOURCE, /truck-fill-4\)[\s\S]*?min-height:\s*260px/);
});
