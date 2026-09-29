"use strict";
/* Матрица без сети 30.09.2026: разгрузка рейса машиниста удержанием ставила на
   круг следующий экскаватор («ЭКС-1»), а отпускание пальца (сброс удержания)
   тут же возвращало подпись, запомненную при привязке экрана («ККД»). После
   записанной разгрузки сброс подпись не трогает. */
const test = require("node:test");
const assert = require("node:assert/strict");
const {driverScreenSource} = require("./driver-screen-source");

const SOURCE = driverScreenSource();

test("the hold reset restores the ready label only when no unload was submitted", () => {
    const start = SOURCE.indexOf("unloadHoldGuard = window.createDriverRoleHoldGuard({");
    assert.notEqual(start, -1);
    const block = SOURCE.slice(start, SOURCE.indexOf("window.driverUnloadHoldGuard = unloadHoldGuard;", start));
    assert.match(block, /onStart: function \(\) \{[\s\S]*?unloadHoldSubmitted = false;/);
    assert.match(block, /if \(!unloadHoldSubmitted && dialLabel && resetLabel/);
    assert.match(block, /if \(!submitDriverUnloadOnce\(\)\) \{\s*unloadHoldGuard\.cancel\(\);\s*return;\s*\}\s*unloadHoldSubmitted = true;/);
});
