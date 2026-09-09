"use strict";

/* Раскладка плиток самосвалов в карточках комплексов.

   Размер считается от размеров САМОГО поля, а не от гаражной плитки справа:
   раньше плитка 73px заезжала в полосу высотой 10px и её срезал overflow
   карточки, а вёрсткой карточки управлял чужой элемент.

   Раскладка подбирается перебором числа колонок, а не по лестнице
   фиксированных размеров: лестница брала первый подошедший размер и
   останавливалась, поэтому при двенадцати машинах справа оставалась пустая
   колонка шириной в целую плитку.

   Размер плитки один на всю доску — по самому загруженному комплексу, но не
   мельче нижней границы. Иначе рядом оказывались плитки 168px и 70px: поле
   каждой карточки заполнено, но по размеру плиток уже нельзя на глаз
   сравнить загрузку комплексов, а диспетчер смотрит на доску целиком. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const TEMPLATE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "dispatcher_control.html"),
    "utf8"
);
const SCRIPT = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-control-v1.js"),
    "utf8"
);
const CSS = fs.readFileSync(
    path.join(BACKEND, "static", "css", "dispatcher-control-v1.css"),
    "utf8"
);

const GAP = 6;
const MAX = { w: 168, h: 124 };
const MIN = { w: 34, h: 20 };
const FLOOR = { w: 88, h: 63 };
const ASPECT = { min: 1.15, max: 1.55 };
const RICH_MIN_H = 42;

/* Те же формулы, что в refreshComplexTruckRack. */
function tileForGrid(w, h, cols, rows) {
    const cellW = (w - GAP * (cols - 1)) / cols;
    const cellH = (h - GAP * (rows - 1)) / rows;
    if (cellW < MIN.w || cellH < MIN.h) return null;
    let tw = Math.min(cellW, MAX.w);
    let th = Math.min(cellH, MAX.h);
    if (tw / th > ASPECT.max) tw = th * ASPECT.max;
    if (tw / th < ASPECT.min) th = tw / ASPECT.min;
    tw = Math.floor(tw);
    th = Math.floor(th);
    if (tw < MIN.w || th < MIN.h) return null;
    return { w: tw, h: th, cols, rows, area: tw * th };
}

function layout(rackW, rackH, count) {
    let best = null;
    for (let cols = 1; cols <= count; cols += 1) {
        const fit = tileForGrid(rackW, rackH, cols, Math.ceil(count / cols));
        if (!fit) continue;
        if (!best || fit.area > best.area || (fit.area === best.area && fit.rows < best.rows)) best = fit;
    }
    return best;
}

/* Общий размер доски: самый скромный из нужных, но не мельче границы. */
function boardTile(rackW, rackH, counts) {
    let common = null;
    counts.filter((n) => n > 0).forEach((n) => {
        const size = layout(rackW, rackH, n) || MIN;
        if (!common || size.w * size.h < common.w * common.h) common = size;
    });
    if (!common) common = MAX;
    if (common.w * common.h < FLOOR.w * FLOOR.h) common = FLOOR;
    return common;
}

function place(rackW, rackH, size, count) {
    const cols = Math.max(1, Math.floor((rackW + GAP) / (size.w + GAP)));
    const rows = Math.max(1, Math.floor((rackH + GAP) / (size.h + GAP)));
    const capacity = cols * rows;
    const visible = count <= capacity ? count : Math.max(1, capacity - 1);
    return { cols, rows, capacity, visible, hidden: count - visible };
}

/* Поле карточки на боевом окне 1536x886: холст 1921x1108, карточка 590x233. */
const RACK = { w: 372, h: 201 };

function rackFunction(source) {
    const from = source.indexOf("function complexTileForGrid");
    assert.notEqual(from, -1, "функция расчёта плитки не найдена");
    const to = source.indexOf("function watchComplexTruckRacks", from);
    return source.slice(from, to);
}

test("размер плитки считается от поля, а не от гаражной плитки", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        const fn = rackFunction(source);
        assert.match(fn, /rack\.clientWidth/);
        assert.match(fn, /rack\.clientHeight/);
        /* Гараж — чужой элемент; поле больше не должно о нём знать. */
        assert.doesNotMatch(fn, /garage/i);
        assert.doesNotMatch(fn, /--gd-truck-slot/);
    }
});

test("расчёт не выполняется на неразложенном поле", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /width < COMPLEX_TILE_MIN\.w \|\| height < COMPLEX_TILE_MIN\.h/);
    }
});

test("за размером поля следит ResizeObserver", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /complexRackResizeObserver = new ResizeObserver/);
        assert.match(source, /complexRackResizeObserver\.observe\(rack\)/);
    }
});

test("раскладка подбирается перебором колонок, а не по лестнице размеров", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        const fn = rackFunction(source);
        assert.match(fn, /for \(var cols = 1; cols <= count; cols \+= 1\)/);
        assert.match(fn, /fit\.area > best\.area/);
        assert.doesNotMatch(fn, /COMPLEX_TILE_RICH\b/);
    }
});

test("пустое место отдаётся плиткам: двенадцать машин заполняют поле по ширине", () => {
    const fit = layout(RACK.w, RACK.h, 12);
    const used = fit.cols * fit.w + (fit.cols - 1) * GAP;
    assert.ok(used / RACK.w > 0.95, `ширина заполнена на ${Math.round((used / RACK.w) * 100)}%`);
    assert.equal(fit.cols, 4);
    assert.equal(fit.rows, 3);
});

test("плитка не становится вертикальной: коридор пропорций соблюдён", () => {
    for (const n of [1, 2, 3, 4, 5, 6, 8, 12, 16, 20]) {
        const fit = layout(RACK.w, RACK.h, n);
        assert.ok(fit, `нет раскладки для ${n}`);
        const ratio = fit.w / fit.h;
        assert.ok(ratio >= ASPECT.min - 0.01 && ratio <= ASPECT.max + 0.01,
            `${n} машин: пропорция ${ratio.toFixed(2)} вне коридора`);
    }
});

test("размер плитки один на всей доске", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /function refreshAllComplexTruckRacks/);
        assert.match(source, /if \(!common \|\| \(size\.w \* size\.h\) < \(common\.w \* common\.h\)\)/);
        assert.match(source, /applyComplexTruckLayout\(m\.rack, m\.tiles, m\.empty, common/);
    }
});

test("при обычной смене плитки крупные и одинаковые", () => {
    const counts = [6, 6, 6, 5, 0, 6, 6, 1, 6, 5];   // раздача с боевого экрана
    const size = boardTile(RACK.w, RACK.h, counts);
    assert.equal(size.w, 120);
    assert.equal(size.h, 97);
    assert.ok(size.h >= RICH_MIN_H, "подпись состояния должна остаться видимой");
    counts.filter((n) => n > 0).forEach((n) => {
        assert.equal(place(RACK.w, RACK.h, size, n).hidden, 0, `${n} машин должны поместиться`);
    });
});

test("один перегруженный комплекс не мельчит всю доску", () => {
    const counts = [16, 6, 6, 5, 0, 6, 6, 1, 6, 5];
    const size = boardTile(RACK.w, RACK.h, counts);
    assert.equal(size.w, FLOOR.w, "размер не должен уйти ниже границы читаемости");
    assert.equal(size.h, FLOOR.h);
    const crowded = place(RACK.w, RACK.h, size, 16);
    assert.ok(crowded.hidden > 0, "перегруз показывается счётчиком");
    assert.equal(crowded.visible + crowded.hidden, 16);
    assert.equal(place(RACK.w, RACK.h, size, 6).hidden, 0, "обычные карточки не режутся");
});

test("подпись и картинка видны, пока плитка достаточно высокая", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /classList\.toggle\("is-rich-trucks", size\.h >= COMPLEX_TILE_RICH_MIN_H\)/);
    }
    assert.match(CSS, /:not\(\.is-rich-trucks\) \.complex-truck-tile img,[\s\S]*?display: none;/);
    assert.match(CSS, /\.complex-truck-tile img \{[^}]*display: block;/s);
    assert.match(CSS, /\.complex-truck-tile span \{[^}]*display: block;/s);
});

test("номер не лежит поверх картинки", () => {
    assert.match(
        CSS,
        /\.complex-truck-tile > strong,[\s\S]*?\.complex-truck-tile > img,[\s\S]*?\.complex-truck-tile > span \{[^}]*position: static;/s
    );
});

test("карточка разделена на столбец информации и поле машин", () => {
    assert.match(CSS, /grid-template-areas:\s*\n?\s*"head trucks"\s*\n?\s*"info trucks"/);
    assert.match(CSS, /\.complex-assigned-trucks \{[^}]*grid-area: trucks;/s);
    assert.match(CSS, /\.complex-context \{[^}]*grid-area: info;/s);
});

test("разделители не отрываются в начало строки", () => {
    /* Точка ставится через ::after у предыдущего значения; как ::before у
       следующего она повисала в начале строки после переноса. */
    assert.match(CSS, /\.chip-horizon::after \{[^}]*content:/s);
    assert.match(CSS, /\.chip-unload:not\(:last-child\)::after \{[^}]*content:/s);
    assert.doesNotMatch(CSS, /\.chip-block::before \{[^}]*content:/s);
});

test("длинный текст не выдавливает поле машин", () => {
    assert.match(CSS, /\.complex-state-chip[\s\S]*?text-overflow: ellipsis;/);
    assert.match(CSS, /\.complex-context \{[^}]*overflow: hidden;/s);
    assert.match(CSS, /\.dispatcher-complex-card:not\(\.status-empty\) > \* \{[^}]*min-height: 0;/s);
});
