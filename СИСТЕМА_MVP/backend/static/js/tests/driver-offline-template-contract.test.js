"use strict";

const test = require("node:test");
const {driverScreenSource} = require("./driver-screen-source");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const template = driverScreenSource();
const offlineRuntime = fs.readFileSync(path.resolve(__dirname, "../driver-offline-outbox-v2.js"), "utf8");
const views = fs.readFileSync(path.resolve(__dirname, "../../../users/views.py"), "utf8");
const roleApps = fs.readFileSync(path.resolve(__dirname, "../../../users/role_apps.py"), "utf8");

function functionSource(source, name) {
    const start = source.indexOf("function " + name + "(");
    assert.notEqual(start, -1);
    const bodyStart = source.indexOf("{", start);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index += 1) {
        if (source[index] === "{") depth += 1;
        if (source[index] === "}") depth -= 1;
        if (depth === 0) return source.slice(start, index + 1);
    }
    throw new Error("function_not_closed");
}

test("driver v226 shell precaches the durable runtime and exact authenticated dependencies", () => {
    assert.match(template, /driver-offline-outbox-v2\.js/);
    assert.doesNotMatch(template, /createDriverUnloadOutbox/);
    assert.match(views, /DRIVER_SHELL_VERSION = 'driver-mobile-shell-v291'/);
    assert.match(views, /driver-offline-outbox-v2\.js\?v=\{DRIVER_SHELL_VERSION\}/);
    assert.match(views, /async function isValidatedDriverShell/);
    assert.match(views, /html\.includes\("data-driver-shell"\)/);
    assert.match(views, /function driverShellStaticDependencies/);
    assert.match(views, /async function driverShellClosureComplete/);
    assert.match(views, /cacheAuthenticatedDriverShell/);
    assert.match(views, /networkFirstDriverShell/);
    assert.match(views, /migratePreviousAuthenticatedShell/);
    assert.match(views, /Authenticated driver shell is unavailable/);
    assert.match(views, /hasValidatedCurrentShell/);
    const coreAssets = views.match(/const CORE_ASSETS = \[([\s\S]*?)\];/)[1];
    assert.doesNotMatch(coreAssets, /APP_SHELL_URL|LEGACY_SHELL_URL/);
    assert.match(roleApps, /shell_version='driver-mobile-shell-v291'/);
});

test("a legacy loaded shell reloads before adopting a fragment that requires newer assets", () => {
    assert.match(views, /'driver_operational_fragment': requested_fragment == 'driver'/);
    assert.match(template, /\{% if driver_operational_fragment %\}/);
    assert.match(template, /data-driver-fragment-shell="\{\{ driver_shell_version \}\}"/);
    assert.match(template, /runtime\.currentShellVersion/);
    assert.match(template, /loaded!==fresh\)\{window\.location\.reload\(\)/);
    const handler = template.match(/data-driver-fragment-shell="\{\{ driver_shell_version \}\}"[^>]*onload="([^"]+)"/)[1]
        .replaceAll("&amp;", "&");
    function execute(loadedVersion) {
        let reloads = 0;
        let removals = 0;
        const node = {
            dataset: {driverFragmentShell: "driver-mobile-shell-v291"},
            remove() { removals += 1; },
        };
        vm.runInNewContext(`(function(){${handler}}).call(node)`, {
            node,
            String,
            window: {
                __driverPwaUpdateRuntime: {currentShellVersion: loadedVersion},
                location: {reload() { reloads += 1; }},
            },
        });
        return {reloads, removals};
    }
    assert.deepEqual(execute("driver-mobile-shell-v218"), {reloads: 1, removals: 0});
    assert.deepEqual(execute("driver-mobile-shell-v291"), {reloads: 0, removals: 1});
});

test("expired session update migrates a valid shell without touching a nonempty event queue", () => {
    const install = views.split('self.addEventListener("install"', 2)[1].split('self.addEventListener("activate"', 1)[0];
    const activate = views.split('self.addEventListener("activate"', 2)[1].split('async function networkFirst', 1)[0];
    assert.match(install, /if \(prepared \|\| await migratePreviousAuthenticatedShell\(\)\) return/);
    assert.match(install, /throw new Error\("Authenticated driver shell/);
    assert.doesNotMatch(install, /caches\.delete/);
    assert.match(activate, /if \(!prepared\) return \[\]/);
    assert.match(views, /const requests = await source\.keys\(\)/);
    assert.match(views, /await target\.put\(request, response\.clone\(\)\)/);
    assert.match(offlineRuntime, /field-offline-events-v1/);
    assert.doesNotMatch(views, /deleteDatabase|indexedDB\.delete/);
});

test("driver shell exposes confirmed identity and shift context without granting offline shift open", () => {
    assert.match(template, /data-driver-actor-id="\{\{ access\.employee_id \}\}"/);
    assert.match(template, /data-driver-shift-id=/);
    assert.match(template, /data-driver-current-truck-id=/);
    assert.match(template, /data-driver-auth-generation=/);
    assert.match(template, /data-driver-actual-dump-point-id=/);
    assert.match(template, /event_type: "driver\.trip\.unloaded"/);
    assert.match(offlineRuntime, /event_type: "driver\.trip\.dump_point_changed"/);
    assert.match(template, /depends_on: pendingPoint \? \[pendingPoint\.event_id\] : \[\]/);
    assert.match(template, /"driver\.downtime\.started"/);
    assert.match(template, /"driver\.downtime\.ended"/);
    assert.doesNotMatch(template, /event_type: "driver\.shift\.opened"/);
    assert.match(template, /createDriverPointChangeEvent/);
    assert.match(template, /String\(pointId\) === String\(shell\.dataset\.driverActualDumpPointId/);
    assert.match(template, /applyDriverPointSelection\(shell, pointId, pointName, "local"\)/);
    assert.match(template, /function applyDriverPointSelection\(/);
    assert.match(template, /function setDriverPointSyncState\(/);
    assert.doesNotMatch(template, /String\(event\.trip_id \|\| ""\) === unloadTripId\s*&&\s*event\.state === "pending"/);
    assert.match(template, /createDriverDowntimeEndEvent/);
    assert.match(offlineRuntime, /local_downtime_id:[\s\S]*driver\.downtime\.started/);
    assert.match(template, /getServerMapping\(localStartId\)/);
    assert.match(template, /Нет подтверждённого загруженного рейса/);
});

test("dump-point projection helper updates the tile, dial, dataset and sync label", () => {
    const currentStatus = {textContent: "", classList: {toggle() {}}};
    const oldTileStatus = {textContent: "old"};
    const newTileStatus = {innerHTML: ""};
    const oldTile = {
        classList: {remove() {}},
        removeAttribute() {},
        querySelector() { return oldTileStatus; },
    };
    const newTile = {
        dataset: {driverPointName: "Отвал"},
        classList: {add() {}, remove() {}},
        setAttribute() {},
        removeAttribute() {},
        querySelector(selector) { return selector === "[data-driver-point-tile-status]" ? newTileStatus : null; },
    };
    const form = {querySelector() { return newTile; }};
    const pointInput = {closest() { return form; }};
    const currentPoint = {textContent: "ККД"};
    const dial = {textContent: "ККД", dataset: {}};
    const current = {
        dataset: {driverActualDumpPointId: "1", driverActualDumpPointName: "ККД"},
        querySelectorAll() { return [oldTile, newTile]; },
        querySelector(selector) {
            if (selector === '[data-driver-point-sync-state]') return currentStatus;
            if (selector.includes('[value="2"]')) return pointInput;
            if (selector === "[data-driver-current-point-name]") return currentPoint;
            if (selector === "[data-driver-dial-label]") return dial;
            return null;
        },
    };
    const sandbox = {scheduleDriverDialLabelFit() {}};
    vm.runInNewContext(functionSource(template, "setDriverPointSyncState"), sandbox);
    vm.runInNewContext(functionSource(template, "applyDriverPointSelection"), sandbox);
    sandbox.applyDriverPointSelection(current, 2, "Отвал", "local");
    assert.equal(current.dataset.driverActualDumpPointId, "2");
    assert.equal(currentPoint.textContent, "Отвал");
    assert.equal(dial.textContent, "Отвал");
    assert.equal(dial.dataset.driverDialRaw, "Отвал");
    assert.equal(currentStatus.textContent, "Действие сохранено");
    assert.match(newTileStatus.innerHTML, /Текущая/);
});

test("sync uses canonical batch endpoint and distinguishes pending review and storage failure", () => {
    assert.match(template, /fetch\("\/offline-events\/sync\/"/);
    assert.match(template, /status: "auth_required"/);
    assert.match(template, /isDriverSyncAuthResponse/);
    assert.match(offlineRuntime, /responseUrl\.pathname === "\/"/);
    assert.match(offlineRuntime, /data-mobile-role-login/);
    assert.match(template, /resumeAuthRequired/);
    assert.match(template, /setBindings/);
    assert.match(template, /window\.driverOfflinePendingCount/);
    assert.match(template, /window\.driverOfflinePendingCount = pending/);
    assert.match(template, /Действие сохранено/);
    assert.match(template, /Не подтверждено/);
    assert.doesNotMatch(template, /data-driver-sync-count/);
    assert.doesNotMatch(template, /Нужна сверка/);
    assert.match(template, /Не удалось открыть защищённое хранилище/);
    assert.match(template, /aria-live="polite"/);
    assert.match(template, /driver-active-tab-v1:/);
});

test("a failed background flush never recasts a durable unload as a storage failure", () => {
    const submit = functionSource(template, "submitDriverUnloadOnce");
    assert.match(submit, /var flushPromise = driverOfflineOutbox\.flush\(\)/);
    assert.match(submit, /flushPromise\.catch\(function \(\) \{\}\)/);
    assert.doesNotMatch(submit, /return driverOfflineOutbox\.flush\(\)/);
});

test("unload hold keeps the existing dial and avoids per-frame style writes", () => {
    const submit = functionSource(template, "submitDriverUnloadOnce");
    const holdOptions = template.slice(template.indexOf("unloadHoldGuard = window.createDriverRoleHoldGuard"));
    const holdCoreRule = template.match(
        /body\.driver-mobile-screen \.driver-work-dial-button\.is-holding \.driver-work-dial-core \{([\s\S]*?)\}/
    )[1];
    assert.match(template, /The familiar dial stays visually intact while it is held/);
    assert.doesNotMatch(template, /driver-work-hold-progress|driver-work-hold-angle/);
    // Кольцо удержания: в покое невидимо, набирается только поворотом двух полуколец
    // (композитор), без conic-gradient, масок и перекрасок за кадр.
    assert.match(template, /\.driver-work-hold-ring \{[\s\S]*?opacity: 0;[\s\S]*?will-change: opacity;/);
    assert.doesNotMatch(template, /driver-work-hold-bar/);
    const holdKeyframes = template.match(/@keyframes driver-hold-right \{([\s\S]*?to \{[^}]*\})/)[1];
    assert.match(holdKeyframes, /transform: rotate\(/);
    assert.doesNotMatch(holdKeyframes, /background|box-shadow|filter|opacity|clip-path/);
    assert.match(holdCoreRule, /transform:\s*scale\(0\.985\)/);
    assert.doesNotMatch(holdCoreRule, /box-shadow|filter|animation|background/);
    assert.doesNotMatch(template, /\.driver-work-dial:has\(\.driver-work-dial-button\.is-holding\)/);
    assert.doesNotMatch(template, /\.driver-work-dial-button\.is-holding \.driver-work-dial-core::before/);
    assert.doesNotMatch(template, /\.driver-work-dial-button\.is-holding \.driver-work-label/);
    assert.match(submit, /applyDriverOfflineProjection\(shell, driverOfflineEvents\)/);
    assert.doesNotMatch(holdOptions.slice(0, holdOptions.indexOf("window.driverUnloadHoldGuard")), /onProgress:/);
    assert.match(
        template,
        /A completed normal hold is an application action,[\s\S]*?event\.preventDefault\(\);[\s\S]*?holdGuard\.start\(\);/
    );
    assert.doesNotMatch(template, /DriverCosmicDial|DriverOrbitalDial|driver-orbital-v1/);
});

test("экран обновляется послойно, а результат сверяется снимком", () => {
    // Живые узлы остаются на месте — значит и обработчики нажатий остаются, и заново
    // привязывать их не надо. Полная подмена остаётся запасным путём.
    assert.match(template, /window\.driverMorphShell = function \(live, fresh\)/);
    assert.match(
        template,
        /morphed = window\.driverMorphShell\(oldShell, freshShell\)\s*&&\s*window\.driverFragmentSnapshot\(oldShell\)\.full === freshSnapshot\.full/,
        "Результат послойного обновления обязан сверяться снимком с серверным экраном."
    );
    assert.match(
        template,
        /if \(!morphed\) oldShell\.replaceWith\(freshShell\);/,
        "Не сошлось — возвращаемся к полной подмене экрана."
    );
    // Любое расхождение структуры — отказ: иначе вставленный узел остался бы без обработчика.
    const morphSource = template.slice(template.indexOf("window.driverMorphShell = function"));
    assert.match(morphSource.slice(0, morphSource.indexOf("/* DRIVER_FRAGMENT_SNAPSHOT_END */")), /return !an && !bn;/);
    // Области, которые рисует клиент, послойное обновление не трогает и снимок не сравнивает.
    assert.match(template, /window\.DRIVER_CLIENT_OWNED = "\[data-driver-downtime-drum\]/);
    assert.match(template, /all\(window\.DRIVER_CLIENT_OWNED\)\.forEach/);
});

test("круг устроен одинаково с рейсом и без него", () => {
    // Пока состояния различались разметкой, разгрузка меняла структуру экрана и
    // послойное обновление было невозможно.
    assert.doesNotMatch(template, /<div class="driver-work-dial-button is-empty/);
    assert.match(template, /data-driver-hold-button data-driver-pending-label="ОТПРАВКА"\{% if not active_trip %\} disabled/);
});

test("timer-only unload hold never starts a requestAnimationFrame loop", () => {
    const start = template.indexOf("window.createDriverRoleHoldGuard = function");
    const end = template.indexOf("/* DRIVER_ROLE_HOLD_GUARD_END */");
    assert.notEqual(start, -1);
    assert.notEqual(end, -1);
    let rafCalls = 0;
    const sandbox = {
        window: {
            isAppRoleReadonly() { return false; },
            clearTimeout() {},
            cancelAnimationFrame() {},
            requestAnimationFrame() { rafCalls += 1; return 1; },
            setTimeout() { return 1; },
            addEventListener() {},
            removeEventListener() {},
        },
        Date: {now() { return 1000; }},
        Math,
        Number,
    };
    vm.runInNewContext(template.slice(start, end), sandbox);
    const guard = sandbox.window.createDriverRoleHoldGuard({holdMs: 900});
    assert.equal(guard.start(), true);
    assert.equal(rafCalls, 0);
});

test("review-only queue does not block a safe fragment refresh", () => {
    const sandbox = {
        window: {driverOfflinePendingCount: 0},
        document: {activeElement: null, querySelector() { return null; }},
    };
    vm.runInNewContext(functionSource(template, "isDriverOperationalRefreshUnsafe"), sandbox);
    const shell = {
        contains() { return false; },
        querySelector() { return null; },
    };
    assert.equal(sandbox.isDriverOperationalRefreshUnsafe(shell), false);
    sandbox.window.driverOfflinePendingCount = 1;
    assert.equal(sandbox.isDriverOperationalRefreshUnsafe(shell), true);
});
