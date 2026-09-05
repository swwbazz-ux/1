"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const TEMPLATE_PATH = path.resolve(
    __dirname,
    "..",
    "..",
    "..",
    "templates",
    "trips",
    "excavator_work.html"
);
const FACE_CSS_PATH = path.resolve(
    __dirname,
    "..",
    "..",
    "css",
    "mobile-face-unified-v1.css"
);
const TEMPLATE_SOURCE = fs.readFileSync(TEMPLATE_PATH, "utf8").replace(/\r\n?/g, "\n");
const FACE_CSS_SOURCE = fs.readFileSync(FACE_CSS_PATH, "utf8");


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
            if (escaped) escaped = false;
            else if (character === "\\") escaped = true;
            else if (character === quote) quote = "";
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
        if (character === "{") depth += 1;
        if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`${label} closing brace was not found.`);
}


function extractDumpStateSource() {
    const startMarker =
        '    var dumpChoiceButtons = Array.prototype.slice.call(shell.querySelectorAll("[data-eo-dump-select]"));';
    const endMarker = "    function snapshotTruckCard(card)";
    const start = TEMPLATE_SOURCE.indexOf(startMarker);
    const end = TEMPLATE_SOURCE.indexOf(endMarker, start);
    assert.notEqual(start, -1, "Production dump-state start marker was not found.");
    assert.notEqual(end, -1, "Production dump-state end marker was not found.");
    return TEMPLATE_SOURCE.slice(start, end);
}


function extractSettingsSuccessHandler() {
    const successMarker =
        '}).then(function (data) {\n                playExcavatorSound("action_ok");\n                var savedIds = normalizeDumpPointIds(data.dump_point_ids || ids);';
    const successStart = TEMPLATE_SOURCE.indexOf(successMarker);
    assert.notEqual(successStart, -1, "Production Face-settings success handler was not found.");
    return extractBraceBlock(
        TEMPLATE_SOURCE,
        "function (data)",
        "Production Face-settings success handler",
        successStart
    );
}


class FakeClassList {
    constructor(names = []) {
        this.names = new Set(names);
    }

    add(name) {
        this.names.add(name);
    }

    remove(name) {
        this.names.delete(name);
    }

    contains(name) {
        return this.names.has(name);
    }

    toggle(name, force) {
        const enabled = force === undefined ? !this.names.has(name) : Boolean(force);
        if (enabled) this.names.add(name);
        else this.names.delete(name);
        return enabled;
    }
}


class FakeElement {
    constructor({classes = [], dataset = {}, value = ""} = {}) {
        this.attributes = new Map();
        this.classList = new FakeClassList(classes);
        this.dataset = {...dataset};
        this.disabled = false;
        this.textContent = "";
        this.value = value;
    }

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }
}


function dumpButton(id, name, {persisted = false, selected = false} = {}) {
    return new FakeElement({
        classes: selected ? ["is-selected"] : [],
        dataset: {
            eoDumpName: name,
            eoDumpPersisted: persisted ? "true" : "false",
            eoDumpSelect: String(id),
        },
    });
}


function createRuntime({refreshResult = true} = {}) {
    const points = [
        dumpButton(1, "Буферный склад", {persisted: true, selected: true}),
        dumpButton(2, "ККД"),
        dumpButton(3, "Отвал"),
    ];
    const applySettings = new FakeElement({
        dataset: {
            eoSettingsApplied: "false",
            eoSettingsAvailable: "true",
        },
    });
    const rockSelect = new FakeElement({value: "7"});
    const horizonInput = new FakeElement({value: "75"});
    const blockInput = new FakeElement({value: "52"});
    const faceControls = [rockSelect, horizonInput, blockInput];
    const faceScreen = new FakeElement();
    const faceContent = new FakeElement({dataset: {eoFaceSettingsAvailable: "true"}});
    const dumpPointsInput = new FakeElement();
    const dumpInput = new FakeElement({value: "1"});
    const shell = {
        dataset: {},
        querySelector(selector) {
            const controls = {
                "[data-eo-apply-settings]": applySettings,
                "[data-eo-rock-select]": rockSelect,
                "[data-eo-face-horizon]": horizonInput,
                "[data-eo-face-block]": blockInput,
            };
            return controls[selector] || null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-eo-dump-select]") return points;
            if (selector === "[data-eo-face-horizon], [data-eo-face-block], [data-eo-rock-select]") {
                return faceControls;
            }
            assert.fail(`Unexpected production selector: ${selector}`);
        },
    };
    const notices = [];
    const activatedTabs = [];
    const refreshCalls = [];
    const sandbox = {
        context: {},
        shell,
        faceScreen,
        faceContent,
        dumpPointsInput,
        dumpInput,
        applySettings,
        ids: ["1"],
        window: {},
        showExcavatorNotice(message) {
            notices.push(message);
        },
        playExcavatorSound() {
            return Promise.resolve(true);
        },
        setApplySettingsLabel(text) {
            applySettings.textContent = text;
        },
        refreshExcavatorWorkFromServer(options) {
            refreshCalls.push(options || {});
            return Promise.resolve(refreshResult);
        },
        activateTab(tab) {
            activatedTabs.push(tab);
        },
    };

    vm.runInNewContext(
        `
        ${extractDumpStateSource()}
        context.render = renderDumpChoiceStates;
        context.toggle = toggleDumpChoice;
        context.selectedIds = selectedDumpPointIds;
        context.persistedIds = function () { return Object.keys(persistedDumpPointIds).sort(); };
        context.setPending = setFaceSettingsPending;
        context.success = ${extractSettingsSuccessHandler()};
        `,
        sandbox,
        {filename: "templates/trips/excavator_work.html#face-dump-state"}
    );

    return {
        ...sandbox.context,
        activatedTabs,
        applySettings,
        dumpInput,
        dumpPointsInput,
        notices,
        points,
        refreshCalls,
        shell,
    };
}


function assertState(button, expected) {
    assert.equal(button.dataset.eoDumpState, expected.state);
    assert.equal(button.classList.contains("is-selected"), expected.selected);
    assert.equal(button.classList.contains("is-dump-applied"), expected.applied);
    assert.equal(button.classList.contains("is-dump-pending"), expected.pendingAdd);
    assert.equal(button.classList.contains("is-dump-pending-remove"), expected.pendingRemove);
    assert.equal(button.getAttribute("aria-pressed"), expected.selected ? "true" : "false");
}


test("Face dump choices distinguish applied, pending-add and pending-remove states", () => {
    const runtime = createRuntime();
    const [applied, added] = runtime.points;

    assertState(applied, {
        state: "applied",
        selected: true,
        applied: true,
        pendingAdd: false,
        pendingRemove: false,
    });
    assert.match(applied.getAttribute("aria-label"), /Активная точка разгрузки/);

    runtime.toggle(added);
    assertState(added, {
        state: "pending-add",
        selected: true,
        applied: false,
        pendingAdd: true,
        pendingRemove: false,
    });
    assert.match(added.getAttribute("aria-label"), /Будет добавлена после применения/);

    runtime.toggle(applied);
    assertState(applied, {
        state: "pending-remove",
        selected: false,
        applied: false,
        pendingAdd: false,
        pendingRemove: true,
    });
    assert.match(applied.getAttribute("aria-label"), /Будет отключена после применения/);
    assert.deepEqual(Array.from(runtime.selectedIds()), ["2"]);
    assert.equal(runtime.dumpPointsInput.value, "2");
    assert.deepEqual(runtime.notices, []);
});


test("A successful save canonicalizes numeric server IDs into the persisted green set", async () => {
    const runtime = createRuntime();

    await runtime.success({
        dump_point_ids: [2, 3],
        work_context_changed: false,
        active_downtime_reason: "",
    });

    assert.deepEqual(Array.from(runtime.persistedIds()), ["2", "3"]);
    assert.deepEqual(Array.from(runtime.selectedIds()), ["2", "3"]);
    assertState(runtime.points[0], {
        state: "idle",
        selected: false,
        applied: false,
        pendingAdd: false,
        pendingRemove: false,
    });
    runtime.points.slice(1).forEach((button) => {
        assertState(button, {
            state: "applied",
            selected: true,
            applied: true,
            pendingAdd: false,
            pendingRemove: false,
        });
        assert.equal(button.dataset.eoDumpPersisted, "true");
    });
    assert.equal(runtime.points[0].dataset.eoDumpPersisted, "false");
    assert.equal(runtime.dumpPointsInput.value, "2,3");
    assert.equal(runtime.dumpInput.value, "2");
    assert.equal(runtime.refreshCalls.length, 1);
    assert.equal(runtime.refreshCalls[0].pendingOwner, "face");
});


test("A failed post-save fragment refresh keeps Face visible and safely unlocks the confirmed draft", async () => {
    const runtime = createRuntime({refreshResult: false});
    runtime.setPending(true);
    runtime.applySettings.classList.add("is-pending");

    await runtime.success({
        dump_point_ids: [1, 2],
        work_context_changed: false,
        active_downtime_reason: "",
    });

    assert.equal(runtime.refreshCalls.length, 1);
    assert.equal(runtime.refreshCalls[0].pendingOwner, "face");
    assert.equal(runtime.shell.dataset.eoActiveTab, "face");
    assert.deepEqual(runtime.activatedTabs, ["face"]);
    assert.equal(runtime.applySettings.classList.contains("is-pending"), false);
    assert.equal(runtime.points.every((button) => !button.disabled), true);
    assert.match(runtime.notices.at(-1), /Настройки сохранены/);
});


test("Face state classes own the requested green, yellow and neutral visual contracts", () => {
    assert.match(
        FACE_CSS_SOURCE,
        /\.is-dump-applied\s*\{[^}]*border-color:\s*#67e854[^}]*color:\s*#7df06c/s
    );
    assert.match(
        FACE_CSS_SOURCE,
        /\.is-dump-pending\s*\{[^}]*border-color:\s*#ffd52a[^}]*color:\s*#ffe063/s
    );
    assert.match(
        FACE_CSS_SOURCE,
        /\.is-dump-pending-remove\s*\{[^}]*background:\s*rgba\(7,\s*18,\s*23,[^)]+\)[^}]*color:\s*#aab7bd[^}]*box-shadow:\s*none/s
    );
});
