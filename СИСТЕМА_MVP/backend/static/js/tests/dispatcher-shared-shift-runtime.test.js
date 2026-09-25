"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const backendRoot = path.resolve(__dirname, "../../..");
const shiftActionTemplate = fs.readFileSync(
    path.join(backendRoot, "templates/includes/dispatcher_shift_action.html"),
    "utf8",
);
const realtimeSource = fs.readFileSync(
    path.join(backendRoot, "static/js/dispatcher-realtime-v1.js"),
    "utf8",
);

test("shared shift dialog exposes an idempotent rebind entrypoint", () => {
    assert.match(shiftActionTemplate, /global\.initSharedShiftLogin = initSharedShiftLogin/);
    assert.match(shiftActionTemplate, /dataset\.sharedShiftLoginBound/);
    assert.match(shiftActionTemplate, /querySelectorAll\("\[data-shared-shift-login-cancel\]"\)/);
    assert.match(shiftActionTemplate, /initSharedShiftLogin\(document\)/);
});

test("dispatcher realtime rebinds the shared shift dialog after DOM reconciliation", () => {
    assert.match(
        realtimeSource,
        /typeof hostWindow\.initSharedShiftLogin === "function"[\s\S]+hostWindow\.initSharedShiftLogin\(document\)/,
    );
});
