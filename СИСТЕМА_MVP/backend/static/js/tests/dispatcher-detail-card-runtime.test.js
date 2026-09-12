"use strict";

/* Карточка техники/комплекса на пульте диспетчера: раскладка в два столбца,
   блок машиниста со сменой и служебное завершение смены прямо из карточки.

   Сервер отдаёт смену в build_dispatcher_equipment_card() через
   dispatcher_shift_card_payload() (trips/views.py); отправка идёт обычной
   формой на dispatcher_service_close_shift — тем же маршрутом, что и
   «Незакрытые смены» в журнале. Здесь проверяем только разметку, стили и
   склейку в скрипте пульта. */

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
const JS = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-control-v1.js"),
    "utf8"
);
const VIEWS = fs.readFileSync(path.join(BACKEND, "trips", "views.py"), "utf8");

const card = TEMPLATE.slice(
    TEMPLATE.indexOf("data-gd-equipment-detail hidden"),
    TEMPLATE.indexOf("</main>")
);

test("шапка карточки: иконка, имя, статус, паспорт одной строкой и план смены крупно", () => {
    assert.match(card, /<header class="gd-detail-hero">/);
    assert.match(card, /<p class="gd-detail-meta" data-gd-detail-meta><\/p>/);
    assert.match(card, /<div class="gd-detail-hero-plan" data-gd-detail-plan hidden>/);
    assert.match(card, /<strong data-gd-detail-plan-percent>/);
});

test("два столбца: слева люди и действия, справа настройки работы и отчёт смены", () => {
    assert.match(card, /<div class="gd-detail-columns">/);
    const people = card.indexOf("gd-detail-col-people");
    const work = card.indexOf("gd-detail-col-work");
    assert.ok(people > 0 && work > people, "столбцы идут в порядке люди → работа");
    for (const hook of ["data-gd-detail-employee", "data-gd-detail-downtime", "data-gd-detail-trucks", "data-gd-detail-list"]) {
        const at = card.indexOf(hook);
        assert.ok(at > people && at < work, `${hook} должен быть в левом столбце`);
    }
    for (const hook of ["data-gd-detail-settings", "data-gd-destination-list", "data-gd-detail-shift-report"]) {
        assert.ok(card.indexOf(hook) > work, `${hook} должен быть в правом столбце`);
    }
    assert.match(CSS, /\.gd-detail-columns \{[^}]*grid-template-columns: 360px minmax\(0, 1fr\);/s);
    /* На узком окне столбцы складываются в один. */
    const narrow = CSS.indexOf("КАРТОЧКА ТЕХНИКИ / КОМПЛЕКСА НА ПУЛЬТЕ");
    assert.notEqual(narrow, -1);
    assert.match(CSS.slice(narrow), /@media \(max-width: 1180px\) \{[\s\S]*?\.gd-detail-columns \{\s*grid-template-columns: minmax\(0, 1fr\);/);
});

test("все прежние якоря карточки сохранены — скрипт наполняет их как раньше", () => {
    for (const hook of [
        "data-gd-detail-close", "data-gd-detail-icon-slot", "data-gd-detail-type", "data-gd-detail-title",
        "data-gd-detail-status", "data-gd-detail-zone", "data-gd-detail-employee-img",
        "data-gd-detail-employee-initials", "data-gd-detail-employee-name", "data-gd-detail-employee-phone",
        "data-gd-detail-employee-presence", "data-gd-detail-downtime-reason", "data-gd-detail-downtime-started",
        "data-gd-detail-downtime-timer", "data-gd-detail-downtime-close", "data-gd-detail-downtime-result",
        "data-gd-detail-settings-title", "data-gd-detail-settings-hint", "data-gd-detail-settings-status",
        "data-gd-setting-horizon", "data-gd-setting-block", "data-gd-setting-rock", "data-gd-destination-count",
        "data-gd-destination-add", "data-gd-destination-list", "data-gd-setting-save", "data-gd-detail-load-state",
        "data-gd-detail-load-message", "data-gd-detail-retry", "data-gd-detail-metrics", "data-gd-detail-tabs",
        "data-gd-detail-dashboard",
    ]) {
        assert.ok(card.includes(hook), `нет якоря ${hook}`);
    }
});

test("смена машиниста: сведения о смене и форма служебного завершения с csrf и показаниями", () => {
    assert.match(card, /<dl class="gd-detail-crew-shift" data-gd-detail-shift hidden>/);
    for (const hook of ["data-gd-detail-shift-type", "data-gd-detail-shift-opened", "data-gd-detail-shift-presence", "data-gd-detail-shift-seen"]) {
        assert.ok(card.includes(hook), `нет якоря ${hook}`);
    }
    assert.match(card, /<form class="gd-detail-shift-close" data-gd-detail-service-close method="post" action="" hidden>\s*\{% csrf_token %\}/);
    assert.match(card, /<input type="text" name="reason" maxlength="200" required/);
    /* Сервер принимает только целые показания (parse_required_shift_integer /
       validate_driver_close_readings) — форма не должна предлагать дробные. */
    /* Показания необязательны: диспетчер закрывает смену за сотрудника,
       который её не закрыл, и показаний у него обычно нет. */
    assert.match(card, /name="end_fuel" min="0" step="1" inputmode="numeric" placeholder="—">/);
    assert.match(card, /<label data-gd-detail-service-close-mileage hidden>\s*<span>Одометр, км<\/span>\s*<input type="number" name="end_mileage" min="0" step="1" inputmode="numeric" placeholder="—">/);
    assert.match(card, /name="end_engine_hours" min="0" step="1" inputmode="numeric" placeholder="—">/);
    assert.doesNotMatch(card, /name="end_(fuel|mileage|engine_hours)"[^>]*required/);
    assert.match(card, /Причина обязательна, показания — если он их продиктовал\./);
    assert.match(card, /data-gd-detail-service-close-hint hidden/);
    /* Два исхода: «не закрыл сам» одним нажатием, «по согласованию» — форма. */
    assert.match(card, /<input type="hidden" name="close_kind" value="neglected" data-gd-detail-service-close-kind>/);
    /* Подпись кнопки разведена на две строки: действие и пояснение, иначе
       текст ломался в три строки и сминался. */
    assert.match(card, /data-gd-detail-service-close-neglect><strong>Закрыть смену<\/strong><small>сотрудник не закрыл сам и не сообщил<\/small></);
    assert.match(card, /data-gd-detail-service-close-toggle><strong>Закрыть по согласованию<\/strong><small>сотрудник попросил по рации<\/small></);
    assert.match(card, /data-gd-detail-service-close-cancel>Отмена</);
    assert.match(card, /class="gd-detail-shift-close-submit">Закрыть по согласованию</);
    assert.match(card, /<dt>Автозакрытие<\/dt><dd data-gd-detail-shift-autoclose><\/dd>/);
    assert.match(JS, /submitDetailServiceClose\(\s*"neglected",/);
    assert.match(JS, /submitDetailServiceClose\(\s*"coordinated",/);
    assert.match(JS, /detailServiceCloseKind\.value = kind;/);
    assert.match(CSS, /\.gd-detail-shift-close\.is-open \.gd-detail-shift-close-choice \{\s*display: none;/);
});

test("скрипт: смена приходит в карточке, форма получает action, одометр только у самосвала, подтверждение перед отправкой", () => {
    assert.match(JS, /function renderDetailShift\(shift, employee\)/);
    assert.match(JS, /detailServiceClose\.hidden = !shift\.service_close_url;/);
    assert.match(JS, /detailServiceClose\.setAttribute\("action", shift\.service_close_url\)/);
    assert.match(JS, /detailServiceCloseMileage\.hidden = !shift\.is_truck;/);
    assert.match(JS, /var closeLocked = dispatcherRoleIsReadonly\(\) \|\| !dispatcherShiftOpen;/);
    assert.match(JS, /detailServiceCloseNeglect\.disabled = closeLocked;/);
    assert.match(JS, /function renderDetailShiftReadingBounds\(shift\)/);
    assert.match(JS, /hours\.max = String\(Math\.round\(startHours\) \+ 12\);/);
    assert.match(JS, /renderDetailShift\(data\.shift \|\| null, data\.employee \|\| null\);/);
    assert.match(JS, /window\.openAppConfirmDialog\(message, function \(\) \{ detailServiceClose\.submit\(\); \}, 0, "Закрыть смену"/);
    /* Сброс при открытии другой карточки — action и введённое не должны утекать. */
    assert.match(JS, /detailServiceClose\.removeAttribute\("action"\);/);
});

test("скрипт: паспорт уходит в строку под именем, сведения о смене — в блок машиниста, состав — фишками", () => {
    assert.match(JS, /var DETAIL_META_LABELS = \["Экскаватор", "Модель", "ГП, т", "Кузов\/ковш, м3", "Гаражный N"\];/);
    assert.match(JS, /var DETAIL_SHIFT_LABELS = \["Смена", "Смена открыта", "Связь", "Последняя связь", "Приложение", "В составе"\];/);
    assert.match(JS, /if \(DETAIL_META_LABELS\.indexOf\(row\.label\) >= 0\) return;/);
    assert.match(JS, /if \(data\.shift && DETAIL_SHIFT_LABELS\.indexOf\(row\.label\) >= 0\) return;/);
    assert.match(JS, /function renderDetailTrucks\(data\)/);
    assert.match(JS, /function renderDetailPlan\(plan\)/);
});

test("точка связи машиниста и раскрытие формы описаны в стилях пульта, а не в мобильном контуре", () => {
    for (const state of ["is-online", "is-background", "is-recent", "is-offline"]) {
        assert.match(CSS, new RegExp(`\\.gd-detail-crew-presence\\.${state}::before`));
    }
    assert.match(CSS, /\.gd-detail-shift-close\.is-open \.gd-detail-shift-close-toggle \{[^}]*display: none;/s);
    assert.match(CSS, /\.gd-detail-shift-close-hint \{/);
    assert.match(CSS, /\.gd-detail-truck-chips\.is-removed/);
});

test("сервер отдаёт смену в карточке: id, вид техники, связь, показания на начало и маршрут служебного закрытия", () => {
    assert.match(VIEWS, /def dispatcher_shift_card_payload\(shift\):/);
    assert.match(VIEWS, /'service_close_url': reverse\('dispatcher_service_close_shift', args=\[shift\.id\]\),/);
    assert.match(VIEWS, /'is_truck': is_truck,/);
    assert.match(VIEWS, /'start_engine_hours': dispatcher_shift_reading_label\(shift\.start_engine_hours\),/);
    assert.match(VIEWS, /'shift': dispatcher_shift_card_payload\(shift\),/);
    /* Смена передаётся во всех местах сборки карточек: гаражи, комплекс, самосвалы комплекса. */
    const passes = VIEWS.match(/shift=open_shift_by_equipment_id\.get\(/g) || [];
    assert.ok(passes.length >= 4, `ожидали >= 4 передач открытой смены в карточку, нашли ${passes.length}`);
    assert.match(VIEWS, /shift=truck_shift,/);
});
