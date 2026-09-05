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


function loadWaitingModeRuntime({hasTrip = true} = {}) {
    const source = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function applyDriverWaitingMode(payload)",
        "Driver waiting-operation UI helper"
    );
    const dial = {classList: createClassList()};
    const note = {textContent: hasTrip ? "ТОЧКА РАЗГРУЗКИ" : "НА ЗАГРУЗКУ"};
    const holdForm = hasTrip ? {dataset: {driverUnloadOneTap: "false"}} : null;
    const workDialControl = {
        classList: createClassList([hasTrip ? "is-loaded" : "is-empty"]),
        querySelector(selector) {
            return selector === ".driver-work-note" ? note : null;
        },
    };
    const context = {apply: null};

    vm.runInNewContext(
        `${source}\ncontext.apply = applyDriverWaitingMode;`,
        {String, context, holdForm, workDial: dial, workDialControl},
        {filename: "templates/users/driver_shift.html#waiting-operation-mode"}
    );
    assert.equal(typeof context.apply, "function");
    return {apply: context.apply, dial, holdForm, note, workDialControl};
}


function createEventTarget(properties = {}) {
    const listeners = new Map();
    return Object.assign({
        classList: createClassList(),
        addEventListener(type, listener) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(listener);
        },
        removeEventListener(type, listener) {
            const entries = listeners.get(type) || [];
            listeners.set(type, entries.filter((entry) => entry !== listener));
        },
        dispatch(type, properties = {}) {
            const event = Object.assign({
                type,
                defaultPrevented: false,
                preventDefault() {
                    this.defaultPrevented = true;
                },
            }, properties);
            for (const listener of [...(listeners.get(type) || [])]) {
                listener(event);
            }
            return event;
        },
        listenerCount(type) {
            return (listeners.get(type) || []).length;
        },
    }, properties);
}


function loadUnloadGestureBinder() {
    const source = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "window.bindDriverUnloadGesture = function (options)",
        "Driver unload gesture binder"
    );
    const context = {bind: null};
    const runtimeWindow = {};

    vm.runInNewContext(
        `${source};\ncontext.bind = window.bindDriverUnloadGesture;`,
        {context, window: runtimeWindow},
        {filename: "templates/users/driver_shift.html#unload-gesture"}
    );
    assert.equal(typeof context.bind, "function");
    return context.bind;
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
        DRIVER_WORKFLOW_SOURCE,
        /DRIVER_DOWNTIME_FLOW_WAITING_LOADING\s*=\s*["']waiting_loading["']/
    );
    assert.match(
        DRIVER_WORKFLOW_SOURCE,
        /DRIVER_DOWNTIME_WORK_FLOWS\s*=\s*frozenset\([\s\S]*DRIVER_DOWNTIME_FLOW_WAITING_LOADING[\s\S]*DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD/
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


test("waiting_unload enables one-tap generic yellow mode and clearing removes it", () => {
    const runtime = loadWaitingModeRuntime({hasTrip: true});

    assert.equal(runtime.apply({
        workflow: "waiting_unload",
        reason_label: "Ожидание ККД",
    }), true);
    assert.equal(runtime.holdForm.dataset.driverUnloadOneTap, "true");
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-operation"), true);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-unload"), true);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-loading"), false);
    assert.equal(runtime.dial.classList.contains("is-waiting-operation"), true);
    assert.equal(runtime.dial.classList.contains("is-waiting-unload"), true);
    assert.equal(runtime.note.textContent, "ОЖИДАНИЕ ККД");

    assert.equal(runtime.apply({workflow: ""}), false);
    assert.equal(runtime.holdForm.dataset.driverUnloadOneTap, "false");
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-operation"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-loading"), false);
    assert.equal(runtime.dial.classList.contains("is-waiting-operation"), false);
    assert.equal(runtime.dial.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.note.textContent, "ТОЧКА РАЗГРУЗКИ");

    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /\.driver-work-dial\.is-waiting-operation,[\s\S]*\.driver-work-dial-button\.is-waiting-operation\s*\{[\s\S]*?--driver-green:\s*#facc15/
    );
});


test("waiting_loading turns the empty Work dial yellow but keeps it inert", () => {
    const runtime = loadWaitingModeRuntime({hasTrip: false});

    assert.equal(runtime.apply({
        workflow: "waiting_loading",
        reason: "Ожидание погрузки",
    }), true);
    assert.equal(runtime.holdForm, null);
    assert.equal(runtime.dial.classList.contains("is-waiting-operation"), true);
    assert.equal(runtime.dial.classList.contains("is-waiting-loading"), true);
    assert.equal(runtime.dial.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-operation"), true);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-loading"), true);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-empty"), true);
    assert.equal(runtime.note.textContent, "ОЖИДАНИЕ ПОГРУЗКИ");

    assert.equal(runtime.apply({workflow: ""}), false);
    assert.equal(runtime.dial.classList.contains("is-waiting-operation"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-operation"), false);
    assert.equal(runtime.note.textContent, "НА ЗАГРУЗКУ");
});


test("one-tap pointerup submits once without relying on a synthetic click", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "true", holdComplete: "false"},
    });
    const captured = [];
    const button = createEventTarget({
        disabled: false,
        setPointerCapture(pointerId) {
            captured.push(pointerId);
        },
    });
    const holdCalls = {start: 0, reset: 0, cancel: 0};
    const holdGuard = {
        start() { holdCalls.start += 1; },
        reset() { holdCalls.reset += 1; },
        cancel() { holdCalls.cancel += 1; },
    };
    let submissions = 0;

    bind({
        form,
        button,
        holdGuard,
        canTrigger() { return true; },
        onOneTap() {
            submissions += 1;
            button.disabled = true;
            return true;
        },
    });

    const pointerDown = button.dispatch("pointerdown", {pointerId: 41});
    assert.equal(button.classList.contains("is-touch-armed"), true);
    button.dispatch("pointerleave", {pointerId: 41});
    assert.equal(
        button.classList.contains("is-touch-armed"),
        true,
        "a slight move outside the captured control must not disarm one-tap unload"
    );
    const pointerUp = button.dispatch("pointerup", {pointerId: 41});
    assert.equal(pointerDown.defaultPrevented, true);
    assert.equal(pointerUp.defaultPrevented, true);
    assert.deepEqual(captured, [41]);
    assert.equal(submissions, 1, "pointerup must submit even when no click event follows");
    assert.equal(button.classList.contains("is-touch-armed"), false);
    assert.equal(holdCalls.start, 0, "one-tap unload must not start the hold timer");

    const lateSyntheticClick = button.dispatch("click");
    assert.equal(lateSyntheticClick.defaultPrevented, true);
    assert.equal(submissions, 1, "the click fallback must not duplicate the pointerup request");
});


test("pointercancel disarms one-tap unload without submitting", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "true", holdComplete: "false"},
    });
    const button = createEventTarget({disabled: false, setPointerCapture() {}});
    let submissions = 0;

    bind({
        form,
        button,
        holdGuard: {start() {}, reset() {}, cancel() {}},
        onOneTap() {
            submissions += 1;
            return true;
        },
    });

    button.dispatch("pointerdown", {pointerId: 51});
    assert.equal(button.classList.contains("is-touch-armed"), true);
    button.dispatch("pointercancel", {pointerId: 51});
    assert.equal(button.classList.contains("is-touch-armed"), false);
    button.dispatch("pointerup", {pointerId: 51});
    assert.equal(submissions, 0);
});


test("keyboard click remains a one-tap fallback", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "true", holdComplete: "false"},
    });
    const button = createEventTarget({disabled: false});
    const holdGuard = {start() {}, reset() {}, cancel() {}};
    let submissions = 0;

    bind({
        form,
        button,
        holdGuard,
        onOneTap() {
            submissions += 1;
            button.disabled = true;
            return true;
        },
    });

    const click = button.dispatch("click", {detail: 0});
    assert.equal(click.defaultPrevented, true);
    assert.equal(submissions, 1);
});


test("normal unload still uses the hold guard", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "false", holdComplete: "false"},
    });
    const button = createEventTarget({disabled: false, setPointerCapture() {}});
    const holdCalls = {start: 0, reset: 0, cancel: 0};
    const holdGuard = {
        start() { holdCalls.start += 1; },
        reset() { holdCalls.reset += 1; },
        cancel() { holdCalls.cancel += 1; },
    };
    let submissions = 0;

    bind({
        form,
        button,
        holdGuard,
        onOneTap() { submissions += 1; },
    });

    button.dispatch("pointerdown", {pointerId: 7});
    button.dispatch("pointerup", {pointerId: 7});
    assert.equal(holdCalls.start, 1);
    assert.equal(holdCalls.reset, 1);
    assert.equal(submissions, 0);
});


test("fragment-style destroy and rebind leaves exactly one gesture listener", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "true", holdComplete: "false"},
    });
    const button = createEventTarget({disabled: false, setPointerCapture() {}});
    const holdGuard = {start() {}, reset() {}, cancel() {}};
    let submissions = 0;
    const options = {
        form,
        button,
        holdGuard,
        onOneTap() {
            submissions += 1;
            button.disabled = true;
            return true;
        },
    };

    const initialBinding = bind(options);
    assert.equal(button.listenerCount("pointerup"), 1);
    button.dispatch("pointerdown", {pointerId: 8});
    assert.equal(button.classList.contains("is-touch-armed"), true);
    initialBinding.destroy();
    assert.equal(button.classList.contains("is-touch-armed"), false);
    button.disabled = false;
    const rebound = bind(options);
    assert.ok(rebound);
    assert.equal(button.listenerCount("pointerup"), 1);

    button.dispatch("pointerdown", {pointerId: 9});
    button.dispatch("pointerup", {pointerId: 9});
    assert.equal(submissions, 1);
});


test("an armed one-tap gesture blocks operational fragment replacement", () => {
    const unsafeSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function isDriverOperationalRefreshUnsafe(shell)",
        "Driver unsafe-refresh guard"
    );
    const context = {isUnsafe: null};
    let touchArmed = true;
    const unsafeSelector = (
        ".is-touch-armed, .is-holding, .is-pending, .is-dragging, "
        + "[data-driver-point-sheet]:not([hidden])"
    );
    const shell = {
        contains() { return false; },
        querySelector(selector) {
            if (selector === unsafeSelector) return touchArmed ? {} : null;
            return null;
        },
    };
    const document = {
        activeElement: null,
        querySelector() { return null; },
    };

    vm.runInNewContext(
        `${unsafeSource}\ncontext.isUnsafe = isDriverOperationalRefreshUnsafe;`,
        {context, document},
        {filename: "templates/users/driver_shift.html#unsafe-refresh-guard"}
    );

    assert.match(
        unsafeSource,
        /shell\.querySelector\(["']\.is-touch-armed,\s*\.is-holding,\s*\.is-pending,\s*\.is-dragging,\s*\[data-driver-point-sheet\]:not\(\[hidden\]\)["']\)/,
        "The static refresh guard must include the armed touch state."
    );
    assert.equal(context.isUnsafe(shell), true);
    touchArmed = false;
    assert.equal(context.isUnsafe(shell), false);
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


test("reduced motion keeps both waiting states but disables their pulse", () => {
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
            block.includes(".driver-work-dial.is-waiting-operation::before")
            && block.includes(".driver-work-dial-button.is-waiting-operation .driver-work-dial-core")
            && /animation:\s*none/.test(block)
        )),
        "The waiting-operation pulse must stop when reduced motion is requested."
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /\.driver-work-dial\.is-waiting-operation,[\s\S]*\.driver-work-dial-button\.is-waiting-operation\s*\{[\s\S]*?--driver-green:\s*#facc15/,
        "Reduced motion must not remove the static yellow waiting state."
    );
});


test("Driver JavaScript branches on workflow instead of an exact Russian reason label", () => {
    const waitingModeSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function applyDriverWaitingMode(payload)",
        "Driver waiting-operation UI helper"
    );
    assert.match(
        waitingModeSource,
        /flow\s*===\s*["']waiting_loading["']/
    );
    assert.match(
        waitingModeSource,
        /flow\s*===\s*["']waiting_unload["']/
    );
    assert.doesNotMatch(
        waitingModeSource,
        /String\s*\(\s*payload\.reason[\s\S]{0,160}(?:===|==)[\s\S]{0,80}["']ожидание разгрузки["']/i
    );
    assert.doesNotMatch(
        waitingModeSource,
        /["']ожидание разгрузки["'][\s\S]{0,80}(?:===|==)[\s\S]{0,160}payload\.reason/i
    );
});
