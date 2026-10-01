"use strict";
/* Матрица E1, 01.10.2026: машинист смахнул машину на ККД, водитель тоже смахнул
   ККД в ручном режиме. Сервер сводит обе отметки в один рейс машиниста, ручная
   проекция телефона гаснет («automatic»). Круг водителя при этом красился пустым
   «ЭКС-1 НА ЗАГРУЗКУ», а следующие ответы сервера совпадали со снимком экрана —
   подмена пропускалась, и круг оставался тёмным при гружёной машине. Здесь
   syncDial() исполняется по-настоящему, с подменёнными узлами круга. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const DRUM = fs.readFileSync(path.resolve(__dirname, "../driver-point-drum-v1.js"), "utf8").replace(/\r\n/g, "\n");

function fn(name) {
    const start = DRUM.indexOf("    function " + name + "(");
    if (start < 0) return "";
    let depth = 0;
    for (let i = DRUM.indexOf("{", start); i < DRUM.length; i += 1) {
        if (DRUM[i] === "{") depth += 1;
        else if (DRUM[i] === "}") {
            depth -= 1;
            if (depth === 0) return DRUM.slice(start, i + 1);
        }
    }
    throw new Error(name);
}

function classes(initial) {
    const set = new Set(initial);
    return {
        contains: (n) => set.has(n),
        add: (...n) => n.forEach((x) => set.add(x)),
        remove: (...n) => n.forEach((x) => set.delete(x)),
        toggle: (n, on) => { if (on) set.add(n); else set.delete(n); return !!on; },
        list: () => Array.from(set).sort()
    };
}

function element(initialClasses) {
    const attrs = new Map();
    return {
        dataset: {},
        disabled: false,
        classList: classes(initialClasses),
        setAttribute: (n, v) => attrs.set(n, String(v)),
        getAttribute: (n) => (attrs.has(n) ? attrs.get(n) : null),
        hasAttribute: (n) => attrs.has(n),
        removeAttribute: (n) => attrs.delete(n)
    };
}

function run({ projectionState, origin, loaded }) {
    // Круг в момент, когда ручная точка ещё стояла в нём: гружёный, ручной.
    const button = element(["driver-work-dial-button", "is-loaded"]);
    button.dataset.driverManualDial = "true";
    button.dataset.driverManualDialLabel = "ККД";
    const wrap = element(["driver-work-dial", "is-loaded"]);
    const shellNode = element([]);
    Object.assign(shellNode.dataset, {
        driverActiveTripOrigin: origin,
        driverHasLoadedTrip: loaded,
        driverActualDumpPointName: "ККД"
    });
    const manualWorkspace = element([]);
    manualWorkspace.dataset.driverManualExcavatorLabel = "ЭКС-1";
    const note = { textContent: "ТОЧКА РАЗГРУЗКИ" };
    const labels = [];
    const reconciles = [];
    const root = {
        driverForceFragmentApply: false,
        AppRealtime: { requestReconcile: (reason) => { reconciles.push(reason); return true; } },
        setTimeout: () => 0
    };
    const nodes = {
        "[data-driver-manual-workspace]": manualWorkspace,
        ".driver-work-note": note
    };
    const context = vm.createContext({
        root,
        button,
        wrap,
        shellNode,
        nodes,
        labels,
        projection: projectionState
    });
    const source = [
        "function q(sel) { return nodes[sel] || null; }",
        "function holdButton() { return button; }",
        "function dial() { return wrap; }",
        "function shell() { return shellNode; }",
        "function isManual() { return true; }",
        "function assignedPointId() { return ''; }",
        "function pointName() { return ''; }",
        "function setDialLabel(text) { labels.push(text); }",
        "function engine() { return { projectionState: function () { return projection; } }; }",
        fn("setUnloadWait"),
        fn("serverTripOwnsDial"),
        fn("handDialToServerTrip"),
        fn("syncDial"),
        "syncDial();"
    ].join("\n");
    vm.runInContext(source, context);
    return { button, wrap, note, labels, root, reconciles };
}

test("a manual load merged into the excavator's trip hands the dial to the loaded server trip", () => {
    const result = run({ projectionState: "automatic", origin: "excavator", loaded: "true" });
    assert.equal(result.button.classList.contains("is-loaded"), true);
    assert.equal(result.button.classList.contains("is-empty"), false);
    assert.equal(result.button.disabled, false);
    assert.equal(result.button.hasAttribute("aria-disabled"), false);
    assert.equal(result.button.getAttribute("aria-label"), "Подтвердить разгрузку. Удерживайте 1 секунду.");
    assert.equal(result.wrap.classList.contains("is-loaded"), true);
    assert.equal(result.wrap.classList.contains("is-empty"), false);
    assert.deepEqual(result.labels, ["ККД"]);
    assert.equal(result.note.textContent, "ТОЧКА РАЗГРУЗКИ");
    assert.equal(result.button.dataset.driverManualDial, undefined);
    // Точную разметку круга вернёт подмена, минуя сверку со снимком экрана.
    assert.equal(result.root.driverForceFragmentApply, true);
    assert.deepEqual(result.reconciles, ["driver_manual_handover"]);
});

test("a manual trip that really ended still leaves an empty dial heading back to the excavator", () => {
    // Завершение ручного рейса: экран сервера ещё старый (рейс машиниста), но
    // проекция — «completing», а не «automatic»: круг гаснет как раньше.
    const result = run({ projectionState: "completing", origin: "excavator", loaded: "true" });
    assert.equal(result.button.classList.contains("is-empty"), true);
    assert.equal(result.button.disabled, true);
    assert.deepEqual(result.labels, ["ЭКС-1"]);
    assert.equal(result.note.textContent, "НА ЗАГРУЗКУ");
    assert.equal(result.root.driverForceFragmentApply, false);
});

test("without a loaded server trip the dial goes empty even after an automatic takeover", () => {
    const result = run({ projectionState: "automatic", origin: "excavator", loaded: "false" });
    assert.equal(result.button.classList.contains("is-empty"), true);
    assert.equal(result.root.driverForceFragmentApply, false);
});
