"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const backendRoot = path.resolve(__dirname, "../../..");
const baseTemplate = fs.readFileSync(
    path.join(backendRoot, "templates", "base.html"),
    "utf8"
);
const driverTemplate = fs.readFileSync(
    path.join(backendRoot, "templates", "users", "driver_shift.html"),
    "utf8"
);

function extractBetween(source, startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert.ok(start >= 0 && end > start, `missing ${startMarker} / ${endMarker}`);
    return source.slice(start + startMarker.length, end);
}

function extractNamedIife(source, name) {
    const marker = `(function ${name}() {`;
    const start = source.indexOf(marker);
    const endMarker = "\n    })();";
    const end = source.indexOf(endMarker, start);
    assert.ok(start >= 0 && end > start, `missing runtime ${name}`);
    return source.slice(start, end + endMarker.length);
}

const guardSource = extractBetween(
    baseTemplate,
    "/* APP_PWA_CONTRACT_GUARD_START */",
    "/* APP_PWA_CONTRACT_GUARD_END */"
);
const driverPwaSource = extractNamedIife(driverTemplate, "initDriverPwaUpdates");

class EventTargetStub {
    constructor() {
        this.listeners = new Map();
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    dispatchEvent(event) {
        event.target = event.target || this;
        (this.listeners.get(event.type) || [])
            .slice()
            .forEach((listener) => listener.call(this, event));
        return !event.defaultPrevented;
    }
}

class CustomEventStub {
    constructor(type, init = {}) {
        this.type = type;
        this.detail = init.detail;
        this.defaultPrevented = false;
        this.target = null;
    }

    preventDefault() {
        this.defaultPrevented = true;
    }

    stopImmediatePropagation() {}

    stopPropagation() {}
}

class ClassListStub {
    constructor() {
        this.values = new Set();
    }

    toggle(name, force) {
        if (force === true) {
            this.values.add(name);
            return true;
        }
        if (force === false) {
            this.values.delete(name);
            return false;
        }
        if (this.values.has(name)) {
            this.values.delete(name);
            return false;
        }
        this.values.add(name);
        return true;
    }
}

class ElementStub extends EventTargetStub {
    constructor(tagName = "div") {
        super();
        this.tagName = String(tagName).toUpperCase();
        this.nodeName = this.tagName;
        this.dataset = {};
        this.attributes = new Map();
        this.classList = new ClassListStub();
        this.children = [];
        this.parentNode = null;
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.className = "";
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    getAttribute(name) {
        return this.attributes.get(name) || "";
    }

    hasAttribute(name) {
        return this.attributes.has(name);
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
        if (!this.parentNode) return;
        const index = this.parentNode.children.indexOf(this);
        if (index >= 0) this.parentNode.children.splice(index, 1);
        this.parentNode = null;
    }
}

class DocumentStub extends EventTargetStub {
    constructor() {
        super();
        this.readyState = "loading";
        this.activeElement = null;
        this.body = new ElementStub("body");
        Object.assign(this.body.dataset, {
            appContractVersion: "contract-v2",
            appShellVersion: "driver-mobile-shell-v113",
            appRoleCode: "driver",
            appContractReady: "false",
            appServiceWorkerUrl: "/driver-service-worker.js",
            appServiceWorkerScope: "/driver/",
        });
        this.nodes = new Map();
    }

    createElement(tagName) {
        return new ElementStub(tagName);
    }

    querySelector(selector) {
        if (this.nodes.has(selector)) return this.nodes.get(selector);
        if (selector === "[data-app-contract-banner]") {
            return this.body.children.find(
                (child) => child.hasAttribute("data-app-contract-banner")
            ) || null;
        }
        return null;
    }

    querySelectorAll(selector) {
        const node = this.querySelector(selector);
        return node ? [node] : [];
    }

    addNode(selector, node = new ElementStub("button")) {
        this.nodes.set(selector, node);
        this.body.appendChild(node);
        return node;
    }
}

class StorageStub {
    constructor() {
        this.values = new Map();
    }

    getItem(key) {
        return this.values.has(String(key)) ? this.values.get(String(key)) : null;
    }

    setItem(key, value) {
        this.values.set(String(key), String(value));
    }
}

function createMessageChannel() {
    const port1 = {
        onmessage: null,
        postMessage(data) {
            if (port2.onmessage) port2.onmessage({data});
        },
        start() {},
        close() {},
    };
    const port2 = {
        onmessage: null,
        postMessage(data) {
            if (port1.onmessage) port1.onmessage({data});
        },
        start() {},
        close() {},
    };
    return {port1, port2};
}

function createIntegratedRuntime(options = {}) {
    const document = new DocumentStub();
    const windowTarget = new EventTargetStub();
    const serviceWorkerTarget = new EventTargetStub();
    const registrationTarget = new EventTargetStub();
    const updateResolvers = [];
    let updateCalls = 0;
    let updateFailures = Number(options.updateFailures || 0);

    const activeWorker = {
        state: "activated",
        postMessage(message, transfer) {
            if (!message || message.type !== "GET_VERSION" || !transfer || !transfer[0]) {
                return;
            }
            transfer[0].postMessage({
                appContractVersion: "contract-v2",
                shellVersion: "driver-mobile-shell-v113",
                roleCode: "driver",
                version: "driver-mobile-shell-v113",
            });
        },
    };
    const registration = Object.assign(registrationTarget, {
        active: activeWorker,
        waiting: null,
        installing: null,
        update() {
            updateCalls += 1;
            if (updateFailures > 0) {
                updateFailures -= 1;
                return Promise.reject(new Error("planned update failure"));
            }
            if (options.deferUpdates) {
                return new Promise((resolve) => {
                    updateResolvers.push(() => resolve(registration));
                });
            }
            return Promise.resolve(registration);
        },
    });
    const serviceWorker = Object.assign(serviceWorkerTarget, {
        controller: activeWorker,
        ready: Promise.resolve(registration),
        getRegistration() {
            return Promise.resolve(registration);
        },
        register() {
            return Promise.resolve(registration);
        },
    });
    const navigator = {serviceWorker};
    const window = Object.assign(windowTarget, {
        document,
        navigator,
        location: {reload() {}},
        localStorage: new StorageStub(),
        sessionStorage: new StorageStub(),
        CustomEvent: CustomEventStub,
        Event: CustomEventStub,
        Promise,
        URL,
        console,
        setTimeout,
        clearTimeout,
    });
    function MessageChannelStub() {
        const channel = createMessageChannel();
        this.port1 = channel.port1;
        this.port2 = channel.port2;
    }
    window.MessageChannel = MessageChannelStub;
    const context = vm.createContext({
        window,
        self: window,
        document,
        navigator,
        location: window.location,
        localStorage: window.localStorage,
        sessionStorage: window.sessionStorage,
        CustomEvent: CustomEventStub,
        Event: CustomEventStub,
        MessageChannel: MessageChannelStub,
        Promise,
        URL,
        console,
        setTimeout,
        clearTimeout,
    });

    const badge = document.addNode(
        "[data-driver-pwa-update-badge]",
        new ElementStub("span")
    );
    badge.hidden = true;
    document.addNode("[data-driver-pwa-update-nav-target]");
    const modal = document.addNode(
        "[data-driver-pwa-update-modal]",
        new ElementStub("div")
    );
    modal.hidden = true;
    const status = document.addNode(
        "[data-driver-pwa-update-status]",
        new ElementStub("p")
    );
    document.addNode(
        "[data-driver-pwa-current-version]",
        new ElementStub("span")
    );
    document.addNode(
        "[data-driver-pwa-new-version]",
        new ElementStub("span")
    );
    const applyButton = document.addNode(
        "[data-driver-pwa-update-apply]",
        new ElementStub("button")
    );
    document.addNode("[data-driver-pwa-update-later]");

    vm.runInContext(guardSource, context, {
        filename: "templates/base.html::AppPwaContractGuard",
    });
    document.dispatchEvent(new CustomEventStub("DOMContentLoaded"));
    window.AppPwaContractGuard.registerJavaScript("contract-v2");
    window.AppPwaContractGuard.acceptServerContract({
        app_contract_version: "contract-v2",
        role_shell_version: "driver-mobile-shell-v114",
        role_app_code: "driver",
    });

    context.shell = {
        dataset: {driverPwaVersion: "driver-mobile-shell-v113"},
    };
    vm.runInContext(driverPwaSource, context, {
        filename: "templates/users/driver_shift.html::initDriverPwaUpdates",
    });

    return {
        applyButton,
        badge,
        status,
        window,
        get updateCalls() {
            return updateCalls;
        },
        resolveUpdates() {
            assert.ok(updateResolvers.length, "no update is pending");
            updateResolvers.splice(0).forEach((resolve) => resolve());
        },
    };
}

async function flushPromises(iterations = 24) {
    for (let index = 0; index < iterations; index += 1) {
        await Promise.resolve();
    }
}

test(
    "real Driver button joins the real guard automatic update single-flight",
    async () => {
        const runtime = createIntegratedRuntime({deferUpdates: true});
        await flushPromises();
        assert.equal(runtime.updateCalls, 1, "the automatic update must be pending");
        assert.equal(runtime.badge.hidden, false, "the role UI must show the newer shell");

        runtime.applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();
        assert.equal(
            runtime.updateCalls,
            1,
            "the real role button must join the real guard update flight"
        );
        assert.match(runtime.status.textContent, /Проверяем/);

        runtime.resolveUpdates();
        await flushPromises();
        assert.equal(runtime.updateCalls, 1);
        assert.match(runtime.status.textContent, /актуальная версия/i);
    }
);

test(
    "real Driver button retries the real guard after an automatic update error",
    async () => {
        const runtime = createIntegratedRuntime({updateFailures: 1});
        await flushPromises();
        assert.equal(runtime.updateCalls, 1, "the first automatic update must fail");

        runtime.applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();

        assert.equal(runtime.updateCalls, 2, "the manual retry must start one new update");
        assert.match(runtime.status.textContent, /актуальная версия/i);
    }
);
