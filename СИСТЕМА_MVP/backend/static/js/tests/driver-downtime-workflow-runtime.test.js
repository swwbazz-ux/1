"use strict";

const assert = require("node:assert/strict");
const {driverScreenSource} = require("./driver-screen-source");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");
const DRIVER_TEMPLATE_SOURCE = driverScreenSource();
const DRIVER_VIEWS_SOURCE = fs.readFileSync(
    path.join(BACKEND_ROOT, "users", "views.py"),
    "utf8"
);
const DRIVER_WORKFLOW_SOURCE = fs.readFileSync(
    path.join(BACKEND_ROOT, "downtimes", "driver_workflow.py"),
    "utf8"
);


function extractBraceBlock(source, signature, label, fromIndex = 0) {
    const start = source.indexOf(signature, fromIndex);
    assert.notEqual(start, -1, `${label} signature was not found.`);
    const open = source.indexOf("{", start + signature.length);
    assert.notEqual(open, -1, `${label} opening brace was not found.`);

    let depth = 0;
    let quote = "";
    let escaped = false;
    let lineComment = false;
    let blockComment = false;

    for (let index = open; index < source.length; index += 1) {
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
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`${label} closing brace was not found.`);
}


function createClassList(initial = []) {
    const values = new Set(initial);
    return {
        add(...names) {
            names.forEach((name) => values.add(name));
        },
        remove(...names) {
            names.forEach((name) => values.delete(name));
        },
        toggle(name, force) {
            const enabled = typeof force === "boolean" ? force : !values.has(name);
            if (enabled) values.add(name);
            else values.delete(name);
            return enabled;
        },
        contains(name) {
            return values.has(name);
        },
    };
}


function loadWaitingModeRuntime({hasTrip = true} = {}) {
    const source = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function applyDriverWaitingMode(payload)",
        "Driver waiting-operation UI helper"
    );
    const dial = {classList: createClassList()};
    const note = {textContent: hasTrip ? "ТОЧКА РАЗГРУЗКИ" : "НА ЗАГРУЗКУ"};
    const holdForm = hasTrip ? {dataset: {driverUnloadOneTap: "false"}} : null;
    const workDialControl = {
        classList: createClassList([hasTrip ? "is-loaded" : "is-empty"]),
        querySelector(selector) {
            return selector === ".driver-work-note" ? note : null;
        },
    };
    const context = {apply: null};

    vm.runInNewContext(
        `${source}\ncontext.apply = applyDriverWaitingMode;`,
        {String, context, holdForm, workDial: dial, workDialControl},
        {filename: "templates/users/driver_shift.html#waiting-operation-mode"}
    );
    assert.equal(typeof context.apply, "function");
    return {apply: context.apply, dial, holdForm, note, workDialControl};
}


function createEventTarget(properties = {}) {
    const listeners = new Map();
    return Object.assign({
        classList: createClassList(),
        addEventListener(type, listener) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(listener);
        },
        removeEventListener(type, listener) {
            const entries = listeners.get(type) || [];
            listeners.set(type, entries.filter((entry) => entry !== listener));
        },
        dispatch(type, properties = {}) {
            const event = Object.assign({
                type,
                defaultPrevented: false,
                preventDefault() {
                    this.defaultPrevented = true;
                },
            }, properties);
            for (const listener of [...(listeners.get(type) || [])]) {
                listener(event);
            }
            return event;
        },
        listenerCount(type) {
            return (listeners.get(type) || []).length;
        },
    }, properties);
}


function loadUnloadGestureBinder() {
    const source = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "window.bindDriverUnloadGesture = function (options)",
        "Driver unload gesture binder"
    );
    const context = {bind: null};
    const runtimeWindow = createEventTarget();
    const runtimeDocument = createEventTarget({
        hidden: false,
        visibilityState: "visible",
    });

    vm.runInNewContext(
        `${source};\ncontext.bind = window.bindDriverUnloadGesture;`,
        {context, document: runtimeDocument, window: runtimeWindow},
        {filename: "templates/users/driver_shift.html#unload-gesture"}
    );
    assert.equal(typeof context.bind, "function");
    return context.bind;
}


function loadActiveDowntimeRuntime() {
    const signatures = [
        "function formatDriverDowntimeDuration(seconds)",
        "function clearDriverDowntimeTimer()",
        "function renderDriverReasonDuration(button, totalSeconds, isActive)",
        "function syncDriverReasonTotals(payload, skipReasonId)",
        "function setDriverDowntimeStatusClass(statusKey)",
        "function driverDowntimeCanonicalEventId(eventId)",
        "function driverDowntimeIdentityKey(reasonId, startedAt)",
        "function driverDowntimeClosedKeys(reasonId, startedAt, eventId)",
        "function markDriverDowntimeInstanceClosed(reasonId, startedAt, eventId)",
        "function driverDowntimeInstanceIsClosed(reasonId, startedAt, eventId)",
        "function startDriverDowntimeTimer(payload)",
        "function applyDriverActiveDowntime(payload)",
        "function clearDriverActiveDowntime(payload)",
        "function syncDriverDowntimeTimerFromCard()",
    ];
    const source = "var driverDowntimeCardBlankTimer = null;\nvar driverDowntimeCardBlankConfirmed = false;\n"
        + signatures.map((signature) => extractBraceBlock(DRIVER_TEMPLATE_SOURCE, signature, signature)).join("\n");
    let nowMs = Date.parse("2026-09-27T01:00:00.000Z");
    let intervalCallback = null;
    const duration = () => ({hidden: true, textContent: ""});
    const makeButton = (id, baseSeconds) => {
        const reasonDuration = duration();
        return {
            dataset: {
                driverDowntimeReasonId: String(id),
                driverReasonSeconds: String(baseSeconds),
                driverReasonLabel: `Reason ${id}`,
            },
            classList: createClassList(),
            getAttribute() { return null; },
            setAttribute() {},
            querySelector(selector) {
                return selector === "[data-driver-reason-duration]" ? reasonDuration : null;
            },
            reasonDuration,
        };
    };
    const buttons = [makeButton(1, 10), makeButton(2, 3)];
    const downtimeCard = {dataset: {driverShiftDowntimeSeconds: "0", driverActiveElapsedSeconds: "0"}, classList: createClassList(), className: ""};
    const downtimeDuration = {textContent: ""};
    const downtimeTitle = {textContent: ""};
    const downtimeReason = {textContent: ""};
    const downtimeClose = {disabled: true, classList: createClassList(["is-disabled"]), setAttribute() {}, removeAttribute() {}};
    let nextTimeoutId = 100;
    const pendingTimeouts = new Map();
    const runtimeWindow = {
        driverDowntimeTimerId: null,
        driverDowntimeClock: null,
        driverDowntimeActiveEventId: "",
        setInterval(callback) {
            intervalCallback = callback;
            return 17;
        },
        clearInterval() {
            intervalCallback = null;
        },
        setTimeout(callback) {
            const id = nextTimeoutId++;
            pendingTimeouts.set(id, callback);
            return id;
        },
        clearTimeout(id) {
            pendingTimeouts.delete(id);
        },
    };
    const fireAllTimeouts = () => {
        const callbacks = [...pendingTimeouts.values()];
        pendingTimeouts.clear();
        callbacks.forEach((callback) => callback());
    };
    const shell = {
        querySelector(selector) {
            const match = selector.match(/driver-downtime-reason-id="([^"]+)"/);
            return match ? buttons.find((button) => button.dataset.driverDowntimeReasonId === match[1]) || null : null;
        },
    };
    const runtimeDocument = {
        querySelector(selector) {
            if (selector === "[data-driver-shell]") return runtimeDocumentShell;
            return null;
        },
    };
    const runtimeDocumentShell = {
        querySelector(selector) {
            return selector === "[data-driver-active-duration]" ? downtimeDuration : null;
        },
        querySelectorAll(selector) {
            return selector === "[data-driver-downtime-reason-button]" ? buttons : [];
        },
    };
    const RuntimeDate = {
        now: () => nowMs,
        parse: Date.parse,
    };
    const context = {};
    vm.runInNewContext(
        `${source}\n`
        + `context.apply = applyDriverActiveDowntime;\n`
        + `context.clear = clearDriverActiveDowntime;\n`
        + `context.start = startDriverDowntimeTimer;\n`
        + `context.syncFromCard = syncDriverDowntimeTimerFromCard;`,
        {
            context,
            Date: RuntimeDate,
            Math,
            Number,
            String,
            Array,
            Object,
            document: runtimeDocument,
            downtimeCard,
            downtimeDuration,
            downtimeTitle,
            downtimeReason,
            downtimeClose,
            downtimeReasonButtons: buttons,
            shell,
            window: runtimeWindow,
            applyDriverWaitingMode(payload) {
                const flow = String((payload && payload.workflow) || "");
                return flow === "waiting_loading" || flow === "waiting_unload";
            },
        },
        {filename: "templates/users/driver_shift.html#active-downtime"}
    );
    return {
        buttons,
        context,
        downtimeCard,
        downtimeDuration,
        window: runtimeWindow,
        setNow(value) { nowMs = Date.parse(value); },
        tick() { assert.ok(intervalCallback); intervalCallback(); },
        fireAllTimeouts,
    };
}


function loadDowntimeTimerRuntime() {
    const signatures = [
        "function formatDriverDowntimeDuration(seconds)",
        "function clearDriverDowntimeTimer()",
        "function renderDriverReasonDuration(button, totalSeconds, isActive)",
        "function syncDriverReasonTotals(payload, skipReasonId)",
        "function driverDowntimeCanonicalEventId(eventId)",
        "function driverDowntimeIdentityKey(reasonId, startedAt)",
        "function startDriverDowntimeTimer(payload)",
        "function snapshotDriverDowntimeTimer(atMs)",
    ];
    const source = signatures.map((signature) => (
        extractBraceBlock(DRIVER_TEMPLATE_SOURCE, signature, signature)
    )).join("\n");
    let nowMs = Date.parse("2026-09-17T01:00:00.000Z");
    let intervalCallback = null;
    const duration = () => ({hidden: true, textContent: ""});
    const makeButton = (id, baseSeconds) => {
        const reasonDuration = duration();
        return {
            dataset: {
                driverDowntimeReasonId: String(id),
                driverReasonSeconds: String(baseSeconds),
                driverReasonLabel: `Reason ${id}`,
            },
            classList: createClassList(),
            getAttribute() { return null; },
            setAttribute() {},
            querySelector(selector) {
                return selector === "[data-driver-reason-duration]" ? reasonDuration : null;
            },
            reasonDuration,
        };
    };
    const buttons = [makeButton(1, 10), makeButton(2, 3)];
    const downtimeCard = {dataset: {driverShiftDowntimeSeconds: "13", driverActiveElapsedSeconds: "10"}};
    const downtimeDuration = {textContent: ""};
    const runtimeWindow = {
        driverDowntimeTimerId: null,
        driverDowntimeClock: null,
        setInterval(callback) {
            intervalCallback = callback;
            return 17;
        },
        clearInterval() {
            intervalCallback = null;
        },
    };
    const shell = {
        querySelector(selector) {
            const match = selector.match(/driver-downtime-reason-id="([^"]+)"/);
            return match ? buttons.find((button) => button.dataset.driverDowntimeReasonId === match[1]) || null : null;
        },
    };
    const runtimeDocumentShell = {
        querySelector(selector) {
            return selector === "[data-driver-active-duration]" ? downtimeDuration : null;
        },
        querySelectorAll(selector) {
            return selector === "[data-driver-downtime-reason-button]" ? buttons : [];
        },
    };
    const runtimeDocument = {
        querySelector(selector) {
            return selector === "[data-driver-shell]" ? runtimeDocumentShell : null;
        },
    };
    const RuntimeDate = {
        now: () => nowMs,
        parse: Date.parse,
    };
    const context = {};
    vm.runInNewContext(
        `${source}\ncontext.start = startDriverDowntimeTimer; context.snapshot = snapshotDriverDowntimeTimer;`,
        {
            context,
            Date: RuntimeDate,
            Math,
            Number,
            Object,
            String,
            document: runtimeDocument,
            downtimeCard,
            downtimeDuration,
            downtimeReasonButtons: buttons,
            shell,
            window: runtimeWindow,
        },
        {filename: "templates/users/driver_shift.html#downtime-timers"}
    );
    return {
        buttons,
        context,
        downtimeCard,
        downtimeDuration,
        window: runtimeWindow,
        setNow(value) { nowMs = Date.parse(value); },
        tick() { assert.ok(intervalCallback); intervalCallback(); },
    };
}


test("all three unloading waits use one semantic workflow and template availability contract", () => {
    const tuple = DRIVER_WORKFLOW_SOURCE.match(
        /TRUCK_UNLOADING_WAIT_REASON_NAMES\s*=\s*\(([\s\S]*?)\)\s*\n/
    );
    assert.ok(tuple, "The canonical unloading-wait tuple must be declared.");
    const reasonNames = Array.from(
        tuple[1].matchAll(/["']([^"']+)["']/g),
        (match) => match[1]
    );
    assert.deepEqual(reasonNames, [
        "Ожидание разгрузки",
        "Ожидание разгрузки ККД",
        "Ожидание разгрузки СКДР",
    ]);
    assert.match(
        DRIVER_WORKFLOW_SOURCE,
        /DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD\s*=\s*["']waiting_unload["']/
    );
    assert.match(
        DRIVER_WORKFLOW_SOURCE,
        /DRIVER_DOWNTIME_FLOW_WAITING_LOADING\s*=\s*["']waiting_loading["']/
    );
    assert.match(
        DRIVER_WORKFLOW_SOURCE,
        /DRIVER_DOWNTIME_WORK_FLOWS\s*=\s*frozenset\([\s\S]*DRIVER_DOWNTIME_FLOW_WAITING_LOADING[\s\S]*DRIVER_DOWNTIME_FLOW_WAITING_UNLOAD/
    );
    assert.match(
        DRIVER_VIEWS_SOURCE,
        /for reason in downtime_reasons:[\s\S]*reason\.driver_workflow\s*=\s*driver_downtime_flow\(reason\)[\s\S]*reason\.driver_requires_loaded_trip\s*=\s*driver_downtime_requires_loaded_trip\(reason\)/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /data-driver-downtime-flow="\{\{ reason\.driver_workflow \}\}"/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /reason\.driver_unavailable_message/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /data-driver-unavailable-message="\{\{ reason\.driver_unavailable_message \}\}"/
    );
    // Гружёный — по любому открытому рейсу, включая ручной; правила — в driver_workflow.
    assert.match(
        DRIVER_VIEWS_SOURCE,
        /reason\.driver_unavailable_message = driver_downtime_unavailable_message\(\s*reason,\s*truck_loaded=driver_downtime_truck_loaded,/
    );
    assert.match(DRIVER_WORKFLOW_SOURCE, /DRIVER_LOADED_TRIP_REQUIRED_MESSAGE = 'Доступно только после погрузки'/);
    assert.match(
        DRIVER_VIEWS_SOURCE,
        /reason\.driver_requires_empty_truck and driver_has_open_trip:[\s\S]*Самосвал уже загружен/
    );
});

test("downtime review reconciles server truth and terminal events are not projected as active", () => {
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /window\.selectDriverDowntimeProjection\(ordered\)/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /event\.event_type === "driver\.downtime\.started" \|\| event\.event_type === "driver\.downtime\.ended"[\s\S]*window\.AppRealtime\.requestReconcile\([\s\S]*"driver_downtime_review"/
    );
});

test("a confirmed downtime receipt overrides only an older cached shell", () => {
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /function restoreDriverConfirmedDowntime\(outbox\)[\s\S]*getDowntimeProjectionReceipt\(context\.shiftId, context\.equipmentId\)/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /receiptAt <= shellAt[\s\S]*receipt\.event_type === "driver\.downtime\.ended"[\s\S]*clearDriverActiveDowntime/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /closedProjection\.shift_total_seconds[\s\S]*reason_totals: closedProjection\.reason_totals/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /restoreDriverConfirmedDowntime\(driverOfflineOutbox\)\.catch/
    );
});

test("downtime switch freezes the previous reason while the shift total stays continuous", () => {
    const runtime = loadDowntimeTimerRuntime();
    runtime.context.start({
        active: true,
        reason_id: 1,
        elapsed_seconds: 10,
        shift_total_seconds: 13,
        calculated_at: "2026-09-17T01:00:00.000Z",
        reason_totals: {1: 10, 2: 3},
    });
    runtime.setNow("2026-09-17T01:00:05.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:18");
    assert.equal(runtime.buttons[0].reasonDuration.textContent, "00:00:15");

    assert.equal(runtime.context.snapshot(Date.parse("2026-09-17T01:00:05.000Z")), 18);
    assert.equal(runtime.downtimeCard.dataset.driverShiftDowntimeSeconds, "18");
    assert.equal(runtime.buttons[0].dataset.driverReasonSeconds, "15");

    runtime.context.start({
        active: true,
        reason_id: 2,
        elapsed_seconds: 0,
        shift_total_seconds: 18,
        calculated_at: "2026-09-17T01:00:05.000Z",
    });
    runtime.setNow("2026-09-17T01:00:09.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:22");
    assert.equal(runtime.buttons[0].reasonDuration.textContent, "00:00:15");
    assert.equal(runtime.buttons[1].reasonDuration.textContent, "00:00:07");
});

test("repeated refresh of the same active downtime does not reset the running timer", () => {
    // Боевой 27.09.2026 (v358): каждое фоновое обновление экрана (опрос,
    // подмена фрагмента) заново вызывало startDriverDowntimeTimer с тем же
    // самым простоем (тот же event_id/причина), но оптимистичным/устаревшим
    // payload.elapsed_seconds (обычно 0) — таймер обнулялся каждые
    // несколько секунд, хотя простой всё это время был тем же самым.
    const runtime = loadDowntimeTimerRuntime();
    runtime.context.start({
        active: true,
        event_id: "local:downtime-1",
        reason_id: 1,
        started_at: "2026-09-17T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 13,
        calculated_at: "2026-09-17T01:00:00.000Z",
    });
    runtime.setNow("2026-09-17T01:00:02.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:15");

    // Повторная отрисовка того же простоя — как при фоновом опросе — со
    // "свежим" elapsed_seconds: 0, тем же event_id/причиной.
    runtime.context.start({
        active: true,
        event_id: "local:downtime-1",
        reason_id: 1,
        started_at: "2026-09-17T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 13,
        calculated_at: "2026-09-17T01:00:02.000Z",
    });
    runtime.setNow("2026-09-17T01:00:07.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:20");
});

test("the timer's base always counts from started_at, ignoring a zero or stale elapsed_seconds snapshot", () => {
    const runtime = loadDowntimeTimerRuntime();
    runtime.context.start({
        active: true,
        event_id: "local:downtime-2",
        reason_id: 1,
        // Простой на самом деле идёт уже 30 с, но снимок elapsed_seconds
        // прислал 0 (оптимистичный локальный payload) — таймер обязан
        // считать от started_at, а не от этого числа.
        started_at: "2026-09-17T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-17T01:00:30.000Z",
    });
    runtime.setNow("2026-09-17T01:00:30.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:30");
});

test("a local downtime syncing to a server-confirmed numeric id does not reset the timer", () => {
    // Боевой 27.09.2026 (v359), под нагрузкой (много техники меняет
    // состояние каждые секунды — версия сервера растёт часто): офлайн-запись
    // простоя синхронизируется, её event_id переходит из "local:<uuid>" в
    // серверный числовой ID, и КАЖДОЕ фоновое обновление после этого
    // сравнивало старый и новый вид ID как «другой простой» — таймер
    // обнулялся. started_at не меняется при синхронизации, поэтому именно
    // он — правильный признак «тот же простой».
    const runtime = loadActiveDowntimeRuntime();
    runtime.context.apply({
        event_id: "local:driver-downtime-uuid-1",
        reason_id: 1,
        started_at: "2026-09-27T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:00.000Z",
    });
    runtime.setNow("2026-09-27T01:00:04.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:04");

    // Тот же простой, но теперь под серверным числовым ID — та же причина,
    // тот же started_at, "свежий" elapsed_seconds: 0. Алиас ещё не известен —
    // сработать должен запасной признак (причина + started_at).
    runtime.context.apply({
        event_id: "55",
        reason_id: 1,
        started_at: "2026-09-27T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:04.000Z",
    });
    runtime.setNow("2026-09-27T01:00:09.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:09");
});

test("a device with a skewed clock still recognises the same downtime via the server-id alias", () => {
    // Замечание координатора 27.09.2026: started_at (причина + время начала)
    // — только запасной признак. На телефоне со сбитыми часами (реальный
    // Xiaomi отставал на ~30 мин) сервер хранит уже СКОРРЕКТИРОВАННОЕ
    // started_at (device_clock_ahead/behind), а телефон запомнил своё
    // собственное (несверенное) время старта — секунды не совпадут вообще.
    // driver-offline-outbox-v2.js пишет алиас "local:<uuid>" → серверный ID
    // синхронно в момент подтверждения (window.driverDowntimeIdAliases) —
    // именно он должен решать, а не started_at.
    const runtime = loadActiveDowntimeRuntime();
    runtime.context.apply({
        event_id: "local:driver-downtime-uuid-skew",
        reason_id: 4,
        started_at: "2026-09-27T01:00:00.000Z", // часы телефона, ещё не сверены
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:00.000Z",
    });
    runtime.setNow("2026-09-27T01:00:04.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:04");

    // Подтверждение пришло: outbox узнал server_ids.downtime_event_id и
    // записал алиас — ДО следующего вызова apply (как в реальном коде).
    runtime.window.driverDowntimeIdAliases = {
        "driver-downtime-uuid-skew": "77",
    };

    // Сервер прислал started_at, скорректированный на 30 минут вперёд —
    // причина+started_at НЕ совпадут с локальной записью.
    runtime.context.apply({
        event_id: "77",
        reason_id: 4,
        started_at: "2026-09-27T01:30:00.000Z", // серверная поправка часов
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:04.000Z",
    });
    runtime.setNow("2026-09-27T01:00:09.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:09");
});

test("a downtime closed on this phone cannot be resurrected by a stale local snapshot", () => {
    // Боевой 27.09.2026 (v359): после закрытия простоя ("завершить" или
    // честная разгрузка) более старая запись «простой начат», ещё не
    // вытесненная в локальном списке офлайн-событий записью «завершён»,
    // повторно применялась как активная при следующей же перерисовке — под
    // нагрузкой это окно ловилось часто (мигание окантовки, простой
    // «оживал»). Правило 1: локальное закрытие — истина, более старый снимок
    // его не отменяет.
    const runtime = loadActiveDowntimeRuntime();
    const payload = {
        event_id: "local:driver-downtime-uuid-2",
        reason_id: 2,
        started_at: "2026-09-27T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:00.000Z",
        status_key: "red",
    };
    runtime.context.apply(payload);
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), true);

    runtime.context.clear({shift_total_seconds: 0});
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), false);

    // Устаревший снимок того же простоя приходит ПОСЛЕ закрытия.
    const resurrectionAttempt = runtime.context.apply(payload);
    assert.equal(resurrectionAttempt, false);
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), false);
    assert.equal(runtime.downtimeCard.dataset.driverActiveReasonId, "");
});

test("a stale fragment that writes is-active directly onto the card gets corrected back, not just ignored", () => {
    // driver-shift-refresh-v1.js (ownDowntimeOnly-подмена и общий
    // driverMorphShell) копирует набор data-driver-active-* атрибутов И ВЕСЬ
    // class карточки состояния прямо с фрагмента — в обход
    // applyDriverActiveDowntime/clearDriverActiveDowntime. Если фрагмент
    // оказался запоздавшим снимком ДО закрытия, класс is-active и мигание
    // (driver-downtime-drum-v1.js читает ту же карточку) успевают вернуться
    // раньше, чем эта функция вообще заметит попытку воскрешения. Отказ
    // должен не просто "не применять" новое состояние, а АКТИВНО откатить
    // уже случившуюся прямую правку DOM.
    const runtime = loadActiveDowntimeRuntime();
    const payload = {
        event_id: "local:driver-downtime-uuid-3",
        reason_id: 3,
        started_at: "2026-09-27T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:00.000Z",
        status_key: "red",
    };
    runtime.context.apply(payload);
    runtime.context.clear({shift_total_seconds: 0});
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), false);

    // Имитация прямой правки фрагмента: атрибуты и class вернулись напрямую,
    // в обход applyDriverActiveDowntime.
    runtime.downtimeCard.classList.add("is-active");
    runtime.downtimeCard.dataset.driverActiveReasonId = String(payload.reason_id);
    runtime.downtimeCard.dataset.driverActiveDowntimeId = payload.event_id;
    runtime.downtimeCard.dataset.driverActiveStartedAt = payload.started_at;

    // То, что обычно вызывает syncDriverDowntimeTimerFromCard после любого
    // обновления экрана, — повторное применение того же payload.
    runtime.context.apply(payload);
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), false);
    assert.equal(runtime.downtimeCard.dataset.driverActiveReasonId, "");
});

test("a transient blank card from a background reconcile does not reset the timer or drop the border", () => {
    // Боевой 27.09.2026 (v359): фоновое обновление раз в ~20 с иногда
    // приносит карточку состояния простоя без data-driver-active-reason-id
    // / data-driver-active-downtime-id, хотя простой реально ещё идёт —
    // syncDriverDowntimeTimerFromCard (её вызывает каждое обновление экрана)
    // раньше читала это как «простоя нет» и сразу гасила таймер и
    // окантовку. Пустое значение само по себе не доказательство закрытия:
    // гасим только если оно ПОДТВЕРДИТСЯ ещё раз чуть позже, а не с первого
    // наблюдения.
    const runtime = loadActiveDowntimeRuntime();
    const payload = {
        event_id: "local:driver-downtime-uuid-4",
        reason_id: 4,
        started_at: "2026-09-27T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:00.000Z",
        status_key: "red",
    };
    runtime.context.apply(payload);
    // Обычный первый вызов после привязки экрана — синхронизирует
    // window.driverDowntimeActiveEventId с уже применённым простоем.
    runtime.context.syncFromCard();
    const clockBeforeBlank = runtime.window.driverDowntimeClock;
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), true);

    // Имитация подмены карточки без атрибутов активного простоя (core/
    // ownDowntimeOnly-снимок), в обход apply/clear — как это делает
    // driver-shift-refresh-v1.js напрямую через setAttribute.
    runtime.downtimeCard.dataset.driverActiveReasonId = "";
    runtime.downtimeCard.dataset.driverActiveDowntimeId = "";

    runtime.context.syncFromCard();
    // Одно пустое наблюдение — ещё не закрытие: таймер и окантовка целы.
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), true);
    assert.strictEqual(runtime.window.driverDowntimeClock, clockBeforeBlank);

    // Следующее обычное обновление (через ~2 с) снова приносит верные
    // атрибуты — ровно так это и происходило на бою между 20-секундными
    // реконсайлами.
    runtime.downtimeCard.dataset.driverActiveReasonId = String(payload.reason_id);
    runtime.downtimeCard.dataset.driverActiveDowntimeId = payload.event_id;
    runtime.context.syncFromCard();
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), true);
    assert.strictEqual(runtime.window.driverDowntimeClock, clockBeforeBlank);

    // Отложенная проверка (700 мс) срабатывает уже на восстановленной
    // карточке — подтверждения закрытия так и не случилось.
    runtime.fireAllTimeouts();
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), true);
    assert.strictEqual(runtime.window.driverDowntimeClock, clockBeforeBlank);
});

test("a card that stays blank through the confirmation window is treated as a genuine closure", () => {
    const runtime = loadActiveDowntimeRuntime();
    const payload = {
        event_id: "local:driver-downtime-uuid-5",
        reason_id: 5,
        started_at: "2026-09-27T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:00.000Z",
        status_key: "red",
    };
    runtime.context.apply(payload);
    runtime.context.syncFromCard();
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), true);

    runtime.downtimeCard.dataset.driverActiveReasonId = "";
    runtime.downtimeCard.dataset.driverActiveDowntimeId = "";
    runtime.context.syncFromCard();
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), true);

    // Карточка так и осталась пустой к моменту подтверждения — теперь это
    // считается настоящим закрытием.
    runtime.fireAllTimeouts();
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), false);
    assert.equal(runtime.window.driverDowntimeClock, null);
});

test("loading the page with a server-confirmed active downtime ticks with no local start event", () => {
    // Боевой 27.09.2026 (v360, реальный телефон владельца, Capacitor): после
    // очистки данных приложения очередь пустая, сервер знает активный простой
    // «Ожидание погрузки» — таймер отрисовался с серверным elapsed (00:07:40)
    // и НЕ тикал. Это ровно то, что делает bindDriverMobileShell при первой
    // отрисовке страницы: startDriverDowntimeTimer вызывается НАПРЯМУЮ с
    // атрибутами уже отрендеренной карточки, без единого локального события
    // в очереди.
    const runtime = loadActiveDowntimeRuntime();
    runtime.context.start({
        active: true,
        event_id: "56",
        reason_id: "18",
        started_at: "2026-09-27T00:52:20.000Z",
        elapsed_seconds: "460",
        shift_total_seconds: "460",
        calculated_at: "2026-09-27T01:00:00.000Z",
    });
    assert.equal(runtime.downtimeDuration.textContent, "00:07:40");

    runtime.setNow("2026-09-27T01:00:05.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:07:45");

    runtime.setNow("2026-09-27T01:00:11.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:07:51");
});

test("ten fragment resyncs in a minute never roll the timer back", () => {
    // Дополнение координатора 27.09.2026: после перезапуска простоя на живом
    // телефоне таймер несколько раз "затупил", откатился назад и снова
    // пошёл — источник тот же класс багов, что и "не тикает": повторный
    // вызов с тем же простоем (каждая фоновая сверка фрагмента раз в ~20 с,
    // здесь смоделировано десятью подряд за минуту) не должен трогать точку
    // отсчёта вовсе.
    const runtime = loadActiveDowntimeRuntime();
    const payload = {
        active: true,
        event_id: "56",
        reason_id: "18",
        started_at: "2026-09-27T01:00:00.000Z",
        elapsed_seconds: "0",
        shift_total_seconds: "0",
        calculated_at: "2026-09-27T01:00:00.000Z",
    };
    runtime.context.start(payload);
    assert.equal(runtime.downtimeDuration.textContent, "00:00:00");

    const readings = [];
    const startMs = Date.parse("2026-09-27T01:00:00.000Z");
    for (let i = 1; i <= 10; i += 1) {
        runtime.setNow(new Date(startMs + i * 6000).toISOString());
        runtime.tick();
        // Та же самая фоновая сверка фрагмента, тот же payload — как повторный
        // вызов startDriverDowntimeTimer из syncDriverDowntimeTimerFromCard.
        runtime.context.start(payload);
        readings.push(runtime.downtimeDuration.textContent);
    }
    assert.deepEqual(readings, [
        "00:00:06", "00:00:12", "00:00:18", "00:00:24", "00:00:30",
        "00:00:36", "00:00:42", "00:00:48", "00:00:54", "00:01:00",
    ]);
    for (let i = 1; i < readings.length; i += 1) {
        const toSeconds = (text) => text.split(":").reduce((total, part) => total * 60 + Number(part), 0);
        assert.ok(
            toSeconds(readings[i]) > toSeconds(readings[i - 1]),
            `reading ${i} (${readings[i]}) must be strictly greater than reading ${i - 1} (${readings[i - 1]})`
        );
    }
});

test("closing the downtime stops the display for good and a stale resync cannot revive it", () => {
    const runtime = loadActiveDowntimeRuntime();
    const payload = {
        event_id: "local:driver-downtime-uuid-6",
        reason_id: "18",
        started_at: "2026-09-27T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:00.000Z",
        status_key: "red",
    };
    runtime.context.apply(payload);
    runtime.setNow("2026-09-27T01:00:05.000Z");
    runtime.tick();
    assert.equal(runtime.downtimeDuration.textContent, "00:00:05");

    runtime.context.clear({shift_total_seconds: 5});
    assert.equal(runtime.downtimeDuration.textContent, "00:00:05");
    assert.equal(runtime.window.driverDowntimeClock, null);

    // Устаревшая фоновая сверка (тот же простой) приходит уже ПОСЛЕ закрытия
    // — applyDriverActiveDowntime отказывает через реестр закрытых простоев,
    // отсчёт не воскресает.
    const resurrectionAttempt = runtime.context.apply(payload);
    assert.equal(resurrectionAttempt, false);
    assert.equal(runtime.downtimeCard.classList.contains("is-active"), false);
    assert.equal(runtime.downtimeDuration.textContent, "00:00:05");
    assert.equal(runtime.window.driverDowntimeClock, null);
});

function loadDowntimeTimerAcrossShellReplacementRuntime() {
    const signatures = [
        "function formatDriverDowntimeDuration(seconds)",
        "function clearDriverDowntimeTimer()",
        "function renderDriverReasonDuration(button, totalSeconds, isActive)",
        "function syncDriverReasonTotals(payload, skipReasonId)",
        "function driverDowntimeCanonicalEventId(eventId)",
        "function driverDowntimeIdentityKey(reasonId, startedAt)",
        "function startDriverDowntimeTimer(payload)",
    ];
    const source = signatures.map((signature) => extractBraceBlock(DRIVER_TEMPLATE_SOURCE, signature, signature)).join("\n");
    let nowMs = Date.parse("2026-09-27T01:00:00.000Z");
    let intervalCallback = null;
    const makeButton = (id) => ({
        dataset: {driverDowntimeReasonId: String(id), driverReasonSeconds: "0", driverReasonLabel: `Reason ${id}`},
        classList: createClassList(),
        getAttribute() { return null; },
        setAttribute() {},
        querySelector(selector) {
            return selector === "[data-driver-reason-duration]" ? {hidden: true, textContent: ""} : null;
        },
    });
    const makeShell = () => {
        const buttons = [makeButton(18)];
        const duration = {textContent: ""};
        return {
            duration,
            buttons,
            querySelector(selector) {
                if (selector === "[data-driver-active-duration]") return duration;
                return null;
            },
            querySelectorAll(selector) {
                return selector === "[data-driver-downtime-reason-button]" ? buttons : [];
            },
        };
    };
    let currentShell = makeShell();
    const runtimeDocument = {
        querySelector(selector) {
            return selector === "[data-driver-shell]" ? currentShell : null;
        },
    };
    const runtimeWindow = {
        driverDowntimeTimerId: null,
        driverDowntimeClock: null,
        setInterval(callback) {
            intervalCallback = callback;
            return 17;
        },
        clearInterval() {
            intervalCallback = null;
        },
    };
    const RuntimeDate = {
        now: () => nowMs,
        parse: Date.parse,
    };
    const context = {};
    vm.runInNewContext(
        `${source}\ncontext.start = startDriverDowntimeTimer;`,
        {
            context,
            Date: RuntimeDate,
            Math,
            Number,
            String,
            Array,
            Object,
            document: runtimeDocument,
            shell: currentShell,
            downtimeReasonButtons: currentShell.buttons,
            window: runtimeWindow,
        },
        {filename: "templates/users/driver_shift.html#downtime-timer-shell-replacement"}
    );
    return {
        context,
        window: runtimeWindow,
        currentShell: () => currentShell,
        replaceShell() { currentShell = makeShell(); return currentShell; },
        setNow(value) { nowMs = Date.parse(value); },
        tick() { assert.ok(intervalCallback); intervalCallback(); },
    };
}

test("a full shell replacement does not freeze the timer on a detached node", () => {
    // Боевой 27.09.2026 (v361): владелец видел таймер "2:56", а следующим
    // обновлением (ровно один цикл ~20 с) он стал "3:18" — не тикал плавно,
    // а замирал и потом скачком показывал уже верное значение. Причина:
    // полная подмена <main data-driver-shell> создаёт новую оболочку и
    // заново вызывает bindDriverMobileShell, но "тот же простой" (v362 —
    // startDriverDowntimeTimer) намеренно не перезапускает интервал — он
    // продолжает жить на window. Раньше tick() держал ссылки на
    // downtimeDuration/downtimeReasonButtons из ЗАМЫКАНИЯ того вызова
    // bindDriverMobileShell, где интервал впервые стартовал, — эти узлы
    // после подмены отсоединены от документа, и видимый (новый) узел
    // переставал обновляться вовсе.
    const runtime = loadDowntimeTimerAcrossShellReplacementRuntime();
    runtime.context.start({
        active: true,
        event_id: "56",
        reason_id: "18",
        started_at: "2026-09-27T01:00:00.000Z",
        elapsed_seconds: 0,
        shift_total_seconds: 0,
        calculated_at: "2026-09-27T01:00:00.000Z",
    });
    const staleShell = runtime.currentShell();
    assert.equal(staleShell.duration.textContent, "00:00:00");

    // Полная подмена оболочки: bindDriverMobileShell перепривязывается на
    // НОВОМ узле (новая карточка, новая кнопка причины), интервал остаётся
    // тем же самым (window.driverDowntimeTimerId не менялся).
    const freshShell = runtime.replaceShell();

    runtime.setNow("2026-09-27T01:00:03.000Z");
    runtime.tick();

    assert.equal(freshShell.duration.textContent, "00:00:03");
    // Старый (отсоединённый) узел больше не должен как-либо использоваться.
    assert.equal(staleShell.duration.textContent, "00:00:00");
});

test("active downtime reason is a no-op and offline switches keep chronological dependencies", () => {
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /driverActiveReasonId[\s\S]*=== String\(button\.dataset\.driverDowntimeReasonId[\s\S]*return;/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /var latestPendingDowntime[\s\S]*depends_on: latestPendingDowntime \? \[latestPendingDowntime\.event_id\] : \[\]/
    );
});


test("waiting_unload enables the yellow mode but keeps hold-to-unload; clearing removes it", () => {
    const runtime = loadWaitingModeRuntime({hasTrip: true});

    assert.equal(runtime.apply({
        workflow: "waiting_unload",
        reason_label: "Ожидание ККД",
    }), true);
    // Разгрузка удержанием со шкалой и в ожидании разгрузки.
    assert.equal(runtime.holdForm.dataset.driverUnloadOneTap, "false");
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-operation"), true);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-unload"), true);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-loading"), false);
    assert.equal(runtime.dial.classList.contains("is-waiting-operation"), true);
    assert.equal(runtime.dial.classList.contains("is-waiting-unload"), true);
    assert.equal(runtime.note.textContent, "ОЖИДАНИЕ ККД");

    assert.equal(runtime.apply({workflow: ""}), false);
    assert.equal(runtime.holdForm.dataset.driverUnloadOneTap, "false");
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-operation"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-loading"), false);
    assert.equal(runtime.dial.classList.contains("is-waiting-operation"), false);
    assert.equal(runtime.dial.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.note.textContent, "ТОЧКА РАЗГРУЗКИ");

    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /\.driver-work-dial\.is-waiting-operation,[\s\S]*\.driver-work-dial-button\.is-waiting-operation\s*\{[\s\S]*?--driver-green:\s*#facc15/
    );
});

test("durable automatic and manual trip completion immediately suppress waiting_unload projection", () => {
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /function tripTerminalEventClosesWaitingUnload\(events, latestDowntime, activeFlow\)/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /event\.event_type === "driver\.trip\.unloaded"[\s\S]*event\.event_type === "driver\.trip\.manual_completed"/
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /tripTerminalEventClosesWaitingUnload\(ordered, latestDowntime, projectedDowntimeFlow\)[\s\S]*snapshotDriverDowntimeTimer[\s\S]*clearDriverActiveDowntime/
    );

    const source = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function tripTerminalEventClosesWaitingUnload(events, latestDowntime, activeFlow)",
        "Driver terminal waiting-unload projection helper"
    );
    const context = {helper: null};
    vm.runInNewContext(
        `${source}\ncontext.helper = tripTerminalEventClosesWaitingUnload;`,
        {context, String, Number},
        {filename: "static/js/driver-shift-v1.js#terminal-waiting-unload"}
    );
    const started = {event_type: "driver.downtime.started", sequence: 1, state: "pending"};
    const unloaded = {event_type: "driver.trip.unloaded", sequence: 2, state: "pending"};
    const nextWait = {event_type: "driver.downtime.started", sequence: 3, state: "pending"};
    const reviewed = {event_type: "driver.trip.manual_completed", sequence: 4, state: "conflict"};
    const manualCompleted = {event_type: "driver.trip.manual_completed", sequence: 5, state: "pending"};

    assert.equal(context.helper([started, unloaded], started, "waiting_unload"), unloaded);
    assert.equal(context.helper([started, unloaded, nextWait], nextWait, "waiting_unload"), null);
    assert.equal(context.helper([reviewed], null, "waiting_unload"), null);
    assert.equal(context.helper([manualCompleted], null, "waiting_loading"), null);
    assert.equal(context.helper([manualCompleted], null, "waiting_unload"), manualCompleted);
});


test("waiting_loading turns the empty Work dial yellow but keeps it inert", () => {
    const runtime = loadWaitingModeRuntime({hasTrip: false});

    assert.equal(runtime.apply({
        workflow: "waiting_loading",
        reason: "Ожидание погрузки",
    }), true);
    assert.equal(runtime.holdForm, null);
    assert.equal(runtime.dial.classList.contains("is-waiting-operation"), true);
    assert.equal(runtime.dial.classList.contains("is-waiting-loading"), true);
    assert.equal(runtime.dial.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-operation"), true);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-loading"), true);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-unload"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-empty"), true);
    assert.equal(runtime.note.textContent, "ОЖИДАНИЕ ПОГРУЗКИ");

    assert.equal(runtime.apply({workflow: ""}), false);
    assert.equal(runtime.dial.classList.contains("is-waiting-operation"), false);
    assert.equal(runtime.workDialControl.classList.contains("is-waiting-operation"), false);
    assert.equal(runtime.note.textContent, "НА ЗАГРУЗКУ");
});


test("one-tap pointerup submits once without relying on a synthetic click", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "true", holdComplete: "false"},
    });
    const captured = [];
    const button = createEventTarget({
        disabled: false,
        setPointerCapture(pointerId) {
            captured.push(pointerId);
        },
    });
    const holdCalls = {start: 0, reset: 0, cancel: 0};
    const holdGuard = {
        start() { holdCalls.start += 1; },
        reset() { holdCalls.reset += 1; },
        cancel() { holdCalls.cancel += 1; },
    };
    let submissions = 0;

    bind({
        form,
        button,
        holdGuard,
        canTrigger() { return true; },
        onOneTap() {
            submissions += 1;
            button.disabled = true;
            return true;
        },
    });

    const pointerDown = button.dispatch("pointerdown", {pointerId: 41});
    assert.equal(button.classList.contains("is-touch-armed"), true);
    button.dispatch("pointerleave", {pointerId: 41});
    assert.equal(
        button.classList.contains("is-touch-armed"),
        true,
        "a slight move outside the captured control must not disarm one-tap unload"
    );
    const pointerUp = button.dispatch("pointerup", {pointerId: 41});
    assert.equal(pointerDown.defaultPrevented, true);
    assert.equal(pointerUp.defaultPrevented, true);
    assert.deepEqual(captured, [41]);
    assert.equal(submissions, 1, "pointerup must submit even when no click event follows");
    assert.equal(button.classList.contains("is-touch-armed"), false);
    assert.equal(holdCalls.start, 0, "one-tap unload must not start the hold timer");

    const lateSyntheticClick = button.dispatch("click");
    assert.equal(lateSyntheticClick.defaultPrevented, true);
    assert.equal(submissions, 1, "the click fallback must not duplicate the pointerup request");
});


test("pointercancel disarms one-tap unload without submitting", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "true", holdComplete: "false"},
    });
    const button = createEventTarget({disabled: false, setPointerCapture() {}});
    let submissions = 0;

    bind({
        form,
        button,
        holdGuard: {start() {}, reset() {}, cancel() {}},
        onOneTap() {
            submissions += 1;
            return true;
        },
    });

    button.dispatch("pointerdown", {pointerId: 51});
    assert.equal(button.classList.contains("is-touch-armed"), true);
    button.dispatch("pointercancel", {pointerId: 51});
    assert.equal(button.classList.contains("is-touch-armed"), false);
    button.dispatch("pointerup", {pointerId: 51});
    assert.equal(submissions, 0);
});


test("keyboard click remains a one-tap fallback", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "true", holdComplete: "false"},
    });
    const button = createEventTarget({disabled: false});
    const holdGuard = {start() {}, reset() {}, cancel() {}};
    let submissions = 0;

    bind({
        form,
        button,
        holdGuard,
        onOneTap() {
            submissions += 1;
            button.disabled = true;
            return true;
        },
    });

    const click = button.dispatch("click", {detail: 0});
    assert.equal(click.defaultPrevented, true);
    assert.equal(submissions, 1);
});


test("normal unload still uses the hold guard", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "false", holdComplete: "false"},
    });
    const button = createEventTarget({disabled: false, setPointerCapture() {}});
    const holdCalls = {start: 0, reset: 0, cancel: 0};
    const holdGuard = {
        start() { holdCalls.start += 1; },
        reset() { holdCalls.reset += 1; },
        cancel() { holdCalls.cancel += 1; },
    };
    let submissions = 0;

    bind({
        form,
        button,
        holdGuard,
        onOneTap() { submissions += 1; },
    });

    button.dispatch("pointerdown", {pointerId: 7});
    button.dispatch("pointerup", {pointerId: 7});
    assert.equal(holdCalls.start, 1);
    assert.equal(holdCalls.reset, 1);
    assert.equal(submissions, 0);
});


test("fragment-style destroy and rebind leaves exactly one gesture listener", () => {
    const bind = loadUnloadGestureBinder();
    const form = createEventTarget({
        dataset: {driverUnloadOneTap: "true", holdComplete: "false"},
    });
    const button = createEventTarget({disabled: false, setPointerCapture() {}});
    const holdGuard = {start() {}, reset() {}, cancel() {}};
    let submissions = 0;
    const options = {
        form,
        button,
        holdGuard,
        onOneTap() {
            submissions += 1;
            button.disabled = true;
            return true;
        },
    };

    const initialBinding = bind(options);
    assert.equal(button.listenerCount("pointerup"), 1);
    button.dispatch("pointerdown", {pointerId: 8});
    assert.equal(button.classList.contains("is-touch-armed"), true);
    initialBinding.destroy();
    assert.equal(button.classList.contains("is-touch-armed"), false);
    button.disabled = false;
    const rebound = bind(options);
    assert.ok(rebound);
    assert.equal(button.listenerCount("pointerup"), 1);

    button.dispatch("pointerdown", {pointerId: 9});
    button.dispatch("pointerup", {pointerId: 9});
    assert.equal(submissions, 1);
});


test("an armed one-tap gesture blocks operational fragment replacement", () => {
    const unsafeSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function isDriverOperationalRefreshUnsafe(shell)",
        "Driver unsafe-refresh guard"
    );
    const context = {isUnsafe: null};
    let touchArmed = true;
    const unsafeSelector = (
        ".is-touch-armed, .is-holding, .is-pending, .is-dragging, "
        + ".is-lifting, .is-dropping, .is-snapping, .driver-drum-ghost, "
        + "[data-driver-point-sheet]:not([hidden]), "
        + "[data-driver-free-bucket-sheet]:not([hidden])"
    );
    const shell = {
        contains() { return false; },
        querySelector(selector) {
            if (selector === unsafeSelector) return touchArmed ? {} : null;
            return null;
        },
    };
    const document = {
        activeElement: null,
        querySelector() { return null; },
    };

    vm.runInNewContext(
        `${unsafeSource}\ncontext.isUnsafe = isDriverOperationalRefreshUnsafe;`,
        {context, document},
        {filename: "templates/users/driver_shift.html#unsafe-refresh-guard"}
    );

    assert.match(
        unsafeSource,
        /shell\.querySelector\(["']\.is-touch-armed,\s*\.is-holding,\s*\.is-pending,\s*\.is-dragging,\s*\.is-lifting,\s*\.is-dropping,\s*\.is-snapping,\s*\.driver-drum-ghost,\s*\[data-driver-point-sheet\]:not\(\[hidden\]\),\s*\[data-driver-free-bucket-sheet\]:not\(\[hidden\]\)["']\)/,
        "The static refresh guard must include the armed touch state."
    );
    assert.equal(context.isUnsafe(shell), true);
    touchArmed = false;
    assert.equal(context.isUnsafe(shell), false);
});


test("realtime waiting_loading to loaded-trip transition opens Work from server truth", async () => {
    const tabSyncSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function syncDriverTabMarkup(shell, tab)",
        "Driver tab markup synchronizer"
    );
    const refreshSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "window.applyOperationalStateRefresh = function (context)",
        "Driver operational refresh"
    );
    let replaced = false;
    let rebound = 0;
    const previousDowntimeCard = {
        dataset: {driverActiveDowntimeFlow: "waiting_loading"},
    };
    const freshWorkPanel = {
        dataset: {driverTabPanel: "work"},
        classList: createClassList(),
    };
    const freshDowntimesPanel = {
        dataset: {driverTabPanel: "downtimes"},
        classList: createClassList(["is-active"]),
    };
    const persistentWorkTab = {
        dataset: {driverTabOpen: "work"},
        classList: createClassList(),
    };
    const persistentDowntimesTab = {
        dataset: {driverTabOpen: "downtimes"},
        classList: createClassList(["is-active"]),
    };
    const oldShell = {
        dataset: {activeTab: "downtimes"},
        querySelector(selector) {
            if (selector === "[data-driver-active-downtime-flow]") {
                return previousDowntimeCard;
            }
            return null;
        },
        replaceWith(node) {
            replaced = node === freshShell;
        },
    };
    const freshShell = {
        dataset: {activeTab: "downtimes", driverHasLoadedTrip: "true"},
        querySelector(selector) {
            if (selector === '[data-driver-tab-panel="work"]') return freshWorkPanel;
            if (selector === '[data-driver-tab-panel="downtimes"]') return freshDowntimesPanel;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-driver-tab-panel]") {
                return [freshWorkPanel, freshDowntimesPanel];
            }
            return [];
        },
    };
    const document = {
        body: {dataset: {}},
        querySelector(selector) {
            return selector === "[data-driver-shell]" ? oldShell : null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-driver-tab-open]") {
                return [persistentWorkTab, persistentDowntimesTab];
            }
            return [];
        },
    };
    const runtimeWindow = {
        AppOperationalFragment: {
            request(_screen, version) {
                return Promise.resolve({html: "<main data-driver-shell></main>", version});
            },
            parseRoot() {
                return freshShell;
            },
        },
        bindDriverMobileShell() {
            rebound += 1;
        },
    };
    const context = {refresh: null};

    vm.runInNewContext(
        `${tabSyncSource}\n${refreshSource};\ncontext.refresh = window.applyOperationalStateRefresh;`,
        {
            context,
            document,
            isDriverOperationalRefreshUnsafe() {
                return false;
            },
            playDriverAssignmentAlert() {},
            playDriverDumpPointAlert() {},
            window: runtimeWindow,
        },
        {filename: "templates/users/driver_shift.html#operational-refresh"}
    );

    assert.equal((await context.refresh({version: 81234})).applied, true);
    assert.equal(replaced, true);
    assert.equal(freshShell.dataset.activeTab, "work");
    assert.equal(
        freshWorkPanel.classList.contains("is-active"),
        true,
        "The Work panel itself must become visible, not only the shell dataset."
    );
    assert.equal(freshDowntimesPanel.classList.contains("is-active"), false);
    assert.equal(persistentWorkTab.classList.contains("is-active"), true);
    assert.equal(persistentDowntimesTab.classList.contains("is-active"), false);
    assert.equal(document.body.dataset.operationalStateVersion, "81234");
    assert.equal(rebound, 1);
});


test("reduced motion keeps both waiting states but disables their pulse", () => {
    const reducedMotionBlocks = [];
    let offset = 0;
    while (true) {
        const start = DRIVER_TEMPLATE_SOURCE.indexOf(
            "@media (prefers-reduced-motion: reduce)",
            offset
        );
        if (start === -1) break;
        const block = extractBraceBlock(
            DRIVER_TEMPLATE_SOURCE,
            "@media (prefers-reduced-motion: reduce)",
            "Driver reduced-motion CSS",
            start
        );
        reducedMotionBlocks.push(block);
        offset = start + block.length;
    }
    assert.ok(reducedMotionBlocks.length >= 1);
    assert.ok(
        reducedMotionBlocks.some((block) => (
            block.includes(".driver-work-dial.is-waiting-operation::before")
            && block.includes(".driver-work-dial-button.is-waiting-operation .driver-work-dial-core")
            && /animation:\s*none/.test(block)
        )),
        "The waiting-operation pulse must stop when reduced motion is requested."
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /\.driver-work-dial\.is-waiting-operation,[\s\S]*\.driver-work-dial-button\.is-waiting-operation\s*\{[\s\S]*?--driver-green:\s*#facc15/,
        "Reduced motion must not remove the static yellow waiting state."
    );
});


test("Driver JavaScript branches on workflow instead of an exact Russian reason label", () => {
    const waitingModeSource = extractBraceBlock(
        DRIVER_TEMPLATE_SOURCE,
        "function applyDriverWaitingMode(payload)",
        "Driver waiting-operation UI helper"
    );
    assert.match(
        waitingModeSource,
        /flow\s*===\s*["']waiting_loading["']/
    );
    assert.match(
        waitingModeSource,
        /flow\s*===\s*["']waiting_unload["']/
    );
    assert.doesNotMatch(
        waitingModeSource,
        /String\s*\(\s*payload\.reason[\s\S]{0,160}(?:===|==)[\s\S]{0,80}["']ожидание разгрузки["']/i
    );
    assert.doesNotMatch(
        waitingModeSource,
        /["']ожидание разгрузки["'][\s\S]{0,80}(?:===|==)[\s\S]{0,160}payload\.reason/i
    );
});
