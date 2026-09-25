"use strict";

/* Контракт живого поиска техники на desktop-пульте.

   Поле находится в общей шапке, поведение — в модуле доски, оформление —
   в адаптивном слое. Проверка нужна именно на стыке этих файлов: production
   долго сохранял поле из отдельной выкладки, хотя в релизной ветке разметка
   отсутствовала и локальный пульт молча терял быстрый набор номера. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {dispatcherStyleSource} = require("./dispatcher-style-source");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const read = (...parts) => fs.readFileSync(path.join(BACKEND, ...parts), "utf8");

const HEADER = read("templates", "includes", "dispatcher_header.html");
const BOARD_RUNTIME = read("static", "js", "dispatcher-board-v1.js");
const STYLES = dispatcherStyleSource();
const PRODUCTION_MANIFEST = fs.readFileSync(
    path.resolve(BACKEND, "..", "..", ".github", "deploy", "production-files.txt"),
    "utf8"
);

test("поле поиска есть только на desktop-пульте смены и входит в релиз", () => {
    const from = HEADER.indexOf("dispatcher-equipment-search");
    assert.notEqual(from, -1, "поле поиска не найдено в шапке");
    const guard = HEADER.slice(Math.max(0, from - 500), from);
    assert.match(guard, /dispatcher_nav_active == "control"/);
    assert.match(guard, /not mining_master_mobile_enabled/);
    assert.match(HEADER, /data-dispatcher-equipment-search\b/);
    assert.match(HEADER, /data-dispatcher-equipment-search-count/);
    assert.match(HEADER, /maxlength="3"/);
    assert.doesNotMatch(
        HEADER,
        /data-dispatcher-equipment-search[^>]*placeholder="[^"]+"/
    );
    assert.match(
        PRODUCTION_MANIFEST,
        /templates\/includes\/dispatcher_header\.html/
    );
});

test("поиск ловит набор номера без предварительного фокуса", () => {
    assert.match(BOARD_RUNTIME, /function bindDispatcherEquipmentSearch\(\)/);
    assert.match(BOARD_RUNTIME, /bindDispatcherEquipmentSearch\(\);/);
    assert.match(BOARD_RUNTIME, /name\.indexOf\(needle\) === 0/);
    assert.match(BOARD_RUNTIME, /replace\(\/k\/g, "к"\)/);
    assert.match(BOARD_RUNTIME, /event\.key === "Escape"/);
    assert.match(
        BOARD_RUNTIME,
        /document\.addEventListener\("keydown", function \(event\) \{\s*if \(event\.defaultPrevented \|\| event\.ctrlKey/
    );
    assert.match(
        BOARD_RUNTIME,
        /key\.length === 1 && \/\[0-9a-zа-яё\\-\]\/i\.test\(key\)/
    );
    assert.match(BOARD_RUNTIME, /isTypingElsewhere\(\) \|\| isDialogOpen\(\)/);
    assert.match(
        BOARD_RUNTIME,
        /document\.addEventListener\("pointerdown", function \(event\) \{\s*if \(query === "" \|\| box\.contains\(event\.target\)\) return;\s*clearEquipmentSearch\(\);/
    );
    assert.match(
        BOARD_RUNTIME,
        /new MutationObserver\(function \(\) \{\s*if \(query === "" \|\| pending\) return;/
    );
});

test("совпадение заметно, а статусная окраска карточки сохраняется", () => {
    assert.match(STYLES, /@keyframes dispatcher-search-blink/);
    assert.match(STYLES, /\.is-search-hit \{[^}]*animation: dispatcher-search-blink/s);
    assert.match(
        STYLES,
        /\.is-equipment-search \.complex-truck-tile:not\(\.is-search-hit\)/
    );
    assert.match(
        STYLES,
        /\.is-equipment-search \.dispatcher-truck-tile:not\(\.is-search-hit\)/
    );
    assert.doesNotMatch(
        STYLES,
        /\.is-equipment-search \.dispatcher-complex-card:not\(\.is-search-hit\)/
    );
    assert.match(STYLES, /prefers-reduced-motion: reduce\) \{\s*body[^{]*\.is-search-hit \{[^}]*animation: none/s);
});
