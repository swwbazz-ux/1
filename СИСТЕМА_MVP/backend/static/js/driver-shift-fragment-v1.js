/* Послойное обновление экрана водителя: снимок серверной разметки, состав
   барабана простоев и точечная подмена изменившихся узлов вместо полной
   перерисовки. Вынесено из driver-shift-v1.js без изменений. */
/* DRIVER_FRAGMENT_SNAPSHOT_START */
/* Снимок серверного экрана без переменных полей. Полная подмена DOM стоит на телефоне
   1–3 с замирания (растр 3D-барабана, градиентов круга, сотни строк журнала), а клиент
   реального времени просит «серверную правду» каждые 20 с. Поэтому свежий фрагмент
   сначала сравнивается с тем снимком, из которого собран текущий экран: совпал —
   подменять нечего. «full» — всё, что рисует сервер, кроме CSRF, номера действия,
   секундомеров и активной вкладки; «core» — то же без состояния простоя (его уже
   показала местная проекция) и без скрытых вкладок «Простои» и «Журнал». */
/* Области экрана, которые рисует клиент, и атрибуты, которые он же проставляет.
   Один список на два механизма: снимок их не сравнивает, послойное обновление не трогает. */
window.DRIVER_CLIENT_OWNED = "[data-driver-downtime-drum], [data-driver-drum-link], [data-driver-drum-link-active], [data-driver-free-bucket-current], [data-driver-free-bucket-grid], [data-driver-point-tile-status]";
window.DRIVER_CLIENT_ATTRS = [
    "data-driver-shell-bound", "data-driver-density", "data-driver-viewport-density",
    "data-driver-dial-fit-key", "data-driver-dial-raw", "data-driver-unload-submitting",
    "data-hold-complete", "data-driver-shift-dirty", "data-driver-shift-opening-dirty",
    "data-driver-shift-opening-pending", "data-driver-free-bucket-active"
];
window.driverAttributeIsClientOwned = function (name) {
    return window.DRIVER_CLIENT_ATTRS.indexOf(name) >= 0
        || name.indexOf("data-driver-free-bucket-baseline") === 0
        || name.indexOf("data-driver-drum") === 0;
};
window.driverFragmentSnapshot = function (shell) {
    if (!shell || typeof shell.cloneNode !== "function") return null;
    var copy = shell.cloneNode(true);
    var all = function (selector) { return Array.prototype.slice.call(copy.querySelectorAll(selector)); };
    copy.removeAttribute("data-active-tab");
    all("[data-driver-fragment-shell]").forEach(function (node) { node.parentNode.removeChild(node); });
    all("input[name='csrfmiddlewaretoken'], input[name='client_action_id']").forEach(function (node) { node.setAttribute("value", ""); });
    all("[data-driver-tab-panel]").forEach(function (node) { node.classList.remove("is-active"); });
    all("[data-driver-active-downtime-id]").forEach(function (node) {
        ["data-driver-active-elapsed-seconds", "data-driver-shift-downtime-seconds", "data-driver-downtime-calculated-at"]
            .forEach(function (name) { node.removeAttribute(name); });
    });
    all("[data-driver-active-duration], [data-driver-reason-duration], [data-driver-drum-total]").forEach(function (node) {
        node.textContent = "";
        node.removeAttribute("hidden");
    });
    all("[data-driver-reason-seconds]").forEach(function (node) { node.removeAttribute("data-driver-reason-seconds"); });
    all("[data-driver-downtime-reason-button]").forEach(function (node) { node.removeAttribute("aria-label"); });
    all("[data-driver-report-downtime-total]").forEach(function (node) { node.removeAttribute("data-driver-report-downtime-total"); });
    /* Области, которые рисует клиент, из сравнения исключаем: серверная разметка с живой
       там не совпадёт никогда. Барабан ведёт свой скрипт (копии граней, углы, подсветка);
       свободный ковш рисует свои плитки из отдельных данных ответа; отметка «Текущая»
       на плитке точки ставится сразу при выборе. Состав причин при этом остаётся под
       наблюдением: он виден на вкладке «Простои», а она в снимок входит. */
    all(window.DRIVER_CLIENT_OWNED).forEach(function (node) { node.parentNode.removeChild(node); });
    /* Атрибуты, которые ставит клиент: в серверной разметке их нет. */
    [copy].concat(all("*")).forEach(function (node) {
        Array.prototype.slice.call(node.attributes).forEach(function (attr) {
            if (window.driverAttributeIsClientOwned(attr.name)) node.removeAttribute(attr.name);
        });
    });
    copy.removeAttribute("style");
    /* Подпись круга: сервер отдаёт её одной строкой, а подгонка разбивает на строки-спаны
       и проставляет кегль. Сравниваем исходный текст. */
    all("[data-driver-dial-label]").forEach(function (node) {
        var raw = node.dataset.driverDialRaw || node.textContent || "";
        node.textContent = String(raw).trim().replace(/\s+/g, " ");
        ["data-driver-dial-raw", "data-driver-dial-fit-key", "style"].forEach(function (name) {
            node.removeAttribute(name);
        });
        ["is-single-medium", "is-two-line", "is-three-line"].forEach(function (name) {
            node.classList.remove(name);
        });
    });
    all(".driver-work-dial-core").forEach(function (node) { node.classList.remove("has-multiline-label"); });
    /* Длительности в путёвке считаются на сервере в минутах и растут, пока идёт простой:
       сами строки (состав журнала) сравниваем, их секундомеры — нет. */
    all("[data-driver-tab-panel='manifest'] .driver-report-row").forEach(function (row) {
        row.removeAttribute("data-duration");
        Array.prototype.slice.call(row.querySelectorAll("strong")).forEach(function (node) { node.textContent = ""; });
    });
    var full = copy.outerHTML;
    all("[data-driver-tab-panel='downtimes'], [data-driver-tab-panel='manifest']").forEach(function (node) { node.parentNode.removeChild(node); });
    all(".driver-work-dial, [data-driver-work-dial-control]").forEach(function (node) {
        Array.prototype.slice.call(node.classList).forEach(function (name) {
            if (name.indexOf("is-waiting-") === 0) node.classList.remove(name);
        });
    });
    all(".driver-work-note").forEach(function (node) { node.textContent = ""; });
    return {full: full, core: copy.outerHTML};
};
/* Базовая линия — разметка, из которой собран экран при загрузке (до любых правок скриптами). */
window.driverAppliedFragmentSnapshot = window.driverFragmentSnapshot(document.querySelector("[data-driver-shell]"));
/* Скрытые вкладки отстали от сервера после нашего же простоя: при их открытии экран подтягивается целиком. */
window.driverDomBehindBaseline = false;
window.driverForceFragmentApply = false;
/* Барабан простоев — самый дорогой узел экрана: двенадцать граней, повёрнутых в 3D.
   При подмене экрана браузер растеризует их заново; по замеру на телефоне водителя это
   ~0,28 с из ~0,45 с всей подмены, хотя состав причин почти никогда не меняется.
   Если он совпал, переносим ЖИВОЙ узел в свежий экран вместо серверной копии: слои
   сохраняются, а скрипт барабана видит тот же элемент и не пересобирает геометрию.
   Своих обработчиков на барабане нет (он слушает document), поэтому перенос безопасен;
   во время жеста обновление и так отложено — см. isDriverOperationalRefreshUnsafe. */
/* Послойное обновление экрана. Подменять весь экран дорого: на телефоне водителя это
   0,6–0,9 с замирания, из них около 0,25 с только на барабан простоев. Здесь вместо
   подмены живые узлы остаются на месте, а с сервера переносятся лишь изменившиеся
   атрибуты и тексты. Это же снимает главную опасность частичного обновления: узлы не
   пересоздаются, значит обработчики нажатий никуда не деваются и заново их вешать не надо.

   Правила:
   — структура должна совпадать; любое добавление или удаление узла — отказ, дальше
     работает обычная полная подмена с полной перепривязкой (иначе новый узел остался бы
     без обработчика);
   — барабан непрозрачен: его содержимое ведёт свой скрипт. Если сервер изменил состав
     причин, барабан заменяется целиком и пересобирается;
   — клиентские атрибуты и классы (подгонка подписи, плотность, состояние жеста) не
     стираются серверными.
   Результат проверяется снимком: если после обновления экран не совпал с серверным,
   вызывающий код откатывается на полную подмену. */
window.driverDrumComposition = function (root) {
    var drum = root && root.querySelector ? root.querySelector("[data-driver-downtime-drum]") : null;
    if (!drum) return "";
    var parts = [String(drum.dataset.driverQuickMin || "")];
    Array.prototype.forEach.call(
        drum.querySelectorAll("[data-driver-drum-card]:not([data-driver-drum-clone])"),
        function (card) {
            var label = card.querySelector(".driver-drum-card-label");
            parts.push([
                card.dataset.driverDrumReasonId || "",
                card.dataset.driverDrumQuick || "",
                card.classList.contains("is-unavailable") ? "1" : "0",
                label ? label.textContent.trim() : ""
            ].join(":"));
        }
    );
    return parts.join("|");
};

window.driverMorphShell = function (live, fresh) {
    /* Классы, которые ставит клиент и которых нет в серверной разметке. */
    var CLIENT_CLASSES = [
        "has-multiline-label", "is-single-medium", "is-two-line", "is-three-line",
        "is-touch-armed", "is-holding", "is-pending", "is-dragging"
    ];
    var OPAQUE = window.DRIVER_CLIENT_OWNED;
    /* Узлы, чей style ведёт клиент: высота окна, подогнанный кегль подписи. */
    var KEEP_STYLE = "[data-driver-shell], [data-driver-dial-label]";

    function preserved(name) {
        return window.driverAttributeIsClientOwned(name);
    }

    function syncAttributes(a, b) {
        var keepStyle = !!(a.matches && a.matches(KEEP_STYLE));
        var i, attr;
        for (i = b.attributes.length - 1; i >= 0; i -= 1) {
            attr = b.attributes[i];
            if (attr.name === "class" || preserved(attr.name)) continue;
            if (attr.name === "style" && keepStyle) continue;
            if (a.getAttribute(attr.name) !== attr.value) a.setAttribute(attr.name, attr.value);
        }
        for (i = a.attributes.length - 1; i >= 0; i -= 1) {
            attr = a.attributes[i];
            if (attr.name === "class" || preserved(attr.name)) continue;
            if (attr.name === "style" && keepStyle) continue;
            if (!b.hasAttribute(attr.name)) a.removeAttribute(attr.name);
        }
        var mine = [];
        CLIENT_CLASSES.forEach(function (name) { if (a.classList.contains(name)) mine.push(name); });
        var next = b.getAttribute("class");
        if ((a.getAttribute("class") || "") !== (next || "")) {
            if (next === null) a.removeAttribute("class");
            else a.setAttribute("class", next);
        }
        mine.forEach(function (name) { a.classList.add(name); });
    }

    function morph(a, b) {
        if (a.nodeType !== b.nodeType) return false;
        if (a.nodeType === 3 || a.nodeType === 8) {
            if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue;
            return true;
        }
        if (a.nodeType !== 1) return true;
        if (a.tagName !== b.tagName) return false;
        if (a.matches && a.matches(OPAQUE)) {
            /* Барабан: сменился состав причин — обновление послойно невозможно, нужен
               полный пересбор. Остальные клиентские области просто не трогаем. */
            if (a.hasAttribute("data-driver-downtime-drum")) {
                return window.driverDrumComposition(a.parentNode) === window.driverDrumComposition(b.parentNode);
            }
            return true;
        }
        if (a.matches && a.matches("[data-driver-dial-label]")) {
            /* Подпись круга разбита подгонкой на строки-спаны, у сервера она одной строкой.
               Содержимое не трогаем: меняем исходный текст и сбрасываем ключ подгонки,
               дальше подпись перерисует и подгонит сама подгонка. */
            syncAttributes(a, b);
            var freshText = String(b.textContent || "").trim().replace(/\s+/g, " ");
            var liveText = String(a.dataset.driverDialRaw || a.textContent || "").trim().replace(/\s+/g, " ");
            if (freshText && freshText !== liveText) {
                a.dataset.driverDialRaw = freshText;
                delete a.dataset.driverDialFitKey;
            }
            return true;
        }
        syncAttributes(a, b);
        var an = a.firstChild;
        var bn = b.firstChild;
        while (an && bn) {
            if (!morph(an, bn)) return false;
            an = an.nextSibling;
            bn = bn.nextSibling;
        }
        return !an && !bn;
    }

    try {
        return morph(live, fresh);
    } catch (error) {
        return false;
    }
};
/* DRIVER_FRAGMENT_SNAPSHOT_END */
