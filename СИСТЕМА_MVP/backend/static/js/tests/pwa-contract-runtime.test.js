#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const backendRoot = path.resolve(__dirname, "../../..");
const baseTemplatePath = path.join(backendRoot, "templates", "base.html");
const baseTemplate = fs.readFileSync(baseTemplatePath, "utf8");
const driverTemplatePath = path.join(
    backendRoot,
    "templates",
    "users",
    "driver_shift.html"
);
const excavatorTemplatePath = path.join(
    backendRoot,
    "templates",
    "trips",
    "excavator_work.html"
);
const miningMasterTemplatePath = path.join(
    backendRoot,
    "templates",
    "trips",
    "dispatcher_control.html"
);
const driverTemplate = fs.readFileSync(driverTemplatePath, "utf8");
const excavatorTemplate = fs.readFileSync(excavatorTemplatePath, "utf8");
const miningMasterTemplate = fs.readFileSync(
    miningMasterTemplatePath,
    "utf8"
);
const excavatorCssPath = path.join(
    backendRoot,
    "static",
    "css",
    "excavator-work-v55-shift.css"
);
const excavatorCss = fs.readFileSync(excavatorCssPath, "utf8");
const guardStartMarker = "/* APP_PWA_CONTRACT_GUARD_START */";
const guardEndMarker = "/* APP_PWA_CONTRACT_GUARD_END */";

function extractGuardSource() {
    const startIndex = baseTemplate.indexOf(guardStartMarker);
    const endIndex = baseTemplate.indexOf(guardEndMarker);
    if (
        startIndex < 0
        || endIndex < 0
        || endIndex <= startIndex
    ) {
        return null;
    }
    return baseTemplate.slice(
        startIndex + guardStartMarker.length,
        endIndex
    );
}

const guardSource = extractGuardSource();
const guardUnavailable = !guardSource;

class EventTargetStub {
    constructor() {
        this.listeners = new Map();
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    removeEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        this.listeners.set(
            type,
            listeners.filter((candidate) => candidate !== listener)
        );
    }

    dispatchEvent(event) {
        const nextEvent = event || {};
        if (!nextEvent.type) {
            throw new Error("Event type is required");
        }
        if (!nextEvent.target) {
            nextEvent.target = this;
        }
        (this.listeners.get(nextEvent.type) || [])
            .slice()
            .forEach((listener) => listener.call(this, nextEvent));
        return !nextEvent.defaultPrevented;
    }

    listenerCount(type) {
        return (this.listeners.get(type) || []).length;
    }
}

class ClassListStub {
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
        const enabled = typeof force === "boolean"
            ? force
            : !this.values.has(name);
        if (enabled) {
            this.values.add(name);
        } else {
            this.values.delete(name);
        }
        return enabled;
    }
}

function datasetKey(attributeName) {
    return attributeName
        .slice(5)
        .replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

class ElementStub extends EventTargetStub {
    constructor(nodeName = "div", attributes = {}) {
        super();
        this.nodeName = nodeName.toUpperCase();
        this.attributes = new Map();
        this.dataset = {};
        this.classList = new ClassListStub();
        this.children = [];
        this.parentNode = null;
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.innerHTML = "";
        this.title = "";
        this.style = {
            setProperty() {},
            removeProperty() {},
        };
        Object.entries(attributes).forEach(([name, value]) => {
            this.setAttribute(name, value);
        });
    }

    get isConnected() {
        let current = this;
        while (current) {
            if (current.nodeName === "HTML") {
                return true;
            }
            current = current.parentNode;
        }
        return false;
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

    toggleAttribute(name, force) {
        const enabled = typeof force === "boolean"
            ? force
            : !this.hasAttribute(name);
        if (enabled) {
            this.setAttribute(name, "");
        } else {
            this.removeAttribute(name);
        }
        return enabled;
    }

    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    prepend(child) {
        child.parentNode = this;
        this.children.unshift(child);
        return child;
    }

    remove() {
        if (!this.parentNode) {
            return;
        }
        this.parentNode.children = this.parentNode.children.filter(
            (child) => child !== this
        );
        this.parentNode = null;
    }

    focus() {}

    closest(selector) {
        let current = this;
        while (current) {
            if (matchesSelector(current, selector)) {
                return current;
            }
            current = current.parentNode;
        }
        return null;
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    querySelectorAll(selector) {
        const matches = [];
        function visit(node) {
            if (matchesSelector(node, selector)) {
                matches.push(node);
            }
            node.children.forEach(visit);
        }
        this.children.forEach(visit);
        return matches;
    }
}

function matchesSelector(element, selector) {
    if (!selector || !element) {
        return false;
    }
    if (selector.startsWith("#")) {
        return element.getAttribute("id") === selector.slice(1);
    }
    const dataMatch = selector.match(/^\[([a-zA-Z0-9_-]+)(?:="([^"]*)")?\]$/);
    if (dataMatch) {
        if (!element.hasAttribute(dataMatch[1])) {
            return false;
        }
        return typeof dataMatch[2] === "undefined"
            || element.getAttribute(dataMatch[1]) === dataMatch[2];
    }
    return element.nodeName.toLowerCase() === selector.toLowerCase();
}

class DocumentStub extends EventTargetStub {
    constructor(dataset) {
        super();
        this.readyState = "complete";
        this.visibilityState = "visible";
        this.documentElement = new ElementStub("html");
        this.body = new ElementStub("body");
        this.documentElement.appendChild(this.body);
        Object.entries(dataset).forEach(([key, value]) => {
            this.body.dataset[key] = String(value);
            const attributeName = `data-${key.replace(
                /[A-Z]/g,
                (letter) => `-${letter.toLowerCase()}`
            )}`;
            this.body.setAttribute(attributeName, value);
        });
    }

    createElement(nodeName) {
        return new ElementStub(nodeName);
    }

    getElementById(id) {
        return this.documentElement.querySelector(`#${id}`);
    }

    querySelector(selector) {
        if (matchesSelector(this.body, selector)) {
            return this.body;
        }
        return this.documentElement.querySelector(selector);
    }

    querySelectorAll(selector) {
        const matches = this.documentElement.querySelectorAll(selector);
        if (matchesSelector(this.body, selector)) {
            matches.unshift(this.body);
        }
        return matches;
    }
}

class StorageStub {
    constructor(initialValues = {}) {
        this.values = new Map(Object.entries(initialValues));
    }

    getItem(key) {
        return this.values.has(key) ? this.values.get(key) : null;
    }

    setItem(key, value) {
        this.values.set(key, String(value));
    }

    removeItem(key) {
        this.values.delete(key);
    }

    clear() {
        this.values.clear();
    }
}

class CustomEventStub {
    constructor(type, options = {}) {
        this.type = type;
        this.detail = options.detail;
        this.defaultPrevented = false;
    }

    preventDefault() {
        this.defaultPrevented = true;
    }

    stopImmediatePropagation() {}

    stopPropagation() {}
}

function createMessageChannel() {
    const port1 = {
        onmessage: null,
        postMessage(message) {
            if (typeof port2.onmessage === "function") {
                port2.onmessage({data: message});
            }
        },
        start() {},
        close() {},
    };
    const port2 = {
        onmessage: null,
        postMessage(message) {
            if (typeof port1.onmessage === "function") {
                port1.onmessage({data: message});
            }
        },
        start() {},
        close() {},
    };
    return {port1, port2};
}

function createRuntime(options = {}) {
    const htmlContractVersion = options.htmlContractVersion || "contract-v2";
    const htmlShellVersion = options.htmlShellVersion || "driver-shell-v2";
    const roleCode = options.roleCode || "driver";
    const locationOrigin = options.locationOrigin || "https://driver.localhost";
    const locationHref = options.locationHref || `${locationOrigin}/driver/`;
    const locationPathname = options.locationPathname || "/driver/";
    const serviceWorkerUrl = options.serviceWorkerUrl || "/driver-service-worker.js";
    const serviceWorkerScope = options.serviceWorkerScope || "/driver/";
    const document = new DocumentStub({
        appContractVersion: htmlContractVersion,
        appShellVersion: htmlShellVersion,
        appRoleCode: roleCode,
        appContractReady: "false",
        appServiceWorkerUrl: serviceWorkerUrl,
        appServiceWorkerScope: serviceWorkerScope,
    });
    let unfinishedActionActive = Boolean(options.unfinishedAction);
    if (options.unfinishedAction) {
        const unfinishedAction = new ElementStub("article", {
            "data-active-trip-id": "42",
            "data-trip-status": "loaded_waiting_unload",
        });
        document.body.appendChild(unfinishedAction);
    }
    const windowTarget = new EventTargetStub();
    const serviceWorkerTarget = new EventTargetStub();
    const registrationTarget = new EventTargetStub();
    const waitingWorkerMessages = [];
    const activeWorkerMessages = [];
    const fetchRequests = [];
    const timers = [];
    const scheduledTimerDelays = [];
    const mutationObservers = [];
    let reloadCalls = 0;
    let updateCalls = 0;
    let registerCalls = 0;
    const registerRequests = [];
    let registerFailuresRemaining = Number(options.registerFailures || 0);
    let updateFailuresRemaining = Number(options.updateFailures || 0);
    let registrationAvailable = options.registrationAvailable !== false;
    const deferredRegistrationResolvers = [];
    const deferredUpdateResolvers = [];
    const activeWorkerVersionResolvers = [];
    const waitingWorkerVersionResolvers = [];
    const replacementWorkerVersionResolvers = [];
    const replacementWorkerMessages = [];
    let online = options.online !== false;

    function workerVersionPayload(contractVersion, shellVersion) {
        return {
            appContractVersion: contractVersion,
            shellVersion,
            roleCode,
        };
    }

    function respondWithWorkerVersion(transfer, payload, resolvers, deferred) {
        if (!transfer || !transfer[0]) return;
        const respond = () => transfer[0].postMessage(payload);
        if (deferred) {
            resolvers.push(respond);
        } else {
            respond();
        }
    }

    const waitingWorker = Object.assign(new EventTargetStub(), {
        state: "installed",
        postMessage(message, transfer) {
            waitingWorkerMessages.push(message);
            if (message && message.type === "GET_VERSION") {
                respondWithWorkerVersion(
                    transfer,
                    workerVersionPayload(
                        options.waitingWorkerContractVersion
                            || options.workerContractVersion
                            || htmlContractVersion,
                        options.waitingWorkerShellVersion
                            || options.workerShellVersion
                            || htmlShellVersion
                    ),
                    waitingWorkerVersionResolvers,
                    Boolean(options.deferWaitingWorkerVersion)
                );
            }
        },
    });
    const activeWorker = {
        state: "activated",
        scriptURL: new URL(
            options.registrationWorkerUrl || serviceWorkerUrl,
            locationHref
        ).href,
        postMessage(message, transfer) {
            activeWorkerMessages.push(message);
            if (
                message
                && message.type === "GET_VERSION"
                && transfer
                && transfer[0]
            ) {
                respondWithWorkerVersion(
                    transfer,
                    workerVersionPayload(
                        options.workerContractVersion || htmlContractVersion,
                        options.workerShellVersion || htmlShellVersion
                    ),
                    activeWorkerVersionResolvers,
                    Boolean(options.deferActiveWorkerVersion)
                );
            }
        },
    };
    const replacementWorker = {
        state: "activated",
        postMessage(message, transfer) {
            replacementWorkerMessages.push(message);
            if (
                message
                && message.type === "GET_VERSION"
                && transfer
                && transfer[0]
            ) {
                if (options.replacementWorkerNoResponse) return;
                respondWithWorkerVersion(
                    transfer,
                    workerVersionPayload(
                        options.replacementWorkerContractVersion
                            || htmlContractVersion,
                        options.replacementWorkerShellVersion
                            || htmlShellVersion
                    ),
                    replacementWorkerVersionResolvers,
                    Boolean(options.deferReplacementWorkerVersion)
                );
            }
        },
    };
    const registration = Object.assign(registrationTarget, {
        scope: new URL(
            options.registrationScope || serviceWorkerScope,
            locationHref
        ).href,
        waiting: options.hasWaitingWorker === false ? null : waitingWorker,
        installing: null,
        active: activeWorker,
        update() {
            updateCalls += 1;
            if (updateFailuresRemaining > 0) {
                updateFailuresRemaining -= 1;
                return Promise.reject(new Error("transient update failure"));
            }
            if (options.deferUpdates) {
                return new Promise((resolve) => {
                    deferredUpdateResolvers.push(() => resolve(registration));
                });
            }
            return Promise.resolve(registration);
        },
    });
    const serviceWorker = Object.assign(serviceWorkerTarget, {
        controller: activeWorker,
        ready: Promise.resolve(registration),
        getRegistration() {
            return Promise.resolve(registrationAvailable ? registration : null);
        },
        getRegistrations() {
            return Promise.resolve([registration]);
        },
        register(workerUrl, registerOptions = {}) {
            registerCalls += 1;
            registerRequests.push({
                workerUrl: String(workerUrl),
                scope: String(registerOptions.scope || ""),
                updateViaCache: String(registerOptions.updateViaCache || ""),
            });
            if (registerFailuresRemaining > 0) {
                registerFailuresRemaining -= 1;
                return Promise.reject(new Error("transient registration failure"));
            }
            if (options.deferRegistration) {
                return new Promise((resolve) => {
                    deferredRegistrationResolvers.push(() => {
                        registrationAvailable = true;
                        resolve(registration);
                    });
                });
            }
            registration.scope = new URL(
                registerOptions.scope || serviceWorkerScope,
                locationHref
            ).href;
            activeWorker.scriptURL = new URL(workerUrl, locationHref).href;
            registrationAvailable = true;
            return Promise.resolve(registration);
        },
    });
    const navigator = {
        serviceWorker,
        get onLine() {
            return online;
        },
        set onLine(value) {
            online = Boolean(value);
        },
    };
    class MutationObserverStub {
        constructor(callback) {
            this.callback = callback;
            this.connected = false;
        }

        observe() {
            this.connected = true;
            mutationObservers.push(this);
        }

        disconnect() {
            this.connected = false;
        }
    }
    const location = {
        origin: locationOrigin,
        href: locationHref,
        pathname: locationPathname,
        assign(value) {
            this.href = String(value);
        },
        replace(value) {
            this.href = String(value);
        },
        reload() {
            reloadCalls += 1;
        },
    };
    const window = Object.assign(windowTarget, {
        document,
        navigator,
        location,
        sessionStorage: new StorageStub(options.sessionStorage),
        localStorage: new StorageStub(),
        CustomEvent: CustomEventStub,
        Event: CustomEventStub,
        Promise,
        URL,
        console,
        MutationObserver: MutationObserverStub,
        AppRealtimeConfig: {
            stateUrl: "/api/operational-state/version/",
        },
    });
    const runtime = {
        document,
        window,
        navigator,
        registration,
        activeWorker,
        waitingWorker,
        replacementWorker,
        waitingWorkerMessages,
        activeWorkerMessages,
        replacementWorkerMessages,
        fetchRequests,
        scheduledTimerDelays,
        get reloadCalls() {
            return reloadCalls;
        },
        get updateCalls() {
            return updateCalls;
        },
        get registerCalls() {
            return registerCalls;
        },
        registerRequests,
        resolveRegistration() {
            assert.ok(
                deferredRegistrationResolvers.length,
                "no deferred registration is pending"
            );
            deferredRegistrationResolvers.splice(0).forEach((resolve) => resolve());
        },
        resolveUpdates() {
            assert.ok(
                deferredUpdateResolvers.length,
                "no deferred service-worker update is pending"
            );
            deferredUpdateResolvers.splice(0).forEach((resolve) => resolve());
        },
        resolveActiveWorkerVersions() {
            assert.ok(
                activeWorkerVersionResolvers.length,
                "no deferred active-worker version is pending"
            );
            activeWorkerVersionResolvers.splice(0).forEach((resolve) => resolve());
        },
        resolveWaitingWorkerVersions() {
            assert.ok(
                waitingWorkerVersionResolvers.length,
                "no deferred waiting-worker version is pending"
            );
            waitingWorkerVersionResolvers.splice(0).forEach((resolve) => resolve());
        },
        resolveReplacementWorkerVersions() {
            assert.ok(
                replacementWorkerVersionResolvers.length,
                "no deferred replacement-worker version is pending"
            );
            replacementWorkerVersionResolvers.splice(0).forEach((resolve) => resolve());
        },
        requestWaitingWorkerVersion() {
            registration.installing = waitingWorker;
            registration.waiting = waitingWorker;
            registration.dispatchEvent(new CustomEventStub("updatefound"));
            waitingWorker.state = "installed";
            waitingWorker.dispatchEvent(new CustomEventStub("statechange"));
        },
        activateReplacementWorker() {
            registration.active = replacementWorker;
            registration.waiting = null;
            registration.installing = null;
            serviceWorker.controller = replacementWorker;
            serviceWorker.dispatchEvent(new CustomEventStub("controllerchange"));
        },
        listenerCount(type) {
            return serviceWorker.listenerCount(type);
        },
        setOnline(value) {
            online = Boolean(value);
            navigator.onLine = online;
        },
        finishUnfinishedAction() {
            unfinishedActionActive = false;
            document.body.children.slice().forEach((child) => {
                if (child.hasAttribute("data-active-trip-id")) {
                    child.remove();
                }
            });
        },
        notifyMutation(mutation) {
            mutationObservers
                .filter((observer) => observer.connected)
                .forEach((observer) => observer.callback([mutation]));
        },
        runTimers(limit = 100) {
            let runs = 0;
            while (timers.length && runs < limit) {
                const timer = timers.shift();
                if (!timer.cancelled) {
                    timer.callback();
                }
                runs += 1;
            }
            if (timers.length) {
                throw new Error("PWA guard left a runaway timer loop");
            }
        },
    };
    let nextTimerId = 1;
    function setTimeoutStub(callback, delay) {
        const timer = {
            id: nextTimerId,
            callback,
            delay: Number(delay || 0),
            cancelled: false,
        };
        nextTimerId += 1;
        timers.push(timer);
        scheduledTimerDelays.push(timer.delay);
        return timer.id;
    }
    function clearTimeoutStub(timerId) {
        const timer = timers.find((candidate) => candidate.id === timerId);
        if (timer) {
            timer.cancelled = true;
        }
    }
    async function fetchStub(url, init = {}) {
        fetchRequests.push({url: String(url), init});
        if (!online || !options.serverPayload) {
            throw new Error("offline");
        }
        return {
            ok: true,
            status: 200,
            json: async () => options.serverPayload,
        };
    }

    const context = {
        window,
        self: window,
        document,
        navigator,
        location,
        sessionStorage: window.sessionStorage,
        localStorage: window.localStorage,
        CustomEvent: CustomEventStub,
        Event: CustomEventStub,
        MessageChannel: function MessageChannelStub() {
            const channel = createMessageChannel();
            this.port1 = channel.port1;
            this.port2 = channel.port2;
        },
        Promise,
        URL,
        console,
        MutationObserver: MutationObserverStub,
        fetch: fetchStub,
        setTimeout: setTimeoutStub,
        clearTimeout: clearTimeoutStub,
        setInterval: setTimeoutStub,
        clearInterval: clearTimeoutStub,
    };
    window.fetch = fetchStub;
    window.setTimeout = setTimeoutStub;
    window.clearTimeout = clearTimeoutStub;
    window.setInterval = setTimeoutStub;
    window.clearInterval = clearTimeoutStub;
    window.MessageChannel = context.MessageChannel;

    vm.runInNewContext(guardSource, context, {
        filename: "templates/base.html::AppPwaContractGuard",
    });
    document.dispatchEvent(new CustomEventStub("DOMContentLoaded"));
    window.dispatchEvent(new CustomEventStub("load"));
    runtime.guard = window.AppPwaContractGuard;
    assert.ok(
        runtime.guard,
        "base.html must publish window.AppPwaContractGuard"
    );
    runtime.guard.registerUnsafeCheck(() => unfinishedActionActive);
    return runtime;
}

async function flushRuntime(runtime) {
    for (let index = 0; index < 8; index += 1) {
        await Promise.resolve();
    }
    runtime.runTimers();
    for (let index = 0; index < 4; index += 1) {
        await Promise.resolve();
    }
}

function observedState(runtime) {
    const state = runtime.guard.getState();
    const readyValue = typeof state.ready !== "undefined"
        ? state.ready
        : runtime.document.body.dataset.appContractReady;
    const lockedValue = typeof state.locked !== "undefined"
        ? state.locked
        : runtime.document.body.dataset.appContractLocked;
    const reloadCount = typeof state.reloadCount !== "undefined"
        ? Number(state.reloadCount)
        : runtime.reloadCalls;
    return {
        state,
        ready: readyValue === true || readyValue === "true",
        locked: lockedValue === true || lockedValue === "true",
        reloadCount,
    };
}

function assertContractState(runtime, expected) {
    const observed = observedState(runtime);
    assert.equal(observed.ready, expected.ready, observed.state);
    assert.equal(observed.locked, expected.locked, observed.state);
    assert.equal(
        runtime.document.body.dataset.appContractReady,
        expected.ready ? "true" : "false"
    );
    return observed;
}

async function flushPromises(iterations = 12) {
    for (let index = 0; index < iterations; index += 1) {
        await Promise.resolve();
    }
}

function extractNamedIife(source, name) {
    const marker = `(function ${name}() {`;
    const startIndex = source.indexOf(marker);
    assert.ok(startIndex >= 0, `missing ${marker}`);
    const endMarker = "\n    })();";
    const endIndex = source.indexOf(endMarker, startIndex);
    assert.ok(endIndex > startIndex, `missing closing IIFE for ${name}`);
    return source.slice(startIndex, endIndex + endMarker.length);
}

function createRolePwaRuntime(options = {}) {
    const serviceWorkerTarget = new EventTargetStub();
    const registrationTarget = new EventTargetStub();
    const fetchRequests = [];
    const waitingWorkerMessages = [];
    let updateCalls = 0;
    let registerCalls = 0;
    let reloadCalls = 0;
    const manualUpdateResolvers = [];
    const waitingVersionResolvers = [];
    let deferManualUpdates = false;

    const activeWorker = {
        state: "activated",
        postMessage(message, transfer) {
            if (
                message
                && message.type === "GET_VERSION"
                && transfer
                && transfer[0]
            ) {
                transfer[0].postMessage({
                    version: options.activeShellVersion || options.shellVersion,
                    appContractVersion: options.contractVersion || "contract-v2",
                    shellVersion: options.activeShellVersion || options.shellVersion,
                    roleCode: options.roleCode,
                });
            }
        },
    };
    const waitingWorker = {
        state: "installed",
        postMessage(message, transfer) {
            waitingWorkerMessages.push(message);
            if (
                message
                && message.type === "GET_VERSION"
                && transfer
                && transfer[0]
            ) {
                const respond = () => transfer[0].postMessage({
                    version: options.nextShellVersion || options.shellVersion,
                });
                if (options.deferWaitingVersion) {
                    waitingVersionResolvers.push(respond);
                } else {
                    respond();
                }
            }
        },
        addEventListener() {},
    };
    const registration = Object.assign(registrationTarget, {
        active: activeWorker,
        waiting: options.hasWaitingWorker ? waitingWorker : null,
        installing: null,
        update() {
            updateCalls += 1;
            if (!deferManualUpdates) {
                return Promise.resolve(registration);
            }
            return new Promise((resolve) => {
                manualUpdateResolvers.push(() => resolve(registration));
            });
        },
    });
    const serviceWorker = Object.assign(serviceWorkerTarget, {
        controller: activeWorker,
        ready: Promise.resolve(registration),
        getRegistration() {
            return Promise.resolve(registration);
        },
        register() {
            registerCalls += 1;
            return Promise.resolve(registration);
        },
    });
    const navigator = {serviceWorker};
    const windowTarget = new EventTargetStub();
    const document = new DocumentStub({});
    const nodes = new Map();
    const originalDocumentQuerySelector = document.querySelector.bind(document);
    document.querySelector = function querySelector(selector) {
        return nodes.get(selector) || originalDocumentQuerySelector(selector);
    };
    document.querySelectorAll = function querySelectorAll(selector) {
        const node = nodes.get(selector);
        return node ? [node] : [];
    };
    const timers = [];
    let nextTimerId = 1;
    function setTimeoutStub(callback, delay) {
        const timer = {
            id: nextTimerId,
            callback,
            delay: Number(delay || 0),
            cancelled: false,
        };
        nextTimerId += 1;
        timers.push(timer);
        return timer.id;
    }
    function clearTimeoutStub(timerId) {
        const timer = timers.find((candidate) => candidate.id === timerId);
        if (timer) timer.cancelled = true;
    }
    async function fetchStub(url, init = {}) {
        fetchRequests.push({url: String(url), init});
        return {
            ok: true,
            status: 200,
            text: async () => options.shellVersion,
            json: async () => ({
                role_shell_version: options.shellVersion,
                role_app_code: options.roleCode,
                app_contract_version: options.contractVersion || "contract-v2",
            }),
        };
    }
    const window = Object.assign(windowTarget, {
        document,
        navigator,
        location: {
            reload() {
                reloadCalls += 1;
            },
        },
        localStorage: new StorageStub(),
        sessionStorage: new StorageStub(),
        MessageChannel: function MessageChannelStub() {
            const channel = createMessageChannel();
            this.port1 = channel.port1;
            this.port2 = channel.port2;
        },
        AppPwaContractGuard: {
            getState() {
                return {
                    ready: true,
                    locked: false,
                    server: {
                        appContractVersion: options.contractVersion || "contract-v2",
                        shellVersion: options.shellVersion,
                        roleCode: options.roleCode,
                    },
                    serviceWorker: {
                        appContractVersion: options.contractVersion || "contract-v2",
                        shellVersion: options.shellVersion,
                        roleCode: options.roleCode,
                    },
                };
            },
        },
        Promise,
        Date,
        console,
        setTimeout: setTimeoutStub,
        clearTimeout: clearTimeoutStub,
        setInterval: setTimeoutStub,
        clearInterval: clearTimeoutStub,
        fetch: fetchStub,
    });
    const context = vm.createContext({
        window,
        self: window,
        document,
        navigator,
        location: window.location,
        localStorage: window.localStorage,
        sessionStorage: window.sessionStorage,
        MessageChannel: window.MessageChannel,
        Promise,
        Date,
        console,
        setTimeout: setTimeoutStub,
        clearTimeout: clearTimeoutStub,
        setInterval: setTimeoutStub,
        clearInterval: clearTimeoutStub,
        fetch: fetchStub,
    });
    return {
        context,
        document,
        window,
        navigator,
        registration,
        waitingWorker,
        waitingWorkerMessages,
        fetchRequests,
        nodes,
        get updateCalls() {
            return updateCalls;
        },
        get registerCalls() {
            return registerCalls;
        },
        get reloadCalls() {
            return reloadCalls;
        },
        listenerCount(type) {
            return serviceWorker.listenerCount(type);
        },
        deferManualUpdates() {
            deferManualUpdates = true;
        },
        resolveManualUpdate() {
            assert.ok(manualUpdateResolvers.length, "no deferred manual update");
            manualUpdateResolvers.splice(0).forEach((resolve) => resolve());
            deferManualUpdates = false;
        },
        resolveWaitingVersion() {
            assert.ok(waitingVersionResolvers.length, "no deferred waiting-worker version");
            waitingVersionResolvers.splice(0).forEach((resolve) => resolve());
        },
        promoteWaitingWorker() {
            registration.active = waitingWorker;
            registration.waiting = null;
            serviceWorker.controller = waitingWorker;
        },
        addNode(selector, node = new ElementStub("button")) {
            nodes.set(selector, node);
            return node;
        },
    };
}

function executeDriverPwaBinding(runtime, shellVersion) {
    const source = extractNamedIife(driverTemplate, "initDriverPwaUpdates");
    runtime.context.shell = {
        dataset: {
            driverPwaVersion: shellVersion,
        },
    };
    vm.runInContext(source, runtime.context, {
        filename: "templates/users/driver_shift.html::initDriverPwaUpdates",
    });
}

function extractExcavatorPwaSource() {
    const startMarker = "function formatExcavatorVersion(version) {";
    const endMarker = "\ndocument.addEventListener(\"DOMContentLoaded\", function () {";
    const startIndex = excavatorTemplate.indexOf(startMarker);
    const endIndex = excavatorTemplate.indexOf(endMarker, startIndex);
    assert.ok(startIndex >= 0, `missing ${startMarker}`);
    assert.ok(endIndex > startIndex, "missing Excavator PWA runtime terminator");
    return excavatorTemplate
        .slice(startIndex, endIndex)
        .replaceAll(
            "{% if role_app.role_code == 'excavator_operator' %}/{% else %}/excavator/{% endif %}",
            "/excavator/"
        );
}

test("base.html publishes PWA contract datasets and executable guard markers", () => {
    assert.ok(
        guardSource,
        `missing ${guardStartMarker} / ${guardEndMarker} in base.html`
    );
    assert.match(baseTemplate, /data-app-contract-version="/);
    assert.match(baseTemplate, /data-app-shell-version="/);
    assert.match(baseTemplate, /data-app-role-code="/);
    assert.match(baseTemplate, /data-app-contract-ready="false"/);
});

test("role controller listeners only reconcile stale UI while base owns contract reload", () => {
    assert.match(
        driverTemplate,
        /registerUnsafeCheck\(isDriverOperationalRefreshUnsafe\)/
    );
    assert.match(
        excavatorTemplate,
        /registerUnsafeCheck\(isExcavatorRefreshUnsafe\)/
    );
    assert.match(
        miningMasterTemplate,
        /registerUnsafeCheck\(isMobileOperationalRefreshUnsafe\)/
    );
    [
        ["Driver", driverTemplate, 0],
        ["Excavator", excavatorTemplate, 1],
        ["Mining Master", miningMasterTemplate, 1],
    ].forEach(([roleName, source, expectedBindings]) => {
        const bindings = source.match(
            /navigator\.serviceWorker\.addEventListener\("controllerchange",\s*[^)]+\)/g
        ) || [];
        assert.equal(
            bindings.length,
            expectedBindings,
            `${roleName} must have only its expected UI reconciliation listener`
        );
        bindings.forEach((binding) => {
            assert.match(
                binding,
                /,\s*(?:syncContractState|syncMiningMasterPwaContractState)\s*\)$/,
                `${roleName} controllerchange may only invoke the stale-modal UI reconciler`
            );
            assert.doesNotMatch(
                binding,
                /register|update|reload|acceptServiceWorkerVersion|acceptServerContract/,
                `${roleName} must not compete with the base registration/update/reload owner`
            );
        });
        assert.doesNotMatch(
            source,
            /addEventListener\("controllerchange",\s*(?:function|\()/,
            `${roleName} must not add inline controller contract logic`
        );
    });
});

test("Driver shift manual update delegates to the shared guarded single-flight path", () => {
    const start = driverTemplate.indexOf(
        "runtime.requestManualUpdate = function ()"
    );
    const end = driverTemplate.indexOf(
        'window.addEventListener("app-pwa-contract-state"',
        start
    );
    assert.ok(start >= 0 && end > start, "Driver update handler was not found");
    const handler = driverTemplate.slice(start, end);
    assert.match(handler, /AppPwaContractGuard/);
    assert.match(
        handler,
        /guard\s*&&\s*typeof guard\.requestManualUpdate === "function"[\s\S]*?guard\.requestManualUpdate\(\)/
    );
    assert.match(handler, /if \(runtime\.applyPromise\) return runtime\.applyPromise/);
    assert.match(handler, /актуальная версия|Не удалось проверить обновление/i);
});

test("Excavator recovery banner is viewport-bound, safe-area aware and one-row", () => {
    assert.doesNotMatch(
        excavatorCss,
        /--eo-contract-banner-reserve/,
        "a floating recovery notice must not add an 80px workstation reflow reserve"
    );
    assert.doesNotMatch(
        excavatorCss,
        /\.app-contract-banner::before/,
        "the message must be real accessible DOM, not a generated grid row"
    );
    const start = excavatorCss.indexOf(
        "body.excavator-operator-screen .app-contract-banner {"
    );
    const end = excavatorCss.indexOf("}", start);
    assert.ok(start >= 0 && end > start, "the Excavator banner rule is missing");
    const rule = excavatorCss.slice(start, end);
    assert.match(rule, /left:\s*max\(8px,\s*env\(safe-area-inset-left,\s*0px\)\)\s*!important/);
    assert.match(rule, /right:\s*max\(8px,\s*env\(safe-area-inset-right,\s*0px\)\)\s*!important/);
    assert.match(rule, /transform:\s*none\s*!important/);
    assert.match(rule, /width:\s*auto\s*!important/);
    assert.match(rule, /max-width:\s*calc\(100vw - 16px\)\s*!important/);
    assert.match(rule, /grid-template-columns:\s*auto minmax\(0,\s*1fr\) auto\s*!important/);
    assert.match(rule, /grid-template-rows:\s*auto\s*!important/);
    assert.match(rule, /max-height:\s*none\s*!important/);
    assert.match(rule, /overflow:\s*visible\s*!important/);
    assert.match(rule, /-webkit-text-size-adjust:\s*100%\s*!important/);
    assert.match(rule, /text-size-adjust:\s*100%\s*!important/);
    assert.match(excavatorCss, /white-space:\s*nowrap/);
    const compactStart = excavatorCss.indexOf("@media (max-width: 480px)", end);
    const compactEnd = excavatorCss.indexOf("@media (orientation: landscape)", compactStart);
    assert.ok(compactStart >= 0 && compactEnd > compactStart, "the 360px compact banner fallback is missing");
    const compactRule = excavatorCss.slice(compactStart, compactEnd);
    assert.match(compactRule, /grid-template-columns:\s*auto minmax\(0,\s*1fr\)\s*!important/);
    assert.match(compactRule, /grid-template-rows:\s*auto auto\s*!important/);
    assert.match(compactRule, /white-space:\s*normal\s*!important/);
    assert.match(compactRule, /overflow-wrap:\s*anywhere/);
    assert.match(compactRule, /\[data-app-contract-retry\][\s\S]*?grid-row:\s*2\s*!important/);
});

test("shared contract banner contains long recovery text on narrow role screens", () => {
    const baseStyleStart = baseTemplate.indexOf(".app-contract-banner {");
    const compactStart = baseTemplate.indexOf(
        "@media (max-width: 520px), (max-height: 520px)",
        baseStyleStart
    );
    const compactEnd = baseTemplate.indexOf("@keyframes app-contract-spin", compactStart);
    assert.ok(baseStyleStart >= 0, "the shared contract banner rule is missing");
    assert.ok(compactStart > baseStyleStart && compactEnd > compactStart,
        "the shared compact banner fallback is missing");

    const baseRule = baseTemplate.slice(baseStyleStart, compactStart);
    const compactRule = baseTemplate.slice(compactStart, compactEnd);
    assert.match(baseRule, /display:\s*flex/);
    assert.match(baseRule, /max-width:\s*min\(420px,\s*calc\(100vw - 32px\)\)/);
    assert.match(baseRule, /white-space:\s*normal/);
    assert.match(baseRule, /overflow-wrap:\s*anywhere/);
    assert.match(compactRule, /left:\s*max\(8px,\s*env\(safe-area-inset-left,\s*0px\)\)/);
    assert.match(compactRule, /right:\s*max\(8px,\s*env\(safe-area-inset-right,\s*0px\)\)/);
    assert.match(compactRule, /transform:\s*none/);
    assert.match(compactRule, /grid-template-columns:\s*auto minmax\(0,\s*1fr\)/);
    assert.match(compactRule, /white-space:\s*normal/);
    assert.match(compactRule, /overflow-wrap:\s*anywhere/);
    assert.match(compactRule, /\[data-app-contract-retry\][\s\S]*?grid-row:\s*2/);
});

test(
    "quick initial verification keeps the generic banner hidden but locks mutations immediately",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({hasWaitingWorker: false});
        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(runtime.document.querySelector("[data-app-contract-banner]"), null);

        const mutation = new ElementStub("button", {type: "button"});
        runtime.document.body.appendChild(mutation);
        const blockedClick = new CustomEventStub("click");
        blockedClick.target = mutation;
        runtime.document.dispatchEvent(blockedClick);
        assert.equal(blockedClick.defaultPrevented, true);

        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        assertContractState(runtime, {ready: true, locked: false});
        await flushRuntime(runtime);
        assert.equal(
            runtime.document.querySelector("[data-app-contract-banner]"),
            null,
            "a verification completed inside the debounce window must never flash a banner"
        );
    }
);

test(
    "prolonged contract lock exposes a banner and non-secret diagnostics",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            hasWaitingWorker: false,
        });
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        assert.equal(runtime.document.querySelector("[data-app-contract-banner]"), null);
        await flushRuntime(runtime);

        const banner = runtime.document.querySelector("[data-app-contract-banner]");
        assert.ok(banner, "a lock that outlives the debounce must remain visibly explained");
        assert.equal(
            banner.querySelector("[data-app-contract-message]").textContent,
            "Проверяем рабочий экран…"
        );
        assert.equal(runtime.document.body.dataset.appContractExpected, "contract-v2:driver:driver-shell-v2");
        assert.equal(runtime.document.body.dataset.appContractJavascript, "pending");
        assert.equal(runtime.document.body.dataset.appContractServiceWorker, "contract-v2:driver:driver-shell-v2");
        assert.equal(runtime.document.body.dataset.appContractServer, "contract-v2:driver:driver-shell-v2");
        assert.match(runtime.document.body.dataset.appContractLockReason, /javascript-pending/);
        assert.equal(runtime.guard.getState().locked, true);
    }
);

test(
    "generic banner follows a role update modal opening and closing without duplicates",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({hasWaitingWorker: false});
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);
        assert.ok(runtime.document.querySelector("[data-app-contract-banner]"));

        const roleModal = new ElementStub("div", {
            "data-eo-pwa-update-modal": "",
        });
        roleModal.hidden = false;
        runtime.document.body.appendChild(roleModal);
        runtime.notifyMutation({
            type: "childList",
            target: runtime.document.body,
            addedNodes: [roleModal],
            removedNodes: [],
        });
        assert.equal(
            runtime.document.querySelector("[data-app-contract-banner]"),
            null,
            "a visible role modal must replace the generic notice"
        );

        roleModal.hidden = true;
        runtime.notifyMutation({
            type: "attributes",
            attributeName: "hidden",
            target: roleModal,
        });
        assert.equal(
            runtime.document.querySelector("[data-app-contract-banner]"),
            null,
            "the generic notice must still honour its debounce after closing a modal"
        );
        runtime.runTimers();
        assert.equal(
            runtime.document.body.children.filter(
                (node) => node.hasAttribute("data-app-contract-banner")
            ).length,
            1,
            "closing a role modal must restore exactly one generic notice"
        );

        roleModal.hidden = false;
        runtime.notifyMutation({
            type: "attributes",
            attributeName: "hidden",
            target: roleModal,
        });
        roleModal.hidden = true;
        runtime.notifyMutation({
            type: "attributes",
            attributeName: "hidden",
            target: roleModal,
        });
        runtime.runTimers();
        assert.equal(
            runtime.document.body.children.filter(
                (node) => node.hasAttribute("data-app-contract-banner")
            ).length,
            1,
            "repeated modal visibility changes must not duplicate the banner"
        );
    }
);

test(
    "a ready contract clears its completed flight so the same contract can relock and update again",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            workerShellVersion: "driver-shell-v1",
            hasWaitingWorker: false,
        });
        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v1",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        await flushPromises(24);
        assert.equal(runtime.updateCalls, 1, "the initial same-contract mismatch must update once");

        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        assertContractState(runtime, {ready: true, locked: false});
        assert.equal(runtime.guard.getState().update.attempt, 0);

        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v1",
            roleCode: "driver",
        });
        await flushPromises(24);
        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(
            runtime.updateCalls,
            2,
            "a later mismatch for the same contract must start a fresh update flight"
        );
    }
);

test(
    "hanging registration.update is timed out, retried with bounded backoff, then exposes Retry",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            workerShellVersion: "driver-shell-v1",
            hasWaitingWorker: false,
            deferUpdates: true,
            online: false,
        });
        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v1",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });

        for (let pass = 0; pass < 10; pass += 1) {
            await flushRuntime(runtime);
        }
        const exhausted = runtime.guard.getState();
        assert.equal(runtime.updateCalls, 3, "the automatic sequence must be bounded at three attempts");
        assert.equal(exhausted.update.exhausted, true);
        assert.equal(exhausted.update.state, "recovery-required");
        assert.equal(exhausted.update.failure, "update-timeout");
        assert.ok(runtime.scheduledTimerDelays.includes(700), "the first retry backoff is missing");
        assert.ok(runtime.scheduledTimerDelays.includes(1800), "the second retry backoff is missing");
        assert.equal(
            runtime.scheduledTimerDelays.filter((delay) => delay === 8000).length,
            3,
            "every hanging update attempt must have its own timeout"
        );

        const banner = runtime.document.querySelector("[data-app-contract-banner]");
        const retry = banner && banner.querySelector("[data-app-contract-retry]");
        const spinner = banner && banner.querySelector("[data-app-contract-spinner]");
        assert.ok(retry, "the exhausted state must expose an accessible retry control");
        assert.equal(retry.hidden, false);
        assert.equal(retry.disabled, false);
        assert.equal(spinner.hidden, true);

        const allowedClick = new CustomEventStub("click");
        allowedClick.target = retry;
        runtime.document.dispatchEvent(allowedClick);
        assert.equal(allowedClick.defaultPrevented, false, "the lock must not swallow Retry");

        retry.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();
        assert.equal(runtime.updateCalls, 4, "Retry must start a fresh bounded sequence");
        runtime.resolveUpdates();
        await flushPromises();
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        assertContractState(runtime, {ready: true, locked: false});
        assert.equal(runtime.document.querySelector("[data-app-contract-banner]"), null);
    }
);

test(
    "stable role shells perform no full version_check GET and no automatic update",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            hasWaitingWorker: false,
        });

        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);

        assertContractState(runtime, {ready: true, locked: false});
        assert.equal(
            runtime.updateCalls,
            0,
            "a coherent stable shell must not call registration.update()"
        );
        assert.equal(
            runtime.fetchRequests.filter(
                ({url}) => url.includes("?version_check")
            ).length,
            0
        );
        for (const [roleName, source] of [
            ["Driver", driverTemplate],
            ["Excavator", excavatorTemplate],
            ["Mining Master", miningMasterTemplate],
        ]) {
            assert.equal(
                source.includes("?version_check"),
                false,
                `${roleName} must not load a full service worker for version checks`
            );
        }
    }
);

test(
    "each new role_shell_version causes exactly one controlled update",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            hasWaitingWorker: false,
        });
        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);
        const baselineUpdates = runtime.updateCalls;

        for (let repeat = 0; repeat < 10; repeat += 1) {
            runtime.guard.acceptServerContract({
                app_contract_version: "contract-v2",
                role_shell_version: "driver-shell-v3",
                role_app_code: "driver",
            });
            await flushRuntime(runtime);
        }
        assert.equal(
            runtime.updateCalls - baselineUpdates,
            1,
            "ten observations of one new shell must remain single-flight"
        );

        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v4",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);
        assert.equal(
            runtime.updateCalls - baselineUpdates,
            2,
            "the next distinct shell must cause one new controlled update"
        );
    }
);

test(
    "manual update joins an in-flight automatic update for the same target shell",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            hasWaitingWorker: false,
            deferUpdates: true,
        });
        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        });
        await flushPromises();
        assert.equal(runtime.updateCalls, 1, "automatic update must be in flight");

        const manualResult = runtime.guard.requestManualUpdate();
        await flushPromises();
        assert.equal(
            runtime.updateCalls,
            1,
            "manual update must join the automatic registration.update()"
        );

        runtime.resolveUpdates();
        assert.equal((await manualResult).status, "current");
        assert.equal(runtime.updateCalls, 1);
    }
);

test(
    "controllerchange reads the current controller and ignores a delayed old active response",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v3",
            htmlShellVersion: "driver-shell-v3",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            replacementWorkerContractVersion: "contract-v3",
            replacementWorkerShellVersion: "driver-shell-v3",
            hasWaitingWorker: false,
            deferActiveWorkerVersion: true,
        });
        runtime.guard.registerJavaScript("contract-v3");
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v3",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        });
        await flushPromises();
        assert.ok(
            runtime.activeWorkerMessages.some(
                (message) => message && message.type === "GET_VERSION"
            ),
            "the original active worker must have a delayed version request"
        );

        runtime.activateReplacementWorker();
        await flushPromises();
        assert.equal(
            runtime.replacementWorkerMessages.filter(
                (message) => message && message.type === "GET_VERSION"
            ).length,
            1,
            "controllerchange must query the current controller exactly once"
        );
        assertContractState(runtime, {ready: true, locked: false});
        assert.equal(
            runtime.guard.getState().serviceWorker.shellVersion,
            "driver-shell-v3"
        );

        runtime.resolveActiveWorkerVersions();
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});
        assert.equal(
            runtime.guard.getState().serviceWorker.shellVersion,
            "driver-shell-v3",
            "a delayed old active response must not roll contract state back"
        );
    }
);

test(
    "delayed old waiting response after controllerchange cannot roll contract state back",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v3",
            htmlShellVersion: "driver-shell-v3",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            waitingWorkerContractVersion: "contract-v2",
            waitingWorkerShellVersion: "driver-shell-v2",
            replacementWorkerContractVersion: "contract-v3",
            replacementWorkerShellVersion: "driver-shell-v3",
            deferWaitingWorkerVersion: true,
        });
        runtime.guard.registerJavaScript("contract-v3");
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v3",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);
        runtime.requestWaitingWorkerVersion();
        await flushPromises();
        assert.ok(
            runtime.waitingWorkerMessages.some(
                (message) => message && message.type === "GET_VERSION"
            ),
            "the old waiting worker must have a delayed version request"
        );

        runtime.activateReplacementWorker();
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});
        runtime.resolveWaitingWorkerVersions();
        await flushPromises();

        assertContractState(runtime, {ready: true, locked: false});
        assert.equal(
            runtime.guard.getState().serviceWorker.shellVersion,
            "driver-shell-v3",
            "a delayed old waiting response must not roll contract state back"
        );
    }
);

test(
    "controllerchange locks immediately until the new controller version is verified",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v3",
            htmlShellVersion: "driver-shell-v3",
            workerContractVersion: "contract-v3",
            workerShellVersion: "driver-shell-v3",
            replacementWorkerContractVersion: "contract-v3",
            replacementWorkerShellVersion: "driver-shell-v3",
            hasWaitingWorker: false,
            deferReplacementWorkerVersion: true,
        });
        runtime.guard.registerJavaScript("contract-v3");
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v3",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        });
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});

        runtime.activateReplacementWorker();
        await flushPromises();

        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(
            runtime.guard.getState().serviceWorker,
            null,
            "the previous controller version must be invalidated immediately"
        );
        assert.equal(
            runtime.reloadCalls,
            0,
            "a controller that has not replied must not consume the pending reload"
        );

        runtime.resolveReplacementWorkerVersions();
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});
        assert.equal(runtime.reloadCalls, 0, "a verified matching controller needs no reload");
    }
);

test(
    "controllerchange with no version response cannot inherit the previous ready state",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v3",
            htmlShellVersion: "driver-shell-v3",
            workerContractVersion: "contract-v3",
            workerShellVersion: "driver-shell-v3",
            replacementWorkerNoResponse: true,
            hasWaitingWorker: false,
        });
        runtime.guard.registerJavaScript("contract-v3");
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v3",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        });
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});

        runtime.activateReplacementWorker();
        await flushPromises();

        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(runtime.reloadCalls, 0);
    }
);

test(
    "matching waiting responses cannot unlock an unverified replacement controller",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v3",
            htmlShellVersion: "driver-shell-v3",
            workerContractVersion: "contract-v3",
            workerShellVersion: "driver-shell-v3",
            replacementWorkerContractVersion: "contract-v3",
            replacementWorkerShellVersion: "driver-shell-v3",
            waitingWorkerContractVersion: "contract-v3",
            waitingWorkerShellVersion: "driver-shell-v3",
            deferReplacementWorkerVersion: true,
        });
        runtime.guard.registerJavaScript("contract-v3");
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v3",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        });
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});

        runtime.activateReplacementWorker();
        await flushPromises();
        assertContractState(runtime, {ready: false, locked: true});

        runtime.requestWaitingWorkerVersion();
        await flushPromises();
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "GET_VERSION"
            ).length,
            1
        );
        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(runtime.guard.getState().serviceWorker, null);
        assert.equal(runtime.reloadCalls, 0);

        runtime.waitingWorker.dispatchEvent(
            new CustomEventStub("statechange")
        );
        await flushPromises();
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "GET_VERSION"
            ).length,
            2,
            "the regression must exercise two matching waiting responses"
        );
        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(runtime.guard.getState().serviceWorker, null);
        assert.equal(runtime.reloadCalls, 0);

        runtime.resolveReplacementWorkerVersions();
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});
        assert.equal(
            runtime.guard.getState().serviceWorker.shellVersion,
            "driver-shell-v3",
            "only the exact controller may complete pending verification"
        );
        assert.equal(runtime.reloadCalls, 0);
    }
);

test(
    "waiting responses cannot hide a late controller mismatch or create a reload loop",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v3",
            htmlShellVersion: "driver-shell-v3",
            workerContractVersion: "contract-v3",
            workerShellVersion: "driver-shell-v3",
            replacementWorkerContractVersion: "contract-v2",
            replacementWorkerShellVersion: "driver-shell-v2",
            waitingWorkerContractVersion: "contract-v3",
            waitingWorkerShellVersion: "driver-shell-v3",
            deferReplacementWorkerVersion: true,
        });
        runtime.guard.registerJavaScript("contract-v3");
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v3",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        });
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});

        runtime.activateReplacementWorker();
        await flushPromises();
        runtime.requestWaitingWorkerVersion();
        await flushPromises();
        runtime.waitingWorker.dispatchEvent(
            new CustomEventStub("statechange")
        );
        await flushPromises();

        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(runtime.guard.getState().serviceWorker, null);
        assert.equal(runtime.reloadCalls, 0);

        runtime.resolveReplacementWorkerVersions();
        await flushPromises();
        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(
            runtime.guard.getState().serviceWorker.shellVersion,
            "driver-shell-v2"
        );
        assert.equal(runtime.reloadCalls, 1);

        runtime.navigator.serviceWorker.dispatchEvent(
            new CustomEventStub("controllerchange")
        );
        await flushPromises();
        runtime.resolveReplacementWorkerVersions();
        await flushPromises();
        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(
            runtime.reloadCalls,
            1,
            "the same late mismatch must not create a reload loop"
        );
    }
);

test(
    "mismatching replacement controller triggers at most one safe reload",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v3",
            htmlShellVersion: "driver-shell-v3",
            workerContractVersion: "contract-v3",
            workerShellVersion: "driver-shell-v3",
            replacementWorkerContractVersion: "contract-v2",
            replacementWorkerShellVersion: "driver-shell-v2",
            hasWaitingWorker: false,
        });
        runtime.guard.registerJavaScript("contract-v3");
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v3",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        });
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});

        runtime.activateReplacementWorker();
        await flushPromises();
        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(runtime.reloadCalls, 1);

        runtime.navigator.serviceWorker.dispatchEvent(
            new CustomEventStub("controllerchange")
        );
        await flushPromises();
        assert.equal(
            runtime.reloadCalls,
            1,
            "the same mismatching contract must never cause a reload loop"
        );
    }
);

test(
    "mismatching controller waits for unfinished work and reloads after it becomes safe",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v3",
            htmlShellVersion: "driver-shell-v3",
            workerContractVersion: "contract-v3",
            workerShellVersion: "driver-shell-v3",
            replacementWorkerContractVersion: "contract-v2",
            replacementWorkerShellVersion: "driver-shell-v2",
            hasWaitingWorker: false,
            unfinishedAction: true,
        });
        runtime.guard.registerJavaScript("contract-v3");
        const serverContract = {
            app_contract_version: "contract-v3",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        };
        runtime.guard.acceptServerContract(serverContract);
        await flushPromises();
        assertContractState(runtime, {ready: true, locked: false});

        runtime.activateReplacementWorker();
        await flushPromises();
        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(runtime.reloadCalls, 0, "unfinished work must block the reload");

        runtime.finishUnfinishedAction();
        runtime.guard.acceptServerContract(serverContract);
        await flushPromises();
        assert.equal(
            runtime.reloadCalls,
            1,
            "the pending mismatch must recover once unfinished work is complete"
        );
    }
);

test(
    "two Driver fragment rebinds add no SW registration or controllerchange listener",
    async () => {
        const runtime = createRolePwaRuntime({
            roleCode: "driver",
            shellVersion: "driver-mobile-shell-v113",
        });
        const updateBadge = runtime.addNode(
            "[data-driver-pwa-update-badge]",
            new ElementStub("span")
        );
        updateBadge.hidden = true;
        runtime.addNode(
            "[data-driver-pwa-update-nav-target]",
            new ElementStub("button")
        );
        executeDriverPwaBinding(runtime, "driver-mobile-shell-v113");
        await flushPromises();
        const baselineRegistrations = runtime.registerCalls;
        const baselineControllerListeners = runtime.listenerCount(
            "controllerchange"
        );

        executeDriverPwaBinding(runtime, "driver-mobile-shell-v113");
        await flushPromises();
        executeDriverPwaBinding(runtime, "driver-mobile-shell-v113");
        await flushPromises();

        assert.equal(
            runtime.registerCalls,
            baselineRegistrations,
            "fragment rebind must reuse the existing Driver registration"
        );
        assert.equal(
            runtime.listenerCount("controllerchange"),
            baselineControllerListeners,
            "fragment rebind must not accumulate global controller listeners"
        );
        assert.equal(
            runtime.fetchRequests.filter(
                ({url}) => url.includes("?version_check")
            ).length,
            0,
            "fragment rebind must not fetch the full Driver service worker"
        );
        assert.equal(
            runtime.window.__driverPwaUpdateRuntime.currentShellVersion,
            "driver-mobile-shell-v113",
            "fragment HTML must not rewrite the immutable version of the loaded Driver JavaScript"
        );
        runtime.window.dispatchEvent(new CustomEventStub(
            "app-pwa-contract-state",
            {
                detail: {
                    ready: false,
                    server: {shellVersion: "driver-mobile-shell-v114"},
                },
            }
        ));
        assert.equal(
            updateBadge.hidden,
            false,
            "a newer server shell must stay visible after a newer fragment is rebound"
        );
    }
);

test(
    "Driver can activate a waiting worker that matches newly loaded HTML",
    async () => {
        const runtime = createRolePwaRuntime({
            roleCode: "driver",
            shellVersion: "driver-mobile-shell-v114",
            activeShellVersion: "driver-mobile-shell-v112",
            nextShellVersion: "driver-mobile-shell-v114",
            hasWaitingWorker: true,
        });
        const updateBadge = runtime.addNode(
            "[data-driver-pwa-update-badge]",
            new ElementStub("span")
        );
        updateBadge.hidden = true;
        const updateTarget = runtime.addNode(
            "[data-driver-pwa-update-nav-target]",
            new ElementStub("button")
        );
        const updateModal = runtime.addNode(
            "[data-driver-pwa-update-modal]",
            new ElementStub("div")
        );
        updateModal.hidden = true;
        runtime.addNode(
            "[data-driver-pwa-update-status]",
            new ElementStub("div")
        );
        runtime.addNode(
            "[data-driver-pwa-current-version]",
            new ElementStub("span")
        );
        runtime.addNode(
            "[data-driver-pwa-new-version]",
            new ElementStub("span")
        );
        const applyButton = runtime.addNode(
            "[data-driver-pwa-update-apply]",
            new ElementStub("button")
        );
        runtime.addNode(
            "[data-driver-pwa-update-later]",
            new ElementStub("button")
        );

        executeDriverPwaBinding(runtime, "driver-mobile-shell-v114");
        await flushPromises(24);
        assert.equal(
            updateBadge.hidden,
            false,
            "active v112 plus waiting v113 must expose the controlled activation path"
        );

        updateTarget.dispatchEvent(new CustomEventStub("click"));
        assert.equal(updateModal.hidden, false, "the recovery target must open the update modal");
        applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises(24);
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            1,
            "the user-controlled action must activate the waiting worker exactly once"
        );
    }
);

test(
    "Driver never offers an older waiting worker over newer loaded HTML",
    async () => {
        const runtime = createRolePwaRuntime({
            roleCode: "driver",
            shellVersion: "driver-mobile-shell-v114",
            activeShellVersion: "driver-mobile-shell-v111",
            nextShellVersion: "driver-mobile-shell-v112",
            hasWaitingWorker: true,
        });
        const updateBadge = runtime.addNode(
            "[data-driver-pwa-update-badge]",
            new ElementStub("span")
        );
        updateBadge.hidden = true;
        runtime.addNode(
            "[data-driver-pwa-update-nav-target]",
            new ElementStub("button")
        );
        const updateModal = runtime.addNode(
            "[data-driver-pwa-update-modal]",
            new ElementStub("div")
        );
        updateModal.hidden = true;
        runtime.addNode(
            "[data-driver-pwa-update-status]",
            new ElementStub("div")
        );
        runtime.addNode(
            "[data-driver-pwa-current-version]",
            new ElementStub("span")
        );
        runtime.addNode(
            "[data-driver-pwa-new-version]",
            new ElementStub("span")
        );
        runtime.addNode(
            "[data-driver-pwa-update-apply]",
            new ElementStub("button")
        );
        runtime.addNode(
            "[data-driver-pwa-update-later]",
            new ElementStub("button")
        );

        executeDriverPwaBinding(runtime, "driver-mobile-shell-v114");
        await flushPromises(24);
        assert.equal(
            updateBadge.hidden,
            true,
            "waiting v112 must not replace already loaded HTML v113"
        );
    }
);

test(
    "Driver ignores a delayed stale waiting worker after promotion and fragment rebind",
    async () => {
        const runtime = createRolePwaRuntime({
            roleCode: "driver",
            shellVersion: "driver-mobile-shell-v114",
            activeShellVersion: "driver-mobile-shell-v112",
            nextShellVersion: "driver-mobile-shell-v114",
            hasWaitingWorker: true,
            deferWaitingVersion: true,
        });
        const updateBadge = runtime.addNode(
            "[data-driver-pwa-update-badge]",
            new ElementStub("span")
        );
        updateBadge.hidden = true;
        runtime.addNode(
            "[data-driver-pwa-update-nav-target]",
            new ElementStub("button")
        );
        const updateModal = runtime.addNode(
            "[data-driver-pwa-update-modal]",
            new ElementStub("div")
        );
        updateModal.hidden = true;
        runtime.addNode(
            "[data-driver-pwa-update-status]",
            new ElementStub("div")
        );
        runtime.addNode(
            "[data-driver-pwa-current-version]",
            new ElementStub("span")
        );
        runtime.addNode(
            "[data-driver-pwa-new-version]",
            new ElementStub("span")
        );
        const applyButton = runtime.addNode(
            "[data-driver-pwa-update-apply]",
            new ElementStub("button")
        );
        runtime.addNode(
            "[data-driver-pwa-update-later]",
            new ElementStub("button")
        );

        executeDriverPwaBinding(runtime, "driver-mobile-shell-v114");
        await flushPromises(12);
        runtime.promoteWaitingWorker();
        executeDriverPwaBinding(runtime, "driver-mobile-shell-v114");
        runtime.resolveWaitingVersion();
        await flushPromises(24);

        assert.equal(
            updateBadge.hidden,
            true,
            "a worker that is no longer waiting must not restore the update badge"
        );
        applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises(24);
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "a stale worker must never receive a second activation request"
        );
    }
);

test(
    "double manual Excavator check is single-flight and keeps an understandable result",
    async () => {
        const runtime = createRolePwaRuntime({
            roleCode: "excavator_operator",
            shellVersion: "excavator-mobile-shell-v127",
        });
        const checkButton = runtime.addNode(
            "[data-eo-pwa-update-check]",
            new ElementStub("button")
        );
        const checkLabel = runtime.addNode(
            "[data-eo-pwa-update-check-label]",
            new ElementStub("b")
        );
        runtime.addNode(
            "[data-eo-pwa-update-check-version]",
            new ElementStub("em")
        );
        runtime.addNode(
            "[data-eo-pwa-update-status]",
            new ElementStub("div")
        );
        runtime.context.excavatorShellVersion =
            "excavator-mobile-shell-v127";
        runtime.context.scheduleExcavatorViewportHeightSync = function () {};
        vm.runInContext(extractExcavatorPwaSource(), runtime.context, {
            filename: "templates/trips/excavator_work.html::PwaUpdates",
        });
        runtime.context.initExcavatorPwaUpdates();
        await flushPromises();
        const baselineUpdates = runtime.updateCalls;

        runtime.deferManualUpdates();
        checkButton.dispatchEvent(new CustomEventStub("click"));
        checkButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();

        assert.equal(
            runtime.updateCalls - baselineUpdates,
            1,
            "double click while a manual check is pending must reuse one update"
        );
        assert.match(
            checkLabel.textContent,
            /Провер/,
            "the pending manual check must have an understandable status"
        );

        runtime.resolveManualUpdate();
        await flushPromises(24);
        assert.match(
            checkLabel.textContent,
            /Актуально|Доступно|Обнов/,
            "the completed manual check must keep an understandable result"
        );
    }
);

test(
    "unfinished Driver action never receives SKIP_WAITING or a forced reload",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            unfinishedAction: true,
        });
        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);
        runtime.navigator.serviceWorker.dispatchEvent(
            new CustomEventStub("controllerchange")
        );
        await flushRuntime(runtime);

        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "an unfinished action must not activate a waiting worker"
        );
        assert.equal(
            runtime.reloadCalls,
            0,
            "an unfinished action must not be reloaded"
        );
    }
);

test(
    "controller change waits for an unfinished action and reloads exactly once when safe",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            unfinishedAction: true,
        });
        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        const newerServerContract = {
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        };
        runtime.guard.acceptServerContract(newerServerContract);
        await flushRuntime(runtime);
        runtime.navigator.serviceWorker.dispatchEvent(
            new CustomEventStub("controllerchange")
        );
        await flushRuntime(runtime);
        assert.equal(runtime.reloadCalls, 0);

        runtime.finishUnfinishedAction();
        runtime.guard.acceptServerContract(newerServerContract);
        await flushRuntime(runtime);
        assert.equal(
            runtime.reloadCalls,
            1,
            "the pending controller change must recover once the action is safe"
        );

        runtime.guard.acceptServerContract(newerServerContract);
        await flushRuntime(runtime);
        assert.equal(runtime.reloadCalls, 1, "the safe retry must remain single-shot");
    }
);

test(
    "a broader worker from another role cannot replace the configured scoped worker",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlShellVersion: "clerk-workplace-shell-v1",
            workerShellVersion: "clerk-workplace-shell-v1",
            roleCode: "settlement_clerk",
            locationOrigin: "https://dispatcher.localhost",
            locationHref: "https://dispatcher.localhost/clerk/",
            locationPathname: "/clerk/",
            serviceWorkerUrl: "/clerk/sw.js",
            serviceWorkerScope: "/clerk/",
            registrationScope: "/",
            registrationWorkerUrl: "/dispatcher-service-worker.js",
            hasWaitingWorker: false,
        });
        await flushRuntime(runtime);

        assert.equal(runtime.registerCalls, 1);
        assert.deepEqual(runtime.registerRequests, [{
            workerUrl: "/clerk/sw.js",
            scope: "/clerk/",
            updateViaCache: "none",
        }]);
        assert.equal(
            runtime.registration.scope,
            "https://dispatcher.localhost/clerk/"
        );
        assert.equal(
            runtime.activeWorker.scriptURL,
            "https://dispatcher.localhost/clerk/sw.js"
        );
    }
);

test(
    "a legacy settlement worker cannot replace the configured clerk worker",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlShellVersion: "clerk-workplace-shell-v1",
            workerShellVersion: "clerk-workplace-shell-v1",
            roleCode: "settlement_clerk",
            locationOrigin: "https://clerk.localhost",
            locationHref: "https://clerk.localhost/clerk/",
            locationPathname: "/clerk/",
            serviceWorkerUrl: "/clerk/sw.js",
            serviceWorkerScope: "/clerk/",
            registrationScope: "/settlement/",
            registrationWorkerUrl: "/settlement/sw.js",
            hasWaitingWorker: false,
        });
        await flushRuntime(runtime);

        assert.equal(runtime.registerCalls, 1);
        assert.deepEqual(runtime.registerRequests, [{
            workerUrl: "/clerk/sw.js",
            scope: "/clerk/",
            updateViaCache: "none",
        }]);
        assert.equal(runtime.registration.scope, "https://clerk.localhost/clerk/");
        assert.equal(runtime.activeWorker.scriptURL, "https://clerk.localhost/clerk/sw.js");
    }
);

test(
    "transient registration failure recovers automatically without an online event",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            hasWaitingWorker: false,
            registrationAvailable: false,
            registerFailures: 1,
            online: false,
        });
        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        const newerServerContract = {
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        };
        runtime.guard.acceptServerContract(newerServerContract);
        for (let pass = 0; pass < 3; pass += 1) {
            await flushRuntime(runtime);
        }
        assert.equal(runtime.registerCalls, 2, "the bounded retry must re-register offline");
        assert.equal(runtime.updateCalls, 1, "the recovered registration must be updated");

        for (let repeat = 0; repeat < 10; repeat += 1) {
            runtime.guard.acceptServerContract(newerServerContract);
            await flushRuntime(runtime);
        }
        assert.equal(
            runtime.registerCalls,
            2,
            "realtime observations must not create a registration storm"
        );
        assert.equal(runtime.updateCalls, 1);

        runtime.window.dispatchEvent(new CustomEventStub("online"));
        runtime.window.dispatchEvent(new CustomEventStub("online"));
        await flushRuntime(runtime);
        assert.equal(runtime.registerCalls, 2);
        assert.equal(runtime.updateCalls, 1, "online signals must not restart a completed sequence");
    }
);

test(
    "failed controlled update retries with backoff even while offline and does not storm",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            hasWaitingWorker: false,
            updateFailures: 1,
            online: false,
        });
        runtime.guard.registerJavaScript("contract-v2");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v2",
            shellVersion: "driver-shell-v2",
            roleCode: "driver",
        });
        const newerServerContract = {
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v3",
            role_app_code: "driver",
        };
        runtime.guard.acceptServerContract(newerServerContract);
        await flushRuntime(runtime);
        assert.equal(runtime.updateCalls, 2, "one transient failure must retry automatically");

        for (let repeat = 0; repeat < 10; repeat += 1) {
            runtime.guard.acceptServerContract(newerServerContract);
            await flushRuntime(runtime);
        }
        assert.equal(
            runtime.updateCalls,
            2,
            "same-version observations must not restart the bounded update sequence"
        );

        runtime.window.dispatchEvent(new CustomEventStub("online"));
        await flushRuntime(runtime);
        assert.equal(
            runtime.updateCalls,
            2,
            "online recovery must not duplicate an automatic retry"
        );
    }
);

test(
    "online burst reuses an in-flight service-worker registration",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v2",
            workerShellVersion: "driver-shell-v2",
            hasWaitingWorker: false,
            registrationAvailable: false,
            deferRegistration: true,
        });
        await flushPromises();
        assert.equal(runtime.registerCalls, 1);

        runtime.window.dispatchEvent(new CustomEventStub("online"));
        runtime.window.dispatchEvent(new CustomEventStub("online"));
        runtime.window.dispatchEvent(new CustomEventStub("online"));
        await flushPromises();
        assert.equal(
            runtime.registerCalls,
            1,
            "online signals must not replace an unresolved registration promise"
        );

        runtime.resolveRegistration();
        await flushRuntime(runtime);
        assert.equal(runtime.registerCalls, 1);
    }
);

test(
    "PWA recovery controls remain clickable while the contract is locked",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v1",
            workerShellVersion: "driver-shell-v1",
        });
        runtime.guard.registerJavaScript("contract-v1");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v1",
            shellVersion: "driver-shell-v1",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);

        for (const attribute of [
            "data-eo-pwa-update-nav-target",
            "data-eo-pwa-update-check",
        ]) {
            const updateButton = new ElementStub("button", {[attribute]: ""});
            runtime.document.body.appendChild(updateButton);
            const click = new CustomEventStub("click");
            click.target = updateButton;
            runtime.document.dispatchEvent(click);
            assert.equal(
                click.defaultPrevented,
                false,
                `a locked shell must still allow its ${attribute} recovery control`
            );
        }
        const loginForm = new ElementStub("form", {
            method: "post",
            "data-validated-login": "",
        });
        const loginSubmit = new ElementStub("button", {type: "submit"});
        loginForm.appendChild(loginSubmit);
        runtime.document.body.appendChild(loginForm);
        const loginClick = new CustomEventStub("click");
        loginClick.target = loginSubmit;
        runtime.document.dispatchEvent(loginClick);
        assert.equal(
            loginClick.defaultPrevented,
            false,
            "the submit button inside the safe login form must stay clickable"
        );
        const submit = new CustomEventStub("submit");
        submit.target = loginForm;
        runtime.document.dispatchEvent(submit);
        assert.equal(
            submit.defaultPrevented,
            false,
            "an old PWA shell must not make the login form impossible to submit"
        );
    }
);

test(
    "old cached JavaScript and service worker lock new HTML and request an atomic update",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v1",
            workerShellVersion: "driver-shell-v1",
        });

        runtime.guard.registerJavaScript("contract-v1");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v1",
            shellVersion: "driver-shell-v1",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);

        assertContractState(runtime, {ready: false, locked: true});
        assert.ok(
            runtime.updateCalls >= 1,
            "contract mismatch must request registration.update()"
        );
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "automatic contract recovery may download a release but must not activate it"
        );
        assert.ok(runtime.reloadCalls <= 1);
    }
);

test(
    "a coherent old shell remains usable offline and locks once a newer online contract is known",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v1",
            htmlShellVersion: "driver-shell-v1",
            workerContractVersion: "contract-v1",
            workerShellVersion: "driver-shell-v1",
            online: false,
        });

        runtime.guard.registerJavaScript("contract-v1");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v1",
            shellVersion: "driver-shell-v1",
            roleCode: "driver",
        });
        runtime.guard.markServerUnavailable();
        await flushRuntime(runtime);
        assertContractState(runtime, {ready: true, locked: false});

        runtime.setOnline(true);
        runtime.window.dispatchEvent(new CustomEventStub("online"));
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);

        assertContractState(runtime, {ready: false, locked: true});
        assert.ok(runtime.reloadCalls <= 1);
    }
);

test(
    "contract recovery never mutates or removes an open trip from the DOM",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v2",
            htmlShellVersion: "driver-shell-v2",
            workerContractVersion: "contract-v1",
            workerShellVersion: "driver-shell-v1",
        });
        const openTrip = new ElementStub("article", {
            "data-active-trip-id": "42",
            "data-trip-status": "loaded_waiting_unload",
        });
        openTrip.textContent = "Рейс 42 · На разгрузку";
        runtime.document.body.appendChild(openTrip);

        runtime.guard.registerJavaScript("contract-v1");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v1",
            shellVersion: "driver-shell-v1",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v2",
            role_shell_version: "driver-shell-v2",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);

        assertContractState(runtime, {ready: false, locked: true});
        assert.equal(openTrip.isConnected, true);
        assert.equal(openTrip.dataset.activeTripId, "42");
        assert.equal(openTrip.dataset.tripStatus, "loaded_waiting_unload");
        assert.equal(openTrip.textContent, "Рейс 42 · На разгрузку");
        assert.equal(
            runtime.fetchRequests.filter(
                (request) => String(request.init.method || "GET").toUpperCase()
                    !== "GET"
            ).length,
            0,
            "contract guard must not send a trip mutation"
        );
    }
);

test(
    "two sequential releases allow at most one reload for each expected contract",
    {skip: guardUnavailable},
    async () => {
        const runtime = createRuntime({
            htmlContractVersion: "contract-v1",
            htmlShellVersion: "driver-shell-v1",
            workerContractVersion: "contract-v1",
            workerShellVersion: "driver-shell-v1",
        });
        runtime.guard.registerJavaScript("contract-v1");
        runtime.guard.acceptServiceWorkerVersion({
            appContractVersion: "contract-v1",
            shellVersion: "driver-shell-v1",
            roleCode: "driver",
        });
        runtime.guard.acceptServerContract({
            app_contract_version: "contract-v1",
            role_shell_version: "driver-shell-v1",
            role_app_code: "driver",
        });
        await flushRuntime(runtime);
        assertContractState(runtime, {ready: true, locked: false});

        for (const release of [2, 3]) {
            const reloadsBeforeRelease = runtime.reloadCalls;
            const contractVersion = `contract-v${release}`;
            const shellVersion = `driver-shell-v${release}`;
            runtime.document.body.dataset.appContractVersion = contractVersion;
            runtime.document.body.dataset.appShellVersion = shellVersion;
            runtime.guard.acceptServerContract({
                app_contract_version: contractVersion,
                role_shell_version: shellVersion,
                role_app_code: "driver",
            });
            runtime.guard.acceptServerContract({
                app_contract_version: contractVersion,
                role_shell_version: shellVersion,
                role_app_code: "driver",
            });
            runtime.navigator.serviceWorker.dispatchEvent(
                new CustomEventStub("controllerchange")
            );
            runtime.navigator.serviceWorker.dispatchEvent(
                new CustomEventStub("controllerchange")
            );
            await flushRuntime(runtime);
            assertContractState(runtime, {ready: false, locked: true});
            assert.ok(
                runtime.reloadCalls - reloadsBeforeRelease <= 1,
                `release ${contractVersion} entered a reload loop`
            );

            runtime.guard.registerJavaScript(contractVersion);
            runtime.guard.acceptServiceWorkerVersion({
                appContractVersion: contractVersion,
                shellVersion,
                roleCode: "driver",
            });
            await flushRuntime(runtime);
            assertContractState(runtime, {ready: true, locked: false});
        }
        assert.ok(observedState(runtime).reloadCount <= 2);
    }
);

test("all role PWA cache prefixes are unique and cleanup stays role-scoped", () => {
    const roleAppsPath = path.join(backendRoot, "users", "role_apps.py");
    const roleAppsSource = fs.readFileSync(roleAppsPath, "utf8");
    const roleVersions = Array.from(
        roleAppsSource.matchAll(
            /role_code='([^']+)'[\s\S]*?shell_version='([^']+)'/g
        ),
        (match) => ({roleCode: match[1], shellVersion: match[2]})
    );
    assert.equal(roleVersions.length, 12);

    const prefixes = roleVersions.map(({roleCode, shellVersion}) => {
        const markerIndex = shellVersion.lastIndexOf("-v");
        assert.ok(markerIndex > 0, `${roleCode} has no versioned shell`);
        return {
            roleCode,
            prefix: `${shellVersion.slice(0, markerIndex)}-`,
        };
    });
    assert.equal(
        new Set(prefixes.map((item) => item.prefix)).size,
        prefixes.length,
        "two role PWAs share one cache prefix"
    );
    for (const left of prefixes) {
        for (const right of prefixes) {
            if (left.roleCode === right.roleCode) {
                continue;
            }
            assert.equal(
                left.prefix.startsWith(right.prefix),
                false,
                `${left.roleCode} cache can be selected by ${right.roleCode}`
            );
        }
    }

    const explicitWorkerFiles = [
        path.join(backendRoot, "users", "views.py"),
        path.join(backendRoot, "trips", "views.py"),
        path.join(backendRoot, "assignments", "views.py"),
        path.join(backendRoot, "assignments", "deputy_views.py"),
    ];
    const expectedPrefixes = new Set(prefixes.map((item) => item.prefix));
    const explicitPrefixes = [];
    for (const workerPath of explicitWorkerFiles) {
        const source = fs.readFileSync(workerPath, "utf8");
        explicitPrefixes.push(
            ...Array.from(
                source.matchAll(/\bconst CACHE_PREFIX = "([^"]+)";/g),
                (match) => match[1]
            )
        );
    }
    assert.ok(explicitPrefixes.length >= 5);
    explicitPrefixes.forEach((prefix) => {
        assert.ok(
            expectedPrefixes.has(prefix),
            `unknown or cross-role cache prefix: ${prefix}`
        );
    });
    assert.match(
        roleAppsSource,
        /key\.startsWith\(CACHE_PREFIX\) && key !== CACHE_NAME/
    );
});
