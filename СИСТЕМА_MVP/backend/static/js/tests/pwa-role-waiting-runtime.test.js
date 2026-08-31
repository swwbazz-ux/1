#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const backendRoot = path.resolve(__dirname, "../../..");
const excavatorTemplate = fs.readFileSync(
    path.join(backendRoot, "templates", "trips", "excavator_work.html"),
    "utf8"
);
const miningMasterTemplate = fs.readFileSync(
    path.join(backendRoot, "templates", "trips", "dispatcher_control.html"),
    "utf8"
);

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
        const nextEvent = event || {};
        nextEvent.target = nextEvent.target || this;
        (this.listeners.get(nextEvent.type) || [])
            .slice()
            .forEach((listener) => listener.call(this, nextEvent));
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

class ElementStub extends EventTargetStub {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.textContent = "";
        this.title = "";
        this.classList = new ClassListStub();
        this.attributes = new Map();
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    removeAttribute(name) {
        this.attributes.delete(name);
    }

    focus() {}
}

class StorageStub {
    constructor() {
        this.values = new Map();
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
}

class DocumentStub extends EventTargetStub {
    constructor() {
        super();
        this.nodes = new Map();
        this.ids = new Map();
    }

    addNode(selector, node = new ElementStub()) {
        this.nodes.set(selector, node);
        return node;
    }

    querySelector(selector) {
        return this.nodes.get(selector) || null;
    }

    querySelectorAll(selector) {
        const node = this.nodes.get(selector);
        return node ? [node] : [];
    }

    getElementById(id) {
        return this.ids.get(id) || null;
    }
}

class CustomEventStub {
    constructor(type, options = {}) {
        this.type = type;
        this.detail = options.detail;
        this.target = options.target || null;
    }

    preventDefault() {}
}

function createMessageChannel() {
    const port1 = {
        onmessage: null,
        postMessage(message) {
            if (typeof port2.onmessage === "function") {
                port2.onmessage({data: message});
            }
        },
    };
    const port2 = {
        onmessage: null,
        postMessage(message) {
            if (typeof port1.onmessage === "function") {
                port1.onmessage({data: message});
            }
        },
    };
    return {port1, port2};
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

function extractMiningMasterPwaSource() {
    const versionMatch = miningMasterTemplate.match(
        /var miningMasterShellVersion = "mining-master-mobile-shell-v\d+";/
    );
    assert.ok(versionMatch, "missing Mining Master shell version");

    const coreStartMarker = "    var miningMasterUpdateModal = ";
    const coreEndMarker = "    function setTheme(theme) {";
    const coreStart = miningMasterTemplate.indexOf(coreStartMarker);
    const coreEnd = miningMasterTemplate.indexOf(coreEndMarker, coreStart);
    assert.ok(coreStart >= 0, `missing ${coreStartMarker}`);
    assert.ok(coreEnd > coreStart, `missing ${coreEndMarker}`);

    const bootStartMarker = "    if (\"serviceWorker\" in navigator) {";
    const bootEndMarker = "        {% else %}";
    const bootStart = miningMasterTemplate.lastIndexOf(bootStartMarker);
    const bootEnd = miningMasterTemplate.indexOf(bootEndMarker, bootStart);
    assert.ok(bootStart >= 0, "missing Mining Master role PWA boot");
    assert.ok(bootEnd > bootStart, "missing Mining Master role PWA boot terminator");

    const core = miningMasterTemplate.slice(coreStart, coreEnd);
    const boot = miningMasterTemplate
        .slice(bootStart, bootEnd)
        .replace("{% if mining_master_mobile_enabled %}", "")
        .replaceAll(
            "{% if role_app.role_code == 'mining_master' %}/{% else %}/mining-master/{% endif %}",
            "/mining-master/"
        );

    return [
        versionMatch[0],
        "var dispatcherSyncPendingCount = 0;",
        "var dispatcherSyncQueueFlushing = false;",
        "function readDispatcherSyncQueue() { return []; }",
        core,
        boot,
        "    }",
    ].join("\n");
}

function createRoleRuntime(options = {}) {
    const document = new DocumentStub();
    const serviceWorkerTarget = new EventTargetStub();
    const registrationTarget = new EventTargetStub();
    const waitingWorkerMessages = [];
    const deferredVersionReplies = [];
    const timers = new Map();
    let nextTimerId = 1;
    let registrationUpdateCalls = 0;
    let manualUpdateCalls = 0;

    function createWorker(version, options = {}) {
        const worker = {
            state: options.state || "activated",
            deferVersion: Boolean(options.deferVersion),
            addEventListener() {},
            postMessage(message, transfer) {
                if (options.waiting) {
                    waitingWorkerMessages.push(message);
                }
                if (
                    !message
                    || message.type !== "GET_VERSION"
                    || !transfer
                    || !transfer[0]
                ) {
                    return;
                }
                const reply = () => transfer[0].postMessage({version});
                if (worker.deferVersion) {
                    deferredVersionReplies.push({worker, reply});
                } else {
                    reply();
                }
            },
        };
        return worker;
    }

    const activeWorker = createWorker(options.activeVersion);
    const waitingWorker = createWorker(options.waitingVersion, {
        state: "installed",
        waiting: true,
        deferVersion: true,
    });
    const registration = Object.assign(registrationTarget, {
        active: activeWorker,
        waiting: options.hasWaitingWorker === false ? null : waitingWorker,
        installing: null,
        update() {
            registrationUpdateCalls += 1;
            return Promise.resolve(registration);
        },
    });
    const serviceWorker = Object.assign(serviceWorkerTarget, {
        controller: activeWorker,
        ready: Promise.resolve(registration),
        getRegistration() {
            return Promise.resolve(registration);
        },
    });
    const navigator = {serviceWorker};
    const windowTarget = new EventTargetStub();
    const localStorage = new StorageStub();

    function setTimeoutStub(callback) {
        const timerId = nextTimerId;
        nextTimerId += 1;
        timers.set(timerId, callback);
        return timerId;
    }

    function clearTimeoutStub(timerId) {
        timers.delete(timerId);
    }

    const guard = {
        getRegistration() {
            return Promise.resolve(registration);
        },
        getState() {
            return {
                ready: true,
                locked: false,
                server: {
                    shellVersion: options.serverVersion || options.activeVersion,
                    roleCode: options.roleCode,
                },
            };
        },
        requestManualUpdate() {
            manualUpdateCalls += 1;
            var result = typeof options.manualUpdateResult === "function"
                ? options.manualUpdateResult(registration)
                : options.manualUpdateResult;
            return Promise.resolve(result || {status: "current", registration});
        },
    };
    const window = Object.assign(windowTarget, {
        document,
        navigator,
        localStorage,
        MessageChannel: function MessageChannelStub() {
            const channel = createMessageChannel();
            this.port1 = channel.port1;
            this.port2 = channel.port2;
        },
        AppPwaContractGuard: guard,
        setTimeout: setTimeoutStub,
        clearTimeout: clearTimeoutStub,
        Promise,
        Date,
        console,
    });
    const context = vm.createContext({
        window,
        self: window,
        document,
        navigator,
        localStorage,
        MessageChannel: window.MessageChannel,
        CustomEvent: CustomEventStub,
        Promise,
        Date,
        console,
        setTimeout: setTimeoutStub,
        clearTimeout: clearTimeoutStub,
    });

    function activateWaitingWorker() {
        waitingWorker.state = "activated";
        waitingWorker.deferVersion = false;
        registration.active = waitingWorker;
        registration.waiting = null;
        serviceWorker.controller = waitingWorker;
        serviceWorker.dispatchEvent(new CustomEventStub("controllerchange"));
    }

    function resolveWorkerVersions(worker, missingMessage) {
        const replies = deferredVersionReplies.filter(
            (entry) => entry.worker === worker
        );
        for (let index = deferredVersionReplies.length - 1; index >= 0; index -= 1) {
            if (deferredVersionReplies[index].worker === worker) {
                deferredVersionReplies.splice(index, 1);
            }
        }
        assert.ok(replies.length, missingMessage);
        replies.forEach((entry) => entry.reply());
    }

    return {
        context,
        document,
        window,
        navigator,
        registration,
        waitingWorker,
        waitingWorkerMessages,
        get registrationUpdateCalls() {
            return registrationUpdateCalls;
        },
        get manualUpdateCalls() {
            return manualUpdateCalls;
        },
        deferActiveVersion() {
            activeWorker.deferVersion = true;
        },
        resolveActiveVersion() {
            resolveWorkerVersions(
                activeWorker,
                "missing delayed active-worker response"
            );
        },
        promoteWaitingWorker() {
            assert.ok(
                deferredVersionReplies.some(
                    (entry) => entry.worker === waitingWorker
                ),
                "the role runtime must have requested the waiting-worker version"
            );
            activateWaitingWorker();
        },
        activateWaitingWorker,
        resolveWaitingVersion() {
            resolveWorkerVersions(
                waitingWorker,
                "missing delayed waiting-worker response"
            );
        },
    };
}

function addExcavatorNodes(runtime) {
    const badge = runtime.document.addNode("[data-eo-pwa-update-badge]");
    badge.hidden = true;
    runtime.document.addNode("[data-eo-pwa-update-nav-target]");
    const modal = runtime.document.addNode("[data-eo-pwa-update-modal]");
    modal.hidden = true;
    runtime.document.addNode("[data-eo-pwa-current-version]");
    runtime.document.addNode("[data-eo-pwa-new-version]");
    runtime.document.addNode("[data-eo-pwa-update-status]");
    runtime.document.addNode("[data-eo-pwa-update-text]");
    runtime.document.addNode("[data-eo-pwa-update-later]");
    const applyButton = runtime.document.addNode("[data-eo-pwa-update-apply]");
    const checkButton = runtime.document.addNode("[data-eo-pwa-update-check]");
    const checkLabel = runtime.document.addNode("[data-eo-pwa-update-check-label]");
    runtime.document.addNode("[data-eo-pwa-update-check-version]");
    return {badge, modal, applyButton, checkButton, checkLabel};
}

function addMiningMasterNodes(runtime) {
    const badge = runtime.document.addNode("[data-mm-pwa-update-badge]");
    badge.hidden = true;
    runtime.document.addNode("[data-mm-pwa-update-nav-target]");
    const modal = runtime.document.addNode("[data-mm-pwa-update-modal]");
    modal.hidden = true;
    runtime.document.addNode("[data-mm-pwa-update-text]");
    runtime.document.addNode("[data-mm-pwa-update-status]");
    runtime.document.addNode("[data-mm-pwa-current-version]");
    runtime.document.addNode("[data-mm-pwa-new-version]");
    runtime.document.addNode("[data-mm-pwa-update-later]");
    const applyButton = runtime.document.addNode("[data-mm-pwa-update-apply]");
    const checkButton = runtime.document.addNode("[data-mm-pwa-check-update]");
    const status = runtime.document.querySelector("[data-mm-pwa-update-status]");
    return {badge, modal, applyButton, checkButton, status};
}

async function flushPromises(iterations = 20) {
    for (let index = 0; index < iterations; index += 1) {
        await Promise.resolve();
    }
}

test(
    "Excavator manual check stops on an unavailable shared-guard result",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "excavator_operator",
            activeVersion: "excavator-mobile-shell-v127",
            waitingVersion: "excavator-mobile-shell-v127",
            hasWaitingWorker: false,
            manualUpdateResult: {status: "unavailable", registration: null},
        });
        const nodes = addExcavatorNodes(runtime);
        runtime.context.excavatorShellVersion = "excavator-mobile-shell-v127";
        vm.runInContext(extractExcavatorPwaSource(), runtime.context, {
            filename: "templates/trips/excavator_work.html::PwaUpdates",
        });
        runtime.context.initExcavatorPwaUpdates();
        await flushPromises();

        nodes.checkButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();

        assert.equal(runtime.manualUpdateCalls, 1);
        assert.equal(runtime.registrationUpdateCalls, 0);
        assert.equal(nodes.checkLabel.textContent, "Недоступно");
        assert.equal(nodes.checkButton.classList.contains("is-current"), false);
    }
);

test(
    "Mining Master manual check stops on a shared-guard error before fallback registration",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "mining_master",
            activeVersion: "mining-master-mobile-shell-v120",
            waitingVersion: "mining-master-mobile-shell-v120",
            hasWaitingWorker: false,
            manualUpdateResult: {status: "error", registration: null},
        });
        const nodes = addMiningMasterNodes(runtime);
        vm.runInContext(extractMiningMasterPwaSource(), runtime.context, {
            filename: "templates/trips/dispatcher_control.html::MiningMasterPwaUpdates",
        });
        await flushPromises();

        await runtime.context.checkMiningMasterPwaUpdateManually();
        await flushPromises();

        assert.equal(runtime.manualUpdateCalls, 1);
        assert.equal(runtime.registrationUpdateCalls, 0);
        assert.match(nodes.status.textContent, /Не удалось проверить обновление/);
        assert.equal(nodes.modal.hidden, true, "an error must not be displayed as current");
    }
);

test(
    "Excavator ignores delayed waiting-worker version after promotion",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "excavator_operator",
            activeVersion: "excavator-mobile-shell-v125",
            waitingVersion: "excavator-mobile-shell-v127",
        });
        const nodes = addExcavatorNodes(runtime);
        runtime.context.excavatorShellVersion =
            "excavator-mobile-shell-v127";
        vm.runInContext(extractExcavatorPwaSource(), runtime.context, {
            filename: "templates/trips/excavator_work.html::PwaUpdates",
        });

        runtime.context.initExcavatorPwaUpdates();
        await flushPromises();
        runtime.promoteWaitingWorker();
        runtime.resolveWaitingVersion();
        await flushPromises();

        assert.equal(nodes.badge.hidden, true, "stale worker must show zero badges");
        assert.equal(nodes.modal.hidden, true, "stale worker must show zero modals");
        nodes.applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "an activated worker must receive zero repeated SKIP_WAITING messages"
        );
    }
);

test(
    "Mining Master ignores delayed waiting-worker version after promotion",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "mining_master",
            activeVersion: "mining-master-mobile-shell-v118",
            waitingVersion: "mining-master-mobile-shell-v120",
        });
        const nodes = addMiningMasterNodes(runtime);
        vm.runInContext(extractMiningMasterPwaSource(), runtime.context, {
            filename: "templates/trips/dispatcher_control.html::MiningMasterPwaUpdates",
        });

        await flushPromises();
        runtime.promoteWaitingWorker();
        runtime.resolveWaitingVersion();
        await flushPromises();

        assert.equal(nodes.badge.hidden, true, "stale worker must show zero badges");
        assert.equal(nodes.modal.hidden, true, "stale worker must show zero modals");
        nodes.applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "an activated worker must receive zero repeated SKIP_WAITING messages"
        );
    }
);

test(
    "Excavator never sends SKIP_WAITING after a shown worker becomes active",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "excavator_operator",
            activeVersion: "excavator-mobile-shell-v125",
            waitingVersion: "excavator-mobile-shell-v127",
        });
        const nodes = addExcavatorNodes(runtime);
        runtime.context.excavatorShellVersion =
            "excavator-mobile-shell-v127";
        vm.runInContext(extractExcavatorPwaSource(), runtime.context, {
            filename: "templates/trips/excavator_work.html::PwaUpdates",
        });

        runtime.context.initExcavatorPwaUpdates();
        await flushPromises();
        runtime.resolveWaitingVersion();
        await flushPromises();
        assert.equal(nodes.modal.hidden, false, "the waiting worker must be offered first");

        runtime.activateWaitingWorker();
        nodes.applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();

        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "the already active worker must receive zero repeated SKIP_WAITING messages"
        );
    }
);

test(
    "Mining Master never sends SKIP_WAITING after a shown worker becomes active",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "mining_master",
            activeVersion: "mining-master-mobile-shell-v118",
            waitingVersion: "mining-master-mobile-shell-v120",
        });
        const nodes = addMiningMasterNodes(runtime);
        vm.runInContext(extractMiningMasterPwaSource(), runtime.context, {
            filename: "templates/trips/dispatcher_control.html::MiningMasterPwaUpdates",
        });

        await flushPromises();
        runtime.resolveWaitingVersion();
        await flushPromises();
        assert.equal(nodes.modal.hidden, false, "the waiting worker must be offered first");

        runtime.activateWaitingWorker();
        nodes.applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();

        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "the already active worker must receive zero repeated SKIP_WAITING messages"
        );
    }
);

test(
    "Excavator closes a shown update when another tab activates its worker",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "excavator_operator",
            activeVersion: "excavator-mobile-shell-v125",
            waitingVersion: "excavator-mobile-shell-v127",
        });
        const nodes = addExcavatorNodes(runtime);
        runtime.context.excavatorShellVersion =
            "excavator-mobile-shell-v127";
        vm.runInContext(extractExcavatorPwaSource(), runtime.context, {
            filename: "templates/trips/excavator_work.html::PwaUpdates",
        });

        runtime.context.initExcavatorPwaUpdates();
        await flushPromises();
        runtime.resolveWaitingVersion();
        await flushPromises();
        assert.equal(nodes.modal.hidden, false, "the waiting-worker modal must be visible first");
        assert.equal(nodes.badge.hidden, false, "the waiting-worker badge must be visible first");

        runtime.activateWaitingWorker();
        await flushPromises();

        assert.equal(nodes.modal.hidden, true, "controllerchange must close the stale modal");
        assert.equal(nodes.badge.hidden, true, "controllerchange must hide the stale badge");
        assert.equal(
            runtime.window.localStorage.getItem("excavator-pwa-update-available"),
            null,
            "controllerchange must clear the stale stored update"
        );
        runtime.window.dispatchEvent(new CustomEventStub("app-pwa-contract-state"));
        await flushPromises();

        assert.equal(nodes.modal.hidden, true, "the following contract event must keep the stale modal closed");
        assert.equal(nodes.badge.hidden, true, "the following contract event must keep the stale badge hidden");
        nodes.applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "the stale modal path must send zero repeated SKIP_WAITING messages"
        );
    }
);

test(
    "Mining Master closes a shown update when another tab activates its worker",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "mining_master",
            activeVersion: "mining-master-mobile-shell-v118",
            waitingVersion: "mining-master-mobile-shell-v120",
        });
        const nodes = addMiningMasterNodes(runtime);
        vm.runInContext(extractMiningMasterPwaSource(), runtime.context, {
            filename: "templates/trips/dispatcher_control.html::MiningMasterPwaUpdates",
        });

        await flushPromises();
        runtime.resolveWaitingVersion();
        await flushPromises();
        assert.equal(nodes.modal.hidden, false, "the waiting-worker modal must be visible first");
        assert.equal(nodes.badge.hidden, false, "the waiting-worker badge must be visible first");

        runtime.activateWaitingWorker();
        await flushPromises();

        assert.equal(nodes.modal.hidden, true, "controllerchange must close the stale modal");
        assert.equal(nodes.badge.hidden, true, "controllerchange must hide the stale badge");
        assert.equal(
            runtime.window.localStorage.getItem("mining-master-pwa-update-available"),
            null,
            "controllerchange must clear the stale stored update"
        );
        runtime.window.dispatchEvent(new CustomEventStub("app-pwa-contract-state"));
        await flushPromises();

        assert.equal(nodes.modal.hidden, true, "the following contract event must keep the stale modal closed");
        assert.equal(nodes.badge.hidden, true, "the following contract event must keep the stale badge hidden");
        nodes.applyButton.dispatchEvent(new CustomEventStub("click"));
        await flushPromises();
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "the stale modal path must send zero repeated SKIP_WAITING messages"
        );
    }
);

test(
    "Excavator ignores a late old active-worker sync after the new sync wins",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "excavator_operator",
            activeVersion: "excavator-mobile-shell-v125",
            waitingVersion: "excavator-mobile-shell-v127",
            serverVersion: "excavator-mobile-shell-v127",
        });
        const nodes = addExcavatorNodes(runtime);
        runtime.context.excavatorShellVersion =
            "excavator-mobile-shell-v127";
        vm.runInContext(extractExcavatorPwaSource(), runtime.context, {
            filename: "templates/trips/excavator_work.html::PwaUpdates",
        });

        runtime.context.initExcavatorPwaUpdates();
        await flushPromises();
        runtime.resolveWaitingVersion();
        await flushPromises();
        assert.equal(nodes.modal.hidden, false, "the old waiting-worker modal must be visible first");
        assert.equal(nodes.badge.hidden, false, "the old waiting-worker badge must be visible first");

        runtime.deferActiveVersion();
        runtime.window.dispatchEvent(new CustomEventStub("app-pwa-contract-state"));
        await flushPromises();
        runtime.activateWaitingWorker();
        await flushPromises();

        assert.equal(nodes.modal.hidden, true, "the winning new sync must close the stale modal");
        assert.equal(nodes.badge.hidden, true, "the winning new sync must hide the stale badge");
        assert.equal(
            runtime.window.localStorage.getItem("excavator-pwa-update-available"),
            null,
            "the winning new sync must clear stale storage"
        );

        runtime.resolveActiveVersion();
        await flushPromises();

        assert.equal(nodes.modal.hidden, true, "late old sync must not resurrect modal");
        assert.equal(nodes.badge.hidden, true, "late old sync must not resurrect badge");
        assert.equal(
            runtime.window.localStorage.getItem("excavator-pwa-update-available"),
            null,
            "late old sync must not write stale storage"
        );
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "out-of-order sync must send zero repeated SKIP_WAITING messages"
        );
    }
);

test(
    "Mining Master ignores a late old active-worker sync after the new sync wins",
    async () => {
        const runtime = createRoleRuntime({
            roleCode: "mining_master",
            activeVersion: "mining-master-mobile-shell-v118",
            waitingVersion: "mining-master-mobile-shell-v120",
            serverVersion: "mining-master-mobile-shell-v120",
        });
        const nodes = addMiningMasterNodes(runtime);
        vm.runInContext(extractMiningMasterPwaSource(), runtime.context, {
            filename: "templates/trips/dispatcher_control.html::MiningMasterPwaUpdates",
        });

        await flushPromises();
        runtime.resolveWaitingVersion();
        await flushPromises();
        assert.equal(nodes.modal.hidden, false, "the old waiting-worker modal must be visible first");
        assert.equal(nodes.badge.hidden, false, "the old waiting-worker badge must be visible first");

        runtime.deferActiveVersion();
        runtime.window.dispatchEvent(new CustomEventStub("app-pwa-contract-state"));
        await flushPromises();
        runtime.activateWaitingWorker();
        await flushPromises();

        assert.equal(nodes.modal.hidden, true, "the winning new sync must close the stale modal");
        assert.equal(nodes.badge.hidden, true, "the winning new sync must hide the stale badge");
        assert.equal(
            runtime.window.localStorage.getItem("mining-master-pwa-update-available"),
            null,
            "the winning new sync must clear stale storage"
        );

        runtime.resolveActiveVersion();
        await flushPromises();

        assert.equal(nodes.modal.hidden, true, "late old sync must not resurrect modal");
        assert.equal(nodes.badge.hidden, true, "late old sync must not resurrect badge");
        assert.equal(
            runtime.window.localStorage.getItem("mining-master-pwa-update-available"),
            null,
            "late old sync must not write stale storage"
        );
        assert.equal(
            runtime.waitingWorkerMessages.filter(
                (message) => message && message.type === "SKIP_WAITING"
            ).length,
            0,
            "out-of-order sync must send zero repeated SKIP_WAITING messages"
        );
    }
);
