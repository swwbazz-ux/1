"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");
const TEMPLATE_SOURCE = fs.readFileSync(
    path.join(BACKEND_ROOT, "templates", "users", "driver_shift.html"),
    "utf8"
);

function extractBraceBlock(source, signature) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${signature} was not found`);
    const open = source.indexOf("{", start + signature.length);
    let depth = 0;
    let quote = "";
    let escaped = false;
    for (let index = open; index < source.length; index += 1) {
        const character = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (character === "\\") escaped = true;
            else if (character === quote) quote = "";
            continue;
        }
        if (character === "\"" || character === "'") {
            quote = character;
            continue;
        }
        if (character === "{") depth += 1;
        if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`${signature} closing brace was not found`);
}

function createClassList(initial = []) {
    const values = new Set(initial);
    return {
        add(...names) { names.forEach((name) => values.add(name)); },
        remove(...names) { names.forEach((name) => values.delete(name)); },
        toggle(name, force) {
            const enabled = typeof force === "boolean" ? force : !values.has(name);
            if (enabled) values.add(name);
            else values.delete(name);
            return enabled;
        },
        contains(name) { return values.has(name); },
    };
}

function createReasonButton(id, seconds = 0) {
    const duration = {hidden: true, textContent: ""};
    const attributes = new Map();
    return {
        dataset: {
            driverDowntimeReasonId: String(id),
            driverReason: `Причина ${id}`,
            driverReasonLabel: `Причина ${id}`,
            driverReasonSeconds: String(seconds),
        },
        classList: createClassList(),
        duration,
        querySelector(selector) {
            return selector === "[data-driver-reason-duration]" ? duration : null;
        },
        getAttribute(name) { return attributes.get(name) || null; },
        setAttribute(name, value) { attributes.set(name, String(value)); },
    };
}

test("per-reason downtime timer survives close and marks every used reason", () => {
    const signatures = [
        "function formatDriverDowntimeDuration(seconds)",
        "function clearDriverDowntimeTimer()",
        "function renderDriverReasonDuration(button, totalSeconds, isActive)",
        "function syncDriverReasonTotals(payload)",
        "function startDriverDowntimeTimer(payload)",
    ];
    const source = signatures.map((signature) => extractBraceBlock(TEMPLATE_SOURCE, signature)).join("\n");
    const first = createReasonButton(1);
    const second = createReasonButton(2);
    const unused = createReasonButton(3);
    const downtimeReasonButtons = [first, second, unused];
    const downtimeDuration = {textContent: ""};
    let now = 1_000_000;
    let scheduledTick = null;
    const runtimeWindow = {
        driverDowntimeTimerId: null,
        clearInterval() { scheduledTick = null; },
        setInterval(callback) {
            scheduledTick = callback;
            return 42;
        },
    };
    class FakeDate extends Date {
        static now() { return now; }
    }
    const context = {start: null, sync: null};
    vm.runInNewContext(
        `${source}\ncontext.start = startDriverDowntimeTimer; context.sync = syncDriverReasonTotals;`,
        {
            context,
            Date: FakeDate,
            Math,
            Number,
            Object,
            String,
            downtimeDuration,
            downtimeReasonButtons,
            window: runtimeWindow,
        },
        {filename: "templates/users/driver_shift.html#driver-downtime-history"}
    );

    context.start({
        active: true,
        reason_id: 2,
        elapsed_seconds: 65,
        reason_totals: {1: 120, 2: 65},
    });
    assert.equal(first.duration.textContent, "00:02:00");
    assert.equal(second.duration.textContent, "00:01:05");
    assert.equal(first.classList.contains("is-used"), true);
    assert.equal(second.classList.contains("is-used"), true);
    assert.equal(unused.classList.contains("is-used"), false);

    now += 2_000;
    scheduledTick();
    assert.equal(second.duration.textContent, "00:01:07");

    context.sync({active: false, reason_totals: {1: 120, 2: 67}});
    assert.equal(first.classList.contains("is-used"), true);
    assert.equal(second.classList.contains("is-used"), true);
    assert.equal(second.duration.textContent, "00:01:07");
    assert.equal(unused.duration.hidden, true);
});

test("trip and downtime report cards scroll vertically with readable rows", () => {
    assert.match(TEMPLATE_SOURCE, /data-driver-report-trip-scroll/);
    assert.match(TEMPLATE_SOURCE, /data-driver-report-downtime-scroll/);
    assert.match(
        TEMPLATE_SOURCE,
        /\.driver-report-section-scroll\s*\{[\s\S]*?overflow-x:\s*hidden;[\s\S]*?overflow-y:\s*auto;/
    );
    assert.match(
        TEMPLATE_SOURCE,
        /\.driver-report-section-scroll \.driver-report-row\s*\{[\s\S]*?min-height:\s*clamp\(34px,\s*4\.8dvh,\s*42px\);/
    );
});

test("Shift, Downtimes and Waybill share the same bottom action rails", () => {
    assert.equal(
        (TEMPLATE_SOURCE.match(/--driver-nav-content-h:\s*var\(--mobile-shift-role-nav-h,\s*78px\)/g) || []).length,
        2,
        "Driver content reserve must use the same adaptive height as the visible navigation"
    );
    assert.match(
        TEMPLATE_SOURCE,
        /\.mobile-shift\[data-mobile-shift-role="driver"\]\s*\{[\s\S]*?inset:\s*calc\(var\(--driver-safe-top\) \+ var\(--driver-header-h\) \+ var\(--driver-work-gap\)\)[\s\S]*?var\(--driver-edge\)[\s\S]*?calc\(var\(--driver-safe-bottom\) \+ var\(--driver-nav-content-h\)\)[\s\S]*?padding:\s*0 0 var\(--driver-footer-action-gap\)/
    );
    assert.match(
        TEMPLATE_SOURCE,
        /\.mobile-shift__actions,[\s\S]*?\.driver-downtime-close,[\s\S]*?\.driver-report-actions\s*\{[\s\S]*?height:\s*var\(--driver-footer-action-h\)/
    );
    assert.match(
        TEMPLATE_SOURCE,
        /\[data-driver-tab-panel="shift"\]\.is-active,[\s\S]*?\[data-driver-tab-panel="downtimes"\]\.is-active,[\s\S]*?\[data-driver-tab-panel="manifest"\]\.is-active\s*\{[\s\S]*?height:\s*calc\(100% \+ var\(--driver-work-gap\)\)[\s\S]*?max-height:\s*calc\(100% \+ var\(--driver-work-gap\)\)/
    );
});

test("short landscape keeps two-line downtime reasons readable and vertically scrollable", () => {
    assert.match(
        TEMPLATE_SOURCE,
        /\.driver-downtime-list button\s*\{[\s\S]*?-webkit-text-size-adjust:\s*none;[\s\S]*?text-size-adjust:\s*none;/
    );
    assert.match(
        TEMPLATE_SOURCE,
        /@media \(orientation:\s*landscape\) and \(max-height:\s*520px\)\s*\{[\s\S]*?\.driver-downtime-list\s*\{[\s\S]*?grid-auto-rows:\s*52px\s*!important;[\s\S]*?overflow-x:\s*hidden;[\s\S]*?overflow-y:\s*auto;/
    );
    assert.match(
        TEMPLATE_SOURCE,
        /\.driver-downtime-reason-duration\s*\{[\s\S]*?font-size:[^;]+!important;/
    );
});
