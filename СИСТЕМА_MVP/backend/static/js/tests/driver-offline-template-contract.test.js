"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const template = fs.readFileSync(path.resolve(__dirname, "../../../templates/users/driver_shift.html"), "utf8");
const views = fs.readFileSync(path.resolve(__dirname, "../../../users/views.py"), "utf8");
const roleApps = fs.readFileSync(path.resolve(__dirname, "../../../users/role_apps.py"), "utf8");

test("driver v213 shell precaches the durable offline runtime", () => {
    assert.match(template, /driver-offline-outbox-v2\.js/);
    assert.doesNotMatch(template, /createDriverUnloadOutbox/);
    assert.match(views, /DRIVER_SHELL_VERSION = 'driver-mobile-shell-v213'/);
    assert.match(views, /driver-offline-outbox-v2\.js\?v=\{DRIVER_SHELL_VERSION\}/);
    assert.match(roleApps, /shell_version='driver-mobile-shell-v213'/);
});

test("driver shell exposes confirmed identity and shift context without granting offline shift open", () => {
    assert.match(template, /data-driver-actor-id="\{\{ access\.employee_id \}\}"/);
    assert.match(template, /data-driver-shift-id=/);
    assert.match(template, /data-driver-current-truck-id=/);
    assert.match(template, /event_type: "driver\.trip\.unloaded"/);
    assert.match(template, /event_type: "driver\.trip\.dump_point_changed"/);
    assert.match(template, /depends_on: pendingPoint \? \[pendingPoint\.event_id\] : \[\]/);
    assert.match(template, /"driver\.downtime\.started"/);
    assert.match(template, /"driver\.downtime\.ended"/);
    assert.doesNotMatch(template, /event_type: "driver\.shift\.opened"/);
});

test("sync uses canonical batch endpoint and distinguishes pending review and storage failure", () => {
    assert.match(template, /fetch\("\/offline-events\/sync\/"/);
    assert.match(template, /status: "auth_required"/);
    assert.match(template, /response\.redirected === true/);
    assert.match(template, /window\.driverOfflinePendingCount/);
    assert.match(template, /Сохранено на телефоне/);
    assert.match(template, /Нужна сверка/);
    assert.match(template, /Не удалось открыть защищённое хранилище/);
});
