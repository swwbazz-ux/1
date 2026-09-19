"use strict";
/* Внутри Android-приложения WebView не держит DOM-фокус. Без признака
   «виден — значит активен» realtime-клиент выключает опрос экрана водителя и
   откладывает применение обновлений: экран узнаёт о погрузке только от
   10-секундного нативного пульса, а индикатор связи вечно синий. У экскаваторщика
   признак стоит давно — этот тест не даёт водителю снова его потерять. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");
const read = (relative) => fs.readFileSync(path.join(BACKEND_ROOT, ...relative.split("/")), "utf8");

test("driver and excavator screens both count a visible screen as active", () => {
    const flag = /document\.body\.dataset\.realtimeVisibilityOnly = "true";/;
    assert.match(read("templates/users/driver_shift.html"), flag);
    assert.match(read("templates/trips/excavator_work.html"), flag);
});

test("driver sets the flag before the realtime client reads it", () => {
    const driver = read("templates/users/driver_shift.html");
    const flagAt = driver.indexOf('document.body.dataset.realtimeVisibilityOnly = "true";');
    const shellAt = driver.indexOf("<main class=\"driver-shell\"");
    assert.ok(flagAt >= 0 && shellAt >= 0 && flagAt < shellAt, "признак стоит в начале body, до разметки экрана");
    const client = read("static/js/realtime-client.js");
    assert.match(client, /visibilityOnlyActivity = document\.body\.dataset\.realtimeVisibilityOnly === "true"/);
});

test("driver template has no multi-line {# #} comments — Django renders those as visible text", () => {
    /* 20.09.2026: пояснение к признаку было оформлено многострочным {# … #},
       и Django вывел его на экран водителя как обычный текст. */
    const driver = read("templates/users/driver_shift.html");
    for (const match of driver.matchAll(/\{#([\s\S]*?)#\}/g)) {
        assert.ok(!match[1].includes("\n"), `многострочный комментарий: ${match[1].slice(0, 60)}…`);
    }
});

test("an outdated shell still applies the fragment and reloads at most once per 30 s", () => {
    /* 20.09.2026: пока сервер перезапускался после выкладки, экран перезагружался
       на каждый фрагмент и выбрасывал саму разметку — 14 с слепоты на погрузке. */
    const refresh = read("static/js/driver-shift-refresh-v1.js");
    const block = refresh.slice(refresh.indexOf("loadedShellVersion !== freshShellVersion"), refresh.indexOf("var freshSnapshot"));
    assert.doesNotMatch(block, /return \{deferred: true, reason: "driver_shell_outdated"\}/, "фрагмент больше не выбрасывается");
    assert.match(block, /driver-shell-outdated-reload-at/);
    assert.match(block, /> 30000/);
    assert.match(block, /setTimeout\(function \(\) \{ window\.location\.reload\(\); \}, 1500\)/);
});
