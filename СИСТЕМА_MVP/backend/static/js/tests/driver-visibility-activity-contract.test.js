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
