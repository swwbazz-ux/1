"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const TEMPLATE_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "..", "templates", "users", "driver_shift.html"),
    "utf8"
);


function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${label} signature was not found.`);
    const open = source.indexOf("{", start + signature.length);
    assert.notEqual(open, -1, `${label} opening brace was not found.`);
    let depth = 0;
    for (let index = open; index < source.length; index += 1) {
        if (source[index] === "{") depth += 1;
        if (source[index] === "}") depth -= 1;
        if (depth === 0) return source.slice(start, index + 1);
    }
    assert.fail(`${label} closing brace was not found.`);
}


function createClassList() {
    const values = new Set();
    return {
        toggle(name, force) {
            if (force) values.add(name);
            else values.delete(name);
        },
        contains(name) {
            return values.has(name);
        },
    };
}


function loadController() {
    const source = extractBraceBlock(
        TEMPLATE_SOURCE,
        "function createDriverReportDeliveryController(options)",
        "Driver report delivery controller"
    );
    const context = {};
    vm.runInNewContext(`${source}\ncontext.factory = createDriverReportDeliveryController;`, {context});
    return context.factory;
}


function createFixture({copyFails = false} = {}) {
    const factory = loadController();
    const classList = createClassList();
    const button = {classList, dataset: {}, disabled: false};
    const title = {textContent: ""};
    const hint = {textContent: ""};
    const copied = [];
    const opened = [];
    const notices = [];
    let reportText = "Путёвка №1";
    let readonly = false;
    const controller = factory({
        button,
        title,
        hint,
        groupUrl: "https://max.ru/join/test-group",
        buildText() {
            return reportText;
        },
        copyText(text) {
            copied.push(text);
            return copyFails ? Promise.reject(new Error("copy_failed")) : Promise.resolve();
        },
        openGroup(url) {
            opened.push(url);
        },
        notify(message) {
            notices.push(message);
        },
        isReadonly() {
            return readonly;
        },
    });
    return {
        button,
        classList,
        copied,
        opened,
        notices,
        title,
        hint,
        controller,
        setReportText(value) {
            reportText = value;
        },
        setReadonly(value) {
            readonly = value;
        },
    };
}


test("Driver report uses one two-stage MAX delivery control with the configured group", () => {
    assert.match(TEMPLATE_SOURCE, /data-driver-report-delivery/);
    assert.match(TEMPLATE_SOURCE, /Подготовить путёвку/);
    assert.match(TEMPLATE_SOURCE, /Открыть группу/);
    assert.match(TEMPLATE_SOURCE, /https:\/\/max\.ru\/join\/haXmcD7Efa-2_dVX3_VLqfftNKU1QyMlnVTWgiQSDdE/);
    assert.doesNotMatch(TEMPLATE_SOURCE, /data-driver-report-share/);
    assert.doesNotMatch(TEMPLATE_SOURCE, /navigator\.share/);
    assert.match(TEMPLATE_SOURCE, /#471aff/);
    assert.match(TEMPLATE_SOURCE, /#9500ff/);
});


test("first tap copies the report and second tap opens MAX without copying twice", async () => {
    const fixture = createFixture();
    assert.equal(fixture.button.dataset.driverReportState, "prepare");
    assert.equal(fixture.title.textContent, "Подготовить путёвку");

    fixture.controller.handleClick();
    assert.equal(fixture.button.disabled, true);
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepEqual(fixture.copied, ["Путёвка №1"]);
    assert.equal(fixture.button.disabled, false);
    assert.equal(fixture.button.dataset.driverReportState, "max");
    assert.equal(fixture.classList.contains("is-max-ready"), true);
    assert.equal(fixture.title.textContent, "Открыть группу");
    assert.equal(fixture.hint.textContent, "текст уже скопирован");

    fixture.controller.handleClick();
    assert.deepEqual(fixture.copied, ["Путёвка №1"]);
    assert.deepEqual(fixture.opened, ["https://max.ru/join/test-group"]);
});


test("changed report cannot open MAX until the fresh text is copied", async () => {
    const fixture = createFixture();
    fixture.controller.handleClick();
    await new Promise((resolve) => setImmediate(resolve));
    fixture.setReportText("Путёвка №2");

    fixture.controller.handleClick();
    assert.deepEqual(fixture.opened, []);
    assert.equal(fixture.button.dataset.driverReportState, "prepare");
    assert.equal(fixture.notices.at(-1), "Данные изменились. Подготовьте путёвку снова");

    fixture.controller.handleClick();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(fixture.copied, ["Путёвка №1", "Путёвка №2"]);
    assert.equal(fixture.button.dataset.driverReportState, "max");
});


test("clipboard failure keeps the control in the safe prepare state", async () => {
    const fixture = createFixture({copyFails: true});
    fixture.controller.handleClick();
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(fixture.button.disabled, false);
    assert.equal(fixture.button.dataset.driverReportState, "prepare");
    assert.equal(fixture.classList.contains("is-max-ready"), false);
    assert.deepEqual(fixture.opened, []);
    assert.equal(fixture.notices.at(-1), "Не удалось скопировать путёвку");
});


test("copy completion does not re-enable delivery after the screen becomes read-only", async () => {
    const fixture = createFixture();
    fixture.controller.handleClick();
    fixture.setReadonly(true);
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(fixture.button.dataset.driverReportState, "max");
    assert.equal(fixture.button.disabled, true);
    assert.deepEqual(fixture.opened, []);
});
