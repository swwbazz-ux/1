"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const template = fs.readFileSync(path.resolve(__dirname, "../../../templates/users/driver_shift.html"), "utf8");
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

test("driver v215 shell precaches the durable runtime and exact authenticated dependencies", () => {
    assert.match(template, /driver-offline-outbox-v2\.js/);
    assert.doesNotMatch(template, /createDriverUnloadOutbox/);
    assert.match(views, /DRIVER_SHELL_VERSION = 'driver-mobile-shell-v216'/);
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
    assert.match(roleApps, /shell_version='driver-mobile-shell-v216'/);
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
    assert.match(template, /createDriverDowntimeEndEvent/);
    assert.match(offlineRuntime, /local_downtime_id:[\s\S]*driver\.downtime\.started/);
    assert.match(template, /getServerMapping\(localStartId\)/);
    assert.match(template, /Нет подтверждённого загруженного рейса/);
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
    assert.match(template, /Сохранено на телефоне/);
    assert.match(template, /Нужна сверка/);
    assert.match(template, /Не удалось открыть защищённое хранилище/);
    assert.match(template, /aria-live="polite"/);
    assert.match(template, /driver-active-tab-v1:/);
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
