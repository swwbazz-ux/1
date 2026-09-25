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
    assert.equal(driverRuntime.resultText("saving"), "Сохраняем на телефоне…");
    assert.equal(driverRuntime.resultText("confirmed", 451), "Подтверждено · рейс №451");
    assert.match(driverRuntime.resultText("review"), /Отметьте погрузку заново\.$/);
    assert.equal(driverRuntime.resultText("storage-error"), "Не сохранено · повторите отправку");
});

test("manual cancellation copy is explicit and does not claim a second unload", () => {
    assert.match(driverRuntime.resultText("cancel-pending"), /ОТМЕН/);
    assert.match(driverRuntime.resultText("cancelled"), /РЕЙС ОТМЕН/);
});

test("manual trip creation completion and cancellation have distinct durable feedback", () => {
    const haptics = [];
    const tones = [];
    const context = {
        currentTime: 10,
        destination: {},
        createOscillator() {
            const frequency = {
                setValueAtTime(value, at) { tones.push({kind: "start", value, at}); },
                exponentialRampToValueAtTime(value, at) { tones.push({kind: "end", value, at}); }
            };
            return {
                type: "",
                frequency,
                connect() {}, start() {}, stop() {}, disconnect() {},
                addEventListener(name, callback) { if (name === "ended") callback(); }
            };
        },
        createGain() {
            return {
                gain: {setValueAtTime() {}, exponentialRampToValueAtTime() {}},
                connect() {}, disconnect() {}
            };
        }
    };
    global.driverHaptic = (pattern, amplitude) => haptics.push({pattern, amplitude});
    global.ExcavatorDashboardDrag = {preparePickupAudio() { return context; }};
    assert.equal(driverRuntime.playManualFeedback("created"), true);
    assert.equal(driverRuntime.playManualFeedback("completed"), true);
    assert.equal(driverRuntime.playManualFeedback("cancelled"), true);
    assert.deepEqual(haptics.map((item) => item.pattern), [
        [55, 36, 95], [75, 42, 135], [58, 38, 92]
    ]);
    assert.equal(tones.filter((tone) => tone.kind === "start").length, 4);
    delete global.driverHaptic;
    delete global.ExcavatorDashboardDrag;
});

test("manual taps target entry and swipe thresholds use strong distinct haptics", () => {
    const haptics = [];
    global.driverHaptic = (pattern, amplitude) => haptics.push({pattern, amplitude});
    for (const kind of ["tap", "target", "returnArmed", "completeArmed"]) {
        driverRuntime.playGestureHaptic(kind);
    }
    assert.deepEqual(haptics, [
        {pattern: [42], amplitude: 255},
        {pattern: [48, 28, 48], amplitude: 255},
        {pattern: [62, 30, 105], amplitude: 255},
        {pattern: [78, 30, 135], amplitude: 255}
    ]);
    delete global.driverHaptic;
});

test("confirmed cancellation outranks older failed loads but never a newer load", () => {
    const cancellation = {occurred_at: "2026-09-21T19:25:47.483Z"};
    const olderConflict = {occurred_at: "2026-09-21T19:01:59.522Z", state: "conflict"};
    const newerLoad = {occurred_at: "2026-09-21T19:26:00.000Z", state: "pending"};
    assert.equal(driverRuntime.manualCancelWins(cancellation, cancellation, olderConflict, 0), true);
    assert.equal(driverRuntime.manualCancelWins(cancellation, cancellation, newerLoad, 0), false);
    assert.equal(driverRuntime.manualCancelWins(cancellation, cancellation, olderConflict, 11), false);
});

test("an active manual trip locks the source until it is completed or cancelled", () => {
    assert.equal(driverRuntime.sourceShouldBeLocked(false, null), false);
    assert.equal(driverRuntime.sourceShouldBeLocked(true, null), true);
    assert.equal(driverRuntime.sourceShouldBeLocked(false, {state: "pending"}), true);
    assert.equal(driverRuntime.sourceShouldBeLocked(false, {state: "confirmed"}), true);
    assert.equal(driverRuntime.sourceShouldBeLocked(false, {state: "conflict"}), true);
});

test("trip timer formats elapsed time and names the selected destination", () => {
    assert.equal(driverRuntime.formatElapsedTime(0), "00:00:00");
    assert.equal(driverRuntime.formatElapsedTime(65), "00:01:05");
    assert.equal(driverRuntime.formatElapsedTime(3661), "01:01:01");
    assert.equal(driverRuntime.formatElapsedTime(-12), "00:00:00");
    assert.equal(driverRuntime.tripTimerLabel("СКЛАД 2.1"), "С ПОГРУЗКИ · СКЛАД 2.1");
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

test("manual point action is reserved for the current Driver-manual trip", () => {
    assert.equal(driverRuntime.pointModeForShell({dataset: {driverActiveTripOrigin: "", driverActiveTripId: ""}}), "unavailable");
    assert.equal(driverRuntime.pointModeForShell({dataset: {driverActiveTripOrigin: "excavator", driverActiveTripId: "451"}}), "unavailable");
    assert.equal(driverRuntime.pointModeForShell({dataset: {driverActiveTripOrigin: "driver_manual", driverActiveTripId: "451"}}), "current");
    assert.deepEqual(driverRuntime.pointActionCopy("unavailable"), {
        label: "ИЗМЕНИТЬ ТОЧКУ",
        hint: "Сначала создайте рейс",
        aria: "Изменение точки доступно после создания ручного рейса"
    });
    assert.deepEqual(driverRuntime.pointActionCopy("current"), {
        label: "ИЗМЕНИТЬ ТОЧКУ",
        hint: "Текущий рейс",
        aria: "Изменить точку разгрузки текущего ручного рейса"
    });
});

test("manual reroute directory excludes standard, current and duplicate points", () => {
    const candidates = driverRuntime.manualRerouteCandidates([
        {id: 2, name: "ККД"},
        {id: 3, name: "Отвал"},
        {id: 4, name: "Подсыпка"},
        {id: 5, name: "ВКР"},
        {id: 5, name: "ВКР duplicate"},
        {id: 6, name: "Бульдозер"}
    ], [{id: 2}, {id: 3}], 4);
    assert.deepEqual(candidates, [
        {id: 5, name: "ВКР"},
        {id: 6, name: "Бульдозер"}
    ]);
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
    const driverShell = read("templates", "users", "driver_shift.html");
    const driver = read("templates", "includes", "driver_manual_excavator_workspace.html");
    const workspace = read("templates", "includes", "excavator_dashboard_workspace.html");
    assert.match(excavator, /include "includes\/excavator_dashboard_workspace\.html"/);
    assert.match(excavator, /css\/excavator-manual-loading-v1\.css/);
    assert.match(driverShell, /css\/excavator-manual-loading-v1\.css/);
    assert.match(driver, /include "includes\/excavator_dashboard_workspace\.html"/);
    assert.match(workspace, /include "includes\/excavator_dashboard_source_card\.html"/);
    assert.match(workspace, /include "includes\/excavator_dashboard_dump_card\.html"/);
    assert.match(workspace, /class="eo-free-bucket-button"/);
    assert.match(workspace, /data-driver-manual-free-bucket-open/);
    assert.match(workspace, /data-eo-free-bucket-open/);
    assert.match(workspace, /data-driver-manual-close/);
    assert.match(workspace, /data-driver-manual-point-open/);
    assert.match(workspace, /data-driver-manual-point-label/);
    assert.match(workspace, /data-driver-manual-trip-timer-state/);
    assert.match(workspace, /data-driver-manual-trip-timer-destination/);
    assert.match(workspace, /data-driver-manual-result/);
    assert.doesNotMatch(driver, /mobile-shift-toast/);
    assert.match(workspace, />ОБЫЧНЫЙ РЕЖИМ</);
    assert.match(workspace, />ИЗМЕНИТЬ ТОЧКУ</);
    assert.match(workspace, /data-driver-manual-point-open[^>]*disabled[^>]*aria-disabled="true"/);
    assert.doesNotMatch(workspace, /Для следующего рейса/);
});

test("manual controls use compact action timer and source rows with one shared gap", () => {
    const workspace = read("templates", "includes", "excavator_dashboard_workspace.html");
    const driverCss = read("static", "css", "driver-manual-excavator-workspace-v1.css");
    const actionStart = workspace.indexOf('data-driver-manual-action-row');
    const timerStart = workspace.indexOf('data-driver-manual-trip-timer');
    const sourceStart = workspace.indexOf('data-driver-manual-source-row');
    assert(actionStart > -1 && timerStart > actionStart && sourceStart > timerStart);
    const actionMarkup = workspace.slice(actionStart, sourceStart);
    // Две кнопки переключения режима плюс кнопка «Понятно» у отклонённой отметки (скрыта по умолчанию, в строке таймера).
    assert.equal((actionMarkup.match(/<button\b/g) || []).length, 3);
    assert.match(actionMarkup, /data-driver-manual-close/);
    assert.match(actionMarkup, /data-driver-manual-point-open/);
    assert.match(actionMarkup, />ОБЫЧНЫЙ РЕЖИМ</);
    assert.match(actionMarkup, />ИЗМЕНИТЬ ТОЧКУ</);
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
    assert.match(driverCss, /--driver-manual-action-height:\s*clamp\(72px, 8\.6dvh, 78px\)/);
    assert.match(driverCss, /--driver-manual-timer-height:\s*clamp\(62px, 7\.5dvh, 68px\)/);
    // Каждая строка просит свою высоту, но обязана уметь сжаться: иначе на
    // невысоком экране нижняя строка вылезает из зоны на панель точек.
    assert.match(driverCss, /grid-template-rows:\s*minmax\(0, var\(--driver-manual-action-height\)\) minmax\(0, var\(--driver-manual-timer-height\)\) minmax\(0, 1fr\)/);
    assert.match(driverCss, /align-content:\s*stretch/);
    assert.match(driverCss, /gap:\s*var\(--driver-manual-workspace-gap\)/);
    assert.match(driverCss, /driver-manual-workspace__action-row\s*\{[^}]*grid-column:\s*1 \/ -1;[^}]*grid-row:\s*1;/s);
    assert.match(driverCss, /driver-manual-workspace__trip-timer\s*\{[^}]*grid-column:\s*1 \/ -1;[^}]*grid-row:\s*2;/s);
    assert.match(driverCss, /driver-manual-workspace__source-row\s*\{[^}]*grid-column:\s*2;[^}]*grid-row:\s*3;/s);
    // Карточка источника (К-1/ЭКС-1) — прямоугольник 4:5 (~1.25), не квадрат:
    // ширина колонки (треть сетки) и старая высотная клемп-переменная совпали
    // на тестовом устройстве, карточка выходила ровным квадратом 118×118.
    // aspect-ratio считает высоту от фактической ширины, не собьётся, даже
    // если ширина колонки сместится в будущем.
    assert.match(driverCss, /driver-manual-workspace__source-row\s*\{[^}]*aspect-ratio:\s*4 \/ 5;[^}]*transform:\s*none;/s);
    assert.match(driverCss, /driver-manual-workspace__action-row\s*\{[^}]*height:\s*min\(var\(--driver-manual-action-height\), 100%\);[^}]*aspect-ratio:\s*auto;/s);
    assert.match(driverCss, /grid-template-columns:\s*40px minmax\(0, 1fr\)/);
    assert.match(driverCss, /font-size:\s*clamp\(11px, 8\.2cqw, 15px\)/);
    assert.match(driverCss, /driver-manual-workspace__action-copy > em\s*\{[^}]*display:\s*none !important/s);
    assert.match(driverCss, /driver-manual-workspace__action--return:active\s*\{[^}]*rgba\(106, 255, 121, \.58\)/s);
    assert.match(driverCss, /driver-manual-workspace__action--point:active\s*\{[^}]*rgba\(139, 226, 255, \.62\)/s);
    assert.doesNotMatch(driverCss, /driver-manual-workspace__source-row\s*\{[^}]*aspect-ratio:\s*1\s*\/\s*1/s);
    assert.doesNotMatch(driverCss, /\.eo-dashboard-truck-card\s*\{[^}]*grid-template-columns/s);
});

test("Driver manual heading keeps name and two rectangular status controls on one row", () => {
    const driverCss = read("static", "css", "driver-manual-excavator-workspace-v1.css");
    assert.match(driverCss, /driver-manual-workspace[^\{]*\.eo-dashboard-head\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) clamp\(68px, 18vw, 74px\) clamp\(62px, 16vw, 68px\)/s);
    assert.match(driverCss, /driver-manual-workspace \.eo-dashboard-plan-widget\s*\{[^}]*display:\s*contents/s);
    assert.match(driverCss, /driver-manual-workspace \.eo-free-bucket-button\s*\{[^}]*height:\s*46px[^}]*border-radius:\s*12px/s);
    assert.match(driverCss, /driver-manual-workspace \.eo-dashboard-plan-ring\s*\{[^}]*height:\s*46px[^}]*border-radius:\s*12px/s);
    assert.match(driverCss, /driver-manual-workspace \.eo-dashboard-plan-ring\s*\{[^}]*border:\s*2px solid #4ade80[^}]*0 0 0 1px rgba\(18, 88, 53, \.8\)/s);
    assert.match(driverCss, /driver-manual-workspace \.eo-dashboard-plan-ring\s*\{[^}]*--eo-dashboard-loop-color:\s*rgba\(74, 222, 128, \.38\)[^}]*--eo-dashboard-loop-glow:\s*rgba\(74, 222, 128, \.26\)/s);
    assert.match(driverCss, /eo-dashboard-plan-ring::before\s*\{[^}]*background:\s*conic-gradient\(from 0deg,[^}]*max\(var\(--eo-dashboard-loop-progress, 0%\), 7%\)/s);
    assert.match(driverCss, /eo-dashboard-plan-ring::before\s*\{[^}]*saturate\(1\.6\)[^}]*brightness\(1\.35\)/s);
    assert.match(driverCss, /eo-dashboard-plan-ring::before\s*\{[^}]*opacity:\s*1;/s);
    assert.match(driverCss, /driver-manual-workspace \.eo-dashboard-plan-ring::after\s*\{[^}]*display:\s*none/s);
    assert.match(driverCss, /driver-manual-workspace \.eo-dashboard-plan-ring\.is-plan-missing::after\s*\{[^}]*display:\s*none/s);
});

test("manual mode survives lower tabs and blocks ordinary mode during an active trip", () => {
    const source = read("static", "js", "driver-manual-excavator-workspace-v1.js");
    const shift = read("static", "js", "driver-shift-v1.js");
    const workspace = read("templates", "includes", "driver_manual_excavator_workspace.html");

    assert.match(source, /function onTabChange\(tab\)/);
    assert.match(source, /closeWorkspace\(workspace, \{preserveRequest: true\}\)/);
    assert.match(source, /if \(workspaceRequestedOpen\) openWorkspace\(null\)/);
    assert.match(shift, /DriverManualExcavatorWorkspace\.onTabChange\(tab\)/);
    assert.doesNotMatch(workspace, /data-driver-manual-exit-confirm|Завершить рейс и выйти/);
    assert.match(source, /function setManualExitAvailability\(workspace\)/);
    assert.match(source, /Сначала завершите рейс свайпом вниз/);
    assert.match(source, /createDriverManualCompletedEvent/);
    assert.match(source, /onComplete:\s*function \(target\)/);
    assert.match(source, /var pendingCompletion = latestManualCompletion\(events\)/);
    assert.match(source, /dependsOn\.push\(String\(pendingCompletion\.event_id\)\)/);
    assert.doesNotMatch(source, /localStorage|indexedDB|fetch\s*\(/);
});

test("only the active dump point exposes cancel and complete swipe cues", () => {
    const card = read("templates", "includes", "excavator_dashboard_dump_card.html");
    const css = read("static", "css", "driver-manual-excavator-workspace-v1.css");
    const source = read("static", "js", "driver-manual-excavator-workspace-v1.js");
    assert.match(card, /driver-manual-workspace__swipe-cue--cancel[\s\S]*▲[\s\S]*ОТМЕНИТЬ/);
    assert.match(card, /driver-manual-workspace__swipe-cue--complete[\s\S]*ЗАВЕРШИТЬ[\s\S]*▼/);
    assert.match(css, /is-active-manual-trip \.driver-manual-workspace__swipe-cue\s*\{[^}]*display:\s*flex/s);
    assert.match(css, /driver-manual-active-dump-pulse/);
    assert.match(css, /driver-manual-workspace__swipe-cue--cancel b\s*\{[^}]*top:\s*-4px/s);
    assert.match(css, /driver-manual-workspace__swipe-cue--complete b\s*\{[^}]*bottom:\s*-4px/s);
    assert.match(source, /is-active-manual-trip", isLast && !!currentTripProjection/);
});

test("long excavator title is fitted to the real source card width", () => {
    const classes = new Set();
    const properties = new Map();
    const title = {
        clientWidth: 100,
        scrollWidth: 200,
        classList: {
            remove(name) { classes.delete(name); },
            toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); }
        },
        style: {
            removeProperty(name) { properties.delete(name); },
            setProperty(name, value) { properties.set(name, value); }
        }
    };
    const fitted = driverRuntime.fitSourceTitle({querySelector() { return title; }});
    assert.equal(fitted, 15);
    assert.equal(properties.get("font-size"), "15.00px");
    assert.equal(classes.has("is-driver-manual-title-wrapped"), true);
});

test("manual point counter applies each optimistic load or cancel exactly once", () => {
    const count = {textContent: "2", setAttribute() {}};
    const target = {
        dataset: {eoDumpTarget: "7", eoDumpName: "Warehouse", driverManualCompletedCount: "2"},
        classList: {contains() { return false; }},
        querySelector() { return count; },
        setAttribute() {}
    };
    const workspace = {
        __driverManualBaseContext: {dump_points: [{id: 7, completed_count: 2}]},
        __driverManualTargetCache: {},
        querySelector(selector) { return selector.includes('"7"') ? target : null; },
        querySelectorAll() { return []; }
    };
    assert.equal(driverRuntime.updateManualTripCount(workspace, 7, 1, "load:1"), 3);
    assert.equal(driverRuntime.updateManualTripCount(workspace, 7, 1, "load:1"), null);
    assert.equal(driverRuntime.updateManualTripCount(workspace, 7, -1, "cancel:1"), 2);
    assert.equal(target.dataset.driverManualCompletedCount, "2");
    assert.equal(count.textContent, "2");
    assert.equal(workspace.__driverManualBaseContext.dump_points[0].completed_count, 2);
});

test("Driver binds the common gesture to the real Excavator shell and preserves its active motion", () => {
    const driverShell = read("templates", "users", "driver_shift.html");
    const source = read("static", "js", "driver-manual-excavator-workspace-v1.js");
    const css = read("static", "css", "driver-manual-excavator-workspace-v1.css");
    assert.match(driverShell, /excavator-dump-return-swipe-v1\.js/);
    assert.match(source, /var excavatorShell = workspace\.querySelector\("\[data-driver-manual-eo-shell\]"\)/);
    assert.match(source, /ExcavatorDashboardDrag\.attach\(\{\s*shell: excavatorShell,/s);
    assert.match(source, /ExcavatorDumpReturnSwipe\.attach\(\{\s*shell: excavatorShell,/s);
    assert.match(source, /onTargetChange:\s*function \(\) \{\s*playGestureHaptic\("target"\)/s);
    assert.match(source, /onArm:\s*function \(target, direction\)/);
    assert.match(source, /target\.dataset\.eoReturnEnabled === "true"/);
    assert.match(source, /createDriverManualLoadCancelledEvent/);
    assert.match(source, /data-driver-manual-current-only/);
    assert.match(source, /targetSelector: '\[data-driver-manual-dump-target\]:not\(\[data-driver-manual-current-only="true"\]\)'/);
    assert.match(css, /:not\(\.is-truck-drag-active\) \.driver-manual-workspace__dump-card\s*\{\s*height:\s*100% !important;/s);
    assert.doesNotMatch(css, /\[data-driver-manual-eo-shell\] \.driver-manual-workspace__dump-card\s*\{[^}]*height:\s*100% !important;/s);
    assert.match(css, /\.is-driver-manual-one-off:not\(\.is-last-dump\):not\(\.is-drop-ready\)/);
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
    assert.match(driverCss, /\.driver-manual-workspace__dump-card\.is-last-dump:not\(\.is-drop-ready\)/);
    assert.equal(driverRuntime.dumpNameSizeClass("ККД"), "is-name-short");
    assert.equal(driverRuntime.dumpNameSizeClass("СКЛАД 2.1 основной север"), "is-name-long");
});

test("the latest destination is the only highlighted Driver dump card", () => {
    function target(id, name, count) {
        const classes = new Set();
        const attributes = {};
        return {
            dataset: {
                eoDumpTarget: String(id),
                eoDumpName: name,
                driverManualCompletedCount: String(count)
            },
            classList: {
                toggle(className, enabled) {
                    if (enabled) classes.add(className);
                    else classes.delete(className);
                },
                contains(className) { return classes.has(className); }
            },
            setAttribute(name, value) { attributes[name] = String(value); },
            removeAttribute(name) { delete attributes[name]; },
            attributes
        };
    }
    const first = target(11, "СКЛАД 2.1", 3);
    const second = target(12, "ККД", 4);
    const workspace = {
        querySelectorAll(selector) {
            assert.equal(selector, "[data-driver-manual-dump-target]");
            return [first, second];
        }
    };
    assert.equal(driverRuntime.markLastDump(workspace, 12), second);
    assert.equal(first.classList.contains("is-last-dump"), false);
    assert.equal(second.classList.contains("is-last-dump"), true);
    assert.equal(first.dataset.driverManualLastSent, "false");
    assert.equal(second.dataset.driverManualLastSent, "true");
    assert.equal(first.attributes["aria-current"], undefined);
    assert.equal(second.attributes["aria-current"], "true");
    assert.match(second.attributes["aria-label"], /последняя точка отправки/);
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

test("initial binding preserves server counters and the last destination", () => {
    const targets = [
        {dataset: {eoDumpTarget: "11", driverManualCompletedCount: "3", driverManualLastSent: "false"}},
        {dataset: {eoDumpTarget: "12", driverManualCompletedCount: "4", driverManualLastSent: "true"}}
    ];
    const workspace = {
        dataset: {
            driverManualAuthorityType: "assignment",
            driverManualExcavatorId: "91"
        },
        querySelectorAll(selector) {
            assert.equal(selector, "[data-driver-manual-dump-target]");
            return targets;
        }
    };
    const context = {
        authority_type: "assignment",
        excavator_id: 91,
        dump_points: [{id: 11}, {id: 12}]
    };

    assert.equal(driverRuntime.renderedContextKey(workspace), driverRuntime.manualContextKey(context));
    assert.equal(driverRuntime.shouldRebuildWorkspaceContext(workspace, context), false);
    assert.equal(targets[0].dataset.driverManualCompletedCount, "3");
    assert.equal(targets[1].dataset.driverManualCompletedCount, "4");
    assert.equal(targets[1].dataset.driverManualLastSent, "true");

    assert.equal(driverRuntime.shouldRebuildWorkspaceContext(workspace, {
        authority_type: "free_bucket",
        excavator_id: 92,
        dump_points: [{id: 11}, {id: 12}]
    }), true);
    assert.equal(workspace.__driverManualContextKey, driverRuntime.manualContextKey(context));
});

test("returning from free bucket restores the cached primary counters and last destination", () => {
    function target(id, count, last) {
        return {
            dataset: {
                eoDumpTarget: String(id),
                driverManualCompletedCount: String(count),
                driverManualLastSent: last ? "true" : "false"
            },
            cloneNode() { return target(id, count, last); }
        };
    }
    let rendered = [target(2, 0, false), target(3, 2, true), target(4, 0, false)];
    const workspace = {
        __driverManualContextKey: "assignment:2:2,3,4",
        querySelectorAll(selector) {
            assert.equal(selector, "[data-driver-manual-dump-target]");
            return rendered;
        }
    };

    driverRuntime.rememberWorkspaceTargets(workspace, workspace.__driverManualContextKey);
    rendered = [target(4, 0, false)];
    const restored = driverRuntime.cachedWorkspaceTargets(
        workspace,
        "assignment:2:2,3,4",
        [{id: 2}, {id: 3}, {id: 4}]
    );

    assert.equal(restored.length, 3);
    assert.equal(restored[1].dataset.driverManualCompletedCount, "2");
    assert.equal(restored[1].dataset.driverManualLastSent, "true");
    assert.equal(driverRuntime.cachedWorkspaceTargets(workspace, "assignment:2:2,3,4", [{id: 4}]), null);
});

test("the primary context survives a temporary DOM replacement when fragment JSON is absent", () => {
    const previousDocument = global.document;
    let rendered = [
        {dataset: {eoDumpTarget: "2", eoDumpName: "Warehouse", eoDumpDistance: "1"}},
        {dataset: {eoDumpTarget: "3", eoDumpName: "Crusher", eoDumpDistance: "2"}}
    ];
    const workspace = {
        dataset: {
            driverManualAuthorityType: "assignment",
            driverManualTruckId: "43",
            driverManualExcavatorId: "2",
            driverManualExcavatorLabel: "EXC-1",
            driverManualAssignmentId: "71"
        },
        querySelector(selector) {
            if (selector === "[data-driver-manual-source]") return {dataset: {assignmentId: "71"}};
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-driver-manual-reroute-point]") return [];
            assert.equal(selector, "[data-driver-manual-dump-target]");
            return rendered;
        }
    };
    global.document = {
        getElementById() { return null; },
        querySelector(selector) {
            assert.equal(selector, "[data-driver-manual-workspace]");
            return workspace;
        }
    };
    try {
        const baseline = driverRuntime.readWorkspaceContext();
        rendered = [{dataset: {eoDumpTarget: "4", eoDumpName: "Temporary", eoDumpDistance: "3"}}];
        const restored = driverRuntime.readWorkspaceContext();
        assert.deepEqual(baseline.dump_points.map(point => point.id), [2, 3]);
        assert.deepEqual(restored.dump_points.map(point => point.id), [2, 3]);
        assert.equal(restored.assignment_id, 71);
    } finally {
        if (previousDocument === undefined) delete global.document;
        else global.document = previousDocument;
    }
});

test("a server free-bucket fragment keeps the primary assignment as the cancellation fallback", () => {
    const previousDocument = global.document;
    const previousFreeBucket = global.DriverFreeBucket;
    const workspace = {
        dataset: {
            driverManualActiveOrigin: "driver_manual",
            driverManualPrimaryTruckId: "43",
            driverManualPrimaryExcavatorId: "2",
            driverManualPrimaryExcavatorLabel: "EXC-1",
            driverManualPrimaryComplexLabel: "K-1",
            driverManualPrimaryAssignmentId: "71",
            driverManualPrimaryPlacementId: "61",
            driverManualPrimaryPlacementUpdatedAt: "2026-09-22T01:00:00Z",
            driverManualPrimaryRockTypeId: "51",
            driverManualPrimaryRockTypeName: "Primary ore",
            driverManualPrimaryLoadingHorizon: "15",
            driverManualPrimaryLoadingBlock: "55"
        },
        querySelector() { return null; },
        querySelectorAll(selector) {
            if (selector === "[data-driver-manual-reroute-point]") return [
                {dataset: {driverManualReroutePointId: "2", driverManualReroutePointName: "Warehouse"}},
                {dataset: {driverManualReroutePointId: "3", driverManualReroutePointName: "Crusher"}},
                {dataset: {driverManualReroutePointId: "9", driverManualReroutePointName: "Road fill"}}
            ];
            if (selector !== "[data-driver-manual-primary-point]") return [];
            return [
                {dataset: {driverManualPrimaryPointId: "2", driverManualPrimaryPointName: "Warehouse", driverManualPrimaryPointDistance: "1.00", driverManualPrimaryPointCount: "2", driverManualPrimaryPointLast: "false"}},
                {dataset: {driverManualPrimaryPointId: "3", driverManualPrimaryPointName: "Crusher", driverManualPrimaryPointDistance: "2.00", driverManualPrimaryPointCount: "1", driverManualPrimaryPointLast: "true"}}
            ];
        }
    };
    global.DriverFreeBucket = {
        currentCatalog() {
            return {
                excavators: [{
                    id: 2,
                    is_primary: true,
                    dump_points: [{id: 2, name: "Stale Warehouse"}, {id: 3, name: "Stale Crusher"}]
                }]
            };
        }
    };
    global.document = {
        getElementById() { return null; },
        querySelector() { return workspace; }
    };
    try {
        const restored = driverRuntime.readWorkspaceContext();
        assert.equal(restored.authority_type, "assignment");
        assert.equal(restored.assignment_id, 71);
        assert.equal(restored.excavator_id, 2);
        assert.deepEqual(restored.dump_points.map(point => point.id), [2, 3]);
        assert.deepEqual(restored.reroute_points.map(point => point.id), [2, 3, 9]);
        assert.deepEqual(
            restored.dump_points.map(point => [point.completed_count, point.is_last_sent]),
            [[2, false], [1, true]]
        );
    } finally {
        if (previousDocument === undefined) delete global.document;
        else global.document = previousDocument;
        if (previousFreeBucket === undefined) delete global.DriverFreeBucket;
        else global.DriverFreeBucket = previousFreeBucket;
    }
});

test("an active free-bucket trip stays separate from the primary context of the next loading", () => {
    const previousDocument = global.document;
    const workspace = {
        dataset: {driverManualActiveOrigin: "driver_manual"},
        querySelector() { return null; },
        querySelectorAll() { return []; }
    };
    const scripts = {
        "driver-manual-workspace-base-context-data": {
            textContent: JSON.stringify({
                authority_type: "assignment",
                assignment_id: 71,
                excavator_id: 2,
                excavator_label: "ЭКС-1",
                dump_points: [{id: 2, name: "СКЛАД 2.1"}]
            })
        },
        "driver-manual-workspace-context-data": {
            textContent: JSON.stringify({
                authority_type: "free_bucket",
                free_bucket_acceptance_id: 81,
                excavator_id: 3,
                excavator_label: "ЭКС-99",
                dump_points: [{id: 3, name: "ККД"}]
            })
        }
    };
    global.document = {
        getElementById(id) { return scripts[id] || null; },
        querySelector() { return workspace; }
    };
    try {
        const nextLoading = driverRuntime.readWorkspaceContext();
        const currentTrip = driverRuntime.readWorkspaceTripContext();
        assert.equal(nextLoading.authority_type, "assignment");
        assert.equal(nextLoading.excavator_id, 2);
        assert.equal(currentTrip.authority_type, "free_bucket");
        assert.equal(currentTrip.excavator_id, 3);
    } finally {
        if (previousDocument === undefined) delete global.document;
        else global.document = previousDocument;
    }
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
    assert.match(driver, /driver-manual-workspace-base-context-data/);
    assert.match(driver, /data-driver-manual-assignment-id=/);
    assert.match(driver, /data-driver-manual-rock-type-id=/);
    assert.match(driver, /data-driver-manual-placement-id=/);
    assert.match(driverRuntimeSource, /workspace\.dataset\.driverManualAuthorityType/);
    assert.match(driverRuntimeSource, /restoreProjection\(root\.driverOfflineOutbox, workspace\)/);
    assert.match(driverShiftSource, /DriverManualExcavatorWorkspace\.restoreProjection\(/);
});

test("отклонённая отметка ручного рейса разблокирует экран сама, без касаний", () => {
    // Боевой случай: машинист пересохранил забой без изменений (только
    // placement_updated_at сдвинулся), сервер отклонил driver.trip.loaded
    // конфликтом manual_work_context_changed. До этой правки currentTripProjection
    // оставался заполненным отклонённой записью навсегда — снять было нечем:
    // активной плитки нет, свайп завершать нечего, а isTerminalState держит
    // setSourceLocked(true) и setManualExitAvailability() без всякого выхода.
    function makeNode(overrides) {
        const dataset = {};
        const classes = new Set();
        const children = {};
        return Object.assign({
            dataset,
            hidden: false,
            disabled: false,
            textContent: "",
            title: "",
            draggable: true,
            classList: {
                add(...names) { names.forEach((n) => classes.add(n)); },
                remove(...names) { names.forEach((n) => classes.delete(n)); },
                toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
                contains(name) { return classes.has(name); }
            },
            setAttribute(name, value) { this["attr:" + name] = value; },
            getAttribute(name) { return this["attr:" + name]; },
            querySelector(selector) { return children[selector] || null; },
            querySelectorAll() { return []; },
            __children: children
        }, overrides || {});
    }

    const shell = {dataset: {}};
    const source = makeNode();
    const result = makeNode();
    const ack = makeNode();
    const closeHint = makeNode();
    const closeButton = makeNode();
    closeButton.__children.em = closeHint;
    closeHint.textContent = "Вернуться к круглой кнопке";
    const pointLabel = makeNode();
    const pointHint = makeNode();
    const pointOpen = makeNode();
    pointOpen.__children["[data-driver-manual-point-label]"] = pointLabel;
    pointOpen.__children["[data-driver-manual-point-hint]"] = pointHint;
    const timerLabel = makeNode();
    const timerState = makeNode();
    const timerDestination = makeNode();
    const timerValue = makeNode();
    const timer = makeNode();
    timer.__children["[data-driver-manual-trip-timer-label]"] = timerLabel;
    timer.__children["[data-driver-manual-trip-timer-state]"] = timerState;
    timer.__children["[data-driver-manual-trip-timer-destination]"] = timerDestination;
    timer.__children["[data-driver-manual-trip-timer-value]"] = timerValue;

    const workspaceChildren = {
        "[data-driver-manual-source]": source,
        "[data-driver-manual-result]": result,
        "[data-driver-manual-dismiss-rejected]": ack,
        "[data-driver-manual-point-open]": pointOpen,
        "[data-driver-manual-trip-timer]": timer
    };
    const workspace = {
        closest() { return shell; },
        querySelector(selector) { return workspaceChildren[selector] || null; },
        querySelectorAll(selector) {
            return selector === "[data-driver-manual-close]" ? [closeButton] : [];
        }
    };

    const rejectedEvent = {
        event_type: "driver.trip.loaded",
        event_id: "evt-1",
        local_trip_id: "local-1",
        state: "conflict",
        sequence: 1,
        occurred_at: "2026-09-25T05:10:00.000Z",
        last_error: {message: "Настройки забоя изменились после сохранения отметки на телефоне."},
        payload: {dump_point_id: 0},
        context_snapshot: {}
    };

    driverRuntime.renderProjection(workspace, [rejectedEvent], null);

    // Сразу после отказа, БЕЗ единого касания: источник и «Обычный режим»
    // уже разблокированы — держать их нечем, currentTripProjection пуст.
    // «Изменить точку» остаётся недоступна — так и должно быть, активного
    // рейса нет, это не регрессия. Сообщение об отказе показано.
    assert.equal(source.disabled, false, "источник не заблокирован ни на миг");
    assert.equal(closeButton.disabled, false, "«Обычный режим» не заблокирован ни на миг");
    assert.equal(ack.hidden, false, "кнопка «Понятно» показана");
    assert.match(result.textContent, /Отметьте погрузку заново\./);

    driverRuntime.dismissRejectedTripProjection(workspace);

    // «Понятно» только прячет сообщение — блокировки, которую снимать,
    // не было вовсе.
    assert.equal(source.disabled, false, "источник остаётся разблокирован");
    assert.equal(closeButton.disabled, false, "«Обычный режим» остаётся разблокирован");
    assert.equal(ack.hidden, true, "кнопка «Понятно» спрятана после нажатия");
    assert.equal(result.hidden, true, "сообщение об отказе убрано");
});

test("отклонённая смена точки разгрузки не проходит молча", () => {
    // Найдено при разборе боевого инцидента: сервер отклоняет
    // driver.trip.dump_point_changed по своим причинам (точка деактивирована,
    // рейс уже не редактируется, точку уже меняли позже) — забой здесь не
    // проверяется вовсе, путь отдельный от погрузки. Клиент тихо оставлял
    // прежнюю точку в payload/context_snapshot и ничего не говорил водителю:
    // тот выбирал новую точку, думал, что сменил, а на деле осталась старая.
    function makeNode(overrides) {
        const dataset = {};
        const classes = new Set();
        const children = {};
        return Object.assign({
            dataset,
            hidden: false,
            disabled: false,
            textContent: "",
            title: "",
            draggable: true,
            classList: {
                add(...names) { names.forEach((n) => classes.add(n)); },
                remove(...names) { names.forEach((n) => classes.delete(n)); },
                toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
                contains(name) { return classes.has(name); }
            },
            setAttribute(name, value) { this["attr:" + name] = value; },
            getAttribute(name) { return this["attr:" + name]; },
            querySelector(selector) { return children[selector] || null; },
            querySelectorAll() { return []; },
            __children: children
        }, overrides || {});
    }

    const shell = {dataset: {}};
    const source = makeNode();
    const result = makeNode();
    const ack = makeNode();
    const pointNotice = makeNode();
    const closeHint = makeNode();
    const closeButton = makeNode();
    closeButton.__children.em = closeHint;
    const pointLabel = makeNode();
    const pointHint = makeNode();
    const pointOpen = makeNode();
    pointOpen.__children["[data-driver-manual-point-label]"] = pointLabel;
    pointOpen.__children["[data-driver-manual-point-hint]"] = pointHint;
    const timerLabel = makeNode();
    const timerState = makeNode();
    const timerDestination = makeNode();
    const timerValue = makeNode();
    const timer = makeNode();
    timer.__children["[data-driver-manual-trip-timer-label]"] = timerLabel;
    timer.__children["[data-driver-manual-trip-timer-state]"] = timerState;
    timer.__children["[data-driver-manual-trip-timer-destination]"] = timerDestination;
    timer.__children["[data-driver-manual-trip-timer-value]"] = timerValue;

    const workspaceChildren = {
        "[data-driver-manual-source]": source,
        "[data-driver-manual-result]": result,
        "[data-driver-manual-dismiss-rejected]": ack,
        "[data-driver-manual-point-notice]": pointNotice,
        "[data-driver-manual-point-open]": pointOpen,
        "[data-driver-manual-trip-timer]": timer
    };
    const workspace = {
        dataset: {},
        closest() { return shell; },
        querySelector(selector) { return workspaceChildren[selector] || null; },
        querySelectorAll(selector) {
            return selector === "[data-driver-manual-close]" ? [closeButton] : [];
        }
    };

    const loadEvent = {
        event_type: "driver.trip.loaded",
        event_id: "load-incident",
        local_trip_id: "local-load-incident",
        state: "pending",
        sequence: 1,
        occurred_at: "2026-09-25T09:00:00.000Z",
        payload: {dump_point_id: 5, assigned_dump_point_id: 5},
        context_snapshot: {selected_dump_point_id: 5, selected_dump_point_name: "ККД"}
    };

    // Контрольный прогон: тот же рейс, без попытки сменить точку — чтобы
    // сравнить, не добавляет ли отклонённая смена НОВУЮ блокировку сверх той,
    // что и так даёт активный рейс.
    driverRuntime.renderProjection(workspace, [loadEvent], null);
    const lockedByTripAlone = source.disabled;

    source.disabled = false; // сбрасываем стаб перед вторым прогоном

    const rejectedPointChange = {
        event_type: "driver.trip.dump_point_changed",
        event_id: "point-incident",
        local_trip_id: "local-load-incident",
        state: "conflict",
        sequence: 2,
        occurred_at: "2026-09-25T09:01:00.000Z",
        last_error: {message: "Точка разгрузки больше недоступна."},
        payload: {dump_point_id: 9},
        context_snapshot: {selected_dump_point_name: "Отвал"}
    };

    const projected = driverRuntime.renderProjection(workspace, [loadEvent, rejectedPointChange], null);

    // Блокировка не меняется отклонённой сменой точки — её и так даёт активный
    // рейс, точка тут ни при чём.
    assert.equal(source.disabled, lockedByTripAlone, "отклонённая смена точки не добавляет блокировку сверх той, что уже даёт активный рейс");
    // Прежняя точка осталась в проекции — выбор не применился.
    assert.equal(projected.payload.dump_point_id, 5, "точка осталась прежней: 9 не применилось");
    // И это видно на экране.
    assert.equal(pointNotice.hidden, false, "уведомление о несостоявшейся смене показано");
    assert.match(pointNotice.textContent, /Точка разгрузки больше недоступна\./);
    assert.match(pointNotice.textContent, /Выберите точку снова\./);

    // Успешный рендер заводит интервал таймера рейса (setInterval) — иначе
    // он тикает вечно и держит процесс живым уже после конца теста.
    driverRuntime.stopTripTimer(workspace);
});
