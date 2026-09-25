"use strict";
/* Карточки барабанов (простои, точки разгрузки) на телефоне были нечитаемо мелкими:
   формула кегля не зависела от длины текста и почти всегда садилась на нижний
   предел (12px на карточке 131×92px). Порог — по числу символов, как и у карточек
   точки в ручном режиме (driver-manual-excavator-workspace-v1.js, dumpNameSizeClass).
   Отдельная находка: у подписи и тела карточки не было собственной ширины — грид без
   width сжимался под свой же текст (в 3D-трансформе и контейнерном запросе width:100%
   не решает — резолвится не от карточки, а к ~40% от неё), перенос слов ломался
   посередине слова. Таймер активного простоя (.driver-drum-card-total) — отдельная
   короткая цифровая строка, не завязан на длину названия причины.
   Пойман и проверен на телефоне 26.09.2026. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const CSS = fs.readFileSync(path.resolve(__dirname, "../../css/driver-downtime-drum-v1.css"), "utf8");
const DOWNTIME_TEMPLATE = fs.readFileSync(
    path.resolve(__dirname, "../../../templates/includes/driver_downtime_drum.html"),
    "utf8"
);
const POINT_TEMPLATE = fs.readFileSync(
    path.resolve(__dirname, "../../../templates/includes/driver_point_drum.html"),
    "utf8"
);

test("drum card label and body get an explicit pixel width, not a percentage", () => {
    // width:100% на гриде без явной ширины внутри 3D-карточки резолвился не от
    // карточки — только calc(var(--drum-card-w) - Npx) даёт настоящую ширину.
    assert.doesNotMatch(CSS, /\.driver-drum-card-body\s*\{[^}]*width:\s*100%/);
    assert.doesNotMatch(CSS, /\.driver-drum-card-label\s*\{[^}]*width:\s*100%/);
    assert.match(CSS, /\.driver-drum-card-body\s*\{[^}]*width:\s*calc\(var\(--drum-card-w\) - 20px\)/s);
    assert.match(CSS, /\.driver-drum-card-label\s*\{[^}]*width:\s*calc\(var\(--drum-card-w\) - 20px\)/s);
});

test("drum label font size scales by text-length tier, short reads much larger than the old flat cap", () => {
    assert.match(CSS, /\.driver-drum-card-label\s*\{[^}]*font-size:\s*clamp\(12px, calc\(var\(--drum-card-h\) \* 0\.28\), 32px\)/s);
    assert.match(CSS, /\.driver-drum-card-label\.is-drum-label-medium\s*\{\s*font-size:\s*clamp\(12px, calc\(var\(--drum-card-h\) \* 0\.20\), 24px\)/);
    assert.match(CSS, /\.driver-drum-card-label\.is-drum-label-long\s*\{\s*font-size:\s*clamp\(11px, calc\(var\(--drum-card-h\) \* 0\.13\), 18px\)/);
});

test("active-downtime timer is not tied to the reason-name tier and reads clearly on its own", () => {
    // Таймер простоя — всегда короткая цифровая строка независимо от того, как длинно
    // называется причина; раньше кегль совпадал с самой мелкой подписью (≤18px).
    assert.doesNotMatch(CSS, /\.driver-drum-card-total\.is-drum-label-medium/);
    assert.doesNotMatch(CSS, /\.driver-drum-card-total\.is-drum-label-long/);
    assert.match(CSS, /\.driver-drum-card-total\s*\{[^}]*font-size:\s*clamp\(14px, calc\(var\(--drum-card-h\) \* 0\.20\), 22px\)/s);
});

test("templates classify labels by character length, matching CSS tiers", () => {
    assert.match(
        DOWNTIME_TEMPLATE,
        /driver-drum-card-label\{% if reason\.button_label\|length > 14 %\} is-drum-label-long\{% elif reason\.button_label\|length > 6 %\} is-drum-label-medium\{% endif %\}/
    );
    assert.match(
        POINT_TEMPLATE,
        /driver-drum-card-label\{% if point\.name\|length > 14 %\} is-drum-label-long\{% elif point\.name\|length > 6 %\} is-drum-label-medium\{% endif %\}/
    );
});
