/* Dispatcher desktop PWA runtime. This file intentionally contains only the
   Dispatcher contour; Mining Master mobile behavior lives in the template's
   dedicated mobile branch. No Django syntax is allowed here. */
document.addEventListener("DOMContentLoaded", function () {
    // 1. Bootstrap and shift state.
    var shell = document.querySelector("[data-dispatcher-theme]");
    var themeToggles = document.querySelectorAll("[data-dispatcher-theme-toggle]");
    var saved = localStorage.getItem("dispatcher-theme") || "night";
    var runtimeConfig = shell ? shell.dataset : {};
    var staticPrefix = runtimeConfig.staticPrefix || "/static/";
    var dispatcherMoveExcavatorUrl = runtimeConfig.dispatcherMoveUrl || "";
    var dispatcherAssignTruckUrl = runtimeConfig.dispatcherAssignUrl || "";
    var initialDispatcherBoard = document.querySelector(".dispatcher-board");
    var dispatcherShiftOpen = Boolean(initialDispatcherBoard && initialDispatcherBoard.dataset.dispatcherShiftOpen === "true");
    function syncDispatcherShiftRuntime(freshBoard) {
        if (!freshBoard) return dispatcherShiftOpen;
        var fragmentShiftOpen = freshBoard.dataset
            ? freshBoard.dataset.dispatcherShiftOpen
            : "";
        if (fragmentShiftOpen === "true") {
            dispatcherShiftOpen = true;
        } else if (fragmentShiftOpen === "false") {
            dispatcherShiftOpen = false;
        } else {
            dispatcherShiftOpen = !freshBoard.classList.contains("is-readonly");
        }
        return dispatcherShiftOpen;
    }
    function expireDispatcherFreeBucketMarkers() {
        var currentBoard = document.querySelector(".dispatcher-board");
        var renderedServerNow = currentBoard && Date.parse(currentBoard.dataset.serverNow || "");
        var clientCapturedAt = currentBoard && Number(currentBoard.dataset.serverNowClientCapturedAt || 0);
        if (currentBoard && Number.isFinite(renderedServerNow) && !clientCapturedAt) {
            clientCapturedAt = Date.now();
            currentBoard.dataset.serverNowClientCapturedAt = String(clientCapturedAt);
        }
        var serverClockOffset = Number.isFinite(renderedServerNow)
            ? renderedServerNow - clientCapturedAt
            : 0;
        var effectiveNow = Date.now() + serverClockOffset;
        document.querySelectorAll("[data-dispatcher-free-bucket-expires-at]").forEach(function (marker) {
            var deadline = Date.parse(marker.dataset.dispatcherFreeBucketExpiresAt || "");
            if (Number.isFinite(deadline) && deadline <= effectiveNow) marker.remove();
        });
    }
    expireDispatcherFreeBucketMarkers();
    window.setInterval(expireDispatcherFreeBucketMarkers, 1000);
  // 2. Shared request helpers and transport integration.
    function getCookie(name) {
        var value = "; " + document.cookie;
        var parts = value.split("; " + name + "=");
        if (parts.length === 2) return parts.pop().split(";").shift();
        return "";
    }
    function getCsrfToken() {
        var input = document.querySelector("[name=csrfmiddlewaretoken]");
        return getCookie("csrftoken") || (input ? input.value : "");
    }
    function updateDispatcherSyncIndicator(state) {
        var desktopBoard = document.querySelector(".dispatcher-board");
        var currentState = state || dispatcherTransport.getDebugState();
        if (desktopBoard) {
            desktopBoard.classList.toggle("is-realtime-stale", !currentState.realtimeConnected);
        }
    }
    if (typeof window.createDispatcherTransport !== "function") {
        throw new Error("Dispatcher transport module is not loaded");
    }
    var dispatcherTransport = window.createDispatcherTransport({
        getCsrfToken: getCsrfToken,
        onServerError: showDispatcherDnDError,
        onStateChange: updateDispatcherSyncIndicator
    });
    var dispatcherSyncQueueKey = dispatcherTransport.queueKey;
    var scheduleDispatcherSyncFlush = dispatcherTransport.scheduleFlush;
    var dispatcherRoleIsReadonly = dispatcherTransport.roleIsReadonly;
    var dispatcherPost = dispatcherTransport.post;
    window.addEventListener("focus", scheduleDispatcherSyncFlush);
    window.addEventListener("pageshow", scheduleDispatcherSyncFlush);
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) scheduleDispatcherSyncFlush();
    });
  // 3. Equipment detail and desktop-board module wiring.
    if (typeof window.createDispatcherDetail !== "function") {
        throw new Error("Dispatcher detail module is not loaded");
    }
    var dispatcherDetail = window.createDispatcherDetail({
        runtimeConfig: runtimeConfig,
        staticPrefix: staticPrefix,
        getCsrfToken: getCsrfToken,
        roleIsReadonly: dispatcherRoleIsReadonly,
        getShiftOpen: function () {
            return dispatcherShiftOpen;
        }
    });
    if (typeof window.createDispatcherBoard !== "function") {
        throw new Error("Dispatcher board module is not loaded");
    }
    var dispatcherRealtime = null;
    var dispatcherBoard = window.createDispatcherBoard({
        post: dispatcherPost,
        moveExcavatorUrl: dispatcherMoveExcavatorUrl,
        assignTruckUrl: dispatcherAssignTruckUrl,
        getShiftOpen: function () {
            return dispatcherShiftOpen;
        },
        showError: showDispatcherDnDError,
        reloadFallback: reloadDispatcherBoardAsFallback,
        openEquipmentCard: dispatcherDetail.openEquipmentCard,
        equipmentStateClass: dispatcherDetail.equipmentStateClass,
        equipmentStateLabel: dispatcherDetail.equipmentStateLabel,
        neutralEquipmentIcon: dispatcherDetail.neutralEquipmentIcon,
        setNodeEquipmentState: dispatcherDetail.setNodeEquipmentState,
        refreshBoardFromServer: function (refreshOptions) {
            return dispatcherRealtime.refreshBoardFromServer(refreshOptions);
        },
        markLocalAssignmentApplied: function () {
            return dispatcherRealtime.markLocalAssignmentApplied();
        }
    });
    function reloadDispatcherBoardAsFallback() {
        if (typeof window.showAppSyncOverlay === "function") {
            window.showAppSyncOverlay({
                title: "Синхронизируем диспетчерский экран",
                text: "Сервер отдал состояние, которое нельзя безопасно применить точечно. Загружаем свежую версию."
            });
        }
        window.setTimeout(function () {
            window.location.reload();
        }, 80);
    }
    // 4. Realtime fragment reconciliation integration.
    if (typeof window.createDispatcherRealtime !== "function") {
        throw new Error("Dispatcher realtime module is not loaded");
    }
    dispatcherRealtime = window.createDispatcherRealtime({
        transport: dispatcherTransport,
        syncShiftRuntime: syncDispatcherShiftRuntime,
        getEquipmentCards: dispatcherDetail.getCards,
        setEquipmentCards: dispatcherDetail.setCards,
        getDetailLayer: dispatcherDetail.getLayer,
        openEquipmentCard: dispatcherDetail.openEquipmentCard,
        bindBoardInteractions: dispatcherBoard.bindInteractions,
        refreshBoardIntegrity: dispatcherBoard.refreshIntegrity,
        updateSyncIndicator: updateDispatcherSyncIndicator
    });
    var markDispatcherLocalAssignmentApplied = dispatcherRealtime.markLocalAssignmentApplied;
    var isDispatcherOperationalRefreshUnsafe = dispatcherRealtime.isOperationalRefreshUnsafe;
    var refreshDispatcherDesktopBoardFromServer = dispatcherRealtime.refreshBoardFromServer;
    var seedDispatcherBoardFingerprints = dispatcherRealtime.seedBoardFingerprints;
    window.applyOperationalStateRefresh = function (context) {
        return dispatcherRealtime.applyOperationalStateRefresh(context);
    };
    var dispatcherNotice = document.querySelector("[data-dispatcher-notice]");
    var dispatcherNoticeMessage = document.querySelector("[data-dispatcher-notice-message]");
    var dispatcherNoticeClose = document.querySelector("[data-dispatcher-notice-close]");
    var dispatcherConflictRefreshPending = false;
    var dispatcherConflictRefreshInFlight = null;
    var dispatcherConflictRefreshTimer = null;
    var dispatcherConflictRefreshApplyFailures = 0;
    var dispatcherConflictRefreshRetryAfterFlight = false;
    function wakeDispatcherConflictRecovery(reason) {
        if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
            window.AppRealtime.wake(reason || "dispatcher_assignment_conflict");
        }
    }
    function scheduleDispatcherConflictRefresh(delayMs) {
        if (!dispatcherConflictRefreshPending || dispatcherConflictRefreshInFlight || dispatcherConflictRefreshTimer !== null) {
            return;
        }
        dispatcherConflictRefreshTimer = window.setTimeout(function () {
            dispatcherConflictRefreshTimer = null;
            flushDispatcherConflictRefresh();
        }, Math.max(0, Number(delayMs) || 0));
    }
    function flushDispatcherConflictRefresh() {
        if (!dispatcherConflictRefreshPending) return Promise.resolve(false);
        if (dispatcherNotice && !dispatcherNotice.hidden) return Promise.resolve(false);
        if (isDispatcherOperationalRefreshUnsafe()) {
            scheduleDispatcherConflictRefresh(500);
            return Promise.resolve(false);
        }
        if (dispatcherConflictRefreshInFlight) return dispatcherConflictRefreshInFlight;
        dispatcherConflictRefreshPending = false;
        dispatcherConflictRefreshInFlight = Promise.resolve().then(function () {
            return refreshDispatcherDesktopBoardFromServer({ forceFullBoard: true });
        }).then(function (applied) {
            if (applied) {
                dispatcherConflictRefreshApplyFailures = 0;
                return true;
            }
            dispatcherConflictRefreshPending = true;
            dispatcherConflictRefreshApplyFailures += 1;
            wakeDispatcherConflictRecovery("dispatcher_conflict_fragment_not_applied");
            if (dispatcherConflictRefreshApplyFailures >= 2) {
                dispatcherConflictRefreshPending = false;
                reloadDispatcherBoardAsFallback();
            } else {
                dispatcherConflictRefreshRetryAfterFlight = true;
            }
            return false;
        }).catch(function () {
            dispatcherConflictRefreshPending = true;
            wakeDispatcherConflictRecovery("dispatcher_conflict_fragment_error");
            return false;
        }).finally(function () {
            dispatcherConflictRefreshInFlight = null;
            if (dispatcherConflictRefreshRetryAfterFlight) {
                dispatcherConflictRefreshRetryAfterFlight = false;
                scheduleDispatcherConflictRefresh(500);
            }
        });
        return dispatcherConflictRefreshInFlight;
    }
    function closeDispatcherNotice() {
        if (dispatcherNotice) dispatcherNotice.hidden = true;
        scheduleDispatcherConflictRefresh(0);
    }
    function showDispatcherNotice(message) {
        if (!dispatcherNotice || !dispatcherNoticeMessage) return false;
        dispatcherNoticeMessage.textContent = message || "Действие не выполнено.";
        dispatcherNotice.hidden = false;
        if (dispatcherNoticeClose) dispatcherNoticeClose.focus();
        return true;
    }
    if (dispatcherNoticeClose) {
        dispatcherNoticeClose.addEventListener("click", closeDispatcherNotice);
    }
    if (dispatcherNotice) {
        dispatcherNotice.addEventListener("click", function (event) {
            if (event.target === dispatcherNotice) closeDispatcherNotice();
        });
    }
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") closeDispatcherNotice();
    });
    function showDispatcherDnDError(error) {
        var message = error && error.message ? error.message : "Действие не выполнено.";
        window.dispatchEvent(new CustomEvent("dispatcher-action-error", {
            detail: {
                message: message,
                code: error && error.code ? String(error.code) : "",
                conflict: Boolean(error && error.conflict)
            }
        }));
        if (error && error.conflict) {
            dispatcherConflictRefreshPending = true;
            dispatcherConflictRefreshApplyFailures = 0;
            dispatcherConflictRefreshRetryAfterFlight = false;
        }
        var noticeShown = showDispatcherNotice(message);
        if (!noticeShown) {
            console.warn(message);
        }
        if (error && error.conflict) {
            if (!noticeShown) scheduleDispatcherConflictRefresh(0);
        }
    }
    window.addEventListener("online", function () {
        scheduleDispatcherConflictRefresh(0);
    });
    window.addEventListener("pageshow", function () {
        scheduleDispatcherConflictRefresh(0);
    });
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) scheduleDispatcherConflictRefresh(0);
    });
  // 5. Theme behavior.
    function setTheme(theme) {
        if (!shell) return;
        shell.classList.toggle("dispatcher-night", theme === "night");
        shell.classList.toggle("dispatcher-day", theme !== "night");
        localStorage.setItem("dispatcher-theme", theme);
    }
    setTheme(saved);
    themeToggles.forEach(function (toggle) {
        toggle.addEventListener("click", function () {
            setTheme(shell.classList.contains("dispatcher-night") ? "day" : "night");
        });
    });

    // 6. Dispatcher service worker and runtime diagnostics.
    if ("serviceWorker" in navigator) {

        navigator.serviceWorker.register("/dispatcher-sw.js", {
            scope: runtimeConfig.dispatcherServiceWorkerScope || "/dispatcher/"
        }).then(function (registration) {
            registration.update().catch(function () {});
            if (registration.waiting) {
                registration.waiting.postMessage({type: "SKIP_WAITING"});
            }
            registration.addEventListener("updatefound", function () {
                var installing = registration.installing;
                if (!installing) return;
                installing.addEventListener("statechange", function () {
                    if (installing.state === "installed" && navigator.serviceWorker.controller) {
                        (registration.waiting || installing).postMessage({type: "SKIP_WAITING"});
                    }
                });
            });
            document.addEventListener("visibilitychange", function () {
                if (!document.hidden) {
                    registration.update().catch(function () {});
                }
            });
        }).catch(function () {});

    }
    updateDispatcherSyncIndicator();
    window.addEventListener("online", scheduleDispatcherSyncFlush);
    window.addEventListener("operational-state-connection", function (event) {
        dispatcherTransport.updateRealtimeConnection(event.detail || {});
    });
    window.addEventListener("storage", function (event) {
        if (event.key === dispatcherSyncQueueKey) updateDispatcherSyncIndicator();
    });
    scheduleDispatcherSyncFlush();
    window.DispatcherSyncDebug = {
        queueKey: dispatcherSyncQueueKey,
        getState: function () {
            var transportState = dispatcherTransport.getDebugState();
            return {
                realtimeConnected: transportState.realtimeConnected,
                realtimeLastSuccessAt: transportState.realtimeLastSuccessAt,
                realtimeLastReason: transportState.realtimeLastReason,
                syncQueue: transportState.syncQueue,
                appRealtime: window.AppRealtime && typeof window.AppRealtime.getDebugState === "function"
                    ? window.AppRealtime.getDebugState()
                    : null
            };
        },
        wake: function () {
            scheduleDispatcherSyncFlush(0);
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("manual_debug");
            }
        }
    };

    seedDispatcherBoardFingerprints(document.querySelector(".dispatcher-board"));
    dispatcherBoard.bindInteractions();
    window.addEventListener("resize", dispatcherBoard.refreshComplexTruckRacks);
});
