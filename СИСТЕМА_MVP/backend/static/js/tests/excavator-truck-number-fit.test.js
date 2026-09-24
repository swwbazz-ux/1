const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.join(__dirname, "..", "excavator-truck-number-fit-v1.js"),
    "utf8",
);

/**
 * Поддельный экран: столько подписей номеров, сколько попросили, плюс счётчик
 * вызовов правила вмещения. Настоящее правило живёт в общем модуле и проверено
 * его собственными тестами — здесь проверяется только связка.
 */
const CARD_CLASS = "eo-dashboard-truck-card";

/**
 * Подпись на экране: номер в карточке лежит ПРЯМО в кнопке карточки, а подпись
 * кнопки режима — внутри вложенного span. Цепочка предков хранится от ближнего
 * к дальнему, как её видит браузер.
 */
function createLabel(text, ancestors) {
    return {
        textContent: text,
        clientWidth: 102,
        clientHeight: 32,
        dataset: {},
        ancestors: ancestors || [CARD_CLASS],
    };
}

/**
 * Маленький разборщик выборки: понимает ровно два вида, которые нас и
 * различают, — прямого потомка карточки и любого вложенного. Нужен, чтобы тест
 * ломался на самом ПРИЁМЕ, а не на записи строки: сверка строки с эталоном
 * ничего не доказывает, её поправят вместе с кодом.
 */
function matchLabels(selector, labels) {
    const direct = selector.indexOf(">") >= 0;
    return labels.filter(function (label) {
        return direct
            ? label.ancestors[0] === CARD_CLASS
            : label.ancestors.indexOf(CARD_CLASS) >= 0;
    });
}

function createScreen(numbers) {
    const labels = numbers.map(function (text) {
        return createLabel(text);
    });
    const calls = [];
    const listeners = {};
    let observer = null;

    const root = {
        document: {
            readyState: "complete",
            body: {},
            addEventListener() {},
            querySelectorAll(selector) {
                return matchLabels(selector, labels);
            },
        },
        EquipmentLabelFit: {
            fit(element, options) {
                calls.push({text: element.textContent, options: options});
                return {deferred: false, fontPx: 14, wrapped: false, squeezed: 1};
            },
        },
        MutationObserver: function (callback) {
            observer = {callback: callback, options: null};
            this.observe = function (target, options) {
                observer.options = options;
            };
        },
        addEventListener(name, handler) {
            listeners[name] = handler;
        },
        /* Без кадра анимации связка считает сразу — так тест видит результат
           без ожидания. */
        requestAnimationFrame: null,
        setTimeout(callback) {
            callback();
            return 1;
        },
    };

    vm.runInNewContext(SOURCE, {window: root, module: null});

    return {
        labels: labels,
        calls: calls,
        api: root.ExcavatorTruckNumberFit,
        observer: function () { return observer; },
        listeners: listeners,
    };
}

test("номер подгоняется с явным запретом переноса", () => {
    // Настоящие номера бывают с пробелом («Тест 1», «ТМС 528»), и пробел не
    // повод разрывать номер надвое: под вторую строку на карточке место есть,
    // поэтому запрет должен быть явным, а не следовать из тесноты.
    const screen = createScreen(["Тест 1"]);

    assert.equal(screen.calls.length, 1);
    // Объект создан внутри песочницы, поэтому сверяем по содержимому,
    // а не целиком: у него чужой прототип.
    assert.equal(screen.calls[0].options.allowWrap, false);
});

test("повторный вызов на неизменившемся экране ничего не пересчитывает", () => {
    // Отсчёты на экране машиниста меняют разметку каждую секунду. Без этой
    // проверки наблюдатель будил бы подбор кегля двенадцать раз в секунду.
    const screen = createScreen(["Тест 1", "10", "САМОСВАЛ-123"]);
    assert.equal(screen.calls.length, 3);

    screen.api.refit();

    assert.equal(screen.calls.length, 3, "подгонка пересчитала то, что не менялось");
});

test("изменение номера пересчитывает только его", () => {
    const screen = createScreen(["Тест 1", "10"]);
    screen.calls.length = 0;

    screen.labels[1].textContent = "САМОСВАЛ-123";
    screen.api.refit();

    assert.equal(screen.calls.map(function (call) { return call.text; }).join(","), "САМОСВАЛ-123");
});

test("сузившаяся карточка пересчитывается заново", () => {
    // Поворот экрана меняет ширину, не трогая разметку.
    const screen = createScreen(["Тест 1"]);
    screen.calls.length = 0;

    screen.labels[0].clientWidth = 84;
    screen.api.refit();

    assert.equal(screen.calls.length, 1);
});

test("связка следит за сменой вкладки, а не только за разметкой", () => {
    // Вкладка «Работа» может быть неактивной в момент отрисовки: ширины нет,
    // замер дал бы ноль. Смена вкладки разметку не меняет, поэтому без
    // наблюдения за признаком вкладки подгонка осталась бы несделанной.
    const screen = createScreen(["Тест 1"]);
    const options = screen.observer().options;

    assert.equal(options.childList, true);
    assert.equal(options.subtree, true);
    assert.equal(Array.from(options.attributeFilter).join(","), "data-eo-active-tab");
});

test("без общего модуля экран работает как раньше", () => {
    // Общий модуль приезжает отдельным файлом и отдельным хешем. Если он не
    // доедет, номер останется прежнего размера — это хуже вида, но не падение
    // экрана в забое.
    const labels = [{textContent: "Тест 1", clientWidth: 102, clientHeight: 32, dataset: {}}];
    const root = {
        document: {
            readyState: "complete",
            body: {},
            addEventListener() {},
            querySelectorAll() { return labels; },
        },
        MutationObserver: function () { this.observe = function () {}; },
        addEventListener() {},
        setTimeout(callback) { callback(); return 1; },
    };

    assert.doesNotThrow(function () {
        vm.runInNewContext(SOURCE, {window: root, module: null});
    });
    assert.equal(Object.keys(labels[0].dataset).length, 0);
});

test("подпись кнопки режима с тем же классом карточки не подгоняется", () => {
    // Класс карточки висит и на кнопках ручного режима водителя («ОБЫЧНЫЙ
    // РЕЖИМ», «ИЗМЕНИТЬ ТОЧКУ»), и внутри каждой есть strong. На экране
    // машиниста этих кнопок сегодня нет, но защита держалась только на этом.
    // Проверка ломается на самом приёме: верни широкую выборку — и подгонка
    // найдёт вторую подпись.
    const labels = [
        createLabel("Тест 1"),
        createLabel("ОБЫЧНЫЙ РЕЖИМ", ["driver-manual-workspace__action-copy", CARD_CLASS]),
    ];
    const calls = [];
    const root = {
        document: {
            readyState: "complete",
            body: {},
            addEventListener() {},
            querySelectorAll(selector) { return matchLabels(selector, labels); },
        },
        EquipmentLabelFit: {
            fit(element) {
                calls.push(element.textContent);
                return {deferred: false, fontPx: 14, wrapped: false, squeezed: 1};
            },
        },
        MutationObserver: function () { this.observe = function () {}; },
        addEventListener() {},
        setTimeout(callback) { callback(); return 1; },
    };

    vm.runInNewContext(SOURCE, {window: root, module: null});

    assert.equal(calls.join(","), "Тест 1");
});
