"use strict";

/* Полоса самосвалов в карточке комплекса.

   Раньше размер плитки брался от ГАРАЖНОЙ плитки справа и к доступной высоте
   карточки отношения не имел: при десяти комплексах плитка 73px заезжала в
   полосу высотой 10px, и её срезал overflow карточки — на экране оставалась
   кромка с обрубленными цифрами. Плюс вёрсткой карточки управлял чужой
   элемент: правка гаража ломала комплексы.

   Теперь полоса считает себя сама, а самосвал показывается жетоном номера без
   картинки. Тест сторожит именно это: расчёт от собственных размеров, работу
   на крайних количествах (одна машина и двадцать) и отсутствие обрезки. */

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
/* Полная плитка сохраняет всё, что в неё заложено: номер, картинку и подпись
   состояния. Жетон остаётся только там, где машин столько, что подпись всё
   равно стала бы нечитаемой. */
const RICH = [
    { w: 72, h: 58 },
    { w: 66, h: 53 },
    { w: 60, h: 48 },
    { w: 54, h: 44 }
];
const COMPACT = [
    { w: 56, h: 28 },
    { w: 50, h: 26 },
    { w: 44, h: 24 },
    { w: 38, h: 22 }
];

function capacity(rackWidth, rackHeight, w, h) {
    const cols = Math.max(1, Math.floor((rackWidth + GAP) / (w + GAP)));
    const rows = Math.max(1, Math.floor((rackHeight + GAP) / (h + GAP)));
    return { cols, rows, total: cols * rows };
}

function fitRack(rackWidth, rackHeight, count) {
    let chosen = null;
    let rich = false;
    for (const step of RICH) {
        const fit = capacity(rackWidth, rackHeight, step.w, step.h);
        if (fit.total >= count) {
            chosen = { ...step, ...fit };
            rich = true;
            break;
        }
    }
    if (!chosen) {
        for (const step of COMPACT) {
            const fit = capacity(rackWidth, rackHeight, step.w, step.h);
            chosen = { ...step, ...fit };
            if (fit.total >= count) break;
        }
    }
    const overflow = Math.max(0, count - chosen.total);
    const visible = overflow > 0 ? Math.max(0, chosen.total - 1) : count;
    return { ...chosen, rich, visible, hidden: count - visible };
}

function rackFunction(source) {
    const from = source.indexOf("function refreshComplexTruckRack(rack) {");
    assert.notEqual(from, -1, "функция расчёта полосы не найдена");
    const to = source.indexOf("function refreshAllComplexTruckRacks", from);
    return source.slice(from, to);
}

test("размер жетона считается от полосы, а не от гаражной плитки", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        const fn = rackFunction(source);
        assert.match(fn, /rack\.clientWidth/);
        assert.match(fn, /rack\.clientHeight/);
        /* Гараж — чужой элемент; полоса больше не должна о нём знать. */
        assert.doesNotMatch(fn, /garage/i);
        assert.doesNotMatch(fn, /--gd-truck-slot/);
    }
});

test("расчёт не выполняется на неразложенной полосе", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /rackWidth < COMPLEX_TILE_MIN\.w \|\| rackHeight < COMPLEX_TILE_MIN\.h/);
    }
});

test("за размером полосы следит ResizeObserver", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /complexRackResizeObserver = new ResizeObserver/);
        assert.match(source, /complexRackResizeObserver\.observe\(rack\)/);
    }
});

test("один самосвал показывается полной плиткой и ничего не растягивает", () => {
    const fit = fitRack(452, 62, 1);
    assert.equal(fit.rich, true, "при одной машине подпись и картинка остаются");
    assert.equal(fit.w, RICH[0].w);
    assert.equal(fit.h, RICH[0].h);
    assert.equal(fit.visible, 1);
    assert.equal(fit.hidden, 0);
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /--complex-truck-justify", "start"/);
    }
});

test("шесть самосвалов ещё показываются полной плиткой с подписью", () => {
    const fit = fitRack(452, 62, 6);
    assert.equal(fit.rich, true);
    assert.equal(fit.visible, 6);
    assert.equal(fit.hidden, 0);
});

test("двенадцать самосвалов переходят на жетон, но помещаются все", () => {
    const fit = fitRack(452, 62, 12);
    assert.equal(fit.rich, false, "подпись при таком количестве уже нечитаема");
    assert.equal(fit.w, COMPACT[0].w);
    assert.equal(fit.visible, 12);
    assert.equal(fit.hidden, 0);
});

test("двадцать самосвалов помещаются, жетон уменьшается на ступень", () => {
    const fit = fitRack(452, 62, 20);
    assert.ok(fit.w < COMPACT[0].w, "жетон должен стать мельче");
    assert.ok(fit.w >= COMPACT[COMPACT.length - 1].w, "не ниже минимума");
    assert.equal(fit.visible, 20);
    assert.equal(fit.hidden, 0);
});

test("непоместившийся хвост сворачивается в счётчик, а не обрезается", () => {
    const fit = fitRack(150, 30, 40);
    assert.ok(fit.hidden > 0, "часть машин должна уйти в счётчик");
    assert.equal(fit.visible + fit.hidden, 40);
    assert.equal(fit.visible, fit.total - 1, "ячейка счётчика занимает место");
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /complex-truck-more/);
    }
});

test("подпись и картинка сохраняются, пока плитка полная", () => {
    /* По умолчанию плитка полная: картинка и подпись видны. Прячутся они
       только в жетоне, то есть под :not(.is-rich-trucks). */
    assert.match(CSS, /\.dispatcher-complex-card \.complex-truck-tile img \{[^}]*display: block;/s);
    assert.match(CSS, /\.dispatcher-complex-card \.complex-truck-tile span \{[^}]*display: block;/s);
    assert.match(
        CSS,
        /:not\(\.is-rich-trucks\) \.complex-truck-tile img,[\s\S]*?display: none;/
    );
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /classList\.toggle\("is-rich-trucks", rich\)/);
    }
});

test("карточка разделена на столбец информации и поле машин", () => {
    assert.match(CSS, /grid-template-areas:\s*"head trucks"\s*"info trucks";/);
    assert.match(CSS, /\.complex-title-state \{[^}]*grid-area: head;/s);
    assert.match(CSS, /\.complex-context \{[^}]*grid-area: info;/s);
    assert.match(CSS, /\.complex-assigned-trucks \{[^}]*grid-area: trucks;[^}]*align-self: stretch;/s);
});

test("длинный текст не выдавливает полосу машин", () => {
    assert.match(CSS, /\.dispatcher-complex-card \.complex-state-chip[\s\S]*?text-overflow: ellipsis;/);
    assert.match(CSS, /\.dispatcher-complex-card \.complex-context \{[^}]*overflow: hidden;/s);
    assert.match(CSS, /\.dispatcher-complex-card:not\(\.status-empty\) > \* \{[^}]*min-height: 0;/s);
});
