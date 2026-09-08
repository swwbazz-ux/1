"use strict";

/* Контракт масштабирования пульта диспетчера.

   Пульт живёт внутри холста постоянной ВЫСОТЫ (1108 логических точек) и
   целиком масштабируется под окно одним transform: scale(). Ширина холста
   не фиксирована — она считается от пропорции окна, поэтому пульт занимает
   любой монитор целиком, а не подогнан под один конкретный экран.

   Тест сторожит именно те решения, которые дались дорого:
   - никакого zoom и никаких единиц вьюпорта внутри скрипта: vw/vh в уже
     уменьшенном transform-ом элементе продолжают мерить настоящее окно;
   - пересчёт по resize/orientationchange/visualViewport;
   - подстановка --gd-vw/--gd-vh, иначе закреплённые значения разъезжаются;
   - порог телефонного режима не ниже мобильных брейкпоинтов стилей пульта,
     иначе мобильные медиазапросы действуют одновременно с холстом. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const TEMPLATE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "dispatcher_control.html"),
    "utf8"
);
const CSS = fs.readFileSync(
    path.join(BACKEND, "static", "css", "dispatcher-control-v1.css"),
    "utf8"
);
const FIT_SCRIPT = TEMPLATE.slice(
    TEMPLATE.indexOf("var CANVAS_HEIGHT"),
    TEMPLATE.indexOf("fitDispatcherCanvas();")
);

const CANVAS_HEIGHT = 1108;
const CANVAS_WIDTH_MIN = 1400;

function fit(width, height) {
    const canvasWidth = Math.max(
        CANVAS_WIDTH_MIN,
        Math.round(CANVAS_HEIGHT * width / height)
    );
    return {
        canvasWidth,
        scale: Math.min(width / canvasWidth, height / CANVAS_HEIGHT),
    };
}

test("пульт завёрнут в холст и масштабируется одним transform: scale", () => {
    assert.match(TEMPLATE, /<div class="dispatcher-canvas" data-dispatcher-canvas="off">/);
    assert.match(TEMPLATE, /var CANVAS_HEIGHT = 1108;/);
    assert.match(TEMPLATE, /var CANVAS_WIDTH_MIN = 1400;/);
    assert.match(
        CSS,
        /\.dispatcher-canvas\[data-dispatcher-canvas="on"\]\s*\{[^}]*transform: translate\(-50%, -50%\) scale\(var\(--dispatcher-canvas-scale, 1\)\);/s
    );
});

test("ширина холста адаптивная, а не прибитая к одному экрану", () => {
    assert.match(
        FIT_SCRIPT,
        /Math\.max\(\s*CANVAS_WIDTH_MIN,\s*Math\.round\(CANVAS_HEIGHT \* window\.innerWidth \/ window\.innerHeight\)\s*\)/
    );
    assert.match(
        FIT_SCRIPT,
        /Math\.min\(\s*window\.innerWidth \/ canvasWidth,\s*window\.innerHeight \/ CANVAS_HEIGHT\s*\)/
    );
});

test("ни zoom, ни единиц вьюпорта в расчёте масштаба", () => {
    assert.doesNotMatch(FIT_SCRIPT, /\bzoom\b/);
    assert.doesNotMatch(FIT_SCRIPT, /\d+(?:\.\d+)?(?:vh|vw|dvh|dvw|svh|lvh)\b/);
});

test("масштаб пересчитывается на все три события изменения размера", () => {
    assert.match(TEMPLATE, /window\.addEventListener\("resize", fitDispatcherCanvas\)/);
    assert.match(TEMPLATE, /window\.addEventListener\("orientationchange"/);
    assert.match(TEMPLATE, /window\.visualViewport\.addEventListener\("resize", fitDispatcherCanvas\)/);
});

test("JS отдаёт в CSS доли опорного размера вместо vw/vh", () => {
    assert.match(FIT_SCRIPT, /setProperty\("--gd-vw", \(canvasWidth \/ 100\) \+ "px"\)/);
    assert.match(FIT_SCRIPT, /setProperty\("--gd-vh", \(CANVAS_HEIGHT \/ 100\) \+ "px"\)/);
    assert.ok(
        CSS.split("var(--gd-vw)").length - 1 >= 20,
        "закреплённые значения должны опираться на --gd-vw, а не на пиксели"
    );
});

test("телефонная заготовка из app.css внутри холста снята", () => {
    assert.match(
        CSS,
        /\.dispatcher-canvas\[data-dispatcher-canvas="on"\] > \.dispatcher-shell\s*\{[^}]*transform: none;/s
    );
});

test("порог телефонного режима не ниже мобильных брейкпоинтов пульта", () => {
    const gate = TEMPLATE.match(
        /"\(orientation: landscape\) and \(max-width: (\d+)px\)"/
    );
    assert.ok(gate, "телефонный порог должен быть задан явно");
    const gateWidth = Number(gate[1]);
    const breakpoints = [...CSS.matchAll(/@media \(max-width: (\d+)px\)/g)]
        .map((m) => Number(m[1]));
    assert.ok(breakpoints.length > 0);
    const widest = Math.max(...breakpoints);
    assert.ok(
        gateWidth >= widest,
        "порог " + gateWidth + " должен быть не меньше " + widest
    );
});

test("холст выключен у горного мастера на телефоне", () => {
    assert.match(FIT_SCRIPT, /classList\.contains\(\s*"mining-master-mobile-screen"\s*\)/);
    assert.match(FIT_SCRIPT, /setAttribute\("data-dispatcher-canvas", "off"\)/);
});

test("холст занимает окно целиком на любом мониторе", () => {
    const viewports = [
        [1536, 886],
        [1536, 826],
        [1920, 1080],
        [2560, 1400],
        [3440, 1400],
        [1300, 800],
    ];
    for (const [width, height] of viewports) {
        const fitted = fit(width, height);
        assert.ok(
            Math.abs(fitted.canvasWidth * fitted.scale - width) <= 1,
            width + "x" + height + ": холст не заполняет ширину"
        );
        assert.ok(
            Math.abs(CANVAS_HEIGHT * fitted.scale - height) <= 1,
            width + "x" + height + ": холст не заполняет высоту"
        );
    }
});

test("узкое окно упирается в минимальную опорную ширину, а не ломает раскладку", () => {
    const narrow = fit(1200, 1000);
    assert.equal(narrow.canvasWidth, CANVAS_WIDTH_MIN);
    assert.ok(narrow.scale < 1000 / CANVAS_HEIGHT, "по высоте должен остаться запас");
});

test("на опорной пропорции масштаб совпадает с прежним зумом 80%", () => {
    const fitted = fit(1536, 886);
    assert.equal(fitted.canvasWidth, 1921);
    assert.ok(Math.abs(fitted.scale - 0.8) < 0.001);
});

test("диагностическая плашка убрана", () => {
    assert.doesNotMatch(TEMPLATE, /data-dispatcher-fit-badge/);
    assert.doesNotMatch(TEMPLATE, /showDiagnostics/);
});
