/* Код экрана водителя. Вынесен из templates/users/driver_shift.html без изменений. */
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
/* DRIVER_ROLE_HOLD_GUARD_START */
window.createDriverRoleHoldGuard = function (options) {
    options = options || {};
    var holdMs = Math.max(0, Number(options.holdMs || 2000));
    var timerId = null;
    var frameId = null;
    var startedAt = 0;
    var holding = false;
    var destroyed = false;

    function roleIsReadonly() {
        return (
            typeof window.isAppRoleReadonly === "function"
            && window.isAppRoleReadonly()
        );
    }

    function clearScheduledCallbacks() {
        if (timerId !== null) {
            window.clearTimeout(timerId);
            timerId = null;
        }
        if (frameId !== null) {
            window.cancelAnimationFrame(frameId);
            frameId = null;
        }
    }

    function reset(force) {
        var hadActiveHold = holding || timerId !== null || frameId !== null;
        clearScheduledCallbacks();
        holding = false;
        startedAt = 0;
        if ((hadActiveHold || force === true) && typeof options.onReset === "function") {
            options.onReset();
        }
    }

    function drawProgress() {
        frameId = null;
        if (!holding) {
            return;
        }
        if (roleIsReadonly()) {
            reset(true);
            return;
        }
        // Most role actions retain their existing progress callback. The
        // unload dial deliberately has none, so a weak WebView has no
        // per-frame JavaScript work at all.
        if (typeof options.onProgress !== "function") {
            return;
        }
        var elapsed = Math.max(0, Date.now() - startedAt);
        var percent = Math.max(0, Math.min(100, (elapsed / holdMs) * 100));
        options.onProgress(percent);
        if (holding && percent < 100) {
            frameId = window.requestAnimationFrame(drawProgress);
        }
    }

    function complete() {
        timerId = null;
        if (!holding) {
            return;
        }
        if (roleIsReadonly()) {
            reset(true);
            return;
        }
        holding = false;
        if (frameId !== null) {
            window.cancelAnimationFrame(frameId);
            frameId = null;
        }
        if (typeof options.onProgress === "function") {
            options.onProgress(100);
        }
        if (roleIsReadonly()) {
            reset(true);
            return;
        }
        if (typeof options.onComplete === "function") {
            options.onComplete();
        }
    }

    function start() {
        reset(true);
        if (destroyed || roleIsReadonly()) {
            return false;
        }
        holding = true;
        startedAt = Date.now();
        if (typeof options.onStart === "function") {
            options.onStart();
        }
        if (roleIsReadonly()) {
            reset(true);
            return false;
        }
        // A dial that supplies no callback is deliberately CSS-only: retain the
        // completion timer, but do not even enter a requestAnimationFrame loop.
        if (typeof options.onProgress === "function") {
            drawProgress();
        }
        if (holding) {
            timerId = window.setTimeout(complete, holdMs);
        }
        return holding;
    }

    function resetOnInactiveRole(event) {
        if (event && event.detail && event.detail.active === false) {
            reset(true);
        }
    }

    window.addEventListener("active-role-state-changed", resetOnInactiveRole);

    return {
        start: start,
        reset: function () {
            reset(false);
        },
        cancel: function () {
            reset(true);
        },
        destroy: function () {
            if (destroyed) {
                return;
            }
            destroyed = true;
            window.removeEventListener("active-role-state-changed", resetOnInactiveRole);
            reset(true);
        }
    };
};
/* DRIVER_ROLE_HOLD_GUARD_END */

/* DRIVER_UNLOAD_RECOVERY_START */
window.createDriverUnloadRecovery = function (options) {
    options = options || {};
    var storagePrefix = String(options.storagePrefix || "driver-trip-unloaded:");
    var storage = options.storage || null;
    var input = options.input || null;
    var tripId = String(options.tripId || "").trim();
    var storageKey = tripId ? storagePrefix + tripId : "";
    var eventTarget = options.eventTarget || window;
    var destroyed = false;

    function storageKeys() {
        var keys = [];
        if (!storage) {
            return keys;
        }
        try {
            for (var index = 0; index < storage.length; index += 1) {
                var key = storage.key(index);
                if (key && key.indexOf(storagePrefix) === 0) {
                    keys.push(key);
                }
            }
        } catch (error) {}
        return keys;
    }

    function removeStorageKey(key) {
        if (!storage || !key) {
            return;
        }
        try {
            storage.removeItem(key);
        } catch (error) {}
    }

    function clearStaleStorage(keepKey) {
        storageKeys().forEach(function (key) {
            if (!keepKey || key !== keepKey) {
                removeStorageKey(key);
            }
        });
    }

    function readStoredActionId() {
        if (!storage || !storageKey) {
            return "";
        }
        try {
            return String(storage.getItem(storageKey) || "").trim();
        } catch (error) {
            return "";
        }
    }

    function storeActionId(actionId) {
        if (!storage || !storageKey || !actionId) {
            return;
        }
        try {
            storage.setItem(storageKey, actionId);
        } catch (error) {}
    }

    function ensureActionId() {
        if (!input || !storageKey) {
            return "";
        }
        var actionId = String(input.value || "").trim() || readStoredActionId();
        if (!actionId && typeof options.generateActionId === "function") {
            actionId = String(options.generateActionId() || "").trim();
        }
        if (actionId) {
            input.value = actionId;
            storeActionId(actionId);
        }
        return actionId;
    }

    function recoverPendingUi(event) {
        if (destroyed || (event && event.type === "pageshow" && event.persisted !== true)) {
            return;
        }
        ensureActionId();
        if (typeof options.onRecover === "function") {
            options.onRecover(event || null);
        }
    }

    clearStaleStorage(storageKey);
    if (storageKey && input) {
        ensureActionId();
        if (eventTarget && typeof eventTarget.addEventListener === "function") {
            eventTarget.addEventListener("pageshow", recoverPendingUi);
        }
    } else {
        clearStaleStorage("");
    }

    return {
        ensureActionId: ensureActionId,
        storageKey: storageKey,
        recover: recoverPendingUi,
        destroy: function () {
            if (destroyed) {
                return;
            }
            destroyed = true;
            if (eventTarget && typeof eventTarget.removeEventListener === "function") {
                eventTarget.removeEventListener("pageshow", recoverPendingUi);
            }
        }
    };
};
/* DRIVER_UNLOAD_RECOVERY_END */

/* DRIVER_UNLOAD_GESTURE_START */
window.bindDriverUnloadGesture = function (options) {
    options = options || {};
    var form = options.form;
    var button = options.button;
    var holdGuard = options.holdGuard;
    if (!form || !button || !holdGuard) {
        return null;
    }
    var activePointerId = null;
    var suppressPointerClickUntil = 0;

    function isOneTap() {
        return form.dataset.driverUnloadOneTap === "true";
    }

    function canTrigger() {
        return (
            !button.disabled
            && !document.hidden
            && (!options.canTrigger || options.canTrigger())
        );
    }

    function clearActivePointer() {
        var pointerId = activePointerId;
        activePointerId = null;
        button.classList.remove("is-touch-armed");
        if (pointerId !== null && typeof button.releasePointerCapture === "function") {
            try {
                button.releasePointerCapture(pointerId);
            } catch (error) { /* Capture may already have been released by the WebView. */ }
        }
    }

    function triggerOneTap(event) {
        if (event && typeof event.preventDefault === "function") {
            event.preventDefault();
        }
        if (!isOneTap() || !canTrigger()) {
            return false;
        }
        clearActivePointer();
        return options.onOneTap() !== false;
    }

    function onPointerDown(event) {
        if (
            !canTrigger()
            || activePointerId !== null
            || event.isPrimary === false
            || (event.button !== undefined && event.button !== 0)
        ) {
            return;
        }
        activePointerId = event.pointerId;
        try {
            button.setPointerCapture?.(event.pointerId);
        } catch (error) { /* Pointer-up/cancel still applies without capture. */ }
        if (isOneTap()) {
            event.preventDefault();
            button.classList.add("is-touch-armed");
            return;
        }
        // A completed normal hold is an application action, never a native text
        // selection gesture.  This matters on budget Android WebViews, where a
        // long press otherwise opens the selection toolbar over the dial.
        event.preventDefault();
        holdGuard.start();
    }

    function onPointerUp(event) {
        if (activePointerId === null || activePointerId !== event.pointerId) {
            return;
        }
        if (isOneTap()) {
            suppressPointerClickUntil = Date.now() + 750;
            triggerOneTap(event);
            clearActivePointer();
            return;
        }
        clearActivePointer();
        if (form.dataset.holdComplete !== "true") {
            holdGuard.reset();
        }
    }

    function cancelGesture() {
        if (activePointerId !== null) suppressPointerClickUntil = Date.now() + 750;
        clearActivePointer();
        if (form.dataset.holdComplete !== "true") {
            holdGuard.reset();
        }
    }

    function onPointerCancel(event) {
        if (activePointerId === null || activePointerId !== event.pointerId) {
            return;
        }
        /* Prevent a compatibility click from resurrecting a cancelled tap. */
        suppressPointerClickUntil = Date.now() + 750;
        cancelGesture();
    }

    function onPointerLeave(event) {
        /* Pointer capture keeps a short tap reliable even when a finger moves
           a few pixels outside the round button. A real pointercancel still
           aborts the action, while the normal hold keeps its old reset rule. */
        if (activePointerId === event.pointerId && !isOneTap() && form.dataset.holdComplete !== "true") {
            cancelGesture();
        }
    }

    function onVisibilityChange() {
        if (document.hidden || document.visibilityState === "hidden") {
            cancelGesture();
        }
    }

    function onClick(event) {
        event.preventDefault();
        /* Keyboard activation and old WebView engines may emit click without
           Pointer Events. A pointer-up compatibility click must not repeat it. */
        if (
            (event.button !== undefined && event.button !== 0)
            || (event.detail !== 0 && Date.now() < suppressPointerClickUntil)
        ) {
            return;
        }
        triggerOneTap();
    }

    function onSubmit(event) {
        if (options.canTrigger && !options.canTrigger()) {
            event.preventDefault();
            holdGuard.cancel();
            return;
        }
        if (!isOneTap() && form.dataset.holdComplete !== "true") {
            event.preventDefault();
            holdGuard.reset();
        }
    }

    button.addEventListener("pointerdown", onPointerDown);
    button.addEventListener("pointerup", onPointerUp);
    button.addEventListener("pointercancel", onPointerCancel);
    button.addEventListener("lostpointercapture", onPointerCancel);
    button.addEventListener("pointerleave", onPointerLeave);
    button.addEventListener("click", onClick);
    form.addEventListener("submit", onSubmit);
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("blur", cancelGesture);

    return {
        triggerOneTap: triggerOneTap,
        destroy: function () {
            button.removeEventListener("pointerdown", onPointerDown);
            button.removeEventListener("pointerup", onPointerUp);
            button.removeEventListener("pointercancel", onPointerCancel);
            button.removeEventListener("lostpointercapture", onPointerCancel);
            button.removeEventListener("pointerleave", onPointerLeave);
            button.removeEventListener("click", onClick);
            form.removeEventListener("submit", onSubmit);
            document.removeEventListener("visibilitychange", onVisibilityChange);
            window.removeEventListener("blur", cancelGesture);
            cancelGesture();
        }
    };
};
/* DRIVER_UNLOAD_GESTURE_END */

if (typeof window.bindAchievementPrizeUnlock === "function") {
    window.bindAchievementPrizeUnlock();
}

function isDriverOperationalRefreshUnsafe(shell) {
    if (document.hidden) return true;
    shell = shell || document.querySelector("[data-driver-shell]");
    if (!shell) return false;
    if (
        typeof window !== "undefined" &&
        Number(window.driverOfflinePendingCount || 0) > 0
    ) return true;
    var active = document.activeElement;
    var activeTag = active && active.tagName ? active.tagName.toLowerCase() : "";
    if (
        active &&
        shell.contains(active) &&
        (active.isContentEditable || activeTag === "input" || activeTag === "textarea" || activeTag === "select")
    ) {
        return true;
    }
    var openingShiftForm = shell.querySelector(".driver-shift-opening-form");
    if (openingShiftForm && (
        openingShiftForm.dataset.driverShiftOpeningDirty === "true" ||
        openingShiftForm.dataset.driverShiftOpeningPending === "true" ||
        openingShiftForm.querySelector(".errorlist")
    )) {
        return true;
    }
    var activeShiftForm = shell.querySelector("[data-driver-shift-close-form]");
    if (activeShiftForm && (
        activeShiftForm.dataset.driverShiftDirty === "true" ||
        activeShiftForm.querySelector(".errorlist")
    )) {
        return true;
    }
    return !!(
        shell.querySelector(".is-touch-armed, .is-holding, .is-pending, .is-dragging, .is-lifting, .is-dropping, .is-snapping, .driver-drum-ghost, [data-driver-point-sheet]:not([hidden]), [data-driver-free-bucket-sheet]:not([hidden])") ||
        document.querySelector("[data-driver-pwa-update-modal]:not([hidden]), .app-confirm-modal:not([hidden])")
    );
}
window.driverHasPendingWork = isDriverOperationalRefreshUnsafe;
if (
    window.AppPwaContractGuard
    && typeof window.AppPwaContractGuard.registerUnsafeCheck === "function"
) {
    window.AppPwaContractGuard.registerUnsafeCheck(isDriverOperationalRefreshUnsafe);
}

/* DRIVER_VOICE_GUARD_START */
/* Одно рабочее событие приходит на экран по трём путям: ранним сигналом сразу
   после ответа сервера, слушателем «operational-state-refresh-applied» и
   сравнением DOM после подмены. Раньше каждый путь решал сам, и отметка о
   произнесённом ставилась уже после возврата из моста — то есть два
   синхронных вызова подряд успевали пройти проверку оба.

   Владелец решения теперь один, и заявка ставится синхронно, до обращения к
   мосту. Ключ — идентификатор самой операции (назначение или рейс), а не
   версия состояния: его одинаково знают и событие сервера, и разметка. */
window.DriverVoiceGuard = (function () {
    var LIMIT = 32;
    var order = [];
    var states = Object.create(null);

    function forget(opKey) {
        delete states[opKey];
        var position = order.indexOf(opKey);
        if (position !== -1) order.splice(position, 1);
    }

    function claim(opKey) {
        if (!opKey) return false;
        if (states[opKey]) return false;
        states[opKey] = "claimed";
        order.push(opKey);
        while (order.length > LIMIT) {
            delete states[order.shift()];
        }
        return true;
    }

    /* Заявка закрывается, когда звук фактически пошёл либо когда событием уже
       владеет другой канал. Освобождать можно только полную тишину, иначе
       следующий путь повторит то, что человек уже услышал. */
    function finalize(opKey) {
        if (opKey && states[opKey]) states[opKey] = "announced";
    }

    function release(opKey) {
        if (opKey && states[opKey] === "claimed") forget(opKey);
    }

    function state(opKey) {
        return (opKey && states[opKey]) || "";
    }

    return {claim: claim, finalize: finalize, release: release, state: state};
})();

function settleDriverVoiceClaim(opKey, result) {
    if (!opKey) return;
    /* playDriverSound и резервный веб-звук отвечают простым признаком
       «сыграло», нативный мост — объектом. Прозвучавшее в любой форме
       закрывает заявку. */
    var announced = result === true || !!(result && result.announced === true);
    var ownedElsewhere = !!(result && String(result.reason || "") === "already_announced");
    if (announced || ownedElsewhere) {
        window.DriverVoiceGuard.finalize(opKey);
        return;
    }
    window.DriverVoiceGuard.release(opKey);
}

/* Единственная точка обращения к мосту под заявкой.

   Мост может не только вернуть отказ, но и бросить исключение — синхронно при
   вызове или отклонением промиса. Без освобождения заявка осталась бы в
   состоянии «принято» до перезагрузки страницы, и событие замолчало бы
   навсегда: ни резервный путь по DOM, ни повторный опрос его уже не подняли
   бы. Любой сбой возвращает операцию следующему источнику. */
function announceDriverVoiceUnderClaim(opKey, produceAnnouncement) {
    var pending;
    try {
        pending = produceAnnouncement();
    } catch (error) {
        window.DriverVoiceGuard.release(opKey);
        return;
    }
    Promise.resolve(pending).then(function (result) {
        settleDriverVoiceClaim(opKey, result);
    }, function () {
        window.DriverVoiceGuard.release(opKey);
    });
}

/* Повторяет правило нативного heartbeat: водителю озвучиваются выданное
   назначение и снятое назначение. Из дельты берётся событие с наибольшей
   версией — если назначение успели перевыдать, прозвучит последнее, а не
   первое. Актуальность определяется состоянием, а не возрастом события. */
function latestDriverAssignmentEvent(context) {
    var selected = null;
    var events = context && Array.isArray(context.events) ? context.events : [];
    events.forEach(function (event) {
        var payload = event && event.payload ? event.payload : null;
        var version = Number(event && event.version || 0);
        if (!payload || !event || event.type !== "assignment_changed") return;
        if (selected && version <= selected.eventVersion) return;
        if (String(event.object_type || "") !== "HaulAssignment") return;
        var assignmentId = String(event.object_id || "").trim();
        if (!assignmentId) return;
        var action = String(payload.action || "");
        if (action === "assignment_pending") {
            selected = {
                eventVersion: version,
                assignmentId: assignmentId,
                kind: "assign",
                excavatorNumber: String(payload.target_excavator_number || "").trim()
            };
            return;
        }
        if (action === "release_applied") {
            selected = {
                eventVersion: version,
                assignmentId: assignmentId,
                kind: "release",
                excavatorNumber: ""
            };
        }
    });
    return selected;
}

window.handleOperationalStateSignals = function (context) {
    playDriverDumpPointAlert(context);
    var assignment = latestDriverAssignmentEvent(context);
    if (!assignment) return;
    playDriverAssignmentAlert(
        assignment.eventVersion,
        assignment.kind,
        assignment.excavatorNumber,
        {opKey: "assign:" + assignment.assignmentId}
    );
};
/* DRIVER_VOICE_GUARD_END */

function playDriverSound(name) {
    if (!window.MobileOperationalSounds || typeof window.MobileOperationalSounds.play !== "function") {
        return Promise.resolve(false);
    }
    return window.MobileOperationalSounds.play(name);
}

function playDriverVoice(cue, voice, options) {
    options = options || {};
    if (!window.MobileOperationalSounds || typeof window.MobileOperationalSounds.announceOperational !== "function") {
        return playDriverSound(cue);
    }
    return window.MobileOperationalSounds.announceOperational({
        cue: cue,
        voice: voice,
        eventVersion: Number(options.eventVersion || 0),
        eventKey: String(options.eventKey || "")
    });
}

function playDriverAssignmentAlert(eventVersion, assignmentKind, excavatorNumber, options) {
    options = options || {};
    var opKey = String(options.opKey || "");
    /* Заявка ставится до вибрации и до моста: иначе два синхронных вызова
       подряд успевают обратиться к проигрывателю оба. */
    if (opKey && !window.DriverVoiceGuard.claim(opKey)) {
        return;
    }
    if (navigator.vibrate) {
        try { navigator.vibrate([180, 90, 180]); } catch (error) {}
    }
    if (assignmentKind !== "assign") {
        announceDriverVoiceUnderClaim(opKey, function () {
            return playDriverVoice("truck_assigned", "voice_assignment_removed", {
                eventVersion: Number(eventVersion || 0),
                eventKey: "driver_assignment"
            });
        });
        return;
    }
    if (window.MobileOperationalSounds && typeof window.MobileOperationalSounds.announceEquipment === "function") {
        announceDriverVoiceUnderClaim(opKey, function () {
            return window.MobileOperationalSounds.announceEquipment({
                cue: "truck_assigned",
                action: "driver_excavator_assigned",
                equipmentNumber: String(excavatorNumber || ""),
                fallbackVoice: "voice_excavator_assigned",
                eventVersion: Number(eventVersion || 0),
                eventKey: "driver_assignment"
            });
        });
        return;
    }
    announceDriverVoiceUnderClaim(opKey, function () {
        return playDriverVoice("truck_assigned", "voice_excavator_assigned", {
            eventVersion: Number(eventVersion || 0),
            eventKey: "driver_assignment"
        });
    });
}

/* Предложение снять назначение только привлекает внимание коротким пиком.

   Голосовая фраза «Назначение снято» принадлежит событию «release_applied»:
   его одинаково видят и ранний обработчик, и нативный heartbeat, и оба
   передают одну и ту же версию события, поэтому фраза звучит ровно один раз.
   Произнести её здесь, на «release_pending», значило бы записать нативный
   маркер на меньшей версии — «release_applied» пришёл бы с большей и
   прозвучал бы вторым, с опозданием.

   Пик идёт мимо аннонсера, через обычный проигрыватель сигналов, и никакого
   маркера не пишет. Пространство ключа отдельное: ключ «assign:<номер>»
   принадлежит самому снятию, и пик не имеет права его закрывать. */
function playDriverReleaseOfferCue(assignmentId) {
    var opKey = assignmentId ? "release-cue:" + String(assignmentId) : "";
    if (opKey && !window.DriverVoiceGuard.claim(opKey)) {
        return;
    }
    if (navigator.vibrate) {
        try { navigator.vibrate([180, 90, 180]); } catch (error) {}
    }
    announceDriverVoiceUnderClaim(opKey, function () {
        return playDriverSound("assignment_removed_notice");
    });
}

function reportDriverAudioDiagnostic(stage, details, extra) {
    if (!document.body || document.body.dataset.nativeApp !== "true" || !window.fetch) return;
    var payload = {
        message: "driver_audio:" + String(stage || "unknown"),
        source: "driver-audio",
        stack: JSON.stringify({details: details || {}, extra: extra || {}}),
        screen: "driver",
        role: "driver",
        appVersion: document.body.dataset.appShellVersion || ""
    };
    try {
        window.fetch("/client-error/", {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            keepalive: true,
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload)
        }).catch(function () {});
    } catch (error) {}
}

/* Отдельной отметки о произнесённом здесь больше нет: она обновлялась только
   после возврата из моста и от синхронного двойного вызова не защищала.
   Владелец решения — DriverVoiceGuard, заявка по ключу рейса. */
function latestDriverDumpPointEvent(context) {
    var selected = null;
    var events = context && Array.isArray(context.events) ? context.events : [];
    events.forEach(function (event) {
        var payload = event && event.payload ? event.payload : null;
        var version = Number(event && event.version || 0);
        if (
            !payload
            || event.type !== "trip_changed"
            || payload.action !== "truck_loaded"
            || (selected && version <= selected.eventVersion)
        ) {
            return;
        }
        var tripId = Number(payload.trip_id || 0);
        var dumpPointId = Number(payload.assigned_dump_point_id || payload.dump_point_id || 0);
        var dumpPointName = String(payload.dump_point_name || "").trim();
        if (!tripId || (!dumpPointId && !dumpPointName)) return;
        selected = {
            eventVersion: version,
            tripId: tripId,
            dumpPointId: dumpPointId,
            dumpPointName: dumpPointName
        };
    });
    return selected;
}

function playDriverDumpPointAlert(context) {
    var details = latestDriverDumpPointEvent(context);
    if (!details) return;
    var opKey = "dump:" + String(details.tripId || "");
    if (!window.DriverVoiceGuard.claim(opKey)) {
        return;
    }
    reportDriverAudioDiagnostic("trigger", details, {
        capacitor: !!window.Capacitor,
        nativeSound: !!(window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.NativeSound)
    });
    var sounds = window.MobileOperationalSounds;
    var announce;
    try {
        announce = sounds && typeof sounds.announceDumpPoint === "function"
            ? sounds.announceDumpPoint(details)
            : Promise.resolve({supported: false, announced: false});
    } catch (error) {
        /* Синхронный бросок моста не должен запирать рейс до перезагрузки. */
        window.DriverVoiceGuard.release(opKey);
        return;
    }
    /* Резервный сигнал тоже закрывает заявку: он уже прозвучал, и повторять
       его следующим путём нельзя. Освобождается заявка только тогда, когда не
       прозвучало ничего. */
    function settleWithFallback(nativeResult, withVibration) {
        if (withVibration && navigator.vibrate) {
            try { navigator.vibrate([180, 90, 180]); } catch (error) {}
        }
        return Promise.resolve(playDriverSound("truck_assigned")).then(function (played) {
            settleDriverVoiceClaim(opKey, played === true ? true : nativeResult);
        });
    }
    Promise.resolve(announce).then(function (nativeResult) {
        nativeResult = nativeResult || {supported: false, announced: false};
        reportDriverAudioDiagnostic("result", details, nativeResult);
        if (sounds && typeof sounds.diagnostics === "function") {
            window.setTimeout(function () {
                sounds.diagnostics().then(function (diagnostics) {
                    reportDriverAudioDiagnostic("playback", details, diagnostics);
                });
            }, 1600);
        }
        if (nativeResult.supported && nativeResult.announced) {
            settleDriverVoiceClaim(opKey, nativeResult);
            return;
        }
        if (nativeResult.supported && nativeResult.reason === "already_announced") {
            settleDriverVoiceClaim(opKey, nativeResult);
            return;
        }
        if (nativeResult.supported) {
            return settleWithFallback(nativeResult, false);
        }
        if (document.body && document.body.dataset.nativeApp === "true" && !nativeResult.supported) {
            return settleWithFallback(nativeResult, false);
        }
        return settleWithFallback(nativeResult, true);
    }).catch(function () {
        window.DriverVoiceGuard.release(opKey);
    });
}

window.addEventListener("operational-state-refresh-applied", function (event) {
    playDriverDumpPointAlert(event && event.detail ? event.detail : null);
});

function driverAppliedActionVoice(actionKind, freshShell) {
    if (actionKind === "shift-open") {
        return freshShell.querySelector("[data-driver-shift-close-button]")
            ? {cue: "shift_start", voice: "voice_shift_opened"}
            : {cue: "action_error", voice: "voice_action_failed"};
    }
    if (actionKind === "shift-close") {
        return freshShell.querySelector("[data-driver-shift-close-button]")
            ? {cue: "action_error", voice: "voice_action_failed"}
            : {cue: "shift_end", voice: "voice_shift_closed"};
    }
    if (actionKind === "complete-trip") {
        return {cue: "action_ok", voice: "voice_trip_finished"};
    }
    return {cue: "action_ok", voice: ""};
}

function syncDriverTabMarkup(shell, tab) {
    if (!shell) return;
    shell.dataset.activeTab = tab;
    shell.querySelectorAll("[data-driver-tab-panel]").forEach(function (panel) {
        panel.classList.toggle("is-active", panel.dataset.driverTabPanel === tab);
    });
    document.querySelectorAll("[data-driver-tab-open]").forEach(function (button) {
        button.classList.toggle("is-active", button.dataset.driverTabOpen === tab);
    });
}

window.applyOperationalStateRefresh = function (context) {
    // Все события дельты — наши же подтверждённые простои (их номера записал
    // onConfirmed). Пустая или усечённая дельта доказательством не считается.
    function refreshEventsAreOwnDowntime(ctx) {
        var events = ctx && Array.isArray(ctx.events) ? ctx.events : [];
        var own = Array.isArray(window.driverOwnDowntimeEventIds) ? window.driverOwnDowntimeEventIds : [];
        if (!events.length || !own.length || (ctx && ctx.eventsTruncated)) return false;
        return events.every(function (event) {
            if (!event || event.type !== "downtime_changed") return false;
            var payload = event.payload || {};
            var id = String(payload.event_id || payload.downtime_id || event.object_id || "");
            return id !== "" && own.indexOf(id) >= 0;
        });
    }
    // Outbox, poll и resume разделяют один запрос; новая версия остаётся
    // pending у AppRealtime, если она появилась во время текущего fragment.
    if (window.driverOperationalRefreshPromise) return window.driverOperationalRefreshPromise;
    var currentShell = document.querySelector("[data-driver-shell]");
    if (!currentShell || !window.AppOperationalFragment || isDriverOperationalRefreshUnsafe(currentShell)) {
        return Promise.resolve({deferred: true, reason: "driver_busy"});
    }
    var activeTab = currentShell.dataset.activeTab || "work";
    var targetVersion = Number(context && context.version || 0);
    var requestShell = currentShell;
    var refreshPromise = window.AppOperationalFragment.request("driver", targetVersion).then(function (payload) {
        var oldShell = document.querySelector("[data-driver-shell]");
        if (!oldShell || oldShell !== requestShell || isDriverOperationalRefreshUnsafe(oldShell)) {
            return {deferred: true, reason: "driver_busy"};
        }
        var freshShell = window.AppOperationalFragment.parseRoot(
            payload.html,
            "[data-driver-shell]"
        );
        if (!freshShell) return {deferred: true, reason: "driver_fragment_invalid"};
        /* Метка версии оболочки во фрагменте: старый экран обязан перезагрузиться, прежде чем
           принять разметку, которой нужны свежие стили и скрипты. Раньше это делала картинка,
           которую вставляли в страницу; проверяем прямо здесь и убираем её — иначе структура
           свежего экрана заведомо не совпадёт с живым и послойное обновление невозможно. */
        var freshShellMark = freshShell.querySelector("[data-driver-fragment-shell]");
        if (freshShellMark) {
            var loadedShellVersion = String(
                (window.__driverPwaUpdateRuntime && window.__driverPwaUpdateRuntime.currentShellVersion) || ""
            );
            var freshShellVersion = String(freshShellMark.dataset.driverFragmentShell || "");
            freshShellMark.parentNode.removeChild(freshShellMark);
            if (loadedShellVersion && freshShellVersion && loadedShellVersion !== freshShellVersion) {
                window.location.reload();
                return {deferred: true, reason: "driver_shell_outdated"};
            }
        }
        /* Сервер прислал тот же экран (или он отличается только нашим же простоем, который
           местная проекция уже показала): DOM не трогаем, версию подтверждаем. Полная подмена
           на телефоне — это 1–3 с замирания, и по таймеру она шла каждые 20 с. */
        var freshSnapshot = typeof window.driverFragmentSnapshot === "function"
            ? window.driverFragmentSnapshot(freshShell)
            : null;
        var baseSnapshot = window.driverAppliedFragmentSnapshot || null;
        if (freshSnapshot && baseSnapshot && window.driverForceFragmentApply !== true) {
            var unchanged = freshSnapshot.full === baseSnapshot.full;
            var ownDowntimeOnly = !unchanged
                && freshSnapshot.core === baseSnapshot.core
                && refreshEventsAreOwnDowntime(context);
            if (unchanged || ownDowntimeOnly) {
                window.driverAppliedFragmentSnapshot = freshSnapshot;
                if (ownDowntimeOnly) window.driverDomBehindBaseline = true;
                if (window.DriverFreeBucket && typeof window.DriverFreeBucket.receiveFragment === "function") {
                    window.DriverFreeBucket.receiveFragment(
                        payload.driver_free_bucket_catalog,
                        payload.driver_free_bucket_state
                    );
                }
                var acceptedVersion = Number(payload.version || 0);
                document.body.dataset.operationalStateVersion = String(acceptedVersion);
                return {applied: true, version: acceptedVersion, skipped: unchanged ? "unchanged" : "own_downtime"};
            }
        }
        var previousDowntimeCard = oldShell.querySelector("[data-driver-active-downtime-flow]");
        var previousDowntimeFlow = previousDowntimeCard
            ? String(previousDowntimeCard.dataset.driverActiveDowntimeFlow || "")
            : "";
        var previousAssignmentForm = oldShell.querySelector("#driver-assignment-action");
        var nextAssignmentForm = freshShell.querySelector("#driver-assignment-action");
        var previousAssignmentKey = previousAssignmentForm
            ? String(previousAssignmentForm.getAttribute("action") || "")
            : "";
        var nextAssignmentKey = nextAssignmentForm
            ? String(nextAssignmentForm.getAttribute("action") || "")
            : "";
        var movedFromLoadingWaitToWork = (
            previousDowntimeFlow === "waiting_loading"
            && freshShell.dataset.driverHasLoadedTrip === "true"
        );
        var becameLoaded = (
            oldShell.dataset.driverHasLoadedTrip !== "true"
            && freshShell.dataset.driverHasLoadedTrip === "true"
        );
        if (movedFromLoadingWaitToWork) {
            activeTab = "work";
        }
        activeTab = movedFromLoadingWaitToWork ? "work" : (oldShell.dataset.activeTab || activeTab);
        syncDriverTabMarkup(freshShell, activeTab);
        if (window.DriverFreeBucket && typeof window.DriverFreeBucket.receiveFragment === "function") {
            window.DriverFreeBucket.receiveFragment(
                payload.driver_free_bucket_catalog,
                payload.driver_free_bucket_state
            );
        }
        if (movedFromLoadingWaitToWork && window.history && window.history.replaceState) {
            var refreshedUrl = new URL(window.location.href);
            refreshedUrl.searchParams.set("tab", activeTab);
            window.history.replaceState({}, "", refreshedUrl.toString());
        }
        var viewState = window.AppOperationalFragment.captureView
            ? window.AppOperationalFragment.captureView(oldShell, "[data-driver-tab-panel]", "data-driver-tab-panel")
            : null;
        /* Сначала пробуем обновить экран послойно: живые узлы остаются, значит и
           обработчики остаются, и барабан не пересобирается. Результат проверяем тем же
           снимком; не сошлось — обычная полная подмена. */
        var morphed = false;
        if (typeof window.driverMorphShell === "function" && freshSnapshot) {
            morphed = window.driverMorphShell(oldShell, freshShell)
                && window.driverFragmentSnapshot(oldShell).full === freshSnapshot.full;
        }
        var appliedShell = morphed ? oldShell : freshShell;
        if (!morphed) oldShell.replaceWith(freshShell);
        if (typeof window.bindDriverMobileShell === "function") {
            window.bindDriverMobileShell();
        }
        if (morphed && typeof scheduleDriverDialLabelFit === "function") {
            /* Текст в круге мог смениться, а подогнанный кегль остался от прежнего. */
            scheduleDriverDialLabelFit(true);
        }
        if (typeof window.checkAchievementPrize === "function") {
            window.checkAchievementPrize(context);
        }
        if (becameLoaded) {
            playDriverDumpPointAlert({events: [{
                version: targetVersion,
                type: "trip_changed",
                payload: {
                    action: "truck_loaded",
                    trip_id: Number(freshShell.dataset.driverActiveTripId || 0),
                    assigned_dump_point_id: Number(freshShell.dataset.driverAssignedDumpPointId || 0),
                    dump_point_name: String(freshShell.dataset.driverAssignedDumpPointName || "")
                }
            }]});
        }
        /* Резервный путь: сюда доходит только то, чего не было в дельте —
           усечённый ответ, повреждённый payload, отсутствующий идентификатор.
           Версия остаётся положительной: это тот же монотонный счётчик
           состояния, поэтому будущие события с большими версиями он не
           заглушит, а нулевую версию нативная сторона отвергла бы.

           Предложение снять назначение отсюда только пикает: голос ждёт
           фактического «release_applied». Иначе маркер записался бы на
           меньшей версии, и снятие прозвучало бы дважды. */
        if (nextAssignmentKey && nextAssignmentKey !== previousAssignmentKey) {
            var fallbackAssignmentId = String(nextAssignmentForm.dataset.driverAssignmentId || "").trim();
            if (String(nextAssignmentForm.dataset.driverAssignmentKind || "") === "assign") {
                playDriverAssignmentAlert(
                    targetVersion,
                    "assign",
                    String(nextAssignmentForm.dataset.driverExcavatorNumber || ""),
                    {opKey: fallbackAssignmentId ? "assign:" + fallbackAssignmentId : ""}
                );
            } else {
                playDriverReleaseOfferCue(fallbackAssignmentId);
            }
        }
        if (window.AppOperationalFragment.restoreView) {
            window.AppOperationalFragment.restoreView(appliedShell, viewState);
        }
        var appliedVersion = Number(payload.version || 0);
        document.body.dataset.operationalStateVersion = String(appliedVersion);
        window.driverAppliedFragmentSnapshot = freshSnapshot;
        window.driverDomBehindBaseline = false;
        window.driverForceFragmentApply = false;
        return {applied: true, version: appliedVersion};
    }).catch(function () {
        return {deferred: true, reason: "driver_refresh_failed"};
    });
    var ownedPromise = refreshPromise.finally(function () {
        if (window.driverOperationalRefreshPromise === ownedPromise) {
            window.driverOperationalRefreshPromise = null;
        }
    });
    window.driverOperationalRefreshPromise = ownedPromise;
    return ownedPromise;
};

/* Разгрузка уходила обычной отправкой формы: браузер перезагружал страницу
   целиком, экран моргал и подвисал. Отправляем по тому же адресу, но ответ
   разбираем сами и меняем только рабочую часть — остальное остаётся на месте.

   Повтор действия безопасен: у формы есть свой номер действия, сервер по нему
   узнаёт уже выполненный рейс. При ошибке оставляем рабочий экран на месте:
   обычная навигация на ответ сервера превращала временный HTTP 500 в чёрный
   экран WebView без возможности восстановиться. */
window.submitDriverFormInPlace = function (form, options) {
    options = options || {};
    var actionKind = String(form.dataset.driverInPlace || "");
    if (!actionKind && form.matches("[data-driver-hold-form]")) actionKind = "complete-trip";
    if (!window.fetch || !window.AppOperationalFragment || !window.FormData) {
        if (!options.silentFailure && typeof window.showDriverToast === "function") {
            window.showDriverToast("Действие не отправлено. Проверьте связь и повторите.");
        }
        return Promise.resolve(false);
    }
    var body = new window.FormData(form);
    return window.fetch(form.getAttribute("action") || window.location.href, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "follow",
        body: body
    }).then(function (response) {
        if (!response.ok) throw new Error("driver action rejected");
        return response.text().then(function (html) {
            return {html: html, url: response.url};
        });
    }).then(function (result) {
        var freshShell = window.AppOperationalFragment.parseRoot(result.html, "[data-driver-shell]");
        var oldShell = document.querySelector("[data-driver-shell]");
        if (!freshShell || !oldShell) throw new Error("driver shell missing");
        var actionVoice = driverAppliedActionVoice(actionKind, freshShell);
        oldShell.replaceWith(freshShell);
        if (result.url && window.history && window.history.replaceState) {
            var responseUrl = new URL(result.url, window.location.href);
            if (responseUrl.origin === window.location.origin) {
                window.history.replaceState(
                    window.history.state,
                    "",
                    responseUrl.pathname + responseUrl.search + responseUrl.hash
                );
            }
        }
        if (typeof window.bindDriverMobileShell === "function") {
            window.bindDriverMobileShell();
        }
        if (actionVoice.voice) {
            playDriverVoice(actionVoice.cue, actionVoice.voice);
        } else {
            playDriverSound(actionVoice.cue);
        }
        return true;
    }).catch(function () {
        if (!options.silentFailure) {
            playDriverVoice(
                "action_error",
                actionKind === "complete-trip" ? "voice_trip_finish_failed" : "voice_action_failed"
            );
        }
        if (!options.silentFailure && typeof window.showDriverToast === "function") {
            window.showDriverToast("Не удалось обновить экран. Проверяем состояние сервера.");
        }
        if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
            window.AppRealtime.wake("driver_in_place_failed");
        }
        return false;
    });
};

/* DRIVER_SHIFT_CLOSE_OUTBOX_START */
window.DriverShiftCloseOutbox = (function () {
    var storageKey = "driver-shift-close-pending:v1";
    var retryInFlight = false;
    var retryTimer = null;
    var maxRetryDelayMs = 60000;

    function nativeConnection() {
        return window.NativeBackgroundConnection || null;
    }

    function currentAuthGeneration() {
        var shell = document.querySelector("[data-driver-shell]");
        return shell ? String(shell.dataset.driverAuthGeneration || "") : "";
    }

    function retryDelayMs(attempt) {
        return Math.min(maxRetryDelayMs, 2000 * Math.pow(2, Math.min(Math.max(1, attempt) - 1, 5)));
    }

    function readLocal() {
        try {
            var value = JSON.parse(window.localStorage.getItem(storageKey) || "null");
            return value && value.clientActionId ? value : null;
        } catch (error) {
            return null;
        }
    }

    function writeLocal(payload) {
        try {
            window.localStorage.setItem(storageKey, JSON.stringify(payload));
            return true;
        } catch (error) {
            return false;
        }
    }

    function removeLocal(expectedActionId) {
        var pending = readLocal();
        if (pending && expectedActionId && pending.clientActionId !== expectedActionId) {
            return false;
        }
        try {
            window.localStorage.removeItem(storageKey);
            return true;
        } catch (error) {
            return false;
        }
    }

    function payloadFromForm(form) {
        function field(name) {
            var input = form.querySelector('[name="' + name + '"]');
            return input ? String(input.value || "").trim() : "";
        }
        return {
            shiftId: String(form.dataset.nativeShiftId || "").trim(),
            clientActionId: field("client_action_id"),
            endFuel: field("end_fuel"),
            endMileage: field("end_mileage"),
            endEngineHours: field("end_engine_hours"),
            confirmationToken: field("reading_confirmation_token"),
            authGeneration: currentAuthGeneration(),
            state: "queued",
            createdAt: Date.now(),
            retryAttempts: 0,
            nextAttemptAt: 0,
            requiresAuthentication: false
        };
    }

    function fillForm(form, payload) {
        if (!form || !payload) return;
        [
            ["client_action_id", "clientActionId"],
            ["end_fuel", "endFuel"],
            ["end_mileage", "endMileage"],
            ["end_engine_hours", "endEngineHours"],
            ["reading_confirmation_token", "confirmationToken"]
        ].forEach(function (mapping) {
            var input = form.querySelector('[name="' + mapping[0] + '"]');
            if (input && payload[mapping[1]] !== undefined) {
                input.value = String(payload[mapping[1]] || "");
            }
        });
    }

    function persistPending(payload) {
        var localStored = writeLocal(payload);
        var native = nativeConnection();
        if (native && typeof native.queueDriverShiftClose === "function") {
            return Promise.resolve(native.queueDriverShiftClose(payload)).then(function () {
                return true;
            }).catch(function (error) {
                removeLocal(payload.clientActionId);
                if (!localStored) throw error;
                throw new Error("Не удалось включить фоновую отправку. Оставьте приложение открытым и повторите.");
            });
        }
        if (localStored) {
            return Promise.resolve(true);
        }
        return Promise.reject(new Error("Не удалось сохранить закрытие смены на телефоне."));
    }

    function persistQueued(payload) {
        payload.state = "queued";
        payload.requiresAttention = false;
        payload.requiresAuthentication = false;
        return persistPending(payload);
    }

    function persistRetry(payload) {
        payload.state = "retry";
        payload.requiresAttention = false;
        payload.requiresAuthentication = false;
        payload.retryAttempts = Math.max(0, Number(payload.retryAttempts) || 0) + 1;
        payload.nextAttemptAt = Date.now() + retryDelayMs(payload.retryAttempts);
        return persistPending(payload);
    }

    function persistAuthRequired(payload) {
        payload.state = "auth_required";
        payload.requiresAttention = false;
        payload.requiresAuthentication = true;
        payload.blockedAuthGeneration = String(payload.authGeneration || currentAuthGeneration() || "");
        payload.nextAttemptAt = 0;
        return persistPending(payload);
    }

    function hasFreshAuthentication(payload) {
        var current = currentAuthGeneration();
        return !!current && current !== String(payload.blockedAuthGeneration || payload.authGeneration || "");
    }

    function resumeAuthentication(payload) {
        payload.authGeneration = currentAuthGeneration();
        payload.blockedAuthGeneration = "";
        payload.retryAttempts = 0;
        payload.nextAttemptAt = 0;
        return persistQueued(payload);
    }

    function clearNative(clientActionId) {
        var native = nativeConnection();
        if (!native || typeof native.acknowledgeDriverShiftClose !== "function") {
            return Promise.resolve();
        }
        return Promise.resolve(native.acknowledgeDriverShiftClose(clientActionId)).catch(function () {});
    }

    function acknowledge(clientActionId) {
        removeLocal(clientActionId);
        return clearNative(clientActionId);
    }

    function showPending(form) {
        if (!form) return;
        form.classList.add("is-sync-pending");
        var panel = form.querySelector("[data-driver-shift-sync-pending]");
        if (panel) panel.hidden = false;
        if (document.body) document.body.dataset.driverShiftClosePending = "true";
        form.querySelectorAll("input, button, select, textarea").forEach(function (control) {
            control.disabled = true;
        });
    }

    function clearPendingUi(form) {
        if (!form) return;
        form.classList.remove("is-sync-pending");
        var panel = form.querySelector("[data-driver-shift-sync-pending]");
        if (panel) panel.hidden = true;
        if (document.body) delete document.body.dataset.driverShiftClosePending;
        form.querySelectorAll("input, button, select, textarea").forEach(function (control) {
            control.disabled = false;
        });
    }

    function resetSubmitUi(form) {
        if (!form) return;
        form.dataset.driverInPlacePending = "false";
        var closeButton = form.querySelector("[data-driver-shift-close-button]");
        if (closeButton) {
            closeButton.disabled = false;
            closeButton.classList.remove("is-pending");
            var closeLabel = closeButton.querySelector("[data-mobile-shift-label]");
            if (closeLabel) closeLabel.textContent = "Закрыть смену";
        }
    }

    function hideAttention(form) {
        if (!form) return;
        var modal = form.querySelector("[data-driver-reading-confirmation]");
        if (modal) modal.hidden = true;
        if (document.body) document.body.classList.remove("modal-open");
    }

    function warningItems(data) {
        if (data && Array.isArray(data.warnings) && data.warnings.length) {
            return data.warnings.map(function (warning) {
                return {
                    code: String(warning.code || "warning"),
                    field: String(warning.field || ""),
                    title: String(warning.title || "Проверьте показание"),
                    message: String(warning.message || data.error || "Проверьте введённое значение.")
                };
            });
        }
        var fieldErrors = data && data.field_errors ? data.field_errors : {};
        var items = [];
        Object.keys(fieldErrors).forEach(function (field) {
            var messages = Array.isArray(fieldErrors[field]) ? fieldErrors[field] : [fieldErrors[field]];
            messages.forEach(function (message) {
                items.push({code: "validation_error", field: field, title: "Показание не принято", message: String(message || "")});
            });
        });
        if (!items.length) {
            items.push({code: "server_error", field: "", title: "Закрытие смены не выполнено", message: String(data && data.error || "Проверьте показания.")});
        }
        return items;
    }

    function renderWarningList(list, items) {
        if (!list) return;
        while (list.firstChild) list.removeChild(list.firstChild);
        items.forEach(function (warning) {
            var item = document.createElement("li");
            item.dataset.warningCode = warning.code;
            var title = document.createElement("strong");
            var message = document.createElement("span");
            title.textContent = warning.title;
            message.textContent = warning.message;
            item.appendChild(title);
            item.appendChild(message);
            list.appendChild(item);
        });
    }

    function focusWarningField(form, items) {
        var firstField = items.length ? items[0].field : "";
        var input = firstField ? form.querySelector('[name="' + firstField + '"]') : null;
        if (input && typeof input.focus === "function") input.focus({preventScroll: true});
    }

    function showAttention(form, payload) {
        if (!form || !payload) return;
        clearPendingUi(form);
        resetSubmitUi(form);
        fillForm(form, payload);
        var modal = form.querySelector("[data-driver-reading-confirmation]");
        if (!modal) {
            if (typeof window.showDriverToast === "function") {
                window.showDriverToast(payload.attentionMessage || payload.error || "Проверьте показания.");
            }
            return;
        }
        var confirmationRequired = payload.confirmationRequired === true;
        var items = warningItems(payload);
        var title = modal.querySelector("[data-driver-reading-confirmation-title]");
        var message = modal.querySelector("[data-driver-reading-confirmation-message]");
        var list = modal.querySelector("[data-driver-reading-confirmation-warnings]");
        var back = modal.querySelector("[data-driver-reading-confirmation-back]");
        var accept = modal.querySelector("[data-driver-reading-confirmation-accept]");
        if (title) title.textContent = confirmationRequired ? "Проверьте подозрительные показания" : "Показания не приняты";
        if (message) {
            message.textContent = confirmationRequired
                ? "Смена ещё не закрыта. Сверьте значения или подтвердите, что они верны."
                : "Сервер отклонил значения. Исправьте поля и отправьте закрытие ещё раз.";
        }
        renderWarningList(list, items);
        if (accept) {
            accept.hidden = !confirmationRequired;
            accept.onclick = function () {
                var tokenInput = form.querySelector('[name="reading_confirmation_token"]');
                if (tokenInput) tokenInput.value = String(payload.confirmationToken || "");
                hideAttention(form);
                form.dataset.driverInPlacePending = "true";
                return submit(form, {payload: payload, confirmed: true}).catch(function (error) {
                    resetSubmitUi(form);
                    if (typeof window.showDriverToast === "function") {
                        window.showDriverToast(error && error.message || "Не удалось сохранить закрытие смены.", "error");
                    }
                    return false;
                });
            };
        }
        if (back) {
            back.textContent = payload.hasActiveShift === false
                ? "Обновить экран"
                : confirmationRequired
                    ? "Вернуться и проверить"
                    : "Отменить отправку и вернуться к вводу";
            back.onclick = function () {
                hideAttention(form);
                if (payload.hasActiveShift === false) {
                    acknowledge(payload.clientActionId).then(function () {
                        if (window.location && typeof window.location.reload === "function") window.location.reload();
                    });
                    return;
                }
                acknowledge(payload.clientActionId);
                var tokenInput = form.querySelector('[name="reading_confirmation_token"]');
                if (tokenInput) tokenInput.value = "";
                resetSubmitUi(form);
                focusWarningField(form, items);
            };
        }
        modal.hidden = false;
        if (document.body) document.body.classList.add("modal-open");
        if (back && typeof back.focus === "function") back.focus();
    }

    function serverAttentionPayload(payload, data) {
        return Object.assign({}, payload, {
            state: "attention",
            requiresAttention: true,
            confirmationRequired: data.confirmation_required === true,
            confirmationToken: String(data.confirmation_token || payload.confirmationToken || ""),
            warnings: Array.isArray(data.warnings) ? data.warnings : [],
            field_errors: data.field_errors || {},
            error: String(data.error || "Проверьте показания на конец смены."),
            attentionMessage: String(data.error || "Проверьте показания на конец смены."),
            hasActiveShift: data.has_active_shift !== false
        });
    }

    function requestServer(form, payload) {
        fillForm(form, payload);
        var body = new window.FormData(form);
        var occurredAt = new Date(Number(payload.createdAt) || Date.now()).toISOString();
        body.set("occurred_at", occurredAt);
        return window.fetch(form.getAttribute("action") || window.location.href, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            redirect: "follow",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest"
            },
            body: body
        }).then(function (response) {
            return response.text().then(function (text) {
                var data = {};
                try { data = JSON.parse(text || "{}"); } catch (error) {
                    data = {ok: false, error: "Сервер вернул непонятный ответ."};
                }
                return {response: response, data: data, rawText: text};
            });
        });
    }

    function isRetryableStatus(status) {
        return status === 408 || status === 429 || status >= 500;
    }

    function isAuthenticationResult(result) {
        var response = result.response;
        if (response.status === 401 || response.status === 403) return true;
        if (!response.redirected || !response.url) return false;
        try {
            var path = new URL(response.url, window.location.origin).pathname;
            return path === "/" || path === "/login" || path === "/login/";
        } catch (error) {
            return false;
        }
    }

    function scheduleRetry(form, payload) {
        if (nativeConnection() || navigator.onLine === false) return;
        if (retryTimer !== null) window.clearTimeout(retryTimer);
        var delay = Math.max(0, Number(payload.nextAttemptAt || 0) - Date.now());
        retryTimer = window.setTimeout(function () {
            retryTimer = null;
            sendStored(form, payload, {quiet: true});
        }, delay);
    }

    function completeApplied(payload, data) {
        return acknowledge(payload.clientActionId).then(function () {
            if (typeof playDriverVoice === "function") playDriverVoice("shift_end", "voice_shift_closed");
            var target = String(data.redirect_url || "/driver/?tab=manifest");
            if (window.location) {
                if (typeof window.location.assign === "function") window.location.assign(target);
                else window.location.href = target;
            }
            return true;
        });
    }

    function handleServerResult(form, payload, result) {
        var response = result.response;
        var data = result.data || {};
        if (response.ok && data.ok) return completeApplied(payload, data);
        if (isAuthenticationResult(result)) {
            return persistAuthRequired(payload).then(function () {
                showPending(form);
                return true;
            });
        }
        if (isRetryableStatus(response.status)) {
            return persistRetry(payload).then(function () {
                showPending(form);
                scheduleRetry(form, payload);
                return true;
            });
        }
        var attention = serverAttentionPayload(payload, data);
        return clearNative(payload.clientActionId).then(function () {
            writeLocal(attention);
            showAttention(form, attention);
            return true;
        });
    }

    function sendStored(form, payload, options) {
        options = options || {};
        if (!form || retryInFlight) return Promise.resolve(false);
        if (Number(payload.nextAttemptAt || 0) > Date.now()) {
            showPending(form);
            scheduleRetry(form, payload);
            return Promise.resolve(true);
        }
        retryInFlight = true;
        clearPendingUi(form);
        fillForm(form, payload);
        return requestServer(form, payload).then(function (result) {
                return handleServerResult(form, payload, result);
            }, function () {
                return persistRetry(payload).then(function () {
                    showPending(form);
                    scheduleRetry(form, payload);
                    return true;
                });
            }).finally(function () {
                retryInFlight = false;
            });
    }

    function submit(form, options) {
        options = options || {};
        var payload = options.payload ? Object.assign({}, options.payload) : payloadFromForm(form);
        if (options.confirmed) {
            var tokenInput = form.querySelector('[name="reading_confirmation_token"]');
            payload.confirmationToken = tokenInput ? String(tokenInput.value || "").trim() : String(payload.confirmationToken || "");
        }
        payload.state = "queued";
        payload.requiresAttention = false;
        return sendStored(form, payload);
    }

    function bindInvalidation(form) {
        if (!form || form.dataset.driverShiftConfirmationBound === "true") return;
        form.dataset.driverShiftConfirmationBound = "true";
        form.querySelectorAll("[name='end_fuel'], [name='end_mileage'], [name='end_engine_hours']").forEach(function (input) {
            input.addEventListener("input", function () {
                var tokenInput = form.querySelector('[name="reading_confirmation_token"]');
                if (tokenInput) tokenInput.value = "";
                var pending = readLocal();
                if (pending && pending.state === "attention") acknowledge(pending.clientActionId);
                hideAttention(form);
            });
        });
    }

    function restore(form) {
        var localPending = readLocal();
        var native = nativeConnection();
        bindInvalidation(form);
        if (!form) {
            if (localPending) acknowledge(localPending.clientActionId);
            return Promise.resolve(false);
        }
        if (!native || typeof native.getState !== "function") {
            if (!localPending || String(form.dataset.nativeShiftId || "") !== String(localPending.shiftId || "")) {
                return Promise.resolve(false);
            }
            if (localPending.state === "attention" || localPending.requiresAttention) {
                showAttention(form, localPending);
                return Promise.resolve(true);
            }
            if (localPending.state === "auth_required" || localPending.requiresAuthentication) {
                if (!hasFreshAuthentication(localPending)) {
                    showPending(form);
                    return Promise.resolve(true);
                }
                return resumeAuthentication(localPending).then(function () {
                    return sendStored(form, localPending, {quiet: true});
                });
            }
            if (navigator.onLine !== false) return sendStored(form, localPending, {quiet: true});
            showPending(form);
            return Promise.resolve(true);
        }
        return Promise.resolve(native.getState()).then(function (state) {
            var nativePending = state && state.pendingDriverShiftClose;
            if (!nativePending) {
                if (localPending && (localPending.state === "attention" || localPending.requiresAttention)) {
                    showAttention(form, localPending);
                    return true;
                }
                if (localPending) removeLocal(localPending.clientActionId);
                return false;
            }
            var pending = nativePending || localPending;
            if (!pending || String(form.dataset.nativeShiftId || "") !== String(pending.shiftId || "")) {
                return false;
            }
            if (pending.requiresAttention) {
                pending.state = "attention";
                pending.confirmationRequired = pending.confirmationRequired === true;
                writeLocal(pending);
            }
            if (pending.state === "auth_required" || pending.requiresAuthentication) {
                if (!hasFreshAuthentication(pending)) {
                    writeLocal(pending);
                    showPending(form);
                    return true;
                }
                return resumeAuthentication(pending).then(function () {
                    showPending(form);
                    return true;
                });
            }
            fillForm(form, pending);
            if (pending.state === "attention" || pending.requiresAttention) {
                showAttention(form, pending);
                return true;
            }
            showPending(form);
            return true;
        }).catch(function () {
            if (localPending && (localPending.state === "attention" || localPending.requiresAttention)) {
                showAttention(form, localPending);
            } else if (localPending) {
                showPending(form);
            }
            return !!localPending;
        });
    }

    return {
        submit: submit,
        restore: restore,
        readLocal: readLocal,
        showPending: showPending,
        acknowledge: acknowledge,
        showAttention: showAttention,
        bindInvalidation: bindInvalidation
    };
})();
window.addEventListener("online", function () {
    if (window.DriverShiftCloseOutbox) {
        window.DriverShiftCloseOutbox.restore(
            document.querySelector("[data-driver-shift-close-form]")
        );
    }
});
/* DRIVER_SHIFT_CLOSE_OUTBOX_END */

function bindDriverShiftHoldAction(form, button, options) {
    options = options || {};
    if (!form || !button || !window.MobileShiftHold) {
        return null;
    }
    return window.MobileShiftHold.bind(button, {
        holdMs: options.holdMs || 2000,
        readyLabel: options.readyLabel || button.textContent.trim(),
        onShortPress: function () {
            if (typeof window.showDriverToast === "function") window.showDriverToast("Удерживайте кнопку");
        },
        onComplete: function () {
            form.dataset.driverShiftHoldComplete = "true";
            form.requestSubmit(button);
        }
    });
}

function bindDriverShiftOpeningForm(shell) {
    var openShiftForm = shell.querySelector(".driver-shift-opening-form");
    var openShiftButton = shell.querySelector("[data-driver-shift-open-button]");
    if (!openShiftForm || !openShiftButton || openShiftForm.dataset.driverShiftOpeningBound === "true") {
        return;
    }
    openShiftForm.dataset.driverShiftOpeningBound = "true";
    openShiftForm.dataset.driverShiftOpeningPending = "false";
    function driverOpeningRoleIsReadonly() {
        return (
            typeof window.isAppRoleReadonly === "function"
            && window.isAppRoleReadonly()
        );
    }
    var openReadings = Array.from(openShiftForm.querySelectorAll("input[type='number']"));
    var initialReadings = openReadings.map(function (input) {
        return input.value;
    });

    function requestDeferredRefresh(reason) {
        if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
            window.AppRealtime.wake(reason);
        }
    }

    function syncOpenShiftFormState(options) {
        options = options || {};
        var wasDirty = openShiftForm.dataset.driverShiftOpeningDirty === "true";
        var isDirty = openReadings.some(function (input, index) {
            return input.value !== initialReadings[index];
        });
        openShiftForm.dataset.driverShiftOpeningDirty = isDirty ? "true" : "false";
        openShiftButton.disabled = !openReadings.length || openReadings.some(function (input) {
            return input.value.trim() === "" || !input.checkValidity();
        });
        if (
            !isDirty &&
            openShiftForm.dataset.driverShiftOpeningPending !== "true" &&
            (wasDirty || options.explicitReset)
        ) {
            requestDeferredRefresh(
                options.explicitReset ? "driver_shift_open_reset" : "driver_shift_open_restored"
            );
        }
    }

    openReadings.forEach(function (input) {
        input.addEventListener("input", syncOpenShiftFormState);
    });
    openShiftForm.addEventListener("reset", function () {
        openShiftForm.dataset.driverShiftOpeningPending = "false";
        openShiftButton.classList.remove("is-pending");
        window.setTimeout(function () {
            syncOpenShiftFormState({explicitReset: true});
        }, 0);
    });
    openShiftForm.addEventListener("submit", function (event) {
        if (
            openShiftButton.disabled
            || driverOpeningRoleIsReadonly()
            || openShiftForm.dataset.driverShiftHoldComplete !== "true"
        ) {
            event.preventDefault();
            if (
                !openShiftButton.disabled
                && openShiftForm.dataset.driverShiftHoldComplete !== "true"
                && typeof window.showDriverToast === "function"
            ) {
                window.showDriverToast("Удерживайте кнопку, чтобы начать смену");
            }
            return;
        }
        /* Одна открытая смена на сотрудника: если в другой роли смена ещё
           открыта, после удержания спрашиваем «Завершить её и начать?» — как
           у экскаваторщика, диспетчера и мастера. Ответ «да» добавляет в
           форму подтверждение, и сервер закрывает ту смену служебно. */
        var otherRoleQuestion = openShiftButton.dataset.otherRoleShiftQuestion || "";
        if (
            otherRoleQuestion
            && openShiftForm.dataset.otherRoleShiftConfirmed !== "true"
            && typeof window.openAppConfirmDialog === "function"
        ) {
            event.preventDefault();
            delete openShiftForm.dataset.driverShiftHoldComplete;
            window.openAppConfirmDialog(otherRoleQuestion, function () {
                var confirmField = openShiftForm.querySelector("[data-other-role-shift-input]");
                if (!confirmField) {
                    confirmField = document.createElement("input");
                    confirmField.type = "hidden";
                    confirmField.name = openShiftButton.dataset.otherRoleShiftField || "close_other_role_shift";
                    confirmField.setAttribute("data-other-role-shift-input", "");
                    openShiftForm.appendChild(confirmField);
                }
                confirmField.value = "1";
                openShiftForm.dataset.otherRoleShiftConfirmed = "true";
                openShiftForm.dataset.driverShiftHoldComplete = "true";
                openShiftForm.requestSubmit(openShiftButton);
            }, 0, openShiftButton.dataset.otherRoleShiftAccept || "Завершить и начать");
            return;
        }
        delete openShiftForm.dataset.otherRoleShiftConfirmed;
        delete openShiftForm.dataset.driverShiftHoldComplete;
        openShiftForm.dataset.driverShiftOpeningPending = "true";
        openShiftButton.disabled = true;
        openShiftButton.classList.add("is-pending");
        var openShiftLabel = openShiftButton.querySelector("[data-mobile-shift-label]");
        if (openShiftLabel) openShiftLabel.textContent = "Открываем смену";
    });
    bindDriverShiftHoldAction(openShiftForm, openShiftButton, {
        holdMs: 1000,
        readyLabel: "Начать смену",
        progressProperty: "--driver-shift-open-hold"
    });
    syncOpenShiftFormState();
}

window.bindDriverMobileShell = function () {
    var shell = document.querySelector("[data-driver-shell]");
    if (!shell) {
        return;
    }
    if (shell.dataset.driverShellBound === "true") {
        return;
    }
    shell.dataset.driverShellBound = "true";
    if (typeof window.bindMobileShiftScreens === "function") {
        window.bindMobileShiftScreens();
    }
    /* Banners rendered by base.html sit in normal flow above the shell and steal
       height from it; without subtracting them the bottom actions fall off screen. */
    function driverLeadingBannerHeight() {
        var total = 0;
        [".app-observer-banner", ".app-inactive-role-banner"].forEach(function (selector) {
            document.querySelectorAll(selector).forEach(function (banner) {
                if (window.getComputedStyle(banner).position === "fixed") return;
                total += banner.getBoundingClientRect().height || 0;
            });
        });
        return Math.round(total);
    }
    var driverStableViewportHeight = 0;
    var driverStableViewportWidth = 0;
    function driverIsEditingField() {
        var active = document.activeElement;
        return !!(active && active.matches && active.matches("input, textarea, select"));
    }
    function driverCurrentViewportMetrics() {
        var viewport = window.visualViewport;
        var height = viewport && Number(viewport.height) > 0 ? Number(viewport.height) : Number(window.innerHeight);
        height = Math.round(height || document.documentElement.clientHeight || 0);
        var width = viewport && Number(viewport.width) > 0 ? Number(viewport.width) : Number(window.innerWidth);
        width = Math.round(width || document.documentElement.clientWidth || 0);
        return {height: height, width: width};
    }
    function driverViewportIsTemporarilyReduced() {
        if (!(driverStableViewportHeight > 0) || !(driverStableViewportWidth > 0)) return false;
        var metrics = driverCurrentViewportMetrics();
        var sameWidth = Math.abs(metrics.width - driverStableViewportWidth) <= 24;
        var keyboardGap = Math.max(96, Math.round(driverStableViewportHeight * 0.18));
        return sameWidth && metrics.height < driverStableViewportHeight - keyboardGap;
    }
    function driverVisualViewportHeight() {
        var metrics = driverCurrentViewportMetrics();
        var height = metrics.height;
        /* Пока открыта клавиатура, видимая часть экрана сжимается почти вдвое.
           Подгонять под неё всю разметку нельзя — строки налезают друг на
           друга. Держим высоту, измеренную без клавиатуры: до нужного поля
           экран и так доводит прокрутка. */
        if (driverIsEditingField() || driverViewportIsTemporarilyReduced()) {
            if (driverStableViewportHeight > 0) height = driverStableViewportHeight;
        } else {
            driverStableViewportHeight = height;
            driverStableViewportWidth = metrics.width;
        }
        return Math.max(320, height - driverLeadingBannerHeight());
    }
    function driverPanelOverflows(panel) {
        if (!panel) return false;
        if (panel.scrollHeight > panel.clientHeight + 1 || panel.scrollWidth > panel.clientWidth + 1) return true;
        var nav = document.querySelector("[data-driver-bottom-nav]");
        var navTop = nav ? nav.getBoundingClientRect().top : driverVisualViewportHeight();
        var probes = panel.querySelectorAll(
            ".driver-shift-scroll, .driver-shift-section, .driver-downtime-list, " +
            ".driver-report-scroll, .driver-report-grid, .driver-report-section, .driver-report-actions, " +
            ".driver-timeline, button, input, select"
        );
        return Array.prototype.some.call(probes, function (node) {
            /* Грани барабана простоев повёрнуты в 3D: по замерам они выходят за экран, хотя
               сцена их обрезает. Считать это переполнением нельзя — из-за него весь экран
               водителя жил в плотности «tight», а при каждом простое дёргался на 2 px. */
            if (node.closest && node.closest("[data-driver-downtime-drum]")) return false;
            var rect = node.getBoundingClientRect();
            return (
                node.scrollHeight > node.clientHeight + 1
                || node.scrollWidth > node.clientWidth + 1
                || rect.bottom > navTop + 1
                || rect.right > window.innerWidth + 1
                || rect.left < -1
            );
        });
    }
    /* Boxes here have capped sizes, so long Russian labels — or a phone with
       enlarged system text — get cut off mid-word. Shrink the text to fit
       instead of silently clipping it. */
    /* У однострочных подписей высота строки шрифта всегда чуть больше заданной
       line-height, и проверка по высоте срабатывала вхолостую, ужимая текст до
       минимума. Для них меряем только ширину. */
    function driverTextOverflows(node, widthOnly) {
        if (node.scrollWidth > node.clientWidth + 1) return true;
        return !widthOnly && node.scrollHeight > node.clientHeight + 1;
    }
    function shrinkDriverTextToFit(node, minSize, widthOnly) {
        if (!node) return;
        node.style.removeProperty("font-size");
        if (!driverTextOverflows(node, widthOnly)) return;
        var size = parseFloat(window.getComputedStyle(node).fontSize);
        if (!(size > 0)) return;
        var guard = 40;
        while (size > minSize && driverTextOverflows(node, widthOnly) && guard > 0) {
            /* Без ограничения снизу цикл проскакивал минимум на один шаг. */
            size = Math.max(minSize, size - 1);
            guard -= 1;
            node.style.fontSize = size + "px";
        }
    }
    var DRIVER_FIT_TARGETS = [
        {selector: ".driver-header-truck", min: 13, widthOnly: true},
        {selector: ".driver-header-person", min: 11, widthOnly: true},
        {selector: ".driver-shift-open, .driver-shift-logout, .driver-shift-submit, .driver-primary-action", min: 12},
        {selector: ".driver-shift-result-cell strong", min: 13, widthOnly: true},
        {selector: ".driver-work-context-machine strong", min: 11, widthOnly: true},
        {selector: ".driver-work-context-machine small", min: 9, widthOnly: true},
        {selector: ".driver-work-context-location", min: 11, widthOnly: true},
        {selector: ".driver-work-context-rock", min: 11, widthOnly: true}
    ];
    function fitDriverText(root) {
        var fitRoot = root && typeof root.querySelectorAll === "function" ? root : shell;
        DRIVER_FIT_TARGETS.forEach(function (target) {
            fitRoot.querySelectorAll(target.selector).forEach(function (node) {
                shrinkDriverTextToFit(node, target.min, target.widthOnly);
            });
        });
    }
    /* Клавиатура закрывает нижнюю половину экрана, поэтому поле, в которое
       водитель ткнул, надо подвести в видимую часть — иначе он печатает
       вслепую или вообще не видит, куда попал. */
    function bindDriverFieldScrollIntoView() {
        if (shell.dataset.driverFieldFocusBound === "true") return;
        shell.dataset.driverFieldFocusBound = "true";
        shell.addEventListener("focusin", function (event) {
            var field = event.target;
            if (!field || !field.matches || !field.matches("input, select, textarea")) return;
            if (field.closest(".mobile-shift")) return;
            var reveal = function () {
                if (!field.scrollIntoView) return;
                try {
                    field.scrollIntoView({block: "center", behavior: "smooth"});
                } catch (error) {
                    field.scrollIntoView();
                }
            };
            /* Ждём, пока клавиатура выедет и пересчитается высота. */
            window.setTimeout(reveal, 60);
            window.setTimeout(reveal, 320);
        });
    }
    function cancelDriverViewportFitFrames() {
        [
            "driverViewportFitFrame",
            "driverViewportFitTextFrame",
            "driverViewportFitDensityFrame"
        ].forEach(function (key) {
            if (window[key] !== null && typeof window[key] !== "undefined") {
                window.cancelAnimationFrame(window[key]);
                window[key] = null;
            }
        });
    }
    function invalidateDriverViewportFit() {
        window.driverViewportFitGeneration = Number(window.driverViewportFitGeneration || 0) + 1;
        cancelDriverViewportFitFrames();
        return window.driverViewportFitGeneration;
    }
    function driverViewportFitIsCurrent(generation) {
        return generation === window.driverViewportFitGeneration && shell.isConnected;
    }
    function fitDriverViewport(generation) {
        if (!driverViewportFitIsCurrent(generation)) return;
        var height = driverVisualViewportHeight();
        var viewport = window.visualViewport;
        var width = Math.max(240, Math.round(
            viewport && Number(viewport.width) > 0
                ? Number(viewport.width)
                : Number(window.innerWidth || document.documentElement.clientWidth || 0)
        ));
        document.documentElement.style.setProperty("--driver-viewport-h", height + "px");
        document.body.style.setProperty("--driver-viewport-h", height + "px");
        shell.style.setProperty("--driver-viewport-h", height + "px");
        shell.style.setProperty("--driver-viewport-w", width + "px");
        var baseDensity = height < 620 || width < 340
            ? "tight"
            : (height < 780 || width < 390 ? "compact" : "normal");
        shell.dataset.driverViewportDensity = baseDensity;
        shell.dataset.driverDensity = baseDensity;
        window.driverViewportFitTextFrame = window.requestAnimationFrame(function () {
            window.driverViewportFitTextFrame = null;
            if (!driverViewportFitIsCurrent(generation)) return;
            fitDriverText();
            /* С открытой клавиатурой всё «не влезает» по определению: она
               закрывает пол-экрана. Уплотнять разметку из-за этого нельзя —
               строки сойдутся друг на друга. */
            if (driverIsEditingField() || driverViewportIsTemporarilyReduced()) return;
            var panel = shell.querySelector("[data-driver-tab-panel].is-active");
            if (panel && panel.isConnected && driverPanelOverflows(panel)) {
                shell.dataset.driverDensity = shell.dataset.driverDensity === "normal" ? "compact" : "tight";
                window.driverViewportFitDensityFrame = window.requestAnimationFrame(function () {
                    window.driverViewportFitDensityFrame = null;
                    if (!driverViewportFitIsCurrent(generation)) return;
                    if (!panel.isConnected || driverViewportIsTemporarilyReduced()) return;
                    if (driverPanelOverflows(panel)) shell.dataset.driverDensity = "tight";
                });
            }
        });
    }
    function scheduleDriverViewportFit() {
        invalidateDriverTabSettle();
        var generation = invalidateDriverViewportFit();
        window.driverViewportFitFrame = window.requestAnimationFrame(function () {
            window.driverViewportFitFrame = null;
            if (!driverViewportFitIsCurrent(generation)) return;
            fitDriverViewport(generation);
        });
    }
    /* DRIVER_TAB_SETTLE_START */
    function cancelDriverTabSettleFrames() {
        [
            "driverTabSettleFirstFrame",
            "driverTabSettleSecondFrame",
            "driverTabSettleDensityFrame"
        ].forEach(function (key) {
            if (window[key] !== null && typeof window[key] !== "undefined") {
                window.cancelAnimationFrame(window[key]);
                window[key] = null;
            }
        });
    }
    function invalidateDriverTabSettle() {
        window.driverTabSettleGeneration = Number(window.driverTabSettleGeneration || 0) + 1;
        cancelDriverTabSettleFrames();
        return window.driverTabSettleGeneration;
    }
    function driverTabSettleIsCurrent(generation, expectedTab, panel) {
        return (
            generation === window.driverTabSettleGeneration
            && shell.isConnected
            && shell.dataset.activeTab === expectedTab
            && panel
            && panel.isConnected
            && panel.classList.contains("is-active")
            && panel.dataset.driverTabPanel === expectedTab
        );
    }
    function settleDriverTabDensity(generation, expectedTab, panel) {
        if (!driverTabSettleIsCurrent(generation, expectedTab, panel)) return;
        if (driverIsEditingField() || driverViewportIsTemporarilyReduced()) return;
        if (!driverPanelOverflows(panel)) return;
        var currentDensity = shell.dataset.driverDensity || "normal";
        var nextDensity = currentDensity === "normal"
            ? "compact"
            : (currentDensity === "compact" ? "tight" : currentDensity);
        if (nextDensity === currentDensity) return;
        shell.dataset.driverDensity = nextDensity;
        if (nextDensity !== "tight") {
            window.driverTabSettleDensityFrame = window.requestAnimationFrame(function () {
                window.driverTabSettleDensityFrame = null;
                settleDriverTabDensity(generation, expectedTab, panel);
            });
        }
    }
    function scheduleDriverTabSettle(expectedTab) {
        invalidateDriverViewportFit();
        var generation = invalidateDriverTabSettle();
        /* Все вызовы планировщика начинают с той же стабильной базы. Основной
           click-path выставляет её ещё раньше — до смены active-классов. */
        shell.dataset.driverDensity = shell.dataset.driverViewportDensity
            || shell.dataset.driverDensity
            || "normal";
        window.driverTabSettleFirstFrame = window.requestAnimationFrame(function () {
            window.driverTabSettleFirstFrame = null;
            if (
                generation !== window.driverTabSettleGeneration
                || !shell.isConnected
                || shell.dataset.activeTab !== expectedTab
            ) {
                return;
            }
            window.driverTabSettleSecondFrame = window.requestAnimationFrame(function () {
                window.driverTabSettleSecondFrame = null;
                var panel = shell.querySelector("[data-driver-tab-panel].is-active");
                if (!driverTabSettleIsCurrent(generation, expectedTab, panel)) return;
                fitDriverText(panel);
                settleDriverTabDensity(generation, expectedTab, panel);
            });
        });
    }
    /* DRIVER_TAB_SETTLE_END */
    bindDriverFieldScrollIntoView();
    window.driverScheduleViewportFit = scheduleDriverViewportFit;
    if (!window.driverViewportFitBound) {
        window.driverViewportFitBound = true;
        var requestCurrentDriverViewportFit = function () {
            if (typeof window.driverScheduleViewportFit === "function") window.driverScheduleViewportFit();
        };
        window.addEventListener("resize", requestCurrentDriverViewportFit, {passive: true});
        window.addEventListener("orientationchange", requestCurrentDriverViewportFit, {passive: true});
        if (window.visualViewport) {
            window.visualViewport.addEventListener("resize", requestCurrentDriverViewportFit, {passive: true});
        }
    }
    scheduleDriverViewportFit();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleDriverViewportFit);
    if (window.driverUnloadHoldGuard && typeof window.driverUnloadHoldGuard.destroy === "function") {
        window.driverUnloadHoldGuard.destroy();
        window.driverUnloadHoldGuard = null;
    }
    if (window.driverUnloadRecovery && typeof window.driverUnloadRecovery.destroy === "function") {
        window.driverUnloadRecovery.destroy();
        window.driverUnloadRecovery = null;
    }
    if (window.driverUnloadGesture && typeof window.driverUnloadGesture.destroy === "function") {
        window.driverUnloadGesture.destroy();
        window.driverUnloadGesture = null;
    }
    function driverRoleIsReadonly() {
        return (
            typeof window.isAppRoleReadonly === "function"
            && window.isAppRoleReadonly()
        ) || (document.body && document.body.dataset.driverShiftClosePending === "true");
    }
    shell.querySelectorAll(".mobile-shift__metric-value input").forEach(function (input) {
        if (!input.getAttribute("placeholder")) input.setAttribute("placeholder", "0");
    });

    /* Резервная навигация для старой разметки: Enter уводит на следующее поле,
       а с последнего убирает клавиатуру и фокусирует кнопку. Общий экран Смены
       использует mobile-shift-unified-v1.js и в этот блок не попадает. */
    shell.querySelectorAll("[data-driver-shift-inputs], .driver-shift-opening-form").forEach(function (group) {
        if (group.closest(".mobile-shift") || group.querySelector(".mobile-shift")) return;
        var fields = Array.prototype.filter.call(
            group.querySelectorAll("input"),
            function (input) {
                return input.type !== "hidden" && !input.disabled && !input.readOnly;
            }
        );
        fields.forEach(function (input, index) {
            var isLast = index === fields.length - 1;
            input.setAttribute("enterkeyhint", isLast ? "done" : "next");
            if (input.dataset.driverEnterBound === "true") return;
            input.dataset.driverEnterBound = "true";
            input.addEventListener("keydown", function (event) {
                if (event.key !== "Enter" && event.keyCode !== 13) return;
                event.preventDefault();
                var next = fields[index + 1];
                if (next) {
                    next.focus();
                    if (next.select) next.select();
                    return;
                }
                /* Одного blur мало: перевод фокуса на кнопку помогает старым
                   WebView закрыть клавиатуру и сразу оставить действие доступным. */
                input.blur();
                var action = group.querySelector("[data-driver-shift-open-button], [data-driver-shift-close-button]")
                    || (group.closest("form") || document).querySelector("[data-driver-shift-open-button], [data-driver-shift-close-button]");
                if (action && !action.disabled) {
                    action.focus();
                    return;
                }
                /* Кнопка ещё не активна — показания не сошлись. Всё равно уводим
                   фокус с поля на саму вкладку, иначе клавиатура может остаться
                   висеть и закрывать пол-экрана. */
                var host = group.closest("[data-driver-tab-panel]") || group;
                if (!host.hasAttribute("tabindex")) host.setAttribute("tabindex", "-1");
                try { host.focus({preventScroll: true}); } catch (error) { host.focus(); }
            });
        });
    });

    bindDriverShiftOpeningForm(shell);
    function syncDriverDialProgress() {
        shell.querySelectorAll(".driver-work-dial").forEach(function (dial) {
            var loopProgress = Number(dial.dataset.driverLoopProgress || 0);
            if (!Number.isFinite(loopProgress)) {
                loopProgress = 0;
            }
            loopProgress = Math.max(0, Math.min(loopProgress, 100));
            var completedLoops = Number(dial.dataset.driverCompletedLoops || 0);
            if (!Number.isFinite(completedLoops)) {
                completedLoops = 0;
            }
            var hasPlan = dial.dataset.driverHasPlan === "1";
            var cappedProgress = hasPlan ? (completedLoops > 0 ? 100 : loopProgress) : 0;
            var overProgress = hasPlan && completedLoops > 0 ? loopProgress : 0;
            dial.style.setProperty("--driver-progress-capped", String(cappedProgress));
            dial.style.setProperty("--driver-over-progress", String(overProgress));
            dial.classList.toggle("is-over-plan", overProgress > 0);
        });
    }
    syncDriverDialProgress();
    function bindAssignmentCountdown() {
        var button = shell.querySelector("[data-driver-assignment-deadline]");
        var output = button ? button.querySelector("[data-driver-assignment-countdown]") : null;
        if (!button || !output) {
            return;
        }
        var deadline = Date.parse(button.dataset.driverAssignmentDeadline || "");
        if (!Number.isFinite(deadline)) {
            return;
        }
        function renderCountdown() {
            if (!button.isConnected) {
                window.clearInterval(timerId);
                return;
            }
            var remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
            var minutes = Math.floor(remaining / 60);
            var seconds = remaining % 60;
            output.textContent = String(minutes).padStart(2, "0") + ":" + String(seconds).padStart(2, "0");
            if (remaining > 0 || button.dataset.driverDeadlineReached === "true") {
                return;
            }
            button.dataset.driverDeadlineReached = "true";
            button.disabled = true;
            window.clearInterval(timerId);
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("assignment-deadline");
            } else {
                window.setTimeout(function () {
                    if (button.isConnected) {
                        window.location.reload();
                    }
                }, 1200);
            }
        }
        var timerId = window.setInterval(renderCountdown, 1000);
        renderCountdown();
    }
    bindAssignmentCountdown();
    function bindDriverShiftControls() {
        var form = shell.querySelector("[data-driver-shift-close-form]");
        var closeButton = shell.querySelector("[data-driver-shift-close-button]");
        var logoutButton = shell.querySelector("[data-driver-shift-logout]");
        var shiftScroll = form ? form.querySelector("[data-driver-shift-scroll]") : null;

        if (form && closeButton && closeButton.dataset.driverShiftBound !== "true") {
            closeButton.dataset.driverShiftBound = "true";
            form.addEventListener("submit", function (event) {
                if (
                    driverRoleIsReadonly()
                    || form.dataset.driverShiftHoldComplete !== "true"
                ) {
                    event.preventDefault();
                    if (
                        !driverRoleIsReadonly()
                        && form.dataset.driverShiftHoldComplete !== "true"
                        && typeof window.showDriverToast === "function"
                    ) {
                        window.showDriverToast("Удерживайте кнопку, чтобы закрыть смену");
                    }
                    return;
                }
                delete form.dataset.driverShiftHoldComplete;
                closeButton.disabled = true;
                closeButton.classList.add("is-pending");
                var closeShiftLabel = closeButton.querySelector("[data-mobile-shift-label]");
                if (closeShiftLabel) closeShiftLabel.textContent = "Закрываем смену";
            });
            bindDriverShiftHoldAction(form, closeButton, {
                holdMs: 1000,
                readyLabel: "Закрыть смену",
                progressProperty: "--driver-shift-hold"
            });

            var firstError = form.querySelector(".mobile-shift__field .errorlist");
            if (firstError) {
                window.requestAnimationFrame(function () {
                    var errorField = firstError.closest(".mobile-shift__field");
                    var errorInput = errorField ? errorField.querySelector("input") : null;
                    if (errorInput) {
                        errorInput.focus({preventScroll: true});
                    }
                });
            }
        }

        if (logoutButton && logoutButton.dataset.driverLogoutBound !== "true") {
            logoutButton.dataset.driverLogoutBound = "true";
            window.MobileShiftHold.bind(logoutButton, {
                holdMs: 2000,
                readyLabel: "Выйти",
                onShortPress: function () {
                    if (typeof window.showDriverToast === "function") window.showDriverToast("Удерживайте кнопку");
                },
                onComplete: function () {
                    var logoutLabel = logoutButton.querySelector("[data-driver-logout-label]");
                    if (logoutLabel) logoutLabel.textContent = "Выходим";
                    if (typeof window.navigateAfterNativeConnectionStop === "function") {
                        window.navigateAfterNativeConnectionStop(logoutButton.dataset.driverLogoutUrl);
                        return;
                    }
                    var stopPromise = window.NativeBackgroundConnection
                        && typeof window.NativeBackgroundConnection.stop === "function"
                        ? window.NativeBackgroundConnection.stop()
                        : null;
                    Promise.resolve(stopPromise).finally(function () {
                        window.location.href = logoutButton.dataset.driverLogoutUrl;
                    });
                }
            });
        }
    }
    bindDriverShiftControls();
    function isTextEditable(target) {
        return Boolean(target && target.closest && target.closest("input, textarea, select, option, [contenteditable='true'], [contenteditable='']"));
    }
    function clearDriverSelection() {
        var selection = window.getSelection ? window.getSelection() : null;
        if (selection && selection.removeAllRanges) {
            selection.removeAllRanges();
        }
    }
    shell.addEventListener("selectstart", function (event) {
        if (!isTextEditable(event.target)) {
            event.preventDefault();
            clearDriverSelection();
        }
    });
    shell.addEventListener("dragstart", function (event) {
        if (event.target && event.target.closest && event.target.closest("img, svg, canvas, video")) {
            event.preventDefault();
        }
    });
    shell.addEventListener("pointerdown", function (event) {
        if (!isTextEditable(event.target)) {
            clearDriverSelection();
        }
    }, true);
    shell.querySelectorAll(".driver-unload-tile strong").forEach(function (label) {
        var length = label.textContent.trim().length;
        if (length > 15) {
            label.classList.add("is-extra-long");
        } else if (length > 8) {
            label.classList.add("is-long");
        }
    });
    function splitDriverDialLabel(text) {
        var words = String(text || "").trim().replace(/\s+/g, " ").split(" ").filter(Boolean);
        if (words.length <= 3) {
            return words;
        }
        var bestLines = [words.slice(0, 1).join(" "), words.slice(1, -1).join(" "), words.slice(-1).join(" ")];
        var bestScore = Infinity;
        for (var firstBreak = 1; firstBreak <= words.length - 2; firstBreak += 1) {
            for (var secondBreak = firstBreak + 1; secondBreak <= words.length - 1; secondBreak += 1) {
                var lines = [
                    words.slice(0, firstBreak).join(" "),
                    words.slice(firstBreak, secondBreak).join(" "),
                    words.slice(secondBreak).join(" ")
                ];
                var lengths = lines.map(function (line) { return line.length; });
                var longest = Math.max.apply(Math, lengths);
                var shortest = Math.min.apply(Math, lengths);
                var score = (longest * 3) + (longest - shortest);
                if (score < bestScore) {
                    bestScore = score;
                    bestLines = lines;
                }
            }
        }
        return bestLines;
    }
    function preferredDriverDialFontSize(coreWidth, lineCount, textLength) {
        if (lineCount >= 3) {
            return Math.min(40, Math.max(30, coreWidth * 0.15));
        }
        if (lineCount === 2) {
            return Math.min(48, Math.max(35, coreWidth * 0.18));
        }
        if (textLength > 5) {
            return Math.min(54, Math.max(42, coreWidth * 0.2));
        }
        return Math.min(60, Math.max(50, coreWidth * 0.23));
    }
    function minimumDriverDialFontSize(lineCount, textLength) {
        if (lineCount >= 3) {
            return 25;
        }
        if (lineCount === 2) {
            return 28;
        }
        return textLength > 5 ? 34 : 42;
    }
    function renderDriverDialLabel(label, text) {
        var lines = splitDriverDialLabel(text);
        label.replaceChildren();
        lines.forEach(function (line) {
            var lineNode = document.createElement("span");
            lineNode.className = "driver-work-label-line";
            lineNode.textContent = line;
            label.appendChild(lineNode);
        });
        label.dataset.driverDialRaw = text;
        label.setAttribute("aria-label", text);
        label.classList.remove("is-single-medium", "is-two-line", "is-three-line");
        if (lines.length >= 3) {
            label.classList.add("is-three-line");
        } else if (lines.length === 2) {
            label.classList.add("is-two-line");
        } else if (text.replace(/\s+/g, "").length > 5) {
            label.classList.add("is-single-medium");
        }
        return lines;
    }
    function driverDialCoreHasVisibleGeometry(core) {
        if (!core || !core.isConnected || core.hidden) return false;
        if (core.closest && core.closest("[hidden]")) return false;
        if (!core.getClientRects || core.getClientRects().length === 0) return false;
        return core.clientWidth >= 1 && core.clientHeight >= 1;
    }
    function fitDriverDialLabel(label, force) {
        var core = label.closest(".driver-work-dial-core");
        var rawText = label.dataset.driverDialRaw || label.textContent.trim().replace(/\s+/g, " ");
        /* Если текст записали напрямую, строк-спанов нет: значит показывают не то, что
           лежит в driverDialRaw. Берём показанное за исходное и подгоняем заново —
           иначе подпись осталась бы кеглем прежней надписи и вылезла за круг. */
        var firstLine = label.firstElementChild;
        var hasLineNodes = !!(firstLine && firstLine.classList && firstLine.classList.contains("driver-work-label-line"));
        if (!hasLineNodes) {
            var shownText = label.textContent.trim().replace(/\s+/g, " ");
            if (shownText && shownText !== rawText) {
                rawText = shownText;
                label.dataset.driverDialRaw = shownText;
                delete label.dataset.driverDialFitKey;
            }
        }
        if (!rawText) return;
        if (!driverDialCoreHasVisibleGeometry(core)) {
            if (force) delete label.dataset.driverDialFitKey;
            return;
        }
        var coreWidth = Math.round(core.clientWidth);
        var coreHeight = Math.round(core.clientHeight);
        var fitKey = JSON.stringify([rawText, coreWidth, coreHeight]);
        if (!force && label.dataset.driverDialFitKey === fitKey) return;
        delete label.dataset.driverDialFitKey;
        label.style.removeProperty("font-size");
        var lines = renderDriverDialLabel(label, rawText);
        var textLength = rawText.replace(/\s+/g, "").length;
        core.classList.toggle("has-multiline-label", lines.length > 1);
        var maxSize = preferredDriverDialFontSize(coreWidth, lines.length, textLength);
        var minSize = Math.min(maxSize, minimumDriverDialFontSize(lines.length, textLength));
        var low = minSize;
        var high = maxSize;
        var best = minSize;
        /* Высота соседей, зазор сетки и ширина подписи от кегля не зависят — меряем их
           один раз до перебора. Раньше каждый шаг перебора читал их заново, и браузер
           пересчитывал раскладку круга до десяти раз подряд: на телефоне водителя это
           116 мс на каждую смену подписи (а при разгрузке их две подряд). */
        var percentNode = core.querySelector(".driver-work-percent");
        var noteNode = core.querySelector(".driver-work-note");
        var coreStyle = window.getComputedStyle(core);
        var gap = parseFloat(coreStyle.rowGap || coreStyle.gap) || 0;
        var availableHeight = core.clientHeight
            - (percentNode ? percentNode.offsetHeight : 0)
            - (noteNode ? noteNode.offsetHeight : 0)
            - (gap * 2)
            - 4;
        /* clientWidth включает внутренние отступы подписи, а строки меряются по полю
           содержимого: без вычета отступов длинная строка «пролезала» проверку и
           вылезала за рамку на их ширину. */
        var labelStyle = window.getComputedStyle(label);
        var availableWidth = label.clientWidth
            - (parseFloat(labelStyle.paddingLeft) || 0)
            - (parseFloat(labelStyle.paddingRight) || 0)
            + 1;
        function fits(size) {
            label.style.fontSize = size.toFixed(2) + "px";
            var linesFit = Array.prototype.every.call(label.children, function (lineNode) {
                return lineNode.scrollWidth <= availableWidth;
            });
            return linesFit && label.scrollHeight <= availableHeight;
        }
        if (fits(high)) {
            best = high;
        } else {
            /* Шесть шагов дают точность меньше половины пикселя на всём рабочем
               диапазоне кеглей — дальше перебирать нечего. */
            for (var step = 0; step < 6; step += 1) {
                var middle = (low + high) / 2;
                if (fits(middle)) {
                    best = middle;
                    low = middle;
                } else {
                    high = middle;
                }
            }
        }
        label.style.fontSize = best.toFixed(2) + "px";
        label.dataset.driverDialFitKey = fitKey;
    }
    function fitDriverDialLabels(force) {
        shell.querySelectorAll("[data-driver-dial-label]").forEach(function (label) {
            fitDriverDialLabel(label, force);
        });
    }
    function scheduleDriverDialLabelFit(force) {
        if (force) window.driverDialLabelFitForce = true;
        if (
            window.driverDialLabelFitFrame !== null
            && typeof window.driverDialLabelFitFrame !== "undefined"
        ) {
            window.cancelAnimationFrame(window.driverDialLabelFitFrame);
            window.driverDialLabelFitFrame = null;
        }
        window.driverDialLabelFitFrame = window.requestAnimationFrame(function () {
            window.driverDialLabelFitFrame = null;
            var shouldForce = window.driverDialLabelFitForce === true;
            window.driverDialLabelFitForce = false;
            fitDriverDialLabels(shouldForce);
        });
    }
    window.scheduleDriverDialLabelFit = scheduleDriverDialLabelFit;
    if (window.driverDialLabelResizeObserver) {
        window.driverDialLabelResizeObserver.disconnect();
    }
    if ("ResizeObserver" in window) {
        window.driverDialLabelResizeObserver = new ResizeObserver(function (entries) {
            var hasVisibleCore = Array.prototype.some.call(entries, function (entry) {
                return driverDialCoreHasVisibleGeometry(entry.target);
            });
            if (hasVisibleCore) scheduleDriverDialLabelFit();
        });
        shell.querySelectorAll(".driver-work-dial-core").forEach(function (core) {
            window.driverDialLabelResizeObserver.observe(core);
        });
    }
    scheduleDriverDialLabelFit();
    if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(function () {
            scheduleDriverDialLabelFit(true);
        });
    }
    function generateClientActionId(prefix) {
        if (window.crypto && window.crypto.randomUUID) {
            return prefix + "-" + window.crypto.randomUUID();
        }
        return prefix + "-" + Date.now() + "-" + Math.random().toString(16).slice(2);
    }
    function fillClientAction(form, prefix) {
        var input = form.querySelector("[data-driver-client-action]");
        if (input && !input.value) {
            input.value = generateClientActionId(prefix);
        }
    }
    function clearDriverMessages() {
        var messages = shell.querySelector(".driver-messages");
        if (messages && shell.dataset.activeTab === "downtimes") {
            messages.innerHTML = "";
        }
    }
    /* Форма начала смены привязывается отдельно, за пределами этой области. */
    window.showDriverToast = function (message) { showDriverToast(message); };
    function showDriverToast(message) {
        var toast = shell.querySelector("[data-driver-toast]");
        if (!toast) {
            return;
        }
        toast.textContent = message || "Действие не выполнено";
        toast.hidden = false;
        if (window.driverToastTimerId) {
            window.clearTimeout(window.driverToastTimerId);
        }
        window.driverToastTimerId = window.setTimeout(function () {
            toast.hidden = true;
        }, 3600);
    }
    function openDriverTab(tab) {
        var allowedTabs = ["work", "shift", "downtimes", "manifest"];
        if (allowedTabs.indexOf(tab) < 0) tab = "work";
        /* Новая панель должна получить базовую плотность до первого paint:
           иначе она на кадр наследует tight/compact от предыдущей вкладки. */
        shell.dataset.driverDensity = shell.dataset.driverViewportDensity
            || shell.dataset.driverDensity
            || "normal";
        syncDriverTabMarkup(shell, tab);
        if (tab !== "work" && window.driverDomBehindBaseline === true
            && window.AppRealtime && typeof window.AppRealtime.requestReconcile === "function") {
            window.driverForceFragmentApply = true;
            window.AppRealtime.requestReconcile("driver_tab_opened");
        }
        if (window.history && window.history.replaceState) {
            var url = new URL(window.location.href);
            url.searchParams.set("tab", tab);
            window.history.replaceState({}, "", url.toString());
        }
        try {
            window.localStorage.setItem("driver-active-tab-v1:" + shell.dataset.driverAccessId, tab);
        } catch (error) {}
        clearDriverMessages();
        if (tab === "work") scheduleDriverDialLabelFit();
        scheduleDriverTabSettle(tab);
    }
    (function restoreDriverTab() {
        var allowedTabs = ["work", "shift", "downtimes", "manifest"];
        var requested = "";
        var stored = "";
        try { requested = new URL(window.location.href).searchParams.get("tab") || ""; } catch (error) {}
        try { stored = window.localStorage.getItem("driver-active-tab-v1:" + shell.dataset.driverAccessId) || ""; } catch (error) {}
        var restored = allowedTabs.indexOf(requested) >= 0 ? requested : stored;
        if (allowedTabs.indexOf(restored) >= 0) openDriverTab(restored);
    })();
    document.querySelectorAll("[data-driver-tab-open]").forEach(function (button) {
        button.addEventListener("click", function () {
            openDriverTab(button.dataset.driverTabOpen);
        });
    });

    var manifestPanel = shell.querySelector("[data-driver-tab-panel='manifest']");
    function driverMetricValue(name) {
        var input = shell.querySelector("[name='" + name + "']");
        if (input && String(input.value || "").trim()) return String(input.value).trim();
        if (!manifestPanel) return "—";
        var reportValues = {
            end_fuel: manifestPanel.dataset.driverReportEndFuel,
            end_mileage: manifestPanel.dataset.driverReportEndMileage,
            end_engine_hours: manifestPanel.dataset.driverReportEndEngineHours
        };
        return String(reportValues[name] || "").trim() || "—";
    }
    function syncDriverReportMetrics() {
        if (!manifestPanel) return;
        var units = {end_fuel: " л", end_mileage: " км", end_engine_hours: " м/ч"};
        manifestPanel.querySelectorAll("[data-driver-report-metric]").forEach(function (node) {
            var name = node.dataset.driverReportMetric;
            var value = driverMetricValue(name);
            node.textContent = value === "—" ? value : value + (units[name] || "");
        });
    }
    function buildDriverShiftReportText() {
        if (!manifestPanel) return "";
        syncDriverReportMetrics();
        function tripWord(count) {
            count = Math.abs(Number(count) || 0);
            if (count % 10 === 1 && count % 100 !== 11) return "рейс";
            if ([2, 3, 4].includes(count % 10) && ![12, 13, 14].includes(count % 100)) return "рейса";
            return "рейсов";
        }
        var lines = ["Б-" + manifestPanel.dataset.driverReportTruck + " · " + manifestPanel.dataset.driverReportDriver, "", "РЕЙСЫ"];
        var tripRows = manifestPanel.querySelectorAll("[data-driver-report-trip]");
        if (tripRows.length) {
            tripRows.forEach(function (row) {
                lines.push(row.dataset.excavator + " " + row.dataset.dumpPoint + " - " + row.dataset.count + " " + tripWord(row.dataset.count) + ".");
            });
        } else {
            lines.push("Завершённых рейсов нет");
        }
        lines.push("Всего: " + manifestPanel.dataset.driverReportTripTotal + " " + tripWord(manifestPanel.dataset.driverReportTripTotal) + ".", "", "ПРОСТОИ");
        var downtimeRows = manifestPanel.querySelectorAll("[data-driver-report-downtime]");
        if (downtimeRows.length) {
            downtimeRows.forEach(function (row) {
                lines.push(row.dataset.reason + " — " + row.dataset.duration);
            });
        } else {
            lines.push("Простоев нет");
        }
        lines.push(
            "Всего простоев: " + manifestPanel.dataset.driverReportDowntimeTotal,
            "",
            "ТЕХНИКА НА КОНЕЦ СМЕНЫ",
            "Топливо: " + driverMetricValue("end_fuel") + (driverMetricValue("end_fuel") === "—" ? "" : " л."),
            "Одометр: " + driverMetricValue("end_mileage") + (driverMetricValue("end_mileage") === "—" ? "" : " км."),
            "Моточасы: " + driverMetricValue("end_engine_hours") + (driverMetricValue("end_engine_hours") === "—" ? "" : " м/ч.")
        );
        return lines.join("\n");
    }
    function copyDriverReportText(text) {
        if (navigator.clipboard && window.isSecureContext && typeof navigator.clipboard.writeText === "function") {
            return navigator.clipboard.writeText(text);
        }
        return new Promise(function (resolve, reject) {
            var field = document.createElement("textarea");
            field.value = text;
            field.setAttribute("readonly", "");
            field.style.position = "fixed";
            field.style.opacity = "0";
            document.body.appendChild(field);
            field.select();
            var copied = false;
            try {
                copied = Boolean(document.execCommand && document.execCommand("copy"));
            } catch (error) {}
            field.remove();
            if (copied) resolve();
            else reject(new Error("copy_failed"));
        });
    }
    function openDriverMaxGroup(url) {
        var link = document.createElement("a");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer external";
        link.hidden = true;
        document.body.appendChild(link);
        link.click();
        link.remove();
    }
    function createDriverReportDeliveryController(options) {
        var preparedText = "";
        var copyPending = false;
        function render(ready) {
            options.button.classList.toggle("is-max-ready", ready);
            options.button.dataset.driverReportState = ready ? "max" : "prepare";
            options.title.textContent = ready ? "Открыть группу" : "Подготовить путёвку";
            options.hint.textContent = ready ? "текст уже скопирован" : "для отправки диспетчеру";
        }
        function reset() {
            preparedText = "";
            render(false);
        }
        function handleClick() {
            if (copyPending) return;
            var currentText = options.buildText();
            if (preparedText) {
                if (currentText !== preparedText) {
                    reset();
                    options.notify("Данные изменились. Подготовьте путёвку снова");
                    return;
                }
                options.openGroup(options.groupUrl);
                return;
            }
            copyPending = true;
            options.button.disabled = true;
            Promise.resolve(options.copyText(currentText)).then(function () {
                preparedText = currentText;
                render(true);
                options.notify("Путёвка скопирована. Теперь откройте MAX");
            }).catch(function () {
                reset();
                options.notify("Не удалось скопировать путёвку");
            }).finally(function () {
                copyPending = false;
                options.button.disabled = Boolean(options.isReadonly && options.isReadonly());
            });
        }
        render(false);
        return {handleClick: handleClick, reset: reset};
    }
    if (manifestPanel) {
        manifestPanel.querySelectorAll("[data-driver-manifest-view-open]").forEach(function (button) {
            button.addEventListener("click", function () {
                var view = button.dataset.driverManifestViewOpen;
                manifestPanel.querySelectorAll("[data-driver-manifest-view-open]").forEach(function (item) {
                    var active = item === button;
                    item.classList.toggle("is-active", active);
                    item.setAttribute("aria-selected", active ? "true" : "false");
                });
                manifestPanel.querySelectorAll("[data-driver-manifest-view]").forEach(function (panel) {
                    panel.classList.toggle("is-active", panel.dataset.driverManifestView === view);
                });
            });
        });
        var reportDeliveryButton = manifestPanel.querySelector("[data-driver-report-delivery]");
        var reportDelivery = null;
        if (reportDeliveryButton) {
            reportDelivery = createDriverReportDeliveryController({
                button: reportDeliveryButton,
                title: reportDeliveryButton.querySelector("[data-driver-report-action-title]"),
                hint: reportDeliveryButton.querySelector("[data-driver-report-action-hint]"),
                groupUrl: reportDeliveryButton.dataset.driverMaxGroupUrl,
                buildText: buildDriverShiftReportText,
                copyText: copyDriverReportText,
                openGroup: openDriverMaxGroup,
                notify: showDriverToast,
                isReadonly: driverRoleIsReadonly
            });
            reportDeliveryButton.addEventListener("click", reportDelivery.handleClick);
        }
        shell.querySelectorAll("[name='end_fuel'], [name='end_mileage'], [name='end_engine_hours']").forEach(function (input) {
            input.addEventListener("input", function () {
                syncDriverReportMetrics();
                if (reportDelivery) reportDelivery.reset();
            });
        });
        syncDriverReportMetrics();
    }
    shell.querySelectorAll("form").forEach(function (form) {
        if (!form.matches("[data-driver-hold-form]")) {
            form.addEventListener("submit", function () {
                fillClientAction(form, "driver-action");
            });
        }
    });
    clearDriverMessages();

    var downtimePanel = shell.querySelector("[data-driver-tab-panel='downtimes']");
    var downtimeCard = shell.querySelector("[data-driver-active-downtime-id]");
    var downtimeDuration = shell.querySelector("[data-driver-active-duration]");
    var downtimeTitle = shell.querySelector("[data-driver-active-title]");
    var downtimeReason = shell.querySelector("[data-driver-active-reason]");
    var downtimeClose = shell.querySelector("[data-driver-close-downtime]");
    var downtimeReasonButtons = shell.querySelectorAll("[data-driver-downtime-reason-button]");
    var downtimeUrl = downtimePanel ? downtimePanel.dataset.driverDowntimeUrl : "";
    var downtimeCsrfInput = downtimePanel ? downtimePanel.querySelector("input[name='csrfmiddlewaretoken']") : null;
    var downtimeCsrfToken = downtimeCsrfInput ? downtimeCsrfInput.value : "";
    var holdForm = shell.querySelector("[data-driver-hold-form]");
    var holdButton = shell.querySelector("[data-driver-hold-button]");
    var workDial = shell.querySelector(".driver-work-dial");
    var workDialControl = shell.querySelector("[data-driver-work-dial-control]");
    var driverOfflineEvents = window.driverOfflineEvents || [];

    function driverInstallId() {
        var key = "field-device-install-id-v1";
        var value = "";
        try { value = String(window.localStorage.getItem(key) || ""); } catch (error) {}
        if (!value) {
            value = window.crypto && typeof window.crypto.randomUUID === "function"
                ? window.crypto.randomUUID()
                : "driver-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
            try { window.localStorage.setItem(key, value); } catch (error) { return ""; }
        }
        return value;
    }

    function driverOfflineContext() {
        var current = document.querySelector("[data-driver-shell]");
        return {
            actorId: current && current.dataset.driverActorId,
            accessId: current && current.dataset.driverAccessId,
            shiftId: current && current.dataset.driverShiftId,
            equipmentId: current && current.dataset.driverCurrentTruckId,
            authGeneration: current && current.dataset.driverAuthGeneration,
            deviceId: driverInstallId()
        };
    }

    function renderDriverOfflineState(state) {
        var current = document.querySelector("[data-driver-shell]");
        if (!current) return;
        driverOfflineEvents = Array.isArray(state.events) ? state.events : [];
        window.driverOfflineEvents = driverOfflineEvents;
        var label = current.querySelector("[data-driver-sync-label]");
        var pending = Number(state.pending || 0);
        var review = Number(state.review || 0);
        var previousPending = Number(window.driverOfflinePendingCount || 0);
        window.driverOfflinePendingCount = pending;
        window.operationalOutboxPendingCount = pending;
        window.dispatchEvent(new CustomEvent("operational-outbox-state", {detail: {
            role: "driver", pendingCount: pending, reviewCount: review
        }}));
        var mode = state.review ? "review" : state.sending ? "sending" : state.pending ? "pending" : "confirmed";
        current.dataset.driverSyncState = mode;
        if (label) {
            label.textContent = mode === "review"
                ? "Не подтверждено"
                : mode === "sending"
                    ? "Отправка"
                    : mode === "pending"
                        ? "Действие сохранено"
                        : "Онлайн";
        }
        // Доступность сервера и accessible-label точки принадлежат общей машине связи.
        applyDriverOfflineProjection(current, driverOfflineEvents);
        if (previousPending > 0 && pending === 0 && window.AppRealtime && typeof window.AppRealtime.requestReconcile === "function") {
            window.AppRealtime.requestReconcile("driver_offline_queue_drained");
        }
    }

    function setDriverPointSyncState(current, mode) {
        var status = current && current.querySelector("[data-driver-point-sync-state]");
        if (!status) return;
        status.classList.toggle("is-local", mode === "local");
        status.classList.toggle("is-review", mode === "review");
        status.textContent = mode === "review"
            ? "Не подтверждено"
            : mode === "local"
                ? "Действие сохранено"
                : "Подтверждено сервером";
    }

    function applyDriverPointSelection(current, pointId, pointName, mode) {
        if (!current || !pointId) return;
        pointId = String(pointId);
        pointName = String(pointName || "").trim();
        current.dataset.driverActualDumpPointId = pointId;
        if (pointName) current.dataset.driverActualDumpPointName = pointName;

        current.querySelectorAll(".driver-unload-tile").forEach(function (tile) {
            tile.classList.remove("is-current");
            tile.removeAttribute("aria-current");
            var tileStatus = tile.querySelector("[data-driver-point-tile-status]");
            if (tileStatus) tileStatus.textContent = "";
        });
        var pointInput = current.querySelector('.driver-unload-tile-form [name="dump_point"][value="' + pointId + '"]');
        var pointButton = pointInput && pointInput.closest("form") && pointInput.closest("form").querySelector(".driver-unload-tile");
        if (pointButton) {
            pointButton.classList.add("is-current");
            pointButton.setAttribute("aria-current", "true");
            pointName = pointName || String(pointButton.dataset.driverPointName || "").trim();
            var selectedStatus = pointButton.querySelector("[data-driver-point-tile-status]");
            if (selectedStatus) selectedStatus.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3 8 3 3 7-7"></path></svg>Текущая';
        }
        var currentPointLabel = current.querySelector("[data-driver-current-point-name]");
        if (currentPointLabel && pointName) currentPointLabel.textContent = pointName;
        var dialLabel = current.querySelector("[data-driver-dial-label]");
        if (dialLabel && pointName) {
            dialLabel.textContent = pointName;
            dialLabel.dataset.driverDialRaw = pointName;
            if (typeof scheduleDriverDialLabelFit === "function") scheduleDriverDialLabelFit();
        }
        setDriverPointSyncState(current, mode || "confirmed");
    }

    function applyDriverOfflineProjection(current, events) {
        if (!current) return;
        var ordered = (events || []).slice().sort(function (a, b) { return Number(a.sequence) - Number(b.sequence); });
        var activeTripId = String(current.dataset.driverActiveTripId || "");
        var unload = ordered.find(function (event) {
            return event.event_type === "driver.trip.unloaded" && String(event.trip_id || "") === activeTripId;
        });
        if (unload) {
            var unloadNeedsReview = ["conflict", "auth_required", "invalid"].includes(unload.state);
            current.dataset.driverHasOpenTrip = "false";
            current.dataset.driverHasLoadedTrip = "false";
            var dial = current.querySelector(".driver-work-dial");
            var button = current.querySelector("[data-driver-hold-button]");
            var dialLabel = current.querySelector("[data-driver-dial-label]");
            var note = current.querySelector(".driver-work-note");
            if (dial) { dial.classList.remove("is-loaded"); dial.classList.add("is-empty"); }
            if (button) {
                button.disabled = true;
                button.classList.remove("is-loaded", "is-pending", "is-holding");
                button.classList.add("is-empty");
            }
            if (dialLabel) {
                /* Подпись обязана пройти подгонку под круг: раньше сюда писали только текст,
                   прежний ключ подгонки оставался прежним, и длинная надпись выводилась
                   кеглем короткой — она вылезала за круг и обрезалась. */
                var unloadDialText = unloadNeedsReview ? "НЕ ПОДТВЕРЖДЕНО" : "РАЗГРУЗКА СОХРАНЕНА";
                dialLabel.textContent = unloadDialText;
                dialLabel.dataset.driverDialRaw = unloadDialText;
                delete dialLabel.dataset.driverDialFitKey;
                if (typeof scheduleDriverDialLabelFit === "function") scheduleDriverDialLabelFit();
            }
            if (note) note.textContent = unloadNeedsReview ? "ПРОВЕРЬТЕ СОБЫТИЕ" : "ОЖИДАНИЕ СИНХРОНИЗАЦИИ";
            var pointCard = current.querySelector("[data-driver-point-open]");
            if (pointCard) {
                pointCard.disabled = true;
                pointCard.hidden = true;
                pointCard.setAttribute("aria-expanded", "false");
            }
            var sheet = current.querySelector("[data-driver-point-sheet]");
            if (sheet) sheet.hidden = true;
        }
        var pointEvents = ordered.filter(function (event) {
            return event.event_type === "driver.trip.dump_point_changed" && String(event.trip_id || "") === activeTripId;
        });
        if (!unload && pointEvents.length) {
            var latestPoint = pointEvents[pointEvents.length - 1];
            var pointId = String(latestPoint.payload && latestPoint.payload.dump_point_id || "");
            var pointForm = current.querySelector('.driver-unload-tile-form [name="dump_point"][value="' + pointId + '"]');
            var pointButton = pointForm && pointForm.closest("form") && pointForm.closest("form").querySelector(".driver-unload-tile");
            var pointName = pointButton && pointButton.dataset.driverPointName || "";
            var pointMode = ["conflict", "auth_required", "invalid"].includes(latestPoint.state) ? "review" : "local";
            applyDriverPointSelection(current, pointId, pointName, pointMode);
        }
        var latestDowntime = typeof window.selectDriverDowntimeProjection === "function"
            ? window.selectDriverDowntimeProjection(ordered)
            : ordered.slice().reverse().find(function (event) {
                return (event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended")
                    && ["conflict", "auth_required", "invalid"].indexOf(String(event.state || "pending")) === -1;
            });
        if (latestDowntime) {
            if (latestDowntime.event_type === "driver.downtime.ended") {
                clearDriverActiveDowntime({
                    shift_total_seconds: downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds || 0
                });
            } else {
                var reasonId = String(latestDowntime.payload && latestDowntime.payload.reason_id || "");
                var reasonButton = current.querySelector('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + reasonId + '"]');
                applyDriverActiveDowntime({
                    active: true,
                    event_id: "local:" + latestDowntime.event_id,
                    reason_id: reasonId,
                    reason: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                    reason_label: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                    workflow: reasonButton && reasonButton.dataset.driverDowntimeFlow || "",
                    status_key: reasonButton && reasonButton.dataset.driverStatusKey || "yellow",
                    started_at: latestDowntime.occurred_at,
                    elapsed_seconds: 0,
                    shift_total_seconds: downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds || 0,
                    calculated_at: latestDowntime.occurred_at
                });
            }
        }
        if (window.DriverFreeBucket && typeof window.DriverFreeBucket.renderProjection === "function") {
            window.DriverFreeBucket.renderProjection(current, ordered);
        }
    }

    function driverOfflineBindings() {
        return {
            context: driverOfflineContext,
            onState: renderDriverOfflineState,
            onConfirmed: function () {
                var args = arguments;
                var event = args[0] || {};
                if (event.event_type === "driver.trip.unloaded" && !window.driverOfflineConfirmationCueScheduled) {
                    window.driverOfflineConfirmationCueScheduled = true;
                    playDriverVoice("action_ok", "voice_trip_finished");
                    window.setTimeout(function () { window.driverOfflineConfirmationCueScheduled = false; }, 750);
                }
                var result = args[1] || {};
                var isDowntime = event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended";
                if (isDowntime) {
                    var serverIds = result.server_ids || {};
                    var downtimeId = String(serverIds.downtime_event_id || serverIds.downtime_id || "");
                    if (downtimeId) {
                        window.driverOwnDowntimeEventIds = (window.driverOwnDowntimeEventIds || []).concat(downtimeId).slice(-50);
                    }
                }
                if (window.AppRealtime && typeof window.AppRealtime.requestReconcile === "function") {
                    // Для простоя версию не передаём: иначе опрос спросит события «после неё»
                    // и не вернёт наш же простой — а он и есть доказательство, что экран
                    // подменять не нужно (см. applyOperationalStateRefresh).
                    if (isDowntime) {
                        window.AppRealtime.requestReconcile("driver_offline_event_confirmed");
                    } else {
                        window.AppRealtime.requestReconcile(
                            "driver_offline_event_confirmed",
                            Number(result.server_version || result.version || 0)
                        );
                    }
                }
            },
            onReview: function (event, result) {
                showDriverToast(result.message || "Действие не подтверждено сервером. Обновите экран; если состояние неверное — сообщите диспетчеру.");
                if (
                    event
                    && (event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended")
                    && window.AppRealtime
                    && typeof window.AppRealtime.requestReconcile === "function"
                ) {
                    window.AppRealtime.requestReconcile(
                        "driver_downtime_review",
                        Number(result.server_version || result.version || 0)
                    );
                }
            }
        };
    }

    function createDriverOfflineRuntime() {
        if (typeof window.createDriverOfflineOutbox !== "function") {
            return {
                enqueue: function () { return Promise.reject(new Error("offline_runtime_unavailable")); },
                flush: function () { return Promise.resolve([]); },
                pending: function () { return Promise.resolve([]); },
                publish: function () { return Promise.resolve([]); },
                getServerMapping: function () { return Promise.resolve(null); },
                getDowntimeProjectionReceipt: function () { return Promise.resolve(null); }
            };
        }
        if (window.driverOfflineOutbox && window.driverOfflineOutboxAccessId === shell.dataset.driverAccessId) {
            var existingBindings = driverOfflineBindings();
            window.driverOfflineOutbox.setBindings(existingBindings).then(function () {
                return window.driverOfflineOutbox.resumeAuthRequired(driverOfflineContext().authGeneration);
            }).then(function () {
                return window.driverOfflineOutbox.flush();
            }).catch(function () {});
            return window.driverOfflineOutbox;
        }
        window.driverOfflineOutboxAccessId = shell.dataset.driverAccessId;
        var csrf = document.querySelector('meta[name="csrf-token"]');
        window.driverOfflineOutbox = window.createDriverOfflineOutbox({
            accessId: shell.dataset.driverAccessId,
            indexedDB: window.indexedDB,
            localStorage: window.localStorage,
            context: driverOfflineContext,
            send: function (batch) {
                return fetch("/offline-events/sync/", {
                    method: "POST",
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-CSRFToken": csrf ? csrf.content : "",
                        "X-Requested-With": "XMLHttpRequest"
                    },
                    body: JSON.stringify(batch)
                }).then(function (response) {
                    return response.text().then(function (body) {
                        var payload = {};
                        try { payload = JSON.parse(body || "{}"); } catch (error) {}
                        if (response.status >= 500) throw new Error("server_unavailable");
                        var needsAuthentication = typeof window.isDriverSyncAuthResponse === "function"
                            && window.isDriverSyncAuthResponse(response, body, window.location.href);
                        if (needsAuthentication) {
                            return {results: batch.events.map(function (event) {
                                return {event_id: event.event_id, status: "auth_required", code: "auth_required", message: "Требуется повторный вход."};
                            })};
                        }
                        if (!response.ok && !Array.isArray(payload.results)) throw new Error("sync_rejected");
                        return payload;
                    });
                });
            },
            onState: driverOfflineBindings().onState,
            onConfirmed: driverOfflineBindings().onConfirmed,
            onReview: driverOfflineBindings().onReview
        });
        window.driverOfflineOutbox.initialize().catch(function () {
            var current = document.querySelector("[data-driver-shell]");
            if (current) {
                current.dataset.driverSyncState = "storage-error";
                var connection = current.querySelector(".driver-online");
                if (connection) connection.setAttribute("aria-label", "Локальное сохранение недоступно; действие не выполнено");
            }
            showDriverToast("Не удалось открыть защищённое хранилище. Действия без связи недоступны.");
        });
        return window.driverOfflineOutbox;
    }

    var driverOfflineOutbox = createDriverOfflineRuntime();
    function restoreDriverConfirmedDowntime(outbox) {
        var context = driverOfflineContext();
        if (!downtimeCard || !outbox || typeof outbox.getDowntimeProjectionReceipt !== "function") {
            return Promise.resolve(false);
        }
        return outbox.getDowntimeProjectionReceipt(context.shiftId, context.equipmentId).then(function (receipt) {
            if (!receipt) return false;
            var receiptAt = Date.parse(receipt.confirmed_at || receipt.occurred_at || "");
            var shellAt = Date.parse(downtimeCard.dataset.driverDowntimeCalculatedAt || "");
            if (!Number.isFinite(receiptAt) || (Number.isFinite(shellAt) && receiptAt <= shellAt)) {
                return false;
            }
            if (receipt.event_type === "driver.downtime.ended") {
                var closedProjection = receipt.projection || {};
                clearDriverActiveDowntime({
                    shift_total_seconds: closedProjection.shift_total_seconds || downtimeCard.dataset.driverShiftDowntimeSeconds || 0,
                    reason_totals: closedProjection.reason_totals || null,
                    calculated_at: receipt.confirmed_at || receipt.occurred_at
                });
                return true;
            }
            if (receipt.event_type !== "driver.downtime.started") return false;
            var reasonId = String(receipt.payload && receipt.payload.reason_id || "");
            var reasonButton = shell.querySelector('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + reasonId + '"]');
            var serverIds = receipt.server_ids || {};
            var activeProjection = receipt.projection || {};
            return applyDriverActiveDowntime({
                active: true,
                event_id: String(serverIds.downtime_event_id || serverIds.downtime_id || "confirmed:" + receipt.event_id),
                reason_id: reasonId,
                reason: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                reason_label: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                workflow: reasonButton && reasonButton.dataset.driverDowntimeFlow || "",
                status_key: reasonButton && reasonButton.dataset.driverStatusKey || "yellow",
                started_at: receipt.occurred_at,
                elapsed_seconds: activeProjection.active_elapsed_seconds || 0,
                shift_total_seconds: activeProjection.shift_total_seconds || downtimeCard.dataset.driverShiftDowntimeSeconds || 0,
                reason_totals: activeProjection.reason_totals || null,
                calculated_at: receipt.confirmed_at || receipt.occurred_at
            });
        });
    }
    restoreDriverConfirmedDowntime(driverOfflineOutbox).catch(function () {});
    if (window.DriverFreeBucket && typeof window.DriverFreeBucket.bind === "function") {
        window.DriverFreeBucket.bind({shell: shell, outbox: driverOfflineOutbox});
    }

    function formatDriverDowntimeDuration(seconds) {
        seconds = Math.max(0, Math.floor(Number(seconds) || 0));
        var hours = Math.floor(seconds / 3600);
        var minutes = Math.floor((seconds % 3600) / 60);
        var rest = seconds % 60;
        return [hours, minutes, rest].map(function (part) {
            return String(part).padStart(2, "0");
        }).join(":");
    }

    function clearDriverDowntimeTimer() {
        if (window.driverDowntimeTimerId) {
            window.clearInterval(window.driverDowntimeTimerId);
            window.driverDowntimeTimerId = null;
        }
        window.driverDowntimeClock = null;
    }

    function renderDriverReasonDuration(button, totalSeconds, isActive) {
        if (!button) return;
        var duration = button.querySelector("[data-driver-reason-duration]");
        if (!duration) return;
        var seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        var isVisible = seconds > 0 || !!isActive;
        duration.hidden = !isVisible;
        button.classList.toggle("is-used", isVisible);
        if (isVisible) {
            duration.textContent = formatDriverDowntimeDuration(seconds);
        }
        if (button.getAttribute("aria-disabled") !== "true") {
            var reasonLabel = button.dataset.driverReasonLabel || button.dataset.driverReason || "Причина простоя";
            var actionLabel = isActive ? "Активный простой" : "Начать простой";
            button.setAttribute(
                "aria-label",
                actionLabel + ": " + reasonLabel + (isVisible ? ". За смену: " + formatDriverDowntimeDuration(seconds) : "")
            );
        }
    }

    function syncDriverReasonTotals(payload) {
        payload = payload || {};
        var reasonTotals = payload.reason_totals;
        var activeReasonId = payload.active ? String(payload.reason_id || "") : "";
        downtimeReasonButtons.forEach(function (button) {
            var reasonId = String(button.dataset.driverDowntimeReasonId || "");
            if (
                reasonTotals
                && typeof reasonTotals === "object"
                && Object.prototype.hasOwnProperty.call(reasonTotals, reasonId)
            ) {
                button.dataset.driverReasonSeconds = String(
                    Math.max(0, Math.floor(Number(reasonTotals[reasonId]) || 0))
                );
            }
            renderDriverReasonDuration(
                button,
                button.dataset.driverReasonSeconds,
                !!activeReasonId && reasonId === activeReasonId
            );
        });
    }

    function startDriverDowntimeTimer(payload) {
        clearDriverDowntimeTimer();
        payload = payload || {};
        syncDriverReasonTotals(payload);
        var activeReasonId = String(payload.reason_id || "");
        var calculatedAtMs = Date.parse(payload.calculated_at || "");
        var syncedAtMs = Number.isFinite(calculatedAtMs)
            ? Math.min(Date.now(), calculatedAtMs)
            : Date.now();
        var clock = {
            activeReasonId: activeReasonId,
            baseActiveElapsedSeconds: Math.max(0, Math.floor(Number(payload.elapsed_seconds) || 0)),
            baseShiftSeconds: Math.max(0, Math.floor(Number(payload.shift_total_seconds) || 0)),
            syncedAtMs: syncedAtMs
        };
        window.driverDowntimeClock = clock;
        function tick() {
            var liveSeconds = Math.max(0, Math.floor((Date.now() - clock.syncedAtMs) / 1000));
            if (downtimeDuration) {
                downtimeDuration.textContent = formatDriverDowntimeDuration(clock.baseShiftSeconds + liveSeconds);
            }
            downtimeReasonButtons.forEach(function (button) {
                var reasonId = String(button.dataset.driverDowntimeReasonId || "");
                var baseSeconds = Math.max(0, Math.floor(Number(button.dataset.driverReasonSeconds) || 0));
                var reasonIsActive = !!activeReasonId && reasonId === activeReasonId;
                renderDriverReasonDuration(
                    button,
                    baseSeconds + (reasonIsActive ? liveSeconds : 0),
                    reasonIsActive
                );
            });
        }
        tick();
        window.driverDowntimeTimerId = window.setInterval(tick, 1000);
    }

    function snapshotDriverDowntimeTimer(atMs) {
        var clock = window.driverDowntimeClock;
        if (!clock || !downtimeCard) {
            return Math.max(0, Math.floor(Number(downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds) || 0));
        }
        atMs = Number.isFinite(Number(atMs)) ? Number(atMs) : Date.now();
        var liveSeconds = Math.max(0, Math.floor((atMs - clock.syncedAtMs) / 1000));
        var shiftTotalSeconds = clock.baseShiftSeconds + liveSeconds;
        var activeReasonButton = shell.querySelector(
            '[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + clock.activeReasonId + '"]'
        );
        if (activeReasonButton) {
            var reasonTotalSeconds = Math.max(0, Math.floor(Number(activeReasonButton.dataset.driverReasonSeconds) || 0)) + liveSeconds;
            activeReasonButton.dataset.driverReasonSeconds = String(reasonTotalSeconds);
            renderDriverReasonDuration(activeReasonButton, reasonTotalSeconds, true);
        }
        clock.baseShiftSeconds = shiftTotalSeconds;
        clock.baseActiveElapsedSeconds += liveSeconds;
        clock.syncedAtMs = atMs;
        downtimeCard.dataset.driverActiveElapsedSeconds = String(clock.baseActiveElapsedSeconds);
        downtimeCard.dataset.driverShiftDowntimeSeconds = String(shiftTotalSeconds);
        if (downtimeDuration) {
            downtimeDuration.textContent = formatDriverDowntimeDuration(shiftTotalSeconds);
        }
        return shiftTotalSeconds;
    }

    function driverDowntimeProjectionSnapshot() {
        var reasonTotals = {};
        downtimeReasonButtons.forEach(function (button) {
            var reasonId = String(button.dataset.driverDowntimeReasonId || "");
            if (reasonId) {
                reasonTotals[reasonId] = Math.max(0, Math.floor(Number(button.dataset.driverReasonSeconds) || 0));
            }
        });
        return {
            active_elapsed_seconds: Math.max(0, Math.floor(Number(downtimeCard && downtimeCard.dataset.driverActiveElapsedSeconds) || 0)),
            shift_total_seconds: Math.max(0, Math.floor(Number(downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds) || 0)),
            reason_totals: reasonTotals
        };
    }

    function setDriverDowntimeStatusClass(statusKey) {
        if (!downtimeCard) {
            return;
        }
        ["status-gray", "status-yellow", "status-green", "status-blue", "status-orange", "status-red"].forEach(function (className) {
            downtimeCard.classList.remove(className);
        });
        downtimeCard.classList.add("status-" + (statusKey || "yellow"));
    }

    function applyDriverWaitingMode(payload) {
        var flow = String((payload && payload.workflow) || "");
        var isLoadingWait = flow === "waiting_loading";
        var isUnloadingWait = flow === "waiting_unload";
        var isWaiting = isLoadingWait || isUnloadingWait;
        if (holdForm) {
            holdForm.dataset.driverUnloadOneTap = isUnloadingWait ? "true" : "false";
        }
        if (workDial) {
            workDial.classList.toggle("is-waiting-operation", isWaiting);
            workDial.classList.toggle("is-waiting-loading", isLoadingWait);
            workDial.classList.toggle("is-waiting-unload", isUnloadingWait);
        }
        if (workDialControl) {
            workDialControl.classList.toggle("is-waiting-operation", isWaiting);
            workDialControl.classList.toggle("is-waiting-loading", isLoadingWait);
            workDialControl.classList.toggle("is-waiting-unload", isUnloadingWait);
        }
        var note = workDialControl ? workDialControl.querySelector(".driver-work-note") : null;
        if (note) {
            note.textContent = isWaiting
                ? String(payload.reason_label || payload.reason || "Ожидание").toLocaleUpperCase("ru-RU")
                : (holdForm ? "ТОЧКА РАЗГРУЗКИ" : "НА ЗАГРУЗКУ");
        }
        return isWaiting;
    }

    function applyDriverActiveDowntime(payload) {
        if (!downtimeCard || !payload || !payload.event_id) {
            return false;
        }
        downtimeCard.dataset.driverActiveDowntimeId = payload.event_id;
        downtimeCard.dataset.driverActiveReasonId = String(payload.reason_id || "");
        downtimeCard.dataset.driverActiveDowntimeFlow = payload.workflow || "";
        downtimeCard.dataset.driverActiveStartedAt = payload.started_at || "";
        downtimeCard.dataset.driverActiveElapsedSeconds = String(payload.elapsed_seconds || 0);
        downtimeCard.dataset.driverShiftDowntimeSeconds = String(payload.shift_total_seconds || 0);
        downtimeCard.dataset.driverDowntimeCalculatedAt = payload.calculated_at || "";
        downtimeCard.classList.add("is-active");
        setDriverDowntimeStatusClass(payload.status_key || "red");
        if (downtimeTitle) downtimeTitle.textContent = "Активный простой";
        if (downtimeReason) downtimeReason.textContent = payload.reason || "";
        startDriverDowntimeTimer(payload);
        if (downtimeClose) {
            downtimeClose.disabled = false;
            downtimeClose.classList.remove("is-disabled");
            downtimeClose.removeAttribute("aria-disabled");
        }
        return applyDriverWaitingMode(payload);
    }

    function clearDriverActiveDowntime(payload) {
        clearDriverDowntimeTimer();
        if (downtimeTitle) downtimeTitle.textContent = "Простоя нет";
        if (downtimeReason) downtimeReason.textContent = "Выберите причину для начала";
        var shiftTotalSeconds = Math.max(0, Number(payload && payload.shift_total_seconds) || Number(downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds) || 0);
        if (downtimeDuration) downtimeDuration.textContent = (payload && payload.shift_total_label) || formatDriverDowntimeDuration(shiftTotalSeconds);
        if (downtimeCard) {
            downtimeCard.dataset.driverActiveDowntimeId = "";
            downtimeCard.dataset.driverActiveReasonId = "";
            downtimeCard.dataset.driverActiveDowntimeFlow = "";
            downtimeCard.dataset.driverActiveStartedAt = "";
            downtimeCard.dataset.driverActiveElapsedSeconds = String((payload && payload.elapsed_seconds) || 0);
            downtimeCard.dataset.driverShiftDowntimeSeconds = String(shiftTotalSeconds);
            downtimeCard.dataset.driverDowntimeCalculatedAt = String((payload && payload.calculated_at) || downtimeCard.dataset.driverDowntimeCalculatedAt || "");
            downtimeCard.classList.remove("is-active");
            setDriverDowntimeStatusClass("yellow");
        }
        syncDriverReasonTotals(Object.assign({}, payload || {}, { active: false }));
        if (downtimeClose) {
            downtimeClose.disabled = true;
            downtimeClose.classList.add("is-disabled");
            downtimeClose.setAttribute("aria-disabled", "true");
        }
        downtimeReasonButtons.forEach(function (button) {
            button.classList.remove("is-selected");
        });
        applyDriverWaitingMode({ workflow: "" });
    }

    function postDriverDowntimeAction(payload) {
        payload = payload || {};
        var isClose = payload.action === "close";
        var occurredAt = new Date().toISOString();
        snapshotDriverDowntimeTimer(Date.parse(occurredAt));
        var projectionSnapshot = driverDowntimeProjectionSnapshot();
        if (!isClose) {
            projectionSnapshot.active_elapsed_seconds = 0;
        }
        var context = driverOfflineContext();
        if (!context.shiftId || !context.equipmentId) {
            return Promise.reject(new Error("Нет подтверждённой смены или самосвала для сохранения простоя."));
        }
        if (!isClose && Number(payload.reason_id) <= 0) {
            return Promise.reject(new Error("Не выбрана причина простоя."));
        }
        return driverOfflineOutbox.pending().then(function (events) {
            function isSameDowntimeContext(event) {
                return Number(event && event.shift_id) === Number(context.shiftId)
                    && Number(event && event.equipment_id) === Number(context.equipmentId);
            }
            var latestPendingDowntime = events.slice().reverse().find(function (event) {
                return (event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended")
                    && event.state === "pending"
                    && isSameDowntimeContext(event);
            });
            var unresolvedStart = events.slice().reverse().find(function (event) {
                return event.event_type === "driver.downtime.started"
                    && event.state === "pending"
                    && isSameDowntimeContext(event);
            });
            var reasonButton = !isClose
                ? shell.querySelector('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + String(payload.reason_id || "") + '"]')
                : null;
            var activeDowntimeId = String(downtimeCard && downtimeCard.dataset.driverActiveDowntimeId || "");
            var localStartId = unresolvedStart ? unresolvedStart.event_id
                : (activeDowntimeId.indexOf("local:") === 0 ? activeDowntimeId.slice(6) : "");
            var mappingPromise = isClose && localStartId && !unresolvedStart
                ? driverOfflineOutbox.getServerMapping(localStartId)
                : Promise.resolve(null);
            return mappingPromise.then(function (mapping) {
                var mappedServerId = Number(mapping && (mapping.downtime_event_id || mapping.downtime_id)) || null;
                var directServerId = activeDowntimeId.indexOf("local:") === 0 ? null : Number(activeDowntimeId) || null;
                if (isClose && !unresolvedStart && !mappedServerId && !directServerId) {
                    throw new Error("Начало простоя ещё не подтверждено. Дождитесь синхронизации или сверки.");
                }
                if (isClose) {
                    if (typeof window.createDriverDowntimeEndEvent !== "function") {
                        throw new Error("offline_runtime_unavailable");
                    }
                    return driverOfflineOutbox.enqueue(window.createDriverDowntimeEndEvent({
                        eventId: payload.client_action_id,
                        occurredAt: occurredAt,
                        pendingStartId: unresolvedStart ? unresolvedStart.event_id : null,
                        serverId: mappedServerId || directServerId,
                        contextSnapshot: {downtime_projection: projectionSnapshot}
                    }));
                }
                return driverOfflineOutbox.enqueue({
                    event_id: payload.client_action_id,
                    event_type: "driver.downtime.started",
                    occurred_at: occurredAt,
                    depends_on: latestPendingDowntime ? [latestPendingDowntime.event_id] : [],
                    context_snapshot: {downtime_projection: projectionSnapshot},
                    payload: {reason_id: Number(payload.reason_id)}
                });
            }).then(function (event) {
                driverOfflineOutbox.flush().catch(function () {});
                if (isClose) {
                    return {
                        ok: true,
                        active: false,
                        closed: true,
                        elapsed_seconds: downtimeCard && downtimeCard.dataset.driverActiveElapsedSeconds || 0,
                        shift_total_seconds: downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds || 0
                    };
                }
                return {
                    ok: true,
                    active: true,
                    event_id: "local:" + event.event_id,
                    reason_id: payload.reason_id,
                    reason: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                    reason_label: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                    workflow: reasonButton && reasonButton.dataset.driverDowntimeFlow || "",
                    status_key: reasonButton && reasonButton.dataset.driverStatusKey || "yellow",
                    started_at: occurredAt,
                    elapsed_seconds: 0,
                    shift_total_seconds: downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds || 0,
                    calculated_at: occurredAt
                };
            });
        });
    }

    function registerDriverDowntimeAction(button, onComplete) {
        if (!button || typeof onComplete !== "function") return;
        var pending = false;
        button.addEventListener("click", function (event) {
            event.preventDefault();
            if (button.getAttribute("aria-disabled") === "true") {
                showDriverToast(button.dataset.driverUnavailableMessage || "Действие недоступно");
                return;
            }
            if (
                button.disabled
                || pending
                || driverRoleIsReadonly()
            ) return;
            pending = true;
            button.classList.add("is-pending");
            var actionResult;
            try {
                actionResult = onComplete();
            } catch (error) {
                actionResult = Promise.reject(error);
            }
            Promise.resolve(actionResult).catch(function (error) {
                showDriverToast(error && error.message ? error.message : "Действие не выполнено");
            }).finally(function () {
                pending = false;
                button.classList.remove("is-pending");
            });
        });
    }

    var downtimeRefresh = shell.querySelector("[data-driver-downtime-refresh]");
    if (downtimeRefresh) {
        downtimeRefresh.addEventListener("click", function () { window.location.reload(); });
    }

    downtimeReasonButtons.forEach(function (button) {
        registerDriverDowntimeAction(button, function () {
            if (button.disabled) return;
            if (
                downtimeCard
                && downtimeCard.dataset.driverActiveDowntimeId
                && String(downtimeCard.dataset.driverActiveReasonId || "") === String(button.dataset.driverDowntimeReasonId || "")
            ) {
                return;
            }
            downtimeReasonButtons.forEach(function (item) {
                item.classList.remove("is-selected");
            });
            button.classList.add("is-selected");
            button.disabled = true;
            return postDriverDowntimeAction({
                action: "start",
                reason_id: button.dataset.driverDowntimeReasonId,
                client_action_id: generateClientActionId("driver-downtime")
            }).then(function (payload) {
                playDriverVoice("action_ok", "voice_downtime_started");
                if (applyDriverActiveDowntime(payload)) {
                    openDriverTab("work");
                }
                if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                    window.AppRealtime.wake("driver_downtime_saved");
                }
            }).catch(function (error) {
                playDriverVoice("action_error", "voice_action_failed");
                showDriverToast(error.message || "Простой не сохранен");
                button.classList.remove("is-selected");
            }).finally(function () {
                button.disabled = false;
            });
        });
    });

    if (downtimeClose) {
        registerDriverDowntimeAction(downtimeClose, function () {
            if (downtimeClose.disabled || downtimeClose.getAttribute("aria-disabled") === "true") {
                return;
            }
            if (!downtimeCard || !downtimeCard.dataset.driverActiveDowntimeId) {
                clearDriverActiveDowntime();
                return;
            }
            downtimeClose.disabled = true;
            downtimeClose.classList.add("is-pending");
            return postDriverDowntimeAction({
                action: "close",
                client_action_id: generateClientActionId("driver-downtime-close")
            }).then(function (payload) {
                playDriverVoice("action_ok", "voice_downtime_finished");
                clearDriverActiveDowntime(payload);
                if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                    window.AppRealtime.wake("driver_downtime_closed");
                }
            }).catch(function (error) {
                playDriverVoice("action_error", "voice_action_failed");
                showDriverToast(error.message || "Простой не завершен");
                downtimeClose.disabled = false;
                downtimeClose.classList.remove("is-disabled");
                downtimeClose.removeAttribute("aria-disabled");
            }).finally(function () {
                downtimeClose.classList.remove("is-pending");
            });
        });
    }

    if (downtimeCard && downtimeCard.dataset.driverActiveDowntimeId && downtimeCard.dataset.driverActiveStartedAt) {
        startDriverDowntimeTimer({
            active: true,
            event_id: downtimeCard.dataset.driverActiveDowntimeId,
            reason_id: downtimeCard.dataset.driverActiveReasonId,
            started_at: downtimeCard.dataset.driverActiveStartedAt,
            elapsed_seconds: downtimeCard.dataset.driverActiveElapsedSeconds,
            shift_total_seconds: downtimeCard.dataset.driverShiftDowntimeSeconds,
            calculated_at: downtimeCard.dataset.driverDowntimeCalculatedAt
        });
    } else if (downtimeDuration) {
        downtimeDuration.textContent = formatDriverDowntimeDuration(downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds);
        syncDriverReasonTotals({ active: false });
        clearDriverDowntimeTimer();
    }

    var unloadHoldGuard = null;
    var unloadSubmissionPending = false;
    var unloadRecoveryStorage = null;
    try {
        unloadRecoveryStorage = window.sessionStorage;
    } catch (error) {}
    var unloadRecovery = window.createDriverUnloadRecovery({
        storage: unloadRecoveryStorage,
        tripId: holdForm ? holdForm.dataset.driverTripId : "",
        input: holdForm ? holdForm.querySelector("[data-driver-client-action]") : null,
        generateActionId: function () {
            return generateClientActionId("trip-unloaded");
        },
        onRecover: function () {
            unloadSubmissionPending = false;
            if (holdForm) {
                delete holdForm.dataset.driverUnloadSubmitting;
            }
            if (unloadHoldGuard) {
                unloadHoldGuard.cancel();
            }
            if (holdButton && !driverRoleIsReadonly()) {
                holdButton.disabled = false;
            }
        }
    });
    window.driverUnloadRecovery = unloadRecovery;

    if (holdForm && holdButton) {
        var dialLabel = holdButton.querySelector("[data-driver-dial-label]");
        var readyDialLabel = dialLabel
            ? (dialLabel.dataset.driverDialRaw || dialLabel.textContent.trim().replace(/\s+/g, " "))
            : "";
        function submitDriverUnloadOnce() {
            if (
                unloadSubmissionPending
                || holdForm.dataset.driverUnloadSubmitting === "true"
                || holdButton.disabled
                || driverRoleIsReadonly()
            ) {
                return false;
            }
            if (!unloadRecovery.ensureActionId()) {
                showDriverToast("Не удалось подготовить разгрузку. Повторите нажатие");
                return false;
            }
            unloadSubmissionPending = true;
            holdForm.dataset.driverUnloadSubmitting = "true";
            holdForm.dataset.holdComplete = "true";
            holdButton.classList.remove("is-loaded", "is-holding");
            holdButton.classList.add("is-pending");
            if (dialLabel) {
                renderDriverDialLabel(
                    dialLabel,
                    holdButton.dataset.driverPendingLabel || "ОТПРАВКА"
                );
                scheduleDriverDialLabelFit();
            }
            holdButton.disabled = true;
            var actionId = holdForm.querySelector("[data-driver-client-action]").value;
            var unloadTripId = String(holdForm.dataset.driverTripId || "");
            var unloadContext = driverOfflineContext();
            if (!unloadTripId || shell.dataset.driverHasLoadedTrip !== "true" || !unloadContext.shiftId || !unloadContext.equipmentId) {
                unloadRecovery.recover({type: "local_guard_failed"});
                holdButton.classList.remove("is-pending");
                showDriverToast("Нет подтверждённого загруженного рейса для разгрузки.");
                return false;
            }
            driverOfflineOutbox.pending().then(function (events) {
                var pendingPoint = events.slice().reverse().find(function (event) {
                    return event.event_type === "driver.trip.dump_point_changed"
                        && String(event.trip_id || "") === unloadTripId;
                });
                return driverOfflineOutbox.enqueue({
                    event_id: actionId,
                    event_type: "driver.trip.unloaded",
                    trip_id: unloadTripId,
                    depends_on: pendingPoint ? [pendingPoint.event_id] : [],
                    payload: {trip_id: Number(unloadTripId)}
                });
            }).then(function (savedEvent) {
                unloadRecovery.recover({type: "queued"});
                applyDriverOfflineProjection(shell, driverOfflineEvents);
                /* The state projection owns the visible result: after a durable
                   local save the dial immediately becomes the quiet inactive
                   instrument.  No completion animation may imply server sync. */
                showDriverToast("Разгрузка сохранена на телефоне.");
                /* Delivery is best-effort. A flush error must never turn a successful
                   durable enqueue into a false "save failed" message or restore the trip. */
                try {
                    var flushPromise = driverOfflineOutbox.flush();
                    if (flushPromise && typeof flushPromise.catch === "function") {
                        flushPromise.catch(function () {});
                    }
                } catch (flushError) {}
            }).catch(function () {
                unloadRecovery.recover({type: "storage_failed"});
                holdButton.classList.remove("is-pending");
                showDriverToast("Не удалось сохранить разгрузку на телефоне. Повторите действие.");
            });
            return true;
        }
        /* Кольцо удержания набирается секциями между делениями (12 штук за holdMs).
           Каждая секция — короткий отклик, заполненное кольцо — длинный. Отклики идут
           по таймеру, а не по кадрам: ни одного лишнего пересчёта во время удержания.
           Если в системных настройках телефона выключен виброотклик при касании,
           Android глушит эти вызовы (в dumpsys они видны со scale 0). */
        var HOLD_SEGMENTS = 12;
        var holdSegmentTimer = null;
        function driverVibrate(pattern) {
            if (!window.navigator || typeof window.navigator.vibrate !== "function") return;
            try { window.navigator.vibrate(pattern); } catch (error) {}
        }
        function stopHoldSegmentFeedback() {
            if (holdSegmentTimer !== null) {
                window.clearInterval(holdSegmentTimer);
                holdSegmentTimer = null;
            }
        }
        function startHoldSegmentFeedback(totalMs) {
            stopHoldSegmentFeedback();
            var fired = 0;
            holdSegmentTimer = window.setInterval(function () {
                fired += 1;
                // Последнюю границу не отбиваем: там срабатывает длинный отклик завершения.
                if (fired >= HOLD_SEGMENTS) { stopHoldSegmentFeedback(); return; }
                driverVibrate(14);
            }, totalMs / HOLD_SEGMENTS);
        }
        unloadHoldGuard = window.createDriverRoleHoldGuard({
            /* Разгрузка повторяется десятки раз за смену: ровно секунда — достаточно,
               чтобы случайное касание не отправило рейс, и не утомляет за смену. */
            holdMs: 1000,
            onStart: function () {
                holdButton.classList.add("is-holding");
                startHoldSegmentFeedback(1000);
            },
            onReset: function () {
                stopHoldSegmentFeedback();
                driverVibrate(0);
                delete holdForm.dataset.holdComplete;
                holdButton.classList.remove("is-holding", "is-pending");
                holdButton.classList.add("is-loaded");
                // После обычного отпускания подпись и так исходная — подгонка текста
                // (замеры ширины в цикле) на слабом телефоне стоила заметного кадра.
                if (dialLabel && readyDialLabel && (dialLabel.dataset.driverDialRaw || dialLabel.textContent.trim().replace(/\s+/g, " ")) !== readyDialLabel) {
                    renderDriverDialLabel(dialLabel, readyDialLabel);
                    scheduleDriverDialLabelFit();
                }
            },
            onComplete: function () {
                stopHoldSegmentFeedback();
                driverVibrate(140);   // кольцо заполнено
                if (!submitDriverUnloadOnce()) {
                    unloadHoldGuard.cancel();
                }
            }
        });
        window.driverUnloadHoldGuard = unloadHoldGuard;
        window.driverUnloadGesture = window.bindDriverUnloadGesture({
            form: holdForm,
            button: holdButton,
            holdGuard: unloadHoldGuard,
            canTrigger: function () {
                return !unloadSubmissionPending && !driverRoleIsReadonly();
            },
            onOneTap: function () {
                return submitDriverUnloadOnce();
            }
        });
    }

    var pointSheet = shell.querySelector("[data-driver-point-sheet]");
    var pointOpen = shell.querySelector("[data-driver-point-open]");
    var pointSheetReturnFocus = null;
    function setPointSheet(open) {
        if (!pointSheet) {
            return;
        }
        pointSheet.hidden = !open;
        shell.classList.toggle("is-point-sheet-open", open);
        if (pointOpen) pointOpen.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
            pointSheetReturnFocus = document.activeElement;
            window.requestAnimationFrame(function () {
                var focusTarget = pointSheet.querySelector(".driver-unload-tile.is-current, [data-driver-point-close]");
                if (focusTarget) focusTarget.focus();
            });
        } else if (pointSheetReturnFocus && typeof pointSheetReturnFocus.focus === "function") {
            pointSheetReturnFocus.focus();
            pointSheetReturnFocus = null;
        }
    }
    if (pointOpen && pointSheet) {
        pointOpen.addEventListener("click", function () {
            setPointSheet(true);
        });
        pointOpen.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                setPointSheet(true);
            }
        });
        pointSheet.querySelectorAll("[data-driver-point-close]").forEach(function (button) {
            button.addEventListener("click", function () {
                setPointSheet(false);
            });
        });
        pointSheet.addEventListener("click", function (event) {
            if (event.target === pointSheet) setPointSheet(false);
        });
        pointSheet.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                event.preventDefault();
                setPointSheet(false);
            }
        });
        pointSheet.querySelectorAll("form").forEach(function (form) {
            form.dataset.driverOfflineManaged = "true";
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                event.stopImmediatePropagation();
                if (form.dataset.driverOfflinePending === "true") return;
                var action = form.querySelector("[data-driver-client-action]");
                var point = form.querySelector('[name="dump_point"]');
                var button = form.querySelector(".driver-unload-tile");
                var tripId = String(shell.dataset.driverActiveTripId || "");
                var pointId = Number(point && point.value);
                var pointName = button && button.dataset.driverPointName || "";
                if (!tripId || !pointId || shell.dataset.driverHasOpenTrip !== "true") {
                    showDriverToast("Нет подтверждённого рейса или точки разгрузки.");
                    return;
                }
                if (String(pointId) === String(shell.dataset.driverActualDumpPointId || "")) {
                    setPointSheet(false);
                    showDriverToast("Эта точка уже выбрана.");
                    return;
                }
                form.dataset.driverOfflinePending = "true";
                driverOfflineOutbox.pending().then(function (events) {
                    if (typeof window.createDriverPointChangeEvent !== "function") {
                        throw new Error("offline_runtime_unavailable");
                    }
                    var change = window.createDriverPointChangeEvent({
                        tripId: tripId,
                        pointId: pointId,
                        currentPointId: shell.dataset.driverActualDumpPointId,
                        events: events
                    });
                    if (action) action.value = change.event_id;
                    return driverOfflineOutbox.enqueue(change);
                }).then(function () {
                    applyDriverPointSelection(shell, pointId, pointName, "local");
                    setPointSheet(false);
                    showDriverToast("Точка разгрузки сохранена на телефоне.");
                    return driverOfflineOutbox.flush();
                }).catch(function () {
                    showDriverToast("Не удалось сохранить точку на телефоне. Повторите действие.");
                }).finally(function () {
                    form.dataset.driverOfflinePending = "false";
                });
            });
        });
    }

    /* Открытие/закрытие смены, принятие назначения и смена
       точки разгрузки раньше перезагружали весь WebView. Серверные
       POST-обработчики не меняем: забираем их готовый HTML и заменяем
       только рабочий shell. Обычная отправка остаётся аварийным fallback. */
    shell.querySelectorAll("form[data-driver-in-place]").forEach(function (form) {
        if (form.dataset.driverOfflineManaged === "true") return;
        if (form.dataset.driverInPlaceBound === "true") return;
        form.dataset.driverInPlaceBound = "true";
        form.addEventListener("submit", function (event) {
            if (event.defaultPrevented || form.dataset.driverInPlacePending === "true") {
                return;
            }
            event.preventDefault();
            form.dataset.driverInPlacePending = "true";
            var actionInput = form.querySelector("[data-driver-client-action], [name='client_action_id']");
            if (actionInput && !actionInput.value) {
                actionInput.value = generateClientActionId(form.dataset.driverInPlace || "driver-action");
            }
            var submitPromise = form.dataset.driverInPlace === "shift-close"
                ? window.DriverShiftCloseOutbox.submit(form)
                : window.submitDriverFormInPlace(form, {fallbackToNavigation: false});
            Promise.resolve(submitPromise).then(function (applied) {
                if (!applied && form.isConnected) {
                    form.dataset.driverInPlacePending = "false";
                    form.dataset.driverShiftOpeningPending = "false";
                    var openButton = form.querySelector("[data-driver-shift-open-button]");
                    if (openButton) {
                        openButton.disabled = false;
                        openButton.classList.remove("is-pending");
                        openButton.textContent = "Начать смену";
                    }
                    var closeButton = form.querySelector("[data-driver-shift-close-button]");
                    if (closeButton) {
                        closeButton.disabled = false;
                        closeButton.classList.remove("is-pending");
                        var closeLabel = closeButton.querySelector("[data-mobile-shift-label]");
                        if (closeLabel) closeLabel.textContent = "Закрыть смену";
                    }
                }
            }).catch(function (error) {
                if (form.isConnected) {
                    form.dataset.driverInPlacePending = "false";
                    var closeButton = form.querySelector("[data-driver-shift-close-button]");
                    if (closeButton) {
                        closeButton.disabled = false;
                        closeButton.classList.remove("is-pending");
                        var closeLabel = closeButton.querySelector("[data-mobile-shift-label]");
                        if (closeLabel) closeLabel.textContent = "Закрыть смену";
                    }
                }
                if (typeof window.showDriverToast === "function") {
                    window.showDriverToast(error && error.message ? error.message : "Не удалось сохранить действие.");
                }
            });
        });
    });
    window.DriverShiftCloseOutbox.restore(
        shell.querySelector("[data-driver-shift-close-form]")
    );

    (function initDriverPwaUpdates() {
        if (!("serviceWorker" in navigator)) {
            return;
        }
        var runtime = window.__driverPwaUpdateRuntime;
        var currentShellVersion = runtime && runtime.currentShellVersion
            ? runtime.currentShellVersion
            : shell.dataset.driverPwaVersion || "";
        var updateModal = document.querySelector("[data-driver-pwa-update-modal]");
        var updateBadge = document.querySelector("[data-driver-pwa-update-badge]");
        var updateTarget = document.querySelector("[data-driver-pwa-update-nav-target]");
        var statusNode = document.querySelector("[data-driver-pwa-update-status]");
        var currentVersionNode = document.querySelector("[data-driver-pwa-current-version]");
        var newVersionNode = document.querySelector("[data-driver-pwa-new-version]");
        var applyButton = document.querySelector("[data-driver-pwa-update-apply]");
        var laterButton = document.querySelector("[data-driver-pwa-update-later]");

        function formatVersion(version) {
            var match = String(version || "").match(/driver-mobile-shell-v(\d+)/);
            return match ? "v" + match[1] : String(version || "v1");
        }
        function versionNumber(version) {
            var match = String(version || "").match(/driver-mobile-shell-v(\d+)/);
            return match ? parseInt(match[1], 10) : 0;
        }
        function setBadge(visible) {
            if (updateBadge) {
                updateBadge.hidden = !visible;
            }
            if (updateTarget) {
                updateTarget.classList.toggle("has-update", visible);
            }
        }
        function setStatus(text) {
            if (statusNode) {
                statusNode.textContent = text;
            }
        }
        function showUpdate(nextVersion) {
            setBadge(true);
            if (currentVersionNode) {
                currentVersionNode.textContent = formatVersion(currentShellVersion);
            }
            if (newVersionNode) {
                newVersionNode.textContent = formatVersion(nextVersion);
            }
            setStatus("Можно установить новую версию экрана водителя.");
        }
        function revealModal() {
            if (updateModal) {
                updateModal.hidden = false;
            }
        }
        function hideModal() {
            if (updateModal) {
                updateModal.hidden = true;
            }
        }

        function releaseVerifiedPageFromStaleWorkerLock(detail) {
            var guard = window.AppPwaContractGuard;
            var body = document.body;
            var server = detail && detail.server;
            var worker = detail && detail.serviceWorker;
            var expectedContractVersion = body && String(body.dataset.appContractVersion || "");
            var expectedShellVersion = body && String(body.dataset.appShellVersion || "");
            var expectedRoleCode = body && String(body.dataset.appRoleCode || "");
            var workerMatches = worker
                && worker.appContractVersion === expectedContractVersion
                && worker.shellVersion === expectedShellVersion
                && worker.roleCode === expectedRoleCode;
            if (
                !guard
                || typeof guard.acceptServiceWorkerVersion !== "function"
                || !detail
                || !detail.locked
                || workerMatches
                || !expectedContractVersion
                || expectedShellVersion !== String(currentShellVersion || "")
                || expectedRoleCode !== "driver"
                || detail.javascriptVersion !== expectedContractVersion
                || !server
                || server.appContractVersion !== expectedContractVersion
                || server.shellVersion !== expectedShellVersion
                || server.roleCode !== expectedRoleCode
            ) {
                return false;
            }
            guard.acceptServiceWorkerVersion({
                appContractVersion: expectedContractVersion,
                shellVersion: expectedShellVersion,
                roleCode: expectedRoleCode
            });
            return true;
        }

        if (!runtime) {
            runtime = {
                registration: null,
                registrationPromise: null,
                waitingWorker: null,
                activationRequestedWorker: null,
                currentShellVersion: currentShellVersion,
                renderUpdate: null,
                clearUpdate: null,
                applyPromise: null,
                controllerRecoveryTimer: 0
            };
            runtime.requestWorkerVersion = function (worker) {
                if (!worker || !worker.postMessage || !window.MessageChannel) {
                    return Promise.resolve("");
                }
                return new Promise(function (resolve) {
                    var channel = new MessageChannel();
                    var timeout = window.setTimeout(function () {
                        resolve("");
                    }, 1200);
                    channel.port1.onmessage = function (event) {
                        window.clearTimeout(timeout);
                        resolve(event.data && event.data.version ? event.data.version : "");
                    };
                    worker.postMessage({type: "GET_VERSION"}, [channel.port2]);
                });
            };
            runtime.scheduleControllerRecovery = function (targetVersion, delay) {
                if (!targetVersion || runtime.controllerRecoveryTimer) {
                    return;
                }
                runtime.controllerRecoveryTimer = window.setTimeout(function () {
                    runtime.controllerRecoveryTimer = 0;
                    runtime.requestWorkerVersion(navigator.serviceWorker.controller).then(function (controllerVersion) {
                        if (String(controllerVersion || "") === String(targetVersion || "")) {
                            return;
                        }
                        var guard = window.AppPwaContractGuard;
                        if (
                            guard
                            && typeof guard.hasUnsafeWorkInProgress === "function"
                            && guard.hasUnsafeWorkInProgress()
                        ) {
                            return;
                        }
                        var reloadKey = "driver-pwa-controller-reload:" + String(targetVersion || "");
                        try {
                            if (window.sessionStorage.getItem(reloadKey) === "1") {
                                return;
                            }
                            window.sessionStorage.setItem(reloadKey, "1");
                        } catch (error) {
                            return;
                        }
                        window.location.reload();
                    });
                }, Number(delay) >= 0 ? Number(delay) : 1200);
            };
            runtime.activateMatchingWaitingWorker = function (registration, worker, waitingVersion) {
                if (!worker || String(waitingVersion || "") !== String(runtime.currentShellVersion || "")) {
                    return false;
                }
                if (registration && registration.waiting && registration.waiting !== worker) {
                    return false;
                }
                var guard = window.AppPwaContractGuard;
                var guardState = guard && typeof guard.getState === "function"
                    ? guard.getState()
                    : null;
                if (!guardState || !guardState.locked) {
                    return false;
                }
                if (
                    guard
                    && typeof guard.hasUnsafeWorkInProgress === "function"
                    && guard.hasUnsafeWorkInProgress()
                ) {
                    return false;
                }
                if (runtime.activationRequestedWorker === worker) {
                    return true;
                }
                runtime.activationRequestedWorker = worker;
                runtime.waitingWorker = null;
                setBadge(false);
                hideModal();
                worker.postMessage({type: "SKIP_WAITING"});
                runtime.scheduleControllerRecovery(waitingVersion, 1200);
                return true;
            };
            runtime.renderWaitingUpdate = function (registration, worker) {
                var waitingWorker = worker || (registration && registration.waiting);
                if (!waitingWorker) return Promise.resolve();
                if (
                    (registration && registration.waiting !== waitingWorker)
                    || runtime.activationRequestedWorker === waitingWorker
                ) {
                    return Promise.resolve();
                }
                var activeWorker = (registration && registration.active)
                    || navigator.serviceWorker.controller;
                return Promise.all([
                    runtime.requestWorkerVersion(activeWorker),
                    runtime.requestWorkerVersion(waitingWorker)
                ]).then(function (versions) {
                    if (
                        (registration && registration.waiting !== waitingWorker)
                        || runtime.activationRequestedWorker === waitingWorker
                    ) {
                        return;
                    }
                    if (runtime.activateMatchingWaitingWorker(
                        registration,
                        waitingWorker,
                        versions[1]
                    )) {
                        return;
                    }
                    if (runtime.renderUpdate) {
                        runtime.renderUpdate(versions[1], versions[0]);
                    }
                });
            };
            runtime.watchRegistration = function (registration) {
                if (!registration || registration.__driverPwaRuntimeBound) return;
                registration.__driverPwaRuntimeBound = true;
                if (registration.waiting) {
                    runtime.waitingWorker = registration.waiting;
                    runtime.renderWaitingUpdate(registration, registration.waiting);
                }
                registration.addEventListener("updatefound", function () {
                    var worker = registration.installing;
                    if (!worker || !worker.addEventListener) return;
                    worker.addEventListener("statechange", function () {
                        if (worker.state !== "installed" || !navigator.serviceWorker.controller) return;
                        runtime.waitingWorker = registration.waiting || worker;
                        runtime.renderWaitingUpdate(registration, runtime.waitingWorker);
                    });
                });
            };
            runtime.ensureRegistration = function () {
                if (runtime.registration) return Promise.resolve(runtime.registration);
                if (runtime.registrationPromise) return runtime.registrationPromise;
                var guard = window.AppPwaContractGuard;
                var source = guard && typeof guard.getRegistration === "function"
                    ? guard.getRegistration()
                    : navigator.serviceWorker.getRegistration(shell.dataset.driverSwScope);
                runtime.registrationPromise = Promise.resolve(source).then(function (registration) {
                    runtime.registration = registration || null;
                    runtime.watchRegistration(runtime.registration);
                    return runtime.registration;
                }).catch(function () {
                    return null;
                });
                return runtime.registrationPromise;
            };
            runtime.requestManualUpdate = function () {
                if (runtime.applyPromise) return runtime.applyPromise;
                var guard = window.AppPwaContractGuard;
                var update = guard && typeof guard.requestManualUpdate === "function"
                    ? guard.requestManualUpdate()
                    : runtime.ensureRegistration().then(function (registration) {
                        if (!registration || !registration.update) {
                            return {status: "unavailable", registration: registration || null};
                        }
                        return Promise.resolve(registration.update()).then(function () {
                            return {
                                status: registration.waiting ? "update-ready" : "current",
                                registration: registration
                            };
                        });
                    });
                runtime.applyPromise = Promise.resolve(update).then(function (result) {
                    var registration = result && result.registration
                        ? result.registration
                        : runtime.registration;
                    var worker = registration
                        ? registration.waiting
                        : runtime.waitingWorker;
                    if (worker && runtime.activationRequestedWorker !== worker) {
                        runtime.activationRequestedWorker = worker;
                        runtime.waitingWorker = null;
                        setBadge(false);
                        hideModal();
                        worker.postMessage({type: "SKIP_WAITING"});
                        return result;
                    }
                    if (result && result.status === "unavailable") {
                        setStatus(
                            "Служба обновления недоступна. Закройте и снова откройте приложение."
                        );
                    } else if (result && result.status === "error") {
                        setStatus("Не удалось проверить обновление. Попробуйте еще раз.");
                    } else {
                        setStatus("Установлена актуальная версия.");
                    }
                    return result;
                }).finally(function () {
                    runtime.applyPromise = null;
                });
                return runtime.applyPromise;
            };
            window.addEventListener("app-pwa-contract-state", function (event) {
                var detail = event && event.detail ? event.detail : {};
                if (releaseVerifiedPageFromStaleWorkerLock(detail)) {
                    return;
                }
                var serverVersion = detail.server && detail.server.shellVersion;
                if (
                    serverVersion
                    && versionNumber(serverVersion) > versionNumber(runtime.currentShellVersion)
                ) {
                    if (runtime.renderUpdate) runtime.renderUpdate(serverVersion);
                } else if (detail.ready && runtime.clearUpdate) {
                    runtime.clearUpdate();
                    runtime.scheduleControllerRecovery(runtime.currentShellVersion, 150);
                }
            });
            window.__driverPwaUpdateRuntime = runtime;
        }

        runtime.renderUpdate = function (nextVersion, baselineVersion) {
            var next = versionNumber(nextVersion);
            var loaded = versionNumber(currentShellVersion);
            var baseline = versionNumber(baselineVersion || currentShellVersion);
            if (
                next > baseline
                && (!baselineVersion || next >= loaded)
            ) {
                showUpdate(nextVersion);
            }
        };
        runtime.clearUpdate = function () {
            setBadge(false);
        };
        runtime.ensureRegistration().then(function (registration) {
            var guardState = window.AppPwaContractGuard
                && typeof window.AppPwaContractGuard.getState === "function"
                ? window.AppPwaContractGuard.getState()
                : null;
            var serverVersion = guardState && guardState.server
                ? guardState.server.shellVersion
                : "";
            if (versionNumber(serverVersion) > versionNumber(currentShellVersion)) {
                showUpdate(serverVersion);
            } else if (registration && registration.waiting) {
                runtime.waitingWorker = registration.waiting;
                runtime.renderWaitingUpdate(registration, registration.waiting);
            }
            if (applyButton && applyButton.dataset.driverPwaUpdateBound !== "true") {
                applyButton.dataset.driverPwaUpdateBound = "true";
                applyButton.addEventListener("click", function () {
                    setStatus("Проверяем и устанавливаем обновление...");
                    runtime.requestManualUpdate();
                });
            }
        }).catch(function () {
            setBadge(false);
        });

        if (laterButton && laterButton.dataset.driverPwaUpdateBound !== "true") {
            laterButton.dataset.driverPwaUpdateBound = "true";
            laterButton.addEventListener("click", hideModal);
        }
        if (updateTarget && updateTarget.dataset.driverPwaUpdateBound !== "true") {
            updateTarget.dataset.driverPwaUpdateBound = "true";
            updateTarget.addEventListener("click", function () {
                if (updateBadge && !updateBadge.hidden) {
                    revealModal();
                }
            }, true);
        }
    })();
};
document.addEventListener("DOMContentLoaded", window.bindDriverMobileShell);
