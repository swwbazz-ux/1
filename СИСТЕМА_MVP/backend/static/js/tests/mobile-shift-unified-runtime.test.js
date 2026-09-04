"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const SCRIPT_PATH = path.resolve(__dirname, "..", "mobile-shift-unified-v1.js");
const SCRIPT_SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");


class FakeClassList {
    constructor() {
        this.names = new Set();
    }

    add(name) {
        this.names.add(name);
    }

    remove(name) {
        this.names.delete(name);
    }

    contains(name) {
        return this.names.has(name);
    }
}


class FakeElement {
    constructor({textContent = "", attributes = {}} = {}) {
        this.attributes = new Map(Object.entries(attributes));
        this.classList = new FakeClassList();
        this.dataset = {};
        this.disabled = false;
        this.hidden = false;
        this.listeners = new Map();
        this.readOnly = false;
        this.selected = false;
        this.textContent = textContent;
        this.value = "";
        this.styleValues = new Map();
        this.style = {
            setProperty: (name, value) => {
                this.styleValues.set(name, value);
            },
        };
        this.focused = false;
        this.blurred = false;
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    dispatch(type, properties = {}) {
        const event = {
            data: null,
            defaultPrevented: false,
            key: "",
            keyCode: 0,
            repeat: false,
            preventDefault() {
                this.defaultPrevented = true;
            },
            ...properties,
        };
        (this.listeners.get(type) || []).forEach((listener) => listener(event));
        return event;
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    hasAttribute(name) {
        return this.attributes.has(name);
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    focus() {
        this.focused = true;
    }

    blur() {
        this.blurred = true;
        this.focused = false;
    }

    select() {
        this.selected = true;
    }

    querySelector() {
        return null;
    }
}


function createComponent(fields, action) {
    const component = new FakeElement();
    component.querySelectorAll = (selector) => {
        assert.equal(selector, ".mobile-shift__metric-value input");
        return fields;
    };
    component.querySelector = (selector) => {
        if (selector.includes("[data-eo-shift-button]")) return action;
        return null;
    };
    return component;
}


function createRuntime({components = [], activeElement = null, nativeKeyboard = false} = {}) {
    let now = 1000;
    let nextTimerId = 1;
    let nextFrameId = 1;
    const timers = new Map();
    const frames = new Map();
    const documentListeners = new Map();
    const document = {
        readyState: "complete",
        activeElement,
        documentElement: {dataset: {}},
        addEventListener(type, listener) {
            documentListeners.set(type, listener);
        },
        querySelectorAll(selector) {
            assert.equal(selector, ".mobile-shift");
            return components;
        },
    };
    const orientation = {
        type: "portrait-primary",
        addEventListener() {},
    };
    let nativeKeyboardHideCalls = 0;
    const window = {
        navigator: {},
        screen: {orientation, width: 390, height: 844},
        addEventListener() {},
        setTimeout(callback, milliseconds) {
            const timerId = nextTimerId;
            nextTimerId += 1;
            timers.set(timerId, {callback, dueAt: now + milliseconds});
            return timerId;
        },
        clearTimeout(timerId) {
            timers.delete(timerId);
        },
        requestAnimationFrame(callback) {
            const frameId = nextFrameId;
            nextFrameId += 1;
            frames.set(frameId, callback);
            return frameId;
        },
        cancelAnimationFrame(frameId) {
            frames.delete(frameId);
        },
    };
    if (nativeKeyboard) {
        window.Capacitor = {
            Plugins: {
                NativeKeyboard: {
                    hide() {
                        nativeKeyboardHideCalls += 1;
                        return Promise.resolve({hidden: true});
                    },
                },
            },
        };
    }
    const FakeDate = {
        now() {
            return now;
        },
    };

    vm.runInNewContext(
        SCRIPT_SOURCE,
        {Date: FakeDate, document, window},
        {filename: SCRIPT_PATH}
    );

    return {
        window,
        advance(milliseconds) {
            now += milliseconds;
            let due;
            do {
                due = Array.from(timers.entries())
                    .filter(([, timer]) => timer.dueAt <= now)
                    .sort((left, right) => left[1].dueAt - right[1].dueAt);
                due.forEach(([timerId, timer]) => {
                    timers.delete(timerId);
                    timer.callback();
                });
            } while (due.length > 0);
        },
        pendingFrames() {
            return frames.size;
        },
        runFrames() {
            const callbacks = Array.from(frames.values());
            frames.clear();
            callbacks.forEach((callback) => callback(now));
        },
        pendingTimers() {
            return timers.size;
        },
        nativeKeyboardHideCalls() {
            return nativeKeyboardHideCalls;
        },
    };
}


test("Enter advances between Shift inputs and finishes on the soft-disabled action", () => {
    const first = new FakeElement();
    const second = new FakeElement();
    const action = new FakeElement({
        textContent: "ЗАКРЫТЬ СМЕНУ",
        attributes: {"aria-disabled": "true"},
    });
    const component = createComponent([first, second], action);

    const runtime = createRuntime({components: [component], nativeKeyboard: true});

    const firstEnter = first.dispatch("keydown", {key: "Enter", keyCode: 13});
    assert.equal(firstEnter.defaultPrevented, true);
    assert.equal(second.focused, true);
    assert.equal(second.selected, true);
    assert.equal(action.focused, false);

    component.dispatch("keyup", {key: "Enter", keyCode: 13});
    const lastEnter = second.dispatch("keydown", {key: "Enter", keyCode: 13});
    runtime.runFrames();
    assert.equal(lastEnter.defaultPrevented, true);
    assert.equal(second.blurred, true, "The last input must release the mobile keyboard.");
    assert.equal(action.focused, true, "Soft-disabled actions must remain keyboard-focusable.");
    assert.equal(action.classList.contains("is-keyboard-target"), true);
    assert.equal(runtime.nativeKeyboardHideCalls(), 1, "The Android bridge must receive an explicit keyboard hide request.");
    assert.equal(action.disabled, false, "Validation must not use native disabled state.");
    assert.equal(first.getAttribute("enterkeyhint"), "next");
    assert.equal(second.getAttribute("enterkeyhint"), "done");
});


test("Last Enter does not focus an action while its mutation is pending", () => {
    const input = new FakeElement();
    const action = new FakeElement();
    action.classList.add("is-pending");
    const component = createComponent([input], action);

    const runtime = createRuntime({components: [component]});

    input.dispatch("keydown", {key: "Enter", keyCode: 13});
    runtime.runFrames();
    assert.equal(input.blurred, true);
    assert.equal(action.focused, false);
    assert.equal(component.focused, true, "Pending actions fall back to the Shift component focus target.");
});


test("Shift inputs accept whole numbers only", () => {
    const input = new FakeElement();
    const action = new FakeElement();
    const component = createComponent([input], action);

    createRuntime({components: [component]});

    assert.equal(input.getAttribute("inputmode"), "numeric");
    assert.equal(input.getAttribute("step"), "1");
    assert.equal(input.getAttribute("pattern"), "[0-9]*");

    const digit = input.dispatch("beforeinput", {data: "7"});
    const decimal = input.dispatch("beforeinput", {data: ","});
    const letters = input.dispatch("beforeinput", {data: "abc"});
    assert.equal(digit.defaultPrevented, false);
    assert.equal(decimal.defaultPrevented, true);
    assert.equal(letters.defaultPrevented, true);

    input.value = "123,45";
    input.dispatch("input");
    assert.equal(input.value, "123");
    input.value = "-9";
    input.dispatch("input");
    assert.equal(input.value, "");
});


test("A blocked hold reports once and never completes", () => {
    const button = new FakeElement({
        textContent: "ЗАКРЫТЬ СМЕНУ",
        attributes: {"aria-disabled": "true"},
    });
    let blockedCalls = 0;
    let completionCalls = 0;
    const runtime = createRuntime();
    runtime.window.MobileShiftHold.bind(button, {
        holdMs: 500,
        onBlockedPress() {
            blockedCalls += 1;
        },
        onComplete() {
            completionCalls += 1;
        },
    });

    const pointerDown = button.dispatch("pointerdown");
    button.dispatch("click");
    runtime.advance(1000);

    assert.equal(pointerDown.defaultPrevented, true);
    assert.equal(blockedCalls, 1, "The paired synthetic click must not repeat the hint.");
    assert.equal(completionCalls, 0);
    assert.equal(runtime.pendingTimers(), 0);
    assert.equal(runtime.pendingFrames(), 0);
    assert.equal(button.styleValues.get("--mobile-shift-hold"), undefined);
});


test("An allowed Shift hold still completes exactly once", () => {
    const label = new FakeElement({textContent: "ЗАКРЫТЬ СМЕНУ"});
    const button = new FakeElement({textContent: "ЗАКРЫТЬ СМЕНУ"});
    button.querySelector = (selector) => {
        assert.equal(selector, "[data-mobile-shift-label]");
        return label;
    };
    let blockedCalls = 0;
    let completionCalls = 0;
    const runtime = createRuntime();
    runtime.window.MobileShiftHold.bind(button, {
        holdMs: 500,
        holdLabel: "Держите",
        canStart() {
            return true;
        },
        onBlockedPress() {
            blockedCalls += 1;
        },
        onComplete() {
            completionCalls += 1;
        },
    });

    button.dispatch("pointerdown");
    assert.equal(label.textContent, "Держите");
    assert.equal(button.classList.contains("is-holding"), true);
    runtime.advance(500);
    button.dispatch("pointerup");
    button.dispatch("click");

    assert.equal(blockedCalls, 0);
    assert.equal(completionCalls, 1);
    assert.equal(button.classList.contains("is-holding"), false);
    assert.equal(button.styleValues.get("--mobile-shift-hold"), "100%");
    assert.equal(runtime.pendingTimers(), 0);
    assert.equal(runtime.pendingFrames(), 0);
});


test("Starting a hold releases an active editor before the timer runs", () => {
    const input = new FakeElement();
    input.matches = (selector) => selector.includes("input");
    input.focused = true;
    const button = new FakeElement({textContent: "ЗАКРЫТЬ СМЕНУ"});
    const runtime = createRuntime({activeElement: input});

    runtime.window.MobileShiftHold.bind(button, {holdMs: 1000});
    const pointerDown = button.dispatch("pointerdown");

    assert.equal(pointerDown.defaultPrevented, true);
    assert.equal(input.blurred, true, "The software keyboard owner must lose focus before holding.");
    assert.equal(button.classList.contains("is-holding"), true);
    assert.equal(runtime.pendingTimers(), 1);
});
