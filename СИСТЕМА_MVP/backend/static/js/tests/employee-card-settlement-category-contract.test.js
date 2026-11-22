"use strict";
/* T-036 frontend-10: перенос отображения «Рабочий / ИТР» из 5fd1bfa1.
 *
 * Проверяем только разметку и клиентский код:
 *   - в карточке сотрудника есть поле «Категория для расселения», заполненное
 *     сервером через form.selected_settlement_category_label;
 *   - employee-card-unified.js перерисовывает подпись при смене кадровой
 *     должности по каталогу (settlement_category_label);
 *   - карточка расселения показывает категорию сотрудника;
 *   - версия скрипта карточки поднята, чтобы браузер не взял старый файл.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = (relative) => fs
    .readFileSync(path.resolve(__dirname, relative), "utf8")
    .replace(/\r\n/g, "\n");

const CARD_TEMPLATE = read("../../../templates/users/employee_card.html");
const CARD_JS = read("../employee-card-unified.js");
const CLERK_JS = read("../settlement-clerk.js");

test("карточка сотрудника показывает категорию для расселения", () => {
    assert.match(CARD_TEMPLATE, /id="employee-settlement-category"/);
    assert.match(
        CARD_TEMPLATE,
        /value="\{\{ form\.selected_settlement_category_label \}\}" readonly/
    );
    assert.match(CARD_TEMPLATE, />Категория для расселения</);
    assert.match(
        CARD_TEMPLATE,
        /data-copy-label="Категория для расселения"/
    );
    // Поле стоит сразу после кадровой должности и до раздела «Работа и учет».
    const positionIndex = CARD_TEMPLATE.indexOf("form.personnel_position }}{{ form.personnel_position.errors");
    const categoryIndex = CARD_TEMPLATE.indexOf('id="employee-settlement-category"');
    const workSectionIndex = CARD_TEMPLATE.indexOf('aria-labelledby="employee-work-title"');
    assert.ok(positionIndex > 0, "кадровая должность на месте");
    assert.ok(categoryIndex > positionIndex, "категория идёт после должности");
    assert.ok(
        workSectionIndex === -1 || workSectionIndex > categoryIndex,
        "категория остаётся в блоке основных сведений"
    );
});

test("версия скрипта карточки поднята", () => {
    assert.match(
        CARD_TEMPLATE,
        /js\/employee-card-unified\.js[\s\S]{0,40}20261122-1/
    );
    assert.equal(
        CARD_TEMPLATE.split("20261122-1").length - 1,
        1
    );
});

test("employee-card-unified.js перерисовывает подпись по каталогу должностей", () => {
    assert.match(
        CARD_JS,
        /var settlementCategory = document\.getElementById\("employee-settlement-category"\);/
    );
    assert.match(
        CARD_JS,
        /settlementCategory\.value = \(config && config\.settlement_category_label\) \|\| "Не указана";/
    );
    // Подпись обновляется внутри той же функции, что и список специализаций.
    const functionIndex = CARD_JS.indexOf("function syncSpecializationOptions()");
    const assignIndex = CARD_JS.indexOf("settlementCategory.value =");
    assert.ok(functionIndex > 0 && assignIndex > functionIndex);
});

test("карточка расселения показывает категорию", () => {
    assert.match(
        CLERK_JS,
        /\["Категория для расселения", employee\.settlement_category_label \|\| "Не указана"\]/
    );
    // Строка стоит в списке сведений сотрудника рядом с должностью.
    const positionIndex = CLERK_JS.indexOf('["Должность", employee.position]');
    const categoryIndex = CLERK_JS.indexOf('"Категория для расселения"');
    assert.ok(positionIndex > 0 && categoryIndex > positionIndex);
});

test("карта берёт новую версию скрипта", () => {
    const mapTemplate = read("../../../templates/settlement/clerk_map.html");
    assert.equal(mapTemplate.split("settlement-map-v39").length - 1, 4);
    assert.equal(mapTemplate.split("settlement-map-v37").length - 1, 0);
    assert.match(
        mapTemplate,
        /js\/settlement-clerk\.js[\s\S]{0,90}settlement-map-v39/
    );
});
