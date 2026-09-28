"use strict";
/* Окно свободного ковша должно выдерживать любое число экскаваторов (до десяти
   своих плюс подрядные, владелец 28.09.2026): плитки по две в ряд, на узком экране
   тоже две, но не ниже ~64px высоты; сетка прокручивается внутри окна; заголовок,
   строка состояния и «закрыть» закреплены; выбранная плитка при открытии на виду;
   длинный подрядный номер («ЭКС-ПОДР-12») ужимается по ширине целиком, без
   переноса по буквам. Проверено на телефоне с 18 экскаваторами. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = (relative) => fs.readFileSync(path.resolve(__dirname, relative), "utf8").replace(/\r\n/g, "\n");
const CSS = read("../../css/driver-free-bucket-v1.css");
const JS = read("../driver-free-bucket-v1.js");
const TEMPLATE = read("../../../templates/users/driver_shift.html");

function rule(selector) {
    const match = CSS.match(new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + " \\{([^}]*)\\}"));
    assert.ok(match, "rule " + selector);
    return match[1];
}

test("the grid stays two columns and the tile never shrinks below a finger", () => {
    assert.match(rule("body.driver-mobile-screen .driver-free-bucket-grid"), /grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
    const tile = rule("body.driver-mobile-screen .driver-free-bucket-tile");
    const minHeight = tile.match(/min-height: clamp\((\d+)px, [^,]+, (\d+)px\)/);
    assert.ok(minHeight && Number(minHeight[1]) >= 64, "minimum tile height is at least 64px");
    // Узкий экран больше не переключается на одну колонку.
    const narrow = CSS.match(/@media \(max-width: 350px\) \{([\s\S]*?)\n\}/)[1];
    assert.doesNotMatch(narrow, /grid-template-columns: 1fr/);
});

test("the dialog scrolls inside and its top block is sticky", () => {
    assert.match(rule("body.driver-mobile-screen .driver-free-bucket-dialog"), /overflow: auto/);
    const top = rule("body.driver-mobile-screen .driver-free-bucket-top");
    assert.match(top, /position: sticky/);
    assert.match(top, /top: 0/);
    // Закрыть, заголовок и строка состояния — внутри закреплённого блока.
    const block = TEMPLATE.match(/<div class="driver-free-bucket-top">([\s\S]*?)\n {16}<\/div>/)[1];
    assert.match(block, /data-driver-free-bucket-close/);
    assert.match(block, /driver-free-bucket-head/);
    assert.match(block, /data-driver-free-bucket-current/);
    assert.doesNotMatch(block, /driver-free-bucket-grid/);
});

test("the selected tile is scrolled into view when the window opens", () => {
    const open = JS.match(/function setOpen\(open\) \{[\s\S]*?\n {8}\}\n/)[0];
    assert.match(open, /is-current[\s\S]*?scrollIntoView\(\{block: "center"/);
});

test("the primary-assignment tile is disabled and labelled ОСНОВНОЙ, the selected one glows", () => {
    // Подпись — и в шаблоне, и в JS-сборке плитки; выбор важнее подписи «основной».
    assert.match(TEMPLATE, /\{% elif excavator\.is_primary %\}Основной\{% elif not excavator\.available %\}Недоступно/);
    const label = JS.match(/function tileStatusLabel\(item, selected\) \{[\s\S]*?\n {4}\}\n/)[0];
    assert.match(label, /if \(selected\) return "Выбран";[\s\S]*?is_primary\) return "Основной";[\s\S]*?"Недоступно"/);
    // Плитка основного выключена (как недоступная) — шаблон ставит disabled по is_primary.
    assert.match(TEMPLATE, /\{% if excavator\.is_primary or not excavator\.available %\} disabled aria-disabled="true"/);
    // Выбранная: окантовка полным цветом и свечение снаружи плитки.
    const current = CSS.match(/\.driver-free-bucket-tile\.is-current \{([^}]*)\}/)[1];
    assert.match(current, /--fb-frame: rgb\(var\(--fb-rgb\)\)/);
    assert.match(current, /0 0 0 3px rgb\(var\(--fb-rgb\)\)/);
    assert.match(current, /0 0 36px rgba\(var\(--fb-rgb\), \.7\)/);
    assert.match(current, /color: #ffffff;/);
    // Три состояния читаются сразу: основной — свой спокойный цвет (голубой ручного
    // режима), недоступный — серый, выбранный — цвет ковша.
    const primary = CSS.match(/\.driver-free-bucket-tile:disabled\[data-is-primary="true"\] \{([^}]*)\}/);
    assert.ok(primary, "primary tile has its own rule");
    assert.match(primary[1], /--driver-manual-accent-rgb/);
    // «Закрыть» на одной оси с заголовком: одинаковый верхний отступ и высота 44px.
    const close = CSS.match(/\.driver-free-bucket-close \{([^}]*)\}/)[1];
    const head = CSS.match(/\.driver-free-bucket-head \{([^}]*)\}/)[1];
    assert.match(close, /top: 18px;[\s\S]*right: 18px;[\s\S]*height: 44px;[\s\S]*place-items: center;[\s\S]*padding: 0;/);
    assert.match(head, /align-items: center;[\s\S]*min-height: 44px;/);
    // Крест — SVG из двух линий, не текстовый «×» (тот стоял не по центру кружка).
    assert.match(TEMPLATE, /data-driver-free-bucket-close aria-label="[^"]*"><svg viewBox="0 0 24 24"[^>]*><path d="M6 6 18 18M18 6 6 18"\/><\/svg><\/button>/);
    assert.doesNotMatch(TEMPLATE, /data-driver-free-bucket-close aria-label="[^"]*">×</);
    assert.match(CSS, /\.driver-free-bucket-close svg \{[^}]*display: block;[^}]*width: 20px;[^}]*height: 20px;/);
});

test("a long contractor number is fitted by width, never broken across lines", () => {
    assert.match(TEMPLATE, /style="--fb-len: \{\{ excavator\.label\|length \}\}"/);
    assert.match(JS, /button\.style\.setProperty\("--fb-len"/);
    assert.match(rule("body.driver-mobile-screen .driver-free-bucket-tile"), /container-type: inline-size/);
    const number = rule("body.driver-mobile-screen .driver-free-bucket-tile-number");
    assert.match(number, /white-space: nowrap/);
    assert.match(number, /font-size: min\(clamp\([^)]*\), calc\(\d+cqw \/ \(var\(--fb-len, \d+\) \* 0\.\d+\)\)\)/);
    assert.doesNotMatch(number, /overflow-wrap: anywhere/);
});
