"use strict";

/* Обновление доски Горного мастера: возврат промиса и отказ от лишних
   фоновых запросов. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const TEMPLATE_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "..", "templates", "trips", "dispatcher_control.html"),
    "utf8"
);

function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, label + ": сигнатура не найдена.");
    const open = source.indexOf("{", start + signature.length);
    assert.notEqual(open, -1, label + ": не найдена открывающая скобка.");
    let depth = 0;
    let quote = "";
    let escaped = false;
    let lineComment = false;
    let blockComment = false;
    for (let index = open; index < source.length; index += 1) {
        const character = source[index];
        const next = source[index + 1] || "";
        if (lineComment) {
            if (character === "\n") lineComment = false;
            continue;
        }
        if (blockComment) {
            if (character === "*" && next === "/") {
                blockComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (escaped) escaped = false;
            else if (character === "\\") escaped = true;
            else if (character === quote) quote = "";
            continue;
        }
        if (character === "/" && next === "/") {
            lineComment = true;
            index += 1;
            continue;
        }
        if (character === "/" && next === "*") {
            blockComment = true;
            index += 1;
            continue;
        }
        if (character === "'" || character === '"' || character === "`") {
            quote = character;
            continue;
        }
        if (character === "{") depth += 1;
        else if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(label + ": не найдена закрывающая скобка.");
    return "";
}

test("refreshMobileBoardFromServer отдаёт промис, а не undefined", async () => {
    /* Раньше функция ничего не возвращала, и window.applyOperationalStateRefresh
       падал на .then(): обновление доски по событию realtime не работало. */
    const requested = [];
    const shellStub = {
        replaceWith() {},
        scrollTop: 0,
        querySelectorAll: () => [],
        querySelector: () => null,
        classList: {contains: () => false, add() {}, remove() {}},
        dataset: {},
    };
    const sandbox = {
        miningMasterMobileRefreshPromise: null,
        document: {
            querySelector: (selector) => (selector === ".mm-mobile-shell" ? shellStub : null),
        },
        window: {
            AppOperationalFragment: {
                request(screen, version) {
                    requested.push({screen: screen, version: version});
                    return Promise.resolve({html: "", version: "0"});
                },
                parseRoot: () => null,
            },
        },
        Number: Number,
        Promise: Promise,
        JSON: JSON,
        captureMobileShellState: () => ({scrollTop: 0}),
        restoreMobileShellState: () => {},
        bindMiningMasterMobileScreens: () => {},
        refreshMiningMasterUpdateIndicatorFromStorage: () => {},
        updateDispatcherSyncIndicator: () => {},
        storeMiningMasterRealtimeVersion: () => {},
        markMiningMasterBoardCurrent: () => {},
        isMobileOperationalRefreshUnsafe: () => false,
        equipmentCardsNode: null,
        equipmentCards: {},
    };
    /* Сверка теперь сообщает о завершении событием — в песочнице нужны заглушки. */
    sandbox.CustomEvent = function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; };
    sandbox.window.dispatchEvent = sandbox.window.dispatchEvent || function () { return true; };
    vm.createContext(sandbox);
    const block = extractBraceBlock(
        TEMPLATE_SOURCE,
        "function refreshMobileBoardFromServer(options)",
        "refreshMobileBoardFromServer"
    );
    const result = vm.runInContext(
        block + "\nrefreshMobileBoardFromServer({preserveScreen: true});",
        sandbox
    );
    assert.notEqual(result, undefined, "Функция обязана вернуть промис обновления.");
    assert.equal(typeof result.then, "function", "Возвращаемое значение должно быть thenable.");
    await result;
    assert.equal(requested.length, 1, "Должен уйти ровно один запрос фрагмента.");
});

test("фоновый таймер не перечитывает доску, пока версия смены не изменилась", () => {
    /* Безусловное обновление раз в 30 секунд тянуло 324 КБ ответа даже когда
       на карьере ничего не происходило. */
    const timerBody = extractBraceBlock(
        TEMPLATE_SOURCE,
        "window.setInterval(function ()",
        "фоновый таймер доски"
    );
    assert.match(
        timerBody,
        /stateChanged/,
        "В таймере должна быть проверка изменения версии состояния смены."
    );
    assert.match(
        timerBody,
        /miningMasterBoardCurrentUpTo/,
        "Проверка должна опираться на версию, которой соответствует текущая доска."
    );
    assert.match(
        timerBody,
        /if\s*\(!stateChanged\s*&&\s*!reconcileDue\)\s*return;/,
        "Без изменений и вне срока полной сверки запрос уходить не должен."
    );
    assert.match(
        TEMPLATE_SOURCE,
        /MINING_MASTER_FULL_RECONCILE_MS\s*=\s*\d+/,
        "Страховочная полная сверка должна остаться: без неё экран может застрять на старых данных."
    );
});

test("глобальные слушатели экрана вешаются один раз на страницу", () => {
    /* bindMiningMasterMobileScreens() вызывается заново после каждой замены
       доски. Раньше это добавляло по новому resize/keydown/ResizeObserver на
       каждое обновление и экран тормозил тем сильнее, чем дольше открыт. */
    const binder = extractBraceBlock(
        TEMPLATE_SOURCE,
        "function bindMiningMasterMobileScreens()",
        "bindMiningMasterMobileScreens"
    );
    assert.match(
        binder,
        /if\s*\(!window\.__mmMobileGlobalHandlersBound\)/,
        "Глобальные слушатели должны ставиться под флагом однократности."
    );
    const guardIndex = binder.indexOf("window.__mmMobileGlobalHandlersBound");
    ["window.addEventListener(\"resize\"", "document.addEventListener(\"keydown\"", "new ResizeObserver"].forEach((needle) => {
        const at = binder.indexOf(needle);
        assert.notEqual(at, -1, "Не найдено: " + needle);
        assert.ok(
            at > guardIndex,
            needle + " должен создаваться только внутри однократного блока."
        );
        assert.equal(
            binder.indexOf(needle, at + 1),
            -1,
            needle + " не должен встречаться дважды."
        );
    });
    assert.match(
        binder,
        /window\.__mmMobileLayoutObserver\.disconnect\(\)/,
        "Единственный ResizeObserver обязан отпускать узлы старой доски."
    );
});

test("прежний неиспользуемый обработчик перетаскивания не уезжает клиенту", () => {
    assert.equal(
        TEMPLATE_SOURCE.indexOf("function bindMobileHomeTruckDragLegacy"),
        -1,
        "Мёртвый обработчик должен быть удалён из шаблона, а не просто не вызываться."
    );
});

test("после отказа сервера доска помечается устаревшей и перечитывается при закрытии окна", () => {
    /* Отклонённая команда не двигает версию смены, поэтому условный таймер
       сам бы не проснулся, а обновление из обработчика ошибки откладывается,
       пока открыто окно «Действие не выполнено». На телефоне после 409
       самосвал оставался нарисован в комплексе, куда не приехал. */
    const flush = extractBraceBlock(
        TEMPLATE_SOURCE,
        "function flushDispatcherSyncQueue()",
        "flushDispatcherSyncQueue"
    );
    const errorBranch = flush.slice(flush.indexOf("error.isServerResponse"));
    assert.ok(
        errorBranch.indexOf("markMiningMasterBoardStale()") !== -1
            && errorBranch.indexOf("markMiningMasterBoardStale()") < errorBranch.indexOf("showDispatcherDnDError(error)"),
        "После отказа сервера доска должна помечаться устаревшей до показа окна."
    );
    const close = extractBraceBlock(
        TEMPLATE_SOURCE,
        "function closeDispatcherNotice()",
        "closeDispatcherNotice"
    );
    assert.match(
        close,
        /miningMasterBoardStale[\s\S]*refreshMobileBoardFromServer/,
        "Закрытие окна должно перечитывать устаревшую доску."
    );
    assert.match(
        TEMPLATE_SOURCE,
        /function markMiningMasterBoardStale\(\)[\s\S]*miningMasterBoardCurrentUpTo = 0;/,
        "Пометка обязана сбрасывать версию доски, чтобы таймер перечитал её независимо от версии смены."
    );
});

test("обновление, запрошенное во время летящего, уходит вторым запросом, а не теряется", async () => {
    /* Иначе фрагмент, ушедший до команды мастера, затирал её результат:
       самосвал на телефоне «отпрыгивал» назад, повторный жест давал 409. */
    let resolveFirst = null;
    const requested = [];
    const shellStub = {
        replaceWith() {}, scrollTop: 0, querySelectorAll: () => [], querySelector: () => null,
        classList: {contains: () => false, add() {}, remove() {}}, dataset: {},
    };
    const sandbox = {
        miningMasterMobileRefreshPromise: null,
        miningMasterMobileRefreshFollowUp: null,
        document: {querySelector: (s) => (s === ".mm-mobile-shell" ? shellStub : null)},
        window: {AppOperationalFragment: {
            request() {
                requested.push(1);
                if (requested.length === 1) return new Promise((r) => { resolveFirst = r; });
                return Promise.resolve({html: "", version: "0"});
            },
            parseRoot: () => null,
        }},
        Number, Promise, JSON,
        captureMobileShellState: () => ({scrollTop: 0}), restoreMobileShellState() {},
        bindMiningMasterMobileScreens() {}, refreshMiningMasterUpdateIndicatorFromStorage() {},
        updateDispatcherSyncIndicator() {}, storeMiningMasterRealtimeVersion() {},
        markMiningMasterBoardCurrent() {}, isMobileOperationalRefreshUnsafe: () => false,
        equipmentCardsNode: null, equipmentCards: {},
    };
    /* Сверка теперь сообщает о завершении событием — в песочнице нужны заглушки. */
    sandbox.CustomEvent = function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; };
    sandbox.window.dispatchEvent = sandbox.window.dispatchEvent || function () { return true; };
    vm.createContext(sandbox);
    const block = extractBraceBlock(TEMPLATE_SOURCE, "function refreshMobileBoardFromServer(options)", "refreshMobileBoardFromServer");
    vm.runInContext(block + "\nvar __first = refreshMobileBoardFromServer({});\nvar __second = refreshMobileBoardFromServer({});", sandbox);
    assert.equal(requested.length, 1, "Пока первый запрос летит, второй не должен дублировать его немедленно.");
    resolveFirst({html: "", version: "0"});
    await vm.runInContext("__second", sandbox);
    assert.equal(requested.length, 2, "После завершения первого обязан уйти второй запрос за свежим состоянием.");
});
