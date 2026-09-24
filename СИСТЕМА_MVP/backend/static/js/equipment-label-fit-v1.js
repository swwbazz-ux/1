/**
 * Общее правило вмещения подписи на карточке техники.
 *
 * Подпись номера стоит на карточке одинаково у водителя (номер экскаватора в
 * ручном режиме) и у машиниста (номер самосвала), поэтому и вмещается она
 * одним правилом, а не двумя похожими.
 *
 * Порядок уступок один и тот же и меняться не должен:
 *   1) уменьшаем кегль, пока подпись не встанет в одну строку, но не ниже пола;
 *   2) если на полу не встала — разрешаем перенос по дефису или пробелу, но
 *      только если под вторую строку есть высота;
 *   3) если и это не помогло — сжимаем подпись по горизонтали;
 *   4) обрезки нет никогда, разрыва посреди слова нет никогда.
 *
 * Ширины у скрытой карточки нет. Замер в этот момент даёт ноль, и подгонка
 * уходит вхолостую — именно так подпись «ЭКС-99» приехала на бой срезанной.
 * Поэтому модуль не сдаётся при нулевой ширине, а дожидается её.
 */
(function (root) {
    "use strict";

    if (!root) return;

    // Ниже этого кегля подпись в кабине уже не прочитать мельком: экран
    // грязный, взгляд короткий. Тот же порог, что у названий точек разгрузки.
    var MIN_FONT_PX = 14;
    // Крайняя мера. Обрезанный номер читается неверно — это хуже мелкого,
    // поэтому когда уступок больше не осталось, кегль уходит ниже пола, но
    // подпись остаётся целой. Ниже этого не опускаемся уже никогда.
    var LAST_RESORT_FONT_PX = 9;
    // Дальше этого сжимать нельзя: буквы слипаются и номер читается неверно.
    var MIN_SQUEEZE = 0.82;
    var MIN_LETTER_SPACING_EM = -0.06;
    // Точность подбора кегля. Мельче доли пикселя глазу безразлично.
    var FONT_STEP_PX = 0.25;

    function px(value) {
        var number = parseFloat(value);
        return isFinite(number) ? number : 0;
    }

    function styleOf(element) {
        if (!root.getComputedStyle) return null;
        try {
            return root.getComputedStyle(element);
        } catch (error) {
            return null;
        }
    }

    function clearFitting(element) {
        var style = element.style;
        if (!style) return;
        ["font-size", "letter-spacing", "transform", "transform-origin", "white-space"].forEach(function (name) {
            style.removeProperty(name);
        });
        if (element.classList) {
            element.classList.remove("is-label-fit-wrapped");
            element.classList.remove("is-label-fit-squeezed");
        }
    }

    function setImportant(element, name, value) {
        if (element.style && element.style.setProperty) {
            element.style.setProperty(name, value, "important");
        }
    }

    function fitsOnOneLine(element) {
        return Number(element.scrollWidth || 0) <= Number(element.clientWidth || 0) + 0.5;
    }

    function boxHasRoom(element) {
        // Коробка подписи бывает двух видов. У машиниста строка под номер
        // фиксированной высоты — тогда всё видно по самой подписи. У водителя
        // строка растёт под содержимое, и подпись всегда «ровно по себе»:
        // тогда спрашиваем карточку, осталось ли в ней место.
        if (Number(element.scrollHeight || 0) <= Number(element.clientHeight || 0) + 0.5) return true;
        var box = element.parentElement;
        if (!box || !Number(box.clientHeight || 0)) return false;
        return Number(box.scrollHeight || 0) <= Number(box.clientHeight || 0) + 0.5;
    }

    function fitsInBox(element) {
        return fitsOnOneLine(element) && boxHasRoom(element);
    }

    function largestFittingFont(element, maxPx, fits) {
        // Двоичный поиск: браузер меряет текст сам, нам остаётся спрашивать.
        var low = MIN_FONT_PX;
        var high = Math.max(MIN_FONT_PX, maxPx);
        setImportant(element, "font-size", high + "px");
        if (fits(element)) return high;
        while (high - low > FONT_STEP_PX) {
            var middle = (low + high) / 2;
            setImportant(element, "font-size", middle + "px");
            if (fits(element)) low = middle;
            else high = middle;
        }
        setImportant(element, "font-size", low + "px");
        return low;
    }

    function canTakeSecondLine(element) {
        var style = styleOf(element);
        var lineHeight = px(style && style.lineHeight);
        if (!lineHeight) lineHeight = px(style && style.fontSize) * 1.2;
        if (!lineHeight) return false;
        if (Number(element.clientHeight || 0) >= lineHeight * 2 - 0.5) return true;
        // Подпись может расти вместе со строкой карточки. Тогда высоту под
        // вторую строку надо искать не в подписи, а в карточке.
        var box = element.parentElement;
        if (!box || !Number(box.clientHeight || 0)) return false;
        var spare = Number(box.clientHeight || 0) - Number(box.scrollHeight || 0);
        return spare >= lineHeight - 0.5;
    }

    function hasBreakPoint(text) {
        // Переносим только по дефису и пробелу. Внутри слова — никогда:
        // «САМОСВАЛ-123» разрывается после дефиса, а не посреди слова.
        return /[\s‐-―-]/.test(String(text || ""));
    }

    /**
     * Подогнать подпись под её собственную коробку.
     *
     * Возвращает описание того, до какой ступени пришлось дойти — это же
     * описание читают тесты.
     */
    function fit(element, options) {
        options = options || {};
        // Номер техники по правилу пользователя всегда стоит одной строкой:
        // пробел внутри номера («ТМС 528», «Тест 1») — не повод переносить.
        // Поэтому ступень переноса можно выключить явно, а не подбирать
        // высоту так, чтобы она случайно не сработала.
        var allowWrap = options.allowWrap !== false;
        if (!element || !element.style) return null;
        clearFitting(element);
        var style = styleOf(element);
        var maxFont = Math.max(MIN_FONT_PX, px(style && style.fontSize) || MIN_FONT_PX);
        var available = Number(element.clientWidth || 0);
        if (available <= 0) {
            // Карточка ещё скрыта. Не сдаёмся: ждём ширину.
            return {deferred: true, fontPx: null, wrapped: false, squeezed: 1};
        }

        setImportant(element, "white-space", "nowrap");
        var fontPx = largestFittingFont(element, maxFont, fitsOnOneLine);
        if (fontPx > MIN_FONT_PX + FONT_STEP_PX / 2 || fitsOnOneLine(element)) {
            return {deferred: false, fontPx: fontPx, wrapped: false, squeezed: 1};
        }

        // Ступень вторая: перенос. Доступна, только если есть высота под вторую
        // строку — на самой тесной карточке машиниста её может не быть.
        var wrapped = false;
        if (allowWrap && hasBreakPoint(element.textContent) && canTakeSecondLine(element)) {
            setImportant(element, "white-space", "normal");
            if (element.classList) element.classList.add("is-label-fit-wrapped");
            fontPx = largestFittingFont(element, maxFont, fitsInBox);
            if (fitsInBox(element)) {
                return {deferred: false, fontPx: fontPx, wrapped: true, squeezed: 1};
            }
            wrapped = true;
        }

        // Ступень третья: сжатие. Сначала межбуквенное, затем по горизонтали.
        setImportant(element, "font-size", MIN_FONT_PX + "px");
        var needed = Math.max(1, Number(element.scrollWidth || available));
        var ratio = Math.max(MIN_SQUEEZE, available / needed);
        setImportant(element, "letter-spacing", MIN_LETTER_SPACING_EM + "em");
        if (!fitsOnOneLine(element) || wrapped) {
            setImportant(element, "transform", "scaleX(" + ratio.toFixed(3) + ")");
            setImportant(element, "transform-origin", "center");
        }
        if (element.classList) element.classList.add("is-label-fit-squeezed");
        var fontPxFinal = MIN_FONT_PX;
        if (!fitsOnOneLine(element)) {
            // Сжатие упёрлось в предел читаемости, а подпись всё ещё шире
            // карточки. Опускаем кегль ниже пола: целая мелкая подпись лучше
            // крупной, но срезанной.
            var low = LAST_RESORT_FONT_PX;
            var high = MIN_FONT_PX;
            while (high - low > FONT_STEP_PX) {
                var middle = (low + high) / 2;
                setImportant(element, "font-size", middle + "px");
                if (fitsOnOneLine(element)) low = middle;
                else high = middle;
            }
            setImportant(element, "font-size", low + "px");
            fontPxFinal = low;
        }
        return {
            deferred: false,
            fontPx: fontPxFinal,
            wrapped: wrapped,
            squeezed: ratio,
            belowFloor: fontPxFinal < MIN_FONT_PX
        };
    }

    /**
     * Следить за подписью и подгонять её заново, когда это нужно.
     *
     * Ширина появляется позже отрисовки (карточку показали, экран повернули), а
     * содержимое приезжает подменой DOM с сервера. Оба случая — явная забота
     * модуля, а не побочный эффект: watch возвращает refit и stop.
     * Настройки (в том числе allowWrap) передаются в каждую подгонку.
     */
    function watch(element, options) {
        options = options || {};
        var target = element;
        if (!target) return null;
        var observers = [];

        function refit() {
            return fit(target, options);
        }

        if (typeof root.ResizeObserver === "function") {
            var sizeWatcher = new root.ResizeObserver(function () { refit(); });
            // Следим за коробкой подписи и, если просили, за самой карточкой:
            // у скрытой карточки ширина нулевая, и первое же её появление
            // приводит сюда.
            sizeWatcher.observe(target);
            if (options.box && options.box !== target) sizeWatcher.observe(options.box);
            observers.push(sizeWatcher);
        }

        if (options.container && typeof root.MutationObserver === "function") {
            var domWatcher = new root.MutationObserver(function () {
                var next = options.select ? options.select(options.container) : null;
                if (next && next !== target) {
                    target = next;
                    if (typeof root.ResizeObserver === "function" && observers[0]) {
                        observers[0].observe(target);
                    }
                }
                refit();
            });
            domWatcher.observe(options.container, {childList: true, subtree: true, characterData: true});
            observers.push(domWatcher);
        }

        refit();

        return {
            refit: refit,
            stop: function () {
                observers.forEach(function (observer) { observer.disconnect(); });
                observers.length = 0;
            },
            current: function () { return target; }
        };
    }

    root.EquipmentLabelFit = Object.freeze({
        MIN_FONT_PX: MIN_FONT_PX,
        MIN_SQUEEZE: MIN_SQUEEZE,
        LAST_RESORT_FONT_PX: LAST_RESORT_FONT_PX,
        fit: fit,
        watch: watch
    });

    if (typeof module === "object" && module.exports) {
        module.exports = root.EquipmentLabelFit;
    }
}(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this)));
