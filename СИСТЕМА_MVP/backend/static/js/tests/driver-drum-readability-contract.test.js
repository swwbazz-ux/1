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

test("dump-point tiles in the change-point sheet match the excavator face-settings card look", () => {
    const css = fs.readFileSync(path.resolve(__dirname, "../../css/mobile-dial-actions-v1.css"), "utf8");
    // Тот же плоский вид, что у .eo-unload-card в mobile-face-unified-v1.css: без
    // капслока, без прежнего градиента, зелёная рамка вместо свечения на выбранной.
    assert.match(css, /\.driver-unload-tile\s*\{[^}]*background:\s*rgba\(7, 18, 23, \.96\)/s);
    assert.match(css, /\.driver-unload-tile\s*\{[^}]*text-transform:\s*none/s);
    assert.doesNotMatch(css, /\.driver-unload-tile\s*\{[^}]*linear-gradient/s);
    // app.css задаёт uppercase и свой цвет ПРЯМО на driver-unload-tile strong —
    // наследование текст-transform/color от родителя это не отменяет, нужно
    // явно погасить и на самом strong (пойман на телефоне 26.09.2026).
    assert.match(css, /\.driver-unload-tile strong\s*\{[^}]*text-transform:\s*none/s);
    assert.match(css, /\.driver-unload-tile strong\s*\{[^}]*color:\s*inherit/s);
    assert.match(css, /\.driver-unload-tile\.is-current\s*\{[^}]*border-color:\s*#67e854/s);
    assert.match(css, /\.driver-unload-tile-status\s*\{\s*display:\s*none;/);
});

test("dial label multiline width stays large — the loaded trip's dump point name is read for the whole trip", () => {
    const css = fs.readFileSync(path.resolve(__dirname, "../../css/driver-shift-v1.css"), "utf8");
    // Первая правка узила коробку до 70%/64% ради края круга при коротком сообщении
    // «РАЗГРУЗКА СОХРАНЕНА» — но та же коробка держит и название точки разгрузки,
    // которое видно весь рейс, а не секунду. Пересчитано по запасу до края круга:
    // 85%/80% дают ещё 11–13px запаса и заметно крупнее (пойман на телефоне 26.09.2026).
    assert.match(css, /\.driver-work-label\.is-two-line\s*\{\s*width:\s*85%;\s*max-width:\s*85%/);
    assert.match(css, /\.driver-work-label\.is-three-line\s*\{\s*width:\s*80%;\s*max-width:\s*80%/);
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

test("without an open shift, the top drum stays a real card slot instead of collapsing to nothing", () => {
    // Раньше пустой цикл {% for point in drum_points %} без смены не рисовал вообще
    // ничего — барабан «схлопывался» в пустое место. Теперь пустая грань того же
    // класса .driver-drum-card стоит на месте всегда, просто серая — как барабан
    // простоев без смены. Текст «не назначены точки» — только когда смена ОТКРЫТА,
    // но назначения на экскаватор нет (это другая, осмысленная причина показать
    // предупреждение). Пойман на реальном полевом тесте 26.09.2026.
    assert.match(POINT_TEMPLATE, /\{% empty %\}[\s\S]*?driver-drum-card driver-drum-card-empty/);
    assert.match(POINT_TEMPLATE, /\{% if open_shift %\}[\s\S]*?Экскаватору не назначены точки разгрузки/);
});

test("downtime drum front card is never highlighted yellow without an open shift", () => {
    // is-center — это «эта грань сейчас смотрит на водителя», не «простой идёт»,
    // но выглядит одинаково ярко-жёлто в обоих случаях. Без смены простоя быть не
    // может вообще, поэтому и переднюю грань подсвечивать нечем — иначе водитель
    // видит ровно то, на что жаловался («ОФР горит без смены»). Пойман на реальном
    // полевом тесте 26.09.2026.
    const drumJs = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");
    assert.match(drumJs, /var isCenter = index === front && hasOpenShift\(\);/);
    assert.match(drumJs, /function hasOpenShift\(\)/);
});
