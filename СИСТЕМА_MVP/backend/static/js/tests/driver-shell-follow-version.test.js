"use strict";
/* Матрица без сети A5 (30.09.2026): метка новой версии во фрагменте
   перезагружала страницу через 1,5 с — раньше установки нового service
   worker, старый отдавал старую страницу, и она оставалась под замком. А
   разовая перезагрузка после смены воркера откладывалась, пока страница скрыта
   или занята, и не повторялась («Позже», свёрнутое приложение). Теперь экран
   ждёт сообщения base.html о новом воркере и перезагружается, когда он уже
   новый и страница видна; обновление воркера ведёт только base.html. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const SHIFT = fs.readFileSync(path.resolve(__dirname, "../driver-shift-v1.js"), "utf8");
const REFRESH = fs.readFileSync(path.resolve(__dirname, "../driver-shift-refresh-v1.js"), "utf8");

function body(source, signature) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, signature);
    return source.slice(start, source.indexOf("\n            };", start));
}

test("a fragment of a newer shell asks to follow the version instead of reloading at once", () => {
    assert.match(REFRESH, /followRuntime && typeof followRuntime\.followShellVersion === "function"[\s\S]{0,400}followRuntime\.followShellVersion\(freshShellVersion\);[\s\S]{0,40}\} else if \(loadedShellVersion && freshShellVersion && loadedShellVersion !== freshShellVersion\)/);
});

test("the page reloads only into a controller that already has the new version, never while hidden, once", () => {
    const check = body(SHIFT, "runtime.checkFollow = function ()");
    assert.match(check, /versionNumber\(controllerVersion\) >= versionNumber\(target\)[\s\S]{0,80}runtime\.reloadIntoWorker\(target\);/);
    const reload = body(SHIFT, "runtime.reloadIntoWorker = function (target)");
    assert.match(reload, /if \(document\.hidden \|\| runtime\.followReloading\) return;/);
    assert.match(reload, /\^focus:\|\^opening_form\$\|\^close_form\$/);
    assert.match(reload, /runtime\.followReloading = true;\s*window\.location\.reload\(\);/);
    const follow = body(SHIFT, "runtime.followShellVersion = function (targetVersion)");
    assert.match(follow, /document\.addEventListener\("visibilitychange", function \(\) \{\s*if \(!document\.hidden\) runtime\.checkFollow\(\);/);
    assert.doesNotMatch(follow, /\.update\(\)|controllerchange/, "base.html owns worker updates and controller changes");
});

test("a newer worker or server reported by base.html starts following that version", () => {
    const start = SHIFT.indexOf('window.addEventListener("app-pwa-contract-state"');
    const listener = SHIFT.slice(start, SHIFT.indexOf("window.__driverPwaUpdateRuntime = runtime;", start));
    assert.match(listener, /versionNumber\(workerVersion\) > versionNumber\(runtime\.currentShellVersion\)[\s\S]{0,80}runtime\.followShellVersion\(workerVersion\);/);
    assert.match(listener, /runtime\.renderUpdate\(serverVersion\);\s*runtime\.followShellVersion\(serverVersion\);/);
});
