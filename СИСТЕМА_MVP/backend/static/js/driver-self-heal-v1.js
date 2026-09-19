/* Аварийный выход для экрана водителя.

   Наблюдение с боевого (20.09.2026): машинист экскаватора отметил погрузку,
   на телефоне водителя прозвучало голосовое подтверждение, а круг на экране
   «Работа» так и остался пустым; индикатор связи при этом горел синим.

   Причина не в сервере — серверная цепочка (назначение → погрузка → рейс)
   проверена отдельно и отрабатывает верно. Экран водителя подменяет разметку
   только когда это «безопасно»: нет неотправленных действий, начатого жеста,
   открытой шторки, фокуса в поле ввода (isDriverOperationalRefreshUnsafe).
   Если хоть один такой признак залипает, realtime-клиент повторяет попытку
   раз в секунду БЕСКОНЕЧНО: версия сервера остаётся неприменённой, связь
   вечно показывает «Восстанавливаем данные…» (синий), а экран стоит. Голос
   при этом слышен, потому что он приходит отдельным путём и от подмены
   разметки не зависит.

   Настольный пульт лечится от той же болезни своим самовосстановлением в
   base.html (dispatcherBoardSelfHeal), но оно намеренно не распространяется
   на приложения ролей. Здесь то же лекарство для водителя.

   Три правила, от мягкого к жёсткому:
   1) экран отстал от сервера дольше минуты — открыть окно, в котором мягкие
      признаки занятости (залипший жест, шторка, неотправленное действие)
      больше не держат обновление, и попросить сверку заново;
   2) не помогло дольше трёх минут — перезагрузить страницу;
   3) любое из этого делать ТОЛЬКО когда связь с сервером жива и экран на
      виду: в карьере телефон часто вне сети, и перезагрузка там означала бы
      потерю рабочего экрана вместо его починки.

   То, что водитель уже ввёл руками, самовосстановление не трогает: открытая
   и заполняемая форма смены остаётся жёсткой причиной отложить обновление.
   На здоровом экране ни одно условие не наступает, и код молчит. */
(function driverScreenSelfHeal() {
    "use strict";

    var TICK_MS = 5000;
    var FORCE_AFTER_MS = 60000;
    var RELOAD_AFTER_MS = 180000;
    var BYPASS_WINDOW_MS = 20000;
    var FORCE_RETRY_MS = 15000;
    var MIN_RELOAD_INTERVAL_MS = 180000;
    var STORAGE_KEY = "driver-self-heal-reload-at";

    if (!document.querySelector("[data-driver-shell]")) return;

    var behindSince = 0;
    var lastForceAt = 0;
    var noticeShown = false;
    var timerId = null;

    function realtimeState() {
        var client = window.AppRealtime;
        return client && typeof client.getDebugState === "function" ? client.getDebugState() : null;
    }

    /* Отставание — это именно «сервер ушёл вперёд, а экран не принял».
       Неотправленное действие само по себе отставанием не считается. */
    function pendingServerVersion(state) {
        if (!state) return 0;
        var pending = Number(state.pendingVersion === null ? 0 : state.pendingVersion || 0);
        return Number.isFinite(pending) ? pending : 0;
    }

    /* Связь должна быть живой: «recovering» означает, что опрос сервера
       проходит, а разметку принять не удаётся — это наш случай. При weak,
       lost и unknown лечить нечего, там работает обычное переподключение. */
    function transportIsHealthy(state) {
        return !!state && state.connectionState === "recovering";
    }

    function driverIsTypingIntoForm(shell) {
        var opening = shell.querySelector(".driver-shift-opening-form");
        if (opening && (
            opening.dataset.driverShiftOpeningDirty === "true"
            || opening.dataset.driverShiftOpeningPending === "true"
        )) return true;
        var closing = shell.querySelector("[data-driver-shift-close-form]");
        return !!(closing && closing.dataset.driverShiftDirty === "true");
    }

    /* Перезагрузка стирает то, что водитель уже выбрал в открытой шторке, но
       ещё не отправил. Продавливать обновление при открытой шторке можно —
       свежая разметка приходит с сервера с закрытой шторкой и лечит как раз
       случай «шторка залипла открытой». А вот перезагружать — нельзя. */
    function sheetOrModalIsOpen(shell) {
        return !!(
            shell.querySelector("[data-driver-point-sheet]:not([hidden]), [data-driver-free-bucket-sheet]:not([hidden])")
            || document.querySelector("[data-driver-pwa-update-modal]:not([hidden]), .app-confirm-modal:not([hidden])")
        );
    }

    function releaseStuckFocus(shell) {
        var active = document.activeElement;
        if (!active || !shell.contains(active) || typeof active.blur !== "function") return;
        var tag = active.tagName ? active.tagName.toLowerCase() : "";
        if (!active.isContentEditable && tag !== "input" && tag !== "textarea" && tag !== "select") return;
        active.blur();
    }

    function recentlyReloaded() {
        try {
            var at = Number(window.sessionStorage.getItem(STORAGE_KEY) || 0);
            return !!at && Date.now() - at < MIN_RELOAD_INTERVAL_MS;
        } catch (error) {
            return false;
        }
    }

    function forceCatchUp(shell, state) {
        lastForceAt = Date.now();
        releaseStuckFocus(shell);
        window.driverRefreshBypassBusyUntil = Date.now() + BYPASS_WINDOW_MS;
        window.driverForceFragmentApply = true;
        if (!noticeShown && typeof window.showDriverToast === "function") {
            noticeShown = true;
            window.showDriverToast("Экран отстал от сервера — обновляем.");
        }
        var client = window.AppRealtime;
        if (client && typeof client.requestReconcile === "function") {
            client.requestReconcile("driver_self_heal", pendingServerVersion(state));
        } else if (client && typeof client.wake === "function") {
            client.wake("driver_self_heal");
        }
    }

    function reloadScreen() {
        if (recentlyReloaded()) return;
        try {
            window.sessionStorage.setItem(STORAGE_KEY, String(Date.now()));
        } catch (error) {
            // Хранилище необязательно: без него остаётся защита по времени в самом тике.
        }
        window.location.reload();
    }

    function tick() {
        traceFragmentRequests();
        var shell = document.querySelector("[data-driver-shell]");
        if (!shell) return;
        /* Скрытую вкладку обновление откладывает намеренно, и время в фоне
           отставанием не считаем — иначе возвращение к телефону каждый раз
           встречало бы перезагрузкой. */
        if (document.hidden === true) {
            behindSince = 0;
            return;
        }
        var state = realtimeState();
        if (!pendingServerVersion(state) || !transportIsHealthy(state)) {
            behindSince = 0;
            noticeShown = false;
            return;
        }
        var now = Date.now();
        if (!behindSince) {
            behindSince = now;
            return;
        }
        var behindFor = now - behindSince;
        if (behindFor >= RELOAD_AFTER_MS && !driverIsTypingIntoForm(shell) && !sheetOrModalIsOpen(shell)) {
            reloadScreen();
            return;
        }
        if (behindFor >= FORCE_AFTER_MS && now - lastForceAt >= FORCE_RETRY_MS && !driverIsTypingIntoForm(shell)) {
            forceCatchUp(shell, state);
        }
    }

    function start() {
        if (timerId !== null) return;
        timerId = window.setInterval(tick, TICK_MS);
    }

    /* Успешная подмена разметки закрывает счёт отставания сразу, не дожидаясь
       тика: иначе после починки окно обхода оставалось бы открытым. */
    window.addEventListener("operational-state-refresh-applied", function () {
        behindSince = 0;
        noticeShown = false;
        window.driverRefreshBypassBusyUntil = 0;
    });

    /* Причину последнего отказа держим на теле страницы: на боевом телефоне
       это единственный способ потом узнать, что именно держало экран. */
    window.addEventListener("operational-state-refresh-deferred", function (event) {
        var detail = event && event.detail ? event.detail : {};
        if (detail.role && detail.role !== "driver") return;
        if (document.body) {
            document.body.dataset.driverRefreshDeferReason = String(detail.reason || "");
        }
        /* В нативном приложении консоль попадает в logcat — это единственный
           журнал, который можно снять с боевого телефона по USB. */
        if (window.console && typeof window.console.info === "function") {
            window.console.info("driver-refresh-deferred " + String(detail.reason || "") + " v=" + String(detail.version || "") + " busy=" + String(window.driverRefreshBusyReason || ""));
        }
    });

    /* Замер запроса фрагмента: в нативном приложении консоль попадает в logcat,
       и по USB видно, сколько шёл каждый запрос и чем кончился (таймаут 15 с,
       несовпадение версии, HTTP-ошибка). Поведение запроса не меняется. */
    /* AppOperationalFragment объявляется в base.html ПОСЛЕ скриптов экрана, поэтому
       при загрузке этого файла его ещё нет: обёртка ставится при готовности
       realtime-клиента и повторяется из тика, пока не встанет. */
    function traceFragmentRequests() {
        var fragment = window.AppOperationalFragment;
        if (!fragment || typeof fragment.request !== "function" || fragment.__driverTraced) return;
        var original = fragment.request;
        fragment.__driverTraced = true;
        fragment.request = function (screenName, version, options) {
            var startedAt = Date.now();
            var log = function (outcome) {
                if (window.console && typeof window.console.info === "function") {
                    window.console.info("driver-fragment " + outcome + " v=" + String(version || 0) + " ms=" + String(Date.now() - startedAt));
                }
            };
            return original.apply(this, arguments).then(function (payload) {
                log("ok server_v=" + String(payload && payload.version));
                return payload;
            }, function (error) {
                log("fail " + String(error && (error.code || error.status || error.message) || "unknown"));
                throw error;
            });
        };
    }
    traceFragmentRequests();
    window.addEventListener("app-realtime-ready", traceFragmentRequests);

    /* Состав очереди неотправленных действий — в журнал при каждом изменении:
       тип, состояние, число попыток, код последней ошибки, возраст. */
    window.addEventListener("operational-outbox-state", function (event) {
        var detail = event && event.detail ? event.detail : {};
        if (detail.role && detail.role !== "driver") return;
        if (!window.console || typeof window.console.info !== "function") return;
        var events = Array.isArray(window.driverOfflineEvents) ? window.driverOfflineEvents : [];
        /* Сначала ожидающие отправки — именно они держат экран и индикатор,
           а «на сверке» могут лежать днями. */
        var ordered = events.slice().sort(function (a, b) {
            return (a.state === "pending" ? 0 : 1) - (b.state === "pending" ? 0 : 1);
        });
        var summary = ordered.slice(0, 6).map(function (item) {
            var age = Date.parse(item.occurred_at || "") ? Math.round((Date.now() - Date.parse(item.occurred_at)) / 1000) : -1;
            return String(item.event_type || "?") + "/" + String(item.state || "?")
                + "/try" + String(item.attempt_count || 0)
                + "/" + String(item.last_error && item.last_error.code || "-")
                + "/" + String(age) + "s";
        });
        window.console.info("driver-outbox pending=" + String(detail.pendingCount) + " review=" + String(detail.reviewCount) + " [" + summary.join(" ") + "]");
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }

    window.driverSelfHeal = {
        tick: tick,
        state: function () {
            return {behindSince: behindSince, lastForceAt: lastForceAt};
        }
    };
})();
