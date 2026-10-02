"use strict";
/* Задание v376, бой 02.10.2026 (Infinix без сети):
   A — «Ожидание погрузки» не гасло при ручной погрузке и даже после разгрузки:
       гасил его только сервер. Теперь простой закрывает сам телефон тем же
       временем, что и рейс, и закрытие встаёт в очередь раньше рейса.
   B — после местной погрузки «Ожидание разгрузки» оставалось серым: доступность
       причин рисовал сервер. Теперь её считает телефон по своему рейсу.
   Функции исполняются по-настоящему, вокруг — подмены. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function source(name) {
    return fs.readFileSync(path.resolve(__dirname, "..", name), "utf8").replace(/\r\n/g, "\n");
}

function block(text, signature) {
    const start = text.indexOf(signature);
    assert.notEqual(start, -1, signature);
    let depth = 0;
    for (let i = text.indexOf("{", start); i < text.length; i += 1) {
        if (text[i] === "{") depth += 1;
        else if (text[i] === "}") {
            depth -= 1;
            if (depth === 0) return text.slice(start, i + 1);
        }
    }
    throw new Error(signature);
}

const WORKSPACE = source("driver-manual-excavator-workspace-v1.js");
const SHIFT = source("driver-shift-v1.js");
const DRUM = source("driver-downtime-drum-v1.js");

test("a manual load closes the open downtime first, at the very same moment", async () => {
    const calls = [];
    const context = vm.createContext({
        Promise,
        Date,
        calls,
        root: {
            driverCloseDowntimeForTrip(kind, at) { calls.push(["close", kind, at]); return Promise.resolve({}); },
            driverOfflineOutbox: {
                pending() { calls.push(["pending"]); return Promise.resolve([]); },
                enqueue(event) { calls.push(["enqueue", event.occurred_at]); return Promise.resolve(event); },
            },
        },
    });
    vm.runInContext([
        "var currentTripProjection = null, manualCompletionPendingKey = '';",
        "function sourceShouldBeLocked() { return false; }",
        "function savingBlocks() { return false; }",
        "function setSavingLocal() {} function setSourceLocked() {} function setResult() {}",
        "function updateManualTripCount() {} function startTripTimer() {} function markLastDump() {}",
        "function syncWorkspaceContext() {} function updatePointAction() {} function playManualFeedback() {}",
        "function announceManualTrip() {} function stopTripTimer() {}",
        "function buildManualLoadEvent(workspace, target, events, occurredAt) { calls.push(['build', occurredAt]); return {event_id: 'load-1', occurred_at: occurredAt}; }",
        block(WORKSPACE, "    function closeDowntimeForTrip("),
        block(WORKSPACE, "    function startManualLoad("),
    ].join("\n"), context);

    const saved = await vm.runInContext(
        "startManualLoad({dataset: {}}, {dataset: {eoDumpTarget: '1', eoDumpName: 'ККД'}})",
        context,
    );

    assert.equal(calls[0][0], "close");
    assert.equal(calls[0][1], "load");
    const at = calls[0][2];
    assert.deepEqual(JSON.parse(JSON.stringify(calls.slice(1))), [["pending"], ["build", at], ["enqueue", at]]);
    assert.equal(saved.occurred_at, at);
});

test("a manual unload closes only the waiting-for-unload downtime, before the completion", async () => {
    const calls = [];
    const context = vm.createContext({
        Promise,
        Date,
        calls,
        root: {
            driverCloseDowntimeForTrip(kind, at) { calls.push(["close", kind, at]); return Promise.resolve(null); },
            driverOfflineOutbox: {
                pending() { calls.push(["pending"]); return Promise.resolve([]); },
                enqueue(event) { calls.push(["enqueue", event.occurred_at]); return Promise.resolve(event); },
            },
        },
    });
    vm.runInContext([
        "var currentTripProjection = {event_id: 'load-1', payload: {dump_point_id: 1}}, manualCompletionPendingKey = '';",
        "function savingBlocks() { return false; }",
        "function setSavingLocal() {} function setSourceLocked() {} function setResult() {}",
        "function updateManualTripCount() {} function stopTripTimer() {} function markLastDump() {}",
        "function restoreStandardTargets() {} function updatePointAction() {} function playManualFeedback() {}",
        "function announceManualTrip() {} function sourceShouldBeLocked() { return false; }",
        "function positive(v) { var n = Number(v); return n > 0 ? n : null; }",
        "function toggleRejectedTripAck() {} function setManualExitAvailability() {} function closeWorkspace() {}",
        "function buildManualCompletedEvent(workspace, target, events, occurredAt) { calls.push(['build', occurredAt]); return {event_id: 'done-1', occurred_at: occurredAt}; }",
        block(WORKSPACE, "    function closeDowntimeForTrip("),
        block(WORKSPACE, "    function completeManualLoad("),
    ].join("\n"), context);

    const target = {
        dataset: {eoReturnEnabled: "true", eoDumpTarget: "1"},
        classList: {add() {}, remove() {}},
    };
    await vm.runInContext("completeManualLoad({dataset: {}}, target)", Object.assign(context, {target}));

    assert.equal(calls[0][1], "unload");
    const at = calls[0][2];
    assert.deepEqual(JSON.parse(JSON.stringify(calls.slice(1, 4))), [["pending"], ["build", at], ["enqueue", at]]);
});

function runClose({ flow, startedAt, kind, at }) {
    const posted = [];
    const cleared = [];
    const card = {
        dataset: {
            driverActiveDowntimeId: "local:dt-1",
            driverActiveDowntimeFlow: flow,
            driverActiveStartedAt: startedAt,
        },
    };
    const context = vm.createContext({
        Promise,
        Date,
        Number,
        String,
        window: {},
        downtimeCard: card,
        posted,
        cleared,
    });
    vm.runInContext([
        "function generateClientActionId(prefix) { return prefix + '-x'; }",
        "function postDriverDowntimeAction(payload) { posted.push(payload); return Promise.resolve({closed: true}); }",
        "function clearDriverActiveDowntime(payload) { cleared.push(payload); }",
        block(SHIFT, "    window.driverCloseDowntimeForTrip = function") + ";",
    ].join("\n"), context);
    return context.window.driverCloseDowntimeForTrip(kind, at).then(() => ({ posted, cleared }));
}

test("a load closes any open downtime with the load time; an unload — only waiting for unload", async () => {
    const at = "2026-10-01T23:53:31.000Z";
    const load = await runClose({ flow: "waiting_loading", startedAt: "2026-10-01T23:53:19.000Z", kind: "load", at });
    assert.deepEqual(JSON.parse(JSON.stringify(load.posted)), [{ action: "close", client_action_id: "driver-downtime-close-x", occurred_at: at }]);
    assert.equal(load.cleared.length, 1);

    const repair = await runClose({ flow: "", startedAt: "2026-10-01T23:50:00.000Z", kind: "load", at });
    assert.equal(repair.posted.length, 1, "ОФР тоже гасит погрузка (п. 4)");

    const unloadOther = await runClose({ flow: "", startedAt: "2026-10-01T23:50:00.000Z", kind: "unload", at });
    assert.equal(unloadOther.posted.length, 0, "разгрузка не трогает обычный простой");

    const unloadWait = await runClose({ flow: "waiting_unload", startedAt: "2026-10-01T23:50:00.000Z", kind: "unload", at });
    assert.equal(unloadWait.posted.length, 1);

    const later = await runClose({ flow: "", startedAt: "2026-10-01T23:54:00.000Z", kind: "load", at });
    assert.equal(later.posted.length, 0, "простой после отметки рейс не закрывает");
});

function classList(initial) {
    const set = new Set(initial);
    return {
        contains: (n) => set.has(n),
        add: (n) => set.add(n),
        remove: (n) => set.delete(n),
        toggle: (n, on) => { if (on) set.add(n); else set.delete(n); return !!on; },
    };
}

function element(dataset, classes) {
    const attrs = new Map();
    return {
        dataset: Object.assign({}, dataset),
        classList: classList(classes || []),
        setAttribute: (n, v) => attrs.set(n, String(v)),
        getAttribute: (n) => (attrs.has(n) ? attrs.get(n) : null),
        hasAttribute: (n) => attrs.has(n),
        removeAttribute: (n) => attrs.delete(n),
    };
}

function runAvailability({ manualPoint, projection, hasOpenTrip }) {
    // Сервер нарисовал «Ожидание разгрузки» недоступным: на сервере рейса ещё нет.
    const waitUnload = element({
        driverDowntimeReasonId: "10", driverDowntimeRequires: "loaded", driverDowntimePoint: "",
        driverReasonLabel: "Ожидание разгрузки", driverUnavailableMessage: "Доступно только после погрузки",
    }, ["is-unavailable"]);
    waitUnload.setAttribute("aria-disabled", "true");
    const waitLoad = element({
        driverDowntimeReasonId: "9", driverDowntimeRequires: "empty", driverDowntimePoint: "",
        driverReasonLabel: "Ожидание погрузки",
    });
    const cards = { "10": [element({}, ["driver-drum-card", "is-unavailable"])], "9": [element({}, ["driver-drum-card"])] };
    const shell = element({ driverHasOpenTrip: hasOpenTrip ? "true" : "false" });
    const hold = element({});
    const context = vm.createContext({
        root: {
            DriverManualExcavatorWorkspace: {
                activeManualPointId: () => manualPoint,
                activeManualPointName: () => (manualPoint ? "ККД" : ""),
                projectionState: () => projection,
            },
        },
        buttons: [waitUnload, waitLoad],
        cards,
        shell,
        hold,
    });
    vm.runInContext([
        "function q(sel) { return sel === '[data-driver-shell]' ? shell : sel === '[data-driver-hold-button]' ? hold : null; }",
        "function all(sel) { if (sel.indexOf('data-driver-drum-reason-id') >= 0) return cards[sel.match(/reason-id=\"(\\d+)\"/)[1]] || []; return buttons; }",
        block(DRUM, "    function truckIsLoaded("),
        block(DRUM, "    function tripPointName("),
        block(DRUM, "    function localRefusal("),
        block(DRUM, "    function syncAvailability("),
        "syncAvailability();",
    ].join("\n"), context);
    return { waitUnload, waitLoad, cards };
}

test("after a manual load on the phone «waiting for unload» becomes available, «waiting for loading» does not", () => {
    const { waitUnload, waitLoad, cards } = runAvailability({ manualPoint: "1", projection: "pending", hasOpenTrip: false });
    assert.equal(waitUnload.getAttribute("aria-disabled"), null);
    assert.equal(waitUnload.classList.contains("is-unavailable"), false);
    assert.equal(cards["10"][0].classList.contains("is-local-available"), true);
    // Сверка состава барабана идёт по is-unavailable — его не трогаем.
    assert.equal(cards["10"][0].classList.contains("is-unavailable"), true);
    assert.equal(waitLoad.getAttribute("aria-disabled"), "true");
    assert.equal(cards["9"][0].classList.contains("is-local-unavailable"), true);
});

test("after a manual unload on the phone the truck is empty although the server screen is old", () => {
    const { waitUnload, waitLoad } = runAvailability({ manualPoint: "", projection: "completing", hasOpenTrip: true });
    assert.equal(waitUnload.getAttribute("aria-disabled"), "true");
    assert.equal(waitUnload.dataset.driverUnavailableMessage, "Доступно только после погрузки");
    assert.equal(waitLoad.getAttribute("aria-disabled"), null);
});
