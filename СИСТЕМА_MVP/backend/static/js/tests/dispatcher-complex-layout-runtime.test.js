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
const DISPATCHER_SCRIPT = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-control-v1.js"),
    "utf8"
);
const LAYOUT_CSS = CSS.slice(CSS.indexOf("/* Карточка комплекса: три независимые строки"));

function fitTruckRack(rackWidth, count) {
    const gap = 6;
    const naturalTile = 92;
    const naturalColumns = Math.max(1, Math.floor((rackWidth + gap) / (naturalTile + gap)));
    const targetColumns = Math.max(1, Math.min(7, count || naturalColumns));
    const needsCompactTiles = count > naturalColumns;
    const fittedTile = Math.floor((rackWidth - (gap * (targetColumns - 1))) / targetColumns);
    const tile = needsCompactTiles
        ? Math.max(44, Math.min(naturalTile - 4, fittedTile))
        : Math.max(44, Math.min(naturalTile, fittedTile));
    const columns = Math.max(1, Math.floor((rackWidth + gap) / (tile + gap)));
    return { tile, rows: count ? Math.ceil(count / columns) : 0 };
}

test("complex card has three direct layout rows", () => {
    assert.match(
        TEMPLATE,
        /\{% if mining_master_mobile_enabled %\}\s*<div class="complex-work-head">/
    );
    assert.match(
        LAYOUT_CSS,
        /> \.complex-work-head\s*\{\s*display: contents;/
    );
    assert.match(LAYOUT_CSS, /grid-template-rows:\s*auto auto minmax\(min-content, 1fr\)/);
    assert.match(LAYOUT_CSS, /\.complex-title-state\s*\{[\s\S]*?grid-row:\s*1/);
    assert.match(LAYOUT_CSS, /\.complex-context\s*\{[\s\S]*?grid-row:\s*2/);
    assert.match(LAYOUT_CSS, /> \.complex-assigned-trucks\s*\{[\s\S]*?grid-row:\s*3/);
});

test("truck rack grows by content and never clips its tiles", () => {
    assert.match(LAYOUT_CSS, /grid-template-columns:\s*repeat\(auto-fill, minmax\(var\(--tile\), 1fr\)\)/);
    assert.match(LAYOUT_CSS, /grid-auto-rows:\s*max-content/);
    assert.match(LAYOUT_CSS, /align-content:\s*start/);
    assert.match(LAYOUT_CSS, /> \.complex-assigned-trucks\s*\{[\s\S]*?overflow:\s*visible/);
    assert.match(LAYOUT_CSS, /\.dispatcher-complex-card:not\(\.status-empty\)[\s\S]*?height:\s*auto/);
    assert.match(LAYOUT_CSS, /\.dispatcher-complex-card:not\(\.status-empty\)[\s\S]*?overflow:\s*visible/);
    assert.match(TEMPLATE, /data-truck-tile/);
});

test("rack reduces the tile before creating a clipped second row", () => {
    assert.deepEqual(fitTruckRack(620, 7), { tile: 83, rows: 1 });
    assert.deepEqual(fitTruckRack(620, 12), { tile: 83, rows: 2 });
    assert.equal(fitTruckRack(150, 7).tile, 44);
    assert.match(DISPATCHER_SCRIPT, /rack\.style\.setProperty\("--tile", tile \+ "px"\)/);
    assert.match(DISPATCHER_SCRIPT, /tile\.setAttribute\("data-truck-tile", ""\)/);
    assert.match(DISPATCHER_SCRIPT, /tile\.removeAttribute\("data-truck-tile"\)/);
});

test("card badges keep whole words on one line and wrap as units", () => {
    assert.match(TEMPLATE, /data-card-single-line-badge/);
    assert.match(LAYOUT_CSS, /\.complex-title-state[\s\S]*?flex-wrap:\s*wrap/);
    assert.match(LAYOUT_CSS, /\.complex-state-chip,[\s\S]*?flex:\s*0 0 auto/);
    assert.match(LAYOUT_CSS, /word-break:\s*normal/);
    assert.match(LAYOUT_CSS, /white-space:\s*nowrap/);
});
