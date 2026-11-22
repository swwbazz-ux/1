"use strict";
/* T-029 этап C: контракт шаблона панели жилфонда (/clerk/settlement/fund/).
 *
 * Проверяем только разметку и связку файлов:
 *   - экран наследует рабочую базу делопроизводителя и подключает свои CSS/JS;
 *   - в шапке рабочего места есть ссылка «Жилфонд» с активным классом;
 *   - четыре формы шлют свои action, основание обязательно, CSRF на месте;
 *   - поля комнаты и койко-места совпадают с серверными именами;
 *   - собственный фонд не получает управляющих форм (только чтение);
 *   - стили и код не задевают карту КИС-5/6.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const read = (relative) => fs
    .readFileSync(path.resolve(__dirname, relative), "utf8")
    .replace(/\r\n/g, "\n");

const TEMPLATE = read("../../../templates/settlement/clerk_fund.html");
const HEADER = read("../../../templates/includes/clerk_workplace_header.html");
const MAP_TEMPLATE = read("../../../templates/settlement/clerk_map.html");
const CSS = read("../../css/settlement-clerk-fund.css");
const JS = read("../settlement-clerk-fund.js");

function formSection(action) {
    const start = TEMPLATE.indexOf('data-fund-action="' + action + '"');
    assert.notEqual(start, -1, "нет формы действия " + action);
    const end = TEMPLATE.indexOf("</form>", start);
    assert.notEqual(end, -1, "форма " + action + " не закрыта");
    return TEMPLATE.slice(start, end);
}

function count(haystack, needle) {
    return haystack.split(needle).length - 1;
}

test("экран наследует базу делопроизводителя и подключает свои файлы", () => {
    assert.match(TEMPLATE, /^{% extends "clerk\/base.html" %}/);
    assert.match(TEMPLATE, /{% load static %}/);
    assert.match(TEMPLATE, /{% block clerk_extra_head %}/);
    assert.match(TEMPLATE, /{% block clerk_content %}/);
    assert.match(TEMPLATE, /{% block clerk_scripts %}/);
    assert.match(
        TEMPLATE,
        /css\/settlement-clerk-fund\.css[\s\S]{0,80}-settlement-clerk-fund-v1/
    );
    assert.match(
        TEMPLATE,
        /js\/settlement-clerk-fund\.js[\s\S]{0,80}-settlement-clerk-fund-v1/
    );
    assert.equal(count(TEMPLATE, "async "), 0, "скрипт не должен ломать порядок загрузки");
    assert.match(TEMPLATE, /src="{% static 'js\/settlement-clerk-fund\.js' %}[^"]*" defer><\/script>/);
});

test("в шапке рабочего места есть ссылка «Жилфонд» с активным классом", () => {
    assert.match(HEADER, /\{% url 'settlement_fund' %\}/);
    assert.match(
        HEADER,
        /href="{% url 'settlement_fund' %}"[\s\S]{0,80}clerk_active_section == 'fund'/
    );
    assert.match(HEADER, />Жилфонд<\/a>/);
});

test("пять действий панели совпадают с серверным контрактом", () => {
    [
        "create_dormitory",
        "create_room",
        "create_bed",
        "close_dormitory",
        "reopen_dormitory"
    ].forEach((action) => {
        assert.match(TEMPLATE, new RegExp('data-fund-action="' + action + '"'));
        assert.match(
            TEMPLATE,
            new RegExp('<input type="hidden" name="action" value="' + action + '">')
        );
    });
    assert.equal(count(TEMPLATE, "{% csrf_token %}"), 5, "CSRF в каждой форме");
    assert.equal(
        count(TEMPLATE, 'name="reason" maxlength="255"'),
        5,
        "основание обязательно в каждой форме"
    );
    assert.equal(
        count(TEMPLATE, 'name="reason" maxlength="255" autocomplete="off" required>'),
        5,
        "поле основания помечено required"
    );
    assert.equal(count(TEMPLATE, "<form "), 5);
    assert.match(TEMPLATE, /action="{% url 'settlement_fund' %}"/);
});

test("форма арендованного общежития: номер, арендодатель, примечание, основание", () => {
    const section = formSection("create_dormitory");
    assert.match(section, /<input type="hidden" name="kind" value="rented">/);
    assert.match(section, /name="number" maxlength="16"/);
    assert.match(section, /name="contractor_organization" required/);
    assert.match(section, /name="note"/);
    assert.match(section, /{% for organization in contractor_organizations %}/);
    assert.match(
        TEMPLATE,
        /{% if contractor_organizations %}[\s\S]*create_dormitory[\s\S]*{% else %}[\s\S]*settlement-fund-form-blocked/
    );
});

test("форма комнаты: этаж 1–2, вместимость 1–6, сторона, позиция, тип", () => {
    const section = formSection("create_room");
    assert.match(section, /name="dormitory" required/);
    assert.match(section, /name="floor" required/);
    [1, 2].forEach((floor) => {
        assert.match(section, new RegExp('<option value="' + floor + '">' + floor + ' этаж'));
    });
    assert.match(section, /name="number" min="1"/);
    assert.match(section, /name="capacity" required/);
    for (let capacity = 1; capacity <= 6; capacity += 1) {
        assert.match(section, new RegExp('<option value="' + capacity + '">'));
    }
    assert.match(section, /name="corridor_side" required/);
    assert.match(section, /<option value="left">/);
    assert.match(section, /<option value="right">/);
    assert.match(section, /name="side_position" min="1"/);
    assert.match(section, /name="room_type" required/);
    assert.match(section, /<option value="standard">/);
    assert.match(section, /<option value="itr">/);
    assert.match(section, /сразу считается переданной/);
});

test("форма койко-места: комната, блоки А/Б/ИТР, позиции 1–3", () => {
    const section = formSection("create_bed");
    assert.match(section, /name="room" required/);
    assert.match(section, /data-fund-bed-room/);
    assert.match(section, /data-dormitory-id="\{\{ dormitory\.id \}\}"/);
    assert.match(section, /name="block" required/);
    assert.match(section, /<option value="A">А<\/option>/);
    assert.match(section, /<option value="B">Б<\/option>/);
    assert.match(section, /<option value="ITR">ИТР<\/option>/);
    assert.match(section, /name="position" required/);
    [1, 2, 3].forEach((position) => {
        assert.match(section, new RegExp('<option value="' + position + '">' + position + '</option>'));
    });
    assert.match(section, /позиции 1–2/);
});

test("закрытие общежития требует основание и не удаляет фонд", () => {
    const section = formSection("close_dormitory");
    assert.match(section, /name="dormitory" required/);
    assert.match(section, /Закрытие не удаляет комнаты и койко-места/);
});

test("повторное открытие: только закрытый арендованный фонд и основание", () => {
    const section = formSection("reopen_dormitory");
    assert.match(section, /name="dormitory" required/);
    assert.match(section, /Выберите закрытое арендованное общежитие/);
    assert.match(section, /\{% if dormitory\.is_manageable and not dormitory\.is_active %\}/);
    assert.match(section, /name="reason" maxlength="255" autocomplete="off" required>/);
    assert.match(section, /<button type="submit">Открыть общежитие<\/button>/);
    assert.doesNotMatch(section, /is-danger/);
    assert.match(section, /Открытие возвращает общежитие в работу/);
    assert.match(section, /не удаляет комнаты и койко-места/);
    assert.equal(
        count(TEMPLATE, "{% if dormitory.is_manageable and not dormitory.is_active %}"),
        1,
        "закрытые арендованные общежития выбираются только в форме открытия"
    );
});

test("собственный фонд доступен для добавления комнат", () => {
    assert.equal(
        count(TEMPLATE, "{% if dormitory.is_manageable and dormitory.is_active %}"),
        3,
        "остальные списки выбора ограничены арендованными активными общежитиями"
    );
    assert.match(TEMPLATE, /{% if dormitory.can_add_room %}/);
    assert.match(TEMPLATE, /{% if not dormitory.can_add_room and not dormitory.is_manageable %}/);
    assert.match(TEMPLATE, /Только чтение/);
    assert.match(TEMPLATE, /is-\{\{ dormitory\.kind \}\}/);
    assert.match(TEMPLATE, /{% if fund_can_manage %}/);
});

test("панель объясняет границу с картой и авторасселением", () => {
    assert.match(TEMPLATE, /не попадает на карту КИС-5\/6/);
    assert.match(TEMPLATE, /авторасселение в него не селит/);
    assert.match(TEMPLATE, /журнал расселения/);
});

test("сообщения и ошибки сервера выводятся без перерисовки", () => {
    assert.match(TEMPLATE, /{% if fund_message %}/);
    assert.match(TEMPLATE, /is-\{\{ fund_message_level\|default:'info' \}\}/);
    assert.match(TEMPLATE, /{% for error in fund_form_errors %}/);
    assert.match(TEMPLATE, /role="status" aria-live="polite"/);
    assert.match(TEMPLATE, /role="alert"/);
});

test("стили изолированы и опираются на токены рабочего места", () => {
    assert.equal(
        CSS.match(/\.settlement-(?!fund)[a-z-]+/g),
        null,
        "в CSS не должно быть чужих классов .settlement-*"
    );
    assert.doesNotMatch(CSS, /(^|\n)\s*(:root|html|body)\s*\{/);
    assert.doesNotMatch(CSS, /\.settlement-room\b|\.settlement-bed\b|\.settlement-floor\b/);
    assert.match(CSS, /var\(--admin-panel\)/);
    assert.match(CSS, /var\(--admin-ink\)/);
    assert.match(CSS, /var\(--admin-line\)/);
    assert.match(CSS, /\.settlement-fund-page/);
    assert.match(CSS, /@media \(max-width: 900px\)/);
    assert.match(CSS, /overflow-y: auto/);
});

test("скрипт минимален: без innerHTML и без своих запросов", () => {
    assert.match(JS, /document\.querySelector\("\[data-settlement-fund\]"\)/);
    assert.match(JS, /data-fund-bed-dormitory/);
    assert.match(JS, /data-dormitory-id/);
    assert.match(JS, /"settlementFundOpenForm"/);
    assert.match(JS, /data-fund-submitting/);
    assert.doesNotMatch(JS, /innerHTML/);
    assert.doesNotMatch(JS, /fetch\(/);
    assert.doesNotMatch(JS, /["'`]\/clerk\//);
    assert.doesNotMatch(JS, /XMLHttpRequest/);
});

test("карта КИС-5/6 не подключает файлы жилфонда", () => {
    assert.doesNotMatch(MAP_TEMPLATE, /settlement-clerk-fund/);
    const workplace = read("../../../templates/clerk/base.html");
    assert.doesNotMatch(workplace, /settlement-clerk-fund/);
    assert.match(workplace, /settlement-clerk\.css/);
});
