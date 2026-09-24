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
    // 2. Equipment detail-card DOM and presentation state.
    var equipmentCardsNode = document.getElementById("gd-equipment-cards-data");
    var equipmentCards = equipmentCardsNode ? JSON.parse(equipmentCardsNode.textContent) : {};
    var equipmentStatesNode = document.getElementById("gd-equipment-states-data");
    var equipmentStates = equipmentStatesNode ? JSON.parse(equipmentStatesNode.textContent) : {};
    var detailLayer = document.querySelector("[data-gd-equipment-detail]");
    var detailIconSlot = document.querySelector("[data-gd-detail-icon-slot]");
    var detailType = document.querySelector("[data-gd-detail-type]");
    var detailTitle = document.querySelector("[data-gd-detail-title]");
    var detailStatus = document.querySelector("[data-gd-detail-status]");
    var detailZone = document.querySelector("[data-gd-detail-zone]");
    var detailList = document.querySelector("[data-gd-detail-list]");
    var detailEmployee = document.querySelector("[data-gd-detail-employee]");
    var detailEmployeeImg = document.querySelector("[data-gd-detail-employee-img]");
    var detailEmployeeInitials = document.querySelector("[data-gd-detail-employee-initials]");
    var detailEmployeeName = document.querySelector("[data-gd-detail-employee-name]");
    var detailEmployeePhone = document.querySelector("[data-gd-detail-employee-phone]");
    var detailEmployeePresence = document.querySelector("[data-gd-detail-employee-presence]");
    var detailDowntime = document.querySelector("[data-gd-detail-downtime]");
    var detailDowntimeReason = document.querySelector("[data-gd-detail-downtime-reason]");
    var detailDowntimeStarted = document.querySelector("[data-gd-detail-downtime-started]");
    var detailDowntimeTimer = document.querySelector("[data-gd-detail-downtime-timer]");
    var detailDowntimeClose = document.querySelector("[data-gd-detail-downtime-close]");
    var detailDowntimeResult = document.querySelector("[data-gd-detail-downtime-result]");
    var detailSettings = document.querySelector("[data-gd-detail-settings]");
    var detailSettingsTitle = document.querySelector("[data-gd-detail-settings-title]");
    var detailSettingsHint = document.querySelector("[data-gd-detail-settings-hint]");
    var detailSettingsStatus = document.querySelector("[data-gd-detail-settings-status]");
    var detailSettingHorizon = document.querySelector("[data-gd-setting-horizon]");
    var detailSettingBlock = document.querySelector("[data-gd-setting-block]");
    var detailSettingRock = document.querySelector("[data-gd-setting-rock]");
    var detailDestinationList = document.querySelector("[data-gd-destination-list]");
    var detailDestinationAdd = document.querySelector("[data-gd-destination-add]");
    var detailDestinationCount = document.querySelector("[data-gd-destination-count]");
    var detailSettingSave = document.querySelector("[data-gd-setting-save]");
    var detailDumpPointOptions = [];
    var detailShiftReport = document.querySelector("[data-gd-detail-shift-report]");
    var detailMetrics = document.querySelector("[data-gd-detail-metrics]");
    var detailMeta = document.querySelector("[data-gd-detail-meta]");
    var detailPlanBox = document.querySelector("[data-gd-detail-plan]");
    var detailPlanPercent = document.querySelector("[data-gd-detail-plan-percent]");
    var detailPlanFact = document.querySelector("[data-gd-detail-plan-fact]");
    var detailShiftBox = document.querySelector("[data-gd-detail-shift]");
    var detailShiftType = document.querySelector("[data-gd-detail-shift-type]");
    var detailShiftOpened = document.querySelector("[data-gd-detail-shift-opened]");
    var detailShiftPresence = document.querySelector("[data-gd-detail-shift-presence]");
    var detailShiftSeen = document.querySelector("[data-gd-detail-shift-seen]");
    var detailServiceClose = document.querySelector("[data-gd-detail-service-close]");
    var detailServiceCloseToggle = document.querySelector("[data-gd-detail-service-close-toggle]");
    var detailServiceCloseBody = document.querySelector("[data-gd-detail-service-close-body]");
    var detailServiceCloseCancel = document.querySelector("[data-gd-detail-service-close-cancel]");
    var detailServiceCloseMileage = document.querySelector("[data-gd-detail-service-close-mileage]");
    var detailServiceCloseNeglect = document.querySelector("[data-gd-detail-service-close-neglect]");
    var detailServiceCloseKind = document.querySelector("[data-gd-detail-service-close-kind]");
    var detailShiftAutoClose = document.querySelector("[data-gd-detail-shift-autoclose]");
    var detailServiceCloseHint = document.querySelector("[data-gd-detail-service-close-hint]");
    var detailCrewTitle = document.querySelector("[data-gd-detail-crew-title]");
    var detailShiftVerdict = document.querySelector("[data-gd-detail-shift-verdict]");
    var detailShiftAlert = document.querySelector("[data-gd-detail-shift-alert]");
    var detailShiftPeriod = document.querySelector("[data-gd-detail-shift-period]");
    var detailShiftDuration = document.querySelector("[data-gd-detail-shift-duration]");
    var detailManualTrip = document.querySelector("[data-gd-detail-manual-trip]");
    var detailManualTripHint = document.querySelector("[data-gd-detail-manual-trip-hint]");
    var detailManualTripBlocked = document.querySelector("[data-gd-detail-manual-trip-blocked]");
    var detailManualTripForm = document.querySelector("[data-gd-detail-manual-trip-form]");
    var detailManualTripToggle = document.querySelector("[data-gd-detail-manual-trip-toggle]");
    var detailManualTripBody = document.querySelector("[data-gd-detail-manual-trip-body]");
    var detailManualTripCancel = document.querySelector("[data-gd-detail-manual-trip-cancel]");
    var detailManualTripDump = document.querySelector("[data-gd-detail-manual-trip-dump]");
    var detailManualTripRock = document.querySelector("[data-gd-detail-manual-trip-rock]");
    var detailManualTripTime = document.querySelector("[data-gd-detail-manual-trip-time]");
    var detailScrollHint = document.querySelector("[data-gd-detail-scroll-hint]");
    var detailScrollPanel = detailLayer ? detailLayer.querySelector(".mm-equipment-detail-panel") : null;
    /* План показан крупно в шапке — те же строки в общем списке не повторяем. */
    var DETAIL_PLAN_LABELS = ["Статус плана", "Факт / план", "Выполнение плана", "План смены", "Группа плана"];

    /* Карточка выше окна листается внутри; без подсказки обрез внизу выглядит
       как оторванный блок. Полоска видна, пока есть что листать. */
    function syncDetailScrollHint() {
        if (!detailScrollHint || !detailScrollPanel) return;
        var rest = detailScrollPanel.scrollHeight - detailScrollPanel.clientHeight - detailScrollPanel.scrollTop;
        detailScrollHint.hidden = rest <= 12;
    }
    if (detailScrollPanel) {
        detailScrollPanel.addEventListener("scroll", syncDetailScrollHint, { passive: true });
        window.addEventListener("resize", syncDetailScrollHint);
        if (typeof ResizeObserver === "function") {
            new ResizeObserver(syncDetailScrollHint).observe(detailScrollPanel);
        }
    }
    var detailTrucks = document.querySelector("[data-gd-detail-trucks]");
    var detailTrucksCount = document.querySelector("[data-gd-detail-trucks-count]");
    var detailTrucksList = document.querySelector("[data-gd-detail-trucks-list]");
    var detailTrucksRemoved = document.querySelector("[data-gd-detail-trucks-removed]");
    /* Паспорт техники уходит в строку под именем, сведения о смене — в блок
       машиниста; в общем списке остаётся только то, чему нет своего места. */
    var DETAIL_META_LABELS = ["Экскаватор", "Модель", "ГП, т", "Кузов/ковш, м3", "Гаражный N"];
    var DETAIL_SHIFT_LABELS = ["Смена", "Смена открыта", "Связь", "Последняя связь", "Приложение", "В составе"];
    var detailTabs = document.querySelector("[data-gd-detail-tabs]");
    var detailDashboard = document.querySelector("[data-gd-detail-dashboard]");
    var detailLoadState = document.querySelector("[data-gd-detail-load-state]");
    var detailLoadMessage = document.querySelector("[data-gd-detail-load-message]");
    var detailRetry = document.querySelector("[data-gd-detail-retry]");
    var detailRequestController = null;
    var detailRequestToken = 0;
    var detailRetryAction = null;
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
    function formatDispatcherDowntimeDuration(totalSeconds) {
        totalSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        var hours = Math.floor(totalSeconds / 3600);
        var minutes = Math.floor((totalSeconds % 3600) / 60);
        var seconds = totalSeconds % 60;
        return [hours, minutes, seconds].map(function (value) {
            return String(value).padStart(2, "0");
        }).join(":");
    }
    function updateDispatcherDowntimeTimers() {
        var now = Date.now();
        document.querySelectorAll("[data-gd-downtime-timer][data-started-at]").forEach(function (timer) {
            var startedAt = Date.parse(timer.dataset.startedAt || "");
            if (!Number.isFinite(startedAt)) return;
            timer.textContent = formatDispatcherDowntimeDuration((now - startedAt) / 1000);
        });
    }
    updateDispatcherDowntimeTimers();
    window.setInterval(updateDispatcherDowntimeTimers, 1000);
    function dispatcherEquipmentState(code) {
        var key = code || "inactive";
        return equipmentStates[key] || equipmentStates.inactive || {
            code: key,
            label: "",
            color_group: "gray",
            allows_assignment: false,
            allows_drag: false,
            blocks_operation: true
        };
    }
    function dispatcherEquipmentStateColor(code) {
        var color = dispatcherEquipmentState(code).color_group || "gray";
        return ["green", "yellow", "blue", "orange", "red", "gray"].indexOf(color) >= 0 ? color : "gray";
    }
    function dispatcherEquipmentStateClass(code) {
        return "status-" + dispatcherEquipmentStateColor(code);
    }
    function dispatcherEquipmentStateLabel(code) {
        return dispatcherEquipmentState(code).label || "";
    }
    function dispatcherEquipmentStateIconColor(code) {
        var color = dispatcherEquipmentStateColor(code);
        return color === "orange" ? "yellow" : color;
    }
    function dispatcherNeutralEquipmentIcon(equipmentType) {
        var prefix = equipmentType === "excavator" ? "excavator" : "truck";
        return staticPrefix + "img/equipment/" + prefix + "-gray.png";
    }
    function setDispatcherNodeEquipmentState(node, code, equipmentType) {
        if (!node) return;
        var state = dispatcherEquipmentState(code);
        node.classList.remove("status-red", "status-yellow", "status-green", "status-blue", "status-orange", "status-gray", "status-normal", "status-risk", "status-danger");
        node.classList.add(dispatcherEquipmentStateClass(state.code));
        node.dataset.equipmentState = state.code;
        if (node.dataset.mmMobileEquipmentState !== undefined) {
            node.dataset.mmMobileEquipmentState = state.code;
        }
        var label = node.querySelector("span");
        if (label && state.label) label.textContent = state.label;
        var img = node.querySelector("img");
        if (img && equipmentType) {
            img.src = dispatcherNeutralEquipmentIcon(equipmentType);
        }
    }
    // 3. Mutations, offline queue and request transport.
    var dispatcherSyncPendingCount = 0;
    var dispatcherSyncQueueKey = "mining-master-mobile-sync-queue-v3";
    try {
        // Очередь v1 не содержала версии назначения. Повторять такие команды
        // после обновления опасно: они могли быть сформированы до более нового
        // решения диспетчера.
        window.localStorage.removeItem("mining-master-mobile-sync-queue-v1");
        window.localStorage.removeItem("mining-master-mobile-sync-queue-v2");
    } catch (error) {}
    var dispatcherSyncQueueFlushing = false;
    var dispatcherSyncFlushTimer = null;
    var dispatcherSyncRequestTimeoutMs = 12000;
    var dispatcherRefreshRequestTimeoutMs = 12000;
    var dispatcherRealtimeConnected = true;
    var dispatcherRealtimeLastSuccessAt = 0;
    var dispatcherRealtimeLastReason = "";
    function readDispatcherSyncQueue() {
        try {
            return JSON.parse(window.localStorage.getItem(dispatcherSyncQueueKey) || "[]");
        } catch (error) {
            return [];
        }
    }
    function writeDispatcherSyncQueue(queue) {
        try {
            window.localStorage.setItem(dispatcherSyncQueueKey, JSON.stringify(queue || []));
        } catch (error) {}
        updateDispatcherSyncIndicator();
    }
    function getDispatcherSyncQueueState() {
        var queue = readDispatcherSyncQueue();
        var now = Date.now();
        var oldestAgeMs = 0;
        queue.forEach(function (item) {
            var createdAt = Number(item && item.createdAt ? item.createdAt : 0);
            var age = createdAt ? Math.max(0, now - createdAt) : 0;
            if (!oldestAgeMs || age > oldestAgeMs) oldestAgeMs = age;
        });
        return {
            length: queue.length,
            oldestAgeMs: oldestAgeMs,
            isFlushing: dispatcherSyncQueueFlushing,
            pendingCount: dispatcherSyncPendingCount
        };
    }
    function updateDispatcherSyncIndicator() {
        var desktopBoard = document.querySelector(".dispatcher-board");
        if (desktopBoard) {
            desktopBoard.classList.toggle("is-realtime-stale", !dispatcherRealtimeConnected);
        }
    }
    function setDispatcherSyncPending(isPending) {
        dispatcherSyncPendingCount = Math.max(0, dispatcherSyncPendingCount + (isPending ? 1 : -1));
        updateDispatcherSyncIndicator();
    }
    function dispatcherRoleIsReadonly() {
        return (
            typeof window.isAppRoleReadonly === "function"
            && window.isAppRoleReadonly()
        );
    }
    function dispatcherInactiveRoleError() {
        var error = new Error("Роль неактивна — доступен только просмотр");
        error.isServerResponse = true;
        error.code = "inactive_role";
        return error;
    }
    function enqueueDispatcherSyncRequest(request, delayMs) {
        if (dispatcherRoleIsReadonly()) {
            return false;
        }
        var queue = readDispatcherSyncQueue();
        var queuedRequest = Object.assign({
            id: "sync-" + Date.now() + "-" + Math.random().toString(16).slice(2),
            createdAt: Date.now(),
            attempts: 0
        }, request || {});
        var replaceIndex = queuedRequest.coalesceKey ? queue.findIndex(function (item, index) {
            return item.coalesceKey === queuedRequest.coalesceKey && !(dispatcherSyncQueueFlushing && index === 0);
        }) : -1;
        if (replaceIndex >= 0) {
            queuedRequest.createdAt = queue[replaceIndex].createdAt || queuedRequest.createdAt;
            queue[replaceIndex] = queuedRequest;
        } else {
            queue.push(queuedRequest);
        }
        writeDispatcherSyncQueue(queue);
        scheduleDispatcherSyncFlush(delayMs);
        return true;
    }
    function sendDispatcherSyncRequest(request) {
        if (dispatcherRoleIsReadonly()) {
            return Promise.reject(dispatcherInactiveRoleError());
        }
        var headers = { "X-CSRFToken": getCsrfToken() };
        var body = null;
        var controller = window.AbortController ? new AbortController() : null;
        var timeoutId = null;
        if (request.kind === "form") {
            body = new FormData();
            Object.keys(request.fields || {}).forEach(function (key) {
                body.append(key, request.fields[key]);
            });
        } else {
            headers["Content-Type"] = "application/json";
            body = JSON.stringify(request.data || {});
        }
        if (controller) {
            timeoutId = window.setTimeout(function () {
                try {
                    controller.abort();
                } catch (error) {}
            }, dispatcherSyncRequestTimeoutMs);
        }
        return fetch(request.url, {
            method: "POST",
            headers: headers,
            body: body,
            credentials: "same-origin",
            cache: "no-store",
            signal: controller ? controller.signal : undefined
        }).then(function (response) {
            if (!response.ok) {
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    var error = new Error(payload.error || "Действие не выполнено.");
                    error.isServerResponse = true;
                    error.code = payload.code || "";
                    error.conflict = Boolean(payload.conflict);
                    error.status = response.status;
                    throw error;
                });
            }
            return response.json().catch(function () { return { ok: true }; });
        }).finally(function () {
            if (timeoutId) {
                window.clearTimeout(timeoutId);
            }
        });
    }
    function dispatcherFetchWithTimeout(url, options, timeoutMs) {
        var controller = window.AbortController ? new AbortController() : null;
        var timeoutId = null;
        var fetchOptions = Object.assign({}, options || {});
        if (controller) {
            fetchOptions.signal = controller.signal;
            timeoutId = window.setTimeout(function () {
                try {
                    controller.abort();
                } catch (error) {}
            }, timeoutMs || dispatcherRefreshRequestTimeoutMs);
        }
        return fetch(url, fetchOptions).finally(function () {
            if (timeoutId) {
                window.clearTimeout(timeoutId);
            }
        });
    }
    function flushDispatcherSyncQueue() {
        if (dispatcherRoleIsReadonly()) {
            updateDispatcherSyncIndicator();
            return;
        }
        if (dispatcherSyncQueueFlushing) {
            updateDispatcherSyncIndicator();
            return;
        }
        var queue = readDispatcherSyncQueue();
        if (!queue.length) {
            updateDispatcherSyncIndicator();
            return;
        }
        dispatcherSyncQueueFlushing = true;
        setDispatcherSyncPending(true);
        var request = queue[0];
        request.attempts = (request.attempts || 0) + 1;
        sendDispatcherSyncRequest(request).then(function () {
            var freshQueue = readDispatcherSyncQueue();
            if (freshQueue.length && freshQueue[0].id === request.id) {
                freshQueue.shift();
            } else {
                freshQueue = freshQueue.filter(function (item) {
                    return item.id !== request.id;
                });
            }
            writeDispatcherSyncQueue(freshQueue);
        }).catch(function (error) {
            if (error && error.isServerResponse) {
                var freshQueue = readDispatcherSyncQueue().filter(function (item) {
                    return item.id !== request.id;
                });
                writeDispatcherSyncQueue(freshQueue);
                showDispatcherDnDError(error);
            } else {
                var retryQueue = readDispatcherSyncQueue();
                if (retryQueue.length && retryQueue[0].id === request.id) {
                    retryQueue[0].attempts = request.attempts;
                    writeDispatcherSyncQueue(retryQueue);
                }
            }
        }).finally(function () {
            dispatcherSyncQueueFlushing = false;
            setDispatcherSyncPending(false);
            if (readDispatcherSyncQueue().length) {
                window.setTimeout(flushDispatcherSyncQueue, 1200);
            }
        });
    }
    function scheduleDispatcherSyncFlush(delayMs) {
        updateDispatcherSyncIndicator();
        if (dispatcherSyncFlushTimer) {
            window.clearTimeout(dispatcherSyncFlushTimer);
        }
        dispatcherSyncFlushTimer = window.setTimeout(function () {
            dispatcherSyncFlushTimer = null;
            flushDispatcherSyncQueue();
        }, typeof delayMs === "number" ? delayMs : 80);
    }
    function dispatcherPost(url, data, options) {
        if (dispatcherRoleIsReadonly()) {
            return Promise.reject(dispatcherInactiveRoleError());
        }
        var payload = Object.assign({}, data || {});
        options = options || {};
        if (!payload.client_action_id) {
            payload.client_action_id = "mm-" + Date.now() + "-" + Math.random().toString(16).slice(2);
        }
        var request = {
            kind: "json",
            url: url,
            data: payload
        };
        setDispatcherSyncPending(true);
        return sendDispatcherSyncRequest(request).catch(function (error) {
            if (error && error.isServerResponse) throw error;
            if (options.queueOnNetworkFailure === false) throw error;
            enqueueDispatcherSyncRequest(request);
            return { queued: true };
        }).finally(function () {
            setDispatcherSyncPending(false);
        });
    }
    function haulAssignmentStateId(node) {
        var value = node && node.dataset ? node.dataset.haulAssignmentStateId : "";
        return /^\d+$/.test(String(value || "")) ? String(value) : "0";
    }
    function collectComplexAssignmentStates(complexCard) {
        var states = {};
        if (!complexCard) return states;
        complexCard.querySelectorAll(
            "[data-complex-truck='true'][data-equipment-id], " +
            "[data-mm-mobile-home-truck-id]"
        ).forEach(function (truck) {
            var truckId = truck.dataset.equipmentId || truck.dataset.mmMobileHomeTruckId || "";
            if (truckId) states[String(truckId)] = haulAssignmentStateId(truck);
        });
        return states;
    }
    function applyHaulAssignmentState(response, truckNode) {
        if (!response || !truckNode || response.assignment_state_id === undefined) return;
        truckNode.dataset.haulAssignmentStateId = String(response.assignment_state_id || 0);
    }
    function applyHaulAssignmentStates(response, root) {
        var states = response && response.assignment_state_ids;
        if (!states || !root) return;
        root.querySelectorAll("[data-equipment-id], [data-mm-mobile-home-truck-id]").forEach(function (node) {
            var truckId = node.dataset.equipmentId || node.dataset.mmMobileHomeTruckId || "";
            if (truckId && Object.prototype.hasOwnProperty.call(states, truckId)) {
                node.dataset.haulAssignmentStateId = String(states[truckId] || 0);
            }
        });
    }
    window.addEventListener("focus", scheduleDispatcherSyncFlush);
    window.addEventListener("pageshow", scheduleDispatcherSyncFlush);
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) scheduleDispatcherSyncFlush();
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
    // 4. Realtime fragment reconciliation.
    var dispatcherRealtimeStorageKey = "operational-state-version";
    var dispatcherRealtimeLastVersion = readRenderedOperationalStateVersion() || readDispatcherRealtimeVersion();
    function readRenderedOperationalStateVersion() {
        var parsed = parseInt(document.body ? document.body.dataset.operationalStateVersion || "0" : "0", 10);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
    }
    function readDispatcherRealtimeVersion() {
        try {
            var raw = window.sessionStorage.getItem(dispatcherRealtimeStorageKey);
            var parsed = parseInt(raw || "0", 10);
            return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
        } catch (error) {
            return 0;
        }
    }
    function storeDispatcherRealtimeVersion(version) {
        var parsed = parseInt(version || "0", 10);
        if (!Number.isFinite(parsed) || parsed <= 0) return;
        dispatcherRealtimeLastVersion = parsed;
        if (document.body) {
            document.body.dataset.operationalStateVersion = String(parsed);
        }
        try {
            window.sessionStorage.setItem(dispatcherRealtimeStorageKey, String(parsed));
        } catch (error) {}
    }
    var dispatcherRealtimeHardLagLimit = 150;
    var dispatcherLocalAssignmentAppliedUntil = 0;
    var dispatcherIncomingRefreshQueueGraceMs = 15000;
    var dispatcherSyncQueueWakeThrottleMs = 1500;
    var dispatcherLastSyncQueueWakeAt = 0;
    function wakeDispatcherSyncQueueForRefresh() {
        var now = Date.now();
        if (now - dispatcherLastSyncQueueWakeAt < dispatcherSyncQueueWakeThrottleMs) return;
        dispatcherLastSyncQueueWakeAt = now;
        scheduleDispatcherSyncFlush(0);
    }
    function isDispatcherSyncQueueBlockingRefresh() {
        if (dispatcherSyncQueueFlushing || dispatcherSyncPendingCount > 0) {
            return true;
        }
        var queue = readDispatcherSyncQueue();
        if (!queue.length) {
            return false;
        }
        wakeDispatcherSyncQueueForRefresh();
        if (navigator && navigator.onLine === false) {
            return true;
        }
        var now = Date.now();
        return queue.some(function (item) {
            var createdAt = Number(item && item.createdAt ? item.createdAt : 0);
            return !createdAt || now - createdAt < dispatcherIncomingRefreshQueueGraceMs;
        });
    }
    function isElementRendered(node) {
        if (!node) return false;
        var style = window.getComputedStyle(node);
        if (!style || style.display === "none" || style.visibility === "hidden") return false;
        return node.getClientRects().length > 0;
    }
    function isDispatcherDesktopPage() {
        return isElementRendered(document.querySelector(".dispatcher-board"));
    }
    function markDispatcherLocalAssignmentApplied() {
        dispatcherLocalAssignmentAppliedUntil = Date.now() + 8000;
    }
    function hasDispatcherRelevantEvents(events) {
        return Array.isArray(events) && events.length > 0;
    }
    function canTrustLocalDispatcherAssignmentEvents(events) {
        if (!Array.isArray(events) || !events.length) return false;
        if (Date.now() > dispatcherLocalAssignmentAppliedUntil) return false;
        return events.every(function (event) {
            return event && event.type === "assignment_changed";
        });
    }
    function isDispatcherOperationalRefreshUnsafe() {
        if (!isDispatcherDesktopPage()) return false;
        var active = document.activeElement;
        var activeTag = active && active.tagName ? active.tagName.toLowerCase() : "";
        if (active && (active.isContentEditable || activeTag === "input" || activeTag === "textarea" || activeTag === "select")) {
            return true;
        }
        if (isDispatcherSyncQueueBlockingRefresh()) {
            return true;
        }
        if (document.body.classList.contains("modal-open")) {
            return true;
        }
        if (document.querySelector(".app-confirm-modal:not([hidden]), .dispatcher-notice-modal:not([hidden]), .mm-mobile-update-modal:not([hidden]), [data-gd-equipment-detail]:not([hidden])")) {
            return true;
        }
        if (document.querySelector(".dispatcher-dragging, .dispatcher-drop-target, .is-dragging")) {
            return true;
        }
        return false;
    }
    function captureDispatcherDesktopState(currentBoard) {
        var selectors = [
            ".dispatcher-left",
            ".dispatcher-excavators",
            ".dispatcher-complexes",
            ".dispatcher-zone-grid",
            ".dispatcher-right",
            ".dispatcher-trucks"
        ];
        var state = {
            scrollX: window.scrollX || 0,
            scrollY: window.scrollY || 0,
            scrolls: {},
            activeDetailCardId: "",
            detailScrollTop: 0
        };
        selectors.forEach(function (selector) {
            var node = currentBoard ? currentBoard.querySelector(selector) : document.querySelector(selector);
            if (!node) return;
            state.scrolls[selector] = {
                top: node.scrollTop || 0,
                left: node.scrollLeft || 0
            };
        });
        if (detailLayer && !detailLayer.hidden) {
            state.activeDetailCardId = detailLayer.dataset.gdActiveCardId || "";
            var panel = detailLayer.querySelector(".mm-equipment-detail-panel");
            state.detailScrollTop = panel ? panel.scrollTop || 0 : 0;
        }
        return state;
    }
    function restoreDispatcherDesktopState(freshBoard, state) {
        if (!state) return;
        Object.keys(state.scrolls || {}).forEach(function (selector) {
            var node = freshBoard ? freshBoard.querySelector(selector) : document.querySelector(selector);
            var saved = state.scrolls[selector];
            if (!node || !saved) return;
            node.scrollTop = saved.top || 0;
            node.scrollLeft = saved.left || 0;
        });
        window.scrollTo(state.scrollX || 0, state.scrollY || 0);
        if (state.activeDetailCardId && equipmentCards[String(state.activeDetailCardId || "")]) {
            openEquipmentCard(state.activeDetailCardId);
            var panel = detailLayer ? detailLayer.querySelector(".mm-equipment-detail-panel") : null;
            if (panel) panel.scrollTop = state.detailScrollTop || 0;
        }
    }
    function dispatcherNodeMarkup(node) {
        return node && typeof node.outerHTML === "string" ? node.outerHTML : "";
    }
    function dispatcherMarkupFingerprint(markup) {
        var value = String(markup || "");
        var hash = 2166136261;
        for (var index = 0; index < value.length; index += 1) {
            hash ^= value.charCodeAt(index);
            hash = Math.imul(hash, 16777619);
        }
        return (hash >>> 0).toString(16) + ":" + value.length;
    }
    function seedDispatcherServerFingerprint(node) {
        if (!node) return "";
        if (!node.__dispatcherServerFingerprint) {
            node.__dispatcherServerFingerprint = dispatcherMarkupFingerprint(dispatcherNodeMarkup(node));
        }
        return node.__dispatcherServerFingerprint;
    }
    function seedDispatcherBoardFingerprints(boardNode) {
        if (!boardNode) return;
        boardNode.querySelectorAll(
            ".dispatcher-equipment-tile, .dispatcher-complex-card[data-zone-id], .dispatcher-truck-tile[data-equipment-id]"
        ).forEach(seedDispatcherServerFingerprint);
    }
    function dispatcherServerMarkupMatches(currentNode, freshNode) {
        var currentFingerprint = seedDispatcherServerFingerprint(currentNode);
        var freshFingerprint = dispatcherMarkupFingerprint(dispatcherNodeMarkup(freshNode));
        freshNode.__dispatcherServerFingerprint = freshFingerprint;
        return currentFingerprint === freshFingerprint;
    }
    function syncDispatcherNodeAttributes(currentNode, freshNode) {
        if (!currentNode || !freshNode) return false;
        Array.prototype.slice.call(currentNode.attributes || []).forEach(function (attribute) {
            if (!freshNode.hasAttribute(attribute.name)) {
                currentNode.removeAttribute(attribute.name);
            }
        });
        Array.prototype.slice.call(freshNode.attributes || []).forEach(function (attribute) {
            currentNode.setAttribute(attribute.name, attribute.value);
        });
        return true;
    }
    function dispatcherItemKey(node, keyName, index) {
        if (!node || !node.dataset) return "__position:" + index;
        return String(node.dataset[keyName] || "__position:" + index);
    }
    function reconcileDispatcherKeyedRegion(currentBoard, freshBoard, definition) {
        var currentRegion = currentBoard.querySelector(definition.region);
        var freshRegion = freshBoard.querySelector(definition.region);
        if (!currentRegion || !freshRegion) return false;
        var currentItems = Array.prototype.slice.call(currentRegion.querySelectorAll(definition.items));
        var freshItems = Array.prototype.slice.call(freshRegion.querySelectorAll(definition.items));
        var currentKeys = currentItems.map(function (node, index) {
            return dispatcherItemKey(node, definition.key, index);
        });
        var freshKeys = freshItems.map(function (node, index) {
            return dispatcherItemKey(node, definition.key, index);
        });
        var keysMatch = currentKeys.length === freshKeys.length
            && currentKeys.every(function (key, index) {
                return key === freshKeys[index];
            });
        if (!keysMatch) {
            seedDispatcherBoardFingerprints(freshRegion);
            currentRegion.replaceWith(freshRegion);
            return true;
        }
        currentItems.forEach(function (currentItem, index) {
            var freshItem = freshItems[index];
            if (!dispatcherServerMarkupMatches(currentItem, freshItem)) {
                currentItem.replaceWith(freshItem);
            }
        });
        syncDispatcherNodeAttributes(currentRegion, freshRegion);
        return true;
    }
    function reconcileDispatcherDesktopBoard(currentBoard, freshBoard) {
        if (!currentBoard || !freshBoard) return null;
        var regions = [
            {
                region: ".dispatcher-excavators",
                items: ".dispatcher-equipment-tile",
                key: "equipmentId"
            },
            {
                region: ".dispatcher-zone-grid",
                items: ".dispatcher-complex-card[data-zone-id]",
                key: "zoneId"
            },
            {
                region: ".dispatcher-trucks",
                items: ".dispatcher-truck-tile[data-equipment-id]",
                key: "equipmentId"
            }
        ];
        var currentTopbar = currentBoard.querySelector(".dispatcher-topbar");
        var freshTopbar = freshBoard.querySelector(".dispatcher-topbar");
        var completeContract = currentTopbar && freshTopbar && regions.every(function (definition) {
            return currentBoard.querySelector(definition.region)
                && freshBoard.querySelector(definition.region);
        });
        if (!completeContract) return null;
        var reconciled = regions.every(function (definition) {
            return reconcileDispatcherKeyedRegion(currentBoard, freshBoard, definition);
        });
        if (!reconciled) return null;
        syncDispatcherNodeAttributes(currentBoard, freshBoard);
        return currentBoard;
    }
    function refreshDispatcherDesktopBoardFromServer(options) {
        options = options || {};
        if (!isDispatcherDesktopPage()) return Promise.resolve(false);
        var currentBoard = document.querySelector(".dispatcher-board");
        var desktopState = captureDispatcherDesktopState(currentBoard);
        if (!window.AppOperationalFragment) return Promise.resolve(false);
        return window.AppOperationalFragment.request(
            "dispatcher",
            Number(options.version || 0)
        ).then(function (payload) {
            if (isDispatcherOperationalRefreshUnsafe()) return false;
            var freshBoard = window.AppOperationalFragment.parseRoot(
                payload.html,
                ".dispatcher-board"
            );
            currentBoard = document.querySelector(".dispatcher-board");
            if (!freshBoard || !currentBoard) return false;
            syncDispatcherShiftRuntime(freshBoard);
            if (payload.equipment_cards && equipmentCardsNode) {
                equipmentCardsNode.textContent = JSON.stringify(payload.equipment_cards);
                try {
                    equipmentCards = JSON.parse(equipmentCardsNode.textContent || "{}");
                } catch (error) {
                    equipmentCards = {};
                }
            }
            var refreshedBoard = options.forceFullBoard
                ? null
                : reconcileDispatcherDesktopBoard(currentBoard, freshBoard);
            if (!refreshedBoard) {
                seedDispatcherBoardFingerprints(freshBoard);
                currentBoard.replaceWith(freshBoard);
                refreshedBoard = freshBoard;
            }
            bindDispatcherDesktopInteractions();
            if (typeof window.initAppConfirmForms === "function") {
                window.initAppConfirmForms();
            }
            if (typeof window.initDispatcherThemeControls === "function") {
                window.initDispatcherThemeControls();
            }
            if (typeof window.initDispatcherRadialClocks === "function") {
                window.initDispatcherRadialClocks();
            }
            restoreDispatcherDesktopState(refreshedBoard, desktopState);
            refreshDesktopBoardIntegrity();
            updateDispatcherSyncIndicator();
            return true;
        });
    }
    function applyDispatcherOperationalStateRefresh(context) {
        if (isDispatcherOperationalRefreshUnsafe()) {
            return Promise.resolve({ deferred: true, reason: "dispatcher_busy" });
        }
        var targetVersion = context && context.version;
        var events = context && context.events;
        var currentStoredVersion = dispatcherRealtimeLastVersion || readDispatcherRealtimeVersion();
        var versionGap = targetVersion && currentStoredVersion ? targetVersion - currentStoredVersion : 0;
        if (context && context.eventsTruncated || versionGap > dispatcherRealtimeHardLagLimit) {
            return refreshDispatcherDesktopBoardFromServer({ version: targetVersion, forceFullBoard: true }).then(function (applied) {
                if (!applied) return { deferred: true, reason: "dispatcher_refresh_failed" };
                storeDispatcherRealtimeVersion(targetVersion);
                return { applied: true };
            }).catch(function () {
                return { deferred: true, reason: "dispatcher_refresh_error" };
            });
        }
        if (!hasDispatcherRelevantEvents(events) || canTrustLocalDispatcherAssignmentEvents(events)) {
            storeDispatcherRealtimeVersion(targetVersion);
            return Promise.resolve({ applied: true });
        }
        return refreshDispatcherDesktopBoardFromServer({ version: targetVersion }).then(function (applied) {
            if (!applied) return { deferred: true, reason: "dispatcher_refresh_failed" };
            storeDispatcherRealtimeVersion(targetVersion);
            return { applied: true };
        }).catch(function () {
            return { deferred: true, reason: "dispatcher_refresh_error" };
        });
    }
    window.applyOperationalStateRefresh = function (context) {
        if (!isDispatcherDesktopPage()) return false;
        return applyDispatcherOperationalStateRefresh(context);
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
    // 5. Theme and equipment-detail behavior.
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

    function normalizeTileStatus(status) {
        var directColors = ["green", "yellow", "blue", "orange", "red", "gray"];
        var legacyCodes = {
            normal: "working",
            danger: "breakdown",
            risk: "waiting",
            reserved: "assigned",
            empty: "inactive"
        };
        if (directColors.indexOf(status) >= 0) return status;
        return dispatcherEquipmentStateColor(legacyCodes[status] || status || "inactive");
    }

    // Атрибуты фазы плана — это настройка заливки с доски: на них держатся
    // правила с мягким цветом. Снимешь — копия вернётся к непрозрачной
    // заливке, и подпись состояния на плитке перестанет читаться.
    var DETAIL_TILE_KEEP_DATA = {
        "data-plan-progress-phase": true,
        "data-plan-loop-percent": true,
        "data-plan-completed-loops": true
    };

    function cleanDetailTile(tile) {
        tile.classList.remove("is-assigned", "is-placeholder", "dispatcher-dragging");
        tile.classList.add("gd-detail-slot-clone");
        tile.removeAttribute("id");
        tile.removeAttribute("role");
        tile.removeAttribute("tabindex");
        tile.removeAttribute("draggable");
        Array.from(tile.attributes).forEach(function (attr) {
            if (attr.name.indexOf("data-") === 0 && !DETAIL_TILE_KEEP_DATA[attr.name]) {
                tile.removeAttribute(attr.name);
            }
        });
        return tile;
    }

    function findSourceGarageTile(cardId) {
        if (!cardId || !window.CSS || !CSS.escape) return null;
        return document.querySelector("[data-equipment-card-id='" + CSS.escape(String(cardId)) + "'][data-garage-item]:not(.is-assigned):not(.is-placeholder)");
    }

    function buildDetailGarageTile(data) {
        var status = normalizeTileStatus(data.status_key);
        var isTruck = String(data.type || "").toLowerCase().indexOf("самосвал") !== -1;
        var plan = data.plan || {};
        var loopProgress = plan.progress_loop_percent;
        var completedLoops = Number(plan.progress_completed_loops || 0);
        var tile = document.createElement("article");
        tile.className = isTruck
            ? "dispatcher-truck-tile status-" + status
            : "dispatcher-equipment-tile dispatcher-excavator-garage-tile status-" + status;
        if (completedLoops > 0) tile.classList.add("is-plan-overrun");
        tile.style.setProperty("--tile-progress", String(loopProgress === null || loopProgress === undefined || loopProgress === "" ? data.percent || 0 : loopProgress) + "%");
        tile.style.setProperty("--tile-total-progress", String(data.percent || 0) + "%");
        if (loopProgress !== null && loopProgress !== undefined && loopProgress !== "") tile.dataset.planLoopPercent = String(loopProgress);
        tile.dataset.planCompletedLoops = String(completedLoops);
        /* Пустая фаза = рейсов по смене нет. Атрибут надо именно снять:
           иначе на перерисованной плитке останется заливка от прошлого
           обновления и пустой самосвал будет выглядеть работающим. */
        if (plan.progress_phase) tile.dataset.planProgressPhase = plan.progress_phase;
        else tile.removeAttribute("data-plan-progress-phase");
        tile.innerHTML =
            "<strong>" + escapeHtml(data.number || "") + "</strong>" +
            '<img src="' + escapeHtml(dispatcherNeutralEquipmentIcon(isTruck ? "truck" : "excavator")) + '" alt="">' +
            "<span>" + escapeHtml(data.status_label || "") + "</span>" +
            (completedLoops > 0 ? '<b class="dispatcher-plan-loop-badge" aria-label="Завершено циклов: ' + completedLoops + '">×' + completedLoops + '</b>' : "");
        return tile;
    }

    function renderDetailGarageIcon(cardId, data) {
        if (!detailIconSlot) return;
        detailIconSlot.innerHTML = "";
        var source = findSourceGarageTile(cardId);
        var tile = source ? source.cloneNode(true) : buildDetailGarageTile(data);
        detailIconSlot.appendChild(cleanDetailTile(tile));
    }

    function buildDetailChartShell(chart) {
        var card = document.createElement("div");
        card.className = "gd-detail-chart-card gd-detail-chart-" + (chart.type || "bar");
        var title = document.createElement("div");
        title.className = "gd-detail-report-title";
        title.textContent = chart.title || "";
        card.appendChild(title);
        if (chart.summary) {
            var summary = document.createElement("div");
            summary.className = "gd-detail-report-summary";
            summary.textContent = chart.summary;
            card.appendChild(summary);
        }
        if (chart.type === "donut-list") {
            return card;
        }
        var gauges = document.createElement("div");
        gauges.className = "gd-detail-gauge-strip";
        chart.rows.slice(0, 3).forEach(function (row) {
            var item = document.createElement("div");
            item.className = "gd-detail-gauge-summary accent-" + (row.accent || "green");
            item.style.setProperty("--gauge-pct", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
            item.innerHTML =
                "<div class=\"gd-detail-gauge\"><strong>" + escapeHtml(row.value || "") + "</strong></div>" +
                "<span>" + escapeHtml(row.target || row.label || row.source || "") + "</span>";
            gauges.appendChild(item);
        });
        card.appendChild(gauges);
        return card;
    }

    function renderDetailChart(chart) {
        if (!detailDashboard) return;
        detailDashboard.innerHTML = "";
        if (!chart || !chart.rows || !chart.rows.length) return;
        var card = buildDetailChartShell(chart);
        if (chart.type === "matrix") {
            var groupedRows = {};
            chart.rows.forEach(function (row) {
                var key = row.label || "не указан";
                if (!groupedRows[key]) {
                    groupedRows[key] = [];
                }
                groupedRows[key].push(row);
            });
            Object.keys(groupedRows).forEach(function (label) {
                var rows = groupedRows[label];
                var matrix = document.createElement("div");
                matrix.className = "gd-detail-matrix-row is-grouped";
                var face = document.createElement("div");
                face.className = "gd-detail-matrix-face";
                face.textContent = label;
                var cell = document.createElement("div");
                cell.className = "gd-detail-matrix-cell gd-detail-matrix-pie-cell";
                var pie = document.createElement("div");
                pie.className = "gd-detail-pie";
                var cursor = 0;
                var totalPercent = rows.reduce(function (sum, row) {
                    return sum + Math.max(0, Number(row.percent || 0));
                }, 0) || 100;
                var stops = rows.map(function (row) {
                    var raw = Math.max(0, Number(row.percent || 0));
                    var size = Math.max(4, Math.min(100, (raw / totalPercent) * 100));
                    var start = cursor;
                    cursor += size;
                    return "var(--pie-" + (row.accent || "green") + ") " + start + "% " + cursor + "%";
                });
                if (cursor < 100) {
                    stops.push("rgba(142, 158, 166, .16) " + cursor + "% 100%");
                }
                pie.style.backgroundImage = "radial-gradient(circle at center, var(--gd-detail-panel) 0 50%, transparent 51%), conic-gradient(" + stops.join(", ") + ")";
                var total = document.createElement("strong");
                total.textContent = rows.length + " напр.";
                pie.appendChild(total);
                var legend = document.createElement("div");
                legend.className = "gd-detail-pie-legend";
                rows.forEach(function (row) {
                    var item = document.createElement("div");
                    item.className = "gd-detail-pie-item accent-" + (row.accent || "green");
                    item.innerHTML = "<span></span><strong>" + escapeHtml(row.target || "") + "</strong><em>" + escapeHtml(row.value || "") + "</em><small>" + escapeHtml(row.meta || "") + "</small>";
                    legend.appendChild(item);
                });
                cell.appendChild(pie);
                cell.appendChild(legend);
                matrix.appendChild(face);
                matrix.appendChild(cell);
                card.appendChild(matrix);
            });
            detailDashboard.appendChild(card);
            return;
        }
        if (chart.type === "donut-list") {
            var breakdown = document.createElement("div");
            breakdown.className = "gd-detail-breakdown";
            var stack = document.createElement("div");
            stack.className = "gd-detail-stack";
            var totalPercent = chart.rows.reduce(function (sum, row) {
                return sum + Math.max(0, Number(row.percent || 0));
            }, 0) || 100;
            chart.rows.forEach(function (row) {
                var segment = document.createElement("i");
                segment.className = "accent-" + (row.accent || "green");
                segment.style.setProperty("--segment-share", Math.max(4, Math.min(100, (Math.max(0, Number(row.percent || 0)) / totalPercent) * 100)) + "%");
                stack.appendChild(segment);
            });
            breakdown.appendChild(stack);
            var donutGrid = document.createElement("div");
            donutGrid.className = "gd-detail-breakdown-grid";
            chart.rows.forEach(function (row) {
                var item = document.createElement("div");
                item.className = "gd-detail-breakdown-row accent-" + (row.accent || "green");
                item.style.setProperty("--bar-pct", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
                item.innerHTML =
                    "<div class=\"gd-detail-breakdown-mark\"></div>" +
                    "<div class=\"gd-detail-breakdown-main\"><strong>" + escapeHtml(row.label || "") + "</strong><span>" + escapeHtml(row.meta || "") + "</span><em><i></i></em></div>" +
                    "<b>" + escapeHtml(row.value || "") + "</b>";
                donutGrid.appendChild(item);
            });
            breakdown.appendChild(donutGrid);
            card.appendChild(breakdown);
            detailDashboard.appendChild(card);
            return;
        }
        if (chart.type === "truck-ledger") {
            var ledger = document.createElement("div");
            ledger.className = "gd-detail-truck-ledger";
            ["current", "removed"].forEach(function (stateKey) {
                var stateRows = chart.rows.filter(function (row) { return row.state_key === stateKey; });
                if (!stateRows.length) return;
                var group = document.createElement("div");
                group.className = "gd-detail-truck-group is-" + stateKey;
                var groupTitle = document.createElement("strong");
                groupTitle.textContent = stateKey === "current" ? "В составе сейчас" : "Работали и выведены";
                group.appendChild(groupTitle);
                stateRows.forEach(function (row) {
                    var item = document.createElement("div");
                    item.className = "gd-detail-truck-ledger-row accent-" + (row.accent || "green");
                    item.style.setProperty("--tile-progress", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
                    item.innerHTML =
                        "<div class=\"gd-detail-truck-mini\"><b>" + escapeHtml(row.truck || row.label || "") + "</b><span>" + escapeHtml(row.state || "") + "</span></div>" +
                        "<div class=\"gd-detail-truck-route\"><strong>" + escapeHtml(row.target || "") + "</strong><span>" + escapeHtml(row.rock || "") + "</span><em><i></i></em></div>" +
                        "<div class=\"gd-detail-truck-value\">" + escapeHtml(row.value || "") + "</div>";
                    group.appendChild(item);
                });
                ledger.appendChild(group);
            });
            card.appendChild(ledger);
            detailDashboard.appendChild(card);
            return;
        }
        chart.rows.forEach(function (row) {
            if (chart.type === "route") {
                var route = document.createElement("div");
                route.className = "gd-detail-route-row accent-" + (row.accent || "green");
                route.style.setProperty("--gauge-pct", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
                var source = document.createElement("div");
                source.className = "gd-detail-route-node";
                source.textContent = row.source || "";
                var flow = document.createElement("div");
                flow.className = "gd-detail-route-flow";
                flow.innerHTML = "<i></i>";
                var target = document.createElement("div");
                target.className = "gd-detail-route-node";
                target.textContent = row.target || "";
                var gauge = document.createElement("div");
                gauge.className = "gd-detail-gauge";
                gauge.innerHTML = "<strong>" + escapeHtml(row.value || "") + "</strong>";
                var meta = document.createElement("div");
                meta.className = "gd-detail-route-meta";
                meta.textContent = row.meta || "";
                route.appendChild(source);
                route.appendChild(flow);
                route.appendChild(target);
                route.appendChild(gauge);
                route.appendChild(meta);
                card.appendChild(route);
                return;
            }
            var line = document.createElement("div");
            line.className = "gd-detail-chart-row accent-" + (row.accent || "green");
            line.style.setProperty("--bar-pct", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
            var head = document.createElement("div");
            head.className = "gd-detail-chart-head";
            var label = document.createElement("strong");
            var value = document.createElement("span");
            label.textContent = row.label || "";
            value.textContent = row.value || "";
            head.appendChild(label);
            head.appendChild(value);
            var meta = document.createElement("div");
            meta.className = "gd-detail-chart-meta";
            meta.textContent = row.meta || "";
            var bar = document.createElement("div");
            bar.className = "gd-detail-chart-bar";
            bar.appendChild(document.createElement("i"));
            line.appendChild(head);
            line.appendChild(meta);
            line.appendChild(bar);
            card.appendChild(line);
        });
        detailDashboard.appendChild(card);
    }

    function renderDetailShiftReport(report) {
        if (!detailShiftReport || !detailMetrics || !detailTabs || !detailDashboard) return;
        var metrics = (report && report.metrics) || [];
        var charts = ((report && report.charts) || []).filter(function (chart) {
            return chart && chart.rows && chart.rows.length;
        });
        detailMetrics.innerHTML = "";
        detailTabs.innerHTML = "";
        detailDashboard.innerHTML = "";
        metrics.forEach(function (metric) {
            if (!metric || !metric.value) return;
            var item = document.createElement("div");
            var label = document.createElement("span");
            var value = document.createElement("strong");
            label.textContent = metric.label || "";
            value.textContent = metric.value || "";
            item.appendChild(label);
            item.appendChild(value);
            detailMetrics.appendChild(item);
        });
        charts.forEach(function (chart, index) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "gd-detail-tab" + (index === 0 ? " is-active" : "");
            button.textContent = chart.title || ("Отчет " + (index + 1));
            button.addEventListener("click", function () {
                var panel = detailLayer ? detailLayer.querySelector(".mm-equipment-detail-panel") : null;
                var savedScrollTop = panel ? panel.scrollTop : 0;
                detailTabs.querySelectorAll(".gd-detail-tab").forEach(function (node) {
                    node.classList.remove("is-active");
                });
                button.classList.add("is-active");
                renderDetailChart(chart);
                if (panel) {
                    panel.scrollTop = savedScrollTop;
                }
            });
            detailTabs.appendChild(button);
        });
        renderDetailChart(charts[0]);
        detailTabs.hidden = charts.length < 2;
        detailShiftReport.hidden = metrics.length === 0 && charts.length === 0;
    }

    function currentDispatcherBoardVersion() {
        var currentBoard = document.querySelector(".dispatcher-board");
        var parsed = Number(currentBoard && currentBoard.dataset
            ? currentBoard.dataset.operationalStateVersion
            : 0);
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
    }

    function setDetailLoadState(message, canRetry) {
        if (detailLoadMessage) detailLoadMessage.textContent = message || "";
        if (detailRetry) detailRetry.hidden = !canRetry;
        if (detailLoadState) detailLoadState.hidden = !message;
    }

    function resetDetailContent() {
        if (detailEmployee) detailEmployee.hidden = true;
        if (detailDowntime) detailDowntime.hidden = true;
        if (detailDowntimeTimer) detailDowntimeTimer.removeAttribute("data-started-at");
        if (detailDowntimeResult) detailDowntimeResult.textContent = "";
        if (detailDowntimeClose) detailDowntimeClose.disabled = false;
        if (detailLayer) delete detailLayer.dataset.gdDowntimeEventId;
        if (detailSettings) detailSettings.hidden = true;
        if (detailSettingsStatus) detailSettingsStatus.textContent = "";
        if (detailList) detailList.innerHTML = "";
        if (detailShiftReport) detailShiftReport.hidden = true;
        if (detailMeta) detailMeta.textContent = "";
        if (detailPlanBox) detailPlanBox.hidden = true;
        if (detailShiftBox) detailShiftBox.hidden = true;
        if (detailTrucks) detailTrucks.hidden = true;
        if (detailShiftAlert) detailShiftAlert.hidden = true;
        if (detailShiftVerdict) detailShiftVerdict.hidden = true;
        if (detailManualTrip) detailManualTrip.hidden = true;
        closeDetailManualTripForm();
        closeDetailServiceCloseForm();
        if (detailServiceClose) {
            detailServiceClose.hidden = true;
            detailServiceClose.removeAttribute("action");
        }
    }

    function closeDetailServiceCloseForm() {
        if (!detailServiceClose) return;
        detailServiceClose.classList.remove("is-open");
        if (detailServiceCloseBody) detailServiceCloseBody.hidden = true;
        Array.prototype.forEach.call(detailServiceClose.querySelectorAll("input:not([type=hidden])"), function (input) {
            input.value = "";
        });
    }

    function openDetailServiceCloseForm() {
        if (!detailServiceClose || !detailServiceCloseToggle || detailServiceCloseToggle.disabled) return;
        detailServiceClose.classList.add("is-open");
        if (detailServiceCloseBody) detailServiceCloseBody.hidden = false;
        var reason = detailServiceClose.querySelector("[name=reason]");
        if (reason) reason.focus();
    }

    /* Смена машиниста/водителя: сведения + служебное завершение. Форма
       обычная: POST на dispatcher_service_close_shift и редирект с сообщением,
       тот же путь, что у «Незакрытых смен» в журнале. */
    function renderDetailShift(shift, employee) {
        var presenceNode = detailEmployeePresence;
        var presenceStatus = (shift && shift.presence_status) || (employee && employee.presence_status) || "";
        if (presenceNode) {
            presenceNode.className = "gd-detail-crew-presence" + (presenceStatus ? " is-" + presenceStatus : "");
        }
        if (!shift) {
            if (detailShiftBox) detailShiftBox.hidden = true;
            if (detailServiceClose) detailServiceClose.hidden = true;
            if (detailShiftAlert) detailShiftAlert.hidden = true;
            if (detailShiftVerdict) detailShiftVerdict.hidden = true;
            return;
        }
        if (detailShiftType) detailShiftType.textContent = shift.type_label || "";
        if (detailShiftOpened) detailShiftOpened.textContent = shift.opened_at_label || "";
        if (detailShiftPresence) detailShiftPresence.textContent = shift.presence_label || "";
        if (detailShiftSeen) detailShiftSeen.textContent = shift.last_seen_label || "";
        if (detailShiftPeriod) detailShiftPeriod.textContent = shift.period_label || shift.type_label || "";
        if (detailShiftDuration) detailShiftDuration.textContent = shift.duration_label || "";
        /* Текущая смена — зелёная метка; хвост прошлой смены — красная и
           развёрнутое предупреждение: диспетчер сразу видит, что водитель
           прошлой смены не закрыл её, а не гадает по дате открытия. */
        if (detailShiftVerdict) {
            var verdict = shift.verdict || "";
            detailShiftVerdict.className = "gd-detail-shift-verdict" + (verdict ? " is-" + verdict : "");
            detailShiftVerdict.textContent = shift.verdict_label || "";
            detailShiftVerdict.hidden = !shift.verdict_label;
        }
        if (detailShiftAlert) {
            detailShiftAlert.textContent = shift.alert || "";
            detailShiftAlert.className = "gd-detail-shift-alert" + (shift.verdict ? " is-" + shift.verdict : "");
            detailShiftAlert.hidden = !shift.alert;
        }
        if (detailShiftBox) detailShiftBox.hidden = false;
        if (detailServiceClose) {
            closeDetailServiceCloseForm();
            detailServiceClose.hidden = !shift.service_close_url;
            if (shift.service_close_url) detailServiceClose.setAttribute("action", shift.service_close_url);
            if (detailServiceCloseMileage) {
                detailServiceCloseMileage.hidden = !shift.is_truck;
                var mileage = detailServiceCloseMileage.querySelector("input");
                if (mileage) mileage.required = false;
            }
            var closeLocked = dispatcherRoleIsReadonly() || !dispatcherShiftOpen;
            if (detailServiceCloseToggle) detailServiceCloseToggle.disabled = closeLocked;
            if (detailServiceCloseNeglect) detailServiceCloseNeglect.disabled = closeLocked;
            if (detailShiftAutoClose) detailShiftAutoClose.textContent = shift.auto_close_at_label || "—";
            renderDetailShiftReadingBounds(shift);
        }
    }

    /* Подсказка по показаниям: сервер требует целые числа, моточасы не меньше
       начальных и не больше +12 за смену — говорим это до отправки, а не после. */
    function renderDetailShiftReadingBounds(shift) {
        if (!detailServiceClose) return;
        var hours = detailServiceClose.querySelector("[name=end_engine_hours]");
        var mileage = detailServiceClose.querySelector("[name=end_mileage]");
        var startHours = shift && shift.start_engine_hours ? Number(String(shift.start_engine_hours).replace(",", ".")) : NaN;
        var startMileage = shift && shift.start_mileage ? Number(String(shift.start_mileage).replace(",", ".")) : NaN;
        if (hours) {
            if (!shift.is_truck && isFinite(startHours)) {
                hours.min = String(Math.round(startHours));
                hours.max = String(Math.round(startHours) + 12);
            } else {
                hours.min = "0";
                hours.removeAttribute("max");
            }
        }
        if (mileage) {
            mileage.min = shift.is_truck && isFinite(startMileage) ? String(Math.floor(startMileage)) : "0";
        }
        if (!detailServiceCloseHint) return;
        var parts = [];
        if (shift.start_fuel) parts.push("топливо " + shift.start_fuel + " л");
        if (shift.is_truck && shift.start_mileage) parts.push("одометр " + shift.start_mileage + " км");
        if (shift.start_engine_hours) parts.push("моточасы " + shift.start_engine_hours);
        var text = parts.length ? "На начало смены: " + parts.join(" · ") + ". " : "";
        text += shift.is_truck
            ? "Если показания известны — целые числа; иначе оставьте пустыми."
            : "Если показания известны — целые числа, моточасы не меньше начальных и не более +12; иначе оставьте пустыми.";
        detailServiceCloseHint.textContent = text;
        detailServiceCloseHint.hidden = false;
    }

    /* Выполнение плана крупно в шапке: одна цифра, которую диспетчер ищет
       первой; факт/план и группа — строкой под ней. */
    function renderDetailPlan(plan) {
        if (!detailPlanBox) return;
        var hasPlan = plan && plan.progress_percent !== null && plan.progress_percent !== undefined;
        if (!plan || (!hasPlan && !plan.plan_status_label)) {
            detailPlanBox.hidden = true;
            return;
        }
        detailPlanBox.hidden = false;
        detailPlanBox.classList.toggle("is-muted", !hasPlan);
        if (detailPlanPercent) detailPlanPercent.textContent = hasPlan ? String(plan.progress_percent) + "%" : "—";
        if (detailPlanFact) {
            detailPlanFact.textContent = hasPlan
                ? [plan.fact_plan_label, plan.plan_group_name].filter(Boolean).join(" · ")
                : (plan.plan_status_label || "");
        }
    }

    function closeDetailManualTripForm() {
        if (!detailManualTripForm) return;
        detailManualTripForm.classList.remove("is-open");
        if (detailManualTripBody) detailManualTripBody.hidden = true;
        var reason = detailManualTripForm.querySelector("[name=reason]");
        if (reason) reason.value = "";
        if (detailManualTripTime) detailManualTripTime.value = "";
    }

    function detailLocalDateTimeValue(date) {
        var pad = function (value) { return (value < 10 ? "0" : "") + value; };
        return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
            + "T" + pad(date.getHours()) + ":" + pad(date.getMinutes());
    }

    function openDetailManualTripForm() {
        if (!detailManualTripForm || detailManualTripForm.hidden) return;
        detailManualTripForm.classList.add("is-open");
        if (detailManualTripBody) detailManualTripBody.hidden = false;
        if (detailManualTripTime) detailManualTripTime.max = detailLocalDateTimeValue(new Date());
        var reason = detailManualTripForm.querySelector("[name=reason]");
        if (reason) reason.focus();
    }

    function appendDetailOption(parent, value, label, selected) {
        var option = document.createElement("option");
        option.value = String(value);
        option.textContent = label;
        if (selected) option.selected = true;
        parent.appendChild(option);
    }

    /* Ручной рейс: сервер отдаёт точки забоя с плечом и остальные активные
       точки, породу по умолчанию из настроек экскаватора. Отправка — обычной
       формой на dispatcher_manual_trip с подтверждением. */
    function renderDetailManualTrip(manual) {
        if (!detailManualTrip) return;
        closeDetailManualTripForm();
        if (!manual) {
            detailManualTrip.hidden = true;
            return;
        }
        detailManualTrip.hidden = false;
        if (detailManualTripHint) {
            detailManualTripHint.textContent = manual.excavator_label
                ? "На экскаватор " + manual.excavator_label + " от имени водителя открытой смены."
                : "";
        }
        var blocked = manual.blocked_reason || "";
        if (!blocked && (dispatcherRoleIsReadonly() || !dispatcherShiftOpen)) {
            blocked = "Нужна открытая смена диспетчера.";
        }
        if (detailManualTripBlocked) {
            detailManualTripBlocked.textContent = blocked;
            detailManualTripBlocked.hidden = !blocked;
        }
        if (detailManualTripForm) {
            detailManualTripForm.hidden = !!blocked || !manual.url;
            if (manual.url) detailManualTripForm.setAttribute("action", manual.url);
            else detailManualTripForm.removeAttribute("action");
            var excavatorInput = detailManualTripForm.querySelector("[name=excavator_id]");
            if (excavatorInput) excavatorInput.value = manual.excavator_id || "";
            var countInput = detailManualTripForm.querySelector("[name=trips_count]");
            if (countInput) {
                countInput.max = String(manual.max_count || 10);
                countInput.value = "1";
            }
        }
        if (detailManualTripDump) {
            detailManualTripDump.innerHTML = "";
            var known = {};
            (manual.destinations || []).forEach(function (row, index) {
                known[String(row.dump_point_id)] = true;
                var distance = row.transport_distance_km ? " · " + String(row.transport_distance_km).replace(".", ",") + " км" : "";
                appendDetailOption(detailManualTripDump, row.dump_point_id, row.name + distance, index === 0);
            });
            var others = (manual.dump_points || []).filter(function (point) { return !known[String(point.id)]; });
            if (others.length) {
                var group = document.createElement("optgroup");
                group.label = (manual.destinations || []).length ? "Другие точки" : "Точки разгрузки";
                others.forEach(function (point) { appendDetailOption(group, point.id, point.name, false); });
                detailManualTripDump.appendChild(group);
            }
        }
        if (detailManualTripRock) {
            detailManualTripRock.innerHTML = "";
            (manual.rock_types || []).forEach(function (rock) {
                appendDetailOption(detailManualTripRock, rock.id, rock.name, String(manual.rock_type_id || "") === String(rock.id));
            });
        }
    }

    function renderDetailTruckChips(container, numbers) {
        if (!container) return;
        container.innerHTML = "";
        (numbers || []).forEach(function (number) {
            var chip = document.createElement("span");
            chip.textContent = String(number);
            container.appendChild(chip);
        });
    }

    function renderDetailTrucks(data) {
        if (!detailTrucks) return;
        var report = data.shift_report || {};
        var current = Array.isArray(report.current_trucks) ? report.current_trucks : [];
        var removed = Array.isArray(report.removed_trucks) ? report.removed_trucks : [];
        if (data.category !== "complex" || (!current.length && !removed.length)) {
            detailTrucks.hidden = true;
            return;
        }
        renderDetailTruckChips(detailTrucksList, current);
        if (!current.length && detailTrucksList) {
            var empty = document.createElement("em");
            empty.textContent = "самосвалы не назначены";
            detailTrucksList.appendChild(empty);
        }
        renderDetailTruckChips(detailTrucksRemoved, removed);
        if (detailTrucksRemoved) {
            detailTrucksRemoved.hidden = !removed.length;
            if (removed.length) {
                var note = document.createElement("em");
                note.textContent = "выведены за смену";
                detailTrucksRemoved.insertBefore(note, detailTrucksRemoved.firstChild);
            }
        }
        if (detailTrucksCount) {
            detailTrucksCount.textContent = current.length
                ? current.length + " " + (current.length === 1 ? "машина" : current.length < 5 ? "машины" : "машин")
                : "";
        }
        detailTrucks.hidden = false;
    }

    function renderDetailDowntime(data) {
        var downtime = data || {};
        if (!detailDowntime || !downtime.active) {
            if (detailDowntime) detailDowntime.hidden = true;
            if (detailLayer) delete detailLayer.dataset.gdDowntimeEventId;
            return;
        }
        if (detailLayer) detailLayer.dataset.gdDowntimeEventId = String(downtime.event_id || "");
        if (detailDowntimeReason) detailDowntimeReason.textContent = downtime.reason || "Простой";
        if (detailDowntimeStarted) {
            detailDowntimeStarted.textContent = downtime.started_at_label
                ? "С начала: " + downtime.started_at_label
                : "";
        }
        if (detailDowntimeTimer) {
            detailDowntimeTimer.dataset.startedAt = downtime.started_at || "";
            detailDowntimeTimer.textContent = downtime.elapsed_label || "00:00:00";
        }
        if (detailDowntimeResult) detailDowntimeResult.textContent = "";
        if (detailDowntimeClose) {
            detailDowntimeClose.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftOpen;
            detailDowntimeClose.textContent = "Завершить простой";
        }
        detailDowntime.hidden = false;
        updateDispatcherDowntimeTimers();
    }

    function dispatcherDowntimeCloseUrl(eventId) {
        var template = String(runtimeConfig.dispatcherDowntimeCloseUrlTemplate || "");
        if (!/^\d+$/.test(String(eventId || "")) || !template) return "";
        var path = template.replace(/0\/close\/$/, String(eventId) + "/close/");
        return path === template ? "" : path;
    }

    function dispatcherDowntimeCloseError(error) {
        if (error && error.status === 401) return "Сессия завершена. Войдите в систему снова.";
        if (error && error.status === 403) return "Нет доступа к завершению простоя.";
        if (error && error.code === "dispatcher_shift_required") return "Смена горного диспетчера закрыта.";
        if (error && error.code === "inactive_role") return "Роль неактивна — доступен только просмотр.";
        if (error && error.status === 409) return "Состояние техники уже изменилось. Карточка будет обновлена.";
        if (error && error.status === 404) return "Этот простой больше не найден.";
        return "Не удалось завершить простой. Проверьте связь и повторите действие.";
    }

    function closeDetailDowntime() {
        if (!detailLayer || !detailDowntimeClose || detailDowntimeClose.disabled) return;
        var eventId = detailLayer.dataset.gdDowntimeEventId || "";
        var url = dispatcherDowntimeCloseUrl(eventId);
        var requestedVersion = Number(detailLayer.dataset.gdRequestedVersion || -1);
        if (!url || requestedVersion < 0) {
            if (detailDowntimeResult) detailDowntimeResult.textContent = "Карточка устарела. Откройте её снова.";
            return;
        }
        detailDowntimeClose.disabled = true;
        detailDowntimeClose.textContent = "Завершаю…";
        if (detailDowntimeResult) detailDowntimeResult.textContent = "";
        fetch(url, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify({state_version: requestedVersion})
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (payload) {
                if (!response.ok || !payload.ok) {
                    var requestError = new Error("downtime_close_failed");
                    requestError.status = response.status;
                    requestError.code = payload.error || "";
                    throw requestError;
                }
                return payload;
            });
        }).then(function () {
            if (detailDowntimeResult) detailDowntimeResult.textContent = "Простой завершён. Обновляем пульт…";
            detailDowntimeClose.textContent = "Простой завершён";
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("dispatcher_downtime_closed");
            }
            window.setTimeout(closeEquipmentCard, 500);
        }).catch(function (error) {
            if (detailDowntimeResult) detailDowntimeResult.textContent = dispatcherDowntimeCloseError(error);
            detailDowntimeClose.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftOpen;
            detailDowntimeClose.textContent = "Завершить простой";
            if (error && error.status === 409 && window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("dispatcher_downtime_stale");
            }
        });
    }

    function requestDetailDowntimeClose() {
        if (!detailLayer || !detailDowntimeClose || detailDowntimeClose.disabled) return;
        var card = equipmentCards[String(detailLayer.dataset.gdActiveCardId || "")] || {};
        var downtime = card.downtime || {};
        var equipmentLabel = card.label || "техника";
        var duration = detailDowntimeTimer ? detailDowntimeTimer.textContent : downtime.elapsed_label;
        var message = "Завершить простой " + equipmentLabel + " «" + (downtime.reason || "Простой") + "»? Длительность " + (duration || "00:00:00") + " будет зафиксирована в отчёте.";
        if (typeof window.openAppConfirmDialog !== "function") {
            if (detailDowntimeResult) detailDowntimeResult.textContent = "Подтверждение недоступно. Обновите страницу.";
            return;
        }
        window.openAppConfirmDialog(message, closeDetailDowntime, 0, "Завершить", {
            confirmTitle: "Завершить простой?",
            confirmDescription: message
        });
    }

    function fillDetailSettingSelect(select, options, selectedId) {
        if (!select) return;
        select.innerHTML = "";
        var placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Выберите";
        select.appendChild(placeholder);
        (options || []).forEach(function (item) {
            var option = document.createElement("option");
            option.value = String(item.id || "");
            option.textContent = item.name || "";
            option.selected = String(item.id || "") === String(selectedId || "");
            select.appendChild(option);
        });
    }

    function detailDestinationRows() {
        return detailDestinationList
            ? Array.prototype.slice.call(detailDestinationList.querySelectorAll("[data-gd-destination-row]"))
            : [];
    }

    function refreshDetailDestinationRows() {
        var rows = detailDestinationRows();
        var selectedIds = rows.map(function (row) {
            var select = row.querySelector("[data-gd-destination-select]");
            return select ? String(select.value || "") : "";
        }).filter(Boolean);
        rows.forEach(function (row) {
            var select = row.querySelector("[data-gd-destination-select]");
            var remove = row.querySelector("[data-gd-destination-remove]");
            if (select) {
                Array.prototype.forEach.call(select.options, function (option) {
                    option.disabled = !!option.value
                        && option.value !== select.value
                        && selectedIds.indexOf(String(option.value)) !== -1;
                });
            }
            if (remove) remove.disabled = rows.length <= 1;
        });
        if (detailDestinationCount) {
            detailDestinationCount.textContent = rows.length
                ? rows.length + " " + (rows.length === 1 ? "точка" : rows.length < 5 ? "точки" : "точек")
                : "не назначены";
        }
        if (detailDestinationAdd) {
            detailDestinationAdd.disabled = dispatcherRoleIsReadonly()
                || !dispatcherShiftOpen
                || rows.length >= detailDumpPointOptions.length;
        }
    }

    function addDetailDestinationRow(destination) {
        if (!detailDestinationList) return;
        var row = document.createElement("div");
        row.className = "gd-detail-destination-row";
        row.setAttribute("data-gd-destination-row", "");

        var selectLabel = document.createElement("label");
        var selectCaption = document.createElement("span");
        selectCaption.textContent = "Точка";
        var select = document.createElement("select");
        select.setAttribute("data-gd-destination-select", "");
        fillDetailSettingSelect(select, detailDumpPointOptions, destination && destination.dump_point_id);
        selectLabel.appendChild(selectCaption);
        selectLabel.appendChild(select);

        var distanceLabel = document.createElement("label");
        distanceLabel.className = "gd-detail-destination-distance";
        var distanceCaption = document.createElement("span");
        distanceCaption.textContent = "Плечо, км";
        var distance = document.createElement("input");
        distance.type = "text";
        distance.inputMode = "decimal";
        distance.maxLength = 12;
        distance.placeholder = "—";
        distance.value = String(destination && destination.transport_distance_km || "").replace(".", ",");
        distance.setAttribute("data-gd-destination-distance", "");
        distanceLabel.appendChild(distanceCaption);
        distanceLabel.appendChild(distance);

        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "gd-detail-destination-remove";
        remove.setAttribute("data-gd-destination-remove", "");
        remove.setAttribute("aria-label", "Убрать точку разгрузки");
        remove.textContent = "×";

        select.addEventListener("change", refreshDetailDestinationRows);
        remove.addEventListener("click", function () {
            row.remove();
            refreshDetailDestinationRows();
        });
        row.appendChild(selectLabel);
        row.appendChild(distanceLabel);
        row.appendChild(remove);
        detailDestinationList.appendChild(row);
        refreshDetailDestinationRows();
    }

    function collectDetailDestinations() {
        var seen = Object.create(null);
        var destinations = [];
        detailDestinationRows().forEach(function (row) {
            var select = row.querySelector("[data-gd-destination-select]");
            var distance = row.querySelector("[data-gd-destination-distance]");
            var id = select ? String(select.value || "") : "";
            if (!id || seen[id]) return;
            seen[id] = true;
            destinations.push({
                dump_point_id: id,
                transport_distance_km: distance ? distance.value : ""
            });
        });
        return destinations;
    }

    function renderDetailSettings(settings) {
        if (!detailSettings) return;
        if (!settings || !settings.editable) {
            detailSettings.hidden = true;
            return;
        }
        detailSettings.hidden = false;
        if (detailSettingsTitle) detailSettingsTitle.textContent = settings.title || "Рабочие параметры комплекса";
        if (detailSettingsHint) detailSettingsHint.textContent = settings.hint || "";
        if (detailSettingsStatus) detailSettingsStatus.textContent = "";
        if (detailSettingHorizon) detailSettingHorizon.value = settings.loading_horizon || "";
        if (detailSettingBlock) detailSettingBlock.value = settings.loading_block || "";
        fillDetailSettingSelect(detailSettingRock, settings.rock_types, settings.rock_type_id);
        detailDumpPointOptions = settings.dump_points || [];
        if (detailDestinationList) detailDestinationList.innerHTML = "";
        var destinations = Array.isArray(settings.destinations) ? settings.destinations : [];
        if (!destinations.length && settings.dump_point_id) {
            destinations = [{
                dump_point_id: settings.dump_point_id,
                transport_distance_km: settings.transport_distance_km || ""
            }];
        }
        destinations.forEach(addDetailDestinationRow);
        if (!destinations.length && detailDumpPointOptions.length) {
            addDetailDestinationRow({dump_point_id: detailDumpPointOptions[0].id});
        }
        refreshDetailDestinationRows();
        if (detailSettingSave) detailSettingSave.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftOpen;
    }

    function detailSettingsErrorMessage(code) {
        if (code === "stale_board") return "Данные пульта уже изменились. Закройте карточку и откройте снова.";
        if (code === "dispatcher_shift_required") return "Сначала откройте смену Горного диспетчера.";
        if (code === "invalid_transport_distance") return "Плечо должно быть числом не меньше нуля.";
        if (code === "invalid_work_settings") return "Выберите действующие породу и точку разгрузки.";
        if (code === "inactive_role") return "Роль неактивна — доступен только просмотр.";
        return "Не удалось сохранить параметры.";
    }

    function saveDetailSettings() {
        if (!detailLayer || !detailSettingSave) return;
        var url = detailLayer.dataset.gdSettingsUrl || "";
        if (!url) return;
        var destinations = collectDetailDestinations();
        if (!detailSettingRock || !detailSettingRock.value || !destinations.length) {
            if (detailSettingsStatus) detailSettingsStatus.textContent = "Выберите породу и хотя бы одну точку.";
            return;
        }
        detailSettingSave.disabled = true;
        if (detailSettingsStatus) detailSettingsStatus.textContent = "Сохраняю…";
        fetch(url, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify({
                state_version: currentDispatcherBoardVersion(),
                loading_horizon: detailSettingHorizon ? detailSettingHorizon.value : "",
                loading_block: detailSettingBlock ? detailSettingBlock.value : "",
                rock_type_id: detailSettingRock.value,
                dump_point_ids: destinations.map(function (row) { return row.dump_point_id; }),
                destinations: destinations
            })
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (payload) {
                if (!response.ok) {
                    var error = new Error("settings_request_failed");
                    error.code = payload.error || "";
                    throw error;
                }
                return payload;
            });
        }).then(function (payload) {
            if (detailSettingsStatus) detailSettingsStatus.textContent = "Настройки сохранены";
            if (payload && payload.settings) renderDetailSettings(payload.settings);
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("dispatcher_settings_saved");
            }
            window.setTimeout(closeEquipmentCard, 650);
        }).catch(function (error) {
            if (detailSettingsStatus) detailSettingsStatus.textContent = detailSettingsErrorMessage(error && error.code);
            detailSettingSave.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftOpen;
        });
    }

    function renderEquipmentCard(cardId, data) {
        if (!data || !detailLayer) return false;
        detailLayer.dataset.gdActiveCardId = String(cardId || "");
        detailLayer.removeAttribute("aria-busy");
        setDetailLoadState("", false);
        renderDetailGarageIcon(cardId, data);
        if (detailType) detailType.textContent = data.type || "";
        if (detailTitle) detailTitle.textContent = data.label || "";
        if (detailStatus) detailStatus.textContent = data.status_label || "";
        if (detailZone) detailZone.textContent = data.zone || "";
        if (detailEmployee) {
            var employee = data.employee || {};
            detailEmployee.hidden = false;
            if (detailEmployeePresence) detailEmployeePresence.textContent = employee.presence_label || "Сотрудник не назначен";
            if (detailEmployeeName) detailEmployeeName.textContent = employee.name || "Сотрудник не назначен";
            if (detailEmployeePhone) {
                detailEmployeePhone.textContent = employee.phone || "телефон не указан";
                if (employee.phone) {
                    detailEmployeePhone.href = "tel:" + employee.phone.replace(/[^\d+]/g, "");
                    detailEmployeePhone.removeAttribute("aria-disabled");
                } else {
                    detailEmployeePhone.removeAttribute("href");
                    detailEmployeePhone.setAttribute("aria-disabled", "true");
                }
            }
            if (detailEmployeeImg && detailEmployeeInitials) {
                if (employee.photo) {
                    detailEmployeeImg.src = employee.photo;
                    detailEmployeeImg.hidden = false;
                    detailEmployeeInitials.hidden = true;
                } else {
                    detailEmployeeImg.removeAttribute("src");
                    detailEmployeeImg.hidden = true;
                    detailEmployeeInitials.textContent = employee.initials || "--";
                    detailEmployeeInitials.hidden = false;
                }
            }
        }
        if (detailCrewTitle) {
            detailCrewTitle.textContent = (data.shift && data.shift.is_truck) || data.type === "Самосвал" ? "Водитель" : "Машинист";
        }
        renderDetailShift(data.shift || null, data.employee || null);
        renderDetailPlan(data.plan || null);
        renderDetailManualTrip(data.manual_trip || null);
        window.setTimeout(syncDetailScrollHint, 0);
        renderDetailDowntime(data.downtime || null);
        renderDetailSettings(data.settings || null);
        renderDetailTrucks(data);
        if (detailMeta) {
            var metaParts = [];
            DETAIL_META_LABELS.forEach(function (label) {
                (data.details || []).forEach(function (row) {
                    if (!row || row.label !== label || !row.value) return;
                    if (label === "Гаражный N" && String(row.value) === String(data.label || "")) return;
                    metaParts.push(label === "ГП, т" ? "ГП " + row.value + " т"
                        : label === "Кузов/ковш, м3" ? "ковш " + row.value + " м³"
                        : label === "Гаражный N" ? "гаражный № " + row.value
                        : String(row.value));
                });
            });
            detailMeta.textContent = metaParts.join(" · ");
        }
        if (detailList) {
            detailList.innerHTML = "";
            (data.details || []).forEach(function (row) {
                if (!row || !row.value) return;
                if (DETAIL_META_LABELS.indexOf(row.label) >= 0) return;
                if (data.shift && DETAIL_SHIFT_LABELS.indexOf(row.label) >= 0) return;
                if (data.category === "complex" && row.label === "В составе") return;
                if (detailPlanBox && !detailPlanBox.hidden && DETAIL_PLAN_LABELS.indexOf(row.label) >= 0) return;
                if (data.downtime && data.downtime.active && ["Простой", "С начала"].indexOf(row.label) >= 0) return;
                var term = document.createElement("dt");
                var value = document.createElement("dd");
                term.textContent = row.label || "";
                value.textContent = row.value || "";
                detailList.appendChild(term);
                detailList.appendChild(value);
            });
        }
        renderDetailShiftReport(data.shift_report || {});
        detailLayer.hidden = false;
        return true;
    }

    function dispatcherDetailUrl(trigger, boardVersion) {
        if (!trigger || !runtimeConfig.dispatcherDetailUrlTemplate) return "";
        var equipmentId = String(trigger.dataset.equipmentId || "");
        if (!/^\d+$/.test(equipmentId)) return "";
        var category = trigger.dataset.dispatcherDrag === "complex" ? "complex" : "equipment";
        var template = String(runtimeConfig.dispatcherDetailUrlTemplate || "");
        var path = template.replace(/equipment\/0\/$/, category + "/" + equipmentId + "/");
        if (path === template) return "";
        return path + "?state_version=" + encodeURIComponent(String(boardVersion));
    }

    function detailErrorMessage(status) {
        if (status === 401) return "Сессия завершена. Войдите в систему снова.";
        if (status === 403 || status === 404) return "Карточка недоступна.";
        if (status === 409) return "Данные изменились — закройте карточку и откройте её снова.";
        return "Нет связи с сервером.";
    }

    function openEquipmentCard(cardId, trigger) {
        if (!detailLayer) return false;
        var cardKey = String(cardId || "");
        trigger = trigger || document.querySelector(
            "[data-equipment-card-id='" + CSS.escape(cardKey) + "'][data-equipment-id]"
        );
        var boardVersion = currentDispatcherBoardVersion();
        var url = dispatcherDetailUrl(trigger, boardVersion);
        if (!url) return false;

        detailRequestToken += 1;
        var requestToken = detailRequestToken;
        if (detailRequestController) detailRequestController.abort();
        detailRequestController = new AbortController();
        delete equipmentCards[cardKey];
        detailLayer.dataset.gdActiveCardId = cardKey;
        detailLayer.dataset.gdRequestedVersion = String(boardVersion);
        detailLayer.dataset.gdSettingsUrl = url;
        detailLayer.setAttribute("aria-busy", "true");
        resetDetailContent();
        if (detailType) detailType.textContent = trigger.dataset.dispatcherDrag === "complex" ? "Комплекс" : "Техника";
        if (detailTitle) detailTitle.textContent = trigger.dataset.equipmentName || "Карточка";
        if (detailStatus) detailStatus.textContent = "";
        if (detailZone) detailZone.textContent = "";
        renderDetailGarageIcon(cardKey, {
            type: trigger.dataset.dispatcherDrag === "truck" ? "Самосвал" : "Экскаватор",
            status_key: "gray"
        });
        setDetailLoadState("Загружаю свежие данные…", false);
        detailLayer.hidden = false;
        detailRetryAction = function () {
            openEquipmentCard(cardKey, trigger);
        };

        fetch(url, {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            signal: detailRequestController.signal,
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest"
            }
        }).then(function (response) {
            if (!response.ok) {
                var requestError = new Error("detail_request_failed");
                requestError.status = response.status;
                throw requestError;
            }
            return response.json();
        }).then(function (payload) {
            var debugState = window.AppRealtime && typeof window.AppRealtime.getDebugState === "function"
                ? window.AppRealtime.getDebugState()
                : null;
            var pendingVersion = Number(debugState && debugState.pendingVersion ? debugState.pendingVersion : 0);
            if (
                requestToken !== detailRequestToken
                || !payload
                || payload.contract !== "dispatcher-equipment-detail-v1"
                || String(payload.card_key || "") !== cardKey
                || Number(payload.operational_state_version || 0) !== boardVersion
                || currentDispatcherBoardVersion() !== boardVersion
                || pendingVersion > boardVersion
            ) {
                var staleError = new Error("stale_detail");
                staleError.status = 409;
                throw staleError;
            }
            equipmentCards[cardKey] = payload.card;
            renderEquipmentCard(cardKey, payload.card);
        }).catch(function (error) {
            if (error && error.name === "AbortError") return;
            if (requestToken !== detailRequestToken || detailLayer.hidden) return;
            resetDetailContent();
            detailLayer.removeAttribute("aria-busy");
            var status = Number(error && error.status ? error.status : 0);
            setDetailLoadState(detailErrorMessage(status), status === 0 || status >= 500);
        });
        return true;
    }

    function closeEquipmentCard() {
        detailRequestToken += 1;
        if (detailRequestController) detailRequestController.abort();
        detailRequestController = null;
        detailRetryAction = null;
        if (detailLayer) {
            detailLayer.hidden = true;
            detailLayer.removeAttribute("aria-busy");
            delete detailLayer.dataset.gdActiveCardId;
            delete detailLayer.dataset.gdRequestedVersion;
            delete detailLayer.dataset.gdSettingsUrl;
        }
        if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
            window.AppRealtime.wake("dispatcher_detail_closed");
        }
    }

    if (detailRetry) {
        detailRetry.addEventListener("click", function () {
            if (typeof detailRetryAction === "function") detailRetryAction();
        });
    }
    if (detailDowntimeClose) {
        detailDowntimeClose.addEventListener("click", requestDetailDowntimeClose);
    }
    if (detailServiceCloseToggle) {
        detailServiceCloseToggle.addEventListener("click", openDetailServiceCloseForm);
    }
    if (detailServiceCloseCancel) {
        detailServiceCloseCancel.addEventListener("click", closeDetailServiceCloseForm);
    }
    if (detailManualTripToggle) {
        detailManualTripToggle.addEventListener("click", openDetailManualTripForm);
    }
    if (detailManualTripCancel) {
        detailManualTripCancel.addEventListener("click", closeDetailManualTripForm);
    }
    if (detailManualTripForm) {
        detailManualTripForm.addEventListener("submit", function (event) {
            event.preventDefault();
            if (!detailManualTripForm.getAttribute("action")) return;
            if (typeof detailManualTripForm.reportValidity === "function" && !detailManualTripForm.reportValidity()) return;
            var card = equipmentCards[String(detailLayer ? detailLayer.dataset.gdActiveCardId || "" : "")] || {};
            var countInput = detailManualTripForm.querySelector("[name=trips_count]");
            var count = parseInt(countInput ? countInput.value : "1", 10) || 1;
            var dumpOption = detailManualTripDump && detailManualTripDump.selectedOptions ? detailManualTripDump.selectedOptions[0] : null;
            var message = "Добавить " + count + " " + (count === 1 ? "рейс" : count < 5 ? "рейса" : "рейсов")
                + " самосвалу " + (card.label || (detailTitle ? detailTitle.textContent : "")) + " → " + (dumpOption ? dumpOption.textContent : "точка")
                + "? Рейс запишется выполненным, отменить его нельзя.";
            if (typeof window.openAppConfirmDialog !== "function") {
                detailManualTripForm.submit();
                return;
            }
            window.openAppConfirmDialog(message, function () { detailManualTripForm.submit(); }, 0, "Добавить рейс", {
                confirmTitle: "Добавить рейс вручную?",
                confirmDescription: message
            });
        });
    }
    /* Два исхода: «не закрыл сам» — одно нажатие без полей (в журнале это
       значит, что сотрудник не выполнил обязанность); «по согласованию» —
       форма с причиной и показаниями по желанию. Вид уходит в close_kind. */
    function submitDetailServiceClose(kind, title, message) {
        if (!detailServiceClose || !detailServiceClose.getAttribute("action")) return;
        if (detailServiceCloseKind) detailServiceCloseKind.value = kind;
        if (typeof window.openAppConfirmDialog !== "function") {
            detailServiceClose.submit();
            return;
        }
        window.openAppConfirmDialog(message, function () { detailServiceClose.submit(); }, 0, "Закрыть смену", {
            confirmTitle: title,
            confirmDescription: message
        });
    }
    function detailServiceCloseSubject() {
        var card = equipmentCards[String(detailLayer ? detailLayer.dataset.gdActiveCardId || "" : "")] || {};
        var employee = card.employee || {};
        return (employee.name || "сотрудника") + " на " + (card.label || (detailTitle ? detailTitle.textContent : "") || "технике");
    }
    if (detailServiceCloseNeglect) {
        detailServiceCloseNeglect.addEventListener("click", function () {
            if (detailServiceCloseNeglect.disabled) return;
            submitDetailServiceClose(
                "neglected",
                "Сотрудник не закрыл смену сам?",
                "Закрыть смену " + detailServiceCloseSubject() + " как незакрытую сотрудником? Причина и показания не нужны; в журнале будет отмечено, что сотрудник не закрыл смену и не сообщил диспетчеру."
            );
        });
    }
    if (detailServiceClose) {
        detailServiceClose.addEventListener("submit", function (event) {
            event.preventDefault();
            if (!detailServiceClose.getAttribute("action")) return;
            if (typeof detailServiceClose.reportValidity === "function" && !detailServiceClose.reportValidity()) return;
            submitDetailServiceClose(
                "coordinated",
                "Закрыть смену по согласованию?",
                "Закрыть смену " + detailServiceCloseSubject() + " по согласованию с сотрудником? Открытые рейсы уйдут в перенос."
            );
        });
    }

    document.querySelectorAll("[data-gd-detail-close]").forEach(function (node) {
        node.addEventListener("click", closeEquipmentCard);
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") closeEquipmentCard();
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
        var detail = event.detail || {};
        dispatcherRealtimeConnected = detail.connected !== false;
        dispatcherRealtimeLastReason = detail.reason || "";
        if (dispatcherRealtimeConnected) {
            dispatcherRealtimeLastSuccessAt = detail.lastSuccessAt || Date.now();
            scheduleDispatcherSyncFlush(0);
        }
        updateDispatcherSyncIndicator();
    });
    window.addEventListener("storage", function (event) {
        if (event.key === dispatcherSyncQueueKey) updateDispatcherSyncIndicator();
    });
    scheduleDispatcherSyncFlush();
    window.DispatcherSyncDebug = {
        queueKey: dispatcherSyncQueueKey,
        getState: function () {
            return {
                realtimeConnected: dispatcherRealtimeConnected,
                realtimeLastSuccessAt: dispatcherRealtimeLastSuccessAt,
                realtimeLastReason: dispatcherRealtimeLastReason,
                syncQueue: getDispatcherSyncQueueState(),
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

    // 7. Desktop board layout, search, drag-and-drop and optimistic updates.
    var draggedTile = null;
    var board = document.querySelector(".dispatcher-board");
    var excavatorGarage = document.querySelector("[data-dispatcher-excavator-garage]");
    var dragGhost = null;
    function refreshExcavatorGarage() {
        if (!board || !excavatorGarage) return;
        sortDesktopEquipmentList(excavatorGarage.querySelector(".dispatcher-excavators"), ".dispatcher-excavator-garage-tile:not(.is-placeholder)");
        var activeTiles = excavatorGarage.querySelectorAll("[data-garage-item='excavator']:not(.is-assigned)");
        board.classList.toggle("is-excavator-garage-empty", activeTiles.length === 0);
    }
    function getDesktopEquipmentSortNumber(tile) {
        if (!tile) return 999999;
        var candidates = [
            tile.dataset.excavatorSlot,
            tile.dataset.equipmentSort,
            tile.dataset.equipmentNumber,
            tile.dataset.equipmentName,
            tile.querySelector("strong") ? tile.querySelector("strong").textContent : ""
        ];
        for (var index = 0; index < candidates.length; index += 1) {
            var match = String(candidates[index] || "").match(/\d+/);
            if (match) return parseInt(match[0], 10);
        }
        return 999999;
    }
    function getDesktopEquipmentSortLabel(tile) {
        if (!tile) return "";
        return String(tile.dataset.equipmentName || (tile.textContent || "")).trim();
    }
    function compareDesktopEquipmentTiles(a, b) {
        var numberDiff = getDesktopEquipmentSortNumber(a) - getDesktopEquipmentSortNumber(b);
        if (numberDiff !== 0) return numberDiff;
        return getDesktopEquipmentSortLabel(a).localeCompare(getDesktopEquipmentSortLabel(b), "ru", { numeric: true });
    }
    function sortDesktopEquipmentList(container, selector) {
        if (!container) return;
        Array.from(container.querySelectorAll(selector))
            .sort(compareDesktopEquipmentTiles)
            .forEach(function (tile) {
                var firstPlaceholder = container.querySelector(".is-placeholder");
                var empty = container.querySelector(".complex-truck-empty");
                if (firstPlaceholder) {
                    container.insertBefore(tile, firstPlaceholder);
                } else if (empty) {
                    container.insertBefore(tile, empty);
                } else {
                    container.appendChild(tile);
                }
            });
    }
    function dispatcherSelectorValue(value) {
        var stringValue = String(value || "");
        if (window.CSS && typeof window.CSS.escape === "function") {
            return window.CSS.escape(stringValue);
        }
        return stringValue.replace(/["\\]/g, "\\$&");
    }
    function removeDuplicateDesktopTruckTiles(truckId, keepTile) {
        if (!truckId) return;
        var selector = '[data-dispatcher-drag="truck"][data-equipment-id="' + dispatcherSelectorValue(truckId) + '"]';
        document.querySelectorAll(selector).forEach(function (tile) {
            if (tile !== keepTile) {
                tile.remove();
            }
        });
    }
    function reconcileDesktopTruckUniqueness() {
        var seen = {};
        document.querySelectorAll('[data-dispatcher-drag="truck"][data-equipment-id]').forEach(function (tile) {
            var truckId = tile.dataset.equipmentId || "";
            if (!truckId) return;
            if (seen[truckId]) {
                tile.remove();
                return;
            }
            seen[truckId] = true;
        });
    }
    function refreshDesktopBoardIntegrity() {
        reconcileDesktopTruckUniqueness();
        refreshTruckGarage();
        refreshAllComplexTruckRacks();
    }
    function refreshTruckGarage() {
        if (!board) return;
        document.querySelectorAll(".dispatcher-truck-tile.is-placeholder").forEach(function (slot) {
            slot.remove();
        });
        var garage = document.querySelector(".dispatcher-trucks");
        sortDesktopEquipmentList(garage, "[data-garage-item='truck']:not(.is-placeholder)");
        var freeTrucks = document.querySelectorAll("[data-garage-item='truck']:not(.is-assigned)");
        var columns = Math.max(1, Math.min(3, Math.ceil(freeTrucks.length / 12)));
        var scrollNeeded = freeTrucks.length > columns * 12;
        board.style.setProperty("--truck-garage-columns", String(columns));
        board.style.setProperty("--truck-garage-scrollbar-w", scrollNeeded ? "14px" : "0px");
        board.classList.toggle("is-truck-garage-empty", freeTrucks.length === 0);
        if (!garage || freeTrucks.length === 0) return;
        var visibleCapacity = columns * 12;
        var placeholderCount = Math.max(0, visibleCapacity - freeTrucks.length);
        for (var index = 0; index < placeholderCount; index += 1) {
            var slot = document.createElement("article");
            slot.className = "dispatcher-truck-tile is-placeholder";
            slot.setAttribute("aria-hidden", "true");
            slot.innerHTML = '<img src="/static/img/equipment/truck-gray.png" alt="">';
            garage.appendChild(slot);
        }
    }
    function clearDragGhost() {
        if (!dragGhost) return;
        dragGhost.remove();
        dragGhost = null;
    }
    function escapeHtml(value) {
        return String(value || "").replace(/[&<>"']/g, function (char) {
            return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char];
        });
    }
    function normalizeComplexGrid() {
        var grid = document.querySelector(".dispatcher-zone-grid");
        if (!grid) return;
        var cards = Array.from(grid.querySelectorAll(".dispatcher-complex-card"));
        function sortWeight(card) {
            if (card.classList.contains("status-red") || card.classList.contains("status-danger")) return 0;
            if (card.classList.contains("status-orange")) return 1;
            if (card.classList.contains("status-yellow") || card.classList.contains("status-risk")) return 2;
            if (card.classList.contains("status-blue")) return 3;
            if (card.classList.contains("status-green") || card.classList.contains("status-normal")) return 4;
            return 5;
        }
        var active = cards
            .filter(function (card) { return !card.classList.contains("status-empty"); })
            .sort(function (a, b) {
                var statusDiff = sortWeight(a) - sortWeight(b);
                if (statusDiff !== 0) return statusDiff;
                return (parseInt(a.dataset.excavatorSlot || "999", 10) || 999) - (parseInt(b.dataset.excavatorSlot || "999", 10) || 999);
            });
        var empty = cards.filter(function (card) { return card.classList.contains("status-empty"); });
        active.concat(empty).forEach(function (card) {
            grid.appendChild(card);
        });
    }
    /* Плитки самосвалов в карточке комплекса.

       Размер считается от размеров САМОГО поля, а не от гаражной плитки
       справа: раньше плитка 73px заезжала в полосу высотой 10px и её срезал
       overflow карточки, а вёрсткой карточки управлял чужой элемент.

       Раскладка подбирается перебором, а не по лестнице фиксированных
       размеров. Лестница брала первый размер, при котором машины помещаются,
       и останавливалась — поэтому при двенадцати машинах в поле оставалась
       пустая колонка справа шириной в целую плитку. Теперь для каждого числа
       колонок считается своя ячейка, и выигрывает та раскладка, где плитка
       крупнее всех: диспетчеру нужно попадать по ним мышью и различать номера
       через комнату, поэтому пустое место всегда отдаётся плиткам.

       Пропорция ограничена коридором, иначе при одной машине на всё поле
       выходил бы вытянутый прямоугольник, а при двадцати — узкая полоска.
       Ниже COMPLEX_TILE_RICH_MIN_H картинка и подпись состояния всё равно
       нечитаемы, поэтому там плитка превращается в жетон номера; состояние
       читается цветом и кольцом плана. Если машин больше, чем ячеек даже при
       минимальном размере, хвост сворачивается в счётчик «+N». Скролла внутри
       карточки нет намеренно: прокручивать десять карточек по отдельности
       диспетчер не станет. */
    var COMPLEX_TILE_GAP = 6;
    /* Потолок держит форму при малом числе машин: исходник картинки 360x245,
       так что до 168px по ширине она не мылится. Нижняя граница коридора
       пропорций 1.15, иначе выигрывала вертикальная плитка (три машины в ряд
       давали 120x126), а самосвал на ней лежит поперёк. */
    var COMPLEX_TILE_MAX = { w: 168, h: 124 };
    var COMPLEX_TILE_MIN = { w: 34, h: 20 };
    /* Ниже этого размера доска не мельчает. Общий размер берётся по самому
       загруженному комплексу, и без нижней границы один комплекс с шестнадцатью
       машинами ужимал плитки на всей доске до нечитаемых. Двенадцать машин на
       один экскаватор — уже за гранью обычной смены, поэтому такой перегруз
       честнее показать счётчиком «+N», чем мельчить все десять карточек. */
    var COMPLEX_TILE_FLOOR = { w: 88, h: 63 };
    /* Минимум информационной панели слева; всё, что шире, отдаётся ей же,
       когда сетка плиток не занимает ширину целиком. */
    var COMPLEX_INFO_MIN_W = 176;
    var COMPLEX_TILE_ASPECT = { min: 1.15, max: 1.55 };
    var COMPLEX_TILE_RICH_MIN_H = 42;

    /* Ячейка при заданном числе колонок и строк, ужатая до коридора пропорций. */
    function complexTileForGrid(rackWidth, rackHeight, cols, rows) {
        var cellW = (rackWidth - (COMPLEX_TILE_GAP * (cols - 1))) / cols;
        var cellH = (rackHeight - (COMPLEX_TILE_GAP * (rows - 1))) / rows;
        if (cellW < COMPLEX_TILE_MIN.w || cellH < COMPLEX_TILE_MIN.h) return null;
        var w = Math.min(cellW, COMPLEX_TILE_MAX.w);
        var h = Math.min(cellH, COMPLEX_TILE_MAX.h);
        if (w / h > COMPLEX_TILE_ASPECT.max) w = h * COMPLEX_TILE_ASPECT.max;
        if (w / h < COMPLEX_TILE_ASPECT.min) h = w / COMPLEX_TILE_ASPECT.min;
        w = Math.floor(w);
        h = Math.floor(h);
        if (w < COMPLEX_TILE_MIN.w || h < COMPLEX_TILE_MIN.h) return null;
        return { w: w, h: h, cols: cols, rows: rows, area: w * h };
    }

    /* Лучшая раскладка для count машин: максимум площади плитки. */
    function complexTruckLayout(rackWidth, rackHeight, count) {
        var best = null;
        for (var cols = 1; cols <= count; cols += 1) {
            var fit = complexTileForGrid(rackWidth, rackHeight, cols, Math.ceil(count / cols));
            if (!fit) continue;
            if (!best || fit.area > best.area || (fit.area === best.area && fit.rows < best.rows)) {
                best = fit;
            }
        }
        return best;
    }

    /* Применяет к полю готовый размер плитки и число колонок и раскладывает
       по ним машины. Число колонок общее на всю доску: тогда сетки всех
       карточек одной ширины, прижаты к правому краю, а информационные панели
       слева получают одинаковую ширину и выстраиваются по одной вертикали. */
    function applyComplexTruckLayout(rack, tiles, empty, size, cols, rackHeight) {
        var rows = Math.max(1, Math.floor((rackHeight + COMPLEX_TILE_GAP) / (size.h + COMPLEX_TILE_GAP)));
        var capacity = cols * rows;
        var visible = tiles.length <= capacity ? tiles.length : Math.max(1, capacity - 1);

        tiles.forEach(function (tile, index) {
            tile.hidden = index >= visible;
        });

        var more = rack.querySelector(".complex-truck-more");
        var hiddenCount = tiles.length - visible;
        if (hiddenCount > 0) {
            if (!more) {
                more = document.createElement("b");
                more.className = "complex-truck-more";
                rack.appendChild(more);
            }
            more.hidden = false;
            more.textContent = "+" + hiddenCount;
            more.title = "Ещё самосвалов: " + hiddenCount;
        } else if (more) {
            more.hidden = true;
        }

        /* Пустые ячейки под недостающие машины: сколько нужно по составу
           сверх назначенных, но не больше свободных ячеек. Диспетчер видит,
           куда ещё ставить, а не просто пустое поле. Ячейки инертны и стоят
           сразу за плитками: сортировка вставляет плитки перед подписью
           пустого поля, поэтому и ячейки идут перед ней. На телефоне горного
           мастера у поля своя вёрстка, там ячеек нет. */
        var need = parseInt(rack.getAttribute("data-truck-need"), 10) || 0;
        var mobile = document.body.classList.contains("mining-master-mobile-screen");
        var free = capacity - visible - (hiddenCount > 0 ? 1 : 0);
        var slots = (mobile || !tiles.length) ? 0 : Math.max(0, Math.min(need - tiles.length, free));
        var ghosts = Array.from(rack.querySelectorAll(".complex-truck-slot"));
        while (ghosts.length < slots) {
            var ghost = document.createElement("i");
            ghost.className = "complex-truck-slot";
            ghost.setAttribute("aria-hidden", "true");
            ghost.textContent = "+";
            ghosts.push(ghost);
        }
        ghosts.forEach(function (ghost, index) {
            ghost.hidden = index >= slots;
            rack.insertBefore(ghost, empty && empty.parentNode === rack ? empty : null);
        });

        rack.classList.remove("truck-fill-1", "truck-fill-2", "truck-fill-3", "truck-fill-4");
        rack.classList.toggle("is-rich-trucks", size.h >= COMPLEX_TILE_RICH_MIN_H);
        rack.style.setProperty("--complex-truck-cols", String(cols));
        rack.style.setProperty("--complex-truck-gap", COMPLEX_TILE_GAP + "px");
        rack.style.setProperty("--complex-truck-w", size.w + "px");
        rack.style.setProperty("--complex-truck-h", size.h + "px");
        rack.style.setProperty("--complex-truck-font", Math.max(10, Math.round(size.h * 0.5)) + "px");
        rack.style.setProperty("--complex-truck-justify", "start");
        if (empty) empty.hidden = tiles.length > 0;
    }

    /* Ширина, которую поле может занять: внутренняя ширина карточки минус
       минимум информационной панели и зазор между ними. Поле стоит в колонке
       auto и само не знает, сколько ему можно, — считаем от карточки. */
    function complexTruckFieldWidth(rack) {
        var card = rack.closest(".dispatcher-complex-card");
        if (!card) return rack.clientWidth || 0;
        var cs = getComputedStyle(card);
        var inner = card.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
        var gap = parseFloat(cs.columnGap) || 0;
        return Math.max(0, inner - COMPLEX_INFO_MIN_W - gap);
    }

    /* Готовит поле к расчёту: сортирует плитки, заводит подпись пустого поля,
       возвращает измерения или null, если карточка ещё не разложена. */
    function measureComplexTruckRack(rack) {
        if (!rack) return null;
        sortDesktopEquipmentList(rack, ".complex-truck-tile");
        var tiles = Array.from(rack.querySelectorAll(".complex-truck-tile"));
        var empty = rack.querySelector(".complex-truck-empty");
        if (!empty && tiles.length === 0) {
            empty = document.createElement("em");
            empty.className = "complex-truck-empty";
            empty.textContent = "самосвалы не назначены";
            rack.appendChild(empty);
        }
        var width = complexTruckFieldWidth(rack);
        var height = rack.clientHeight || rack.getBoundingClientRect().height || 0;
        /* До первой раскладки карточки поле ещё нулевое. Считать по таким
           размерам нельзя: ёмкость выходит в одну ячейку и все машины
           сворачиваются в счётчик, а через кадр пересчёт даёт другое.
           Ждём реальных размеров — ResizeObserver вызовет пересчёт. */
        if (width < COMPLEX_TILE_MIN.w || height < COMPLEX_TILE_MIN.h) return null;
        return { rack: rack, tiles: tiles, empty: empty, width: width, height: height };
    }

    function refreshComplexTruckRack(rack) {
        /* Одиночный пересчёт всё равно идёт через доску: размер и число
           колонок общие, иначе одна карточка выбьется из строя. */
        refreshAllComplexTruckRacks();
    }

    /* Плитки одного размера и сетка одной ширины на всей доске.

       Если считать размер по каждой карточке отдельно, рядом оказываются
       плитки 168px и 70px: поле каждой карточки заполнено, но доска выглядит
       коллажем, и по размеру плиток уже нельзя на глаз сравнить загрузку
       комплексов — а диспетчер смотрит именно на доску целиком. Поэтому
       размер выбирается один на всех — самый скромный из нужных, то есть по
       самому загруженному комплексу, но не мельче нижней границы. Число
       колонок тоже общее — по нему CSS задаёт ширину сетки, а всё, что
       осталось слева, достаётся информационной панели. */
    function refreshAllComplexTruckRacks() {
        var measured = [];
        document.querySelectorAll(".complex-assigned-trucks").forEach(function (rack) {
            var m = measureComplexTruckRack(rack);
            if (m) measured.push(m);
        });
        if (!measured.length) return;

        var common = null;
        var maxCount = 0;
        measured.forEach(function (m) {
            if (!m.tiles.length) return;
            maxCount = Math.max(maxCount, m.tiles.length);
            var size = complexTruckLayout(m.width, m.height, m.tiles.length);
            if (!size) size = COMPLEX_TILE_MIN;
            if (!common || (size.w * size.h) < (common.w * common.h)) common = size;
        });
        if (!common) common = COMPLEX_TILE_MAX;
        if ((common.w * common.h) < (COMPLEX_TILE_FLOOR.w * COMPLEX_TILE_FLOOR.h)) {
            common = COMPLEX_TILE_FLOOR;
        }

        /* Колонок столько, сколько влезает по ширине, но не больше, чем нужно
           самой загруженной карточке: при шести машинах и четырёх колонках
           получалось бы 4+2, а 3+3 ровнее и отдаёт лишнюю ширину панели. */
        var width = measured[0].width;
        var height = measured[0].height;
        var fitCols = Math.max(1, Math.floor((width + COMPLEX_TILE_GAP) / (common.w + COMPLEX_TILE_GAP)));
        var fitRows = Math.max(1, Math.floor((height + COMPLEX_TILE_GAP) / (common.h + COMPLEX_TILE_GAP)));
        var needCols = Math.max(1, Math.ceil(Math.max(1, maxCount) / fitRows));
        var cols = Math.max(1, Math.min(fitCols, needCols));

        measured.forEach(function (m) {
            applyComplexTruckLayout(m.rack, m.tiles, m.empty, common, cols, m.height);
        });
    }

    /* Размер жетона зависит от размера полосы, поэтому следим именно за ним.
       Одного-двух кадров после DOMContentLoaded не хватает: карточки внутри
       холста раскладываются позже, и расчёт выходил на нулевых размерах, а
       жетоны до первого resize оставались гаражными. ResizeObserver закрывает
       и первую раскладку, и смену размера окна, и подгрузку шрифтов. */
    var complexRackResizeObserver = null;
    function watchComplexTruckRacks() {
        if (typeof ResizeObserver === "undefined") {
            requestAnimationFrame(function () {
                requestAnimationFrame(refreshAllComplexTruckRacks);
            });
            window.addEventListener("load", refreshAllComplexTruckRacks, { once: true });
            return;
        }
        if (!complexRackResizeObserver) {
            complexRackResizeObserver = new ResizeObserver(function () {
                refreshAllComplexTruckRacks();
            });
        }
        complexRackResizeObserver.disconnect();
        document.querySelectorAll(".complex-assigned-trucks").forEach(function (rack) {
            complexRackResizeObserver.observe(rack);
        });
    }
    /* Живой поиск техники: набранный номер подсвечивает все плитки этой
       машины — в комплексе, в гараже, карточку комплекса по экскаватору.
       Совпадение по началу номера, чтобы набор сужал круг; «к-2» и «k-2»
       одинаково находят комплекс по имени зоны. Доска после ответа сервера
       перерисовывается целиком — MutationObserver возвращает подсветку. */
    function bindDispatcherEquipmentSearch() {
        var input = document.querySelector("[data-dispatcher-equipment-search]");
        if (!input || document.body.classList.contains("mining-master-mobile-screen")) return;
        var box = input.closest("[data-dispatcher-equipment-search-box]") || input.parentElement;
        var count = document.querySelector("[data-dispatcher-equipment-search-count]");
        var query = "";

        function normalizeSearchText(value) {
            return String(value || "").trim().toLowerCase().replace(/k/g, "к").replace(/\s+/g, "");
        }

        function matchesSearch(node, needle) {
            var name = normalizeSearchText(node.getAttribute("data-equipment-name"));
            if (name && name.indexOf(needle) === 0) return true;
            var zone = normalizeSearchText(node.getAttribute("data-zone-label"));
            return !!zone && zone.indexOf(needle) === 0;
        }

        function applyEquipmentSearch() {
            var needle = normalizeSearchText(query);
            var hits = 0;
            var first = null;
            document.querySelectorAll(".dispatcher-shell [data-equipment-name]").forEach(function (node) {
                var hit = needle !== "" && matchesSearch(node, needle);
                node.classList.toggle("is-search-hit", hit);
                if (hit) {
                    hits += 1;
                    if (!first) first = node;
                }
            });
            document.body.classList.toggle("is-equipment-search", needle !== "");
            box.classList.toggle("has-query", needle !== "");
            if (count) {
                count.hidden = needle === "";
                count.textContent = String(hits);
                count.classList.toggle("is-none", hits === 0);
            }
            /* Гараж прокручивается — первое совпадение подтягиваем в кадр. */
            if (first && typeof first.scrollIntoView === "function") {
                first.scrollIntoView({ block: "nearest", inline: "nearest" });
            }
        }

        input.addEventListener("input", function () {
            query = input.value;
            applyEquipmentSearch();
        });
        input.addEventListener("keydown", function (event) {
            if (event.key === "Escape") clearEquipmentSearch();
        });

        function clearEquipmentSearch() {
            input.value = "";
            query = "";
            applyEquipmentSearch();
            if (document.activeElement === input) input.blur();
        }

        /* Набор без клика по полю: цифра или буква, нажатая когда фокус не в
           другом поле ввода и не открыт диалог, уходит в поиск — диспетчер
           просто начинает печатать номер. Backspace стирает так же. */
        function isTypingElsewhere() {
            var active = document.activeElement;
            if (!active || active === document.body || active === input) return false;
            var tag = active.tagName;
            return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || active.isContentEditable;
        }
        function isDialogOpen() {
            if (document.querySelector("dialog[open]")) return true;
            var modal = document.getElementById("app-confirm-modal");
            return !!(modal && !modal.hidden && getComputedStyle(modal).display !== "none");
        }
        document.addEventListener("keydown", function (event) {
            if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
            if (document.activeElement === input || isTypingElsewhere() || isDialogOpen()) return;
            var key = event.key;
            if (key.length === 1 && /[0-9a-zа-яё\-]/i.test(key)) {
                if (input.maxLength > 0 && input.value.length >= input.maxLength) return;
                input.value += key;
            } else if (key === "Backspace" && input.value) {
                input.value = input.value.slice(0, -1);
            } else {
                return;
            }
            event.preventDefault();
            query = input.value;
            applyEquipmentSearch();
            input.focus({ preventScroll: true });
            input.setSelectionRange(input.value.length, input.value.length);
        });

        /* Клик или захват мышью в любом месте вне поля снимает поиск:
           техника найдена, диспетчер тянет её или работает дальше. */
        document.addEventListener("pointerdown", function (event) {
            if (query === "" || box.contains(event.target)) return;
            clearEquipmentSearch();
        }, true);

        var pending = null;
        var observer = new MutationObserver(function () {
            if (query === "" || pending) return;
            pending = setTimeout(function () {
                pending = null;
                applyEquipmentSearch();
            }, 60);
        });
        observer.observe(document.querySelector(".dispatcher-shell") || document.body, { childList: true, subtree: true });
    }
    bindDispatcherEquipmentSearch();

    watchComplexTruckRacks();
    function findTruckGarageList() {
        return document.querySelector(".dispatcher-trucks");
    }
    function findComplexTruckRack(complexCard) {
        return complexCard ? complexCard.querySelector(".complex-assigned-trucks") : null;
    }
    function getFirstTruckGaragePlaceholder() {
        var garage = findTruckGarageList();
        return garage ? garage.querySelector(".dispatcher-truck-tile.is-placeholder") : null;
    }
    function moveDesktopTruckToGarage(tile) {
        var garage = findTruckGarageList();
        if (!garage || !tile) return false;
        removeDuplicateDesktopTruckTiles(tile.dataset.equipmentId, tile);
        tile.classList.remove("complex-truck-tile", "dispatcher-dragging");
        tile.removeAttribute("data-truck-tile");
        setDispatcherNodeEquipmentState(tile, "free", "truck");
        tile.dataset.dispatcherDrag = "truck";
        tile.dataset.garageItem = "truck";
        tile.setAttribute("draggable", "true");
        tile.removeAttribute("aria-disabled");
        delete tile.dataset.complexTruck;
        delete tile.dataset.assignedZone;
        var firstPlaceholder = getFirstTruckGaragePlaceholder();
        if (firstPlaceholder && firstPlaceholder.parentNode === garage) {
            garage.insertBefore(tile, firstPlaceholder);
        } else {
            garage.appendChild(tile);
        }
        bindDragTile(tile);
        bindEquipmentCardTrigger(tile);
        refreshDesktopBoardIntegrity();
        return true;
    }
    function moveDesktopTruckToComplex(tile, complexCard) {
        var rack = findComplexTruckRack(complexCard);
        if (!rack || !tile) return false;
        var sourceRack = tile.closest(".complex-assigned-trucks");
        var zoneId = complexCard.dataset.zoneId || "";
        removeDuplicateDesktopTruckTiles(tile.dataset.equipmentId, tile);
        tile.classList.add("complex-truck-tile");
        tile.setAttribute("data-truck-tile", "");
        tile.classList.remove("dispatcher-dragging", "is-placeholder");
        setDispatcherNodeEquipmentState(tile, "assigned", "truck");
        tile.dataset.dispatcherDrag = "truck";
        tile.dataset.complexTruck = "true";
        tile.dataset.assignedZone = zoneId;
        tile.setAttribute("draggable", "true");
        tile.removeAttribute("aria-disabled");
        delete tile.dataset.garageItem;
        var empty = rack.querySelector(".complex-truck-empty");
        if (empty) empty.hidden = true;
        rack.appendChild(tile);
        if (sourceRack && sourceRack !== rack) {
            refreshComplexTruckRack(sourceRack);
        }
        bindDragTile(tile);
        bindEquipmentCardTrigger(tile);
        refreshDesktopBoardIntegrity();
        return true;
    }
    function releaseDesktopComplexTrucks(complexCard) {
        var rack = findComplexTruckRack(complexCard);
        if (!rack) return false;
        var trucks = Array.from(rack.querySelectorAll(".complex-truck-tile"));
        if (!trucks.length) {
            refreshComplexTruckRack(rack);
            return true;
        }
        trucks.forEach(moveDesktopTruckToGarage);
        refreshComplexTruckRack(rack);
        refreshTruckGarage();
        return true;
    }
    function findExcavatorGarageList() {
        return document.querySelector(".dispatcher-excavators");
    }
    function getFirstExcavatorGaragePlaceholder() {
        var garage = findExcavatorGarageList();
        return garage ? garage.querySelector(".dispatcher-excavator-garage-tile.is-placeholder") : null;
    }
    function resetDesktopComplexCardToEmpty(complexCard) {
        if (!complexCard) return false;
        var zoneId = complexCard.dataset.zoneId || "";
        complexCard.className = "dispatcher-complex-card status-empty";
        complexCard.style.setProperty("--complex-progress", "0%");
        complexCard.dataset.dispatcherDrop = "complex";
        complexCard.dataset.zoneId = zoneId;
        delete complexCard.dataset.dispatcherDrag;
        delete complexCard.dataset.equipmentCardId;
        delete complexCard.dataset.sourceEquipmentCardId;
        delete complexCard.dataset.equipmentId;
        delete complexCard.dataset.equipmentName;
        delete complexCard.dataset.equipmentState;
        delete complexCard.dataset.excavatorSlot;
        delete complexCard.dataset.dragBound;
        delete complexCard.dataset.cardBound;
        complexCard.removeAttribute("role");
        complexCard.removeAttribute("tabindex");
        complexCard.removeAttribute("draggable");
        complexCard.innerHTML = '<div class="complex-empty" aria-hidden="true"></div>';
        return true;
    }
    function buildDesktopExcavatorGarageTile(complexCard) {
        var tile = document.createElement("article");
        var slot = complexCard.dataset.excavatorSlot || "";
        var name = complexCard.dataset.equipmentName || ("Экскаватор " + slot).trim();
        var cardId = complexCard.dataset.equipmentId || complexCard.dataset.equipmentCardId || complexCard.dataset.sourceEquipmentCardId || "";
        var stateCode = "garage";
        tile.className = "dispatcher-equipment-tile dispatcher-excavator-garage-tile " + dispatcherEquipmentStateClass(stateCode) + " is-sync-pending";
        tile.style.setProperty("--tile-progress", "0%");
        tile.setAttribute("role", "button");
        tile.setAttribute("tabindex", "0");
        tile.setAttribute("draggable", "true");
        tile.dataset.dispatcherDrag = "excavator";
        tile.dataset.equipmentCardId = cardId;
        tile.dataset.equipmentId = cardId;
        tile.dataset.equipmentName = name;
        tile.dataset.equipmentState = stateCode;
        tile.dataset.excavatorSlot = slot;
        tile.dataset.garageItem = "excavator";
        tile.innerHTML =
            (slot ? "<strong>" + escapeHtml(slot) + "</strong>" : "") +
            '<img src="' + escapeHtml(dispatcherNeutralEquipmentIcon("excavator")) + '" alt="">' +
            "<span>" + escapeHtml(dispatcherEquipmentStateLabel(stateCode)) + "</span>";
        return tile;
    }
    function moveDesktopComplexToExcavatorGarage(complexCard) {
        var garage = findExcavatorGarageList();
        if (!garage || !complexCard || complexCard.classList.contains("status-empty")) return false;
        releaseDesktopComplexTrucks(complexCard);
        var tile = buildDesktopExcavatorGarageTile(complexCard);
        var firstPlaceholder = getFirstExcavatorGaragePlaceholder();
        if (firstPlaceholder && firstPlaceholder.parentNode === garage) {
            garage.insertBefore(tile, firstPlaceholder);
        } else {
            garage.appendChild(tile);
        }
        resetDesktopComplexCardToEmpty(complexCard);
        bindDragTile(tile);
        bindEquipmentCardTrigger(tile);
        refreshExcavatorGarage();
        refreshTruckGarage();
        refreshAllComplexTruckRacks();
        normalizeComplexGrid();
        return true;
    }
    function activateDesktopComplexFromExcavatorTile(tile, complexCard) {
        if (!tile || !complexCard) return false;
        var targetCard = complexCard.classList.contains("status-empty")
            ? complexCard
            : document.querySelector(".dispatcher-complex-card.status-empty");
        if (!targetCard) return false;
        var zoneId = targetCard.dataset.zoneId || "К";
        var cardId = tile.dataset.equipmentId || tile.dataset.equipmentCardId || "";
        var name = tile.dataset.equipmentName || "";
        var slot = tile.dataset.excavatorSlot || (tile.querySelector("strong") && tile.querySelector("strong").textContent) || "";
        var zoneLabel = slot ? "K-" + slot : (targetCard.dataset.zoneLabel || "K");
        var stateCode = "assigned";
        targetCard.className = "dispatcher-complex-card " + dispatcherEquipmentStateClass(stateCode) + " is-sync-pending";
        targetCard.style.setProperty("--complex-progress", "0%");
        targetCard.dataset.dispatcherDrop = "complex";
        targetCard.dataset.zoneId = zoneId;
        targetCard.dataset.zoneLabel = zoneLabel;
        targetCard.dataset.dispatcherDrag = "complex";
        targetCard.dataset.equipmentCardId = cardId;
        targetCard.dataset.sourceEquipmentCardId = cardId;
        targetCard.dataset.equipmentId = cardId;
        targetCard.dataset.equipmentName = name;
        targetCard.dataset.equipmentState = stateCode;
        targetCard.dataset.excavatorSlot = slot;
        delete targetCard.dataset.dragBound;
        delete targetCard.dataset.cardBound;
        targetCard.setAttribute("role", "button");
        targetCard.setAttribute("tabindex", "0");
        targetCard.setAttribute("draggable", "true");
        targetCard.innerHTML =
            '<div class="complex-work-head">' +
                '<div class="complex-title-state">' +
                    "<h2>" + escapeHtml(zoneLabel) + "</h2>" +
                    '<span class="complex-state-chip">' + escapeHtml(dispatcherEquipmentStateLabel(stateCode)) + '</span>' +
                '</div>' +
                '<div class="complex-context">' +
                    '<span class="complex-info-chip chip-horizon">Гор. -</span>' +
                    '<span class="complex-info-chip chip-block">Блок -</span>' +
                    '<span class="complex-info-chip chip-rock">порода не указана</span>' +
                "</div>" +
            "</div>" +
            '<div class="complex-assigned-trucks truck-fill-1" style="--complex-truck-cols: 6;" data-truck-need="0" aria-label="Активные самосвалы комплекса">' +
                '<em class="complex-truck-empty">самосвалы не назначены</em>' +
            "</div>";
        if (!document.body.classList.contains("mining-master-mobile-screen")) {
            /* На настольном пульте карточка несёт полосу ключевых цифр, как в
               шаблоне; значений до ответа сервера нет — нули и прочерк, как у
               комплекса без плана. Единицу объёма берём у соседней карточки. */
            var unitNode = document.querySelector('.dispatcher-complex-card .complex-kpi[data-kpi="volume"] small');
            var unit = unitNode ? unitNode.textContent : "т";
            targetCard.querySelector(".complex-assigned-trucks").insertAdjacentHTML(
                "beforebegin",
                '<div class="complex-kpis" aria-label="Показатели комплекса">' +
                    '<span class="complex-kpi is-zero" data-kpi="trucks" title="Назначено самосвалов / нужно по составу"><i>Машин</i><b>0<small>/0</small></b></span>' +
                    '<span class="complex-kpi" data-kpi="volume" title="Погружено за смену"><i>Объём</i><b>0<small>' + escapeHtml(unit) + '</small></b></span>' +
                    '<span class="complex-kpi is-muted" data-kpi="plan" title="План не назначен"><i>План</i><b>&mdash;</b></span>' +
                "</div>"
            );
        }
        tile.remove();
        bindDragTile(targetCard);
        bindEquipmentCardTrigger(targetCard);
        refreshExcavatorGarage();
        refreshTruckGarage();
        refreshAllComplexTruckRacks();
        normalizeComplexGrid();
        return true;
    }
    function confirmDesktopOptimisticBoardAction(response) {
        if (response && response.queued) return response;
        markDispatcherLocalAssignmentApplied();
        return response;
    }
    function refreshDesktopBoardAfterStructuralAction(response, localFallback) {
        if (response && response.queued) {
            if (typeof localFallback === "function") {
                localFallback();
            }
            markDispatcherLocalAssignmentApplied();
            return response;
        }
        return refreshDispatcherDesktopBoardFromServer().then(function (applied) {
            if (!applied) {
                if (typeof localFallback === "function") {
                    localFallback();
                }
                markDispatcherLocalAssignmentApplied();
            }
            return response;
        }).catch(function (error) {
            if (typeof localFallback === "function") {
                localFallback();
                markDispatcherLocalAssignmentApplied();
            } else {
                throw error;
            }
            return response;
        });
    }
    function handleDesktopOptimisticBoardError(error) {
        showDispatcherDnDError(error);
        return refreshDispatcherDesktopBoardFromServer();
    }
    function applyDesktopTruckAction(response, action) {
        if (response && response.queued) return response;
        if (!action || !action.type) return response;
        if (action.truckTile) applyHaulAssignmentState(response, action.truckTile);
        if (action.complexCard) applyHaulAssignmentStates(response, action.complexCard);
        var applied = false;
        if (action.type === "assign") {
            applied = moveDesktopTruckToComplex(action.truckTile, action.complexCard);
        } else if (action.type === "release") {
            applied = moveDesktopTruckToGarage(action.truckTile);
        } else if (action.type === "release_complex") {
            applied = releaseDesktopComplexTrucks(action.complexCard);
        }
        if (!applied) {
            refreshDispatcherDesktopBoardFromServer().catch(reloadDispatcherBoardAsFallback);
        } else {
            markDispatcherLocalAssignmentApplied();
        }
        return response;
    }
    refreshExcavatorGarage();
    refreshTruckGarage();
    function bindDragTile(tile) {
        if (tile.dataset.dragBound === "true") return;
        tile.dataset.dragBound = "true";
        tile.addEventListener("dragstart", function (event) {
            if (!dispatcherShiftOpen) {
                event.preventDefault();
                draggedTile = null;
                return;
            }
            if (tile.dataset.complexTruck === "true") event.stopPropagation();
            if (tile.classList.contains("is-assigned") || tile.classList.contains("is-placeholder")) {
                event.preventDefault();
                draggedTile = null;
                return;
            }
            draggedTile = tile;
            tile.classList.add("dispatcher-dragging");
            if (board && tile.dataset.dispatcherDrag === "complex") {
                board.classList.add("is-complex-dragging");
                var progress = tile.querySelector(".equipment-progress-complex");
                if (progress) {
                    dragGhost = progress.cloneNode(true);
                    dragGhost.className = "dispatcher-drag-ghost";
                    document.body.appendChild(dragGhost);
                    event.dataTransfer.setDragImage(dragGhost, 28, 28);
                }
            }
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", tile.dataset.equipmentName || "");
        });
        tile.addEventListener("dragend", function (event) {
            if (tile.dataset.complexTruck === "true") event.stopPropagation();
            tile.classList.remove("dispatcher-dragging");
            if (board) board.classList.remove("is-complex-dragging");
            clearDragGhost();
            draggedTile = null;
            document.querySelectorAll(".dispatcher-drop-target").forEach(function (target) {
                target.classList.remove("dispatcher-drop-target");
            });
        });
    }
    function bindEquipmentCardTrigger(node) {
        if (!node || node.dataset.cardBound === "true") return;
        node.dataset.cardBound = "true";
        function isEquipmentCardTapBlocked(event) {
            var control = event.target && event.target.closest ? event.target.closest("button, a, input, form") : null;
            return node.classList.contains("is-placeholder") ||
                Boolean(control && control !== node);
        }
        function openBoundEquipmentCard(event) {
            if (!openEquipmentCard(node.dataset.equipmentCardId, node)) return false;
            if (event) {
                event.preventDefault();
                event.stopPropagation();
            }
            return true;
        }
        node.addEventListener("click", function (event) {
            if (isEquipmentCardTapBlocked(event)) return;
            if (node.dataset.complexTruck === "true") event.stopPropagation();
            openBoundEquipmentCard(event);
        });
        node.addEventListener("keydown", function (event) {
            if (event.key !== "Enter" && event.key !== " ") return;
            if (openEquipmentCard(node.dataset.equipmentCardId, node)) {
                event.preventDefault();
            }
        });
    }
    function bindDispatcherComplexDrop(zone) {
        if (!zone || zone.dataset.dispatcherDropBound === "true") return;
        zone.dataset.dispatcherDropBound = "true";
        zone.addEventListener("dragover", function (event) {
            if (!draggedTile) return;
            if (draggedTile.dataset.dispatcherDrag === "complex") return;
            event.preventDefault();
            zone.classList.add("dispatcher-drop-target");
        });
        zone.addEventListener("dragleave", function () {
            zone.classList.remove("dispatcher-drop-target");
        });
        zone.addEventListener("drop", function (event) {
            event.preventDefault();
            zone.classList.remove("dispatcher-drop-target");
            if (!draggedTile) return;
            if (draggedTile.dataset.dispatcherDrag === "complex") return;
            if (draggedTile.dataset.dispatcherDrag === "excavator") {
                var activatedExcavatorTile = draggedTile;
                var targetComplexCard = zone;
                dispatcherPost(dispatcherMoveExcavatorUrl, {
                    excavator_id: activatedExcavatorTile.dataset.equipmentId,
                    zone: "active",
                    expected_zone: "inactive"
                }, { queueOnNetworkFailure: false }).then(function (response) {
                    return refreshDesktopBoardAfterStructuralAction(response, function () {
                        activateDesktopComplexFromExcavatorTile(activatedExcavatorTile, targetComplexCard);
                    });
                }).catch(handleDesktopOptimisticBoardError);
                return;
            }
            if (draggedTile.dataset.dispatcherDrag === "truck") {
                if (!zone.dataset.equipmentId) {
                    return;
                }
                var assignedTruckTile = draggedTile;
                var targetComplexCard = zone;
                dispatcherPost(dispatcherAssignTruckUrl, {
                    action: "assign",
                    truck_id: assignedTruckTile.dataset.equipmentId,
                    excavator_id: zone.dataset.equipmentId,
                    expected_assignment_state_id: haulAssignmentStateId(assignedTruckTile)
                }).then(function (response) {
                    return applyDesktopTruckAction(response, {
                        type: "assign",
                        truckTile: assignedTruckTile,
                        complexCard: targetComplexCard
                    });
                }).catch(showDispatcherDnDError);
                return;
            }
        });
    }
    function bindDispatcherExcavatorGarageDrop(garage) {
        if (!garage || garage.dataset.dispatcherDropBound === "true") return;
        garage.dataset.dispatcherDropBound = "true";
        garage.addEventListener("dragover", function (event) {
            if (!draggedTile || draggedTile.dataset.dispatcherDrag !== "complex") return;
            event.preventDefault();
            garage.classList.add("dispatcher-drop-target");
        });
        garage.addEventListener("dragleave", function () {
            garage.classList.remove("dispatcher-drop-target");
        });
        garage.addEventListener("drop", function (event) {
            event.preventDefault();
            garage.classList.remove("dispatcher-drop-target");
            if (!draggedTile || draggedTile.dataset.dispatcherDrag !== "complex") return;
            var inactiveComplexCard = draggedTile;
            var inactiveExcavatorId = inactiveComplexCard.dataset.equipmentId;
            var inactiveComplexName = (inactiveComplexCard.dataset.equipmentName || "комплекс").trim();
            var inactiveTruckCount = inactiveComplexCard.querySelectorAll("[data-complex-truck='true']").length;
            requestDispatcherDesktopDangerConfirmation({
                title: "Расформировать комплекс?",
                message: "Экскаватор " + inactiveComplexName + " уйдет в гараж экскаваторов, а все самосвалы " +
                    "комплекса (" + inactiveTruckCount + " шт.) — в гараж самосвалов через 5 минут.",
                acceptLabel: "Расформировать",
                action: function () {
                    dispatcherPost(dispatcherMoveExcavatorUrl, {
                        excavator_id: inactiveExcavatorId,
                        zone: "inactive",
                        expected_zone: "active",
                        expected_assignment_states: collectComplexAssignmentStates(inactiveComplexCard)
                    }, { queueOnNetworkFailure: false }).then(function (response) {
                        return refreshDesktopBoardAfterStructuralAction(response, function () {
                            moveDesktopComplexToExcavatorGarage(inactiveComplexCard);
                        });
                    }).catch(handleDesktopOptimisticBoardError);
                }
            });
        });
    }
    if (detailSettingSave) {
        detailSettingSave.addEventListener("click", saveDetailSettings);
    }
    if (detailDestinationAdd) {
        detailDestinationAdd.addEventListener("click", function () {
            var usedIds = collectDetailDestinations().map(function (row) {
                return String(row.dump_point_id);
            });
            var nextPoint = detailDumpPointOptions.find(function (option) {
                return usedIds.indexOf(String(option.id)) === -1;
            });
            if (nextPoint) addDetailDestinationRow({dump_point_id: nextPoint.id});
        });
    }
    function requestDispatcherDesktopDangerConfirmation(options) {
        options = options || {};
        if (typeof options.action !== "function") return;
        if (typeof window.openAppConfirmDialog !== "function") {
            showDispatcherDnDError(new Error("Подтверждение действия недоступно. Обновите страницу."));
            return;
        }
        window.openAppConfirmDialog(
            options.message || "Подтвердить опасное действие?",
            options.action,
            0,
            options.acceptLabel || "Подтвердить",
            {
                confirmTitle: options.title || "Опасное действие",
                confirmDescription: options.message || "Подтвердите действие."
            }
        );
    }
    function bindDispatcherTruckGarageDrop(garage) {
        if (!garage || garage.dataset.dispatcherDropBound === "true") return;
        garage.dataset.dispatcherDropBound = "true";
        garage.addEventListener("dragover", function (event) {
            if (!draggedTile || (draggedTile.dataset.complexTruck !== "true" && draggedTile.dataset.dispatcherDrag !== "complex")) return;
            event.preventDefault();
            garage.classList.add("dispatcher-drop-target");
        });
        garage.addEventListener("dragleave", function () {
            garage.classList.remove("dispatcher-drop-target");
        });
        garage.addEventListener("drop", function (event) {
            event.preventDefault();
            garage.classList.remove("dispatcher-drop-target");
            if (!draggedTile) return;
            if (draggedTile.dataset.dispatcherDrag === "complex") {
                var complexCard = draggedTile;
                var complexName = (complexCard.dataset.equipmentName || "комплекса").trim();
                var truckCount = complexCard.querySelectorAll("[data-complex-truck='true']").length;
                requestDispatcherDesktopDangerConfirmation({
                    title: "Снять все самосвалы?",
                    message: "Снять все самосвалы комплекса " + complexName + " (" + truckCount +
                        " шт.)? Экскаватор останется на месте, самосвалы уйдут в гараж через 5 минут.",
                    acceptLabel: "Снять самосвалы",
                    action: function () {
                        dispatcherPost(dispatcherAssignTruckUrl, {
                            action: "release_complex",
                            excavator_id: complexCard.dataset.equipmentId,
                            expected_assignment_states: collectComplexAssignmentStates(complexCard)
                        }, { queueOnNetworkFailure: false }).then(function (response) {
                            return applyDesktopTruckAction(response, {
                                type: "release_complex",
                                complexCard: complexCard
                            });
                        }).catch(showDispatcherDnDError);
                    }
                });
                return;
            }
            if (draggedTile.dataset.complexTruck !== "true") return;
            var releasedTruckTile = draggedTile;
            dispatcherPost(dispatcherAssignTruckUrl, {
                action: "release",
                truck_id: releasedTruckTile.dataset.equipmentId,
                expected_assignment_state_id: haulAssignmentStateId(releasedTruckTile)
            }).then(function (response) {
                return applyDesktopTruckAction(response, {
                    type: "release",
                    truckTile: releasedTruckTile
                });
            }).catch(showDispatcherDnDError);
        });
    }
    function bindDispatcherDesktopInteractions() {
        board = document.querySelector(".dispatcher-board");
        excavatorGarage = document.querySelector("[data-dispatcher-excavator-garage]");
        document.querySelectorAll("[data-dispatcher-drag]").forEach(bindDragTile);
        document.querySelectorAll("[data-equipment-card-id]").forEach(bindEquipmentCardTrigger);
        normalizeComplexGrid();
        refreshExcavatorGarage();
        refreshTruckGarage();
        refreshAllComplexTruckRacks();
        if (!dispatcherShiftOpen) return;
        document.querySelectorAll("[data-dispatcher-drop='complex']").forEach(bindDispatcherComplexDrop);
        document.querySelectorAll("[data-dispatcher-drop='excavator-garage']").forEach(bindDispatcherExcavatorGarageDrop);
        document.querySelectorAll("[data-dispatcher-drop='truck-garage']").forEach(bindDispatcherTruckGarageDrop);
    }
    seedDispatcherBoardFingerprints(document.querySelector(".dispatcher-board"));
    bindDispatcherDesktopInteractions();
    window.addEventListener("resize", refreshAllComplexTruckRacks);
});
