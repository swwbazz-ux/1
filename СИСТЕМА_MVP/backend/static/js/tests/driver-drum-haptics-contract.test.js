"use strict";
/* Барабан простоев 20.09.2026: пользователь не чувствовал ни одного щелчка, хотя
   код звал вибрацию — 18 мс мотор Xiaomi не отрабатывает, а в обработчике
   resize и в rebuildDrum падал ReferenceError на необъявленном `slot`. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");

test("drum never assigns an undeclared `slot`", () => {
    assert.doesNotMatch(SOURCE, /\bslot\s*=/, "необъявленный slot бросает ReferenceError в strict mode");
});

test("drum haptics are long enough to be felt", () => {
    const pulses = [...SOURCE.matchAll(/haptic\((\d+)\)/g)].map((m) => Number(m[1]));
    assert.ok(pulses.length >= 4, "щелчки барабана на месте");
    for (const ms of pulses) assert.ok(ms >= 20, `импульс ${ms} мс слишком короткий для мотора телефона`);
    const front = SOURCE.match(/if \(lastFront !== -1\) \{ haptic\((\d+)\);/);
    assert.ok(front && Number(front[1]) >= 30, "щелчок смены карточки — от 30 мс");
    const patterns = [...SOURCE.matchAll(/haptic\(\[([\d,\s]+)\]\)/g)].map((m) => m[1].split(",").map(Number));
    assert.ok(patterns.length >= 3, "старт, стоп и отказ имеют свои рисунки");
    for (const pattern of patterns) assert.ok(Math.max(...pattern) >= 45, `рисунок ${pattern} без длинного импульса`);
});

test("drum script parses and installs without touching the DOM", () => {
    const listeners = [];
    const doc = {body: {dataset: {}}, addEventListener: (t, f) => listeners.push(t), querySelector: () => null, querySelectorAll: () => []};
    const win = {document: doc, addEventListener: (t) => listeners.push(t), navigator: {}, setTimeout: () => 0, requestAnimationFrame: () => 0, MutationObserver: function () { this.observe = () => {}; }};
    win.window = win;
    vm.runInNewContext(SOURCE, {window: win, document: doc, MutationObserver: win.MutationObserver, console});
    assert.ok(listeners.length >= 1);
});
