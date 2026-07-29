#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const RATING_TV_PATH = path.resolve(__dirname, "..", "rating-tv-v1.js");
const RATING_TV_SOURCE = fs.readFileSync(RATING_TV_PATH, "utf8");


class FakeClassList {
    constructor(owner) {
        this.owner = owner;
    }

    values() {
        return new Set(
            String(this.owner.className || "")
                .split(/\s+/)
                .filter(Boolean)
        );
    }

    write(values) {
        this.owner.className = Array.from(values).join(" ");
    }

    add(...names) {
        const values = this.values();
        names.filter(Boolean).forEach((name) => values.add(name));
        this.write(values);
    }

    remove(...names) {
        const values = this.values();
        names.forEach((name) => values.delete(name));
        this.write(values);
    }

    contains(name) {
        return this.values().has(name);
    }

    toggle(name, force) {
        const values = this.values();
        const enabled = typeof force === "boolean"
            ? force
            : !values.has(name);
        if (enabled) {
            values.add(name);
        } else {
            values.delete(name);
        }
        this.write(values);
        return enabled;
    }
}


class FakeStyle {
    constructor() {
        this.values = new Map();
    }

    setProperty(name, value) {
        this.values.set(name, String(value));
    }

    getPropertyValue(name) {
        return this.values.get(name) || "";
    }
}


function matchesSelector(element, selector) {
    if (!element || element.isTextNode) return false;
    if (selector.startsWith(".")) {
        return element.classList.contains(selector.slice(1));
    }
    return element.tagName.toLowerCase() === selector.toLowerCase();
}


class FakeElement {
    constructor(tagName = "div") {
        this.tagName = String(tagName).toUpperCase();
        this.children = [];
        this.parentElement = null;
        this.parentNode = null;
        this.className = "";
        this.classList = new FakeClassList(this);
        this.style = new FakeStyle();
        this.dataset = {};
        this.attributes = new Map();
        this.listeners = new Map();
        this.selectorMap = new Map();
        this.textContent = "";
        this.hidden = false;
        this.disabled = false;
        this.selected = false;
        this.value = "";
        this.clientHeight = 0;
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    dispatch(type) {
        const event = {type, target: this, currentTarget: this};
        (this.listeners.get(type) || [])
            .slice()
            .forEach((listener) => listener.call(this, event));
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    getAttribute(name) {
        return this.attributes.has(name)
            ? this.attributes.get(name)
            : null;
    }

    appendChild(child) {
        if (child && child.isFragment) {
            const fragmentChildren = child.children.slice();
            child.children = [];
            fragmentChildren.forEach((item) => this.appendChild(item));
            return child;
        }
        if (child == null) return child;
        child.parentElement = this;
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    append(...children) {
        children.forEach((child) => this.appendChild(child));
    }

    replaceChildren(...children) {
        this.children.forEach((child) => {
            child.parentElement = null;
            child.parentNode = null;
        });
        this.children = [];
        children.forEach((child) => this.appendChild(child));
        if (this.tagName === "SELECT") {
            const selected = this.children.find((child) => child.selected)
                || this.children[0]
                || null;
            this.value = selected ? selected.value : "";
        }
    }

    remove() {
        if (!this.parentElement) return;
        this.parentElement.children = this.parentElement.children.filter(
            (child) => child !== this
        );
        this.parentElement = null;
        this.parentNode = null;
    }

    querySelector(selector) {
        if (this.selectorMap.has(selector)) {
            return this.selectorMap.get(selector);
        }
        return this.querySelectorAll(selector)[0] || null;
    }

    querySelectorAll(selector) {
        const matches = [];
        function visit(node) {
            if (matchesSelector(node, selector)) matches.push(node);
            node.children.forEach(visit);
        }
        this.children.forEach(visit);
        return matches;
    }
}


class FakeTextNode extends FakeElement {
    constructor(text) {
        super("#text");
        this.isTextNode = true;
        this.textContent = String(text);
    }
}


class FakeDocumentFragment extends FakeElement {
    constructor() {
        super("#fragment");
        this.isFragment = true;
    }
}


class FakeDocument {
    constructor(root) {
        this.root = root;
        this.nodesById = new Map();
        this.documentElement = new FakeElement("html");
        this.fullscreenElement = null;
    }

    getElementById(id) {
        return this.nodesById.get(id) || null;
    }

    querySelector(selector) {
        if (selector === "[data-rating-tv]") return this.root;
        return this.root.querySelector(selector);
    }

    createElement(tagName) {
        return new FakeElement(tagName);
    }

    createTextNode(text) {
        return new FakeTextNode(text);
    }

    createDocumentFragment() {
        return new FakeDocumentFragment();
    }
}


class FakeClock {
    constructor() {
        this.now = 0;
        this.nextId = 1;
        this.intervals = new Map();
    }

    setInterval(callback, delay) {
        const normalizedDelay = Math.max(1, Number(delay) || 0);
        const id = this.nextId;
        this.nextId += 1;
        this.intervals.set(id, {
            callback,
            delay: normalizedDelay,
            dueAt: this.now + normalizedDelay
        });
        return id;
    }

    clearInterval(id) {
        this.intervals.delete(id);
    }

    advance(milliseconds) {
        const target = this.now + Number(milliseconds);
        while (true) {
            let nextId = null;
            let nextTask = null;
            this.intervals.forEach((task, id) => {
                if (
                    task.dueAt <= target
                    && (!nextTask || task.dueAt < nextTask.dueAt)
                ) {
                    nextId = id;
                    nextTask = task;
                }
            });
            if (!nextTask) break;

            this.now = nextTask.dueAt;
            nextTask.callback();
            const current = this.intervals.get(nextId);
            if (current === nextTask) {
                current.dueAt += current.delay;
            }
        }
        this.now = target;
    }
}


class FakeAbortController {
    constructor() {
        this.signal = {aborted: false};
    }

    abort() {
        this.signal.aborted = true;
    }
}


function buildScreen() {
    const root = new FakeElement("main");
    const board = new FakeElement("section");
    board.clientHeight = 680;

    const grid = new FakeElement("ol");
    grid.hidden = true;
    grid.clientHeight = 680;
    board.appendChild(grid);

    const message = new FakeElement("div");
    const messageTitle = new FakeElement("strong");
    const messageText = new FakeElement("span");
    messageTitle.textContent = "Получаем серверный снимок";
    messageText.textContent = "На экране появится рейтинг.";
    message.append(messageTitle, messageText);

    const rotationToggle = new FakeElement("button");
    const rotationIcon = new FakeElement("span");
    const rotationLabel = new FakeElement("b");
    rotationIcon.textContent = "Ⅱ";
    rotationLabel.textContent = "Пауза";
    rotationToggle.append(rotationIcon, rotationLabel);

    const programToggle = new FakeElement("button");
    const programIcon = new FakeElement("span");
    const programLabel = new FakeElement("b");
    programIcon.textContent = "＋";
    programLabel.textContent = "В показ";
    programToggle.append(programIcon, programLabel);

    const elements = {
        grid,
        message,
        messageTitle,
        messageText,
        status: new FakeElement("strong"),
        updatedAt: new FakeElement("strong"),
        refreshCountdown: new FakeElement("strong"),
        rotationCountdown: new FakeElement("strong"),
        rotationToggle,
        previous: new FakeElement("button"),
        next: new FakeElement("button"),
        programToggle,
        programCount: new FakeElement("span"),
        fullscreen: new FakeElement("button"),
        period: new FakeElement("select"),
        composition: new FakeElement("select"),
        shiftType: new FakeElement("select"),
        qaDay: new FakeElement("strong"),
        qaDayCount: new FakeElement("span")
    };
    elements.status.textContent = "Загрузка рейтинга";

    const selectors = {
        "[data-rating-grid]": elements.grid,
        "[data-rating-message]": elements.message,
        "[data-rating-status]": elements.status,
        "[data-updated-at]": elements.updatedAt,
        "[data-refresh-countdown]": elements.refreshCountdown,
        "[data-rotation-countdown]": elements.rotationCountdown,
        "[data-rotation-toggle]": elements.rotationToggle,
        "[data-group-previous]": elements.previous,
        "[data-group-next]": elements.next,
        "[data-program-toggle]": elements.programToggle,
        "[data-program-count]": elements.programCount,
        "[data-fullscreen-toggle]": elements.fullscreen,
        "[data-rating-period]": elements.period,
        "[data-watch-composition]": elements.composition,
        "[data-shift-type]": elements.shiftType,
        "[data-qa-day]": elements.qaDay,
        "[data-qa-day-count]": elements.qaDayCount
    };
    Object.entries(selectors).forEach(([selector, element]) => {
        root.selectorMap.set(selector, element);
    });

    return {root, elements};
}


function buildEntries(count, {prefix = "Водитель", reverse = false} = {}) {
    const entries = Array.from({length: count}, (_, index) => {
        const place = index + 1;
        return {
            employee_id: place,
            full_name: `${prefix} ${String(place).padStart(2, "0")}`,
            equipment: [`БелАЗ №${String(place).padStart(2, "0")}`],
            score: (100 - place / 10).toFixed(2),
            place,
            display_order: place,
            position_delta: 0
        };
    });
    return reverse ? entries.reverse() : entries;
}


function buildPayload({
    periodId = 10,
    compositionId = 20,
    compositionIds = [compositionId],
    shiftType = "night",
    fingerprint = `${periodId}-${compositionId}-${shiftType}`,
    entries = buildEntries(3)
} = {}) {
    return {
        available: true,
        official: false,
        status: "Рабочий рейтинг",
        generated_at: "2026-07-30T08:00:00+04:00",
        source_fingerprint: fingerprint,
        rating_period: {
            id: periodId,
            name: "14.07.2026 — 14.08.2026",
            starts_on: "2026-07-14",
            ends_before: "2026-08-14"
        },
        watch_composition: {
            id: compositionId,
            code: `composition-${compositionId}`,
            name: `Состав ${compositionId}`
        },
        shift_type: shiftType,
        shift_type_label: shiftType === "day" ? "Дневная" : "Ночная",
        available_rating_periods: [{
            id: periodId,
            name: "14.07.2026 — 14.08.2026",
            starts_on: "2026-07-14",
            ends_before: "2026-08-14"
        }],
        available_watch_compositions: compositionIds.map((id) => ({
            id,
            code: `composition-${id}`,
            name: `Состав ${id}`
        })),
        entries
    };
}


function queuedResponse(payload, status = 200) {
    return {payload, status};
}


function queuedFailure(error) {
    return {error};
}


function createRuntime({
    qaPreview = false,
    previewPayload = null,
    responses = []
} = {}) {
    const clock = new FakeClock();
    const {root, elements} = buildScreen();
    const document = new FakeDocument(root);
    const fetchCalls = [];
    const responseQueue = responses.slice();
    const windowListeners = new Map();

    const configNode = new FakeElement("script");
    configNode.textContent = JSON.stringify({
        apiUrl: "/reports/rating/driver-period-ranking/",
        photoUrlTemplate: "/reports/rating/employee/__employee_id__/photo/",
        refreshSeconds: 300,
        rotationSeconds: 15,
        qaPreview,
        initialShiftType: "night"
    });
    document.nodesById.set("rating-tv-config", configNode);
    if (previewPayload) {
        const previewNode = new FakeElement("script");
        previewNode.textContent = JSON.stringify(previewPayload);
        document.nodesById.set("rating-tv-preview-payload", previewNode);
    }

    class RuntimeDate extends Date {
        constructor(...args) {
            super(...(args.length ? args : [clock.now]));
        }

        static now() {
            return clock.now;
        }
    }

    function fetchStub(url, options) {
        fetchCalls.push({url, options});
        const next = responseQueue.shift();
        if (!next) {
            return Promise.reject(new Error(`Unexpected fetch: ${url}`));
        }
        if (next.promise) return next.promise;
        if (next.error) return Promise.reject(next.error);
        const status = Number(next.status || 200);
        return Promise.resolve({
            ok: status >= 200 && status < 300,
            status,
            json() {
                return Promise.resolve(next.payload);
            }
        });
    }

    const windowObject = {
        document,
        location: {origin: "https://driverform.test"},
        innerHeight: 1080,
        fetch: fetchStub,
        setInterval: clock.setInterval.bind(clock),
        clearInterval: clock.clearInterval.bind(clock),
        addEventListener(type, listener) {
            const listeners = windowListeners.get(type) || [];
            listeners.push(listener);
            windowListeners.set(type, listeners);
        },
        dispatch(type) {
            (windowListeners.get(type) || [])
                .slice()
                .forEach((listener) => listener({type}));
        }
    };
    windowObject.window = windowObject;

    const context = vm.createContext({
        window: windowObject,
        document,
        AbortController: FakeAbortController,
        URL,
        Intl,
        Date: RuntimeDate,
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
        TypeError,
        encodeURIComponent,
        console
    });
    vm.runInContext(RATING_TV_SOURCE, context, {
        filename: RATING_TV_PATH
    });

    return {
        clock,
        context,
        document,
        elements,
        fetchCalls,
        window: windowObject,
        enqueueResponse(payload, status = 200) {
            responseQueue.push(queuedResponse(payload, status));
        },
        enqueuePending(promise) {
            responseQueue.push({promise});
        },
        enqueueFailure(error) {
            responseQueue.push(queuedFailure(error));
        },
        async flush() {
            for (let index = 0; index < 8; index += 1) {
                await Promise.resolve();
            }
        }
    };
}


test("QA preview renders all 53 sorted rows and premium five without fetch", async () => {
    const previewPayload = buildPayload({
        entries: buildEntries(53, {prefix: "Тестовый водитель", reverse: true}),
        fingerprint: "qa-preview"
    });
    const runtime = createRuntime({
        qaPreview: true,
        previewPayload
    });
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 0);
    assert.equal(runtime.elements.grid.hidden, false);
    assert.equal(runtime.elements.message.hidden, true);
    assert.equal(runtime.elements.grid.children.length, 53);
    assert.equal(runtime.elements.grid.children[0].dataset.displayOrder, "1");
    assert.equal(runtime.elements.grid.children[52].dataset.displayOrder, "53");

    runtime.elements.grid.children.slice(0, 5).forEach((row, index) => {
        assert.equal(row.classList.contains("is-premium"), true);
        assert.equal(row.classList.contains(`is-place-${index + 1}`), true);
    });
    assert.equal(
        runtime.elements.grid.children[5].classList.contains("is-premium"),
        false
    );
});


test("live initial fetch uses private no-store GET and the night query", async () => {
    const runtime = createRuntime({
        responses: [queuedResponse(buildPayload())]
    });
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 1);
    const call = runtime.fetchCalls[0];
    const url = new URL(call.url);
    assert.equal(url.origin, "https://driverform.test");
    assert.equal(url.pathname, "/reports/rating/driver-period-ranking/");
    assert.equal(url.searchParams.get("shift_type"), "night");
    assert.equal(url.searchParams.has("rating_period"), false);
    assert.equal(url.searchParams.has("watch_composition"), false);
    assert.equal(call.options.method, "GET");
    assert.equal(call.options.credentials, "same-origin");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.headers.Accept, "application/json");
    assert.deepEqual(Object.keys(call.options.headers), ["Accept"]);
    assert.equal(call.options.signal.aborted, false);
    assert.equal(runtime.elements.grid.children.length, 3);
});


test("future period stays a manual option and is not added to background refresh", async () => {
    const futurePeriod = {
        id: 99,
        name: "14.08.2026 — 14.09.2026",
        starts_on: "2026-08-14",
        ends_before: "2026-09-14"
    };
    const unavailablePayload = {
        available: false,
        official: false,
        status: "Текущий период рейтинга ещё не открыт.",
        generated_at: "2026-07-30T08:00:00+04:00",
        source_fingerprint: "no-current-period",
        rating_period: null,
        watch_composition: null,
        shift_type: "night",
        shift_type_label: "Ночная",
        available_rating_periods: [futurePeriod],
        available_watch_compositions: [],
        entries: []
    };
    const runtime = createRuntime({
        responses: [queuedResponse(unavailablePayload)]
    });
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(
        new URL(runtime.fetchCalls[0].url).searchParams.has("rating_period"),
        false
    );
    assert.equal(runtime.window.RatingTvScreen.state.selectedPeriod, "");
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "");
    assert.equal(runtime.elements.period.disabled, false);
    assert.equal(runtime.elements.period.value, "");
    assert.equal(runtime.elements.period.children.length, 2);
    assert.equal(runtime.elements.period.children[0].value, "");
    assert.equal(
        runtime.elements.period.children[0].textContent,
        "Период не выбран"
    );
    assert.equal(runtime.elements.period.children[1].value, "99");

    runtime.enqueueResponse(unavailablePayload);
    runtime.clock.advance(300000);
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 2);
    const refreshUrl = new URL(runtime.fetchCalls[1].url);
    assert.equal(refreshUrl.searchParams.has("rating_period"), false);
    assert.equal(refreshUrl.searchParams.has("watch_composition"), false);
    assert.equal(runtime.window.RatingTvScreen.state.selectedPeriod, "");
    assert.equal(runtime.elements.period.disabled, false);
    assert.equal(runtime.elements.period.value, "");
    assert.equal(runtime.elements.period.children[1].value, "99");
});


test("15-second rotation follows only the dispatcher-selected program", async () => {
    const compositionIds = [20, 21, 22];
    const firstGroup = buildPayload({
        compositionId: 20,
        compositionIds,
        shiftType: "night",
        fingerprint: "20-night",
        entries: buildEntries(2, {prefix: "Состав 20 ночь"})
    });
    const availableButNotAddedGroup = buildPayload({
        compositionId: 21,
        compositionIds,
        shiftType: "night",
        fingerprint: "21-night",
        entries: buildEntries(2, {prefix: "Состав 21 ночь"})
    });
    const secondGroup = buildPayload({
        compositionId: 21,
        compositionIds,
        shiftType: "day",
        fingerprint: "21-day",
        entries: buildEntries(2, {prefix: "Состав 21 день"})
    });
    const runtime = createRuntime({
        responses: [queuedResponse(firstGroup)]
    });
    await runtime.flush();

    assert.equal(
        runtime.window.RatingTvScreen.state.presentationPlaylist.length,
        1
    );
    assert.equal(runtime.elements.programCount.textContent, "1 группа");
    assert.equal(runtime.elements.rotationToggle.disabled, true);
    assert.equal(runtime.elements.previous.disabled, true);
    assert.equal(runtime.elements.next.disabled, true);

    runtime.enqueueResponse(availableButNotAddedGroup);
    runtime.elements.composition.value = "21";
    runtime.elements.composition.dispatch("change");
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 2);
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "21");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "night");
    assert.equal(
        runtime.window.RatingTvScreen.state.presentationPlaylist.length,
        1
    );

    runtime.enqueueResponse(secondGroup);
    runtime.elements.shiftType.value = "day";
    runtime.elements.shiftType.dispatch("change");
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 3);
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "21");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "day");

    runtime.elements.programToggle.dispatch("click");
    assert.deepEqual(
        Array.from(
            runtime.window.RatingTvScreen.state.presentationPlaylist,
            (item) => [
                String(item.compositionId),
                item.shiftType
            ]
        ),
        [
            ["20", "night"],
            ["21", "day"]
        ]
    );
    assert.equal(runtime.elements.programCount.textContent, "2 группы");
    assert.equal(runtime.elements.rotationToggle.disabled, false);
    assert.equal(runtime.elements.previous.disabled, false);
    assert.equal(runtime.elements.next.disabled, false);

    runtime.clock.advance(15000);
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 3);
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "20");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "night");
    assert.match(
        runtime.elements.grid.children[0]
            .querySelector(".rating-tv__name")
            .textContent,
        /Состав 20 ночь/
    );

    runtime.clock.advance(15000);
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 3);
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "21");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "day");
    assert.match(
        runtime.elements.grid.children[0]
            .querySelector(".rating-tv__name")
            .textContent,
        /Состав 21 день/
    );

    runtime.elements.rotationToggle.dispatch("click");
    assert.equal(runtime.window.RatingTvScreen.state.rotationPlaying, false);
    assert.equal(runtime.elements.rotationCountdown.textContent, "Пауза");

    runtime.clock.advance(30000);
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 3);
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "21");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "day");

    runtime.elements.next.dispatch("click");
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 3);
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "20");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "night");
    assert.equal(runtime.window.RatingTvScreen.state.rotationPlaying, false);
    assert.equal(
        runtime.window.RatingTvScreen.state.presentationPlaylist.some(
            (item) => (
                String(item.compositionId) === "21"
                && item.shiftType === "night"
            )
        ),
        false
    );
    assert.equal(
        runtime.window.RatingTvScreen.state.presentationPlaylist.some(
            (item) => String(item.compositionId) === "22"
        ),
        false
    );

    runtime.elements.programToggle.dispatch("click");
    await runtime.flush();
    assert.deepEqual(
        Array.from(
            runtime.window.RatingTvScreen.state.presentationPlaylist,
            (item) => [
                String(item.compositionId),
                item.shiftType
            ]
        ),
        [["21", "day"]]
    );
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "21");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "day");
    assert.equal(runtime.fetchCalls.length, 3);
    assert.match(
        runtime.elements.grid.children[0]
            .querySelector(".rating-tv__name")
            .textContent,
        /Состав 21 день/
    );
    assert.equal(runtime.elements.programCount.textContent, "1 группа");
    assert.equal(runtime.elements.rotationToggle.disabled, true);
    assert.equal(runtime.elements.previous.disabled, true);
    assert.equal(runtime.elements.next.disabled, true);
});


test("cached group transition aborts a pending request and ignores its late response", async () => {
    const compositionIds = [20, 21];
    const firstGroup = buildPayload({
        compositionId: 20,
        compositionIds,
        shiftType: "night",
        fingerprint: "20-night",
        entries: buildEntries(2, {prefix: "GROUP A"})
    });
    const secondGroup = buildPayload({
        compositionId: 20,
        compositionIds,
        shiftType: "day",
        fingerprint: "20-day",
        entries: buildEntries(2, {prefix: "GROUP B"})
    });
    const runtime = createRuntime({
        responses: [queuedResponse(firstGroup)]
    });
    await runtime.flush();

    runtime.enqueueResponse(secondGroup);
    runtime.elements.shiftType.value = "day";
    runtime.elements.shiftType.dispatch("change");
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 2);
    assert.equal(
        runtime.window.RatingTvScreen.state.presentationPlaylist.length,
        1
    );

    runtime.elements.programToggle.dispatch("click");
    assert.deepEqual(
        Array.from(
            runtime.window.RatingTvScreen.state.presentationPlaylist,
            (item) => [
                String(item.compositionId),
                item.shiftType
            ]
        ),
        [
            ["20", "night"],
            ["20", "day"]
        ]
    );

    runtime.window.RatingTvScreen.moveGroup(1);
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 2);
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "20");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "night");

    let resolvePendingResponse;
    const pendingResponse = new Promise((resolve) => {
        resolvePendingResponse = (payload, status = 200) => {
            resolve({
                ok: status >= 200 && status < 300,
                status,
                json() {
                    return Promise.resolve(payload);
                }
            });
        };
    });
    runtime.enqueuePending(pendingResponse);
    const pendingFirstGroup = runtime.window.RatingTvScreen.loadRating({
        forceRefresh: true,
        replaceRequest: true
    });
    assert.equal(runtime.fetchCalls.length, 3);
    const pendingSignal = runtime.fetchCalls[2].options.signal;
    assert.equal(pendingSignal.aborted, false);

    runtime.window.RatingTvScreen.moveGroup(1);
    await runtime.flush();
    assert.equal(pendingSignal.aborted, true);
    assert.equal(runtime.fetchCalls.length, 3);
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "20");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "day");
    assert.equal(runtime.window.RatingTvScreen.state.sourceFingerprint, "20-day");
    assert.match(
        runtime.elements.grid.children[0]
            .querySelector(".rating-tv__name")
            .textContent,
        /GROUP B/
    );

    resolvePendingResponse(buildPayload({
        compositionId: 20,
        compositionIds,
        shiftType: "night",
        fingerprint: "20-night-late",
        entries: buildEntries(2, {prefix: "LATE GROUP A"})
    }));
    await pendingFirstGroup;
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 3);
    assert.equal(runtime.window.RatingTvScreen.state.selectedComposition, "20");
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "day");
    assert.equal(runtime.window.RatingTvScreen.state.sourceFingerprint, "20-day");
    const visibleName = runtime.elements.grid.children[0]
        .querySelector(".rating-tv__name")
        .textContent;
    assert.match(visibleName, /GROUP B/);
    assert.doesNotMatch(visibleName, /LATE GROUP A/);
});


test("the active group is refreshed once after 300 seconds", async () => {
    const initial = buildPayload({
        fingerprint: "refresh-1",
        entries: buildEntries(2, {prefix: "До обновления"})
    });
    const refreshed = buildPayload({
        fingerprint: "refresh-2",
        entries: buildEntries(2, {prefix: "После обновления"})
    });
    const runtime = createRuntime({
        responses: [queuedResponse(initial)]
    });
    await runtime.flush();
    const firstOuterSlot = runtime.elements.grid.children[0];
    const firstPlaceNode = firstOuterSlot._ratingPlaceNode;

    runtime.clock.advance(299000);
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 1);

    runtime.enqueueResponse(refreshed);
    runtime.clock.advance(1000);
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 2);
    assert.match(
        runtime.elements.grid.children[0]
            .querySelector(".rating-tv__name")
            .textContent,
        /После обновления/
    );
    assert.equal(runtime.elements.grid.children[0], firstOuterSlot);
    assert.equal(
        runtime.elements.grid.children[0]._ratingPlaceNode,
        firstPlaceNode
    );
});


test("initial 5xx replaces loading state with a terminal server message", async () => {
    const runtime = createRuntime({
        responses: [
            queuedResponse({error: "Сервис рейтинга временно недоступен."}, 503)
        ]
    });
    await runtime.flush();

    assert.equal(runtime.elements.message.hidden, false);
    assert.equal(runtime.elements.grid.hidden, true);
    assert.equal(
        runtime.elements.messageTitle.textContent,
        "Не удалось получить рейтинг"
    );
    assert.equal(
        runtime.elements.messageText.textContent,
        "Сервис рейтинга временно недоступен."
    );
    assert.equal(runtime.window.RatingTvScreen.state.requestInFlight, false);
});


test("network failure preserves the last successful rating snapshot", async () => {
    const initial = buildPayload({
        fingerprint: "last-good",
        entries: buildEntries(4, {prefix: "Последний снимок"})
    });
    const runtime = createRuntime({
        responses: [queuedResponse(initial)]
    });
    await runtime.flush();
    const firstName = runtime.elements.grid.children[0]
        .querySelector(".rating-tv__name")
        .textContent;

    runtime.enqueueFailure(new Error("network unavailable"));
    await runtime.window.RatingTvScreen.loadRating({
        forceRefresh: true,
        replaceRequest: true
    });
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 2);
    assert.equal(runtime.elements.grid.hidden, false);
    assert.equal(runtime.elements.message.hidden, true);
    assert.equal(runtime.elements.grid.children.length, 4);
    assert.equal(
        runtime.elements.grid.children[0]
            .querySelector(".rating-tv__name")
            .textContent,
        firstName
    );
    assert.equal(
        runtime.elements.status.textContent,
        "Показан последний снимок"
    );
});
