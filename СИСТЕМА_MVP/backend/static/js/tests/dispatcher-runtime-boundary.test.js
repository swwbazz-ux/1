"use strict";

/* Граница desktop-пульта и мобильного Горного мастера. Пульты пока делят
   Django-шаблон, но браузер диспетчера не должен повторно загружать мобильный
   runtime: это увеличивает файл и связывает независимые рабочие места. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const DISPATCHER_RUNTIME = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-control-v1.js"),
    "utf8"
);
const SHARED_TEMPLATE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "dispatcher_control.html"),
    "utf8"
);

test("desktop runtime содержит только контур Диспетчера", () => {
    for (const mobileContract of [
        "function bindMiningMasterMobileScreens()",
        "function refreshMobileBoardFromServer(options)",
        "window.MiningMasterPwaUpdates",
        "isMiningMasterMobilePage",
        "miningMasterRealtimeHardLagLimit",
    ]) {
        assert.equal(
            DISPATCHER_RUNTIME.includes(mobileContract),
            false,
            `мобильный контракт не должен попадать в desktop runtime: ${mobileContract}`
        );
    }
    assert.match(DISPATCHER_RUNTIME, /window\.DispatcherSyncDebug = \{/);
});

test("мобильный контур Горного мастера сохранён в своей ветке шаблона", () => {
    assert.match(SHARED_TEMPLATE, /{% if mining_master_mobile_enabled %}\s*<script>/);
    assert.match(SHARED_TEMPLATE, /function bindMiningMasterMobileScreens\(\)/);
    assert.match(SHARED_TEMPLATE, /function refreshMobileBoardFromServer\(options\)/);
    assert.match(SHARED_TEMPLATE, /window\.MiningMasterPwaUpdates = \{/);
});

test("desktop runtime размечен стабильными функциональными секциями", () => {
    for (let section = 1; section <= 7; section += 1) {
        assert.match(DISPATCHER_RUNTIME, new RegExp(`// ${section}\\.`));
    }
});
