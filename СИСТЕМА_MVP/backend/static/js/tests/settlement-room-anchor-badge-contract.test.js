"use strict";
/* T-036 frontend-9: read-only бейдж специального фонда на карте делопроизводителя.
   Проверяем контракт шаблона и стилей:
   - бейдж «ВИП-резерв» появляется только при anchor_type=reserve;
   - бейдж «Защищённая» — только при anchor_type=protected;
   - бейджи стоят внутри {% if room.anchor_types %} и без типа не рендерятся;
   - номер комнаты (36/37/38) в шаблоне не зашит: источник — якоря с сервера;
   - стиль бейджа абсолютный и не меняет сетку комнаты;
   - карта несёт версию settlement-map-v39 (поднята T-036 frontend-10
     из-за категории рабочего/ИТР; сам бейдж версию не менял). */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = (relative) =>
    fs.readFileSync(path.resolve(__dirname, relative), "utf8").replace(/\r\n/g, "\n");
const TEMPLATE = read("../../../templates/settlement/clerk_map.html");
const ROOM_CARD = read("../../../templates/settlement/_room_card.html");
const CLERK_CSS = read("../../css/settlement-clerk.css");

const guardStart = ROOM_CARD.indexOf("{% if room.anchor_types %}");
const guardEnd = guardStart === -1 ? -1 : ROOM_CARD.indexOf('<button type="button"', guardStart);
const badgeBlock = guardStart === -1 || guardEnd === -1
    ? ""
    : ROOM_CARD.slice(guardStart, guardEnd);

test("the room card renders anchor badges only behind the anchor_types guard", () => {
    assert.notEqual(guardStart, -1, "ожидается {% if room.anchor_types %}");
    assert.notEqual(guardEnd, -1, "у условия должен быть закрывающий {% endif %}");
    assert.ok(ROOM_CARD.indexOf("ВИП-резерв") > guardStart, "ВИП-резерв внутри условия");
    assert.ok(ROOM_CARD.indexOf("Защищённая") > guardStart, "Защищённая внутри условия");
    assert.ok(ROOM_CARD.indexOf("ВИП-резерв") < guardEnd, "ВИП-резерв не выходит за условие");
    assert.ok(ROOM_CARD.indexOf("Защищённая") < guardEnd, "Защищённая не выходит за условие");
    assert.doesNotMatch(
        ROOM_CARD.slice(0, guardStart),
        /settlement-room-anchor|ВИП-резерв|Защищённая/,
        "до условия бейджей нет"
    );
});

test("reserve maps to ВИП-резерв and protected maps to Защищённая", () => {
    const reserveLine = badgeBlock
        .split("\n")
        .find((line) => line.includes('data-room-anchor="reserve"'));
    const protectedLine = badgeBlock
        .split("\n")
        .find((line) => line.includes('data-room-anchor="protected"'));
    assert.ok(reserveLine, "ожидается бейдж reserve");
    assert.ok(protectedLine, "ожидается бейдж protected");
    assert.match(reserveLine, /"reserve"\s+in\s+room\.anchor_types/);
    assert.match(reserveLine, /ВИП-резерв/);
    assert.doesNotMatch(reserveLine, /Защищённая/);
    assert.match(protectedLine, /"protected"\s+in\s+room\.anchor_types/);
    assert.match(protectedLine, /Защищённая/);
    assert.doesNotMatch(protectedLine, /ВИП-резерв/);
});

test("the badge is anchor-driven, not room-number-driven", () => {
    assert.doesNotMatch(badgeBlock, /room\.number\s*==/);
    assert.doesNotMatch(badgeBlock, /36|37|38/);
    assert.match(badgeBlock, /room\.anchor_types/);
});

test("badge styles are absolute and keep the room grid untouched", () => {
    const anchorRule = CLERK_CSS.split(".settlement-room-anchor {", 2)[1]
        .split("}", 1)[0];
    assert.match(anchorRule, /position:\s*absolute;/);
    assert.doesNotMatch(anchorRule, /grid-template/);
    assert.match(CLERK_CSS, /\.settlement-room-anchor\.is-anchor-reserve \{/);
    assert.match(CLERK_CSS, /\.settlement-room-anchor\.is-anchor-protected \{/);
    // Правило контейнера комнаты не должно получить бейдж-селекторы.
    const roomRule = CLERK_CSS.split(".settlement-room {", 2)[1].split("}", 1)[0];
    assert.doesNotMatch(roomRule, /settlement-room-anchor/);
    assert.match(roomRule, /grid-template-rows:/);
});

test("the map still loads clerk CSS under settlement-map-v39", () => {
    assert.equal(TEMPLATE.split("settlement-map-v39").length - 1, 4);
    assert.equal(TEMPLATE.split("settlement-map-v37").length - 1, 0);
    assert.match(
        read("../../../templates/clerk/base.html"),
        /css\/settlement-clerk\.css[\s\S]{0,90}settlement-map-v39/
    );
});
