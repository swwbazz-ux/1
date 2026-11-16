"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const {dispatcherScreenSource} = require("./dispatcher-screen-source");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const SETTINGS_PATH = path.join(BACKEND, "static", "js", "dispatcher-detail-settings-v1.js");
const DETAIL_PATH = path.join(BACKEND, "static", "js", "dispatcher-detail-v1.js");
const SETTINGS_SOURCE = fs.readFileSync(SETTINGS_PATH, "utf8");
const DETAIL_SOURCE = fs.readFileSync(DETAIL_PATH, "utf8");
const TEMPLATE = dispatcherScreenSource();
const PWA_SOURCE = fs.readFileSync(path.join(BACKEND, "trips", "dispatcher_pwa.py"), "utf8");
const PRODUCTION_MANIFEST = fs.readFileSync(
    path.resolve(BACKEND, "..", "..", ".github", "deploy", "fixtures", "dispatcher-release-files.txt"),
    "utf8"
);

function fakeElement(tagName) {
    const node = {
        tagName: String(tagName || "div").toUpperCase(),
        attributes: new Map(),
        children: [],
        listeners: new Map(),
        textContent: "",
        hidden: false,
        disabled: false,
        value: "",
        selected: false,
        dataset: {},
        className: "",
        appendChild(child) {
            child.parentNode = this;
            this.children.push(child);
            if (this.tagName === "SELECT" && child.tagName === "OPTION" && child.selected) {
                this.value = child.value;
            }
            return child;
        },
        setAttribute(name, value) {
            this.attributes.set(name, String(value));
        },
        addEventListener(name, handler) {
            this.listeners.set(name, handler);
        },
        remove() {
            if (!this.parentNode) return;
            this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
        },
        querySelector(selector) {
            return this.querySelectorAll(selector)[0] || null;
        },
        querySelectorAll(selector) {
            const match = /^\[([^\]]+)\]$/.exec(selector);
            const attribute = match ? match[1] : "";
            const found = [];
            function visit(current) {
                current.children.forEach((child) => {
                    if (attribute && child.attributes.has(attribute)) found.push(child);
                    visit(child);
                });
            }
            visit(this);
            return found;
        },
    };
    Object.defineProperty(node, "options", {
        get() {
            return this.children.filter((child) => child.tagName === "OPTION");
        },
    });
    let html = "";
    Object.defineProperty(node, "innerHTML", {
        get() {
            return html;
        },
        set(value) {
            html = String(value);
            if (!html) this.children = [];
        },
    });
    return node;
}

function loadSettingsRuntime() {
    const selectors = [
        "[data-gd-detail-settings]", "[data-gd-detail-settings-title]",
        "[data-gd-detail-settings-hint]", "[data-gd-detail-settings-status]",
        "[data-gd-setting-horizon]", "[data-gd-setting-block]", "[data-gd-setting-rock]",
        "[data-gd-destination-list]", "[data-gd-destination-add]",
        "[data-gd-destination-count]", "[data-gd-setting-save]",
    ];
    const nodes = new Map(selectors.map((selector) => [selector, fakeElement("div")]));
    nodes.set("[data-gd-setting-rock]", fakeElement("select"));
    const document = {
        querySelector(selector) {
            return nodes.get(selector) || null;
        },
        createElement(tagName) {
            return fakeElement(tagName);
        },
    };
    const window = {setTimeout() {}};
    vm.runInNewContext(SETTINGS_SOURCE, {window, document, fetch() {}}, {filename: SETTINGS_PATH});
    return {window, nodes};
}

test("detail settings presenter renders work fields and destination rows independently", () => {
    const {window, nodes} = loadSettingsRuntime();
    const detailLayer = fakeElement("section");
    const settings = window.createDispatcherDetailSettings({
        detailLayer,
        getShiftOpen: () => true,
        roleIsReadonly: () => false,
    });

    settings.render({
        editable: true,
        title: "Рабочие параметры",
        hint: "Проверьте значения",
        loading_horizon: "1050",
        loading_block: "12",
        rock_type_id: 7,
        rock_types: [{id: 7, name: "Вскрыша"}],
        dump_points: [{id: 1, name: "Склад"}, {id: 2, name: "Отвал"}],
        destinations: [{dump_point_id: 1, transport_distance_km: "3.5"}],
    });

    assert.equal(nodes.get("[data-gd-detail-settings]").hidden, false);
    assert.equal(nodes.get("[data-gd-setting-horizon]").value, "1050");
    assert.equal(nodes.get("[data-gd-setting-block]").value, "12");
    assert.equal(nodes.get("[data-gd-setting-rock]").value, "7");
    assert.equal(nodes.get("[data-gd-destination-count]").textContent, "1 точка");
    assert.equal(nodes.get("[data-gd-destination-add]").disabled, false);
    assert.deepEqual(
        JSON.parse(JSON.stringify(settings.collectDestinations())),
        [{dump_point_id: "1", transport_distance_km: "3,5"}]
    );

    settings.reset();
    assert.equal(nodes.get("[data-gd-detail-settings]").hidden, true);
});

function optionValues(select) {
    return select.options.map((option) => option.value);
}

function destinationSelects(nodes) {
    const list = nodes.get("[data-gd-destination-list]");
    return list
        .querySelectorAll("[data-gd-destination-row]")
        .map((row) => row.querySelector("[data-gd-destination-select]"));
}

function destinationRemoveButtons(nodes) {
    const list = nodes.get("[data-gd-destination-list]");
    return list
        .querySelectorAll("[data-gd-destination-row]")
        .map((row) => row.querySelector("[data-gd-destination-remove]"));
}

function renderSettings(window, nodes, settings) {
    const runtime = window.createDispatcherDetailSettings({
        detailLayer: fakeElement("section"),
        getShiftOpen: () => true,
        roleIsReadonly: () => false,
    });
    runtime.render(settings);
    return runtime;
}

const THREE_POINTS = [
    {id: 1, name: "ККД"},
    {id: 2, name: "СКДР"},
    {id: 3, name: "Отвал"},
];

test("assigned dump points are absent from other destination rows", () => {
    const {window, nodes} = loadSettingsRuntime();
    renderSettings(window, nodes, {
        editable: true,
        rock_type_id: 7,
        rock_types: [{id: 7, name: "Вскрыша"}],
        dump_points: THREE_POINTS,
        destinations: [{dump_point_id: 1}, {dump_point_id: 2}],
    });

    const selects = destinationSelects(nodes);
    assert.equal(selects.length, 2);
    assert.deepEqual(optionValues(selects[0]), ["", "1", "3"]);
    assert.deepEqual(optionValues(selects[1]), ["", "2", "3"]);
    assert.equal(selects[0].value, "1");
    assert.equal(selects[1].value, "2");
});

test("adding a destination picks a free point and removing frees it again", () => {
    const {window, nodes} = loadSettingsRuntime();
    const runtime = renderSettings(window, nodes, {
        editable: true,
        rock_type_id: 7,
        rock_types: [{id: 7, name: "Вскрыша"}],
        dump_points: THREE_POINTS,
        destinations: [{dump_point_id: 1}, {dump_point_id: 2}],
    });

    nodes.get("[data-gd-destination-add]").listeners.get("click")();

    let selects = destinationSelects(nodes);
    assert.equal(selects.length, 3);
    assert.equal(selects[2].value, "3");
    assert.deepEqual(optionValues(selects[0]), ["", "1"]);
    assert.deepEqual(optionValues(selects[1]), ["", "2"]);
    assert.deepEqual(optionValues(selects[2]), ["", "3"]);
    assert.equal(nodes.get("[data-gd-destination-add]").disabled, true);
    assert.deepEqual(
        JSON.parse(JSON.stringify(runtime.collectDestinations().map((row) => row.dump_point_id))),
        ["1", "2", "3"]
    );

    destinationRemoveButtons(nodes)[2].listeners.get("click")();

    selects = destinationSelects(nodes);
    assert.equal(selects.length, 2);
    assert.deepEqual(optionValues(selects[0]), ["", "1", "3"]);
    assert.deepEqual(optionValues(selects[1]), ["", "2", "3"]);
    assert.equal(nodes.get("[data-gd-destination-add]").disabled, false);
});

test("clearing a row returns its point to the other rows", () => {
    const {window, nodes} = loadSettingsRuntime();
    renderSettings(window, nodes, {
        editable: true,
        rock_type_id: 7,
        rock_types: [{id: 7, name: "Вскрыша"}],
        dump_points: THREE_POINTS,
        destinations: [{dump_point_id: 1}, {dump_point_id: 2}],
    });

    const select = destinationSelects(nodes)[1];
    select.value = "";
    select.listeners.get("change")();

    assert.deepEqual(optionValues(destinationSelects(nodes)[0]), ["", "1", "2", "3"]);
});

test("empty catalogue and fully assigned list leave nothing to add", () => {
    const {window, nodes} = loadSettingsRuntime();
    renderSettings(window, nodes, {
        editable: true,
        rock_type_id: 7,
        rock_types: [{id: 7, name: "Вскрыша"}],
        dump_points: [],
        destinations: [],
    });

    assert.equal(destinationSelects(nodes).length, 0);
    assert.equal(nodes.get("[data-gd-destination-count]").textContent, "не назначены");
    assert.equal(nodes.get("[data-gd-destination-add]").disabled, true);

    const second = loadSettingsRuntime();
    renderSettings(second.window, second.nodes, {
        editable: true,
        rock_type_id: 7,
        rock_types: [{id: 7, name: "Вскрыша"}],
        dump_points: [{id: 1, name: "ККД"}, {id: 2, name: "СКДР"}],
        destinations: [{dump_point_id: 1}, {dump_point_id: 2}],
    });

    assert.equal(second.nodes.get("[data-gd-destination-add]").disabled, true);
    assert.deepEqual(
        optionValues(destinationSelects(second.nodes)[0]),
        ["", "1"]
    );
    assert.deepEqual(
        optionValues(destinationSelects(second.nodes)[1]),
        ["", "2"]
    );
});

test("settings code has one owner and detail runtime delegates render and reset", () => {
    assert.match(SETTINGS_SOURCE, /function saveDetailSettings\(\)/);
    assert.match(SETTINGS_SOURCE, /function addDetailDestinationRow\(destination\)/);
    assert.match(SETTINGS_SOURCE, /global\.createDispatcherDetailSettings = createDispatcherDetailSettings;/);
    assert.match(DETAIL_SOURCE, /global\.createDispatcherDetailSettings\(\{/);
    assert.match(DETAIL_SOURCE, /detailSettingsRuntime\.render\(data\.settings \|\| null\);/);
    assert.match(DETAIL_SOURCE, /detailSettingsRuntime\.reset\(\);/);
    assert.doesNotMatch(DETAIL_SOURCE, /function saveDetailSettings\(\)/);
    assert.doesNotMatch(DETAIL_SOURCE, /function addDetailDestinationRow\(destination\)/);
});

test("settings module is loaded before detail and packaged exactly once", () => {
    const settingsIndex = TEMPLATE.indexOf("dispatcher-detail-settings-v1.js");
    const chartsIndex = TEMPLATE.indexOf("dispatcher-detail-charts-v1.js");
    const detailIndex = TEMPLATE.indexOf("dispatcher-detail-v1.js");
    assert.ok(settingsIndex >= 0, "settings module is absent from dispatcher template");
    assert.ok(chartsIndex > settingsIndex, "charts must load after settings presenter");
    assert.ok(detailIndex > chartsIndex, "detail must load after its presenters");
    assert.match(TEMPLATE, /dispatcher-detail-settings-v1\.js[^\n]+dispatcher-desktop-shell-v167/);

    const pwaMatches = PWA_SOURCE.match(/\/static\/js\/dispatcher-detail-settings-v1\.js/g) || [];
    assert.equal(pwaMatches.length, 1, "settings module must occur once in PWA CORE_ASSETS");

    const manifestPath = "СИСТЕМА_MVP/backend/static/js/dispatcher-detail-settings-v1.js";
    const manifestMatches = PRODUCTION_MANIFEST.split(/\r?\n/).filter((line) => line === manifestPath);
    assert.equal(manifestMatches.length, 1, "settings module must occur once in production manifest");
});
