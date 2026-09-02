"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const DRIVER_TEMPLATE_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "..", "templates", "users", "driver_shift.html"),
    "utf8"
);
const MOBILE_SHIFT_HOLD_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "mobile-shift-unified-v1.js"),
    "utf8"
);


function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
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
            continue;
        }
        if (character === "}") {
            depth -= 1;
            if (depth === 0) {
                return source.slice(start, index + 1);
            }
        }
    }
    assert.fail(`${label} closing brace was not found.`);
}


class FakeClassList {
    constructor() {
        this.values = new Set();
    }

    add(...names) {
        names.forEach((name) => this.values.add(name));
    }

    remove(...names) {
        names.forEach((name) => this.values.delete(name));
    }

    contains(name) {
        return this.values.has(name);
    }
}


function createInput(initialValue = "") {
    const listeners = new Map();
    return {
        value: initialValue,
        defaultValue: initialValue,
        tagName: "INPUT",
        isContentEditable: false,
        addEventListener(type, callback) {
            listeners.set(type, callback);
        },
        dispatch(type) {
            const callback = listeners.get(type);
            if (callback) callback({type, target: this});
        },
        checkValidity() {
            return this.value.trim() !== "";
        },
    };
}


function createRuntime(options = {}) {
    const inputs = (options.initialValues || ["", "", ""]).map(createInput);
    const formListeners = new Map();
    const buttonListeners = new Map();
    const buttonAttributes = new Map();
    const button = {
        dataset: {},
        disabled: false,
        hidden: false,
        textContent: "Начать смену",
        classList: new FakeClassList(),
        style: {setProperty() {}},
        addEventListener(type, callback) {
            const listeners = buttonListeners.get(type) || [];
            listeners.push(callback);
            buttonListeners.set(type, listeners);
        },
        getAttribute(name) {
            return buttonAttributes.has(name) ? buttonAttributes.get(name) : null;
        },
        querySelector() {
            return null;
        },
    };
    const form = {
        dataset: {},
        errorList: options.withErrors ? {} : null,
        querySelector(selector) {
            if (selector === ".errorlist") return this.errorList;
            return null;
        },
        querySelectorAll(selector) {
            return selector === "input[type='number']" ? inputs : [];
        },
        addEventListener(type, callback) {
            formListeners.set(type, callback);
        },
        dispatch(type) {
            const callback = formListeners.get(type);
            const event = {
                type,
                target: this,
                defaultPrevented: false,
                preventDefault() {
                    this.defaultPrevented = true;
                },
            };
            if (callback) callback(event);
            return event;
        },
        requestSubmit() {
            return this.dispatch("submit");
        },
        reset() {
            this.dispatch("reset");
            inputs.forEach((input) => {
                input.value = input.defaultValue;
            });
        },
    };
    const counters = {
        fragmentRequests: 0,
        rootReplacements: 0,
        shellRebinds: 0,
        wakeReasons: [],
    };
    const freshShell = {
        dataset: {},
        querySelector() {
            return null;
        },
        contains() {
            return false;
        },
    };
    let currentShell;
    const shell = {
        dataset: {activeTab: "shift"},
        querySelector(selector) {
            if (selector === ".driver-shift-opening-form") return form;
            if (selector === "[data-driver-shift-open-button]") return button;
            return null;
        },
        contains(node) {
            return inputs.includes(node);
        },
        replaceWith(replacement) {
            assert.equal(replacement, freshShell);
            counters.rootReplacements += 1;
            currentShell = replacement;
        },
    };
    currentShell = shell;

    const fakeDocument = {
        activeElement: null,
        body: {dataset: {}},
        readyState: "loading",
        addEventListener() {},
        querySelector(selector) {
            if (selector === "[data-driver-shell]") return currentShell;
            return null;
        },
    };
    const runtimeWindow = {
        AppRealtime: {
            wake(reason) {
                counters.wakeReasons.push(reason);
            },
        },
        AppOperationalFragment: {
            request(screen, version) {
                counters.fragmentRequests += 1;
                assert.equal(screen, "driver");
                assert.equal(version, 83519);
                return Promise.resolve({
                    contract: "operational-fragment-v1",
                    screen: "driver",
                    version,
                    html: "<main data-driver-shell></main>",
                });
            },
            parseRoot(html, selector) {
                assert.equal(html, "<main data-driver-shell></main>");
                assert.equal(selector, "[data-driver-shell]");
                return freshShell;
            },
        },
        bindDriverMobileShell() {
            counters.shellRebinds += 1;
        },
        setTimeout,
    };
    runtimeWindow.window = runtimeWindow;
    const context = {
        window: runtimeWindow,
        document: fakeDocument,
        Promise,
        Array,
        Number,
        setTimeout,
    };
    const bindSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function bindDriverShiftOpeningForm(shell)",
        "Driver opening shift form binding"
    );
    const holdSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function bindDriverShiftHoldAction(form, button, options)",
        "Driver shared Shift hold adapter"
    );
    const guardSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function isDriverOperationalRefreshUnsafe(shell)",
        "Driver operational refresh guard"
    );
    const refreshSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "window.applyOperationalStateRefresh = function (context)",
        "Driver operational refresh"
    );
    vm.runInNewContext(
        `${MOBILE_SHIFT_HOLD_SOURCE}\n${guardSource}\n${refreshSource};\n${holdSource}\n${bindSource}`,
        context,
        {filename: "templates/users/driver_shift.html#opening-form-refresh"}
    );

    return {
        bind: context.bindDriverShiftOpeningForm,
        refresh: runtimeWindow.applyOperationalStateRefresh,
        inputs,
        form,
        button,
        shell,
        fakeDocument,
        counters,
    };
}


test("driver opening readings survive blur and realtime fragment event; reset applies deferred version", async () => {
    const runtime = createRuntime();
    runtime.bind(runtime.shell);
    assert.equal(runtime.button.dataset.mobileShiftHoldBound, "true");

    runtime.fakeDocument.activeElement = runtime.inputs[0];
    ["321", "654", "987"].forEach((value, index) => {
        runtime.inputs[index].value = value;
        runtime.inputs[index].dispatch("input");
    });
    runtime.fakeDocument.activeElement = null;

    const deferred = await runtime.refresh({version: 83519});

    assert.equal(deferred, false);
    assert.deepEqual(
        runtime.inputs.map((input) => input.value),
        ["321", "654", "987"]
    );
    assert.equal(runtime.form.dataset.driverShiftOpeningDirty, "true");
    assert.equal(runtime.counters.fragmentRequests, 0);
    assert.equal(runtime.counters.rootReplacements, 0);

    runtime.form.reset();
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.deepEqual(runtime.inputs.map((input) => input.value), ["", "", ""]);
    assert.equal(runtime.form.dataset.driverShiftOpeningDirty, "false");
    assert.deepEqual(runtime.counters.wakeReasons, ["driver_shift_open_reset"]);

    const applied = await runtime.refresh({version: 83519});

    assert.equal(applied, true);
    assert.equal(runtime.counters.fragmentRequests, 1);
    assert.equal(runtime.counters.rootReplacements, 1);
    assert.equal(runtime.counters.shellRebinds, 1);
});


test("driver opening form errors and pending submit both block fragment replacement", async () => {
    const errorRuntime = createRuntime({
        initialValues: ["321", "654", "987"],
        withErrors: true,
    });
    errorRuntime.bind(errorRuntime.shell);

    assert.equal(await errorRuntime.refresh({version: 83519}), false);
    assert.equal(errorRuntime.counters.fragmentRequests, 0);

    const pendingRuntime = createRuntime({
        initialValues: ["321", "654", "987"],
    });
    pendingRuntime.bind(pendingRuntime.shell);
    assert.equal(pendingRuntime.button.dataset.mobileShiftHoldBound, "true");
    const blockedSubmit = pendingRuntime.form.dispatch("submit");
    assert.equal(blockedSubmit.defaultPrevented, true);
    assert.equal(pendingRuntime.form.dataset.driverShiftOpeningPending, "false");
    pendingRuntime.form.dataset.driverShiftHoldComplete = "true";
    pendingRuntime.form.dispatch("submit");

    assert.equal(pendingRuntime.form.dataset.driverShiftOpeningPending, "true");
    assert.equal(pendingRuntime.button.classList.contains("is-pending"), true);
    assert.equal(await pendingRuntime.refresh({version: 83519}), false);
    assert.equal(pendingRuntime.counters.fragmentRequests, 0);
    assert.equal(pendingRuntime.counters.rootReplacements, 0);
});
