/**
 * Подгонка номеров самосвалов на экране машиниста.
 *
 * Само правило вмещения общее для обеих ролей и живёт в
 * equipment-label-fit-v1.js. Здесь только связка: у машиниста подписей не одна,
 * а до двенадцати разом, и экран целиком подменяется на каждом обновлении.
 *
 * Почему связка вообще нужна:
 *   1) EquipmentLabelFit.watch следит за ОДНОЙ подписью, а у нас их набор;
 *   2) после подмены оболочки (currentShell.replaceWith) все прежние узлы
 *      выброшены вместе с наблюдателями — подгонку надо звать заново;
 *   3) вкладка «Работа» может быть неактивной в момент отрисовки: ширины нет,
 *      замер дал бы ноль. Модуль в этом случае честно возвращает deferred и
 *      ничего не выставляет, а мы обязаны позвать его снова, когда ширина
 *      появится. Поэтому следим и за сменой вкладки.
 */
(function (root) {
    "use strict";

    if (!root || !root.document) return;

    var CARD_LABEL = ".eo-dashboard-truck-card strong";
    var doc = root.document;
    var pending = null;

    /* Номер техники стоит одной строкой всегда: настоящие номера бывают с
       пробелом («Тест 1», «ТМС 528»), и пробел — не повод разрывать номер
       надвое. Своей высоты под вторую строку у карточки хватает, поэтому
       запрещаем перенос явно, а не полагаемся на тесноту. */
    var FIT_OPTIONS = {allowWrap: false};

    function fitKey(label) {
        /* Отсчёты на экране меняют разметку каждую секунду, и наблюдатель
           будит подгонку впустую. Пересчитываем только то, у чего изменились
           сам номер или отведённое ему место. */
        return label.textContent + "|" + label.clientWidth + "|" + label.clientHeight;
    }

    function fitAll() {
        pending = null;
        var fitter = root.EquipmentLabelFit;
        /* Общий модуль приезжает отдельным файлом. Если его почему-то нет,
           экран обязан работать как раньше, а не падать: номер останется
           прежнего размера, это хуже вида, но не мешает работе. */
        if (!fitter || typeof fitter.fit !== "function") return;
        var labels = doc.querySelectorAll(CARD_LABEL);
        for (var index = 0; index < labels.length; index += 1) {
            var label = labels[index];
            var key = fitKey(label);
            if (label.dataset && label.dataset.eoNumberFitKey === key) continue;
            fitter.fit(label, FIT_OPTIONS);
            if (label.dataset) label.dataset.eoNumberFitKey = fitKey(label);
        }
    }

    function scheduleFit() {
        if (pending !== null) return;
        /* Подмена оболочки идёт пачкой изменений. Ждём кадр, чтобы посчитать
           один раз по готовой разметке, а не по каждому узлу отдельно. */
        pending = root.requestAnimationFrame
            ? root.requestAnimationFrame(fitAll)
            : root.setTimeout(fitAll, 16);
    }

    function start() {
        fitAll();

        if (typeof root.MutationObserver === "function") {
            new root.MutationObserver(scheduleFit).observe(doc.body, {
                childList: true,
                subtree: true,
                /* Смена вкладки не меняет разметку, но именно она даёт ширину
                   ранее скрытым карточкам. */
                attributes: true,
                attributeFilter: ["data-eo-active-tab"]
            });
        }

        if (typeof root.ResizeObserver === "function") {
            var resizeWatcher = new root.ResizeObserver(scheduleFit);
            /* Поворот экрана и клавиатура меняют ширину карточки, не трогая
               разметку. */
            resizeWatcher.observe(doc.documentElement);
        }

        root.addEventListener("orientationchange", scheduleFit);
    }

    if (doc.readyState === "loading") {
        doc.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }

    root.ExcavatorTruckNumberFit = Object.freeze({refit: fitAll});
}(typeof window !== "undefined" ? window : null));
