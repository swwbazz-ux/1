"use strict";
/* T-025a: окно «Внешние жильцы» — только поиск и создание карточки.
   Проверяем контракт шаблона, JS и CSS по исходникам: ручного заселения
   внешнего в окне нет и быть не должно (сервер отвечает
   settlement.manual.external_unavailable), строки результатов не кликабельны,
   поиск уходит POST-телом, Esc перехватывается в capture-фазе. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = (relative) => fs.readFileSync(path.resolve(__dirname, relative), "utf8").replace(/\r\n/g, "\n");
const TEMPLATE = read("../../../templates/settlement/clerk_map.html");
const CSS = read("../../css/settlement-clerk.css");
const JS = read("../settlement-external-residents.js");

const MODAL_ID = "settlement-external-residents-modal";
const modalRegion = TEMPLATE.slice(
    TEMPLATE.indexOf('<section class="settlement-external-residents-modal"'),
    TEMPLATE.indexOf('<aside class="settlement-unsettled-panel"')
);

test("toolbar has the external residents trigger wired to the dialog", () => {
    assert.ok(modalRegion.length > 0, "modal markup is present");
    assert.match(
        TEMPLATE,
        /class="settlement-external-residents-trigger"\s+data-external-residents-toggle\s+aria-controls="settlement-external-residents-modal"\s+aria-haspopup="dialog"\s+aria-expanded="false"/
    );
    assert.match(TEMPLATE, /data-external-residents-toggle[\s\S]{0,200}Внешние жильцы/);
    // Вторичная кнопка панели инструментов стоит после «Нерасселённые».
    assert.ok(
        TEMPLATE.indexOf("data-unsettled-panel-toggle") < TEMPLATE.indexOf("data-external-residents-toggle"),
        "trigger follows the unsettled toggle"
    );
});

test("dialog carries modal semantics and the registry hint", () => {
    assert.match(modalRegion, new RegExp(`id="${MODAL_ID}"`));
    assert.match(modalRegion, /role="dialog"/);
    assert.match(modalRegion, /aria-modal="true"/);
    assert.match(modalRegion, /aria-labelledby="settlement-external-residents-title"/);
    assert.match(modalRegion, /aria-describedby="settlement-external-residents-registry-hint"/);
    assert.match(modalRegion, /id="settlement-external-residents-registry-hint"/);
    // T-044: подсказка описывает фактический путь заселения, а не «реестр».
    assert.match(modalRegion, /нажмите свободную койку на карте/);
    assert.match(modalRegion, /будущего приезда выберите «Подготовить место»/);
    assert.doesNotMatch(modalRegion, /через утверждённый реестр заезда/);
});

test("dialog never offers manual settlement of an external resident", () => {
    assert.doesNotMatch(modalRegion, /<button[^>]*>\s*(Заселить|Переселить|Выбрать для заселения)/);
    assert.doesNotMatch(modalRegion, /data-settle-button|data-relocate-button|data-release-button|data-assignment-type-select/);
    assert.doesNotMatch(JS, /settlement_occupancy_create|occupancyCreateUrl|data-settle-button/);
});

test("search results show the personnel number and open the resident card", () => {
    const rowBuilder = JS.match(/function buildResultRow\(resident\) \{[\s\S]*?\n    \}/)[0];
    assert.match(rowBuilder, /element\("article", "settlement-external-residents-result"\)/);
    assert.match(rowBuilder, /resident\.personnel_number/);
    assert.match(rowBuilder, /settlement:open-resident-card/);
    assert.doesNotMatch(rowBuilder, /fetch\(|occupancyCreateUrl/);
    assert.match(JS, /function residentMeta\(resident\)/);
    // Телефон показывается, фото и аудит — нет.
    assert.match(JS, /resident\.phone/);
    assert.doesNotMatch(JS, /photo|created_by_access|revision/);
});

test("search is POST with a JSON body, no query string, debounce and 2-char minimum", () => {
    assert.match(JS, /root\.dataset\.externalResidentSearchUrl/);
    assert.match(JS, /window\.setTimeout\(runSearch, 220\)/);
    assert.match(JS, /if \(query\.length < 2\)/);
    assert.match(JS, /method: "POST"/);
    assert.match(JS, /body: JSON\.stringify\(\{q: query\}\)/);
    assert.doesNotMatch(JS, /\?q=/);
    assert.match(JS, /setCount\("Найдено: " \+ residents\.length\)/);
});

test("create posts resident fields and handles duplicate confirmation", () => {
    assert.match(JS, /root\.dataset\.externalResidentCreateUrl/);
    ["resident_type", "full_name", "position_title", "organization", "phone", "sex"].forEach((field) => {
        assert.match(JS, new RegExp(`${field}: "`), `${field} is read from the form`);
    });
    assert.match(JS, /body\.requires_confirmation/);
    assert.match(JS, /confirm_duplicate: Boolean\(confirmDuplicate\)/);
    assert.match(TEMPLATE, /data-external-residents-confirm-duplicate>Всё равно создать</);
    assert.match(JS, /showDuplicates\(body\.duplicate_candidates \|\| \[\]\)/);
});

test("control (lease) loss is shown without inventing client-side lease state", () => {
    assert.match(JS, /root\.dataset\.controlState === "held"/);
    assert.match(JS, /String\(body\.code \|\| ""\)\.indexOf\("settlement\.control\."\) === 0/);
    assert.match(JS, /setControlLocked\(body\.error \|\| "Управление расселением не активно\."\)/);
    assert.match(TEMPLATE, /Создание доступно при активном управлении\. Нажмите «Начать работу» на карте\./);
    assert.match(TEMPLATE, /data-external-residents-start-work/);
});

test("esc is captured before the map handler and focus returns to the trigger", () => {
    assert.match(JS, /document\.addEventListener\("keydown", function \(event\) \{[\s\S]*?\n    \}, true\);/);
    assert.match(JS, /event\.preventDefault\(\);\s+event\.stopPropagation\(\);/);
    assert.match(JS, /trigger\.focus\(\)/);
    assert.match(JS, /event\.key === "Tab"\) trapFocus\(event\)/);
    assert.match(JS, /settlement-external-residents-modal-open/);
});

test("csrf token is taken from the dialog form, not from the placement form", () => {
    assert.match(JS, /form\.querySelector\("input\[name='csrfmiddlewaretoken'\]"\)/);
    assert.doesNotMatch(JS, /settlementForm/);
    assert.match(TEMPLATE, /data-external-residents-create-form[\s\S]{0,120}\{% csrf_token %\}/);
});

test("urls come from data attributes of the settlement root", () => {
    assert.match(TEMPLATE, /data-external-resident-search-url="\{% url 'settlement_external_resident_search' %\}"/);
    assert.match(TEMPLATE, /data-external-resident-create-url="\{% url 'settlement_external_resident_create' %\}"/);
});

test("map assets moved to v39 and the new files are connected after the map script", () => {
    assert.equal(TEMPLATE.split("settlement-map-v39").length - 1, 4);
    assert.equal(TEMPLATE.split("settlement-map-v37").length - 1, 0);
    assert.ok(
        TEMPLATE.indexOf("js/settlement-clerk.js") < TEMPLATE.indexOf("js/settlement-external-residents.js"),
        "new script loads after settlement-clerk.js"
    );
    assert.match(TEMPLATE, /js\/settlement-external-residents\.js[\s\S]{0,90}settlement-map-v39/);
});

test("styles exist for the dialog, narrow screen and scroll lock", () => {
    assert.match(CSS, /\.settlement-external-residents-trigger \{/);
    assert.match(CSS, /\.settlement-external-residents-modal \{[\s\S]*?width: min\(720px, calc\(100vw - 32px\)\)/);
    assert.match(CSS, /max-height: calc\(100dvh - 48px\)/);
    assert.match(CSS, /@media \(max-width: 720px\) \{[\s\S]*?\.settlement-external-residents-modal \{[\s\S]*?width: calc\(100vw - 20px\)/);
    assert.match(CSS, /body\.settlement-external-residents-modal-open \{\s*overflow: hidden;/);
    assert.match(CSS, /\.settlement-external-residents-modal\[hidden\][\s\S]{0,400}display: none !important;/);
});
