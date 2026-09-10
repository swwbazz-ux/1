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
const shiftScreenSource = fs.readFileSync(
    path.join(backendRoot, "templates", "includes", "mobile_shift_screen.html"),
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

function readingValidator({fuel, hours, startHours, closing}) {
    const context = {
        Number,
        Object,
        shiftFuelInput: {value: String(fuel), dataset: {eoFuelCapacity: "7000"}},
        shiftHoursInput: {
            value: String(hours),
            dataset: {eoStartEngineHours: String(startHours)},
        },
        isShiftCloseAction() { return closing; },
        showShiftErrors() {},
        clearShiftErrors() {},
    };
    const sources = [
        extractBraceBlock(templateSource, "function parseShiftNumber", "number parser"),
        extractBraceBlock(templateSource, "function calculatedShiftFuelLiters", "fuel calculator"),
        extractBraceBlock(templateSource, "function validateShiftReadings", "reading validator"),
    ];
    vm.runInNewContext(`${sources.join("\n")}; this.validate = validateShiftReadings;`, context);
    return context.validate(false);
}

test("closing accepts anomalous whole readings so the server can request confirmation", () => {
    assert.equal(readingValidator({fuel: 80, hours: 1199, startHours: 1200, closing: true}).valid, true);
    assert.equal(readingValidator({fuel: 80, hours: 1213, startHours: 1200, closing: true}).valid, true);
    assert.equal(readingValidator({fuel: 120, hours: 1201, startHours: 1200, closing: true}).valid, true);
});

test("opening remains strict and malformed close values never reach confirmation", () => {
    assert.equal(readingValidator({fuel: 120, hours: 1201, startHours: 1200, closing: false}).valid, false);
    assert.equal(readingValidator({fuel: -1, hours: 1201, startHours: 1200, closing: true}).valid, false);
    assert.equal(readingValidator({fuel: 80.5, hours: 1201, startHours: 1200, closing: true}).valid, false);
    assert.equal(readingValidator({fuel: 80, hours: -1, startHours: 1200, closing: true}).valid, false);
    assert.equal(readingValidator({fuel: 80, hours: 1200.5, startHours: 1200, closing: true}).valid, false);
});

test("the native number input keeps the 100 percent ceiling only while opening", () => {
    assert.match(
        shiftScreenSource,
        /min="0" \{% if mobile_shift_state != "open" %\}max="100" \{% endif %\}name="shift_fuel"/
    );
});

test("confirmation rendering uses text nodes and keeps warnings scrollable", () => {
    assert.match(templateSource, /title\.textContent = String\(warning\.title/);
    assert.match(templateSource, /message\.textContent = String\(warning\.message/);
    assert.doesNotMatch(
        extractBraceBlock(templateSource, "function renderShiftConfirmationWarnings", "warning renderer"),
        /innerHTML/
    );
    assert.match(shiftCss, /\.eo-reading-confirmation__warnings[\s\S]*overflow-y: auto/);
    assert.match(shiftCss, /\.eo-reading-confirmation__actions button[\s\S]*min-height: 50px/);
});

test("warning and confirmed requests keep one shift snapshot and suppress premature success", () => {
    const submitSource = extractBraceBlock(
        templateSource,
        "function submitExcavatorShiftAction",
        "shift submitter"
    );
    assert.match(submitSource, /shift_id: shell\.dataset\.nativeShiftId/);
    assert.match(submitSource, /payload\.confirmation_token = String\(options\.confirmationToken\)/);
    assert.match(submitSource, /options\.expectedActionKey !== shiftActionKey/);
    assert.match(
        submitSource,
        /error\.confirmation_required === true[\s\S]*showShiftConfirmation\(error, shiftActionKey\)[\s\S]*return false/
    );
    assert.match(
        templateSource,
        /shiftConfirmationRequesting = true;[\s\S]*shiftConfirmationAccept\.disabled = true;[\s\S]*confirmationToken: confirmation\.token/
    );
    assert.match(
        templateSource,
        /\.eo-reading-confirmation:not\(\[hidden\]\)/
    );
});

test("editing readings invalidates a previously issued confirmation", () => {
    assert.match(
        templateSource,
        /\[shiftFuelInput, shiftHoursInput\]\.forEach[\s\S]*input\.addEventListener\("input"[\s\S]*hideShiftConfirmation\(\)/
    );
    assert.match(templateSource, /data-eo-reading-confirmation-back/);
    assert.match(templateSource, /data-eo-reading-confirmation-accept/);
});
