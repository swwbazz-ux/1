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
const DISPATCHER_TRANSPORT = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-transport-v1.js"),
    "utf8"
);
const DISPATCHER_REALTIME = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-realtime-v1.js"),
    "utf8"
);
const SHARED_TEMPLATE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "dispatcher_control.html"),
    "utf8"
);
const TRIPS_VIEWS = fs.readFileSync(path.join(BACKEND, "trips", "views.py"), "utf8");
const PRODUCTION_MANIFEST = fs.readFileSync(
    path.resolve(BACKEND, "..", "..", ".github", "deploy", "production-files.txt"),
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

test("transport загружается перед основным runtime и входит в PWA shell", () => {
    const transportIndex = SHARED_TEMPLATE.indexOf("dispatcher-transport-v1.js");
    const realtimeIndex = SHARED_TEMPLATE.indexOf("dispatcher-realtime-v1.js");
    const controlIndex = SHARED_TEMPLATE.indexOf("dispatcher-control-v1.js");

    assert.ok(transportIndex >= 0, "transport script отсутствует в шаблоне");
    assert.ok(controlIndex > transportIndex, "transport должен загрузиться до основного runtime");
    assert.match(DISPATCHER_TRANSPORT, /global\.createDispatcherTransport = createDispatcherTransport;/);
    assert.ok(realtimeIndex > transportIndex, "realtime must load after transport");
    assert.ok(controlIndex > realtimeIndex, "realtime must load before the main runtime");
    assert.match(DISPATCHER_REALTIME, /global\.createDispatcherRealtime = createDispatcherRealtime;/);
    assert.match(DISPATCHER_RUNTIME, /window\.createDispatcherTransport\(\{/);
    assert.match(DISPATCHER_RUNTIME, /window\.createDispatcherRealtime\(\{/);
    assert.match(TRIPS_VIEWS, /"\/static\/js\/dispatcher-transport-v1\.js"/);
    assert.match(TRIPS_VIEWS, /"\/static\/js\/dispatcher-realtime-v1\.js"/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-transport-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-realtime-v1\.js/);
});

test("offline queue физически отделена, но сохраняет production-контракт", () => {
    assert.match(DISPATCHER_TRANSPORT, /mining-master-mobile-sync-queue-v3/);
    assert.match(DISPATCHER_TRANSPORT, /DISPATCHER_SYNC_REQUEST_TIMEOUT_MS = 12000/);
    assert.match(DISPATCHER_TRANSPORT, /payload\.client_action_id = "mm-"/);
    assert.doesNotMatch(DISPATCHER_RUNTIME, /function sendDispatcherSyncRequest/);
    assert.doesNotMatch(DISPATCHER_RUNTIME, /function enqueueDispatcherSyncRequest/);
});

test("realtime reconciliation is physically separated behind a narrow callback boundary", () => {
    assert.match(DISPATCHER_REALTIME, /function reconcileDispatcherDesktopBoard\(currentBoard, freshBoard\)/);
    assert.match(DISPATCHER_REALTIME, /function refreshDispatcherDesktopBoardFromServer\(refreshOptions\)/);
    assert.match(DISPATCHER_REALTIME, /function applyDispatcherOperationalStateRefresh\(context\)/);
    assert.doesNotMatch(DISPATCHER_RUNTIME, /function reconcileDispatcherDesktopBoard\(/);
    assert.doesNotMatch(DISPATCHER_RUNTIME, /function refreshDispatcherDesktopBoardFromServer\(/);
    assert.doesNotMatch(DISPATCHER_RUNTIME, /function applyDispatcherOperationalStateRefresh\(/);
    assert.match(DISPATCHER_RUNTIME, /bindBoardInteractions: bindDispatcherDesktopInteractions/);
    assert.match(DISPATCHER_RUNTIME, /refreshBoardIntegrity: refreshDesktopBoardIntegrity/);
});
