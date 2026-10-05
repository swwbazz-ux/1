"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const {dispatcherScreenSource} = require("./dispatcher-screen-source");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const CHARTS_PATH = path.join(BACKEND, "static", "js", "dispatcher-detail-charts-v1.js");
const DETAIL_PATH = path.join(BACKEND, "static", "js", "dispatcher-detail-v1.js");
const CHARTS_SOURCE = fs.readFileSync(CHARTS_PATH, "utf8");
const DETAIL_SOURCE = fs.readFileSync(DETAIL_PATH, "utf8");
const TEMPLATE = dispatcherScreenSource();
const PWA_SOURCE = fs.readFileSync(path.join(BACKEND, "trips", "dispatcher_pwa.py"), "utf8");
const PRODUCTION_MANIFEST = fs.readFileSync(
    path.resolve(BACKEND, "..", "..", ".github", "deploy", "production-files.txt"),
    "utf8"
);

function fakeElement(tagName) {
    return {
        tagName: String(tagName || "div").toUpperCase(),
        className: "",
        textContent: "",
        innerHTML: "",
        hidden: false,
        children: [],
        listeners: new Map(),
        style: {
            values: new Map(),
            setProperty(name, value) {
                this.values.set(name, value);
            },
        },
        classList: {
            add() {},
            remove() {},
        },
        appendChild(child) {
            this.children.push(child);
            return child;
        },
        addEventListener(name, handler) {
            this.listeners.set(name, handler);
        },
        querySelector() {
            return null;
        },
        querySelectorAll() {
            return [];
        },
    };
}

function loadChartsRuntime() {
    const document = {
        createElement(tagName) {
            return fakeElement(tagName);
        },
    };
    const window = {};
    vm.runInNewContext(CHARTS_SOURCE, {window, document}, {filename: CHARTS_PATH});
    return {window, document};
}

test("detail chart presenter renders metrics, tabs and a bar chart without the main card runtime", () => {
    const {window} = loadChartsRuntime();
    const nodes = {
        detailLayer: fakeElement("section"),
        detailShiftReport: fakeElement("section"),
        detailMetrics: fakeElement("div"),
        detailTabs: fakeElement("div"),
        detailDashboard: fakeElement("div"),
    };
    const charts = window.createDispatcherDetailCharts(nodes);

    charts.renderShiftReport({
        metrics: [{label: "Рейсы", value: "4"}],
        charts: [{
            type: "bar",
            title: "По направлениям",
            rows: [{label: "Склад <1>", value: "25 м³", meta: "1 рейс", percent: 25}],
        }],
    });

    assert.equal(nodes.detailMetrics.children.length, 1);
    assert.equal(nodes.detailMetrics.children[0].children[0].textContent, "Рейсы");
    assert.equal(nodes.detailTabs.children.length, 1);
    assert.equal(nodes.detailTabs.children[0].textContent, "По направлениям");
    assert.equal(nodes.detailDashboard.children.length, 1);
    assert.match(nodes.detailDashboard.children[0].className, /gd-detail-chart-bar/);
    assert.equal(nodes.detailShiftReport.hidden, false);
});

test("chart code has one owner and the main detail runtime delegates to it", () => {
    assert.match(CHARTS_SOURCE, /function buildDetailChartShell\(chart\)/);
    assert.match(CHARTS_SOURCE, /function renderDetailChart\(chart\)/);
    assert.match(CHARTS_SOURCE, /function renderDetailShiftReport\(report\)/);
    assert.match(DETAIL_SOURCE, /global\.createDispatcherDetailCharts\(\{/);
    assert.match(DETAIL_SOURCE, /detailCharts\.renderShiftReport\(report\);/);
    assert.doesNotMatch(DETAIL_SOURCE, /function buildDetailChartShell\(chart\)/);
    assert.doesNotMatch(DETAIL_SOURCE, /function renderDetailChart\(chart\)/);
});

test("chart module is loaded before detail and packaged exactly once", () => {
    const chartIndex = TEMPLATE.indexOf("dispatcher-detail-charts-v1.js");
    const detailIndex = TEMPLATE.indexOf("dispatcher-detail-v1.js");
    assert.ok(chartIndex >= 0, "chart module is absent from the dispatcher template");
    assert.ok(detailIndex > chartIndex, "chart module must load before the detail runtime");
    assert.match(TEMPLATE, /dispatcher-detail-charts-v1\.js[^\n]+dispatcher-desktop-shell-v175/);

    const pwaMatches = PWA_SOURCE.match(/\/static\/js\/dispatcher-detail-charts-v1\.js/g) || [];
    assert.equal(pwaMatches.length, 1, "chart module must occur once in PWA CORE_ASSETS");

    const manifestPath = "СИСТЕМА_MVP/backend/static/js/dispatcher-detail-charts-v1.js";
    const manifestMatches = PRODUCTION_MANIFEST.split(/\r?\n/).filter((line) => line === manifestPath);
    assert.equal(manifestMatches.length, 1, "chart module must occur once in production manifest");
});
