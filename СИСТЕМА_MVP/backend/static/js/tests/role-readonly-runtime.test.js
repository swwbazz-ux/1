"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function datasetKey(attributeName) {
    return attributeName
        .slice(5)
        .replace(/-([a-z])/g, function (_match, letter) {
            return letter.toUpperCase();
        });
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

    toggle(name, force) {
        const enabled = typeof force === "boolean" ? force : !this.values.has(name);
        if (enabled) {
            this.values.add(name);
        } else {
            this.values.delete(name);
        }
        return enabled;
    }
}

let runtimeWindow = null;

class FakeElement {
    constructor(nodeName, attributes) {
        this.nodeName = String(nodeName || "div").toUpperCase();
        this.attributes = new Map();
        this.dataset = {};
        this.classList = new FakeClassList();
        this.children = [];
        this.parentNode = null;
        this.disabled = false;
        this.hidden = false;
        this.textContent = "";
        this.value = "";
        this.focused = false;
        this.nativeSubmitCount = 0;
        this.eventListeners = new Map();
        const styleValues = new Map();
        this.style = {
            setProperty(name, value) {
                styleValues.set(name, String(value));
            },
            getPropertyValue(name) {
                return styleValues.get(name) || "";
            },
        };
        Object.entries(attributes || {}).forEach(([name, value]) => {
            this.setAttribute(name, value);
        });
    }

    get firstChild() {
        return this.children[0] || null;
    }

    get method() {
        return this.getAttribute("method") || "get";
    }

    set method(value) {
        this.setAttribute("method", value);
    }

    get action() {
        const rawAction = this.getAttribute("action") || runtimeWindow.location.href;
        return new URL(rawAction, runtimeWindow.location.origin).href;
    }

    set action(value) {
        this.setAttribute("action", value);
    }

    get href() {
        const rawHref = this.getAttribute("href") || runtimeWindow.location.href;
        return new URL(rawHref, runtimeWindow.location.origin).href;
    }

    set href(value) {
        this.setAttribute("href", value);
    }

    setAttribute(name, value) {
        const normalizedValue = String(value);
        this.attributes.set(name, normalizedValue);
        if (name.startsWith("data-")) {
            this.dataset[datasetKey(name)] = normalizedValue;
        }
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    hasAttribute(name) {
        return this.attributes.has(name);
    }

    removeAttribute(name) {
        this.attributes.delete(name);
        if (name.startsWith("data-")) {
            delete this.dataset[datasetKey(name)];
        }
    }

    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    addEventListener(type, listener) {
        const listeners = this.eventListeners.get(type) || [];
        listeners.push(listener);
        this.eventListeners.set(type, listeners);
    }

    dispatchEvent(event) {
        const nextEvent = Object.assign(
            {
                type: "",
                target: this,
                preventDefault() {},
                stopImmediatePropagation() {},
            },
            event || {}
        );
        (this.eventListeners.get(nextEvent.type) || []).forEach((listener) => {
            listener(nextEvent);
        });
        return !nextEvent.defaultPrevented;
    }

    click() {
        return this.dispatchEvent({type: "click", target: this});
    }

    focus() {
        this.focused = true;
    }

    insertBefore(child, before) {
        child.parentNode = this;
        const index = before ? this.children.indexOf(before) : -1;
        if (index < 0) {
            this.children.push(child);
        } else {
            this.children.splice(index, 0, child);
        }
        return child;
    }

    remove() {
        if (!this.parentNode) {
            return;
        }
        const index = this.parentNode.children.indexOf(this);
        if (index >= 0) {
            this.parentNode.children.splice(index, 1);
        }
        this.parentNode = null;
    }

    descendants() {
        return this.children.flatMap((child) => [child, ...child.descendants()]);
    }

    matches(selector) {
        return String(selector || "")
            .split(",")
            .map((item) => item.trim())
            .some((item) => this.matchesSingle(item));
    }

    matchesSingle(selector) {
        if (!selector) {
            return false;
        }
        if (selector === "form") {
            return this.nodeName === "FORM";
        }
        if (selector === "button") {
            return this.nodeName === "BUTTON";
        }
        if (selector === "a") {
            return this.nodeName === "A";
        }
        if (selector === "select") {
            return this.nodeName === "SELECT";
        }
        if (selector === "textarea") {
            return this.nodeName === "TEXTAREA";
        }
        if (selector === "input:not([type='hidden'])") {
            return this.nodeName === "INPUT" && this.getAttribute("type") !== "hidden";
        }
        if (selector === "input[type='number']") {
            return this.nodeName === "INPUT" && this.getAttribute("type") === "number";
        }
        const attributeMatch = selector.match(/^\[([a-z0-9-]+)\]$/i);
        if (attributeMatch) {
            const attributeName = attributeMatch[1];
            if (attributeName.startsWith("data-")) {
                return (
                    this.hasAttribute(attributeName)
                    || Object.prototype.hasOwnProperty.call(
                        this.dataset,
                        datasetKey(attributeName)
                    )
                );
            }
            return this.hasAttribute(attributeName);
        }
        return false;
    }

    closest(selector) {
        let current = this;
        while (current) {
            if (current.matches(selector)) {
                return current;
            }
            current = current.parentNode;
        }
        return null;
    }

    querySelectorAll(selector) {
        return this.descendants().filter((element) => element.matches(selector));
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }
}

class FakeFormElement extends FakeElement {
    constructor(attributes) {
        super("form", attributes);
    }

    submit() {
        this.nativeSubmitCount += 1;
    }
}

function createEvent(type, target, extra) {
    return Object.assign(
        {
            type,
            target,
            key: "",
            defaultPrevented: false,
            immediatePropagationStopped: false,
            preventDefault() {
                this.defaultPrevented = true;
            },
            stopImmediatePropagation() {
                this.immediatePropagationStopped = true;
            },
        },
        extra || {}
    );
}

function createRuntime() {
    const documentListeners = new Map();
    const windowListeners = new Map();
    const body = new FakeElement("body");
    body.dataset.roleAccessActive = "true";

    const document = {
        body,
        createElement(nodeName) {
            return new FakeElement(nodeName);
        },
        querySelectorAll(selector) {
            return body.querySelectorAll(selector);
        },
        querySelector(selector) {
            return body.querySelector(selector);
        },
        addEventListener(type, listener) {
            const listeners = documentListeners.get(type) || [];
            listeners.push(listener);
            documentListeners.set(type, listeners);
        },
    };

    const nativeFetchCalls = [];
    const window = {
        location: {
            href: "http://localhost/excavator/work/",
            origin: "http://localhost",
        },
        HTMLFormElement: FakeFormElement,
        Request: globalThis.Request,
        Response: globalThis.Response,
        MutationObserver: null,
        requestAnimationFrame(callback) {
            callback();
        },
        addEventListener(type, listener) {
            const listeners = windowListeners.get(type) || [];
            listeners.push(listener);
            windowListeners.set(type, listeners);
        },
        dispatchEvent(event) {
            (windowListeners.get(event.type) || []).forEach((listener) => listener(event));
        },
        fetch(input, init) {
            nativeFetchCalls.push({input, init});
            return Promise.resolve(new Response("native", {status: 200}));
        },
    };
    window.window = window;
    runtimeWindow = window;

    function dispatchDocumentEvent(type, target, extra) {
        const event = createEvent(type, target, extra);
        for (const listener of documentListeners.get(type) || []) {
            listener(event);
            if (event.immediatePropagationStopped) {
                break;
            }
        }
        return event;
    }

    return {
        document,
        window,
        nativeFetchCalls,
        dispatchDocumentEvent,
    };
}

test("live role switch makes mutations readonly and preserves safe controls", async () => {
    const runtime = createRuntime();
    const mutationForm = new FakeFormElement({
        method: "post",
        action: "/excavator/work/",
    });
    const safeTab = new FakeElement("button", {"data-eo-tab": "shift"});
    const mutationButton = new FakeElement("button", {"data-eo-shift-button": ""});
    mutationForm.appendChild(safeTab);
    mutationForm.appendChild(mutationButton);

    const getForm = new FakeFormElement({method: "get", action: "/reports/"});
    const navigationButton = new FakeElement("button");
    getForm.appendChild(navigationButton);

    runtime.document.body.appendChild(mutationForm);
    runtime.document.body.appendChild(getForm);

    const sourcePath = path.resolve(__dirname, "..", "role-readonly.js");
    const source = fs.readFileSync(sourcePath, "utf8");
    vm.runInNewContext(source, {
        window: runtime.window,
        document: runtime.document,
        URL,
        Request: globalThis.Request,
        Response: globalThis.Response,
    });
    runtime.dispatchDocumentEvent("DOMContentLoaded", runtime.document);

    runtime.window.dispatchEvent({
        type: "active-role-state-changed",
        detail: {active: false},
    });

    assert.equal(runtime.document.body.dataset.roleReadonly, "true");
    assert.ok(runtime.document.querySelector("[data-inactive-role-banner]"));
    assert.equal(mutationButton.disabled, true);
    assert.equal(safeTab.disabled, false);
    assert.equal(navigationButton.disabled, false);

    const safeClick = runtime.dispatchDocumentEvent("click", safeTab);
    const mutationClick = runtime.dispatchDocumentEvent("click", mutationButton);
    const navigationClick = runtime.dispatchDocumentEvent("click", navigationButton);
    assert.equal(safeClick.defaultPrevented, false);
    assert.equal(mutationClick.defaultPrevented, true);
    assert.equal(navigationClick.defaultPrevented, false);

    mutationForm.submit();
    getForm.submit();
    assert.equal(mutationForm.nativeSubmitCount, 0);
    assert.equal(getForm.nativeSubmitCount, 1);

    const blockedResponse = await runtime.window.fetch("/excavator/work/", {
        method: "POST",
    });
    const getResponse = await runtime.window.fetch("/reports/", {method: "GET"});
    assert.equal(blockedResponse.status, 409);
    assert.equal(getResponse.status, 200);
    assert.equal(runtime.nativeFetchCalls.length, 1);

    runtime.window.dispatchEvent({
        type: "active-role-state-changed",
        detail: {active: true},
    });
    assert.equal(runtime.document.body.dataset.roleReadonly, "false");
    assert.equal(mutationButton.disabled, false);
    assert.equal(
        runtime.document.querySelector("[data-inactive-role-banner]"),
        null
    );
    mutationForm.submit();
    assert.equal(mutationForm.nativeSubmitCount, 1);
});

test("ready confirmation is disarmed when the active role changes", () => {
    const basePath = path.resolve(__dirname, "..", "..", "..", "templates", "base.html");
    const baseSource = fs.readFileSync(basePath, "utf8");
    const startMarker = "function closeConfirmDialog()";
    const endMarker = "    if (confirmCancel) {";
    const start = baseSource.indexOf(startMarker);
    const end = baseSource.indexOf(endMarker, start);
    assert.notEqual(start, -1, "Production closeConfirmDialog was not found.");
    assert.notEqual(end, -1, "Production confirmation listener boundary was not found.");
    const productionSnippet = baseSource.slice(start, end);

    const windowListeners = new Map();
    const clearedIntervals = [];
    const confirmModal = new FakeElement("div");
    const confirmAccept = new FakeElement("button");
    const confirmCancel = new FakeElement("button");
    const confirmClose = new FakeElement("button");
    const body = new FakeElement("body");
    const window = {
        addEventListener(type, listener) {
            const listeners = windowListeners.get(type) || [];
            listeners.push(listener);
            windowListeners.set(type, listeners);
        },
        dispatchEvent(event) {
            (windowListeners.get(event.type) || []).forEach((listener) => listener(event));
        },
        clearInterval(timerId) {
            clearedIntervals.push(timerId);
        },
        isAppRoleReadonly() {
            return true;
        },
    };
    const document = {body};
    const retained = {
        confirmModal,
        confirmAccept,
        confirmCancel,
        confirmClose,
        actionCallCount: 0,
        readState: null,
    };
    vm.runInNewContext(
        `
        (function () {
            var confirmModal = context.confirmModal;
            var confirmAccept = context.confirmAccept;
            var confirmCancel = context.confirmCancel;
            var confirmClose = context.confirmClose;
            var pendingConfirmAction = function () {
                context.actionCallCount += 1;
            };
            var pendingConfirmTimer = 77;
            function clearConfirmVariant() {}
            function resetConfirmAccept() {
                confirmAccept.disabled = false;
            }
            ${productionSnippet}
            context.readState = function () {
                return {
                    action: pendingConfirmAction,
                    timer: pendingConfirmTimer
                };
            };
        })();
        `,
        {window, document, context: retained}
    );

    window.dispatchEvent({
        type: "active-role-state-changed",
        detail: {active: false},
    });
    const state = retained.readState();
    assert.equal(confirmModal.hidden, true);
    assert.equal(state.action, null);
    assert.equal(state.timer, null);
    assert.deepEqual(clearedIntervals, [77]);
    assert.equal(confirmCancel.disabled, false);
    assert.equal(confirmClose.disabled, false);
    confirmAccept.click();
    assert.equal(retained.actionCallCount, 0);
});

function extractBaseOpenConfirmSource() {
    const basePath = path.resolve(__dirname, "..", "..", "..", "templates", "base.html");
    const source = fs.readFileSync(basePath, "utf8");
    const startMarker = "function openConfirmDialog(message, action, delaySeconds, acceptLabel, options)";
    const endMarker = "    window.openAppConfirmDialog = openConfirmDialog;";
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.notEqual(start, -1, "Production openConfirmDialog was not found.");
    assert.notEqual(end, -1, "Production openConfirmDialog boundary was not found.");
    return source.slice(start, end);
}

function extractBaseLinkConfirmHandlerSource() {
    const basePath = path.resolve(__dirname, "..", "..", "..", "templates", "base.html");
    const source = fs.readFileSync(basePath, "utf8");
    const bodyMarker = 'var link = event.target.closest("a");';
    const body = source.indexOf(bodyMarker);
    const start = source.lastIndexOf('document.addEventListener("click"', body);
    const end = source.indexOf("    function bindAppConfirmForm(form)", body);
    assert.notEqual(body, -1, "Production link-confirm body was not found.");
    assert.notEqual(start, -1, "Production link-confirm listener was not found.");
    assert.notEqual(end, -1, "Production link-confirm boundary was not found.");
    return source.slice(start, end);
}

test("read-only ordinary logout uses the production link handler and mutation confirms stay blocked", () => {
    const runtime = createRuntime();
    runtime.window.location.href = "http://localhost/mining-master/assignments/";
    let readonly = true;
    runtime.window.isAppRoleReadonly = function () {
        return readonly;
    };
    runtime.window.clearInterval = function () {};

    const confirmModal = new FakeElement("div");
    const confirmMessage = new FakeElement("p");
    const confirmAccept = new FakeElement("button");
    const confirmCancel = new FakeElement("button");
    confirmModal.hidden = true;
    const retained = {
        confirmModal,
        confirmMessage,
        confirmAccept,
        confirmCancel,
        readPendingAction: null,
    };
    vm.runInNewContext(
        `
        (function () {
            var logoutPath = "/logout/";
            var confirmModal = context.confirmModal;
            var confirmMessage = context.confirmMessage;
            var confirmAccept = context.confirmAccept;
            var confirmCancel = context.confirmCancel;
            var pendingConfirmAction = null;
            var pendingConfirmTimer = null;
            function syncConfirmTheme() {}
            function applyConfirmVariant() {}
            function resetConfirmAccept(acceptLabel) {
                confirmAccept.disabled = false;
                confirmAccept.textContent = acceptLabel || "Подтвердить";
            }
            ${extractBaseOpenConfirmSource()}
            ${extractBaseLinkConfirmHandlerSource()}
            context.readPendingAction = function () {
                return pendingConfirmAction;
            };
        })();
        `,
        {
            window: runtime.window,
            document: runtime.document,
            context: retained,
        }
    );

    const logoutLink = new FakeElement("a", {href: "/logout/"});
    const logoutLabel = new FakeElement("span");
    logoutLink.appendChild(logoutLabel);
    runtime.document.body.appendChild(logoutLink);

    const logoutEvent = runtime.dispatchDocumentEvent("click", logoutLabel);
    assert.equal(logoutEvent.defaultPrevented, true);
    assert.equal(runtime.window.location.href, "http://localhost/logout/");
    assert.equal(confirmModal.hidden, true);
    assert.equal(retained.readPendingAction(), null);

    runtime.window.location.href = "http://localhost/mining-master/assignments/";
    const mutationLink = new FakeElement("a", {
        href: "/mining-master/shift/close/",
        "data-confirm": "Завершить смену?",
    });
    runtime.document.body.appendChild(mutationLink);
    const mutationEvent = runtime.dispatchDocumentEvent("click", mutationLink);
    assert.equal(mutationEvent.defaultPrevented, true);
    assert.equal(
        runtime.window.location.href,
        "http://localhost/mining-master/assignments/"
    );
    assert.equal(confirmModal.hidden, true);
    assert.equal(retained.readPendingAction(), null);

    readonly = false;
    const activeLogoutEvent = runtime.dispatchDocumentEvent("click", logoutLink);
    assert.equal(activeLogoutEvent.defaultPrevented, true);
    assert.equal(
        runtime.window.location.href,
        "http://localhost/mining-master/assignments/"
    );
    assert.equal(confirmModal.hidden, false);
    assert.equal(typeof retained.readPendingAction(), "function");
});

function extractDriverHoldGuardSource() {
    const templatePath = path.resolve(
        __dirname,
        "..",
        "..",
        "..",
        "templates",
        "users",
        "driver_shift.html"
    );
    const source = fs.readFileSync(templatePath, "utf8");
    const startMarker = "/* DRIVER_ROLE_HOLD_GUARD_START */";
    const endMarker = "/* DRIVER_ROLE_HOLD_GUARD_END */";
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.notEqual(start, -1, "Driver hold guard start marker was not found.");
    assert.notEqual(end, -1, "Driver hold guard end marker was not found.");
    return source.slice(start + startMarker.length, end);
}

function extractDriverShiftCloseBindingSource() {
    const templatePath = path.resolve(
        __dirname,
        "..",
        "..",
        "..",
        "templates",
        "users",
        "driver_shift.html"
    );
    const source = fs.readFileSync(templatePath, "utf8");
    const startMarker = '        if (form && closeButton && closeButton.dataset.driverShiftBound !== "true") {';
    // Раньше границей служила привязка кнопки «Обновить» рядом со сменой. Её
    // убрали совсем: она проверяла обновление веб-оболочки, а стояла во
    // вкладке «Смена» и читалась как обновление данных смены. Берём следующую
    // привязку в том же блоке — выход из смены.
    const endMarker = '        if (logoutButton && logoutButton.dataset.driverLogoutBound !== "true") {';
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.notEqual(start, -1, "Production driver shift-close binding was not found.");
    assert.notEqual(end, -1, "Production driver shift-close binding boundary was not found.");
    return {
        source: source.slice(start, end),
        template: source,
    };
}

function createHoldRuntime() {
    const windowListeners = new Map();
    const timers = new Map();
    const frames = new Map();
    let nextId = 1;
    let now = 1000;
    let readonly = false;
    const window = {
        addEventListener(type, listener) {
            const listeners = windowListeners.get(type) || [];
            listeners.push(listener);
            windowListeners.set(type, listeners);
        },
        removeEventListener(type, listener) {
            const listeners = windowListeners.get(type) || [];
            windowListeners.set(
                type,
                listeners.filter((candidate) => candidate !== listener)
            );
        },
        dispatchEvent(event) {
            (windowListeners.get(event.type) || []).slice().forEach((listener) => {
                listener(event);
            });
        },
        setTimeout(callback) {
            const id = nextId++;
            timers.set(id, callback);
            return id;
        },
        clearTimeout(id) {
            timers.delete(id);
        },
        requestAnimationFrame(callback) {
            const id = nextId++;
            frames.set(id, callback);
            return id;
        },
        cancelAnimationFrame(id) {
            frames.delete(id);
        },
        isAppRoleReadonly() {
            return readonly;
        },
    };
    const FakeDate = {
        now() {
            return now;
        },
    };
    return {
        window,
        FakeDate,
        setReadonly(value) {
            readonly = value;
        },
        advance(milliseconds) {
            now += milliseconds;
        },
        runTimers() {
            const callbacks = Array.from(timers.values());
            timers.clear();
            callbacks.forEach((callback) => callback());
        },
        runFrames() {
            const callbacks = Array.from(frames.values());
            frames.clear();
            callbacks.forEach((callback) => callback(now));
        },
        pendingTimers() {
            return timers.size;
        },
        pendingFrames() {
            return frames.size;
        },
    };
}

test("driver shift-close and unload holds reset on a mid-hold role switch", () => {
    const runtime = createHoldRuntime();
    vm.runInNewContext(extractDriverHoldGuardSource(), {
        window: runtime.window,
        Date: runtime.FakeDate,
    });

    const shiftState = {progress: 0, holding: false, confirmed: false, submits: 0};
    const unloadState = {
        progress: 0,
        holding: false,
        pending: false,
        holdComplete: false,
        label: "РАЗГРУЗКА",
        submits: 0,
    };
    const shiftGuard = runtime.window.createDriverRoleHoldGuard({
        holdMs: 2000,
        onStart() {
            shiftState.holding = true;
        },
        onProgress(percent) {
            shiftState.progress = percent;
        },
        onReset() {
            shiftState.progress = 0;
            shiftState.holding = false;
            shiftState.confirmed = false;
        },
        onComplete() {
            shiftState.confirmed = true;
            shiftState.submits += 1;
        },
    });
    const unloadGuard = runtime.window.createDriverRoleHoldGuard({
        holdMs: 2000,
        onStart() {
            unloadState.holding = true;
        },
        onProgress(percent) {
            unloadState.progress = percent;
        },
        onReset() {
            unloadState.progress = 0;
            unloadState.holding = false;
            unloadState.pending = false;
            unloadState.holdComplete = false;
            unloadState.label = "РАЗГРУЗКА";
        },
        onComplete() {
            unloadState.pending = true;
            unloadState.holdComplete = true;
            unloadState.label = "ОТПРАВКА";
            unloadState.submits += 1;
        },
    });

    assert.equal(shiftGuard.start(), true);
    assert.equal(unloadGuard.start(), true);
    runtime.advance(800);
    runtime.runFrames();
    assert.ok(shiftState.progress > 0);
    assert.ok(unloadState.progress > 0);

    runtime.setReadonly(true);
    runtime.window.dispatchEvent({
        type: "active-role-state-changed",
        detail: {active: false},
    });
    runtime.advance(2000);
    runtime.runFrames();
    runtime.runTimers();

    assert.deepEqual(shiftState, {
        progress: 0,
        holding: false,
        confirmed: false,
        submits: 0,
    });
    assert.deepEqual(unloadState, {
        progress: 0,
        holding: false,
        pending: false,
        holdComplete: false,
        label: "РАЗГРУЗКА",
        submits: 0,
    });
    assert.equal(runtime.pendingTimers(), 0);
    assert.equal(runtime.pendingFrames(), 0);
});

test("driver hold final callback rechecks readonly before local state changes", () => {
    const runtime = createHoldRuntime();
    vm.runInNewContext(extractDriverHoldGuardSource(), {
        window: runtime.window,
        Date: runtime.FakeDate,
    });
    const state = {pending: false, holdComplete: false, submits: 0, resets: 0};
    const guard = runtime.window.createDriverRoleHoldGuard({
        holdMs: 2000,
        onReset() {
            state.pending = false;
            state.holdComplete = false;
            state.resets += 1;
        },
        onComplete() {
            state.pending = true;
            state.holdComplete = true;
            state.submits += 1;
        },
    });

    assert.equal(guard.start(), true);
    runtime.setReadonly(true);
    runtime.advance(2000);
    runtime.runTimers();

    assert.equal(state.pending, false);
    assert.equal(state.holdComplete, false);
    assert.equal(state.submits, 0);
    assert.ok(state.resets >= 1);
});

test("production driver shift-close requires its hold marker and blocks readonly role", () => {
    const runtime = createHoldRuntime();
    const form = new FakeFormElement({method: "post", action: "/driver/shift/close/"});
    const closeButton = new FakeElement("button");
    const closeLabel = new FakeElement("span", {"data-mobile-shift-label": ""});
    closeButton.appendChild(closeLabel);
    const binding = extractDriverShiftCloseBindingSource();
    vm.runInNewContext(
        `
        (function () {
            var form = context.form;
            var closeButton = context.closeButton;
            var shiftScroll = null;
            function driverRoleIsReadonly() {
                return window.isAppRoleReadonly();
            }
            function bindDriverShiftHoldAction(boundForm, boundButton, options) {
                context.holdBinding = {form: boundForm, button: boundButton, options: options};
            }
            ${binding.source}
        })();
        `,
        {
            window: runtime.window,
            context: {
                form,
                closeButton,
            },
        }
    );

    assert.equal(form.dispatchEvent(createEvent("submit", form)), false);
    assert.equal(closeButton.disabled, false);
    assert.equal(closeButton.classList.contains("is-pending"), false);
    form.dataset.driverShiftHoldComplete = "true";
    assert.equal(form.dispatchEvent(createEvent("submit", form)), true);
    assert.equal(closeButton.disabled, true);
    assert.equal(closeButton.classList.contains("is-pending"), true);
    assert.equal(closeLabel.textContent, "Закрываем смену");
    assert.equal(form.dataset.driverShiftHoldComplete, undefined);

    const readonlyRuntime = createHoldRuntime();
    readonlyRuntime.setReadonly(true);
    const readonlyForm = new FakeFormElement({method: "post", action: "/driver/shift/close/"});
    const readonlyButton = new FakeElement("button");
    vm.runInNewContext(
        `
        (function () {
            var form = context.form;
            var closeButton = context.closeButton;
            var shiftScroll = null;
            function driverRoleIsReadonly() {
                return window.isAppRoleReadonly();
            }
            function bindDriverShiftHoldAction() {}
            ${binding.source}
        })();
        `,
        {
            window: readonlyRuntime.window,
            context: {form: readonlyForm, closeButton: readonlyButton},
        }
    );
    readonlyForm.dataset.driverShiftHoldComplete = "true";
    assert.equal(readonlyForm.dispatchEvent(createEvent("submit", readonlyForm)), false);
    assert.equal(readonlyButton.disabled, false);
    assert.equal(binding.source.includes("holdMs: 2000"), true);
});
