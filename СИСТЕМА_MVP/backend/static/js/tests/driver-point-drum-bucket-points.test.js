"use strict";
/* Infinix v374, 01.10.2026 (матрица C4b): без сети после выбора свободного
   ковша барабан точек оставался с точками основного экскаватора — его рисовал
   только сервер. Погрузка под ковш уходила с чужой точкой, сервер отклонял её
   (free_bucket_work_context_changed). Теперь барабан сам собирает грани из
   каталога ковша (есть на странице и без сети) и возвращается к точкам
   основного экскаватора, когда ковш погашен; пока рейс идёт — не трогается. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const DRUM = fs.readFileSync(path.resolve(__dirname, "../driver-point-drum-v1.js"), "utf8");

function body(signature) {
    const start = DRUM.indexOf(signature);
    assert.notEqual(start, -1, signature);
    return DRUM.slice(start, DRUM.indexOf("\n    }\n", start));
}

test("the drum takes its points from the active free bucket, else from the primary excavator", () => {
    const desired = body("function desiredPoints()");
    assert.match(desired, /bucket && bucket\.active && bucket\.selection/);
    assert.match(desired, /return selection\.dump_points;/);
    assert.match(desired, /api\.readWorkspaceContext\(\)/);
});

test("drum faces are rebuilt only for a different set of points and never while a trip is in the dial", () => {
    const sync = body("function syncPointCards()");
    assert.match(sync, /if \(!c \|\| assignedPointId\(\) !== ""\) return false;/);
    assert.match(sync, /sorted\(signatureOf\(c\)[\s\S]{0,60}=== sorted\(wanted\)\) return false;/);
    assert.match(sync, /c\.appendChild\(makePointCard\(point\)\)/);
    assert.match(body("function refresh()"), /^function refresh\(\) \{\s*syncPointCards\(\);/);
    assert.match(DRUM, /root\.addEventListener\("driver-free-bucket-state-changed", function \(\) \{ refresh\(\); \}\);/);
});

test("a send from the drum carries the point name for the load event and the manifest", () => {
    assert.match(DRUM, /api\.startManualLoadAtPoint\(pointId, card\.dataset\.driverPointName\);/);
});
