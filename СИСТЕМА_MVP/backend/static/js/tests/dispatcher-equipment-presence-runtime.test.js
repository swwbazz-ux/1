"use strict";

/* Точка присутствия техники на пульте диспетчера.

   Данные (has_current_shift/presence_status/presence_label) уже считает
   equipment_presence_fields() в trips/views.py — она общая с мобильным
   контуром горного мастера и подмешивается в те же словари, что питают
   плитки гаража и карточки комплекса. Здесь только разметка и стили для
   настольных плиток: у .mm-mobile-presence-dot (app.css) вся вёрстка живёт
   внутри @media (max-width: 760px) и на ширине пульта не действует —
   поэтому свой класс .dispatcher-presence-dot с той же палитрой. */

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
    /* Не внутри @media — иначе на ширине пульта действовать не будет. */
    const from = CSS.indexOf(".dispatcher-presence-dot {");
    assert.notEqual(from, -1);
    const upto = CSS.slice(0, from);
    const opens = (upto.match(/\{/g) || []).length;
    const closes = (upto.match(/\}/g) || []).length;
    assert.equal(opens, closes, "правило должно быть на верхнем уровне файла, не внутри @media/блока");
});

test("точка встаёт в левый верхний угол плитки — бейдж циклов плана уже занял правый", () => {
    assert.match(
        CSS,
        /\.dispatcher-excavator-garage-tile \.dispatcher-presence-dot,\s*\n\.dispatcher-truck-tile \.dispatcher-presence-dot \{[^}]*position: absolute;[^}]*top: 3px;[^}]*left: 3px;/s
    );
    assert.match(CSS, /\.dispatcher-plan-loop-badge \{[^}]*top: 3px;[^}]*right: 3px;/s);
});

test("на имени комплекса точка стоит инлайн, как у мобильной карточки", () => {
    assert.match(
        CSS,
        /\.dispatcher-complex-card \.complex-title-state h2 \.dispatcher-presence-dot \{[^}]*position: relative;[^}]*margin-left: 6px;/s
    );
});
