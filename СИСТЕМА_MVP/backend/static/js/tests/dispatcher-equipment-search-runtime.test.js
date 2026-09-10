"use strict";

/* Живой поиск техники на пульте диспетчера.

   Поле стоит в строке вкладок шапки только на пульте смены; набранный номер
   подсвечивает машину везде, где она стоит: плитку в комплексе, плитку в
   гараже, карточку комплекса по экскаватору. Скрипт живёт в двух копиях —
   встроенной в шаблон и внешней — обе должны совпадать. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const read = (...parts) => fs.readFileSync(path.join(BACKEND, ...parts), "utf8");

const HEADER = read("templates", "includes", "dispatcher_header.html");
const TEMPLATE = read("templates", "trips", "dispatcher_control.html");
const SCRIPT = read("static", "js", "dispatcher-control-v1.js");
const CSS = read("static", "css", "dispatcher-control-v1.css");

test("поле поиска есть только на пульте смены", () => {
    const from = HEADER.indexOf("dispatcher-equipment-search");
    assert.notEqual(from, -1, "поле поиска не найдено в шапке");
    const guard = HEADER.slice(Math.max(0, from - 400), from);
    assert.match(guard, /dispatcher_nav_active == "control"/);
    assert.match(guard, /not mining_master_mobile_enabled/);
    assert.match(HEADER, /data-dispatcher-equipment-search\b/);
    assert.match(HEADER, /data-dispatcher-equipment-search-count/);
});

test("скрипт поиска одинаков в обеих копиях", () => {
    for (const source of [TEMPLATE, SCRIPT]) {
        assert.match(source, /function bindDispatcherEquipmentSearch\(\)/);
        assert.match(source, /bindDispatcherEquipmentSearch\(\);/);
        /* Совпадение по началу номера — набор сужает круг. */
        assert.match(source, /name\.indexOf\(needle\) === 0/);
        /* Латинская k приводится к кириллической — «k-2» находит К-2. */
        assert.match(source, /replace\(\/k\/g, "к"\)/);
        /* Esc очищает поле. */
        assert.match(source, /event\.key === "Escape"/);
        /* Доска перерисовывается целиком — подсветка возвращается. */
        assert.match(source, /new MutationObserver\(function \(\) \{\s*if \(query === "" \|\| pending\) return;/);
    }
    const pick = (source) => {
        const from = source.indexOf("function bindDispatcherEquipmentSearch");
        return source.slice(from, source.indexOf("bindDispatcherEquipmentSearch();", from));
    };
    assert.equal(pick(TEMPLATE), pick(SCRIPT), "копии скрипта разошлись");
});

test("совпадение мерцает, остальные плитки притушены", () => {
    assert.match(CSS, /@keyframes dispatcher-search-blink/);
    assert.match(CSS, /\.is-search-hit \{[^}]*animation: dispatcher-search-blink/s);
    assert.match(CSS, /\.is-equipment-search \.complex-truck-tile:not\(\.is-search-hit\)/);
    assert.match(CSS, /\.is-equipment-search \.dispatcher-truck-tile:not\(\.is-search-hit\)/);
    /* Карточки комплексов не притушаются — по ним ориентируются. */
    assert.doesNotMatch(CSS, /\.is-equipment-search \.dispatcher-complex-card:not\(\.is-search-hit\)/);
    assert.match(CSS, /prefers-reduced-motion: reduce\) \{\s*body[^{]*\.is-search-hit \{[^}]*animation: none/s);
});

test("вкладки ужаты к левому краю, поле стоит в их строке", () => {
    assert.match(CSS, /\.dispatcher-command-main:has\(\.dispatcher-equipment-search\) \{[^}]*"nav search"/s);
    assert.match(CSS, /\.dispatcher-equipment-search \{[^}]*grid-area: search;/s);
});
