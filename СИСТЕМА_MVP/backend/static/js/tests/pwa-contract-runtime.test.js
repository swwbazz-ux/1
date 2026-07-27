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
    const document = new DocumentStub({
        appContractVersion: htmlContractVersion,
        appShellVersion: htmlShellVersion,
        appRoleCode: roleCode,
        appContractReady: "false",
    });
    const windowTarget = new EventTargetStub();
    const serviceWorkerTarget = new EventTargetStub();
    const registrationTarget = new EventTargetStub();
    const waitingWorkerMessages = [];
    const activeWorkerMessages = [];
    const fetchRequests = [];
    const timers = [];
    let reloadCalls = 0;
    let updateCalls = 0;
    let online = options.online !== false;

    const waitingWorker = {
        state: "installed",
        postMessage(message) {
            waitingWorkerMessages.push(message);
        },
        addEventListener() {},
    };
    const activeWorker = {
        state: "activated",
        postMessage(message, transfer) {
            activeWorkerMessages.push(message);
            if (
                message
                && message.type === "GET_VERSION"
                && transfer
                && transfer[0]
            ) {
                transfer[0].postMessage({
                    appContractVersion: options.workerContractVersion
                        || htmlContractVersion,
                    shellVersion: options.workerShellVersion
                        || htmlShellVersion,
                    roleCode,
                });
            }
        },
    };
    const registration = Object.assign(registrationTarget, {
        waiting: waitingWorker,
        installing: null,
        active: activeWorker,
        update() {
            updateCalls += 1;
            return Promise.resolve(registration);
        },
    });
    const serviceWorker = Object.assign(serviceWorkerTarget, {
        controller: activeWorker,
        ready: Promise.resolve(registration),
        getRegistration() {
            return Promise.resolve(registration);
        },
        getRegistrations() {
            return Promise.resolve([registration]);
        },
        register() {
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
    const location = {
        origin: "https://driver.localhost",
        href: "https://driver.localhost/driver/",
        pathname: "/driver/",
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
        AppRealtimeConfig: {
            stateUrl: "/api/operational-state/version/",
        },
    });
    const runtime = {
        document,
        window,
        navigator,
        registration,
        waitingWorkerMessages,
        activeWorkerMessages,
        fetchRequests,
        get reloadCalls() {
            return reloadCalls;
        },
        get updateCalls() {
            return updateCalls;
        },
        setOnline(value) {
            online = Boolean(value);
            navigator.onLine = online;
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
    function setTimeoutStub(callback) {
        const timer = {
            id: nextTimerId,
            callback,
            cancelled: false,
        };
        nextTimerId += 1;
        timers.push(timer);
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
        assert.ok(
            runtime.waitingWorkerMessages.some(
                (message) => message && message.type === "SKIP_WAITING"
            ),
            "waiting compatible release must receive SKIP_WAITING"
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
    assert.equal(roleVersions.length, 11);

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
