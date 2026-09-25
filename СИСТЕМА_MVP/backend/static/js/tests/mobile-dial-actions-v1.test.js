"use strict";

const test = require("node:test");
const {driverScreenSource} = require("./driver-screen-source");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const include = fs.readFileSync(path.resolve(__dirname, "../../../templates/includes/mobile_dial_actions.html"), "utf8");
const template = driverScreenSource();
const css = fs.readFileSync(path.resolve(__dirname, "../../css/mobile-dial-actions-v1.css"), "utf8");

test("shared dial component provides three independent accessible slots", () => {
    assert.match(include, /data-mobile-dial-action="manual"/);
    assert.match(include, /data-mobile-dial-action="dump-point"/);
    assert.match(include, /data-mobile-dial-action="free-bucket"/);
    assert.match(include, /data-driver-dial-manual-toggle aria-label="Ручной режим"[^>]*aria-pressed="false"/);
    assert.match(include, /mobile_dial_manual_blocked_reason/);
    assert.match(include, /aria-label="Изменить точку разгрузки"/);
    assert.match(include, /aria-label="Свободный ковш"/);
    assert.doesNotMatch(include, /mobile_dial_show_(?:manual|dump_point|free_bucket)/);
    assert.match(template, /mobile_dial_dump_point_enabled=active_trip/);
    assert.match(template, /mobile_dial_manual_enabled=driver_manual_workspace\.can_open/);
    assert.match(template, /mobile_dial_free_bucket_enabled=driver_free_bucket_can_open/);
    assert.match(template, /mobile_dial_has_active_trip=active_trip/);
    assert.match(template, /mobile_dial_has_open_shift=open_shift/);
    assert.match(template, /mobile_dial_has_current_truck=current_truck/);
});

test("all three slots stay rendered while unavailable actions are explicitly disabled", () => {
    assert.match(include, /data-mobile-dial-action="manual"\{% if mobile_dial_manual_enabled %\}/);
    assert.match(include, /\{% else %\} disabled aria-disabled="true" aria-label="\{\{ mobile_dial_manual_blocked_reason/);
    // Ручной рейс не проходит по mobile_dial_dump_point_enabled (свой active_trip у него
    // пустой) — кнопка остаётся доступна и по нему тоже, через движок ручного режима.
    assert.match(include, /data-mobile-dial-action="dump-point"\{% if driver_manual_workspace\.active_trip_origin == "driver_manual" %\} data-driver-manual-point-open/);
    assert.match(include, /\{% elif mobile_dial_dump_point_enabled %\} data-driver-point-open/);
    assert.match(include, /\{% else %\} disabled aria-disabled="true" aria-label="Изменить точку разгрузки — доступно только в активном рейсе"/);
    assert.match(include, /data-mobile-dial-action="free-bucket"\{% if mobile_dial_free_bucket_enabled %\}/);
    assert.match(include, /aria-controls="driver-free-bucket-dialog" aria-expanded="false"/);
    assert.match(include, /Свободный ковш недоступен во время активного рейса/);
    assert.match(include, /Откройте смену, чтобы выбрать свободный ковш/);
    assert.match(include, /Сначала выберите самосвал для текущей смены/);
});

test("dial actions are positioned from the dial and clip their real hit areas", () => {
    assert.match(css, /\.mobile-dial-actions\s*\{[\s\S]*position:\s*absolute;[\s\S]*inset:\s*0;/);
    assert.match(css, /\.mobile-dial-actions\s*\{[\s\S]*pointer-events:\s*none;/);
    assert.match(css, /\.mobile-dial-action\s*\{[\s\S]*pointer-events:\s*auto;/);
    assert.match(include, /clipPath id="driver-dial-corner-clip"/);
    assert.match(include, /mobile-dial-action__rim/);
    assert.match(include, /mobile-dial-action__face/);
    assert.match(css, /clip-path:\s*url\(#driver-dial-corner-clip\)/);
    assert.match(css, /width:\s*27%;/);
    assert.match(css, /aspect-ratio:\s*1 \/ 1;/);
    assert.match(include, /transform="scale\(\.01 \.01\)"/);
    assert.match(include, /mobile-dial-action--spare/);
    assert.match(css, /\.mobile-dial-action:disabled\s*\{[\s\S]*pointer-events:\s*none;/);
    assert.match(css, /\.mobile-dial-action:disabled\s*\{[\s\S]*opacity:\s*\.46;/);
    assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
});

test("only the compact dump point action owns the production modal trigger", () => {
    assert.match(include, /data-mobile-dial-action="dump-point"[\s\S]*data-driver-point-open/);
    assert.doesNotMatch(template, /driver-work-context-card"[^>]*data-driver-point-open/);
    assert.match(template, /id="driver-unload-dialog"/);
    assert.match(template, /aria-modal="true"/);
    assert.match(template, /data-driver-current-point-name/);
    assert.match(template, /Подтверждено сервером/);
    assert.match(template, /Действие сохранено/);
    assert.match(template, /Не подтверждено/);
    assert.doesNotMatch(template, /Нужна сверка/);
});
