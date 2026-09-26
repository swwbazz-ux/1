"use strict";

/* Контракт живого поиска техники на desktop-пульте.

   Поле находится в общей шапке, поведение — в отдельном runtime, оформление —
   в адаптивном слое. Проверка нужна именно на стыке этих файлов: production
   долго сохранял поле из отдельной выкладки, хотя в релизной ветке разметка
   отсутствовала и локальный пульт молча терял быстрый набор номера. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {dispatcherStyleSource} = require("./dispatcher-style-source");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const read = (...parts) => fs.readFileSync(path.join(BACKEND, ...parts), "utf8");

const HEADER = read("templates", "includes", "dispatcher_header.html");
const TEMPLATE = read("templates", "trips", "dispatcher_control.html");
const SEARCH_RUNTIME = read("static", "js", "dispatcher-equipment-search-v1.js");
const BOARD_RUNTIME = read("static", "js", "dispatcher-board-v1.js");
const CONTROL_RUNTIME = read("static", "js", "dispatcher-control-v1.js");
const PWA_SOURCE = read("trips", "dispatcher_pwa.py");
const STYLES = dispatcherStyleSource();
const PRODUCTION_MANIFEST = fs.readFileSync(
    path.resolve(BACKEND, "..", "..", ".github", "deploy", "production-files.txt"),
    "utf8"
);

test("поле поиска есть только на desktop-пульте смены и входит в релиз", () => {
    const from = HEADER.indexOf("dispatcher-equipment-search");
    assert.notEqual(from, -1, "поле поиска не найдено в шапке");
    const guard = HEADER.slice(Math.max(0, from - 500), from);
    assert.match(guard, /dispatcher_nav_active == "control"/);
    assert.match(guard, /not mining_master_mobile_enabled/);
    assert.match(HEADER, /data-dispatcher-equipment-search\b/);
    assert.match(HEADER, /data-dispatcher-equipment-search-count/);
    assert.match(HEADER, /maxlength="3"/);
    assert.doesNotMatch(
        HEADER,
        /data-dispatcher-equipment-search[^>]*placeholder="[^"]+"/
    );
    assert.match(
        PRODUCTION_MANIFEST,
        /templates\/includes\/dispatcher_header\.html/
    );
});

test("поиск ловит набор номера без предварительного фокуса", () => {
    assert.match(SEARCH_RUNTIME, /function createDispatcherEquipmentSearch\(\)/);
    assert.match(SEARCH_RUNTIME, /global\.createDispatcherEquipmentSearch = createDispatcherEquipmentSearch/);
    assert.match(SEARCH_RUNTIME, /function bindEquipmentSearch\(\)/);
    assert.match(SEARCH_RUNTIME, /name\.indexOf\(needle\) === 0/);
    assert.match(SEARCH_RUNTIME, /replace\(\/k\/g, "к"\)/);
    assert.match(SEARCH_RUNTIME, /event\.key === "Escape"/);
    assert.match(
        SEARCH_RUNTIME,
        /document\.addEventListener\("keydown", function \(event\) \{\s*if \(event\.defaultPrevented \|\| event\.ctrlKey/
    );
    assert.match(
        SEARCH_RUNTIME,
        /key\.length === 1 && \/\[0-9a-zа-яё\\-\]\/i\.test\(key\)/
    );
    assert.match(SEARCH_RUNTIME, /isTypingElsewhere\(\) \|\| isDialogOpen\(\)/);
    assert.match(
        SEARCH_RUNTIME,
        /document\.addEventListener\("pointerdown", function \(event\) \{\s*if \(query === "" \|\| \(box && box\.contains\(event\.target\)\)\) return;\s*clearEquipmentSearch\(\);/
    );
    assert.match(
        SEARCH_RUNTIME,
        /new MutationObserver\(function \(records\) \{\s*var nextInput = document\.querySelector\("\[data-dispatcher-equipment-search\]"\);/
    );
    assert.match(SEARCH_RUNTIME, /!\(box && box\.contains\(record\.target\)\)/);
    assert.match(SEARCH_RUNTIME, /input\.dataset\.dispatcherEquipmentSearchBound !== "true"/);
    assert.match(BOARD_RUNTIME, /rebindEquipmentSearch\(\);/);
    assert.match(CONTROL_RUNTIME, /rebindEquipmentSearch: dispatcherEquipmentSearch\.bind/);
    assert.doesNotMatch(BOARD_RUNTIME, /function bindDispatcherEquipmentSearch\(\)/);
});

test("поиск загружается до доски и полностью входит в пакет приложения", () => {
    const searchIndex = TEMPLATE.indexOf("dispatcher-equipment-search-v1.js");
    const boardIndex = TEMPLATE.indexOf("dispatcher-board-v1.js");
    assert.ok(searchIndex >= 0, "модуль поиска отсутствует в шаблоне");
    assert.ok(boardIndex > searchIndex, "поиск должен загрузиться до доски");
    assert.match(
        TEMPLATE,
        /dispatcher-equipment-search-v1\.js[^\n]+dispatcher-desktop-shell-v159/
    );
    assert.equal(
        (PWA_SOURCE.match(/\/static\/js\/dispatcher-equipment-search-v1\.js/g) || []).length,
        1
    );
    const manifestPath = "СИСТЕМА_MVP/backend/static/js/dispatcher-equipment-search-v1.js";
    assert.equal(
        PRODUCTION_MANIFEST.split(/\r?\n/).filter((line) => line === manifestPath).length,
        1
    );
});

test("совпадение заметно, а статусная окраска карточки сохраняется", () => {
    assert.match(STYLES, /@keyframes dispatcher-search-blink/);
    assert.match(STYLES, /\.is-search-hit \{[^}]*animation: dispatcher-search-blink/s);
    assert.match(
        STYLES,
        /\.is-equipment-search \.complex-truck-tile:not\(\.is-search-hit\)/
    );
    assert.match(
        STYLES,
        /\.is-equipment-search \.dispatcher-truck-tile:not\(\.is-search-hit\)/
    );
    assert.doesNotMatch(
        STYLES,
        /\.is-equipment-search \.dispatcher-complex-card:not\(\.is-search-hit\)/
    );
    assert.match(STYLES, /prefers-reduced-motion: reduce\) \{\s*body[^{]*\.is-search-hit \{[^}]*animation: none/s);
});

test("повторная привязка поиска не зацикливается на собственном счётчике", () => {
    function fakeClassList() {
        const values = new Set();
        return {
            add: (name) => values.add(name),
            remove: (name) => values.delete(name),
            contains: (name) => values.has(name),
            toggle: (name, force) => {
                const enabled = force === undefined ? !values.has(name) : Boolean(force);
                if (enabled) values.add(name);
                else values.delete(name);
                return enabled;
            },
        };
    }

    function fakeElement() {
        const listeners = new Map();
        return {
            attributes: new Map(),
            classList: fakeClassList(),
            dataset: {},
            hidden: false,
            isConnected: true,
            maxLength: 0,
            parentElement: null,
            textContent: "",
            value: "",
            addEventListener(type, listener) {
                const current = listeners.get(type) || [];
                current.push(listener);
                listeners.set(type, current);
            },
            emit(type, event = {}) {
                for (const listener of listeners.get(type) || []) listener(event);
            },
            getAttribute(name) {
                return this.attributes.get(name) || null;
            },
            listenerCount(type) {
                return (listeners.get(type) || []).length;
            },
            setAttribute(name, value) {
                this.attributes.set(name, String(value));
            },
            scrollIntoView() {},
        };
    }

    const body = fakeElement();
    const shell = fakeElement();
    const box = fakeElement();
    const count = fakeElement();
    const truck = fakeElement();
    truck.setAttribute("data-equipment-name", "10");
    let currentInput = fakeElement();
    currentInput.maxLength = 3;
    currentInput.closest = (selector) => selector === "[data-dispatcher-equipment-search-box]" ? box : null;
    box.contains = (node) => node === box || node === count || node === currentInput;

    const documentListeners = new Map();
    const document = {
        activeElement: body,
        body,
        addEventListener(type, listener) {
            documentListeners.set(type, listener);
        },
        getElementById() {
            return null;
        },
        querySelector(selector) {
            if (selector === "[data-dispatcher-equipment-search]") return currentInput;
            if (selector === "[data-dispatcher-equipment-search-count]") return count;
            if (selector === ".dispatcher-shell") return shell;
            return null;
        },
        querySelectorAll(selector) {
            return selector === ".dispatcher-shell [data-equipment-name]" ? [truck] : [];
        },
    };

    let observerCallback = null;
    const scheduled = [];
    class FakeMutationObserver {
        constructor(callback) {
            observerCallback = callback;
        }
        observe() {}
    }
    const context = {
        MutationObserver: FakeMutationObserver,
        clearTimeout() {},
        document,
        setTimeout(callback) {
            scheduled.push(callback);
            return scheduled.length;
        },
    };
    context.window = context;
    vm.runInNewContext(SEARCH_RUNTIME, context);

    const search = context.createDispatcherEquipmentSearch();
    assert.equal(search.bind(), true);
    assert.equal(typeof observerCallback, "function");

    currentInput.value = "10";
    currentInput.emit("input");
    assert.equal(truck.classList.contains("is-search-hit"), true);

    // applyEquipmentSearch обновляет этот счётчик. Такая mutation не должна
    // ставить rebind в очередь и создавать цикл setTimeout.
    observerCallback([{target: count}]);
    assert.equal(scheduled.length, 0);

    observerCallback([{target: shell}]);
    assert.equal(scheduled.length, 1, "замена/обновление доски должна перепривязать поиск");
    scheduled.shift()();
    assert.equal(currentInput.listenerCount("input"), 1, "повторный bind не дублирует input-listener");

    const replacedInput = fakeElement();
    replacedInput.maxLength = 3;
    replacedInput.closest = currentInput.closest;
    currentInput.isConnected = false;
    currentInput = replacedInput;
    observerCallback([{target: shell}]);
    assert.equal(scheduled.length, 1, "заменённое поле должно получить сохранённый запрос");
    scheduled.shift()();
    assert.equal(replacedInput.value, "10");
    assert.equal(replacedInput.listenerCount("input"), 1);

    observerCallback([{target: count}]);
    assert.equal(scheduled.length, 0, "счётчик после повторного bind также не создаёт новый цикл");
    assert.equal(documentListeners.has("keydown"), true);
});
