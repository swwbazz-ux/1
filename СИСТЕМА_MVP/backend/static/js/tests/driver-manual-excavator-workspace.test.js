const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const read = (...parts) => fs.readFileSync(path.join(BACKEND, ...parts), "utf8");
const sharedDrag = require(path.join(BACKEND, "static", "js", "excavator-dashboard-drag-v1.js"));
const driverRuntime = require(path.join(BACKEND, "static", "js", "driver-manual-excavator-workspace-v1.js"));

test("shared drag keeps the Excavator seven-pixel pickup threshold", () => {
    assert.equal(sharedDrag.gestureStarted(7, 7), false);
    assert.equal(sharedDrag.gestureStarted(8, 0), true);
    assert.equal(sharedDrag.gestureStarted(0, -8), true);
});

test("Driver reports durable manual-trip states without a false server confirmation", () => {
    assert.equal(driverRuntime.resultText("saving"), "Сохраняем отметку на телефоне…");
    assert.equal(driverRuntime.resultText("confirmed", 451), "Рейс №451 · в пути");
    assert.equal(driverRuntime.resultText("review"), "Отметка не принята · требуется сверка");
    assert.equal(driverRuntime.resultText("storage-error"), "Не удалось сохранить на телефоне. Повторите отправку.");
});

test("trip timer formats elapsed time and names the selected destination", () => {
    assert.equal(driverRuntime.formatElapsedTime(0), "00:00:00");
    assert.equal(driverRuntime.formatElapsedTime(65), "00:01:05");
    assert.equal(driverRuntime.formatElapsedTime(3661), "01:01:01");
    assert.equal(driverRuntime.formatElapsedTime(-12), "00:00:00");
    assert.equal(driverRuntime.tripTimerLabel("СКЛАД 2.1"), "В ПУТИ · СКЛАД 2.1");
});

test("confirmed manual trip takes its current destination from the fresh server fragment", () => {
    const projected = driverRuntime.serverTripProjectionContext({
        dataset: {
            driverActualDumpPointId: "72",
            driverActualDumpPointName: "СКЛАД 2.1"
        }
    }, {
        dump_points: [{id: 72, name: "СКЛАД 2.1"}]
    });
    assert.equal(projected.payload.dump_point_id, 72);
    assert.equal(projected.context_snapshot.selected_dump_point_id, 72);
    assert.equal(projected.context_snapshot.selected_dump_point_name, "СКЛАД 2.1");
});

test("an Excavator-owned acknowledgement requests one exact Driver reconciliation", () => {
    const calls = [];
    let accepts = false;
    global.AppRealtime = {
        requestReconcile(reason, version) {
            calls.push({reason, version});
            return accepts;
        }
    };
    const receipt = {
        event_id: "manual-load-auto-1",
        trip_origin: "excavator",
        server_ids: {trip_id: 452},
        version: 813
    };
    assert.equal(driverRuntime.requestAutomaticTripRefresh(receipt), false);
    accepts = true;
    assert.equal(driverRuntime.requestAutomaticTripRefresh(receipt), true);
    assert.equal(driverRuntime.requestAutomaticTripRefresh(receipt), false);
    assert.deepEqual(calls, [
        {reason: "driver_manual_automatic_trip_confirmed", version: 813},
        {reason: "driver_manual_automatic_trip_confirmed", version: 813}
    ]);
    delete global.AppRealtime;
});

test("manual point action distinguishes the next trip from a current trip", () => {
    assert.equal(driverRuntime.pointModeForShell({dataset: {driverHasOpenTrip: "false", driverActiveTripId: ""}}), "next");
    assert.equal(driverRuntime.pointModeForShell({dataset: {driverHasOpenTrip: "true", driverActiveTripId: "451"}}), "current");
    assert.deepEqual(driverRuntime.pointActionCopy("next"), {
        label: "ТОЧКА РАЗГРУЗКИ",
        hint: "Для следующего рейса",
        aria: "Выбрать точку разгрузки для следующего рейса"
    });
    assert.deepEqual(driverRuntime.pointActionCopy("current"), {
        label: "ТОЧКА РАЗГРУЗКИ",
        hint: "Изменить текущую",
        aria: "Изменить точку разгрузки текущего рейса"
    });
});

test("current-trip point action delegates to the canonical Driver point control", () => {
    let clicks = 0;
    const canonical = {
        disabled: false,
        hasAttribute(name) { return name === "data-driver-manual-point-open" ? false : false; },
        click() { clicks += 1; }
    };
    const shell = {
        dataset: {driverHasOpenTrip: "true", driverActiveTripId: "451"},
        querySelectorAll(selector) {
            assert.equal(selector, "[data-driver-point-open]");
            return [canonical];
        }
    };
    const workspace = {closest(selector) {
        assert.equal(selector, "[data-driver-shell]");
        return shell;
    }};
    assert.equal(driverRuntime.openPointChooser(workspace), true);
    assert.equal(clicks, 1);
});

test("Driver runtime reuses the common outbox without an independent transport or journal", () => {
    const source = read("static", "js", "driver-manual-excavator-workspace-v1.js");
    assert.doesNotMatch(source, /localStorage|indexedDB|fetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket/);
    assert.match(source, /createDriverManualLoadEvent/);
    assert.match(source, /outbox\.enqueue/);
    assert.doesNotMatch(source, /new\s+(?:Map|Set)\s*\([^)]*journal|manualTripQueue|manualTripStorage/i);
});

test("manual free-bucket button delegates to the canonical Driver control", () => {
    let clicks = 0;
    const canonical = {disabled: false, click() { clicks += 1; }};
    const shell = {querySelector(selector) {
        assert.equal(selector, '[data-mobile-dial-action="free-bucket"]');
        return canonical;
    }};
    const workspace = {closest(selector) {
        assert.equal(selector, "[data-driver-shell]");
        return shell;
    }};
    assert.equal(driverRuntime.openFreeBucket(workspace), true);
    assert.equal(clicks, 1);
    canonical.disabled = true;
    assert.equal(driverRuntime.openFreeBucket(workspace), false);
    assert.equal(clicks, 1);
});

test("both roles render the same dashboard and card includes", () => {
    const excavator = read("templates", "trips", "excavator_work.html");
    const driver = read("templates", "includes", "driver_manual_excavator_workspace.html");
    const workspace = read("templates", "includes", "excavator_dashboard_workspace.html");
    assert.match(excavator, /include "includes\/excavator_dashboard_workspace\.html"/);
    assert.match(driver, /include "includes\/excavator_dashboard_workspace\.html"/);
    assert.match(workspace, /include "includes\/excavator_dashboard_source_card\.html"/);
    assert.match(workspace, /include "includes\/excavator_dashboard_dump_card\.html"/);
    assert.match(workspace, /class="eo-free-bucket-button"/);
    assert.match(workspace, /data-driver-manual-free-bucket-open/);
    assert.match(workspace, /data-eo-free-bucket-open/);
    assert.match(workspace, /data-driver-manual-close/);
    assert.match(workspace, /data-driver-manual-point-open/);
    assert.match(workspace, /data-driver-manual-point-label/);
    assert.match(workspace, />ОБЫЧНЫЙ РЕЖИМ</);
    assert.match(workspace, />ТОЧКА РАЗГРУЗКИ</);
});

test("manual controls keep three columns and stack actions timer and source with one shared gap", () => {
    const workspace = read("templates", "includes", "excavator_dashboard_workspace.html");
    const driverCss = read("static", "css", "driver-manual-excavator-workspace-v1.css");
    const actionStart = workspace.indexOf('data-driver-manual-action-row');
    const timerStart = workspace.indexOf('data-driver-manual-trip-timer');
    const sourceStart = workspace.indexOf('data-driver-manual-source-row');
    assert(actionStart > -1 && timerStart > actionStart && sourceStart > timerStart);
    const actionMarkup = workspace.slice(actionStart, sourceStart);
    assert.equal((actionMarkup.match(/<button\b/g) || []).length, 2);
    assert.match(actionMarkup, /data-driver-manual-close/);
    assert.match(actionMarkup, /data-driver-manual-point-open/);
    assert.match(actionMarkup, />ОБЫЧНЫЙ РЕЖИМ</);
    assert.match(actionMarkup, />ТОЧКА РАЗГРУЗКИ</);
    assert.doesNotMatch(actionMarkup, /data-driver-manual-source|data-eo-truck-card|draggable=/);
    assert.match(workspace, /eo-truck-card eo-dashboard-truck-card driver-manual-workspace__action driver-manual-workspace__action--return/);
    assert.match(workspace, /eo-truck-card eo-dashboard-truck-card driver-manual-workspace__action driver-manual-workspace__action--point/);
    assert.match(workspace.slice(sourceStart), /include "includes\/excavator_dashboard_source_card\.html"/);
    assert.match(driverCss, /driver-manual-workspace__action--return/);
    assert.match(driverCss, /driver-manual-workspace__action--point/);
    assert.match(driverCss, /driver-manual-workspace__action-row/);
    assert.match(driverCss, /driver-manual-workspace__trip-timer/);
    assert.match(driverCss, /driver-manual-workspace__source-row/);
    assert.match(driverCss, /grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/);
    assert.match(driverCss, /--driver-manual-workspace-gap:\s*clamp\(/);
    assert.match(driverCss, /grid-template-rows:\s*auto auto auto/);
    assert.match(driverCss, /align-content:\s*start/);
    assert.match(driverCss, /gap:\s*var\(--driver-manual-workspace-gap\)/);
    assert.match(driverCss, /driver-manual-workspace__action-row\s*\{[^}]*grid-column:\s*1 \/ -1;[^}]*grid-row:\s*1;/s);
    assert.match(driverCss, /driver-manual-workspace__trip-timer\s*\{[^}]*grid-column:\s*1 \/ -1;[^}]*grid-row:\s*2;/s);
    assert.match(driverCss, /driver-manual-workspace__source-row\s*\{[^}]*grid-column:\s*2;[^}]*grid-row:\s*3;/s);
    assert.doesNotMatch(driverCss, /\.eo-dashboard-truck-card\s*\{[^}]*grid-template-columns/s);
});

test("Driver dump cards keep a three-column matrix and only show name plus trip count", () => {
    const dumpCard = read("templates", "includes", "excavator_dashboard_dump_card.html");
    const driverCss = read("static", "css", "driver-manual-excavator-workspace-v1.css");
    assert.match(dumpCard, /driver-manual-workspace__dump-card/);
    assert.match(dumpCard, /data-driver-manual-completed-count/);
    assert.match(dumpCard, /data-driver-manual-last-sent/);
    assert.match(dumpCard, /\{% if not driver_manual_dashboard %\}\s*<span class="eo-dashboard-unload-label">Разгружено<\/span>/s);
    assert.match(driverCss, /\.eo-dashboard-unload-grid\s*\{[^}]*grid-template-columns:\s*repeat\(3,/s);
    assert.match(driverCss, /\.eo-dashboard-unload-grid\.is-count-1\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/s);
    assert.match(driverCss, /\.eo-dashboard-unload-grid\.is-count-2\s*\{[^}]*grid-template-columns:\s*repeat\(2,/s);
    assert.match(driverCss, /--driver-manual-dump-name-size/);
    assert.match(driverCss, /\.is-name-long/);
    assert.equal(driverRuntime.dumpNameSizeClass("ККД"), "is-name-short");
    assert.equal(driverRuntime.dumpNameSizeClass("СКЛАД 2.1 основной север"), "is-name-long");
});

test("server-assigned dump points never inherit the one-off marker", () => {
    const classes = new Set(["is-driver-manual-one-off"]);
    const target = {
        dataset: {driverManualOneOff: "true"},
        classList: {
            toggle(name, enabled) {
                if (enabled) classes.add(name);
                else classes.delete(name);
            }
        }
    };
    driverRuntime.setManualTargetOneOff(target, false);
    assert.equal(classes.has("is-driver-manual-one-off"), false);
    assert.equal(Object.hasOwn(target.dataset, "driverManualOneOff"), false);
    driverRuntime.setManualTargetOneOff(target, true);
    assert.equal(classes.has("is-driver-manual-one-off"), true);
    assert.equal(target.dataset.driverManualOneOff, "true");

    const source = read("static", "js", "driver-manual-excavator-workspace-v1.js");
    assert.match(source, /createManualDumpTarget\([\s\S]*?prototype,\s*point\.one_off === true\s*\)/);
});

test("Driver opens from the existing manual corner and preserves bottom navigation", () => {
    const driver = read("templates", "users", "driver_shift.html");
    const actions = read("templates", "includes", "mobile_dial_actions.html");
    assert.match(actions, /data-driver-manual-open/);
    assert.match(actions, /aria-controls="driver-manual-workspace"/);
    assert.match(driver, /include "includes\/driver_manual_excavator_workspace\.html"/);
    assert.match(driver, /data-driver-bottom-nav/);
});

test("shared controller remains the only live card binder in both roles", () => {
    const excavator = read("templates", "trips", "excavator_work.html");
    const driverRuntimeSource = read("static", "js", "driver-manual-excavator-workspace-v1.js");
    assert.match(excavator, /ExcavatorDashboardDrag\.attach\(/);
    assert.match(driverRuntimeSource, /ExcavatorDashboardDrag\.attach\(/);
    assert.match(driverRuntimeSource, /operational-state-refresh-applied/);
});

test("manual context and confirmed timer survive fragment refresh", () => {
    const driver = read("templates", "includes", "driver_manual_excavator_workspace.html");
    const driverRuntimeSource = read("static", "js", "driver-manual-excavator-workspace-v1.js");
    const driverShiftSource = read("static", "js", "driver-shift-v1.js");
    assert.match(driver, /data-driver-manual-authority-type=/);
    assert.match(driver, /data-driver-manual-assignment-id=/);
    assert.match(driver, /data-driver-manual-rock-type-id=/);
    assert.match(driver, /data-driver-manual-placement-id=/);
    assert.match(driverRuntimeSource, /workspace\.dataset\.driverManualAuthorityType/);
    assert.match(driverRuntimeSource, /restoreProjection\(root\.driverOfflineOutbox, workspace\)/);
    assert.match(driverShiftSource, /DriverManualExcavatorWorkspace\.restoreProjection\(/);
});
