"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const SCRIPT = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-canvas-v1.js"),
    "utf8"
);
const CSS = fs.readFileSync(
    path.join(BACKEND, "static", "css", "dispatcher-canvas-v1.css"),
    "utf8"
);
const TEMPLATE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "dispatcher_control.html"),
    "utf8"
);

function makeStyle() {
    const values = new Map();
    return {
        width: "",
        transform: "",
        setProperty(name, value) {
            values.set(name, value);
        },
        removeProperty(name) {
            values.delete(name);
        },
        getPropertyValue(name) {
            return values.get(name) || "";
        },
    };
}

function runCanvas({width, height, phone = false, phoneFit = true}) {
    const listeners = {};
    const viewportListeners = {};
    const shell = {style: makeStyle()};
    const attributes = new Map([["data-dispatcher-canvas", "off"]]);
    if (phoneFit) attributes.set("data-dispatcher-phone-fit", "");
    const canvas = {
        style: makeStyle(),
        hasAttribute(name) {
            return attributes.has(name);
        },
        setAttribute(name, value) {
            attributes.set(name, value);
        },
        getAttribute(name) {
            return attributes.get(name);
        },
        querySelector(selector) {
            return selector === ".dispatcher-shell" ? shell : null;
        },
    };
    const state = {phone};
    const window = {
        innerWidth: width,
        innerHeight: height,
        matchMedia() {
            return {matches: state.phone};
        },
        addEventListener(name, handler) {
            (listeners[name] ||= []).push(handler);
        },
        visualViewport: {
            addEventListener(name, handler) {
                (viewportListeners[name] ||= []).push(handler);
            },
        },
    };
    const document = {
        body: {classList: {contains: () => false}},
        querySelector(selector) {
            return selector === "[data-dispatcher-canvas]" ? canvas : null;
        },
    };
    vm.runInNewContext(SCRIPT, {
        document,
        window,
        setTimeout: (handler) => handler(),
    });
    return {attributes, canvas, listeners, shell, state, window};
}

test("desktop canvas fills the viewport with one proportional transform", () => {
    const runtime = runCanvas({width: 1536, height: 886});
    assert.equal(runtime.attributes.get("data-dispatcher-canvas"), "on");
    assert.equal(runtime.canvas.style.getPropertyValue("--dispatcher-canvas-h"), "1108px");
    assert.equal(runtime.canvas.style.getPropertyValue("--dispatcher-canvas-w"), "1921px");
    assert.ok(
        Math.abs(Number(runtime.canvas.style.getPropertyValue("--dispatcher-canvas-scale")) - 886 / 1108) < 0.001
    );
    assert.equal(runtime.shell.style.width, "");
    assert.equal(runtime.shell.style.transform, "");
});

test("phone landscape uses the control-only 1400x800 shell fit", () => {
    const runtime = runCanvas({width: 784, height: 336, phone: true});
    assert.equal(runtime.attributes.get("data-dispatcher-canvas"), "off");
    assert.equal(runtime.shell.style.width, "1867px");
    assert.match(runtime.shell.style.transform, /^scale\(0\.4199/);

    runtime.state.phone = false;
    runtime.window.innerWidth = 1536;
    runtime.window.innerHeight = 886;
    runtime.listeners.resize[0]();
    assert.equal(runtime.shell.style.width, "");
    assert.equal(runtime.shell.style.transform, "");
    assert.equal(runtime.attributes.get("data-dispatcher-canvas"), "on");
});

test("report canvas does not inherit the control phone transform", () => {
    const runtime = runCanvas({width: 784, height: 336, phone: true, phoneFit: false});
    assert.equal(runtime.attributes.get("data-dispatcher-canvas"), "off");
    assert.equal(runtime.shell.style.width, "");
    assert.equal(runtime.shell.style.transform, "");
});

test("control template delegates desktop and phone fitting to the canvas module", () => {
    assert.match(TEMPLATE, /data-dispatcher-phone-fit/);
    assert.match(
        TEMPLATE,
        /dispatcher-canvas-v1\.js[^\n]+dispatcher-desktop-shell-v144/
    );
    assert.equal(
        (TEMPLATE.match(/function fitDispatcherShellToScreen/g) || []).length,
        1,
        "в шаблоне должен остаться только отдельный масштабатор Горного мастера"
    );
});

test("canvas stylesheet has no malformed dot-prefixed calc functions", () => {
    assert.doesNotMatch(CSS, /\.calc\(/);
    for (const factor of [".82", ".78", ".8", ".95", ".9"]) {
        assert.match(CSS, new RegExp(`calc\\(${factor.replace(".", "\\.")} \\* var\\(--gd-vw\\)\\)`));
    }
});
