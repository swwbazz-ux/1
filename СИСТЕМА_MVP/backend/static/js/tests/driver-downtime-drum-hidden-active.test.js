"use strict";
/* После простоя по причине вне быстрого набора её временная грань прячется,
   а пометка «идёт простой» на ней оставалась: cards() берёт только видимые
   грани. Ручная погрузка (driver-point-drum-v1.js, blockingDowntime) находила
   спрятанную «активную» грань и отказывала «Сначала завершите простой». С сетью
   это маскировала перерисовка сервером, без сети — держало до перезапуска
   (стенд, 30.09.2026). */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const DRUM = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");
const POINT_DRUM = fs.readFileSync(path.resolve(__dirname, "../driver-point-drum-v1.js"), "utf8");

test("the active-downtime mark is removed from drum cards hidden after the downtime ends", () => {
    const syncActive = DRUM.slice(DRUM.indexOf("function syncActive()"));
    assert.match(
        syncActive,
        /allCards\(\)\.forEach\(function \(card\) \{\s*if \(card\.hidden\) card\.classList\.remove\("is-active-downtime"\);\s*\}\);\s*cards\(\)\.forEach/
    );
    // С v376 простой погрузку вообще не держит — погрузка закрывает его сама.
    assert.doesNotMatch(POINT_DRUM, /toast\("Сначала завершите простой"\)/);
});
