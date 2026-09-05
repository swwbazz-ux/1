"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const TEMPLATE_ROOT = path.resolve(__dirname, "..", "..", "..", "templates");
const ACHIEVEMENT_SOURCE = fs.readFileSync(
    path.join(TEMPLATE_ROOT, "includes", "achievement_prize.html"),
    "utf8"
);
const DRIVER_SOURCE = fs.readFileSync(
    path.join(TEMPLATE_ROOT, "users", "driver_shift.html"),
    "utf8"
);
const EXCAVATOR_SOURCE = fs.readFileSync(
    path.join(TEMPLATE_ROOT, "trips", "excavator_work.html"),
    "utf8"
);


function extractInlineScript(source, label) {
    const start = source.indexOf("<script>");
    const end = source.lastIndexOf("</script>");
    assert.notEqual(start, -1, `${label}: opening script tag was not found.`);
    assert.notEqual(end, -1, `${label}: closing script tag was not found.`);
    return source.slice(start + "<script>".length, end);
}


function findClosingBrace(source, openingBrace, label) {
    let depth = 0;
    let quote = "";
    let escaped = false;
    let lineComment = false;
    let blockComment = false;

    for (let index = openingBrace; index < source.length; index += 1) {
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
        } else if (character === "}") {
            depth -= 1;
            if (depth === 0) return index;
        }
    }
    assert.fail(`${label}: closing brace was not found.`);
}


function extractFunctionDeclaration(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${label}: function signature was not found.`);
    const openingBrace = source.indexOf("{", start + signature.length);
    assert.notEqual(openingBrace, -1, `${label}: opening brace was not found.`);
    const end = findClosingBrace(source, openingBrace, label);
    return source.slice(start, end + 1);
}


function extractAssignedFunction(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${label}: assigned function was not found.`);
    const openingBrace = source.indexOf("{", start + signature.length);
    assert.notEqual(openingBrace, -1, `${label}: opening brace was not found.`);
    const end = findClosingBrace(source, openingBrace, label);
    return `${source.slice(start, end + 1)};`;
}


class FakeTimers {
    constructor() {
        this.now = 0;
        this.nextId = 1;
        this.tasks = new Map();
        this.intervalDelays = [];
    }

    setTimeout(callback, delay) {
        return this.add(callback, delay, 0);
    }

    clearTimeout(id) {
        this.tasks.delete(id);
    }

    setInterval(callback, delay) {
        const normalizedDelay = Math.max(1, Number(delay) || 0);
        this.intervalDelays.push(normalizedDelay);
        return this.add(callback, normalizedDelay, normalizedDelay);
    }

    clearInterval(id) {
        this.tasks.delete(id);
    }

    add(callback, delay, interval) {
        const id = this.nextId;
        this.nextId += 1;
        this.tasks.set(id, {
            callback,
            dueAt: this.now + Math.max(0, Number(delay) || 0),
            interval
        });
        return id;
    }

    advance(milliseconds) {
        const target = this.now + Number(milliseconds);
        let executions = 0;
        while (true) {
            let selectedId = null;
            let selectedTask = null;
            this.tasks.forEach((task, id) => {
                if (
                    task.dueAt <= target &&
                    (!selectedTask || task.dueAt < selectedTask.dueAt)
                ) {
                    selectedId = id;
                    selectedTask = task;
                }
            });
            if (!selectedTask) break;
            executions += 1;
            assert.ok(executions < 100000, "Fake timer entered an infinite loop.");
            this.now = selectedTask.dueAt;
            if (selectedTask.interval) {
                selectedTask.dueAt += selectedTask.interval;
            } else {
                this.tasks.delete(selectedId);
            }
            selectedTask.callback();
        }
        this.now = target;
    }
}


class FakeClassList {
    constructor() {
        this.values = new Set();
    }

    add(...values) {
        values.forEach((value) => this.values.add(value));
    }

    remove(...values) {
        values.forEach((value) => this.values.delete(value));
    }

    contains(value) {
        return this.values.has(value);
    }
}


class FakeElement {
    constructor(document, tagName = "div") {
        this.ownerDocument = document;
        this.tagName = String(tagName).toUpperCase();
        this.dataset = {};
        this.hidden = false;
        this.className = "";
        this.classList = new FakeClassList();
        this.children = [];
        this.style = {setProperty() {}};
        this.attributes = new Map();
        this.textContent = "";
        this.onclick = null;
        this._selectorMap = new Map();
        this._innerHTML = "";
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    removeAttribute(name) {
        this.attributes.delete(name);
        if (name === "src") this.src = "";
    }

    appendChild(child) {
        this.children.push(child);
        return child;
    }

    querySelector(selector) {
        return this._selectorMap.get(selector) || null;
    }

    contains() {
        return false;
    }

    replaceWith(replacement) {
        if (this.ownerDocument.driverShell === this) {
            this.ownerDocument.driverShell = replacement;
        }
        if (this.ownerDocument.excavatorShell === this) {
            this.ownerDocument.excavatorShell = replacement;
        }
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
        if (this.attributes.has("data-achievement-prize-modal")) {
            this.ownerDocument.buildAchievementModal(this);
        }
    }

    get innerHTML() {
        return this._innerHTML;
    }
}


class FakeDocument {
    constructor() {
        this.hidden = false;
        this.listeners = new Map();
        this.modal = null;
        this.driverShell = null;
        this.excavatorShell = null;
        this.activeElement = null;
        this.body = new FakeElement(this, "body");
        this.body.dataset = {};
        const originalAppendChild = this.body.appendChild.bind(this.body);
        this.body.appendChild = (child) => {
            if (child.attributes.has("data-achievement-prize-modal")) {
                this.modal = child;
            }
            return originalAppendChild(child);
        };
    }

    get visibilityState() {
        return this.hidden ? "hidden" : "visible";
    }

    createElement(tagName) {
        return new FakeElement(this, tagName);
    }

    querySelector(selector) {
        if (selector === "input[name='csrfmiddlewaretoken']") {
            return {value: "csrf-token"};
        }
        if (selector === "[data-achievement-prize-modal]") {
            return this.modal;
        }
        if (selector === "[data-driver-shell]") {
            return this.driverShell;
        }
        if (selector === "[data-eo-shell]") {
            return this.excavatorShell;
        }
        return null;
    }

    getElementById() {
        return null;
    }

    addEventListener(type, listener) {
        if (!this.listeners.has(type)) this.listeners.set(type, []);
        this.listeners.get(type).push(listener);
    }

    dispatch(type) {
        (this.listeners.get(type) || []).forEach((listener) => listener({type}));
    }

    buildAchievementModal(modal) {
        const stage = new FakeElement(this, "section");
        const confetti = new FakeElement(this, "div");
        const title = new FakeElement(this, "strong");
        const percent = new FakeElement(this, "em");
        const image = new FakeElement(this, "img");
        const save = new FakeElement(this, "button");
        const close = new FakeElement(this, "button");
        stage._selectorMap.set("[data-achievement-confetti]", confetti);
        modal._selectorMap.set("[data-achievement-stage]", stage);
        modal._selectorMap.set("[data-achievement-title]", title);
        modal._selectorMap.set("[data-achievement-percent]", percent);
        modal._selectorMap.set("[data-achievement-image]", image);
        modal._selectorMap.set("[data-achievement-save]", save);
        modal._selectorMap.set("[data-achievement-close]", close);
    }
}


function jsonResponse(payload, ok = true) {
    return {
        ok,
        json() {
            return Promise.resolve(payload);
        }
    };
}


function networkFailure(message = "offline") {
    return {
        then(resolve, reject) {
            reject(new Error(message));
        }
    };
}


function createDeferred() {
    let resolve;
    const promise = new Promise((resolver) => {
        resolve = resolver;
    });
    return {promise, resolve};
}


async function flushAsync(rounds = 12) {
    for (let index = 0; index < rounds; index += 1) {
        await Promise.resolve();
    }
}


function createShell(document, role, equipmentId) {
    const shell = new FakeElement(document, "main");
    if (role === "driver") {
        shell.dataset.activeTab = "work";
        shell.dataset.driverCurrentTruckId = String(equipmentId);
        document.driverShell = shell;
    } else {
        shell.dataset.eoActiveTab = "trucks";
        shell.dataset.eoCurrentExcavatorId = String(equipmentId);
        shell.dataset.eoShiftExcavatorId = String(equipmentId);
        document.excavatorShell = shell;
    }
    return shell;
}


function createRuntime({role = "driver", equipmentId = 17} = {}) {
    const timers = new FakeTimers();
    const document = new FakeDocument();
    createShell(document, role, equipmentId);
    const fetchCalls = [];
    const queuedResponses = [];
    const windowListeners = new Map();
    const defaultPayload = {unlocked: false, shown: false};

    function fetchStub(url, options = {}) {
        fetchCalls.push({url: String(url), options});
        const queued = queuedResponses.length
            ? queuedResponses.shift()
            : jsonResponse(defaultPayload);
        return Promise.resolve(queued).then((value) => value);
    }

    const windowObject = {
        document,
        fetch: fetchStub,
        location: {href: ""},
        open() {
            return {};
        },
        setTimeout: timers.setTimeout.bind(timers),
        clearTimeout: timers.clearTimeout.bind(timers),
        setInterval: timers.setInterval.bind(timers),
        clearInterval: timers.clearInterval.bind(timers),
        addEventListener(type, listener) {
            if (!windowListeners.has(type)) windowListeners.set(type, []);
            windowListeners.get(type).push(listener);
        },
        dispatch(type, detail) {
            (windowListeners.get(type) || []).forEach((listener) => listener({type, detail}));
        },
        sessionStorage: {
            setItem() {},
            getItem() {
                return null;
            }
        }
    };
    windowObject.window = windowObject;
    const context = vm.createContext({
        window: windowObject,
        document,
        fetch: fetchStub,
        console,
        Math,
        Promise,
        setTimeout: windowObject.setTimeout,
        clearTimeout: windowObject.clearTimeout,
        setInterval: windowObject.setInterval,
        clearInterval: windowObject.clearInterval
    });
    vm.runInContext(
        extractInlineScript(ACHIEVEMENT_SOURCE, "achievement prize"),
        context,
        {filename: "achievement_prize.html"}
    );

    return {
        context,
        document,
        fetchCalls,
        timers,
        window: windowObject,
        enqueueResponse(responseOrPromise) {
            queuedResponses.push(responseOrPromise);
        },
        clearFetchCalls() {
            fetchCalls.splice(0, fetchCalls.length);
        }
    };
}


async function bindAndRunInitial(runtime) {
    runtime.window.bindAchievementPrizeUnlock();
    runtime.timers.advance(2000);
    await flushAsync();
}


function completedContext({truckId = 17, excavatorId = 31} = {}) {
    return {
        version: 42,
        events: [{
            type: "trip_changed",
            payload: {
                action: "trip_unloaded",
                status: "completed",
                trip_id: 9001,
                truck_id: truckId,
                excavator_id: excavatorId
            }
        }]
    };
}


async function triggerAndSettle(runtime, context) {
    runtime.window.checkAchievementPrize(context);
    runtime.timers.advance(1000);
    await flushAsync();
}


function achievementGetCalls(runtime) {
    return runtime.fetchCalls.filter((call) => (
        call.url === "/api/achievements/current/" &&
        call.options.method === "GET"
    ));
}


function truncatedContext(version = 42) {
    return {
        version,
        eventsTruncated: true,
        events: [{
            type: "assignment_changed",
            payload: {truck_id: 999, excavator_id: 999}
        }]
    };
}


function installDriverFragmentHandler(runtime, nextTruckId = 17) {
    const functionSource = extractAssignedFunction(
        DRIVER_SOURCE,
        "window.applyOperationalStateRefresh = function (context)",
        "Driver operational refresh"
    );
    runtime.window.bindDriverMobileShell = function () {};
    runtime.window.AppOperationalFragment = {
        request() {
            return Promise.resolve({html: "<main></main>"});
        },
        parseRoot() {
            return createShell(runtime.document, "driver", nextTruckId);
        }
    };
    vm.runInContext(
        [
            "function isDriverOperationalRefreshUnsafe() { return false; }",
            "function syncDriverTabMarkup(shell, tab) { shell.dataset.activeTab = tab; }",
            functionSource
        ].join("\n"),
        runtime.context,
        {filename: "driver_shift.fragment.js"}
    );
}


function installExcavatorFragmentHandler(runtime, nextExcavatorId = 31) {
    const refreshSource = extractFunctionDeclaration(
        EXCAVATOR_SOURCE,
        "function refreshExcavatorWorkFromServer(options)",
        "Excavator fragment refresh"
    );
    const applySource = extractAssignedFunction(
        EXCAVATOR_SOURCE,
        "window.applyOperationalStateRefresh = function (context)",
        "Excavator operational refresh"
    );
    runtime.window.initExcavatorWorkShell = function () {};
    runtime.window.AppOperationalFragment = {
        request() {
            return Promise.resolve({html: "<main></main>"});
        },
        parseRoot() {
            return createShell(runtime.document, "excavator", nextExcavatorId);
        }
    };
    vm.runInContext(
        [
            "function isExcavatorRefreshUnsafe() { return false; }",
            "function scheduleExcavatorViewportHeightSync() {}",
            "function readExcavatorAssignmentSnapshot() { return {}; }",
            "function syncExcavatorAssignmentSnapshot() {}",
            "var excavatorWorkMutationGeneration = 0;",
            "function storeExcavatorRealtimeVersion(version) {",
            "  if (version) document.body.dataset.operationalStateVersion = String(version);",
            "}",
            "function hasExcavatorRelevantEvents(events) {",
            "  return Array.isArray(events) && events.length > 0;",
            "}",
            refreshSource,
            applySource
        ].join("\n"),
        runtime.context,
        {filename: "excavator_work.fragment.js"}
    );
}


test("initial check is exactly one request and twelve hours add no polling", async () => {
    const runtime = createRuntime();
    await bindAndRunInitial(runtime);

    assert.equal(runtime.fetchCalls.length, 1, "Initial check must issue exactly one GET.");
    runtime.timers.advance(12 * 60 * 60 * 1000);
    await flushAsync();

    assert.equal(
        runtime.fetchCalls.length,
        1,
        "Achievement state must not be polled during twelve idle hours."
    );
    assert.deepEqual(
        runtime.timers.intervalDelays,
        [],
        "Achievement runtime must not install a recurring timer."
    );
});


test("irrelevant realtime and generic fragment contexts issue no achievement GET", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();

    const irrelevantContexts = [
        {version: 2, events: [{type: "assignment_changed", payload: {truck_id: 17}}]},
        {
            version: 3,
            events: [{
                type: "trip_changed",
                payload: {status: "loaded_waiting_unload", truck_id: 17, excavator_id: 31}
            }]
        },
        completedContext({truckId: 999, excavatorId: 31}),
        {version: 5, events: [], source: "fragment"}
    ];
    irrelevantContexts.forEach((context) => runtime.window.checkAchievementPrize(context));
    runtime.timers.advance(1000);
    await flushAsync();

    assert.equal(
        runtime.fetchCalls.length,
        0,
        "Only a completed trip for the current equipment may trigger a follow-up GET."
    );
});


test("truncated delta without the matching completion performs one equipment catch-up GET", async () => {
    for (const role of ["driver", "excavator"]) {
        const equipmentId = role === "driver" ? 17 : 31;
        const runtime = createRuntime({role, equipmentId});
        await bindAndRunInitial(runtime);
        runtime.clearFetchCalls();

        runtime.window.checkAchievementPrize(truncatedContext(51));
        runtime.timers.advance(1000);
        await flushAsync();

        assert.equal(
            achievementGetCalls(runtime).length,
            1,
            `${role} must catch up exactly once when the matching completion was truncated.`
        );
    }
});


test("truncated delta is ignored without an active equipment shift", async () => {
    for (const role of ["driver", "excavator"]) {
        const equipmentId = role === "driver" ? 17 : 31;
        const runtime = createRuntime({role, equipmentId});
        await bindAndRunInitial(runtime);
        runtime.clearFetchCalls();
        if (role === "driver") {
            runtime.document.driverShell.dataset.driverCurrentTruckId = "";
        } else {
            runtime.document.excavatorShell.dataset.eoCurrentExcavatorId = "";
        }

        runtime.window.checkAchievementPrize(truncatedContext(52));
        runtime.timers.advance(1000);
        await flushAsync();

        assert.equal(
            achievementGetCalls(runtime).length,
            0,
            `${role} without an open equipment shift must not run truncated catch-up.`
        );
    }
});


test("Excavator assignment without an open shift does not exact-match a completed trip", async () => {
    const runtime = createRuntime({role: "excavator", equipmentId: 31});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    runtime.document.excavatorShell.dataset.eoCurrentExcavatorId = "";
    runtime.document.excavatorShell.dataset.eoShiftExcavatorId = "31";

    await triggerAndSettle(
        runtime,
        completedContext({truckId: 17, excavatorId: 31})
    );

    assert.equal(
        achievementGetCalls(runtime).length,
        0,
        "Assigned equipment must not count as the operator's open shift."
    );
});


test("Excavator assignment without an open shift does not run truncated catch-up", async () => {
    const runtime = createRuntime({role: "excavator", equipmentId: 31});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    runtime.document.excavatorShell.dataset.eoCurrentExcavatorId = "";
    runtime.document.excavatorShell.dataset.eoShiftExcavatorId = "31";

    await triggerAndSettle(runtime, truncatedContext(53));

    assert.equal(
        achievementGetCalls(runtime).length,
        0,
        "A truncated delta must require the operator's real open shift."
    );
});


test("truncated burst and delayed initial collapse into one achievement GET", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    runtime.window.bindAchievementPrizeUnlock();
    runtime.timers.advance(1200);

    for (let index = 0; index < 8; index += 1) {
        runtime.window.checkAchievementPrize(truncatedContext(61));
    }
    runtime.timers.advance(1000);
    await flushAsync();

    assert.equal(
        achievementGetCalls(runtime).length,
        1,
        "Truncated catch-up and the pending initial check must share one request."
    );
});


test("truncated catch-up survives the real Driver and Excavator fragment apply", async () => {
    const cases = [
        {
            role: "driver",
            equipmentId: 17,
            install: installDriverFragmentHandler
        },
        {
            role: "excavator",
            equipmentId: 31,
            install: installExcavatorFragmentHandler
        }
    ];
    for (const testCase of cases) {
        const runtime = createRuntime(testCase);
        await bindAndRunInitial(runtime);
        runtime.clearFetchCalls();
        testCase.install(runtime, testCase.equipmentId);
        const context = truncatedContext(62);

        await runtime.window.applyOperationalStateRefresh(context);
        runtime.timers.advance(1000);
        await flushAsync();

        assert.equal(
            achievementGetCalls(runtime).length,
            1,
            `${testCase.role} must keep catch-up pending after accepting the new version.`
        );
        assert.equal(runtime.document.body.dataset.operationalStateVersion, "62");
    }
});


test("relevant event before initial timeout consumes the single initial check", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    runtime.window.bindAchievementPrizeUnlock();

    runtime.window.checkAchievementPrize(
        completedContext({truckId: 17, excavatorId: 31})
    );
    runtime.timers.advance(300);
    await flushAsync();
    assert.equal(runtime.fetchCalls.length, 1);

    runtime.timers.advance(2000);
    await flushAsync();
    assert.equal(
        runtime.fetchCalls.length,
        1,
        "the delayed initial timer must not duplicate an already completed relevant check"
    );
});


test("Driver real fragment handler forwards context and checks only its completed trip", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    installDriverFragmentHandler(runtime, 17);
    const capturedContexts = [];
    const realCheck = runtime.window.checkAchievementPrize;
    runtime.window.checkAchievementPrize = function (context) {
        capturedContexts.push(context);
        return realCheck(context);
    };

    const irrelevant = completedContext({truckId: 999, excavatorId: 31});
    await runtime.window.applyOperationalStateRefresh(irrelevant);
    runtime.timers.advance(1000);
    await flushAsync();
    assert.strictEqual(capturedContexts[0], irrelevant);
    assert.equal(runtime.fetchCalls.length, 0);

    const relevant = completedContext({truckId: 17, excavatorId: 31});
    await runtime.window.applyOperationalStateRefresh(relevant);
    runtime.timers.advance(1000);
    await flushAsync();
    assert.strictEqual(capturedContexts[1], relevant);
    assert.equal(runtime.fetchCalls.length, 1);
    assert.ok(
        DRIVER_SOURCE.includes("data-driver-current-truck-id="),
        "Driver shell must expose the exact current truck id to the achievement runtime."
    );
});


test("Excavator real fragment handler forwards context and checks only its completed trip", async () => {
    const runtime = createRuntime({role: "excavator", equipmentId: 31});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    installExcavatorFragmentHandler(runtime, 31);
    const capturedContexts = [];
    const realCheck = runtime.window.checkAchievementPrize;
    runtime.window.checkAchievementPrize = function (context) {
        capturedContexts.push(context);
        return realCheck(context);
    };

    const irrelevant = completedContext({truckId: 17, excavatorId: 999});
    await runtime.window.applyOperationalStateRefresh(irrelevant);
    runtime.timers.advance(1000);
    await flushAsync();
    assert.strictEqual(capturedContexts[0], irrelevant);
    assert.equal(runtime.fetchCalls.length, 0);

    const relevant = completedContext({truckId: 17, excavatorId: 31});
    await runtime.window.applyOperationalStateRefresh(relevant);
    runtime.timers.advance(1000);
    await flushAsync();
    assert.strictEqual(capturedContexts[1], relevant);
    assert.equal(runtime.fetchCalls.length, 1);
});


test("relevant event burst is debounced and single-flight", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    const deferred = createDeferred();
    runtime.enqueueResponse(deferred.promise);
    const context = completedContext({truckId: 17, excavatorId: 31});

    for (let index = 0; index < 6; index += 1) {
        runtime.window.checkAchievementPrize(context);
    }
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(runtime.fetchCalls.length, 1, "A same-tick burst must collapse to one GET.");

    for (let index = 0; index < 4; index += 1) {
        runtime.window.checkAchievementPrize(context);
    }
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(runtime.fetchCalls.length, 1, "No second GET is allowed while the first is pending.");

    deferred.resolve(jsonResponse({unlocked: false, shown: false}));
    await flushAsync();
});


test("failed relevant check uses bounded retry with backoff and no recurring polling", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    const context = completedContext({truckId: 17, excavatorId: 31});
    for (let index = 0; index < 12; index += 1) {
        runtime.enqueueResponse(
            index % 2 ? jsonResponse({}, false) : networkFailure()
        );
    }

    await triggerAndSettle(runtime, context);
    assert.equal(achievementGetCalls(runtime).length, 1, "The relevant event starts one failed GET.");

    runtime.timers.advance(900);
    await flushAsync();
    assert.equal(
        achievementGetCalls(runtime).length,
        1,
        "The first retry must respect its backoff delay."
    );

    for (let index = 0; index < 8; index += 1) {
        runtime.timers.advance(60 * 1000);
        await flushAsync();
    }
    assert.equal(
        achievementGetCalls(runtime).length,
        4,
        "One failed check may perform only three automatic retries."
    );

    runtime.timers.advance(12 * 60 * 60 * 1000);
    await flushAsync();
    assert.equal(
        achievementGetCalls(runtime).length,
        4,
        "Exhausted bounded recovery must not turn into periodic polling."
    );
});


test("only a false-to-true reconnect starts one new bounded achievement recovery cycle", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    const context = completedContext({truckId: 17, excavatorId: 31});
    for (let index = 0; index < 8; index += 1) {
        runtime.enqueueResponse(networkFailure());
    }

    await triggerAndSettle(runtime, context);
    for (const delay of [1000, 3000, 10000]) {
        runtime.timers.advance(delay);
        await flushAsync();
    }
    assert.equal(
        achievementGetCalls(runtime).length,
        4,
        "The original check and three bounded retries must be exhausted first."
    );

    runtime.window.dispatch("operational-state-connection", {connected: true});
    runtime.window.dispatch("operational-state-connection", {connected: true});
    runtime.window.dispatch("operational-state-connection", {connected: true});
    runtime.timers.advance(60 * 1000);
    await flushAsync();
    assert.equal(
        achievementGetCalls(runtime).length,
        4,
        "Routine connected=true events must not re-arm an exhausted check."
    );

    runtime.window.dispatch("operational-state-connection", {connected: false});
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(
        achievementGetCalls(runtime).length,
        4,
        "The disconnected half of the transition must not issue a GET."
    );

    runtime.window.dispatch("operational-state-connection", {connected: true});
    runtime.window.dispatch("operational-state-connection", {connected: true});
    runtime.window.dispatch("operational-state-connection", {connected: true});
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(
        achievementGetCalls(runtime).length,
        5,
        "One real false-to-true reconnect must start exactly one recovery GET."
    );

    for (const delay of [1000, 3000, 10000]) {
        runtime.timers.advance(delay);
        await flushAsync();
    }
    assert.equal(
        achievementGetCalls(runtime).length,
        8,
        "The reconnect may run only one new request plus three bounded retries."
    );

    runtime.window.dispatch("operational-state-connection", {connected: true});
    runtime.window.dispatch("operational-state-connection", {connected: true});
    runtime.window.dispatch("operational-state-connection", {connected: true});
    runtime.timers.advance(60 * 60 * 1000);
    await flushAsync();
    assert.equal(
        achievementGetCalls(runtime).length,
        8,
        "Routine connected=true events after the second exhaustion must issue no GET."
    );
    assert.deepEqual(runtime.timers.intervalDelays, []);
});


test("visibility and online recovery bursts keep one retry in flight", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    const context = completedContext({truckId: 17, excavatorId: 31});
    runtime.enqueueResponse(jsonResponse({}, false));

    await triggerAndSettle(runtime, context);
    assert.equal(achievementGetCalls(runtime).length, 1);

    const retryResponse = createDeferred();
    runtime.enqueueResponse(retryResponse.promise);
    runtime.window.dispatch("online");
    runtime.window.dispatch("online");
    runtime.document.hidden = true;
    runtime.document.dispatch("visibilitychange");
    runtime.document.hidden = false;
    runtime.document.dispatch("visibilitychange");
    runtime.window.dispatch("online");
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(
        achievementGetCalls(runtime).length,
        2,
        "A recovery burst must start exactly one retry."
    );

    runtime.window.dispatch("online");
    runtime.document.dispatch("visibilitychange");
    runtime.window.dispatch("online");
    runtime.timers.advance(60 * 1000);
    await flushAsync();
    assert.equal(
        achievementGetCalls(runtime).length,
        2,
        "Recovery signals must join the in-flight retry."
    );

    retryResponse.resolve(jsonResponse({unlocked: false, shown: false}));
    await flushAsync();
});


test("a newer pending achievement event supersedes a failed in-flight event", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    const failedResponse = createDeferred();
    runtime.enqueueResponse(failedResponse.promise);
    const olderContext = completedContext({truckId: 17, excavatorId: 31});
    const newerContext = completedContext({truckId: 17, excavatorId: 31});
    newerContext.version = 43;
    newerContext.events[0].payload.trip_id = 9002;

    runtime.window.checkAchievementPrize(olderContext);
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(runtime.fetchCalls.length, 1);

    runtime.window.checkAchievementPrize(newerContext);
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(runtime.fetchCalls.length, 1, "The newer event waits for the in-flight GET.");

    failedResponse.resolve(jsonResponse({}, false));
    await flushAsync();
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(runtime.fetchCalls.length, 2, "The newer event performs the only follow-up GET.");

    runtime.document.hidden = true;
    runtime.document.dispatch("visibilitychange");
    runtime.document.hidden = false;
    runtime.document.dispatch("visibilitychange");
    runtime.timers.advance(1000);
    await flushAsync();
    runtime.window.checkAchievementPrize(newerContext);
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(
        runtime.fetchCalls.length,
        2,
        "A stale failed key must not overwrite or retry after the newer successful event."
    );
});


test("hidden tab performs zero GET and visible recovery performs exactly one catch-up", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    runtime.document.hidden = true;
    const context = completedContext({truckId: 17, excavatorId: 31});

    runtime.window.checkAchievementPrize(context);
    runtime.window.checkAchievementPrize(context);
    runtime.window.checkAchievementPrize(context);
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(runtime.fetchCalls.length, 0, "Hidden tab must not request achievement state.");

    runtime.document.hidden = false;
    runtime.document.dispatch("visibilitychange");
    runtime.timers.advance(1000);
    await flushAsync();
    assert.equal(
        achievementGetCalls(runtime).length,
        1,
        "Visibility catch-up must issue exactly one pending request."
    );
});


test("successful retry shows unlock, close persists shown, and the key stays consumed", async () => {
    const runtime = createRuntime({role: "driver", equipmentId: 17});
    await bindAndRunInitial(runtime);
    runtime.clearFetchCalls();
    const context = completedContext({truckId: 17, excavatorId: 31});
    runtime.enqueueResponse(jsonResponse({}, false));
    runtime.enqueueResponse(jsonResponse({
        unlocked: true,
        shown: false,
        unlock_id: 177,
        title: "План выполнен",
        percent: 105,
        image_url: "/private/retry-prize.png",
        download_url: "/api/achievements/177/download/"
    }));

    await triggerAndSettle(runtime, context);
    assert.equal(achievementGetCalls(runtime).length, 1);
    runtime.timers.advance(2000);
    await flushAsync();

    assert.equal(achievementGetCalls(runtime).length, 2, "The bounded retry must succeed once.");
    const modal = runtime.document.modal;
    assert.ok(modal, "Successful recovery must create the unlock modal.");
    assert.equal(modal.hidden, false, "Recovered unlock must be shown.");
    modal.querySelector("[data-achievement-close]").onclick();
    await flushAsync();
    assert.equal(modal.hidden, true, "Close must hide the recovered unlock.");
    assert.equal(
        runtime.fetchCalls.filter((call) => (
            call.url === "/api/achievements/177/shown/" &&
            call.options.method === "POST"
        )).length,
        1,
        "Close after recovery must persist shown exactly once."
    );

    await triggerAndSettle(runtime, context);
    assert.equal(
        achievementGetCalls(runtime).length,
        2,
        "A successfully recovered key must remain consumed."
    );
});


test("unlocked prize opens modal, close marks shown, and shown prize stays closed", async () => {
    const runtime = createRuntime();
    runtime.enqueueResponse(jsonResponse({
        unlocked: true,
        shown: false,
        unlock_id: 77,
        title: "План выполнен",
        percent: 108,
        image_url: "/private/prize.png",
        download_url: "/api/achievements/77/download/"
    }));
    await bindAndRunInitial(runtime);

    const modal = runtime.document.modal;
    assert.ok(modal, "Unlocked prize must create its modal.");
    assert.equal(modal.hidden, false, "Unlocked and unseen prize must be visible.");
    const closeButton = modal.querySelector("[data-achievement-close]");
    assert.equal(typeof closeButton.onclick, "function");
    closeButton.onclick();
    await flushAsync();
    assert.equal(modal.hidden, true);
    assert.equal(
        runtime.fetchCalls.filter((call) => (
            call.url === "/api/achievements/77/shown/" &&
            call.options.method === "POST"
        )).length,
        1,
        "Closing the prize must persist its shown state exactly once."
    );

    const shownRuntime = createRuntime();
    shownRuntime.enqueueResponse(jsonResponse({
        unlocked: true,
        shown: true,
        unlock_id: 88,
        title: "Уже показано",
        percent: 100,
        image_url: "/private/already-shown.png"
    }));
    await bindAndRunInitial(shownRuntime);
    assert.equal(
        shownRuntime.document.modal,
        null,
        "A prize already marked as shown must not open another modal."
    );
});
