"use strict";

/* Точка присутствия техники и заливка плана на пульте диспетчера.

   Данные (has_current_shift/presence_status/presence_label, --tile-progress)
   уже считает equipment_presence_fields()/dispatcher_plan_for_equipment() в
   trips/views.py — общие с мобильным контуром горного мастера. Здесь только
   разметка и стили для настольных плиток. */

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

test("точка стоит на гараже экскаваторов, гараже самосвалов, самосвалах комплекса и имени комплекса", () => {
    assert.match(
        TEMPLATE,
        /\{% if not tile\.is_placeholder and tile\.has_current_shift %\}<i class="dispatcher-presence-dot is-\{\{ tile\.presence_status \}\}"/
    );
    assert.match(
        TEMPLATE,
        /\{% if truck\.has_current_shift %\}<i class="dispatcher-presence-dot is-\{\{ truck\.presence_status \}\}"/
    );
    assert.match(
        TEMPLATE,
        /\{% if tile\.has_current_shift %\}<i class="dispatcher-presence-dot is-\{\{ tile\.presence_status \}\}"/
    );
    assert.match(
        TEMPLATE,
        /<h2>\{\{ complex\.id \}\}\{% if not mining_master_mobile_enabled and complex\.has_current_shift %\}<i class="dispatcher-presence-dot is-\{\{ complex\.presence_status \}\}"/
    );
});

test("своя палитра и размер точки, не завязанные на мобильный media-запрос", () => {
    assert.match(CSS, /\.dispatcher-presence-dot \{[^}]*width: 8px;[^}]*height: 8px;/s);
    for (const state of ["is-online", "is-background", "is-recent", "is-offline", "is-not_registered"]) {
        assert.match(CSS, new RegExp(`\\.dispatcher-presence-dot\\.${state} \\{`));
    }
    /* Не внутри @media — иначе на ширине пульта действовать не будет
       (.mm-mobile-presence-dot из app.css вся живёт внутри
       @media (max-width: 760px), нарисованного под мобильный контур). */
    const from = CSS.indexOf(".dispatcher-presence-dot {");
    assert.notEqual(from, -1);
    const upto = CSS.slice(0, from);
    const opens = (upto.match(/\{/g) || []).length;
    const closes = (upto.match(/\}/g) || []).length;
    assert.equal(opens, closes, "правило должно быть на верхнем уровне файла, не внутри @media/блока");
});

test("точка стоит в правом верхнем углу плитки — бейдж циклов плана сдвинут вниз, чтобы не спорить с ней", () => {
    assert.match(
        CSS,
        /\.dispatcher-excavator-garage-tile \.dispatcher-presence-dot,\s*\n\.dispatcher-truck-tile \.dispatcher-presence-dot \{[^}]*position: absolute;[^}]*top: 3px;[^}]*right: 3px;/s
    );
    /* Экскаваторный гараж и без того держал бейдж на 17px — теперь то же
       и у самосвалов (гараж и внутри комплекса), угол 3/3 остаётся точке. */
    assert.match(CSS, /\.dispatcher-excavator-garage-tile \.dispatcher-plan-loop-badge \{[^}]*top: 17px;/s);
    assert.match(CSS, /\.dispatcher-truck-tile \.dispatcher-plan-loop-badge \{[^}]*top: 17px;/s);
    assert.match(CSS, /\.complex-truck-tile \.dispatcher-plan-loop-badge \{[^}]*top: 17px;/s);
});

test("на имени комплекса точка стоит инлайн, по центру строки", () => {
    assert.match(
        CSS,
        /\.dispatcher-complex-card \.complex-title-state h2 \.dispatcher-presence-dot \{[^}]*vertical-align: middle;/s
    );
});

test("заливка плана самосвала — на заднем плане, во весь контур, как у горного мастера, а не тонким кольцом поверх", () => {
    /* mm-mobile-truck-card .mm-mobile-plan-ring-layer тоже без маски
       (mask: none) — та же идея: не тонкая кайма, а заливка всей плитки.
       На пульте фон обязан быть НИЖЕ содержимого (отрицательный z-index),
       иначе номер и иконку самосвала будет перекрывать цветом. */
    const from = CSS.indexOf(".dispatcher-truck-tile::before {");
    assert.notEqual(from, -1);
    const rule = CSS.slice(from, CSS.indexOf("}", from) + 1);
    assert.match(rule, /z-index: -1;/);
    assert.match(rule, /conic-gradient\(/);
    assert.doesNotMatch(rule, /mask/);
});

test("заливка начинается сверху и растёт по часовой — не с левого края", () => {
    /* conic-gradient по умолчанию (и с "from -90deg", который тут был
       раньше) отсчитывает 0% от 9 часов — при малом проценте это читалось
       как зелёная полоса у левого края плитки, а не рост от верхней точки.
       Общее с .dispatcher-excavator-garage-tile — рамка гаражной плитки
       экскаватора использует то же правило. */
    const from = CSS.indexOf(
        '.dispatcher-excavator-garage-tile[data-plan-progress-phase]:not([data-plan-progress-phase=""])::before,\n.dispatcher-truck-tile[data-plan-progress-phase]'
    );
    assert.notEqual(from, -1);
    const rule = CSS.slice(from, CSS.indexOf("}", from) + 1);
    assert.match(rule, /from 0deg/);
    assert.doesNotMatch(rule, /from -90deg/);
});
