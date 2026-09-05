"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");
const DRIVER_TEMPLATE_SOURCE = fs.readFileSync(
    path.join(BACKEND_ROOT, "templates", "users", "driver_shift.html"),
    "utf8"
);
const DRIVER_VIEWS_SOURCE = fs.readFileSync(
    path.join(BACKEND_ROOT, "users", "views.py"),
    "utf8"
);
const DRIVER_WORKFLOW_SOURCE = fs.readFileSync(
    path.join(BACKEND_ROOT, "downtimes", "driver_workflow.py"),
    "utf8"
);


function extractBraceBlock(source, signature, label, fromIndex = 0) {
    const start = source.indexOf(signature, fromIndex);
    assert.notEqual(start, -1, `${label} signature was not found.`);
    const open = source.indexOf("{", start + signature.length);
    assert.notEqual(open, -1, `${label} opening brace was not found.`);

    let depth = 0;
    let quote = "";
    let escaped = false;
    let lineComment = false;
    let blockComment = false;

    for (let index = open; index < source.length; index += 1) {
        const character = source[index];
        const next = source[index + 1] || "";

        if (lineComment) {
            if (character === "\n") lineComment = false;
            continue;
        }
        if (blockComment) {
            if (character === "*" && next === "/") {
                blockComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (escaped) {
                escaped = false;
            } else if (character === "\\") {
                escaped = true;
            } else if (character === quote) {
                quote = "";
            }
            continue;
        }
        if (character === "/" && next === "/") {
            lineComment = true;
            index += 1;
            continue;
        }
        if (character === "/" && next === "*") {
            blockComment = true;
            index += 1;
            continue;
        }
        if (character === "'" || character === '"' || character === "`") {
            quote = character;
            continue;
        }
        if (character === "{") {
            depth += 1;
        } else if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`${label} closing brace was not found.`);
}


function createClassList(initial = []) {
    const values = new Set(initial);
    return {
        add(...names) {
            names.forEach((name) => values.add(name));
        },
        remove(...names) {
            names.forEach((name) => values.delete(name));
        },
        toggle(name, force) {
            const enabled = typeof force === "boolean" ? force : !values.has(name);
            if (enabled) values.add(name);
            else values.delete(name);
            return enabled;
        },
        contains(name) {
            return values.has(name);
        },
    };
}


function loadUnloadingWaitModeRuntime() {
    const source = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function applyDriverUnloadingWaitMode(payload)",
        "Driver unloading-wait UI helper"
    );
    const dial = {classList: createClassList()};
    const note = {textContent: "ТОЧКА РАЗГРУЗКИ"};
    const holdForm = {dataset: {driverUnloadOneTap: "false"}};
    const holdButton = {
        classList: createClassList(["is-loaded"]),
        closest(selector) {
            return selector === ".driver-work-dial" ? dial : null;
        },
        querySelector(selector) {
            return selector === ".driver-work-note" ? note : null;
        },
    };
    const context = {apply: null};

    vm.runInNewContext(
        `${source}\ncontext.apply = applyDriverUnloadingWaitMode;`,
        {Boolean, String, context, holdButton, holdForm},
        {filename: "templates/users/driver_shift.html#unloading-wait-mode"}
    );
    assert.equal(typeof context.apply, "function");
    return {apply: context.apply, dial, holdButton, holdForm, note};
}


test("all three unloading waits use one semantic workflow and template availability contract", () => {
    const tuple = DRIVER_WORKFLOW_SOURCE.match(
        /TRUCK_UNLOADING_WAIT_REASON_NAMES\s*=\s*\(([\s\S]*?)\)\s*\n/
    );
    assert.ok(tuple, "The canonical unloading-wait tuple must be declared.");
    const reasonNames = Array.from(
        tuple[1].matchAll(/["']([^"']+)["']/g),
        (match) => match[1]
    );
    assert.deepEqual(reasonNames, [
        "Ожидание разгрузки",
        "Ожидание разгрузки ККД",
        "Ожидание разгрузки СКДР",
    ]);
    assert.match(
        DRIVER_WORKFLOW_SOURCE,
        /DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD\s*=\s*["']waiting_unload["']/
    );
    assert.match(
        DRIVER_VIEWS_SOURCE,
        /for reason in downtime_reasons:[\s\S]*reason\.driver_workflow\s*=\s*driver_downtime_flow\(reason\)[\s\S]*reason\.driver_requires_loaded_trip\s*=\s*driver_downtime_requires_loaded_trip\(reason\)/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /data-driver-downtime-flow="\{\{ reason\.driver_workflow \}\}"/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /reason\.driver_unavailable_message/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /data-driver-unavailable-message="\{\{ reason\.driver_unavailable_message \}\}"/
    );
    assert.match(
        DRIVER_VIEWS_SOURCE,
        /reason\.driver_requires_loaded_trip and not driver_has_loaded_trip:[\s\S]*Доступно только после погрузки/
    );
    assert.match(
        DRIVER_VIEWS_SOURCE,
        /reason\.driver_requires_empty_truck and driver_has_open_trip:[\s\S]*Самосвал уже загружен/
    );
});


test("waiting_unload enables one-tap yellow mode and clearing removes it", () => {
    const runtime = loadUnloadingWaitModeRuntime();

    assert.equal(runtime.apply({
        workflow: "waiting_unload",
        reason_label: "Ожидание ККД",
    }), true);
    assert.equal(runtime.holdForm.dataset.driverUnloadOneTap, "true");
    assert.equal(runtime.holdButton.classList.contains("is-waiting-unload"), true);
    assert.equal(runtime.dial.classList.contains("is-waiting-unload"), true);
    assert.equal(runtime.note.textContent, "ОЖИДАНИЕ ККД");

    assert.equal(runtime.apply({workflow: ""}), false);
    assert.equal(runtime.holdForm.dataset.driverUnloadOneTap, "false");
    assert.equal(runtime.holdButton.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.dial.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.note.textContent, "ТОЧКА РАЗГРУЗКИ");

    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /\.driver-work-dial(?:-button)?\.is-waiting-unload[\s\S]*--driver-green:\s*#facc15/
    );
});


test("realtime waiting_loading to loaded-trip transition opens Work from server truth", async () => {
    const tabSyncSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function syncDriverTabMarkup(shell, tab)",
        "Driver tab markup synchronizer"
    );
    const refreshSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "window.applyOperationalStateRefresh = function (context)",
        "Driver operational refresh"
    );
    let replaced = false;
    let rebound = 0;
    const previousDowntimeCard = {
        dataset: {driverActiveDowntimeFlow: "waiting_loading"},
    };
    const freshWorkPanel = {
        dataset: {driverTabPanel: "work"},
        classList: createClassList(),
    };
    const freshDowntimesPanel = {
        dataset: {driverTabPanel: "downtimes"},
        classList: createClassList(["is-active"]),
    };
    const persistentWorkTab = {
        dataset: {driverTabOpen: "work"},
        classList: createClassList(),
    };
    const persistentDowntimesTab = {
        dataset: {driverTabOpen: "downtimes"},
        classList: createClassList(["is-active"]),
    };
    const oldShell = {
        dataset: {activeTab: "downtimes"},
        querySelector(selector) {
            if (selector === "[data-driver-active-downtime-flow]") {
                return previousDowntimeCard;
            }
            return null;
        },
        replaceWith(node) {
            replaced = node === freshShell;
        },
    };
    const freshShell = {
        dataset: {activeTab: "downtimes", driverHasLoadedTrip: "true"},
        querySelector(selector) {
            if (selector === '[data-driver-tab-panel="work"]') return freshWorkPanel;
            if (selector === '[data-driver-tab-panel="downtimes"]') return freshDowntimesPanel;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-driver-tab-panel]") {
                return [freshWorkPanel, freshDowntimesPanel];
            }
            return [];
        },
    };
    const document = {
        body: {dataset: {}},
        querySelector(selector) {
            return selector === "[data-driver-shell]" ? oldShell : null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-driver-tab-open]") {
                return [persistentWorkTab, persistentDowntimesTab];
            }
            return [];
        },
    };
    const runtimeWindow = {
        AppOperationalFragment: {
            request() {
                return Promise.resolve({html: "<main data-driver-shell></main>"});
            },
            parseRoot() {
                return freshShell;
            },
        },
        bindDriverMobileShell() {
            rebound += 1;
        },
    };
    const context = {refresh: null};

    vm.runInNewContext(
        `${tabSyncSource}\n${refreshSource};\ncontext.refresh = window.applyOperationalStateRefresh;`,
        {
            context,
            document,
            isDriverOperationalRefreshUnsafe() {
                return false;
            },
            playDriverAssignmentAlert() {},
            window: runtimeWindow,
        },
        {filename: "templates/users/driver_shift.html#operational-refresh"}
    );

    assert.equal(await context.refresh({version: 81234}), true);
    assert.equal(replaced, true);
    assert.equal(freshShell.dataset.activeTab, "work");
    assert.equal(
        freshWorkPanel.classList.contains("is-active"),
        true,
        "The Work panel itself must become visible, not only the shell dataset."
    );
    assert.equal(freshDowntimesPanel.classList.contains("is-active"), false);
    assert.equal(persistentWorkTab.classList.contains("is-active"), true);
    assert.equal(persistentDowntimesTab.classList.contains("is-active"), false);
    assert.equal(document.body.dataset.operationalStateVersion, "81234");
    assert.equal(rebound, 1);
});


test("reduced motion keeps unloading-wait state but disables its pulse", () => {
    const reducedMotionBlocks = [];
    let offset = 0;
    while (true) {
        const start = DRIVER_TEMPLATE_SOURCE.indexOf(
            "@media (prefers-reduced-motion: reduce)",
            offset
        );
        if (start === -1) break;
        const block = extractBraceBlock(
            DRIVER_TEMPLATE_SOURCE,
            "@media (prefers-reduced-motion: reduce)",
            "Driver reduced-motion CSS",
            start
        );
        reducedMotionBlocks.push(block);
        offset = start + block.length;
    }
    assert.ok(reducedMotionBlocks.length >= 1);
    assert.ok(
        reducedMotionBlocks.some((block) => (
            block.includes(".driver-work-dial.is-waiting-unload::before")
            && block.includes(".driver-work-dial-button.is-waiting-unload .driver-work-dial-core")
            && /animation:\s*none/.test(block)
        )),
        "The unloading-wait pulse must stop when reduced motion is requested."
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /\.driver-work-dial(?:-button)?\.is-waiting-unload[\s\S]*--driver-green:\s*#facc15/,
        "Reduced motion must not remove the static yellow waiting state."
    );
});


test("Driver JavaScript branches on workflow instead of an exact Russian reason label", () => {
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /payload\s*&&\s*payload\.workflow\s*===\s*["']waiting_unload["']/
    );
    assert.doesNotMatch(
        DRIVER_TEMPLATE_SOURCE,
        /String\s*\(\s*payload\.reason[\s\S]{0,160}(?:===|==)[\s\S]{0,80}["']ожидание разгрузки["']/i
    );
    assert.doesNotMatch(
        DRIVER_TEMPLATE_SOURCE,
        /["']ожидание разгрузки["'][\s\S]{0,80}(?:===|==)[\s\S]{0,160}payload\.reason/i
    );
});
