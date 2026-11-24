"use strict";
/* T-031 B3c: вахтенная смена делопроизводителя в панели управления картой.
   Проверяем контракт по исходникам: маршруты берутся из data-атрибутов корня,
   кнопки скрыты и включаются только по состоянию с сервера (can_open /
   can_close / can_handover), причина требуется для приёма вахты, ошибки идут
   в панель управления, после открытия/приёма вахты карта берёт управление.
   Карта (settlement-clerk.js) и карточка койки не менялись. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = (relative) => fs.readFileSync(path.resolve(__dirname, relative), "utf8").replace(/\r\n/g, "\n");
const TEMPLATE = read("../../../templates/settlement/clerk_map.html");
const ROOM_CARD = read("../../../templates/settlement/_room_card.html");
const CSS = read("../../css/settlement-clerk.css");
const JS = read("../settlement-clerk-shift.js");
const MAP_JS = read("../settlement-clerk.js");

test("shift routes come from data attributes of the settlement root", () => {
    [
        "data-clerk-shift-state-url=\"{% url 'settlement_clerk_shift_state' %}\"",
        "data-clerk-shift-open-url=\"{% url 'settlement_clerk_shift_open' %}\"",
        "data-clerk-shift-close-url=\"{% url 'settlement_clerk_shift_close' %}\"",
        "data-clerk-shift-handover-url=\"{% url 'settlement_clerk_shift_handover' %}\""
    ].forEach((attribute) => {
        assert.match(TEMPLATE, new RegExp(attribute.replace(/[{}%[\]()]/g, "\\$&")), attribute);
    });
    assert.match(JS, /root\.dataset\.clerkShiftStateUrl/);
    assert.match(JS, /root\.dataset\.clerkShiftOpenUrl/);
    assert.match(JS, /root\.dataset\.clerkShiftCloseUrl/);
    assert.match(JS, /root\.dataset\.clerkShiftHandoverUrl/);
});

test("shift panel sits inside the existing control panel with three actions", () => {
    const panel = TEMPLATE.slice(
        TEMPLATE.indexOf('class="settlement-control-panel"'),
        TEMPLATE.indexOf('class="settlement-map-mode-panel"')
    );
    assert.ok(panel.length > 0, "control panel markup is present");
    assert.match(panel, /data-clerk-shift/);
    assert.match(panel, /data-clerk-shift-status/);
    assert.match(panel, /data-clerk-shift-watch/);
    assert.match(panel, /data-clerk-shift-open hidden disabled>Открыть смену</);
    assert.match(panel, /data-clerk-shift-close hidden disabled>Закрыть смену</);
    assert.match(panel, /data-clerk-shift-handover hidden disabled>Принять вахту</);
    // Существующие кнопки управления не менялись.
    assert.match(panel, /data-control-acquire disabled>Начать работу</);
    assert.match(panel, /data-control-release disabled>Завершить работу</);
    assert.match(panel, /data-control-feedback/);
});

test("only the actions allowed by the server state are shown", () => {
    assert.match(JS, /function shiftMode\(payload\)/);
    assert.match(JS, /if \(!payload \|\| blockedByWatch\(payload\)\) return "";/);
    assert.match(JS, /openShift && openShift\.is_mine && payload\.can_close\) return "close";/);
    assert.match(JS, /openShift && !openShift\.is_mine && payload\.can_handover\) return "handover";/);
    assert.match(JS, /!openShift && payload\.can_open\) return "open";/);
    assert.match(JS, /setHidden\(openButton, mode !== "open"\);/);
    assert.match(JS, /setHidden\(closeButton, mode !== "close"\);/);
    assert.match(JS, /setHidden\(handoverButton, mode !== "handover"\);/);
    assert.match(JS, /openButton\.disabled = mode !== "open";/);
    assert.match(JS, /closeButton\.disabled = mode !== "close";/);
    assert.match(JS, /handoverButton\.disabled = mode !== "handover";/);
});

test("watchnight status shows the watch, my shift and a foreign open shift", () => {
    assert.match(JS, /"Вахта: " \+ \(period\.name \|\| ""\)/);
    assert.match(JS, /"Смена открыта: " \+ \(openShift\.clerk_name \|\| ""\)/);
    assert.match(JS, /"Смена открыта другим делопроизводителем: "/);
    assert.match(JS, /var text = "Смена не открыта";/);
    assert.match(JS, /blockedByWatch\(state\.payload\)/);
});

test("handover requires a reason and close sends the shift id", () => {
    assert.match(JS, /var reason = askReason\("Причина приёма вахты"\);/);
    assert.match(JS, /if \(!reason\) return;/);
    assert.match(JS, /var value = window\.prompt\(question\);/);
    assert.match(JS, /\{reason: reason\},/);
    assert.match(JS, /shift_id: state\.payload\.open_shift\.id/);
    assert.match(JS, /root\.dataset\.clerkShiftCloseUrl, payload/);
});

test("after open or handover the map is asked to take control if the lease is free", () => {
    assert.match(JS, /function ensureControlLease\(\)/);
    assert.match(JS, /if \(root\.dataset\.controlState === "held"\) return;/);
    assert.match(JS, /if \(!acquireButton\.disabled\) \{\s+acquireButton\.click\(\);/);
    assert.match(JS, /return loadShiftState\(\)\.then\(ensureControlLease\);/);
    assert.match(JS, /if \(lease && lease\.acquired\) \{/);
    // Закрытие смены не тянет acquire за собой.
    const closeSource = JS.match(/function closeShift\(\) \{[\s\S]*?\n    \}\n/)[0];
    assert.doesNotMatch(closeSource, /ensureControlLease/);
});

test("shift errors are shown in the control panel", () => {
    assert.match(JS, /function showMessage\(message, isError\)/);
    assert.match(JS, /controlFeedback\.textContent = message;/);
    assert.match(JS, /error\.httpStatus = response\.status;/);
    assert.match(JS, /if \(error && error\.httpStatus === 409\) \{/);
    assert.match(JS, /showShiftError\(/);
    assert.match(JS, /"Не удалось загрузить состояние смены\."/);
    assert.doesNotMatch(JS, /innerHTML/);
    assert.doesNotMatch(JS, /style\.display/);
});

test("the map script and the room card were not changed for the shift panel", () => {
    assert.doesNotMatch(MAP_JS, /clerkShift|data-clerk-shift/);
    assert.doesNotMatch(ROOM_CARD, /clerk-shift|data-clerk-shift/);
    assert.match(JS, /root\.querySelector\("\[data-control-feedback\]"\)/);
    assert.match(JS, /root\.querySelector\("\[data-control-acquire\]"\)/);
});

test("new styles are additive and the map geometry is untouched", () => {
    assert.match(CSS, /\.settlement-clerk-shift \{/);
    assert.match(CSS, /\.settlement-clerk-shift-copy \{/);
    assert.match(CSS, /\.settlement-clerk-shift-actions button \{/);
    assert.match(CSS, /\.settlement-clerk-shift-actions button\[hidden\]/);
    assert.doesNotMatch(CSS, /--settlement-room-min-height:\s*\d+px/);
    assert.doesNotMatch(CSS, /--settlement-bed-card-min-height:\s*\d+px/);
});

/* T-036: после «одной кнопки» и спецфонда подключаются свои файлы со своими
   версиями (-settlement-one-button-v1, -settlement-special-fund-v1), поэтому
   v39-скриптов по-прежнему четыре, а всего скриптов шесть. */
test("assets stay on v39 with four versioned scripts in load order", () => {
    assert.equal(TEMPLATE.split("settlement-map-v39").length - 1, 4);
    assert.equal(TEMPLATE.split("settlement-map-v37").length - 1, 0);
    const mapIndex = TEMPLATE.indexOf("js/settlement-clerk.js");
    const shiftIndex = TEMPLATE.indexOf("js/settlement-clerk-shift.js");
    const externalIndex = TEMPLATE.indexOf("js/settlement-external-residents.js");
    assert.ok(mapIndex > 0 && shiftIndex > mapIndex, "shift panel loads after the map");
    assert.ok(externalIndex > shiftIndex, "external residents stay last");
    assert.ok(TEMPLATE.indexOf("js/settlement-bed-checkin.js") < mapIndex);
    assert.equal((TEMPLATE.match(/<script src=/g) || []).length, 7);
    assert.equal(
        (TEMPLATE.match(/<script src="[^"]*-settlement-map-v39/g) || []).length,
        4
    );
    assert.match(TEMPLATE, /js\/settlement-clerk-shift\.js[\s\S]{0,90}settlement-map-v39/);
});
