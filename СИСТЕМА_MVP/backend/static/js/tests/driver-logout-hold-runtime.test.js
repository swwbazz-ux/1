const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const DRIVER_TEMPLATE_PATH = path.resolve(
    __dirname,
    "..",
    "..",
    "..",
    "templates",
    "users",
    "driver_shift.html"
);
const DRIVER_TEMPLATE_SOURCE = fs.readFileSync(DRIVER_TEMPLATE_PATH, "utf8");
const MOBILE_SHIFT_HOLD_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "mobile-shift-unified-v1.js"),
    "utf8"
);

function extractDriverLogoutBindingSource() {
    const startMarker =
        '        if (logoutButton && logoutButton.dataset.driverLogoutBound !== "true") {';
    const start = DRIVER_TEMPLATE_SOURCE.indexOf(startMarker);
    const bindingCall = DRIVER_TEMPLATE_SOURCE.indexOf(
        "    bindDriverShiftControls();",
        start
    );
    const end = DRIVER_TEMPLATE_SOURCE.lastIndexOf("    }", bindingCall);
    assert.notEqual(start, -1, "Production Driver logout binding was not found.");
    assert.notEqual(bindingCall, -1, "Production Driver controls binding was not found.");
    assert.notEqual(end, -1, "Production Driver logout binding boundary was not found.");
    return DRIVER_TEMPLATE_SOURCE.slice(start, end);
}

class FakeLogoutButton {
    constructor() {
        this.dataset = {
            driverLogoutUrl: "/logout/",
        };
        this.attributes = new Map();
        this.disabled = false;
        this.hidden = false;
        this.listeners = new Map();
        this.progress = "";
        this.textContent = "Выйти";
        const classNames = new Set();
        this.classList = {
            add(name) {
                classNames.add(name);
            },
            remove(name) {
                classNames.delete(name);
            },
            contains(name) {
                return classNames.has(name);
            },
        };
        this.style = {
            setProperty: (name, value) => {
                if (name === "--mobile-shift-hold") {
                    this.progress = value;
                }
            },
        };
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    dispatch(type) {
        const event = {
            defaultPrevented: false,
            preventDefault() {
                this.defaultPrevented = true;
            },
        };
        (this.listeners.get(type) || []).forEach((listener) => listener(event));
        return event;
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    querySelector() {
        return null;
    }
}

function createLogoutRuntime({readonly = false} = {}) {
    let now = 1000;
    let nextFrameId = 1;
    let nextTimerId = 1;
    const frames = new Map();
    const timers = new Map();
    const navigations = [];
    let href = "http://driver.localhost:8000/driver/?tab=shift";
    const location = {};
    Object.defineProperty(location, "href", {
        get() {
            return href;
        },
        set(value) {
            href = value;
            navigations.push(value);
        },
    });
    const window = {
        location,
        isAppRoleReadonly() {
            return readonly;
        },
        requestAnimationFrame(callback) {
            const frameId = nextFrameId++;
            frames.set(frameId, callback);
            return frameId;
        },
        cancelAnimationFrame(frameId) {
            frames.delete(frameId);
        },
        setTimeout(callback, milliseconds) {
            const timerId = nextTimerId++;
            timers.set(timerId, {callback, dueAt: now + milliseconds});
            return timerId;
        },
        clearTimeout(timerId) {
            timers.delete(timerId);
        },
    };
    const FakeDate = {
        now() {
            return now;
        },
    };
    const button = new FakeLogoutButton();

    vm.runInNewContext(
        `
        ${MOBILE_SHIFT_HOLD_SOURCE}
        (function () {
            var logoutButton = context.logoutButton;
            ${extractDriverLogoutBindingSource()}
        })();
        `,
        {
            context: {logoutButton: button},
            Date: FakeDate,
            document: {
                readyState: "loading",
                addEventListener() {},
            },
            window,
        },
        {filename: "templates/users/driver_shift.html#logout-hold"}
    );

    return {
        button,
        navigations,
        advance(milliseconds) {
            now += milliseconds;
            const due = Array.from(timers.entries())
                .filter(([, timer]) => timer.dueAt <= now)
                .sort((left, right) => left[1].dueAt - right[1].dueAt);
            due.forEach(([timerId, timer]) => {
                timers.delete(timerId);
                timer.callback();
            });
        },
        runFrames() {
            const callbacks = Array.from(frames.values());
            frames.clear();
            callbacks.forEach((callback) => callback(now));
        },
        pendingFrames() {
            return frames.size;
        },
        pendingTimers() {
            return timers.size;
        },
    };
}

test("production Driver logout hold navigates exactly once and can be repeated", () => {
    const runtime = createLogoutRuntime();

    const firstPointerDown = runtime.button.dispatch("pointerdown");
    assert.equal(firstPointerDown.defaultPrevented, true);
    runtime.advance(2000);
    runtime.runFrames();
    assert.deepEqual(runtime.navigations, ["/logout/"]);
    assert.equal(runtime.button.progress, "100%");
    assert.equal(runtime.pendingFrames(), 0);
    assert.equal(runtime.pendingTimers(), 0);

    runtime.button.dispatch("pointerup");
    runtime.button.dispatch("click");
    runtime.button.dispatch("pointerdown");
    assert.equal(runtime.button.progress, "0%");
    runtime.advance(2000);
    runtime.runFrames();
    assert.deepEqual(runtime.navigations, ["/logout/", "/logout/"]);
    assert.equal(runtime.pendingFrames(), 0);
    assert.equal(runtime.pendingTimers(), 0);
});

test("production Driver logout hold resets after an early release", () => {
    const runtime = createLogoutRuntime();

    runtime.button.dispatch("pointerdown");
    runtime.advance(900);
    runtime.runFrames();
    assert.equal(runtime.button.progress, "45%");
    runtime.button.dispatch("pointerup");
    runtime.advance(2000);
    runtime.runFrames();

    assert.deepEqual(runtime.navigations, []);
    assert.equal(runtime.button.progress, "0%");
    assert.equal(runtime.pendingFrames(), 0);
    assert.equal(runtime.pendingTimers(), 0);
});

test("production Driver logout hold remains available in read-only mode", () => {
    const runtime = createLogoutRuntime({readonly: true});

    runtime.button.dispatch("pointerdown");
    runtime.advance(2000);
    runtime.runFrames();

    assert.deepEqual(runtime.navigations, ["/logout/"]);
    assert.equal(runtime.pendingFrames(), 0);
    assert.equal(runtime.pendingTimers(), 0);
});
