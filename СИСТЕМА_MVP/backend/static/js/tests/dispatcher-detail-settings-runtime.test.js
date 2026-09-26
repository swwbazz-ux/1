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
    path.resolve(BACKEND, "..", "..", ".github", "deploy", "production-files.txt"),
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
    assert.match(TEMPLATE, /dispatcher-detail-settings-v1\.js[^\n]+dispatcher-desktop-shell-v158/);

    const pwaMatches = PWA_SOURCE.match(/\/static\/js\/dispatcher-detail-settings-v1\.js/g) || [];
    assert.equal(pwaMatches.length, 1, "settings module must occur once in PWA CORE_ASSETS");

    const manifestPath = "СИСТЕМА_MVP/backend/static/js/dispatcher-detail-settings-v1.js";
    const manifestMatches = PRODUCTION_MANIFEST.split(/\r?\n/).filter((line) => line === manifestPath);
    assert.equal(manifestMatches.length, 1, "settings module must occur once in production manifest");
});
