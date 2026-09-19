/* Применение серверного обновления к экрану водителя и отправка форм без
   перезагрузки страницы. Вынесено из driver-shift-v1.js без изменений. */
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
                /* Пока сервер перезапускается после выкладки, страница по сети ещё
                   старая: перезагрузка на каждый фрагмент давала шторм перезагрузок
                   раз в секунду, а сама разметка (погрузка!) при этом выбрасывалась —
                   экран слеп на всё окно перезапуска (боевой замер 20.09.2026: 14 с).
                   Теперь свежую разметку применяем как обычно, а перезагрузку просим
                   не чаще раза в 30 с: обновлённые стили и скрипты подъедут со
                   следующей загрузкой страницы, разметка между версиями совместима. */
                var reloadKey = "driver-shell-outdated-reload-at";
                var lastReloadAt = 0;
                try { lastReloadAt = Number(window.sessionStorage.getItem(reloadKey) || 0); } catch (error) {}
                if (!lastReloadAt || Date.now() - lastReloadAt > 30000) {
                    try { window.sessionStorage.setItem(reloadKey, String(Date.now())); } catch (error) {}
                    window.setTimeout(function () { window.location.reload(); }, 1500);
                }
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
