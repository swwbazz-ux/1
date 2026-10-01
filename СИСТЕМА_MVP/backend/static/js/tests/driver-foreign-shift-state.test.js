"use strict";
/* Матрица без сети B3a (30.09.2026): экран нарисован сервером для прошлой
   смены. На новой местной смене серверный простой прошлой смены снимается
   (он закончился закрытием смены), свои простои «local:…» остаются, а
   гружёный рейс остаётся — он переходит к следующей смене (решение 13
   владельца). */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const SHIFT = fs.readFileSync(path.resolve(__dirname, "../driver-shift-v1.js"), "utf8");
const LOCAL = fs.readFileSync(path.resolve(__dirname, "../driver-local-shift-v1.js"), "utf8");

test("only a server downtime of another shift is dropped; the carryover trip stays", () => {
    const start = SHIFT.indexOf("window.driverDropForeignShiftState = function (target)");
    assert.notEqual(start, -1);
    const body = SHIFT.slice(start, SHIFT.indexOf("\n    };", start));
    assert.match(body, /driverShellShowsForeignShift\(current\)/);
    assert.match(body, /activeDowntimeId\.indexOf\("local:"\) === 0\) return false;/);
    assert.match(body, /clearDriverActiveDowntime\(\);/);
    assert.doesNotMatch(body, /showDriverTripGone|driverActiveTripId/, "a loaded trip carries over to the next shift");
    assert.match(SHIFT, /return String\(current\.dataset\.driverShiftId \|\| ""\) !== String\(current\.dataset\.driverServerShiftId \|\| ""\);/);
});

test("the local shift projection and the screen binding both drop a foreign downtime", () => {
    assert.match(LOCAL, /typeof root\.driverDropForeignShiftState === "function"[\s\S]{0,80}root\.driverDropForeignShiftState\(shell\);/);
    assert.match(SHIFT, /var driverOfflineOutbox = createDriverOfflineRuntime\(\);\s*\/\/[^\n]*\n\s*window\.driverDropForeignShiftState\(shell\);/);
});
