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
        this.hidden = false;
        this.listeners = new Map();
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

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    dispatch(type, overrides = {}) {
        let prevented = false;
        const event = {
            type,
            target: this,
            currentTarget: this,
            ctrlKey: false,
            altKey: false,
            metaKey: false,
            preventDefault() {
                prevented = true;
            },
            ...overrides
        };
        (this.listeners.get(type) || [])
            .slice()
            .forEach((listener) => listener.call(this, event));
        return {event, prevented};
    }
}


class FakeClock {
    constructor() {
        this.now = 0;
        this.nextId = 1;
        this.intervals = new Map();
        this.timeouts = new Map();
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

    setTimeout(callback, delay) {
        const normalizedDelay = Math.max(1, Number(delay) || 0);
        const id = this.nextId;
        this.nextId += 1;
        this.timeouts.set(id, {
            callback,
            dueAt: this.now + normalizedDelay
        });
        return id;
    }

    clearTimeout(id) {
        this.timeouts.delete(id);
    }

    advance(milliseconds) {
        const target = this.now + Number(milliseconds);
        while (true) {
            let nextId = null;
            let nextTask = null;
            let nextKind = "";
            this.intervals.forEach((task, id) => {
                if (
                    task.dueAt <= target
                    && (!nextTask || task.dueAt < nextTask.dueAt)
                ) {
                    nextId = id;
                    nextTask = task;
                    nextKind = "interval";
                }
            });
            this.timeouts.forEach((task, id) => {
                if (
                    task.dueAt <= target
                    && (!nextTask || task.dueAt < nextTask.dueAt)
                ) {
                    nextId = id;
                    nextTask = task;
                    nextKind = "timeout";
                }
            });
            if (!nextTask) break;

            this.now = nextTask.dueAt;
            if (nextKind === "timeout") {
                this.timeouts.delete(nextId);
            }
            nextTask.callback();
            if (nextKind === "interval") {
                const current = this.intervals.get(nextId);
                if (current === nextTask) {
                    current.dueAt += current.delay;
                }
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

    function replayButton(iconText, labelText) {
        const button = new FakeElement("button");
        const icon = new FakeElement("span");
        const label = new FakeElement("b");
        icon.textContent = iconText;
        label.textContent = labelText;
        button.append(icon, label);
        button.disabled = true;
        return button;
    }

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
        qaDayCount: new FakeElement("span"),
        qaDaySelect: new FakeElement("select"),
        qaReplayStatus: new FakeElement("span"),
        qaBackward: replayButton("◀◀", "Назад"),
        qaPause: replayButton("▶", "Запуск"),
        qaStep: replayButton("▶", "Шаг"),
        qaForward: replayButton("▶▶", "Вперёд"),
        qaSpeed: new FakeElement("select"),
        qaLiveStep: new FakeElement("strong"),
        qaLiveVirtualAt: new FakeElement("strong"),
        qaLiveShift: new FakeElement("strong"),
        qaLiveRevision: new FakeElement("strong"),
        qaLiveSourceFingerprint: new FakeElement("b"),
        qaLiveScoreFingerprint: new FakeElement("b"),
        qaLiveStateStatus: new FakeElement("p")
    };
    elements.status.textContent = "Загрузка рейтинга";
    elements.qaDaySelect.disabled = true;
    elements.qaSpeed.disabled = true;

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
        "[data-qa-day-count]": elements.qaDayCount,
        "[data-qa-day-select]": elements.qaDaySelect,
        "[data-qa-replay-status]": elements.qaReplayStatus,
        "[data-qa-backward]": elements.qaBackward,
        "[data-qa-pause]": elements.qaPause,
        "[data-qa-step]": elements.qaStep,
        "[data-qa-forward]": elements.qaForward,
        "[data-qa-speed]": elements.qaSpeed,
        "[data-qa-live-step]": elements.qaLiveStep,
        "[data-qa-live-virtual-at]": elements.qaLiveVirtualAt,
        "[data-qa-live-shift]": elements.qaLiveShift,
        "[data-qa-live-revision]": elements.qaLiveRevision,
        "[data-qa-live-source-fingerprint]": (
            elements.qaLiveSourceFingerprint
        ),
        "[data-qa-live-score-fingerprint]": (
            elements.qaLiveScoreFingerprint
        ),
        "[data-qa-live-state-status]": elements.qaLiveStateStatus
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

function buildQaLiveState({
    step = 1,
    periodId = 10,
    compositionId = 20,
    shiftType = "night",
    placeholders = []
} = {}) {
    return {
        schema: "driver-rating-qa-live-state",
        schema_version: 1,
        synthetic: true,
        official: false,
        official_rating_eligible: false,
        run_id: "qa-live-runtime-run",
        site_code: "section_2",
        rating_period_id: periodId,
        watch_composition_id: compositionId,
        step,
        virtual_at: (
            "2026-07-30T"
            + `${String(step % 24).padStart(2, "0")}:00:00+04:00`
        ),
        shift_type: shiftType,
        placeholders
    };
}

function buildReplayDocument() {
    const ratingLevels = {
        1: "Алмазный уровень",
        2: "Платиновый уровень",
        3: "Золотой уровень",
        4: "Серебряный уровень",
        5: "Медный уровень"
    };
    const ratingPeriod = {
        id: -1001,
        name: "Тестовый период 30 дней",
        starts_on: "2026-05-01",
        ends_before: "2026-05-31",
        is_active: true
    };
    const watchComposition = {
        id: -2001,
        code: "qa-runtime-replay",
        name: "Тестовый состав runtime",
        is_active: true
    };
    const replayId = "QA-RUNTIME-30D";
    const snapshots = [];
    let previousPlaces = null;

    for (let day = 1; day <= 30; day += 1) {
        const ordinals = Array.from({length: 53}, (_, index) => index + 1);
        ordinals.sort((left, right) => {
            const leftRank = (left + day * 7) % 53;
            const rightRank = (right + day * 7) % 53;
            return leftRank - rightRank || left - right;
        });
        const entries = ordinals.map((ordinal, index) => {
            const place = index + 1;
            const employeeId = -ordinal;
            return {
                employee_id: employeeId,
                full_name: `ТЕСТ_Водитель ${String(ordinal).padStart(2, "0")}`,
                equipment: [`БелАЗ №${String(ordinal).padStart(2, "0")}`],
                shift_count: day,
                score: (100 - place / 2).toFixed(2),
                place,
                shared_score_place: place,
                display_order: place,
                level: ratingLevels[place] || "",
                position_delta: previousPlaces === null
                    ? 0
                    : previousPlaces.get(employeeId) - place
            };
        });
        previousPlaces = new Map(
            entries.map((entry) => [entry.employee_id, entry.place])
        );
        const generatedAt = (
            `2026-05-${String(day).padStart(2, "0")}T22:00:00+10:00`
        );
        snapshots.push({
            day,
            work_date: `2026-05-${String(day).padStart(2, "0")}`,
            as_of: generatedAt,
            previous_payload_sha256: day === 1 ? null : `${day - 1}`,
            payload_sha256: `${day}`,
            payload: {
                available: true,
                official: false,
                official_rating_eligible: false,
                synthetic: true,
                formula_evaluated: false,
                rating_mode: "qa_saved_replay",
                scope_type: "rating_period",
                formula_version: "TV_VISUAL_REPLAY_NOT_KPI",
                formula_label: "Визуальный replay — не KPI",
                status: `Сохранённый день ${day}`,
                generated_at: generatedAt,
                source_fingerprint: `day-${day}`,
                rating_period: ratingPeriod,
                watch_composition: watchComposition,
                shift_type: "night",
                shift_type_label: "Ночная",
                available_rating_periods: [ratingPeriod],
                available_watch_compositions: [watchComposition],
                summary: {
                    employee_count: 53,
                    rated_shift_count: 53 * day,
                    withheld_shift_count: 0,
                    withheld_reasons: {}
                },
                entries,
                qa_day: day,
                qa_day_count: 30,
                qa_work_date: `2026-05-${String(day).padStart(2, "0")}`,
                replay_run_id: replayId
            }
        });
    }
    return {
        schema: "copper.driver-rating-replay",
        schema_version: 1,
        data_classification: "synthetic_qa_only",
        synthetic: true,
        official: false,
        official_rating_eligible: false,
        warning: "Синтетический QA replay",
        replay: {
            id: replayId,
            label: "Runtime replay",
            scenario_version: "RUNTIME_V1",
            rating_mode: "qa_saved_replay",
            synthetic: true,
            formula_evaluated: false,
            official: false,
            day_count: 30,
            expected_employee_count: 53,
            initial_day: 1,
            base_step_ms: 3000,
            formula_version: "TV_VISUAL_REPLAY_NOT_KPI",
            formula_label: "Визуальный replay — не KPI"
        },
        scope: {
            scope_type: "rating_period",
            profession: "driver",
            rating_period: ratingPeriod,
            watch_composition: watchComposition,
            shift_type: "night",
            shift_type_label: "Ночная"
        },
        snapshots,
        integrity: {
            algorithm: "sha256",
            canonicalization: "json-sort-keys-utf8-v1",
            snapshot_chain_sha256: "runtime",
            canonical_sha256: "runtime"
        }
    };
}

function buildFormulaReplayDocument(shiftType = "night") {
    const shiftLabel = shiftType === "day" ? "Дневная" : "Ночная";
    const ratingLevels = {
        1: "Алмазный уровень",
        2: "Платиновый уровень",
        3: "Золотой уровень",
        4: "Серебряный уровень",
        5: "Медный уровень"
    };
    const ratingPeriod = {
        id: -3101,
        name: "Синтетический формульный период",
        starts_on: "2026-05-01",
        ends_before: "2026-05-31",
        is_active: true
    };
    const watchComposition = {
        id: shiftType === "day" ? -3202 : -3201,
        code: `qa-formula-${shiftType}`,
        name: `Синтетический ${shiftLabel.toLowerCase()} состав`,
        is_active: true
    };
    const replayId = `QA-FORMULA-RUNTIME-${shiftType.toUpperCase()}`;
    const cohort = Array.from({length: 53}, (_, index) => ({
        employee_id: -(index + 1),
        full_name: `ТЕСТ_Водитель ${String(index + 1).padStart(2, "0")}`
    }));
    const scoreFor = (ordinal) => {
        if (ordinal === 1) return "99.0001";
        if (ordinal === 2) return "99.0000";
        if (ordinal === 3 || ordinal === 4) return "98.0000";
        return (98 - (ordinal - 4) / 10).toFixed(4);
    };
    const snapshots = [];
    let previousPlaces = new Map();
    let previousStatuses = new Map();

    for (let day = 1; day <= 30; day += 1) {
        const entries = [];
        let previousScore = null;
        let densePlace = 0;
        for (let ordinal = 1; ordinal <= 51; ordinal += 1) {
            const score = scoreFor(ordinal);
            if (previousScore === null || score !== previousScore) {
                densePlace += 1;
            }
            const employeeId = -ordinal;
            const place = densePlace;
            entries.push({
                employee_id: employeeId,
                full_name: cohort[ordinal - 1].full_name,
                equipment: [`БелАЗ №${String(ordinal).padStart(2, "0")}`],
                row_status: "rated",
                ranking_eligible: true,
                shift_count: day,
                withheld_shift_count: 0,
                withheld_reasons: {},
                quality_flags: [],
                quality_flags_status: "captured",
                trip_count: 17 * day,
                volume_m3: (382.5 * day).toFixed(2),
                tonnage_t: (939.25 * day).toFixed(2),
                score,
                blocks: {
                    production: "80.0000",
                    work_time: "81.0000",
                    stability: "82.0000",
                    assignments: "83.0000",
                    digital_accounting: "84.0000"
                },
                confidence: "90.0000",
                source_shift_ids: Array.from(
                    {length: day},
                    (_, shiftIndex) => -(ordinal * 1000 + shiftIndex + 1)
                ),
                place,
                shared_score_place: place,
                display_order: ordinal,
                level: ratingLevels[place] || "",
                position_delta: previousStatuses.get(employeeId) === "rated"
                    ? previousPlaces.get(employeeId) - place
                    : null
            });
            previousScore = score;
        }
        entries.push({
            employee_id: -52,
            full_name: cohort[51].full_name,
            equipment: ["БелАЗ №52"],
            row_status: "withheld",
            ranking_eligible: false,
            shift_count: day,
            withheld_shift_count: day,
            withheld_reasons: {"blocking_quality:data_conflict": day},
            quality_flags: ["data_conflict"],
            quality_flags_status: "captured",
            trip_count: null,
            volume_m3: null,
            tonnage_t: null,
            score: null,
            blocks: null,
            confidence: null,
            source_shift_ids: Array.from(
                {length: day},
                (_, shiftIndex) => -(52000 + shiftIndex + 1)
            ),
            place: null,
            shared_score_place: null,
            display_order: 52,
            level: "",
            position_delta: null
        });
        entries.push({
            employee_id: -53,
            full_name: cohort[52].full_name,
            equipment: [],
            row_status: "not_observed",
            ranking_eligible: false,
            shift_count: 0,
            withheld_shift_count: 0,
            withheld_reasons: {},
            quality_flags: [],
            quality_flags_status: "not_applicable",
            trip_count: null,
            volume_m3: null,
            tonnage_t: null,
            score: null,
            blocks: null,
            confidence: null,
            source_shift_ids: [],
            place: null,
            shared_score_place: null,
            display_order: 53,
            level: "",
            position_delta: null
        });

        previousPlaces = new Map(
            entries
                .filter((entry) => entry.row_status === "rated")
                .map((entry) => [entry.employee_id, entry.place])
        );
        previousStatuses = new Map(
            entries.map((entry) => [entry.employee_id, entry.row_status])
        );
        const workDate = `2026-05-${String(day).padStart(2, "0")}`;
        const generatedAt = `${workDate}T22:00:00+10:00`;
        snapshots.push({
            day,
            work_date: workDate,
            as_of: generatedAt,
            previous_payload_sha256: day === 1 ? null : `${day - 1}`,
            payload_sha256: `${day}`,
            payload: {
                available: true,
                calculation_available: true,
                official: false,
                official_rating_eligible: false,
                synthetic: true,
                formula_evaluated: true,
                rating_mode: "qa_formula_replay",
                scope_type: "rating_period",
                formula_version: "DRIVER_WATCH_V3_NO_DISTANCE_TIME_POLICY_NEUTRAL",
                formula_label: "Рабочая формула",
                status: `Синтетический формульный день ${day}`,
                generated_at: generatedAt,
                rating_period: ratingPeriod,
                watch_composition: watchComposition,
                shift_type: shiftType,
                shift_type_label: shiftLabel,
                available_rating_periods: [ratingPeriod],
                available_watch_compositions: [watchComposition],
                entries,
                qa_day: day,
                qa_day_count: 30,
                qa_work_date: workDate,
                replay_run_id: replayId
            }
        });
    }

    return {
        schema: "copper.driver-rating-formula-replay",
        schema_version: 1,
        data_classification: "isolated_synthetic_formula_qa_only",
        synthetic: true,
        formula_evaluated: true,
        official: false,
        official_rating_eligible: false,
        warning: "Синтетический формульный QA-прогон",
        replay: {
            id: replayId,
            label: "Runtime formula replay",
            scenario_version: "RUNTIME_FORMULA_V1",
            rating_mode: "qa_formula_replay",
            synthetic: true,
            formula_evaluated: true,
            official: false,
            day_count: 30,
            expected_employee_count: 53,
            initial_day: 1,
            base_step_ms: 3000
        },
        scope: {
            scope_type: "rating_period",
            profession: "driver",
            profession_label: "Водитель самосвала",
            rating_period: ratingPeriod,
            watch_composition: watchComposition,
            shift_type: shiftType,
            shift_type_label: shiftLabel,
            cohort
        },
        snapshots,
        integrity: {
            algorithm: "SHA-256",
            canonicalization: "json-sort-keys-utf8-v1",
            snapshot_chain_sha256: "runtime",
            canonical_sha256: "runtime"
        }
    };
}


function queuedResponse(payload, status = 200) {
    return {payload, status};
}


function queuedFailure(error) {
    return {error};
}

function deferredResponse() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return {
        promise,
        resolve(payload, status = 200) {
            resolve({
                ok: status >= 200 && status < 300,
                status,
                json() {
                    return Promise.resolve(payload);
                }
            });
        },
        reject
    };
}


function createRuntime({
    qaPreview = false,
    qaLive = false,
    qaReplayEnabled = false,
    qaReplayKind = "visual",
    qaFormulaEnabledShiftTypes = (
        qaReplayKind === "formula" ? ["day", "night"] : []
    ),
    initialShiftType = "night",
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
        apiUrl: qaLive
            ? "/reports/rating/tv/qa-live/data/"
            : "/reports/rating/driver-period-ranking/",
        photoUrlTemplate: "/reports/rating/employee/__employee_id__/photo/",
        refreshSeconds: qaLive ? 10 : 300,
        rotationSeconds: 15,
        qaPreview,
        qaLive,
        qaLiveRunId: qaLive ? "qa-live-runtime-run" : "",
        qaLiveSiteCode: qaLive ? "section_2" : "",
        qaLiveStateUrl: qaLive
            ? "/reports/rating/tv/qa-live/state/"
            : "",
        qaReplayEnabled,
        qaReplayKind,
        qaReplayUrl: qaReplayKind === "formula"
            ? "/reports/rating/tv/qa-formula-replay-data/"
            : "/reports/rating/tv/qa-replay-data/",
        qaReplaySchema: qaReplayKind === "formula"
            ? "copper.driver-rating-formula-replay"
            : "copper.driver-rating-replay",
        qaFormulaEnabledShiftTypes,
        initialShiftType
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
        innerWidth: 1920,
        innerHeight: 1080,
        fetch: fetchStub,
        setInterval: clock.setInterval.bind(clock),
        clearInterval: clock.clearInterval.bind(clock),
        setTimeout: clock.setTimeout.bind(clock),
        clearTimeout: clock.clearTimeout.bind(clock),
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
            for (let index = 0; index < 24; index += 1) {
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

test("saved replay fetches once and boots paused without live timers", async () => {
    const replay = buildReplayDocument();
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        responses: [queuedResponse(replay)]
    });
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 1);
    const call = runtime.fetchCalls[0];
    const url = new URL(call.url);
    assert.equal(url.pathname, "/reports/rating/tv/qa-replay-data/");
    assert.equal(url.search, "");
    assert.equal(call.options.method, "GET");
    assert.equal(call.options.credentials, "same-origin");
    assert.equal(call.options.cache, "no-store");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 1);
    assert.equal(runtime.elements.grid.children.length, 53);
    assert.equal(runtime.elements.qaDaySelect.children.length, 30);
    assert.equal(runtime.elements.qaDaySelect.value, "1");
    assert.equal(runtime.elements.qaDaySelect.disabled, false);
    assert.equal(runtime.elements.qaBackward.disabled, true);
    assert.equal(runtime.elements.qaForward.disabled, false);
    assert.equal(runtime.elements.refreshCountdown.textContent, "Сохранённый снимок");

    runtime.clock.advance(300000);
    await runtime.flush();
    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 1);
    assert.equal(runtime.window.RatingTvScreen.state.presentationPlaylist.length, 0);
});


test("saved replay plays forward to day 30 and stops without wrapping", async () => {
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        responses: [queuedResponse(buildReplayDocument())]
    });
    await runtime.flush();

    runtime.elements.qaSpeed.value = "4";
    runtime.elements.qaSpeed.dispatch("change");
    runtime.elements.qaForward.dispatch("click");
    assert.equal(
        runtime.window.RatingTvScreen.state.qaReplayPhase,
        "PLAYING_FORWARD"
    );

    runtime.clock.advance(29 * 750);
    await runtime.flush();
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 30);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
    assert.equal(runtime.elements.qaForward.disabled, true);
    assert.match(runtime.elements.qaReplayStatus.textContent, /завершён/i);

    runtime.clock.advance(30000);
    await runtime.flush();
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 30);
    assert.equal(runtime.fetchCalls.length, 1);
});


test("saved replay plays backward to day 1 and stops without wrapping", async () => {
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        responses: [queuedResponse(buildReplayDocument())]
    });
    await runtime.flush();

    runtime.elements.qaDaySelect.value = "30";
    runtime.elements.qaDaySelect.dispatch("change");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 30);
    runtime.elements.qaSpeed.value = "4";
    runtime.elements.qaSpeed.dispatch("change");
    runtime.elements.qaBackward.dispatch("click");

    runtime.clock.advance(29 * 750);
    await runtime.flush();
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 1);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
    assert.equal(runtime.elements.qaBackward.disabled, true);
    assert.match(runtime.elements.qaReplayStatus.textContent, /день 1/i);
});


test("replay speed reschedules once while pause and step stay deterministic", async () => {
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        responses: [queuedResponse(buildReplayDocument())]
    });
    await runtime.flush();

    runtime.elements.qaForward.dispatch("click");
    runtime.clock.advance(1000);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 1);

    runtime.elements.qaSpeed.value = "4";
    runtime.elements.qaSpeed.dispatch("change");
    runtime.clock.advance(749);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 1);
    runtime.clock.advance(1);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 2);

    runtime.elements.qaPause.dispatch("click");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
    runtime.clock.advance(10000);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 2);

    runtime.elements.qaStep.dispatch("click");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 3);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
    assert.equal(runtime.fetchCalls.length, 1);
});


test("random replay navigation keeps saved deltas and fixed place slots", async () => {
    const replay = buildReplayDocument();
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        responses: [queuedResponse(replay)]
    });
    await runtime.flush();

    const firstSlot = runtime.elements.grid.children[0];
    const firstPlaceNode = firstSlot._ratingPlaceNode;
    runtime.elements.qaDaySelect.value = "3";
    runtime.elements.qaDaySelect.dispatch("change");
    const savedDayThreeDeltas = Array.from(
        replay.snapshots[2].payload.entries,
        (entry) => entry.position_delta
    );
    assert.deepEqual(
        Array.from(
            runtime.window.RatingTvScreen.state.payload.entries,
            (entry) => entry.position_delta
        ),
        savedDayThreeDeltas
    );
    assert.equal(runtime.elements.grid.children[0], firstSlot);
    assert.equal(runtime.elements.grid.children[0]._ratingPlaceNode, firstPlaceNode);

    runtime.elements.qaDaySelect.value = "1";
    runtime.elements.qaDaySelect.dispatch("change");
    runtime.elements.qaDaySelect.value = "2";
    runtime.elements.qaDaySelect.dispatch("change");
    assert.deepEqual(
        Array.from(
            runtime.window.RatingTvScreen.state.payload.entries,
            (entry) => entry.position_delta
        ),
        Array.from(
            replay.snapshots[1].payload.entries,
            (entry) => entry.position_delta
        )
    );
    assert.equal(runtime.elements.grid.children.length, 53);
    assert.equal(runtime.elements.grid.children[0], firstSlot);
    assert.equal(runtime.elements.grid.children[0]._ratingPlaceNode, firstPlaceNode);
});


test("incomplete saved replay fails closed without static fallback", async () => {
    const replay = buildReplayDocument();
    replay.snapshots.pop();
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        previewPayload: buildPayload({entries: buildEntries(53)}),
        responses: [queuedResponse(replay)]
    });
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "ERROR");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplay, null);
    assert.equal(runtime.elements.grid.hidden, true);
    assert.equal(runtime.elements.message.hidden, false);
    assert.equal(runtime.elements.qaDaySelect.disabled, true);
    assert.match(runtime.elements.messageTitle.textContent, /недоступно/i);
});


test("saved replay rejects different places for one equal score", async () => {
    const replay = buildReplayDocument();
    const firstDayEntries = replay.snapshots[0].payload.entries;
    firstDayEntries[1].score = firstDayEntries[0].score;
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        responses: [queuedResponse(replay)]
    });
    await runtime.flush();

    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "ERROR");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplay, null);
    assert.equal(runtime.elements.grid.hidden, true);
    assert.match(runtime.elements.messageTitle.textContent, /недоступно/i);
});


test("saved replay rejects a day marked unavailable", async () => {
    const replay = buildReplayDocument();
    replay.snapshots[11].payload.available = false;
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        responses: [queuedResponse(replay)]
    });
    await runtime.flush();

    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "ERROR");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplay, null);
    assert.equal(runtime.elements.grid.hidden, true);
});


test("keyboard visibility and mobile state pause replay safely", async () => {
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        responses: [queuedResponse(buildReplayDocument())]
    });
    await runtime.flush();

    const right = runtime.document.dispatch("keydown", {
        key: "ArrowRight",
        target: runtime.document
    });
    assert.equal(right.prevented, true);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 2);

    const ignored = runtime.document.dispatch("keydown", {
        key: "ArrowRight",
        target: runtime.elements.qaSpeed
    });
    assert.equal(ignored.prevented, false);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 2);

    runtime.elements.qaForward.dispatch("click");
    runtime.document.hidden = true;
    runtime.document.dispatch("visibilitychange");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
    runtime.clock.advance(10000);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 2);

    runtime.document.hidden = false;
    runtime.elements.qaForward.dispatch("click");
    runtime.window.innerWidth = 390;
    runtime.window.dispatch("resize");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
    runtime.clock.advance(10000);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 2);

    runtime.window.innerWidth = 1280;
    runtime.elements.qaForward.dispatch("click");
    assert.equal(
        runtime.window.RatingTvScreen.state.qaReplayPhase,
        "PLAYING_FORWARD"
    );
    runtime.window.innerWidth = 1152;
    runtime.window.dispatch("resize");
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
});


test("fullscreen rejection preserves replay day phase and status", async () => {
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        responses: [queuedResponse(buildReplayDocument())]
    });
    await runtime.flush();

    const originalStatus = runtime.elements.qaReplayStatus.textContent;
    runtime.document.documentElement.requestFullscreen = () => (
        Promise.reject(new Error("Fullscreen unavailable"))
    );

    runtime.elements.fullscreen.dispatch("click");
    await runtime.flush();

    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 1);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
    assert.equal(runtime.elements.qaReplayStatus.textContent, originalStatus);
    assert.equal(
        runtime.elements.fullscreen.title,
        "Полноэкранный режим недоступен"
    );
});


test("formula replay uses its pinned schema and preserves exact nullable KPI rows", async () => {
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        qaReplayKind: "formula",
        responses: [queuedResponse(buildFormulaReplayDocument("night"))]
    });
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 1);
    const requestUrl = new URL(runtime.fetchCalls[0].url);
    assert.equal(
        requestUrl.pathname,
        "/reports/rating/tv/qa-formula-replay-data/"
    );
    assert.deepEqual(
        Array.from(requestUrl.searchParams.keys()),
        ["shift_type"]
    );
    assert.equal(requestUrl.searchParams.get("shift_type"), "night");
    assert.equal(runtime.fetchCalls[0].options.signal.aborted, false);
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayPhase, "PAUSED");
    assert.equal(
        runtime.elements.refreshCountdown.textContent,
        "Формульный снимок"
    );
    assert.equal(runtime.elements.grid.children.length, 53);

    const firstScore = runtime.elements.grid.children[0]
        .querySelector(".rating-tv__score").children[0].textContent;
    const secondScore = runtime.elements.grid.children[1]
        .querySelector(".rating-tv__score").children[0].textContent;
    assert.equal(firstScore, "99.0001");
    assert.equal(secondScore, "99.0000");

    for (const row of runtime.elements.grid.children) {
        const avatar = row.querySelector(".rating-tv__avatar");
        assert.equal(avatar.children.length, 0);
    }
    const unrankedExpectations = [
        {
            rowIndex: 51,
            rowStatus: "withheld",
            rowClass: "is-withheld",
            statusText: "Удержан",
            statusLabel: "проверка данных"
        },
        {
            rowIndex: 52,
            rowStatus: "not_observed",
            rowClass: "is-not-observed",
            statusText: "Нет смен",
            statusLabel: "за период"
        }
    ];
    for (const expected of unrankedExpectations) {
        const row = runtime.elements.grid.children[expected.rowIndex];
        const sourceEntry = runtime.window.RatingTvScreen.state.qaReplay
            .snapshots[0].payload.entries[expected.rowIndex];
        assert.equal(row.dataset.rowStatus, expected.rowStatus);
        assert.equal(row.classList.contains(expected.rowClass), true);
        assert.equal(row.dataset.place, "");
        assert.equal(row.classList.contains("is-premium"), false);
        const place = row.querySelector(".rating-tv__place");
        assert.equal(place.children.length, 1);
        assert.equal(place.children[0].textContent, "—");
        const score = row.querySelector(".rating-tv__score");
        assert.equal(
            score.children[0].textContent,
            expected.statusText
        );
        assert.equal(
            score.children[1].textContent,
            expected.statusLabel
        );
        assert.equal(
            score.children.some((child) => child.textContent === "балл"),
            false
        );
        const movement = row.querySelector(".rating-tv__movement");
        assert.equal(movement.textContent, "");
        assert.equal(movement.classList.contains("is-unranked"), true);
        assert.equal(movement.getAttribute("aria-hidden"), "true");
        for (const field of [
            "score",
            "place",
            "shared_score_place",
            "position_delta"
        ]) {
            assert.equal(sourceEntry[field], null);
        }
        assert.equal(sourceEntry.level, "");
    }
});


test("visual and formula replay validators never autodetect or fall back", async () => {
    const formulaAsVisual = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        qaReplayKind: "visual",
        responses: [queuedResponse(buildFormulaReplayDocument("night"))]
    });
    await formulaAsVisual.flush();
    assert.equal(
        formulaAsVisual.window.RatingTvScreen.state.qaReplayPhase,
        "ERROR"
    );
    assert.equal(formulaAsVisual.window.RatingTvScreen.state.qaReplay, null);

    const visualAsFormula = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        qaReplayKind: "formula",
        responses: [queuedResponse(buildReplayDocument())]
    });
    await visualAsFormula.flush();
    assert.equal(
        visualAsFormula.window.RatingTvScreen.state.qaReplayPhase,
        "ERROR"
    );
    assert.equal(visualAsFormula.window.RatingTvScreen.state.qaReplay, null);
});


test("formula replay rejects fake zero KPI and non-four-decimal rated score", async () => {
    const fakeZero = buildFormulaReplayDocument("night");
    const withheld = fakeZero.snapshots[0].payload.entries[51];
    withheld.score = "0.0000";
    withheld.place = 0;
    withheld.shared_score_place = 0;
    withheld.position_delta = 0;

    const zeroRuntime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        qaReplayKind: "formula",
        responses: [queuedResponse(fakeZero)]
    });
    await zeroRuntime.flush();
    assert.equal(
        zeroRuntime.window.RatingTvScreen.state.qaReplayPhase,
        "ERROR"
    );
    assert.equal(zeroRuntime.elements.grid.hidden, true);

    const rounded = buildFormulaReplayDocument("night");
    rounded.snapshots[0].payload.entries[0].score = "99.00";
    const roundedRuntime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        qaReplayKind: "formula",
        responses: [queuedResponse(rounded)]
    });
    await roundedRuntime.flush();
    assert.equal(
        roundedRuntime.window.RatingTvScreen.state.qaReplayPhase,
        "ERROR"
    );
});


test("formula shift switch aborts stale response and preserves day on pause", async () => {
    const delayedDay = deferredResponse();
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        qaReplayKind: "formula",
        responses: [
            queuedResponse(buildFormulaReplayDocument("night")),
            {promise: delayedDay.promise},
            queuedResponse(buildFormulaReplayDocument("night"))
        ]
    });
    await runtime.flush();

    runtime.elements.qaDaySelect.value = "12";
    runtime.elements.qaDaySelect.dispatch("change");
    runtime.elements.qaForward.dispatch("click");
    assert.equal(
        runtime.window.RatingTvScreen.state.qaReplayPhase,
        "PLAYING_FORWARD"
    );

    runtime.elements.shiftType.value = "day";
    runtime.elements.shiftType.dispatch("change");
    assert.equal(runtime.fetchCalls.length, 2);
    assert.equal(runtime.elements.grid.hidden, true);
    assert.match(runtime.elements.messageText.textContent, /формульный/i);
    const staleSignal = runtime.fetchCalls[1].options.signal;
    assert.equal(staleSignal.aborted, false);

    runtime.elements.shiftType.value = "night";
    runtime.elements.shiftType.dispatch("change");
    assert.equal(runtime.fetchCalls.length, 3);
    assert.equal(staleSignal.aborted, true);
    await runtime.flush();

    assert.equal(
        runtime.window.RatingTvScreen.state.qaReplay.scope.shift_type,
        "night"
    );
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 12);
    assert.equal(
        runtime.window.RatingTvScreen.state.qaReplayPhase,
        "PAUSED"
    );
    assert.equal(runtime.elements.grid.hidden, false);

    delayedDay.resolve(buildFormulaReplayDocument("day"));
    await runtime.flush();
    assert.equal(
        runtime.window.RatingTvScreen.state.qaReplay.scope.shift_type,
        "night"
    );
    assert.equal(runtime.window.RatingTvScreen.state.qaReplayDay, 12);
    assert.equal(
        runtime.window.RatingTvScreen.state.qaReplayPhase,
        "PAUSED"
    );
});


test("formula preview cannot force a disabled shift", async () => {
    const runtime = createRuntime({
        qaPreview: true,
        qaReplayEnabled: true,
        qaReplayKind: "formula",
        qaFormulaEnabledShiftTypes: ["night"],
        responses: [queuedResponse(buildFormulaReplayDocument("night"))]
    });
    await runtime.flush();

    assert.equal(runtime.elements.shiftType.disabled, true);
    runtime.elements.shiftType.value = "day";
    runtime.elements.shiftType.dispatch("change");
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.window.RatingTvScreen.state.shiftType, "night");
    assert.equal(runtime.elements.shiftType.value, "night");
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


test("QA live reads state then materialized data and renders held placeholders", async () => {
    const qaState = buildQaLiveState({
        placeholders: [{
            employee_id: 99,
            status: "withheld",
            reasons: ["blocking_quality:data_conflict"],
            full_name: "ТЕСТ Удержанный водитель"
        }]
    });
    const payload = buildPayload({
        fingerprint: "source-materialized-abcdef",
        entries: buildEntries(2, {prefix: "LIVE водитель"})
    });
    payload.snapshot_revision = 7;
    payload.shift_score_fingerprint = "score-materialized-uvwxyz";
    const runtime = createRuntime({
        qaLive: true,
        responses: [
            queuedResponse(qaState),
            queuedResponse(payload)
        ]
    });
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 2);
    assert.equal(
        new URL(
            runtime.fetchCalls[0].url,
            "https://driverform.test"
        ).pathname,
        "/reports/rating/tv/qa-live/state/"
    );
    const dataUrl = new URL(runtime.fetchCalls[1].url);
    assert.equal(
        dataUrl.pathname,
        "/reports/rating/tv/qa-live/data/"
    );
    assert.equal(dataUrl.searchParams.get("rating_period"), "10");
    assert.equal(dataUrl.searchParams.get("watch_composition"), "20");
    assert.equal(dataUrl.searchParams.get("shift_type"), "night");
    assert.equal(
        dataUrl.searchParams.get("qa_run_id"),
        "qa-live-runtime-run"
    );
    assert.equal(dataUrl.searchParams.get("qa_step"), "1");
    for (const call of runtime.fetchCalls) {
        assert.equal(call.options.method, "GET");
        assert.equal(call.options.credentials, "same-origin");
        assert.equal(call.options.cache, "no-store");
    }
    assert.equal(runtime.elements.period.disabled, true);
    assert.equal(runtime.elements.composition.disabled, true);
    assert.equal(runtime.elements.shiftType.disabled, true);
    assert.equal(runtime.elements.refreshCountdown.textContent, "00:10");
    assert.equal(runtime.elements.qaLiveStep.textContent, "1");
    assert.equal(runtime.elements.qaLiveShift.textContent, "Ночная");
    assert.equal(runtime.elements.qaLiveRevision.textContent, "7");
    assert.equal(
        runtime.elements.qaLiveSourceFingerprint.textContent,
        "source-mat"
    );
    assert.equal(
        runtime.elements.qaLiveScoreFingerprint.textContent,
        "score-mate"
    );
    assert.equal(runtime.elements.grid.children.length, 3);
    const heldRow = runtime.elements.grid.children[2];
    assert.equal(heldRow.dataset.rowStatus, "withheld");
    assert.equal(heldRow.dataset.place, "");
    assert.equal(
        heldRow.querySelector(".rating-tv__name").textContent,
        "ТЕСТ Удержанный водитель"
    );
    assert.equal(
        heldRow.querySelector(".rating-tv__score").children[0].textContent,
        "Удержан"
    );
    assert.equal(
        runtime.window.RatingTvScreen.state.presentationPlaylist.length,
        0
    );
});


test("QA live refreshes the real chain after ten seconds", async () => {
    const firstPayload = buildPayload({
        fingerprint: "qa-live-source-1",
        entries: buildEntries(2, {prefix: "До шага"})
    });
    firstPayload.snapshot_revision = 1;
    firstPayload.shift_score_fingerprint = "qa-live-scores-1";
    const secondPayload = buildPayload({
        fingerprint: "qa-live-source-2",
        entries: buildEntries(2, {prefix: "После шага"})
    });
    secondPayload.snapshot_revision = 2;
    secondPayload.shift_score_fingerprint = "qa-live-scores-2";
    const runtime = createRuntime({
        qaLive: true,
        responses: [
            queuedResponse(buildQaLiveState({step: 1})),
            queuedResponse(firstPayload)
        ]
    });
    await runtime.flush();

    runtime.enqueueResponse(buildQaLiveState({step: 2}));
    runtime.enqueueResponse(secondPayload);
    runtime.clock.advance(10000);
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 4);
    assert.equal(runtime.elements.qaLiveStep.textContent, "2");
    assert.equal(runtime.elements.qaLiveRevision.textContent, "2");
    assert.match(
        runtime.elements.grid.children[0]
            .querySelector(".rating-tv__name")
            .textContent,
        /После шага/
    );
});


test("QA live never keeps another group under a 503 response", async () => {
    const firstPayload = buildPayload({
        compositionId: 20,
        shiftType: "night",
        fingerprint: "qa-live-night",
        entries: buildEntries(2, {prefix: "Чужая ночная группа"})
    });
    firstPayload.snapshot_revision = 1;
    firstPayload.shift_score_fingerprint = "night-scores";
    const runtime = createRuntime({
        qaLive: true,
        responses: [
            queuedResponse(buildQaLiveState({
                compositionId: 20,
                shiftType: "night"
            })),
            queuedResponse(firstPayload)
        ]
    });
    await runtime.flush();
    assert.equal(runtime.elements.grid.hidden, false);

    runtime.enqueueResponse(buildQaLiveState({
        step: 2,
        compositionId: 21,
        shiftType: "day"
    }));
    runtime.enqueueResponse(
        {error: "Снимок дневной группы ещё не сформирован."},
        503
    );
    await runtime.window.RatingTvScreen.loadQaLive();
    await runtime.flush();

    assert.equal(runtime.elements.grid.hidden, true);
    assert.equal(runtime.elements.message.hidden, false);
    assert.equal(runtime.window.RatingTvScreen.state.payload, null);
    assert.equal(runtime.window.RatingTvScreen.state.activeGroupKey, "");
    assert.doesNotMatch(
        runtime.elements.messageText.textContent,
        /Чужая ночная группа/
    );
    assert.equal(
        runtime.elements.messageText.textContent,
        "Снимок дневной группы ещё не сформирован."
    );
});


test("QA live rejects a materialized response with another group identity", async () => {
    const mismatchedPayload = buildPayload({
        periodId: 10,
        compositionId: 21,
        shiftType: "night",
        fingerprint: "wrong-group",
        entries: buildEntries(2, {prefix: "Чужой ответ"})
    });
    mismatchedPayload.snapshot_revision = 4;
    mismatchedPayload.shift_score_fingerprint = "wrong-scores";
    const runtime = createRuntime({
        qaLive: true,
        responses: [
            queuedResponse(buildQaLiveState({
                periodId: 10,
                compositionId: 20,
                shiftType: "night"
            })),
            queuedResponse(mismatchedPayload)
        ]
    });
    await runtime.flush();

    assert.equal(runtime.elements.grid.hidden, true);
    assert.equal(runtime.window.RatingTvScreen.state.payload, null);
    assert.equal(
        runtime.elements.messageTitle.textContent,
        "Снимок другой группы отклонён"
    );
    assert.doesNotMatch(
        runtime.elements.messageText.textContent,
        /Чужой ответ/
    );
});


test("QA live clears a same-group frame on 409 or 503 and retries later", async () => {
    for (const status of [409, 503]) {
        const firstPayload = buildPayload({
            fingerprint: `same-group-before-${status}`,
            entries: buildEntries(2, {prefix: "Старый кадр"})
        });
        firstPayload.snapshot_revision = 3;
        firstPayload.shift_score_fingerprint = `scores-before-${status}`;
        const runtime = createRuntime({
            qaLive: true,
            responses: [
                queuedResponse(buildQaLiveState({step: 1})),
                queuedResponse(firstPayload)
            ]
        });
        await runtime.flush();
        assert.equal(runtime.elements.grid.hidden, false);

        runtime.enqueueResponse(buildQaLiveState({step: 2}));
        runtime.enqueueResponse(
            {
                error: (
                    status === 409
                        ? "Состояние шага изменилось."
                        : "Состояние шага временно недоступно."
                )
            },
            status
        );
        await runtime.window.RatingTvScreen.loadQaLive();
        await runtime.flush();

        assert.equal(runtime.elements.grid.hidden, true);
        assert.equal(runtime.elements.message.hidden, false);
        assert.equal(runtime.window.RatingTvScreen.state.payload, null);
        assert.equal(runtime.window.RatingTvScreen.state.activeGroupKey, "");
        assert.equal(runtime.elements.qaLiveRevision.textContent, "—");
        assert.equal(
            runtime.elements.qaLiveSourceFingerprint.textContent,
            "—"
        );
        assert.equal(
            runtime.elements.qaLiveScoreFingerprint.textContent,
            "—"
        );
        assert.equal(
            runtime.elements.messageTitle.textContent,
            "Ожидаем согласованный QA-live снимок"
        );
        assert.equal(
            runtime.window.RatingTvScreen.state.groupCache.size,
            0
        );

        const recoveredPayload = buildPayload({
            fingerprint: `same-group-after-${status}`,
            entries: buildEntries(2, {prefix: "Новый кадр"})
        });
        recoveredPayload.snapshot_revision = 4;
        recoveredPayload.shift_score_fingerprint = (
            `scores-after-${status}`
        );
        runtime.enqueueResponse(buildQaLiveState({step: 2}));
        runtime.enqueueResponse(recoveredPayload);
        await runtime.window.RatingTvScreen.loadQaLive();
        await runtime.flush();

        assert.equal(runtime.elements.grid.hidden, false);
        assert.equal(runtime.elements.qaLiveRevision.textContent, "4");
        assert.match(
            runtime.elements.grid.children[0]
                .querySelector(".rating-tv__name")
                .textContent,
            /Новый кадр/
        );
    }
});


test("QA live ignores a late materialized response from the previous step", async () => {
    const lateData = deferredResponse();
    const runtime = createRuntime({
        qaLive: true,
        responses: [
            queuedResponse(buildQaLiveState({step: 1})),
            lateData
        ]
    });
    await runtime.flush();

    assert.equal(runtime.fetchCalls.length, 2);
    const lateSignal = runtime.fetchCalls[1].options.signal;
    assert.equal(lateSignal.aborted, false);
    assert.equal(
        new URL(runtime.fetchCalls[1].url).searchParams.get("qa_step"),
        "1"
    );

    const currentPayload = buildPayload({
        fingerprint: "current-step-source",
        entries: buildEntries(2, {prefix: "Текущий шаг"})
    });
    currentPayload.snapshot_revision = 8;
    currentPayload.shift_score_fingerprint = "current-step-scores";
    runtime.enqueueResponse(buildQaLiveState({step: 2}));
    runtime.enqueueResponse(currentPayload);
    await runtime.window.RatingTvScreen.loadQaLive();
    await runtime.flush();

    assert.equal(lateSignal.aborted, true);
    assert.equal(runtime.fetchCalls.length, 4);
    assert.equal(
        new URL(runtime.fetchCalls[3].url).searchParams.get("qa_step"),
        "2"
    );
    assert.equal(runtime.elements.qaLiveStep.textContent, "2");
    assert.equal(runtime.elements.qaLiveRevision.textContent, "8");

    const stalePayload = buildPayload({
        fingerprint: "late-step-source",
        entries: buildEntries(2, {prefix: "Опоздавший шаг"})
    });
    stalePayload.snapshot_revision = 7;
    stalePayload.shift_score_fingerprint = "late-step-scores";
    lateData.resolve(stalePayload);
    await runtime.flush();

    assert.equal(runtime.elements.qaLiveStep.textContent, "2");
    assert.equal(runtime.elements.qaLiveRevision.textContent, "8");
    assert.match(
        runtime.elements.grid.children[0]
            .querySelector(".rating-tv__name")
            .textContent,
        /Текущий шаг/
    );
    assert.doesNotMatch(
        runtime.elements.grid.children[0]
            .querySelector(".rating-tv__name")
            .textContent,
        /Опоздавший шаг/
    );
});
