#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const backendRoot = path.resolve(__dirname, "../../..");
const deputyScriptPath = path.join(
    backendRoot,
    "static",
    "js",
    "deputy-mining-manager-v3.js"
);
const deputyWorkerPath = path.join(
    backendRoot,
    "assignments",
    "deputy_views.py"
);
const deputyScript = fs.readFileSync(deputyScriptPath, "utf8");
const deputyWorkerModule = fs.readFileSync(deputyWorkerPath, "utf8");

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
        if (!nextEvent.type) {
            throw new Error("Event type is required");
        }
        if (!nextEvent.target) {
            nextEvent.target = this;
        }
        nextEvent.currentTarget = this;
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
        names.filter(Boolean).forEach((name) => this.values.add(name));
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

    setFromString(value) {
        this.values = new Set(String(value || "").split(/\s+/).filter(Boolean));
    }

    toString() {
        return Array.from(this.values).join(" ");
    }
}

function matchesSelector(element, selector) {
    if (!element || !selector) {
        return false;
    }
    if (selector.startsWith(".")) {
        return element.classList.contains(selector.slice(1));
    }
    if (selector.startsWith("#")) {
        return element.getAttribute("id") === selector.slice(1);
    }
    const attributeMatch = selector.match(
        /^(?:([a-zA-Z0-9_-]+))?\[([a-zA-Z0-9_-]+)(?:=['"]([^'"]*)['"])?\]$/
    );
    if (attributeMatch) {
        const [, tagName, attributeName, attributeValue] = attributeMatch;
        if (
            tagName
            && element.nodeName.toLowerCase() !== tagName.toLowerCase()
        ) {
            return false;
        }
        if (!element.hasAttribute(attributeName)) {
            return false;
        }
        return typeof attributeValue === "undefined"
            || element.getAttribute(attributeName) === attributeValue;
    }
    return element.nodeName.toLowerCase() === selector.toLowerCase();
}

class ElementStub extends EventTargetStub {
    constructor(nodeName = "div", attributes = {}) {
        super();
        this.nodeName = String(nodeName).toUpperCase();
        this.attributes = new Map();
        this.classList = new ClassListStub();
        this.children = [];
        this.parentNode = null;
        this.hidden = false;
        this.disabled = false;
        this.draggable = false;
        this.value = "";
        this.textContent = "";
        this.tabIndex = 0;
        this.style = {
            setProperty() {},
            removeProperty() {},
        };
        Object.entries(attributes).forEach(([name, value]) => {
            this.setAttribute(name, value);
        });
    }

    get className() {
        return this.classList.toString();
    }

    set className(value) {
        this.classList.setFromString(value);
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    hasAttribute(name) {
        return this.attributes.has(name);
    }

    removeAttribute(name) {
        this.attributes.delete(name);
    }

    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    replaceChildren(...children) {
        this.children.forEach((child) => {
            child.parentNode = null;
        });
        this.children = [];
        children.forEach((child) => this.appendChild(child));
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

    contains(candidate) {
        let current = candidate;
        while (current) {
            if (current === this) {
                return true;
            }
            current = current.parentNode;
        }
        return false;
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

class DocumentStub extends EventTargetStub {
    constructor() {
        super();
        this.cookie = "csrftoken=test-token";
        this.body = new ElementStub("body");
    }

    createElement(nodeName) {
        return new ElementStub(nodeName);
    }

    getElementById(id) {
        return this.body.querySelector(`#${id}`);
    }

    querySelector(selector) {
        if (matchesSelector(this.body, selector)) {
            return this.body;
        }
        return this.body.querySelector(selector);
    }

    querySelectorAll(selector) {
        const matches = this.body.querySelectorAll(selector);
        if (matchesSelector(this.body, selector)) {
            matches.unshift(this.body);
        }
        return matches;
    }
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
}

function planningPayload({assigned = false, version = 1} = {}) {
    const employee = {
        id: 101,
        full_name: "Иванов Иван",
        position_label: "Водитель",
    };
    return {
        plan: {
            id: 7,
            version,
            revision: 1,
            editable: true,
            work_date_label: "26.07.2026",
            updated_at_label: "",
        },
        role: {
            code: "driver",
            label: "Водители",
            category_label: "Самосвалы",
        },
        endpoints: {
            slot: "/deputy-mining-manager/api/slot/",
            publish: "/deputy-mining-manager/api/publish/",
            temporary_transfer_request: "/deputy-mining-manager/api/transfer/",
            export: "/deputy-mining-manager/export/7/",
        },
        summary: {
            conflict_count: 0,
            unfilled_count: assigned ? 1 : 2,
        },
        temporary_transfer: {
            available: false,
            candidates: [],
            target_specializations: [],
            watch_periods: [],
        },
        categories: [],
        employees: assigned ? [] : [employee],
        rows: [{
            equipment: {
                id: 501,
                label: "БЕЛАЗ 501",
                model_label: "БЕЛАЗ",
                status_label: "Исправен",
            },
            slots: [
                {
                    shift_type: "day",
                    label: "День",
                    employee: assigned ? employee : null,
                },
                {
                    shift_type: "night",
                    label: "Ночь",
                    employee: null,
                },
            ],
        }],
    };
}

function appendDataNode(document, payload) {
    const node = new ElementStub("script", {id: "deputy-planning-data"});
    node.textContent = JSON.stringify(payload);
    document.body.appendChild(node);
    return node;
}

function appendRootNode(document, attributeName, nodeName = "div") {
    const node = new ElementStub(nodeName, {[attributeName]: ""});
    document.body.appendChild(node);
    return node;
}

function createDeputyRuntime(options = {}) {
    const document = new DocumentStub();
    const window = new EventTargetStub();
    const shell = appendRootNode(document, "data-admin-theme");
    const root = appendRootNode(document, "data-deputy-planning-root", "main");
    appendDataNode(document, planningPayload());

    const searchInput = new ElementStub("input", {"data-planning-search": ""});
    const employeePool = new ElementStub("section", {"data-employee-pool-drop": ""});
    const employeeList = new ElementStub("div", {"data-employee-list": ""});
    const employeeEmpty = new ElementStub("p", {"data-employee-empty": ""});
    const board = new ElementStub("div", {"data-assignment-board": ""});
    const boardEmpty = new ElementStub("p", {"data-board-empty": ""});
    const autosaveState = new ElementStub("span", {"data-autosave-state": ""});
    const autosaveText = new ElementStub("span", {"data-autosave-text": ""});
    const publishButton = new ElementStub("button", {"data-publish-button": ""});
    const exportButton = new ElementStub("a", {"data-export-excel": ""});

    [
        searchInput,
        employeePool,
        employeeList,
        employeeEmpty,
        board,
        boardEmpty,
        autosaveState,
        autosaveText,
        publishButton,
        exportButton,
    ].forEach((node) => root.appendChild(node));

    const fetchCalls = [];
    let locked = options.locked !== false;
    const contractGuard = {
        getState() {
            if (typeof options.getContractState === "function") {
                return options.getContractState();
            }
            return {locked};
        },
    };
    const fetchStub = async (url, init = {}) => {
        fetchCalls.push({url: String(url), init});
        return {
            ok: true,
            async json() {
                return {
                    ok: true,
                    payload: planningPayload({assigned: true, version: 2}),
                };
            },
        };
    };

    Object.assign(window, {
        document,
        AppPwaContractGuard: contractGuard,
        localStorage: new StorageStub(),
        innerWidth: 1280,
        innerHeight: 720,
        fetch: fetchStub,
        location: {reload() {}},
        getComputedStyle() {
            return {getPropertyValue() { return ""; }};
        },
        setTimeout() {
            return 1;
        },
        clearTimeout() {},
        isAppRoleReadonly() {
            return false;
        },
    });

    const instrumentedSource = deputyScript.replace(
        "    var initialSavedLabel = state.plan.updated_at_label",
        "    window.__deputyContractTestHooks = {"
            + " postJson: postJson,"
            + " saveSlot: saveSlot,"
            + " planEditable: planEditable"
            + " };\n"
            + "    var initialSavedLabel = state.plan.updated_at_label"
    );
    assert.notEqual(
        instrumentedSource,
        deputyScript,
        "test hook marker no longer matches production deputy script"
    );
    vm.runInNewContext(
        instrumentedSource,
        {
            window,
            document,
            navigator: {},
            console,
            URL,
            Promise,
            Map,
            Set,
            JSON,
            Math,
            Number,
            String,
            Boolean,
            Array,
            Object,
            Error,
            decodeURIComponent,
        },
        {filename: deputyScriptPath}
    );

    return {
        window,
        document,
        shell,
        root,
        searchInput,
        employeePool,
        employeeList,
        employeeEmpty,
        board,
        autosaveText,
        fetchCalls,
        setLocked(value) {
            locked = Boolean(value);
        },
        contractGuard,
        hooks: window.__deputyContractTestHooks,
    };
}

function dragEvent(type, payload) {
    const values = new Map();
    if (payload) {
        values.set("application/json", JSON.stringify(payload));
        if (payload.employeeId) {
            values.set("text/plain", String(payload.employeeId));
        }
    }
    return {
        type,
        clientX: 200,
        clientY: 160,
        defaultPrevented: false,
        dataTransfer: {
            effectAllowed: "",
            dropEffect: "",
            setData(key, value) {
                values.set(key, String(value));
            },
            getData(key) {
                return values.get(key) || "";
            },
            setDragImage() {},
        },
        preventDefault() {
            this.defaultPrevented = true;
        },
        stopPropagation() {},
        stopImmediatePropagation() {},
    };
}

async function flushPromises() {
    for (let index = 0; index < 12; index += 1) {
        await Promise.resolve();
    }
}

test(
    "locked deputy drag source stays inactive and artificial drop sends no request",
    async () => {
        const runtime = createDeputyRuntime({locked: true});
        const card = runtime.employeeList.querySelector(".deputy-employee-card");
        const slot = runtime.board.querySelector(".deputy-slot");
        const savedLabelBefore = runtime.autosaveText.textContent;

        assert.ok(card, "employee card was not rendered");
        assert.ok(slot, "planning slot was not rendered");
        assert.equal(card.draggable, false);

        slot.dispatchEvent(dragEvent("drop", {employeeId: 101}));
        await flushPromises();

        assert.equal(runtime.fetchCalls.length, 0);
        assert.equal(
            runtime.board.querySelector(".deputy-slot-person"),
            null,
            "locked drop changed the rendered plan"
        );
        assert.equal(runtime.autosaveText.textContent, savedLabelBefore);

        runtime.searchInput.value = "нет совпадений";
        runtime.searchInput.dispatchEvent({type: "input"});
        assert.equal(runtime.employeeList.hidden, true);
        assert.equal(runtime.fetchCalls.length, 0);

        runtime.searchInput.value = "";
        runtime.setLocked(false);
        runtime.window.dispatchEvent({
            type: "app-pwa-contract-state",
            detail: {locked: false},
        });
        const unlockedCard = runtime.employeeList.querySelector(
            ".deputy-employee-card"
        );
        assert.ok(unlockedCard, "safe live unlock did not rerender the board");
        assert.equal(unlockedCard.draggable, true);
    }
);

test("saveSlot and postJson both recheck a locked contract", async () => {
    const runtime = createDeputyRuntime({locked: true});
    const row = planningPayload().rows[0];
    const slot = row.slots[0];
    const savedLabelBefore = runtime.autosaveText.textContent;

    await runtime.hooks.saveSlot(row, slot, 101, null);
    await assert.rejects(
        runtime.hooks.postJson("/deputy-mining-manager/api/slot/", {}),
        (error) => error && error.code === "app_contract_locked"
    );

    assert.equal(runtime.fetchCalls.length, 0);
    assert.equal(runtime.autosaveText.textContent, savedLabelBefore);
});

test("unlocked deputy drag-and-drop persists exactly once", async () => {
    const runtime = createDeputyRuntime({locked: false});
    const card = runtime.employeeList.querySelector(".deputy-employee-card");
    const slot = runtime.board.querySelector(".deputy-slot");

    assert.equal(card.draggable, true);
    card.dispatchEvent(dragEvent("dragstart"));
    slot.dispatchEvent(dragEvent("drop"));
    await flushPromises();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.fetchCalls[0].init.method, "POST");
    assert.ok(
        runtime.board.querySelector(".deputy-slot-person"),
        "server payload was not applied after the single unlocked POST"
    );
});

class CacheStub {
    constructor() {
        this.entries = new Map();
    }

    key(request) {
        return request && request.url ? request.url : String(request);
    }

    async put(request, response) {
        this.entries.set(this.key(request), response.clone());
    }

    async match(request) {
        const response = this.entries.get(this.key(request));
        return response ? response.clone() : undefined;
    }

    async keys() {
        return Array.from(this.entries.keys(), (url) => new Request(url));
    }

    async delete(request) {
        return this.entries.delete(this.key(request));
    }
}

function extractDeputyWorker() {
    const match = deputyWorkerModule.match(
        /DEPUTY_SERVICE_WORKER_JS = r"""([\s\S]*?)"""/
    );
    assert.ok(match, "DEPUTY_SERVICE_WORKER_JS was not found");
    return match[1];
}

test(
    "deputy static cache keeps the query string and never serves an old cache-buster",
    async () => {
        const workerSource = extractDeputyWorker();
        const cacheNameMatch = workerSource.match(
            /const CACHE_NAME = "([^"]+)";/
        );
        assert.ok(cacheNameMatch, "deputy worker CACHE_NAME was not found");
        const activeCacheName = cacheNameMatch[1];
        const cache = new CacheStub();
        const cacheNames = new Map();
        cacheNames.set(activeCacheName, cache);
        const listeners = new Map();
        const self = {
            location: {origin: "https://deputy.localhost"},
            clients: {async claim() {}},
            async skipWaiting() {},
            addEventListener(type, listener) {
                listeners.set(type, listener);
            },
        };
        const caches = {
            async open(name) {
                if (!cacheNames.has(name)) {
                    cacheNames.set(name, new CacheStub());
                }
                return cacheNames.get(name);
            },
            async keys() {
                return Array.from(cacheNames.keys());
            },
            async delete(name) {
                return cacheNames.delete(name);
            },
        };
        const context = {
            self,
            caches,
            fetch: async () => {
                throw new Error("offline");
            },
            Request,
            Response,
            URL,
            Set,
            Promise,
            console,
        };
        vm.runInNewContext(workerSource, context, {
            filename: "assignments/deputy_views.py::DEPUTY_SERVICE_WORKER_JS",
        });

        const oldUrl = (
            "https://deputy.localhost/static/js/"
            + "deputy-mining-manager-v3.js?v=old"
        );
        const newUrl = (
            "https://deputy.localhost/static/js/"
            + "deputy-mining-manager-v3.js?v=new"
        );
        const activeCache = await caches.open(activeCacheName);
        await activeCache.put(
            new Request(oldUrl.split("?")[0]),
            new Response("old-worker")
        );
        await activeCache.put(new Request(oldUrl), new Response("old-worker"));

        const oldResponse = await context.networkFirstStatic(
            new Request(oldUrl)
        );
        const newResponse = await context.networkFirstStatic(
            new Request(newUrl)
        );

        assert.equal(await oldResponse.text(), "old-worker");
        assert.equal(newResponse.status, 503);
        assert.notEqual(await newResponse.text(), "old-worker");
    }
);
