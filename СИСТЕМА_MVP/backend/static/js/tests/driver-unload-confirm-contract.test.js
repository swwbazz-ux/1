"use strict";
/* Удержание разгрузки (владелец, 28.09.2026): кольцо заполнялось только наполовину —
   CSS набирал его за 1000 мс (две половины по 500), а действие срабатывало через
   holdMs: 500. Сразу после срабатывания проекция перестраивала круг в «ЭКС-1 / НА
   ЗАГРУЗКУ», голос звучал только по ответу сервера (иногда через секунды) — было
   непонятно, засчитался ли рейс. Теперь: кольцо полное ровно в миг срабатывания,
   затем ~0,9 с «засчитано» (галочка, «РАЗГРУЖЕНО», голос), и только потом новое
   состояние; перерисовка и подмена фрагмента в это окно ждут. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = (relative) => fs.readFileSync(path.resolve(__dirname, relative), "utf8").replace(/\r\n/g, "\n");
const CSS = read("../../css/driver-shift-v1.css");
const SHIFT = read("../driver-shift-v1.js");
const VOICE = read("../driver-shift-voice-v1.js");
const DRUM = read("../driver-point-drum-v1.js");

function ms(value, unit) {
    return unit === "s" ? Number(value) * 1000 : Number(value);
}

test("hold ring fills completely exactly when the unload hold fires", () => {
    const holdMs = Number(SHIFT.match(/unloadHoldGuard = window\.createDriverRoleHoldGuard\(\{[\s\S]*?holdMs: (\d+)/)[1]);
    const right = CSS.match(/is-holding \.driver-work-hold-half\.is-right \.driver-work-hold-fill \{\s*animation: driver-hold-right ([\d.]+)(m?s) steps/);
    const left = CSS.match(/is-holding \.driver-work-hold-half\.is-left \.driver-work-hold-fill \{\s*animation: driver-hold-left ([\d.]+)(m?s) steps\(6, end\) ([\d.]+)(m?s)/);
    assert.ok(right && left, "both ring halves are animated");
    const rightMs = ms(right[1], right[2]);
    const leftMs = ms(left[1], left[2]);
    const leftDelay = ms(left[3], left[4]);
    assert.equal(leftDelay, rightMs, "left half starts when the right half is full");
    assert.equal(rightMs + leftMs, holdMs, "ring completes at the same instant the action fires");
    // Полное кольцо держится и во время «засчитано».
    assert.match(CSS, /\.is-confirmed \.driver-work-hold-half\.is-left \.driver-work-hold-fill \{ transform: rotate\(180deg\); \}/);
});

test("a saved unload shows a timed confirmation with voice before the next state", () => {
    const helper = SHIFT.match(/function showDriverDialConfirmed\(\) \{[\s\S]*?\n        \}\n/)[0];
    assert.match(helper, /classList\.add\("is-confirmed"\)/);
    assert.match(helper, /"РАЗГРУЖЕНО"/);
    assert.match(helper, /playDriverVoice\("action_ok", "voice_trip_finished"\)/);
    assert.match(helper, /driverDialConfirmUntil = Date\.now\(\) \+ DRIVER_DIAL_CONFIRM_MS/);
    // По окончании показа — проекция и ручной барабан переводят круг дальше.
    assert.match(helper, /setTimeout\(function \(\) \{[\s\S]*?driverDialConfirmUntil = 0;[\s\S]*?applyDriverOfflineProjection\(current, driverOfflineEvents\)[\s\S]*?DriverPointDrum\.refresh\(\)/);
    const confirmMs = Number(SHIFT.match(/var DRIVER_DIAL_CONFIRM_MS = (\d+);/)[1]);
    assert.ok(confirmMs >= 800 && confirmMs <= 1000, "confirmation lasts ~0.8–1 s");
    // Показ — только после записи на телефоне, и для обычной, и для ручной разгрузки.
    assert.match(SHIFT, /unloadRecovery\.recover\(\{type: "queued"\}\);[\s\S]*?showDriverDialConfirmed\(\);\s*applyDriverOfflineProjection\(shell, driverOfflineEvents\);/);
    assert.match(DRUM, /if \(!saved\) \{[^\n]*return; \}\s*if \(typeof onSaved === "function"\) onSaved\(\);/);
});

test("nothing repaints the dial while the confirmation is shown", () => {
    assert.match(SHIFT, /function applyDriverOfflineProjection\(current, events\) \{\s*if \(!current\) return;[\s\S]{0,300}?if \(Number\(window\.driverDialConfirmUntil \|\| 0\) > Date\.now\(\)\) return;/);
    assert.match(VOICE, /Number\(window\.driverDialConfirmUntil \|\| 0\) > Date\.now\(\)\s*\) return busy\("dial_confirmed"\);/);
    assert.match(DRUM, /function syncDial\(\) \{[\s\S]{0,400}?if \(button\.classList\.contains\("is-confirmed"\)\) return;/);
});

test("the completion buzz is not cancelled by the post-hold reset", () => {
    assert.match(SHIFT, /onReset: function \(\) \{\s*stopHoldSegmentFeedback\(\);\s*if \(!unloadHoldCompleted\) driverVibrate\(0\);/);
    assert.match(SHIFT, /unloadHoldCompleted = true;\s*driverVibrate\(160\);/);
});

test("server confirmation no longer repeats the trip-finished voice", () => {
    const confirmed = SHIFT.match(/onConfirmed: function \(\) \{[\s\S]*?var result = args\[1\]/)[0];
    assert.doesNotMatch(confirmed, /voice_trip_finished/);
});
