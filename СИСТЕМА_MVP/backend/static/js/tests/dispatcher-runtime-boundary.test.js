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
const DISPATCHER_DETAIL = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-detail-v1.js"),
    "utf8"
);
const DISPATCHER_DETAIL_SETTINGS = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-detail-settings-v1.js"),
    "utf8"
);
const DISPATCHER_DETAIL_CHARTS = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-detail-charts-v1.js"),
    "utf8"
);
const DISPATCHER_EQUIPMENT_SEARCH = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-equipment-search-v1.js"),
    "utf8"
);
const DISPATCHER_COMPLEX_TRUCK_RACKS = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-complex-truck-racks-v1.js"),
    "utf8"
);
const DISPATCHER_HAUL_ASSIGNMENT_STATE = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-haul-assignment-state-v1.js"),
    "utf8"
);
const DISPATCHER_BOARD_DND = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-board-dnd-v1.js"),
    "utf8"
);
const DISPATCHER_BOARD_MUTATIONS = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-board-mutations-v1.js"),
    "utf8"
);
const DISPATCHER_BOARD = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-board-v1.js"),
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
const DISPATCHER_BOARD_TEMPLATE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "includes", "dispatcher_board.html"),
    "utf8"
);
const DISPATCHER_SERVICE_LISTS = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "includes", "dispatcher_service_lists.html"),
    "utf8"
);
const DISPATCHER_EQUIPMENT_DETAIL = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "includes", "dispatcher_equipment_detail.html"),
    "utf8"
);
const DISPATCHER_PUSH_INVITE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "includes", "dispatcher_push_invite.html"),
    "utf8"
);
const DISPATCHER_PWA = fs.readFileSync(
    path.join(BACKEND, "trips", "dispatcher_pwa.py"),
    "utf8"
);
const PRODUCTION_MANIFEST = fs.readFileSync(
    path.resolve(BACKEND, "..", "..", ".github", "deploy", "production-files.txt"),
    "utf8"
);

test("desktop runtime содержит только контур Диспетчера", () => {
    const desktopModules = [DISPATCHER_RUNTIME, DISPATCHER_TRANSPORT, DISPATCHER_DETAIL_SETTINGS, DISPATCHER_DETAIL_CHARTS, DISPATCHER_DETAIL, DISPATCHER_EQUIPMENT_SEARCH, DISPATCHER_COMPLEX_TRUCK_RACKS, DISPATCHER_HAUL_ASSIGNMENT_STATE, DISPATCHER_BOARD_DND, DISPATCHER_BOARD, DISPATCHER_REALTIME].join("\n");
    for (const mobileContract of [
        "function bindMiningMasterMobileScreens()",
        "function refreshMobileBoardFromServer(options)",
        "window.MiningMasterPwaUpdates",
        "isMiningMasterMobilePage",
        "miningMasterRealtimeHardLagLimit",
    ]) {
        assert.equal(
            desktopModules.includes(mobileContract),
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

test("серверная доска живёт в отдельном упакованном include", () => {
    assert.match(
        SHARED_TEMPLATE,
        /{% include "trips\/includes\/dispatcher_board\.html" %}/
    );
    assert.doesNotMatch(SHARED_TEMPLATE, /<section class="dispatcher-board/);
    assert.match(DISPATCHER_BOARD_TEMPLATE, /^{% load static %}/);
    assert.match(DISPATCHER_BOARD_TEMPLATE, /<section class="dispatcher-board/);
    assert.match(DISPATCHER_BOARD_TEMPLATE, /includes\/dispatcher_header\.html/);
    assert.match(DISPATCHER_BOARD_TEMPLATE, /data-dispatcher-excavator-garage/);
    assert.match(DISPATCHER_BOARD_TEMPLATE, /data-dispatcher-drop="complex"/);
    assert.match(DISPATCHER_BOARD_TEMPLATE, /data-dispatcher-drop="truck-garage"/);
    assert.doesNotMatch(DISPATCHER_BOARD_TEMPLATE, /class="mm-mobile-shell/);
    assert.doesNotMatch(DISPATCHER_BOARD_TEMPLATE, /data-mm-mobile-/);
    assert.match(
        PRODUCTION_MANIFEST,
        /templates\/trips\/includes\/dispatcher_board\.html/
    );
});

test("скрытые служебные списки живут в отдельном упакованном include", () => {
    assert.match(
        SHARED_TEMPLATE,
        /{% include "trips\/includes\/dispatcher_service_lists\.html" %}/
    );
    assert.doesNotMatch(SHARED_TEMPLATE, /class="dispatcher-tools"/);
    assert.match(DISPATCHER_SERVICE_LISTS, /class="dispatcher-tools"[^>]*hidden/);
    assert.match(DISPATCHER_SERVICE_LISTS, /class="dispatcher-filters"/);
    assert.match(DISPATCHER_SERVICE_LISTS, /dispatcher_complete_trip/);
    assert.match(DISPATCHER_SERVICE_LISTS, /dispatcher_cancel_assignment/);
    assert.match(DISPATCHER_SERVICE_LISTS, /dispatcher_service_close_shift/);
    assert.doesNotMatch(DISPATCHER_SERVICE_LISTS, /data-gd-equipment-detail/);
    assert.match(
        PRODUCTION_MANIFEST,
        /templates\/trips\/includes\/dispatcher_service_lists\.html/
    );
});

test("detail-карточка живёт в отдельном упакованном include", () => {
    assert.match(
        SHARED_TEMPLATE,
        /{% include "trips\/includes\/dispatcher_equipment_detail\.html" %}/
    );
    assert.doesNotMatch(
        SHARED_TEMPLATE,
        /<div class="gd-equipment-detail mm-equipment-detail"/
    );
    assert.match(
        DISPATCHER_EQUIPMENT_DETAIL,
        /class="gd-equipment-detail mm-equipment-detail"[^>]*data-gd-equipment-detail[^>]*hidden/
    );
    assert.match(DISPATCHER_EQUIPMENT_DETAIL, /data-gd-detail-service-close/);
    assert.match(DISPATCHER_EQUIPMENT_DETAIL, /data-gd-detail-downtime-close/);
    assert.match(DISPATCHER_EQUIPMENT_DETAIL, /data-gd-detail-settings/);
    assert.match(DISPATCHER_EQUIPMENT_DETAIL, /data-gd-detail-manual-trip-form/);
    assert.doesNotMatch(DISPATCHER_EQUIPMENT_DETAIL, /class="dispatcher-push-invite"/);
    assert.match(
        PRODUCTION_MANIFEST,
        /templates\/trips\/includes\/dispatcher_equipment_detail\.html/
    );
});

test("desktop-приглашение уведомлений живёт в отдельном упакованном include", () => {
    assert.match(
        SHARED_TEMPLATE,
        /{% if not mining_master_mobile_enabled %}\s*{% include "trips\/includes\/dispatcher_push_invite\.html" %}\s*{% endif %}/
    );
    assert.doesNotMatch(SHARED_TEMPLATE, /class="dispatcher-push-invite"/);
    assert.match(DISPATCHER_PUSH_INVITE, /class="dispatcher-push-invite"/);
    assert.match(DISPATCHER_PUSH_INVITE, /data-app-name="Диспетчер"/);
    assert.doesNotMatch(DISPATCHER_PUSH_INVITE, /data-mm-/);
    assert.match(
        PRODUCTION_MANIFEST,
        /templates\/trips\/includes\/dispatcher_push_invite\.html/
    );
});

test("карточка и desktop-доска физически отделены от оркестратора", () => {
    assert.match(DISPATCHER_DETAIL_SETTINGS, /function createDispatcherDetailSettings\(options\)/);
    assert.match(DISPATCHER_DETAIL_SETTINGS, /global\.createDispatcherDetailSettings = createDispatcherDetailSettings;/);
    assert.match(DISPATCHER_DETAIL_CHARTS, /function createDispatcherDetailCharts\(options\)/);
    assert.match(DISPATCHER_DETAIL_CHARTS, /global\.createDispatcherDetailCharts = createDispatcherDetailCharts;/);
    assert.match(DISPATCHER_DETAIL_CHARTS, /function renderDetailChart\(chart\)/);
    assert.match(DISPATCHER_DETAIL, /function createDispatcherDetail\(options\)/);
    assert.match(DISPATCHER_DETAIL, /global\.createDispatcherDetail = createDispatcherDetail;/);
    assert.match(DISPATCHER_DETAIL, /function openEquipmentCard\(cardId, trigger\)/);
    assert.match(DISPATCHER_DETAIL, /global\.createDispatcherDetailSettings\(\{/);
    assert.match(DISPATCHER_DETAIL, /global\.createDispatcherDetailCharts\(\{/);
    assert.doesNotMatch(DISPATCHER_DETAIL, /function saveDetailSettings\(\)/);
    assert.doesNotMatch(DISPATCHER_DETAIL, /function addDetailDestinationRow\(destination\)/);
    assert.doesNotMatch(DISPATCHER_DETAIL, /function buildDetailChartShell\(chart\)/);
    assert.doesNotMatch(DISPATCHER_DETAIL, /function renderDetailChart\(chart\)/);
    assert.match(DISPATCHER_EQUIPMENT_SEARCH, /function createDispatcherEquipmentSearch\(\)/);
    assert.match(DISPATCHER_EQUIPMENT_SEARCH, /global\.createDispatcherEquipmentSearch = createDispatcherEquipmentSearch;/);
    assert.match(DISPATCHER_COMPLEX_TRUCK_RACKS, /function createDispatcherComplexTruckRacks\(options\)/);
    assert.match(DISPATCHER_COMPLEX_TRUCK_RACKS, /global\.createDispatcherComplexTruckRacks = createDispatcherComplexTruckRacks;/);
    assert.match(DISPATCHER_COMPLEX_TRUCK_RACKS, /function refreshAll\(\)/);
    assert.match(DISPATCHER_HAUL_ASSIGNMENT_STATE, /function createDispatcherHaulAssignmentState\(\)/);
    assert.match(DISPATCHER_HAUL_ASSIGNMENT_STATE, /global\.createDispatcherHaulAssignmentState = createDispatcherHaulAssignmentState;/);
    assert.doesNotMatch(DISPATCHER_HAUL_ASSIGNMENT_STATE, /dispatcherPost|dragstart|refreshBoardFromServer/);
    assert.match(DISPATCHER_BOARD_DND, /function createDispatcherBoardDnD\(options\)/);
    assert.match(DISPATCHER_BOARD_DND, /global\.createDispatcherBoardDnD = createDispatcherBoardDnD;/);
    assert.match(DISPATCHER_BOARD_DND, /function resetSession\(\)/);
    assert.match(DISPATCHER_BOARD_DND, /function bindDispatcherComplexDrop\(zone\)/);
    assert.doesNotMatch(DISPATCHER_BOARD_DND, /moveDesktopTruckToComplex|refreshDispatcherDesktopBoardFromServer/);
    assert.doesNotMatch(DISPATCHER_BOARD, /function complexTileForGrid\(/);
    assert.doesNotMatch(DISPATCHER_BOARD, /function watchComplexTruckRacks\(/);
    assert.doesNotMatch(DISPATCHER_BOARD, /function bindDispatcherEquipmentSearch\(\)/);
    assert.doesNotMatch(DISPATCHER_BOARD, /function haulAssignmentStateId\(/);
    assert.doesNotMatch(DISPATCHER_BOARD, /function bindDispatcherComplexDrop\(zone\)/);
    assert.doesNotMatch(DISPATCHER_BOARD, /function bindDispatcherTruckGarageDrop\(garage\)/);
    assert.match(DISPATCHER_BOARD, /function createDispatcherBoard\(options\)/);
    assert.match(DISPATCHER_BOARD, /global\.createDispatcherBoard = createDispatcherBoard;/);
    assert.match(DISPATCHER_BOARD, /function bindDispatcherDesktopInteractions\(\)/);
    assert.doesNotMatch(DISPATCHER_RUNTIME, /function openEquipmentCard\(/);
    assert.doesNotMatch(DISPATCHER_RUNTIME, /function bindDispatcherDesktopInteractions\(/);
    assert.match(DISPATCHER_RUNTIME, /window\.createDispatcherDetail\(\{/);
    assert.match(DISPATCHER_RUNTIME, /window\.createDispatcherComplexTruckRacks\(\{/);
    assert.match(DISPATCHER_RUNTIME, /window\.createDispatcherHaulAssignmentState\(\)/);
    assert.match(DISPATCHER_RUNTIME, /window\.createDispatcherBoardDnD/);
    assert.match(DISPATCHER_RUNTIME, /window\.createDispatcherBoard\(\{/);
    assert.match(DISPATCHER_RUNTIME, /complexTruckRacks: dispatcherComplexTruckRacks/);
    assert.match(DISPATCHER_RUNTIME, /assignmentState: dispatcherHaulAssignmentState/);
});

test("transport загружается перед основным runtime и входит в PWA shell", () => {
    const transportIndex = SHARED_TEMPLATE.indexOf("dispatcher-transport-v1.js");
    const detailSettingsIndex = SHARED_TEMPLATE.indexOf("dispatcher-detail-settings-v1.js");
    const detailChartsIndex = SHARED_TEMPLATE.indexOf("dispatcher-detail-charts-v1.js");
    const detailIndex = SHARED_TEMPLATE.indexOf("dispatcher-detail-v1.js");
    const equipmentSearchIndex = SHARED_TEMPLATE.indexOf("dispatcher-equipment-search-v1.js");
    const complexTruckRacksIndex = SHARED_TEMPLATE.indexOf("dispatcher-complex-truck-racks-v1.js");
    const assignmentStateIndex = SHARED_TEMPLATE.indexOf("dispatcher-haul-assignment-state-v1.js");
    const dndIndex = SHARED_TEMPLATE.indexOf("dispatcher-board-dnd-v1.js");
    const mutationsIndex = SHARED_TEMPLATE.indexOf("dispatcher-board-mutations-v1.js");
    const boardIndex = SHARED_TEMPLATE.indexOf("dispatcher-board-v1.js");
    const realtimeIndex = SHARED_TEMPLATE.indexOf("dispatcher-realtime-v1.js");
    const controlIndex = SHARED_TEMPLATE.indexOf("dispatcher-control-v1.js");

    assert.ok(transportIndex >= 0, "transport script отсутствует в шаблоне");
    assert.ok(controlIndex > transportIndex, "transport должен загрузиться до основного runtime");
    assert.match(DISPATCHER_TRANSPORT, /global\.createDispatcherTransport = createDispatcherTransport;/);
    assert.ok(detailSettingsIndex > transportIndex, "detail settings must load after transport");
    assert.ok(detailChartsIndex > detailSettingsIndex, "detail charts must load after detail settings");
    assert.ok(detailIndex > detailChartsIndex, "detail must load after its chart presenter");
    assert.ok(equipmentSearchIndex > detailIndex, "equipment search must load after detail");
    assert.ok(complexTruckRacksIndex > equipmentSearchIndex, "complex racks must load after equipment search");
    assert.ok(assignmentStateIndex > complexTruckRacksIndex, "assignment state must load after complex racks");
    assert.ok(dndIndex > assignmentStateIndex, "drag-and-drop must load after assignment state");
    assert.ok(mutationsIndex > dndIndex, "local board mutations must load after drag-and-drop");
    assert.ok(boardIndex > mutationsIndex, "board must load after its local mutation helpers");
    assert.ok(realtimeIndex > boardIndex, "realtime must load after board");
    assert.ok(controlIndex > realtimeIndex, "realtime must load before the main runtime");
    assert.match(DISPATCHER_REALTIME, /global\.createDispatcherRealtime = createDispatcherRealtime;/);
    assert.match(DISPATCHER_RUNTIME, /window\.createDispatcherTransport\(\{/);
    assert.match(DISPATCHER_RUNTIME, /window\.createDispatcherRealtime\(\{/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-transport-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-detail-settings-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-detail-charts-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-detail-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-equipment-search-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-complex-truck-racks-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-haul-assignment-state-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-board-dnd-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-board-mutations-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-board-v1\.js"/);
    assert.match(DISPATCHER_PWA, /"\/static\/js\/dispatcher-realtime-v1\.js"/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-transport-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-detail-settings-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-detail-charts-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-detail-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-equipment-search-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-complex-truck-racks-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-haul-assignment-state-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-board-dnd-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-board-mutations-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-board-v1\.js/);
    assert.match(PRODUCTION_MANIFEST, /static\/js\/dispatcher-realtime-v1\.js/);
});

test("модуль раскладки самосвалов упакован ровно один раз", () => {
    const runtimeName = "dispatcher-complex-truck-racks-v1.js";
    const manifestPath = "СИСТЕМА_MVP/backend/static/js/dispatcher-complex-truck-racks-v1.js";
    assert.equal((SHARED_TEMPLATE.match(new RegExp(runtimeName, "g")) || []).length, 1);
    assert.equal((DISPATCHER_PWA.match(/\/static\/js\/dispatcher-complex-truck-racks-v1\.js/g) || []).length, 1);
    assert.equal(
        PRODUCTION_MANIFEST.split(/\r?\n/).filter((line) => line === manifestPath).length,
        1
    );
});

test("модуль версий назначений упакован ровно один раз", () => {
    const runtimeName = "dispatcher-haul-assignment-state-v1.js";
    const manifestPath = "СИСТЕМА_MVP/backend/static/js/dispatcher-haul-assignment-state-v1.js";
    assert.equal((SHARED_TEMPLATE.match(new RegExp(runtimeName, "g")) || []).length, 1);
    assert.equal((DISPATCHER_PWA.match(/\/static\/js\/dispatcher-haul-assignment-state-v1\.js/g) || []).length, 1);
    assert.equal(
        PRODUCTION_MANIFEST.split(/\r?\n/).filter((line) => line === manifestPath).length,
        1
    );
});

test("модуль drag-and-drop упакован ровно один раз", () => {
    const runtimeName = "dispatcher-board-dnd-v1.js";
    const manifestPath = "СИСТЕМА_MVP/backend/static/js/dispatcher-board-dnd-v1.js";
    assert.equal((SHARED_TEMPLATE.match(new RegExp(runtimeName, "g")) || []).length, 1);
    assert.equal((DISPATCHER_PWA.match(/\/static\/js\/dispatcher-board-dnd-v1\.js/g) || []).length, 1);
    assert.equal(
        PRODUCTION_MANIFEST.split(/\r?\n/).filter((line) => line === manifestPath).length,
        1
    );
});

test("модуль локальных перестановок упакован ровно один раз и владеет только DOM-операциями", () => {
    const runtimeName = "dispatcher-board-mutations-v1.js";
    const manifestPath = "СИСТЕМА_MVP/backend/static/js/dispatcher-board-mutations-v1.js";
    assert.equal((SHARED_TEMPLATE.match(new RegExp(runtimeName, "g")) || []).length, 1);
    assert.equal((DISPATCHER_PWA.match(/\/static\/js\/dispatcher-board-mutations-v1\.js/g) || []).length, 1);
    assert.equal(
        PRODUCTION_MANIFEST.split(/\r?\n/).filter((line) => line === manifestPath).length,
        1
    );
    assert.match(DISPATCHER_BOARD_MUTATIONS, /global\.createDispatcherBoardMutations = createDispatcherBoardMutations;/);
    assert.match(DISPATCHER_BOARD_MUTATIONS, /function moveTruckToGarage/);
    assert.match(DISPATCHER_BOARD_MUTATIONS, /function moveComplexToExcavatorGarage/);
    assert.doesNotMatch(DISPATCHER_BOARD_MUTATIONS, /dispatcherPost\s*\(/);
    assert.doesNotMatch(DISPATCHER_BOARD, /function moveDesktopTruckToGarage/);
    assert.match(DISPATCHER_BOARD, /global\.createDispatcherBoardMutations/);
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
    assert.match(DISPATCHER_RUNTIME, /bindBoardInteractions: dispatcherBoard\.bindInteractions/);
    assert.match(DISPATCHER_RUNTIME, /refreshBoardIntegrity: dispatcherBoard\.refreshIntegrity/);
});
