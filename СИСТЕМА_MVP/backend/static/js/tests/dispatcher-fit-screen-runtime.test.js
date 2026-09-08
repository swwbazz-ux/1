"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const TEMPLATE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "dispatcher_control.html"),
    "utf8"
);
const CSS = fs.readFileSync(
    path.join(BACKEND, "static", "css", "dispatcher-control-v1.css"),
    "utf8"
);

function fit(width, height) {
    const calculatedZoom = Math.min(width / 2400, height / 1500);
    const zoom = Math.max(0.65, calculatedZoom);
    return {
        zoom,
        minimum: calculatedZoom < 0.65,
        visibleHeight: calculatedZoom < 0.65
            ? Math.min(1500, height / zoom)
            : 1500,
    };
}

test("dispatcher desktop uses a fixed 2400 by 1500 CSS canvas", () => {
    assert.match(TEMPLATE, /data-dispatcher-fit-canvas/);
    assert.match(TEMPLATE, /var DESIGN_WIDTH = 2400;/);
    assert.match(TEMPLATE, /var DESIGN_HEIGHT = 1500;/);
    assert.match(TEMPLATE, /var MIN_ZOOM = 0\.65;/);
    assert.match(CSS, /width: 2400px;\s+min-width: 2400px;\s+max-width: 2400px;/);
    assert.match(CSS, /--dispatcher-fit-visible-height: 1500px;/);
});

test("fit is driven by a document ResizeObserver and CSS zoom", () => {
    assert.match(TEMPLATE, /var root = document\.documentElement;/);
    assert.match(TEMPLATE, /new ResizeObserver/);
    assert.match(TEMPLATE, /resizeObserver\.observe\(root\)/);
    assert.match(TEMPLATE, /shell\.style\.zoom = String\(zoom\);/);
    assert.doesNotMatch(TEMPLATE, /window\.addEventListener\("resize", fitDispatcherShellToScreen\)/);
    assert.doesNotMatch(TEMPLATE, /shell\.style\.transform = "scale\(/);
});

test("target viewports calculate the requested zoom values", () => {
    assert.deepEqual(fit(1920, 1200), {
        zoom: 0.8,
        minimum: false,
        visibleHeight: 1500,
    });
    assert.equal(fit(1920, 1080).zoom, 0.72);
    assert.equal(fit(1600, 900).zoom, 0.65);
    assert.equal(fit(1600, 900).minimum, true);
    assert.equal(fit(1600, 900).visibleHeight, 900 / 0.65);
});

test("only the complexes grid receives fallback scrolling at minimum zoom", () => {
    assert.match(
        CSS,
        /\.dispatcher-shell\.is-fit-minimum \.dispatcher-zone-grid\s*\{[^}]*overflow-x: hidden;[^}]*overflow-y: auto;/s
    );
    assert.match(CSS, /body\.dispatcher-fit-screen\s*\{[^}]*overflow: hidden;/s);
    assert.match(
        CSS,
        /body\.dispatcher-fit-screen \.dispatcher-right\s*\{[^}]*grid-column: 3;[^}]*display: block;/s
    );
});
