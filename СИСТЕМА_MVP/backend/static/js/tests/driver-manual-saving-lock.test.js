"use strict";
/* Стенд без сети 30.09.2026: после погрузки под свободный ковш завершение и
   отмена ручного рейса отвечали «Рейс ещё сохраняется, повторите» до
   перезапуска — признак «идёт запись на телефоне» (savingLocal) оставался
   навсегда. Запись в очередь занимает миллисекунды; признак старше 8 с больше
   не держит завершение, отмену и новую погрузку. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const SOURCE_PATH = path.resolve(__dirname, "..", "driver-manual-excavator-workspace-v1.js");
const engine = require(SOURCE_PATH);
const SOURCE = fs.readFileSync(SOURCE_PATH, "utf8");

test("a saving lock older than 8 s is stale, a fresh one is not", () => {
    assert.equal(engine.savingLockIsStale(0, 50000), false);
    assert.equal(engine.savingLockIsStale(10000, 17999), false);
    assert.equal(engine.savingLockIsStale(10000, 18000), true);
});

test("complete, cancel and start of a manual trip consult the stale-aware guard", () => {
    const body = (name) => {
        const start = SOURCE.indexOf("function " + name + "(");
        assert.notEqual(start, -1, name);
        return SOURCE.slice(start, SOURCE.indexOf("\n    }\n", start));
    };
    assert.match(body("completeManualLoad"), /savingBlocks\(\)\s*\|\| !projection/);
    assert.match(body("cancelManualLoad"), /savingBlocks\(\)\s*\|\| !projection/);
    assert.match(body("startManualLoad"), /sourceShouldBeLocked\(savingBlocks\(\), currentTripProjection\)/);
    assert.doesNotMatch(SOURCE, /[;{]\s*savingLocal = (true|false);/, "the lock is set only through setSavingLocal");
});
