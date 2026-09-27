"use strict";
/* Удержание разгрузки (владелец, 28.09.2026): кольцо заполнялось только наполовину —
   CSS набирал его за 1000 мс (две половины по 500), а действие срабатывало через
   holdMs: 500. Сразу после срабатывания проекция перестраивала круг в «ЭКС-1 / НА
   ЗАГРУЗКУ», голос звучал только по ответу сервера (иногда через секунды) — было
   непонятно, засчитался ли рейс. Теперь: кольцо полное ровно в миг срабатывания,
   затем ~5 с поверх круга лежит отдельный слой «засчитано» (кольцо со вспышкой,
   галочка прорисовывается, «РАЗГРУЖЕНО», ореол, затухание), голос сразу. Слой
   лежит на body и берёт прямоугольник круга: сам круг не меняется ни на пиксель,
   под слоем экран живёт как обычно (проекция, очередь, сверка ничего не ждут),
   касание закрывает показ досрочно. */
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
});

test("a saved unload shows the confirmation layer with voice, for the hold and the manual dial alike", () => {
    const helper = SHIFT.match(/function showDriverDialConfirmed\(\) \{[\s\S]*?\n        \}\n/)[0];
    assert.match(helper, /classList\.add\("is-showing"\)/);
    assert.match(helper, /placeDriverDialConfirmLayer\(\)/);
    assert.match(helper, /playDriverVoice\("action_ok", "voice_trip_finished"\)/);
    // Касание закрывает досрочно; слой сам касаний не ловит.
    assert.match(helper, /document\.addEventListener\("pointerdown", dismissDriverDialConfirmed, true\)/);
    const total = Number(SHIFT.match(/var DRIVER_DIAL_CONFIRM_MS = (\d+);/)[1]);
    const fade = Number(SHIFT.match(/var DRIVER_DIAL_CONFIRM_FADE_MS = (\d+);/)[1]);
    assert.ok(total >= 4500 && total <= 5500, "confirmation lasts ~5 s");
    assert.ok(fade >= 300 && fade <= 600, "it fades out, not snaps");
    assert.match(helper, /DRIVER_DIAL_CONFIRM_MS - DRIVER_DIAL_CONFIRM_FADE_MS/);
    // Слой создаётся на body и содержит ореол, галочку и подпись — и ничего снаружи
    // сердцевины: ни кольца на месте кольца удержания, ни свечения вокруг него.
    const layer = SHIFT.match(/function driverDialConfirmLayer\(\) \{[\s\S]*?\n        \}\n/)[0];
    assert.match(layer, /document\.body\.appendChild\(layer\)/);
    ["driver-work-confirm-halo", "driver-work-confirm-check", "driver-work-confirm-text"].forEach((cls) => {
        assert.match(layer, new RegExp(cls));
    });
    assert.doesNotMatch(layer, /confirm-ring/);
    assert.doesNotMatch(CSS, /driver-work-confirm-ring/);
    // Прямоугольник слоя — прямоугольник сердцевины, не всей кнопки.
    const place = SHIFT.match(/function placeDriverDialConfirmLayer\(\) \{[\s\S]*?\n        \}\n/)[0];
    assert.match(place, /querySelector\("\.driver-work-dial-core"\)/);
    assert.match(place, /var rect = core\.getBoundingClientRect\(\)/);
    assert.match(layer, /pathLength="100"/);
    assert.match(layer, /РАЗГРУЖЕНО/);
    // Показ — только после записи на телефоне, и для обычной, и для ручной разгрузки.
    assert.match(SHIFT, /unloadRecovery\.recover\(\{type: "queued"\}\);[\s\S]*?showDriverDialConfirmed\(\);\s*applyDriverOfflineProjection\(shell, driverOfflineEvents\);/);
    assert.match(SHIFT, /completeFromDial\(showDriverDialConfirmed\)/);
    assert.match(DRUM, /if \(!saved\) \{[^\n]*return; \}\s*if \(typeof onSaved === "function"\) onSaved\(\);/);
});

test("the layer never touches the dial: geometry under it is untouched and nothing waits for it", () => {
    // Никаких правил «засчитано» на самой кнопке, кольце или сердцевине.
    assert.doesNotMatch(CSS, /\.driver-work-dial-button\.is-confirmed/);
    assert.doesNotMatch(SHIFT, /classList\.add\("is-confirmed"\)/);
    // Проекция, сверка и барабан точек показ не ждут.
    assert.doesNotMatch(SHIFT, /driverDialConfirmUntil/);
    assert.doesNotMatch(VOICE, /dial_confirmed/);
    assert.doesNotMatch(DRUM, /is-confirmed/);
    // Слой: fixed на body, вне потока, касаний не ловит, размеры в cqw от круга.
    const layerRule = CSS.match(/\.driver-work-confirm \{([\s\S]*?)\}/)[1];
    assert.match(layerRule, /position: fixed;/);
    assert.match(layerRule, /pointer-events: none;/);
    assert.match(layerRule, /container-type: size;/);
    assert.match(layerRule, /visibility: hidden;/);
    // Анимируются только прозрачность и штрих галочки; ни фильтров, ни теней в кадрах.
    const keyframes = CSS.match(/@keyframes driver-confirm-[\s\S]*?\}\n\}/g) || [];
    assert.ok(keyframes.length >= 3, "fade-in, flash and draw keyframes exist");
    keyframes.forEach((block) => {
        assert.doesNotMatch(block, /filter|box-shadow|background|width|height|inset|border/);
        assert.match(block, /opacity|stroke-dashoffset/);
    });
    const draw = CSS.match(/\.driver-work-confirm\.is-showing \.driver-work-confirm-check path \{\s*animation: driver-confirm-draw ([\d.]+)ms/);
    assert.ok(draw && Number(draw[1]) >= 250 && Number(draw[1]) <= 400, "check mark draws in ~300 ms");
    // Только объявления, без комментариев: там filter упоминается как отвергнутый вариант.
    const overlayRules = CSS.match(/\.driver-work-confirm[\s\S]*?@keyframes driver-confirm-fade-in/)[0]
        .replace(/\/\*[\s\S]*?\*\//g, "");
    assert.doesNotMatch(overlayRules, /filter:/);
});

test("the completion buzz is not cancelled by the post-hold reset", () => {
    assert.match(SHIFT, /onReset: function \(\) \{\s*stopHoldSegmentFeedback\(\);\s*if \(!unloadHoldCompleted\) driverVibrate\(0\);/);
    assert.match(SHIFT, /unloadHoldCompleted = true;\s*driverVibrate\(160\);/);
});

test("server confirmation no longer repeats the trip-finished voice", () => {
    const confirmed = SHIFT.match(/onConfirmed: function \(\) \{[\s\S]*?var result = args\[1\]/)[0];
    assert.doesNotMatch(confirmed, /voice_trip_finished/);
});
