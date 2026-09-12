"use strict";

/* Карточка самосвала на пульте: ручной рейс диспетчера и метка «чья смена».

   Сервер считает вердикт в dispatcher_shift_period_fields() и собирает
   данные ручного рейса в dispatcher_manual_trip_payload() (trips/views.py,
   проверены в trips/test_dispatcher_manual_trip.py). Здесь — разметка,
   стили и склейка в скрипте пульта. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const TEMPLATE = fs.readFileSync(path.join(BACKEND, "templates", "trips", "dispatcher_control.html"), "utf8");
const CSS = fs.readFileSync(path.join(BACKEND, "static", "css", "dispatcher-control-v1.css"), "utf8");
const JS = fs.readFileSync(path.join(BACKEND, "static", "js", "dispatcher-control-v1.js"), "utf8");
const URLS = fs.readFileSync(path.join(BACKEND, "trips", "urls.py"), "utf8");

const card = TEMPLATE.slice(
    TEMPLATE.indexOf("data-gd-equipment-detail hidden"),
    TEMPLATE.indexOf("</main>")
);

test("чья смена: метка у заголовка, предупреждение и строки «Смена / Длится» в блоке машиниста", () => {
    assert.match(card, /<span data-gd-detail-crew-title>Машинист<\/span> <span class="gd-detail-shift-verdict" data-gd-detail-shift-verdict hidden>/);
    assert.match(card, /<p class="gd-detail-shift-alert" data-gd-detail-shift-alert hidden><\/p>/);
    assert.match(card, /<dt>Смена<\/dt><dd data-gd-detail-shift-period><\/dd>/);
    assert.match(card, /<dt>Длится<\/dt><dd data-gd-detail-shift-duration><\/dd>/);
    for (const state of ["is-current", "is-overlap", "is-stale"]) {
        assert.match(CSS, new RegExp(`\\.gd-detail-shift-verdict\\.${state} \\{`));
    }
    assert.match(CSS, /\.gd-detail-shift-alert\.is-stale \{/);
    assert.match(JS, /detailShiftVerdict\.className = "gd-detail-shift-verdict" \+ \(verdict \? " is-" \+ verdict : ""\);/);
    assert.match(JS, /detailShiftAlert\.hidden = !shift\.alert;/);
    /* У самосвала заголовок блока — «Водитель», у экскаватора — «Машинист». */
    assert.match(JS, /detailCrewTitle\.textContent = \(data\.shift && data\.shift\.is_truck\) \|\| data\.type === "Самосвал" \? "Водитель" : "Машинист";/);
});

test("ручной рейс: свой блок, форма POST с csrf, точка, порода, количество, время и причина", () => {
    assert.match(card, /<section class="gd-detail-manual-trip" data-gd-detail-manual-trip hidden>/);
    assert.match(card, /<form class="gd-detail-shift-close gd-detail-manual-trip-form" data-gd-detail-manual-trip-form method="post" action="" hidden>\s*\{% csrf_token %\}/);
    assert.match(card, /<input type="hidden" name="excavator_id" value="">/);
    assert.match(card, /<select name="dump_point_id" required data-gd-detail-manual-trip-dump><\/select>/);
    assert.match(card, /<select name="rock_type_id" required data-gd-detail-manual-trip-rock><\/select>/);
    assert.match(card, /name="trips_count" min="1" max="10" step="1" value="1" inputmode="numeric" required/);
    assert.match(card, /<input type="datetime-local" name="completed_at" data-gd-detail-manual-trip-time>/);
    assert.match(card, /<input type="text" name="reason" maxlength="200" required placeholder="Например: водитель не отметил разгрузку, нет связи">/);
    assert.match(card, /data-gd-detail-manual-trip-blocked hidden/);
    assert.match(card, /class="gd-detail-shift-close-submit gd-detail-manual-trip-submit">Добавить рейс</);
    const work = card.indexOf("gd-detail-col-work");
    assert.ok(card.indexOf("data-gd-detail-manual-trip") > work, "блок ручного рейса стоит в правом столбце");
});

test("скрипт ручного рейса: точки забоя с плечом первыми, порода по умолчанию, подтверждение перед отправкой", () => {
    assert.match(JS, /function renderDetailManualTrip\(manual\)/);
    assert.match(JS, /renderDetailManualTrip\(data\.manual_trip \|\| null\);/);
    assert.match(JS, /detailManualTripForm\.hidden = !!blocked \|\| !manual\.url;/);
    assert.match(JS, /blocked = "Нужна открытая смена диспетчера\.";/);
    assert.match(JS, /group\.label = \(manual\.destinations \|\| \[\]\)\.length \? "Другие точки" : "Точки разгрузки";/);
    assert.match(JS, /String\(manual\.rock_type_id \|\| ""\) === String\(rock\.id\)/);
    assert.match(JS, /detailManualTripTime\.max = detailLocalDateTimeValue\(new Date\(\)\);/);
    assert.match(JS, /window\.openAppConfirmDialog\(message, function \(\) \{ detailManualTripForm\.submit\(\); \}, 0, "Добавить рейс"/);
    /* Сброс при смене карточки: форма закрывается, блок прячется. */
    assert.match(JS, /if \(detailManualTrip\) detailManualTrip\.hidden = true;\s*closeDetailManualTripForm\(\);/);
    /* План в шапке — дубли в общем списке не показываем. */
    assert.match(JS, /if \(detailPlanBox && !detailPlanBox\.hidden && DETAIL_PLAN_LABELS\.indexOf\(row\.label\) >= 0\) return;/);
});

test("маршрут ручного рейса объявлен рядом с другими действиями диспетчера", () => {
    assert.match(URLS, /path\('dispatcher\/trucks\/<int:equipment_id>\/manual-trip\/', dispatcher_manual_trip_view, name='dispatcher_manual_trip'\)/);
});

test("подсказка о прокрутке: липкая полоска внизу карточки, скрипт прячет её на конце", () => {
    assert.match(card, /<div class="gd-detail-scroll-hint" data-gd-detail-scroll-hint hidden>Ниже ещё — листайте ▾<\/div>\s*<\/section>/);
    assert.match(CSS, /\.gd-detail-scroll-hint \{[^}]*position: sticky;/s);
    assert.match(JS, /function syncDetailScrollHint\(\)/);
    assert.match(JS, /detailScrollHint\.hidden = rest <= 12;/);
    assert.match(JS, /window\.setTimeout\(syncDetailScrollHint, 0\);/);
});

test("карточка графика не схлопывается в полоску прогресса", () => {
    /* Скрипт даёт карточке класс "gd-detail-chart-" + type, и для bar он
       совпадает с классом тонкой полоски (height: 8px; overflow: hidden). */
    assert.match(
        CSS,
        /\.gd-detail-chart-card\.gd-detail-chart-bar \{[^}]*height: auto;[^}]*overflow: visible;/s
    );
    assert.match(CSS, /\.gd-detail-chart-row \.gd-detail-chart-bar \{[^}]*height: 8px;/s);
});

test("клон плитки экскаватора: подпись под картинкой, крестик точки на уровне поля", () => {
    assert.match(CSS, /\.gd-detail-garage-slot \.dispatcher-excavator-garage-tile span \{[^}]*position: static;/s);
    assert.match(CSS, /\.gd-detail-settings \.gd-detail-destination-remove \{[^}]*margin-bottom: 6px;/s);
});

test("номер самосвала в клоне плитки не уезжает за край: сдвиг в угол без transform", () => {
    assert.match(
        CSS,
        /body\.dispatcher-control-screen:not\(\.mining-master-mobile-screen\) \.gd-detail-garage-slot \.dispatcher-truck-tile strong \{[^}]*transform: none;/s
    );
});
