"use strict";
/* Уровень виброотклика водителя: на вебе сила — это длительность, поэтому
   уровень масштабирует импульсы (не паузы) с нижней планкой; круг и барабан
   обязаны ходить через общий driverHaptic. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.resolve(__dirname, "../driver-haptics-v1.js"), "utf8");

function boot(stored) {
    const calls = [];
    const store = new Map(stored ? [["driver-haptic-level", stored]] : []);
    const buttons = ["weak", "normal", "strong"].map((level) => ({
        level, classes: new Set(), attrs: {},
        getAttribute(name) { return name === "data-driver-haptic-level" ? this.level : this.attrs[name]; },
        setAttribute(name, value) { this.attrs[name] = value; },
        classList: {toggle: (name, on) => { on ? buttons.find(b => b.level === level).classes.add(name) : buttons.find(b => b.level === level).classes.delete(name); }},
    }));
    const win = {
        localStorage: {getItem: (k) => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, v)},
        navigator: {vibrate(p) { calls.push(p); return true; }},
        addEventListener() {},
    };
    const doc = {readyState: "complete", addEventListener() {}, querySelectorAll: () => buttons};
    vm.runInNewContext(SOURCE, {window: win, document: doc, Array, Number, Math, String});
    return {win, calls, buttons};
}

test("default level is strong and scales pulses but not gaps", () => {
    const {win, calls} = boot();
    assert.equal(win.driverHaptics.getLevel(), "strong");
    win.driverHaptic([35, 45, 70]);
    assert.deepEqual(calls.at(-1), [63, 45, 126]);
    win.driverHaptic(14);
    assert.equal(calls.at(-1), 30, "короткий импульс поднимается до нижней планки уровня");
    win.driverHaptic(0);
    assert.equal(calls.at(-1), 0, "отмена вибрации остаётся отменой");
});

test("weak and normal levels keep their own factors and floors", () => {
    const weak = boot("weak");
    weak.win.driverHaptic(100);
    assert.equal(weak.calls.at(-1), 60);
    const normal = boot("normal");
    normal.win.driverHaptic(14);
    assert.equal(normal.calls.at(-1), 20);
});

test("choosing a level stores it, marks the button and plays a sample", () => {
    const {win, calls, buttons} = boot("weak");
    win.driverHaptics.setLevel("strong");
    assert.equal(win.driverHaptics.getLevel(), "strong");
    assert.ok(buttons.find(b => b.level === "strong").classes.has("is-active"));
    assert.ok(!buttons.find(b => b.level === "weak").classes.has("is-active"));
    assert.deepEqual([...calls.at(-1)], [198, 60, 198], "пробный импульс тоже масштабируется уровнем (массив из VM-контекста — сравниваем по значениям)");
    assert.equal(win.driverHaptics.setLevel("bogus"), "strong", "неизвестный уровень не принимается");
});

test("dial and drum route their haptics through the shared level", () => {
    const dial = fs.readFileSync(path.resolve(__dirname, "../driver-shift-v1.js"), "utf8");
    const drum = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");
    assert.match(dial, /if \(typeof window\.driverHaptic === "function"\) \{ window\.driverHaptic\(pattern\); return; \}/);
    assert.match(drum, /if \(typeof root\.driverHaptic === "function"\) \{ root\.driverHaptic\(pattern\); return; \}/);
    const template = fs.readFileSync(path.resolve(__dirname, "../../../templates/users/driver_shift.html"), "utf8");
    assert.match(template, /driver-haptics-v1\.js/);
    for (const level of ["weak", "normal", "strong"]) assert.match(template, new RegExp(`data-driver-haptic-level="${level}"`));
});
