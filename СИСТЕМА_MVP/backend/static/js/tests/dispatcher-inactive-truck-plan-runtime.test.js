const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const TEMPLATE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "includes", "dispatcher_board.html"),
    "utf8"
);
const CSS = fs.readFileSync(
    path.join(BACKEND, "static", "css", "dispatcher-detail-overrides-v1.css"),
    "utf8"
);

test("inactive truck plan fill uses the neutral desktop presentation", () => {
    assert.match(
        TEMPLATE,
        /dispatcher-truck-tile complex-truck-tile[^\n]+\{% if truck\.plan_inactive_fill %\} is-inactive-plan-fill\{% endif %\}/
    );
    assert.match(
        CSS,
        /\.dispatcher-truck-tile\.is-inactive-plan-fill\[data-plan-progress-phase\]:not\(\[data-plan-progress-phase=""\]\)[^}]+--dispatcher-plan-color:\s*rgba\(139, 159, 170,/s
    );
    assert.match(
        CSS,
        /\.dispatcher-complex-card \.complex-assigned-trucks\s*\{[^}]+z-index:\s*5;/s
    );
    assert.doesNotMatch(
        CSS,
        /\.dispatcher-complex-card \.complex-assigned-trucks\s*\{[^}]*position:\s*relative;/s,
        "foreground layering must not reactivate the inherited bottom offset"
    );
    assert.match(
        CSS,
        /\.dispatcher-complex-card \.complex-truck-tile\.is-inactive-plan-fill\s*\{[^}]+background-color:\s*var\(--gd-panel\);[^}]+isolation:\s*isolate;/s
    );
    assert.match(
        CSS,
        /\.dispatcher-complex-card \.complex-truck-tile\.is-inactive-plan-fill img\s*\{[^}]+z-index:\s*4;[^}]+opacity:\s*1;[^}]+filter:\s*grayscale\(1\)/s
    );
});
