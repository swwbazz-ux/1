(function () {
var staticPrefix = document.querySelector("[data-eo-shell]").dataset.eoStaticPrefix;
var compactExcavatorNotice = window.ExcavatorWorkFeedback.compactExcavatorNotice;
var showExcavatorNotice = window.ExcavatorWorkFeedback.showExcavatorNotice;
var playExcavatorSound = window.ExcavatorWorkFeedback.playExcavatorSound;
var playExcavatorVoice = window.ExcavatorWorkFeedback.playExcavatorVoice;
var playExcavatorEquipmentVoice = window.ExcavatorWorkFeedback.playExcavatorEquipmentVoice;
var refreshExcavatorWorkFromServer = window.ExcavatorWorkRuntime.refreshExcavatorWorkFromServer;
var invalidateExcavatorWorkRefresh = window.ExcavatorWorkRuntime.invalidateExcavatorWorkRefresh;
var bindExcavatorTransferCountdowns = window.ExcavatorWorkRuntime.bindExcavatorTransferCountdowns;
var excavatorInstallDeviceId = window.ExcavatorWorkRuntime.excavatorInstallDeviceId;
var newExcavatorFieldId = window.ExcavatorWorkRuntime.newExcavatorFieldId;
var legacyExcavatorFieldSequence = window.ExcavatorWorkRuntime.legacyExcavatorFieldSequence;
var renderExcavatorOfflineStatus = window.ExcavatorWorkRuntime.renderExcavatorOfflineStatus;
var ensureExcavatorFieldOutbox = window.ExcavatorWorkRuntime.ensureExcavatorFieldOutbox;

window.initExcavatorWorkShell = function () {
    var shell = document.querySelector("[data-eo-shell]");
    if (!shell) return;
    bindExcavatorTransferCountdowns(shell);
    if (shell.dataset.eoInitialized === "1") return;
    shell.dataset.eoInitialized = "1";

    var screens = shell.querySelectorAll("[data-eo-screen]");
    var tabs = shell.querySelectorAll("[data-eo-tab]");
    var assignmentInput = shell.querySelector("[data-eo-assignment-input]");
    var dumpInput = shell.querySelector("[data-eo-dump-input]");
    var dumpPointsInput = shell.querySelector("[data-eo-dump-points-input]");
    var transportDistanceInput = shell.querySelector("input[name='transport_distance_km']");
    var downtimeInput = shell.querySelector("[data-eo-downtime-input]");
    var activeDuration = shell.querySelector("[data-eo-active-duration]");
    var downtimeCard = shell.querySelector("[data-eo-active-downtime-id]");
    var downtimeIndicator = shell.querySelector("[data-eo-downtime-indicator]");
    var downtimeReasonButtons = shell.querySelectorAll("[data-eo-downtime-reason-id]");
    var closeEvent = shell.querySelector("[data-eo-close-event]");
    var downtimeActionPending = false;
    var closeEventLabel = closeEvent ? closeEvent.querySelector("[data-mobile-shift-label]") : null;
    var selectedTruck = shell.querySelector("[data-eo-truck-card].is-selected");
    var csrfToken = shell.querySelector("input[name='csrfmiddlewaretoken']") ? shell.querySelector("input[name='csrfmiddlewaretoken']").value : "";
    var settingsUrl = shell.dataset.eoSettingsUrl || "";
    var shiftUrl = shell.dataset.eoShiftUrl || "";
    var truckLoadedUrl = shell.dataset.eoTruckLoadedUrl || "";
    var truckLoadedCancelUrl = shell.dataset.eoTruckLoadedCancelUrl || "";
    var truckGreenIcon = shell.dataset.eoTruckGreenIcon || "";
    var fieldOutbox = ensureExcavatorFieldOutbox(shell);
    var freeBucketController = null;
    var activeQueueDrag = null;
    var shiftButton = shell.querySelector("[data-eo-shift-button]");
    var shiftLabel = shell.querySelector("[data-eo-shift-label]");
    var shiftScreen = shell.querySelector('[data-eo-screen="shift"]');
    var workScreen = shell.querySelector('[data-eo-screen="trucks"]');
    var shiftScroll = shell.querySelector("[data-eo-shift-scroll]");
    var shiftInputs = shell.querySelector("[data-eo-shift-inputs]");
    var shiftFuelInput = shell.querySelector("[data-eo-shift-fuel]");
    var shiftFuelLiters = shell.querySelector("[data-eo-shift-fuel-liters]");
    var shiftHoursInput = shell.querySelector("[data-eo-shift-engine-hours]");
    var shiftFormError = shell.querySelector("[data-eo-shift-form-error]");
    var shiftConfirmationModal = shell.querySelector("[data-eo-reading-confirmation]");
    var shiftConfirmationWarnings = shell.querySelector("[data-eo-reading-confirmation-warnings]");
    var shiftConfirmationBack = shell.querySelector("[data-eo-reading-confirmation-back]");
    var shiftConfirmationAccept = shell.querySelector("[data-eo-reading-confirmation-accept]");
    var faceScreen = shell.querySelector('[data-eo-screen="face"]');
    var faceContent = shell.querySelector("[data-eo-face-settings-available]");
    var syncBadge = shell.querySelector("[data-eo-offline-sync]");
    var syncBadgeHome = shell.querySelector("[data-eo-offline-sync-home]");
    var badgeAnchors = {
        trucks: workScreen && workScreen.querySelector(".eo-operator-role"),
        shift: shiftScreen && shiftScreen.querySelector(".mobile-shift__update-group"),
        face: faceScreen && faceScreen.querySelector(".mobile-shift__update-group")
    };
    var eventsScreen = shell.querySelector('[data-eo-screen="events"]');
    var shiftServerBlocked = !!(
        shiftButton
        && shiftButton.dataset.eoShiftServerBlocked === "true"
    );
    var shiftBlockMessage = shiftButton ? shiftButton.dataset.eoShiftBlockMessage || "" : "";
    var shiftHoldMs = 1000;
    var shiftHoldController = null;
    var logoutButton = shell.querySelector("[data-eo-logout-button]");
    var logoutLabel = shell.querySelector("[data-eo-logout-label]");
    var logoutHoldMs = 2000;

    function queueExcavatorFieldEvent(eventType, payload, options) {
        options = options || {};
        if (!fieldOutbox) return Promise.reject(new Error("Локальное сохранение недоступно."));
        return fieldOutbox.pending().then(function (events) {
            var dependencyTypes = options.dependencyTypes || [];
            var predecessors = events.filter(function (event) {
                return (!dependencyTypes.length || dependencyTypes.indexOf(event.event_type) >= 0)
                    && String(event.shift_id || "") === String(shell.dataset.nativeShiftId || "")
                    && ["conflict", "invalid", "auth_required"].indexOf(event.sync_state) < 0;
            }).sort(function (left, right) { return Number(right.sequence || 0) - Number(left.sequence || 0); });
            var dependencies = Array.isArray(options.dependsOn)
                ? options.dependsOn.slice()
                : (options.noDependency
                    ? []
                    : (options.dependsOnAll
                        ? predecessors.map(function (event) { return event.event_id; })
                        : (predecessors[0] ? [predecessors[0].event_id] : [])));
            var deviceId = excavatorInstallDeviceId();
            var eventId = newExcavatorFieldId(options.idPrefix || "excavator-event");
            return fieldOutbox.allocateSequence(legacyExcavatorFieldSequence(shell.dataset.eoAccessId || "", deviceId)).then(function (sequence) {
                var event = {
                event_id: eventId,
                event_type: eventType,
                format_version: 1,
                actor_id: Number(shell.dataset.eoEmployeeId || 0),
                access_id: Number(shell.dataset.eoAccessId || 0),
                role_code: shell.dataset.eoRoleCode || "excavator_operator",
                device_id: deviceId,
                shift_id: Number(shell.dataset.nativeShiftId || 0),
                equipment_id: Number(shell.dataset.eoCurrentExcavatorId || shell.dataset.eoShiftExcavatorId || 0),
                trip_id: options.tripId || null,
                local_trip_id: options.localTripId || null,
                local_downtime_id: options.localDowntimeIdFromEvent
                    ? eventId
                    : (options.localDowntimeId || null),
                occurred_at: new Date().toISOString(),
                sequence: sequence,
                depends_on: dependencies,
                payload: payload || {}
                };
                return fieldOutbox.queue(event).then(function () {
                    if (navigator.onLine !== false) fieldOutbox.flush();
                    return event;
                });
            });
        });
    }

    function applyLocalShiftClose() {
        shell.dataset.eoOfflineShiftClosed = "true";
        if (workScreen) workScreen.dataset.eoWorkAvailable = "false";
        if (eventsScreen) eventsScreen.dataset.eoDowntimeAvailable = "false";
        if (faceContent) faceContent.dataset.eoFaceSettingsAvailable = "false";
        shell.querySelectorAll("[data-eo-truck-card], [data-eo-dump-target], [data-eo-downtime-reason-id], [data-eo-apply-settings]").forEach(function (control) {
            control.disabled = true;
            control.setAttribute("aria-disabled", "true");
        });
        if (shiftButton) {
            shiftButton.disabled = true;
            shiftButton.classList.remove("is-pending");
            shiftButton.classList.add("is-disabled");
            shiftButton.setAttribute("aria-disabled", "true");
        }
        if (shiftLabel) shiftLabel.textContent = "Закрытие сохранено";
        activateTab("shift");
    }
    var truckDetailLayer = document.querySelector("[data-eo-truck-detail]");
    var truckDetailIconSlot = document.querySelector("[data-eo-truck-detail-icon-slot]");
    var truckDetailType = document.querySelector("[data-eo-truck-detail-type]");
    var truckDetailTitle = document.querySelector("[data-eo-truck-detail-title]");
    var truckDetailStatus = document.querySelector("[data-eo-truck-detail-status]");
    var truckDetailZone = document.querySelector("[data-eo-truck-detail-zone]");
    var truckDetailEmployee = document.querySelector("[data-eo-truck-detail-employee]");
    var truckDetailEmployeeImg = document.querySelector("[data-eo-truck-detail-employee-img]");
    var truckDetailEmployeeInitials = document.querySelector("[data-eo-truck-detail-employee-initials]");
    var truckDetailEmployeeName = document.querySelector("[data-eo-truck-detail-employee-name]");
    var truckDetailEmployeePhone = document.querySelector("[data-eo-truck-detail-employee-phone]");
    var truckDetailList = document.querySelector("[data-eo-truck-detail-list]");
    var truckDetailShiftReport = document.querySelector("[data-eo-truck-detail-shift-report]");
    var truckDetailMetrics = document.querySelector("[data-eo-truck-detail-metrics]");
    var truckDetailDashboard = document.querySelector("[data-eo-truck-detail-dashboard]");

    function escapeHtml(value) {
        return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
            return {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;"
            }[char];
        });
    }

    function readTruckDetailCards() {
        var node = document.getElementById("eo-truck-detail-cards-data");
        if (!node) return {};
        try {
            return JSON.parse(node.textContent || "{}") || {};
        } catch (error) {
            return {};
        }
    }

    function closeTruckDetailCard() {
        if (!truckDetailLayer) return;
        truckDetailLayer.hidden = true;
        truckDetailLayer.setAttribute("aria-hidden", "true");
        delete truckDetailLayer.dataset.eoTruckDetailActiveId;
    }

    function normalizeTruckDetailStatus(status) {
        var known = ["green", "yellow", "blue", "orange", "red", "gray"];
        return known.indexOf(status) >= 0 ? status : "gray";
    }

    function renderTruckDetailIcon(data) {
        if (!truckDetailIconSlot) return;
        var status = normalizeTruckDetailStatus(data.status_key);
        truckDetailIconSlot.innerHTML = "";
        var icon = document.createElement("div");
        icon.className = "mm-detail-icon status-" + status;
        icon.style.setProperty("--tile-progress", String(data.percent || 0) + "%");
        icon.innerHTML =
            "<strong>" + escapeHtml(data.number || data.label || "") + "</strong>" +
            '<img src="' + escapeHtml(staticPrefix + (data.icon || "img/equipment/truck-gray.png")) + '" alt="">' +
            "<span>" + escapeHtml(data.status_label || "") + "</span>";
        truckDetailIconSlot.appendChild(icon);
    }

    function renderTruckDetailEmployee(employee) {
        if (!truckDetailEmployee) return;
        employee = employee || {};
        truckDetailEmployee.hidden = false;
        if (truckDetailEmployeeName) truckDetailEmployeeName.textContent = employee.name || "Водитель не назначен";
        if (truckDetailEmployeePhone) {
            truckDetailEmployeePhone.textContent = employee.phone || "телефон не указан";
            if (employee.phone) {
                truckDetailEmployeePhone.href = "tel:" + employee.phone.replace(/[^\d+]/g, "");
                truckDetailEmployeePhone.removeAttribute("aria-disabled");
            } else {
                truckDetailEmployeePhone.removeAttribute("href");
                truckDetailEmployeePhone.setAttribute("aria-disabled", "true");
            }
        }
        if (truckDetailEmployeeImg && truckDetailEmployeeInitials) {
            if (employee.photo) {
                truckDetailEmployeeImg.src = employee.photo;
                truckDetailEmployeeImg.hidden = false;
                truckDetailEmployeeInitials.hidden = true;
            } else {
                truckDetailEmployeeImg.removeAttribute("src");
                truckDetailEmployeeImg.hidden = true;
                truckDetailEmployeeInitials.textContent = employee.initials || "--";
                truckDetailEmployeeInitials.hidden = false;
            }
        }
    }

    function renderTruckDetailRows(rows) {
        if (!truckDetailList) return;
        truckDetailList.innerHTML = "";
        (rows || []).forEach(function (row) {
            if (!row || !row.value) return;
            var term = document.createElement("dt");
            var value = document.createElement("dd");
            term.textContent = row.label || "";
            value.textContent = row.value || "";
            truckDetailList.appendChild(term);
            truckDetailList.appendChild(value);
        });
    }

    function renderTruckDetailReport(report) {
        if (!truckDetailShiftReport || !truckDetailMetrics || !truckDetailDashboard) return;
        var metrics = (report && report.metrics) || [];
        var charts = ((report && report.charts) || []).filter(function (chart) {
            return chart && chart.rows && chart.rows.length;
        });
        truckDetailMetrics.innerHTML = "";
        truckDetailDashboard.innerHTML = "";
        metrics.forEach(function (metric) {
            if (!metric || !metric.value) return;
            var item = document.createElement("div");
            var label = document.createElement("span");
            var value = document.createElement("strong");
            label.textContent = metric.label || "";
            value.textContent = metric.value || "";
            item.appendChild(label);
            item.appendChild(value);
            truckDetailMetrics.appendChild(item);
        });
        charts.slice(0, 2).forEach(function (chart) {
            var card = document.createElement("div");
            card.className = "gd-detail-chart-card gd-detail-chart-bar";
            var title = document.createElement("div");
            title.className = "gd-detail-report-title";
            title.textContent = chart.title || "";
            card.appendChild(title);
            chart.rows.slice(0, 4).forEach(function (row) {
                var line = document.createElement("div");
                line.className = "gd-detail-chart-row accent-" + (row.accent || "green");
                line.style.setProperty("--bar-pct", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
                line.innerHTML =
                    "<div class=\"gd-detail-chart-head\"><strong>" + escapeHtml(row.label || row.source || row.target || "") + "</strong><span>" + escapeHtml(row.value || "") + "</span></div>" +
                    "<div class=\"gd-detail-chart-meta\">" + escapeHtml(row.meta || row.target || "") + "</div>" +
                    "<div class=\"gd-detail-chart-bar\"><i></i></div>";
                card.appendChild(line);
            });
            truckDetailDashboard.appendChild(card);
        });
        truckDetailShiftReport.hidden = metrics.length === 0 && charts.length === 0;
    }

    function openTruckDetailCard(cardId) {
        var data = readTruckDetailCards()[String(cardId || "")];
        if (!data || !truckDetailLayer) return false;
        truckDetailLayer.dataset.eoTruckDetailActiveId = String(cardId || "");
        renderTruckDetailIcon(data);
        if (truckDetailType) truckDetailType.textContent = data.type || "Самосвал";
        if (truckDetailTitle) truckDetailTitle.textContent = data.label || data.number || "Самосвал";
        if (truckDetailStatus) truckDetailStatus.textContent = data.status_label || "";
        if (truckDetailZone) truckDetailZone.textContent = data.zone || "";
        renderTruckDetailEmployee(data.employee || {});
        renderTruckDetailRows(data.details || []);
        renderTruckDetailReport(data.shift_report || {});
        truckDetailLayer.hidden = false;
        truckDetailLayer.setAttribute("aria-hidden", "false");
        return true;
    }

    if (truckDetailLayer && truckDetailLayer.dataset.eoTruckDetailBound !== "1") {
        truckDetailLayer.dataset.eoTruckDetailBound = "1";
        truckDetailLayer.querySelectorAll("[data-eo-truck-detail-close]").forEach(function (node) {
            node.addEventListener("click", closeTruckDetailCard);
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") closeTruckDetailCard();
        });
    }

    function isInactiveTruck(card) {
        return !!(card && card.dataset.eoTruckInactive === "1");
    }

    function isTruckLoadBlocked(card) {
        return !!(card && card.dataset.eoCanLoad === "0");
    }

    function truckLoadBlockReason(card) {
        return (card && card.dataset.eoLoadBlockReasonLabel) || "Самосвал недоступен для погрузки";
    }

    function canTruckLoad(card) {
        return !!(card && !card.disabled && !card.classList.contains("is-pending") && (
            (!isInactiveTruck(card) && !isTruckLoadBlocked(card))
            || (card.dataset.eoManualAvailable === "1" && card.classList.contains("is-picked-up"))
        ));
    }

    function generateClientActionId(prefix) {
        var randomPart = "";
        if (window.crypto && window.crypto.getRandomValues) {
            var values = new Uint32Array(2);
            window.crypto.getRandomValues(values);
            randomPart = values[0].toString(36) + values[1].toString(36);
        } else {
            randomPart = Math.random().toString(36).slice(2);
        }
        return prefix + "-" + Date.now().toString(36) + "-" + randomPart;
    }

    function isShiftCloseAction() {
        return !!(shiftButton && (shiftButton.dataset.eoShiftAction || "toggle") === "close");
    }

    function resetShiftHold() {
        if (shiftHoldController) shiftHoldController.reset();
    }

    /* Открытие и закрытие смены подтверждаются одинаковым удержанием в одну
       секунду: этого достаточно против случайного касания без задержки работы. */
    function currentShiftHoldMs() {
        return isShiftCloseAction() ? shiftHoldMs : 1000;
    }

    function formatDowntimeDuration(totalSeconds) {
        var seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        var hours = Math.floor(seconds / 3600);
        var minutes = Math.floor((seconds % 3600) / 60);
        var remain = seconds % 60;
        return String(hours).padStart(2, "0") + ":" + String(minutes).padStart(2, "0") + ":" + String(remain).padStart(2, "0");
    }

    function clearDowntimeTimer() {
        if (window.eoDowntimeTimerId) {
            window.clearInterval(window.eoDowntimeTimerId);
            window.eoDowntimeTimerId = null;
        }
    }

    function renderReasonDuration(button, totalSeconds, isActive) {
        if (!button) return;
        var duration = button.querySelector("[data-eo-reason-duration]");
        if (!duration) return;
        var seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        var isVisible = seconds > 0 || !!isActive;
        duration.hidden = !isVisible;
        button.classList.toggle("is-used", isVisible);
        if (isVisible) {
            duration.textContent = formatDowntimeDuration(seconds);
        }
        if (eventsScreen && eventsScreen.dataset.eoDowntimeAvailable === "true") {
            var reasonName = button.dataset.eoReasonName || button.dataset.eoReason || "Причина простоя";
            var actionLabel = isActive ? "Активный простой" : "Начать простой";
            button.setAttribute(
                "aria-label",
                actionLabel + ": " + reasonName + (isVisible ? ". За смену: " + formatDowntimeDuration(seconds) : "")
            );
        }
    }

    function startDowntimeTimer(payload) {
        clearDowntimeTimer();
        payload = payload || {};
        var isActive = !!payload.active;
        var activeReasonId = isActive ? String(payload.reason_id || "") : "";
        var activeCountsTowardsShift = isActive && payload.active_counts_towards_shift !== false;
        var shiftBaseSeconds = Math.max(0, Math.floor(Number(payload.shift_total_seconds) || 0));
        var reasonTotals = payload.reason_totals;
        if (reasonTotals && typeof reasonTotals === "object") {
            downtimeReasonButtons.forEach(function (button) {
                var reasonId = String(button.dataset.eoDowntimeReasonId || "");
                button.dataset.eoReasonSeconds = String(Math.max(0, Math.floor(Number(reasonTotals[reasonId]) || 0)));
            });
        }
        var syncedAtMs = Date.now();
        function tick() {
            var liveSeconds = activeCountsTowardsShift ? Math.max(0, Math.floor((Date.now() - syncedAtMs) / 1000)) : 0;
            var shiftSeconds = shiftBaseSeconds + liveSeconds;
            if (activeDuration) {
                activeDuration.textContent = formatDowntimeDuration(shiftSeconds);
            }
            if (downtimeCard) {
                downtimeCard.dataset.eoShiftDowntimeSeconds = String(shiftSeconds);
                downtimeCard.setAttribute("aria-label", "Простои за смену: " + formatDowntimeDuration(shiftSeconds));
            }
            downtimeReasonButtons.forEach(function (button) {
                var reasonId = String(button.dataset.eoDowntimeReasonId || "");
                var baseSeconds = Math.max(0, Math.floor(Number(button.dataset.eoReasonSeconds) || 0));
                var reasonIsActive = isActive && reasonId === activeReasonId;
                renderReasonDuration(button, baseSeconds + (reasonIsActive ? liveSeconds : 0), reasonIsActive);
            });
        }
        tick();
        if (activeCountsTowardsShift) {
            window.eoDowntimeTimerId = window.setInterval(tick, 1000);
        }
    }

    function setDowntimeStatusClass(statusKey) {
        if (!downtimeCard) return;
        ["status-gray", "status-yellow", "status-green", "status-blue", "status-orange", "status-red"].forEach(function (className) {
            downtimeCard.classList.remove(className);
            if (downtimeIndicator) downtimeIndicator.classList.remove(className);
        });
        var nextClass = "status-" + (statusKey || "yellow");
        downtimeCard.classList.add(nextClass);
        if (downtimeIndicator) downtimeIndicator.classList.add(nextClass);
    }

    function applyActiveDowntime(payload) {
        if (!downtimeCard || !payload || !payload.event_id) return;
        downtimeCard.dataset.eoActiveDowntimeId = payload.event_id;
        downtimeCard.dataset.eoActiveReasonId = String(payload.reason_id || "");
        downtimeCard.dataset.eoActiveStartedAt = payload.started_at || "";
        downtimeCard.dataset.eoActiveElapsedSeconds = String(payload.elapsed_seconds || 0);
        downtimeCard.dataset.eoActiveCountsTowardsShift = payload.active_counts_towards_shift === false ? "false" : "true";
        downtimeCard.dataset.eoDowntimeCalculatedAt = payload.calculated_at || "";
        downtimeCard.dataset.eoDowntimeState = payload.equipment_state_code || "waiting";
        downtimeCard.classList.add("is-active");
        setDowntimeStatusClass(payload.status_key || "yellow");
        startDowntimeTimer(payload);
        downtimeReasonButtons.forEach(function (item) {
            item.classList.toggle("is-selected", String(item.dataset.eoDowntimeReasonId || "") === String(payload.reason_id || ""));
        });
        if (closeEvent) {
            closeEvent.disabled = false;
            closeEvent.classList.remove("is-disabled");
            closeEvent.classList.remove("is-pending");
            closeEvent.removeAttribute("aria-disabled");
            closeEvent.setAttribute("aria-label", "Завершить активный простой");
            if (closeEventLabel) closeEventLabel.textContent = "Завершить простой";
        }
    }

    function clearActiveDowntime(payload) {
        if (downtimeInput) downtimeInput.value = "";
        if (downtimeCard) {
            downtimeCard.dataset.eoActiveDowntimeId = "";
            downtimeCard.dataset.eoActiveReasonId = "";
            downtimeCard.dataset.eoActiveStartedAt = "";
            downtimeCard.dataset.eoActiveElapsedSeconds = "0";
            downtimeCard.dataset.eoActiveCountsTowardsShift = "false";
            downtimeCard.dataset.eoDowntimeCalculatedAt = (payload && payload.calculated_at) || "";
            downtimeCard.dataset.eoDowntimeState = "";
            downtimeCard.classList.remove("is-active");
            setDowntimeStatusClass("gray");
        }
        startDowntimeTimer(payload);
        if (closeEvent) {
            closeEvent.disabled = true;
            closeEvent.classList.add("is-disabled");
            closeEvent.classList.remove("is-pending");
            closeEvent.setAttribute("aria-disabled", "true");
            closeEvent.setAttribute("aria-label", "Завершение недоступно: активного простоя нет");
            if (closeEventLabel) closeEventLabel.textContent = "Завершить простой";
        }
        downtimeReasonButtons.forEach(function (item) {
            item.classList.remove("is-selected");
        });
    }

    function pickFirstActiveTruck() {
        var firstCard = shell.querySelector('[data-eo-truck-card][data-eo-can-load="1"]:not([data-eo-truck-inactive="1"])');
        if (firstCard) {
            selectTruck(firstCard);
        } else if (assignmentInput) {
            assignmentInput.value = "";
        }
    }

    var excavatorTabPreferenceKey = "excavator-active-tab-v1:" +
        String(shell.dataset.eoAccessId || "anonymous");

    function isKnownExcavatorTab(name) {
        var known = false;
        screens.forEach(function (screen) {
            if (screen.dataset.eoScreen === name) known = true;
        });
        return known;
    }

    function restoredExcavatorTab(fallback) {
        var requested = "";
        try {
            requested = new URL(window.location.href).searchParams.get("tab") || "";
        } catch (error) {
            requested = "";
        }
        if (isKnownExcavatorTab(requested)) return requested;
        try {
            requested = window.localStorage.getItem(excavatorTabPreferenceKey) || "";
        } catch (error) {
            requested = "";
        }
        return isKnownExcavatorTab(requested) ? requested : fallback;
    }

    function rememberExcavatorTab(name) {
        // Keep a deep link in step with navigation across fragment and page reloads.
        var url = new URL(window.location.href);
        if (url.searchParams.has("tab") && url.searchParams.get("tab") !== name) {
            url.searchParams.set("tab", name);
            window.history.replaceState(window.history.state, "", url);
        }
        try {
            window.localStorage.setItem(excavatorTabPreferenceKey, name);
        } catch (error) {
            /* The tab still works when storage is blocked. */
        }
    }

    function activateTab(name) {
        /* Экран с таким именем должен существовать, иначе спрячутся все и
           человек увидит пустоту. Так уже случалось: после настроек забоя код
           переключал на «downtime», а вкладка простоя называется «events». */
        if (!isKnownExcavatorTab(name)) {
            name = "trucks";
        }
        var badgeAnchor = badgeAnchors[name] || syncBadgeHome;
        if (downtimeEdge) downtimeEdge.reset();
        if (syncBadge && syncBadgeHome && badgeAnchor && syncBadge.nextElementSibling !== badgeAnchor) {
            badgeAnchor.before(syncBadge);
        }
        screens.forEach(function (screen) {
            screen.hidden = screen.dataset.eoScreen !== name;
        });
        tabs.forEach(function (tab) {
            var isActive = tab.dataset.eoTab === name;
            tab.classList.toggle("is-active", isActive);
            if (isActive) {
                tab.setAttribute("aria-current", "page");
            } else {
                tab.removeAttribute("aria-current");
            }
        });
        shell.dataset.eoActiveTab = name;
        rememberExcavatorTab(name);
        if (downtimeEdge) downtimeEdge.syncNative();
    }

    /* EXCAVATOR_TAB_SETTLE_START */
    var excavatorTabSettleGeneration = 0;
    var excavatorTabSettleFirstFrame = null;
    var excavatorTabSettleSecondFrame = null;

    function clearExcavatorTabSettleFrames() {
        if (excavatorTabSettleFirstFrame !== null) {
            window.cancelAnimationFrame(excavatorTabSettleFirstFrame);
            excavatorTabSettleFirstFrame = null;
        }
        if (excavatorTabSettleSecondFrame !== null) {
            window.cancelAnimationFrame(excavatorTabSettleSecondFrame);
            excavatorTabSettleSecondFrame = null;
        }
    }

    function scheduleExcavatorTabSettle(name) {
        excavatorTabSettleGeneration += 1;
        var generation = excavatorTabSettleGeneration;
        clearExcavatorTabSettleFrames();
        if (name !== "events") return;
        if (!eventsScreen || eventsScreen.dataset.eoDowntimeAvailable !== "true") return;

        excavatorTabSettleFirstFrame = window.requestAnimationFrame(function () {
            excavatorTabSettleFirstFrame = null;
            if (generation !== excavatorTabSettleGeneration || !shell.isConnected) return;
            excavatorTabSettleSecondFrame = window.requestAnimationFrame(function () {
                excavatorTabSettleSecondFrame = null;
                if (generation !== excavatorTabSettleGeneration || !shell.isConnected) return;
                if (shell.dataset.eoActiveTab !== "events") return;
                syncDowntimeStatus();
            });
        });
    }
    /* EXCAVATOR_TAB_SETTLE_END */

    function selectTruck(card) {
        if (!card) return;
        if (!canTruckLoad(card)) {
            return;
        }
        shell.querySelectorAll("[data-eo-truck-card]").forEach(function (item) {
            item.classList.remove("is-selected");
        });
        card.classList.add("is-selected");
        selectedTruck = card;
        if (assignmentInput) {
            assignmentInput.value = card.dataset.assignmentId || "";
        }
    }

    function selectDump(id) {
        if (!id) return;
        shell.querySelectorAll("[data-eo-dump-target]").forEach(function (item) {
            item.classList.toggle("is-selected", item.dataset.eoDumpTarget === id);
            if (item.dataset.eoDumpTarget === id && transportDistanceInput) {
                transportDistanceInput.value = item.dataset.eoDumpDistance || "";
            }
        });
        if (dumpInput) {
            dumpInput.value = id;
        }
    }

    function markLastDumpTarget(target) {
        if (!target) return;
        shell.querySelectorAll("[data-eo-dump-target].is-last-dump").forEach(function (item) {
            item.classList.remove("is-last-dump");
        });
        target.classList.add("is-last-dump");
        selectDump(target.dataset.eoDumpTarget);
    }

    function pendingBadgeList(dumpTarget) {
        return dumpTarget ? dumpTarget.querySelector(".eo-dashboard-unload-badges") : null;
    }

    function removeMutedPendingBadge(list) {
        if (!list) return;
        list.querySelectorAll(".is-muted").forEach(function (item) {
            item.remove();
        });
    }

    function ensureMutedPendingBadge(list) {
        if (!list || list.querySelector("[data-eo-queue-truck]")) return;
        if (!list.querySelector(".is-muted")) {
            var muted = document.createElement("b");
            muted.className = "is-muted";
            muted.textContent = "нет";
            list.appendChild(muted);
        }
    }

    function markLatestPendingTruckBadge(list, latestBadge) {
        if (!list) return null;
        var badges = Array.prototype.slice.call(list.querySelectorAll("[data-eo-queue-truck]"));
        var latest = latestBadge && badges.indexOf(latestBadge) !== -1 ? latestBadge : (badges[0] || null);
        badges.forEach(function (badge) {
            var isLatest = badge === latest;
            badge.classList.toggle("is-last-sent-truck", isLatest);
            badge.dataset.eoLastSentTruck = isLatest ? "true" : "false";
        });
        var dumpTarget = list.closest ? list.closest("[data-eo-dump-target]") : null;
        if (dumpTarget) dumpTarget.dataset.eoHasPendingTrucks = latest ? "true" : "false";
        return latest;
    }

    function latestPendingTruckBadge(dumpTarget) {
        if (!dumpTarget) return null;
        var marked = dumpTarget.querySelector('[data-eo-queue-truck][data-eo-last-sent-truck="true"]');
        return marked || dumpTarget.querySelector("[data-eo-queue-truck]");
    }

    function addPendingTruckBadge(dumpTarget, card, tripId) {
        var list = pendingBadgeList(dumpTarget);
        if (!list || !card) return null;
        var truckId = card.dataset.truckId || "";
        removeMutedPendingBadge(list);
        list.querySelectorAll('[data-eo-queue-truck][data-truck-id="' + truckId + '"]').forEach(function (item) {
            item.remove();
        });
        var badge = document.createElement("b");
        badge.className = "status-green";
        badge.dataset.eoQueueTruck = "1";
        badge.dataset.truckId = truckId;
        badge.dataset.tripId = tripId || "";
        badge.dataset.eoTruckNumber = card.querySelector("strong") ? card.querySelector("strong").textContent.trim() : "";
        if (
            card.dataset.eoTransferDirection === "outgoing"
            && card.dataset.eoTransferDeadline
        ) {
            badge.dataset.eoTransferPreviewId = card.dataset.eoTransferId || "";
            badge.dataset.eoTransferPreviewDeadline = card.dataset.eoTransferDeadline;
        }
        badge.textContent = badge.dataset.eoTruckNumber || truckId;
        list.appendChild(badge);
        markLatestPendingTruckBadge(list, badge);
        return badge;
    }

    function removePendingTruckBadge(dumpTarget, truckId, tripId) {
        var list = pendingBadgeList(dumpTarget);
        if (!list) return;
        var selector = "[data-eo-queue-truck]";
        list.querySelectorAll(selector).forEach(function (item) {
            var sameTruck = truckId && item.dataset.truckId === String(truckId);
            var sameTrip = tripId && item.dataset.tripId === String(tripId);
            if (tripId ? sameTrip : sameTruck) item.remove();
        });
        markLatestPendingTruckBadge(list);
        ensureMutedPendingBadge(list);
    }

    function updatePendingTruckBadgeTrip(dumpTarget, truckId, tripId) {
        var list = pendingBadgeList(dumpTarget);
        if (!list) return;
        var badge = list.querySelector('[data-eo-queue-truck][data-truck-id="' + truckId + '"]');
        if (badge && tripId) {
            badge.dataset.tripId = String(tripId);
        }
    }

    function bindManualDumpCardExpiry(shellRoot) {
        if (window.eoManualQueuePreviewTimer) {
            window.clearInterval(window.eoManualQueuePreviewTimer);
            window.eoManualQueuePreviewTimer = null;
        }
        if (!shellRoot) return;
        var badges = Array.prototype.slice.call(
            shellRoot.querySelectorAll("[data-eo-queue-truck][data-eo-manual-preview-expires-at]")
        ).map(function (badge) {
            return {
                badge: badge,
                tripId: String(badge.dataset.tripId || ""),
                deadline: Date.parse(badge.dataset.eoManualPreviewExpiresAt || "")
            };
        }).filter(function (entry) {
            return entry.tripId && Number.isFinite(entry.deadline);
        });
        if (!badges.length) return;
        var serverEpoch = Date.parse(shellRoot.dataset.eoServerNow || "");
        var clientEpoch = Date.now();
        if (!Number.isFinite(serverEpoch)) serverEpoch = clientEpoch;

        function tick() {
            var authoritativeNow = serverEpoch + (Date.now() - clientEpoch);
            badges.forEach(function (entry) {
                var badge = entry.badge;
                if (!badge.isConnected || entry.deadline > authoritativeNow) return;
                if (String(badge.dataset.tripId || "") !== entry.tripId) return;
                var list = badge.closest ? badge.closest(".eo-dashboard-unload-badges") : null;
                badge.remove();
                shellRoot.querySelectorAll('[data-eo-dump-queue-list] [data-trip-id="' + entry.tripId + '"]').forEach(function (item) {
                    item.remove();
                });
                markLatestPendingTruckBadge(list);
                ensureMutedPendingBadge(list);
            });
            if (!badges.some(function (entry) { return entry.badge.isConnected; }) && window.eoManualQueuePreviewTimer) {
                window.clearInterval(window.eoManualQueuePreviewTimer);
                window.eoManualQueuePreviewTimer = null;
            }
        }
        tick();
        if (badges.some(function (entry) { return entry.badge.isConnected; })) {
            window.eoManualQueuePreviewTimer = window.setInterval(tick, 1000);
        }
    }

    bindManualDumpCardExpiry(shell);

    function bindFreeBucketDumpCardExpiry(shellRoot) {
        if (window.eoFreeBucketQueuePreviewTimer) {
            window.clearInterval(window.eoFreeBucketQueuePreviewTimer);
            window.eoFreeBucketQueuePreviewTimer = null;
        }
        if (!shellRoot) return;
        var badges = Array.prototype.slice.call(
            shellRoot.querySelectorAll("[data-eo-queue-truck][data-eo-free-bucket-preview-expires-at]")
        ).map(function (badge) {
            var tripId = String(badge.dataset.tripId || "");
            var localTripId = String(badge.dataset.eoLocalTripId || "");
            var eventId = String(badge.dataset.eoOfflineEventId || "");
            return {
                badge: badge,
                identity: tripId || localTripId || eventId,
                identityKind: tripId ? "trip" : (localTripId ? "localTrip" : "event"),
                deadline: Date.parse(badge.dataset.eoFreeBucketPreviewExpiresAt || "")
            };
        }).filter(function (entry) {
            return entry.identity && Number.isFinite(entry.deadline);
        });
        if (!badges.length) return;
        var serverEpoch = Date.parse(shellRoot.dataset.eoServerNow || "");
        var clientEpoch = Date.now();
        if (!Number.isFinite(serverEpoch)) serverEpoch = clientEpoch;

        function tick() {
            var authoritativeNow = serverEpoch + (Date.now() - clientEpoch);
            badges.forEach(function (entry) {
                var badge = entry.badge;
                if (!badge.isConnected || entry.deadline > authoritativeNow) return;
                var currentIdentity = entry.identityKind === "trip"
                    ? String(badge.dataset.tripId || "")
                    : entry.identityKind === "localTrip"
                        ? String(badge.dataset.eoLocalTripId || "")
                        : String(badge.dataset.eoOfflineEventId || "");
                if (currentIdentity !== entry.identity) return;
                var list = badge.closest ? badge.closest(".eo-dashboard-unload-badges") : null;
                badge.remove();
                shellRoot.querySelectorAll('[data-eo-dump-queue-list] [data-eo-queue-truck]').forEach(function (item) {
                    var sameTrip = entry.identityKind === "trip" && String(item.dataset.tripId || "") === entry.identity;
                    var sameLocalTrip = entry.identityKind === "localTrip" && String(item.dataset.eoLocalTripId || "") === entry.identity;
                    var sameEvent = entry.identityKind === "event" && String(item.dataset.eoOfflineEventId || "") === entry.identity;
                    if (sameTrip || sameLocalTrip || sameEvent) item.remove();
                });
                markLatestPendingTruckBadge(list);
                ensureMutedPendingBadge(list);
            });
            if (!badges.some(function (entry) { return entry.badge.isConnected; }) && window.eoFreeBucketQueuePreviewTimer) {
                window.clearInterval(window.eoFreeBucketQueuePreviewTimer);
                window.eoFreeBucketQueuePreviewTimer = null;
            }
        }
        tick();
        if (badges.some(function (entry) { return entry.badge.isConnected; })) {
            window.eoFreeBucketQueuePreviewTimer = window.setInterval(tick, 1000);
        }
    }

    bindFreeBucketDumpCardExpiry(shell);

    function restoreTruckAfterLoadedCancel(card, data) {
        if (!card) return;
        card.hidden = false;
        card.classList.remove("status-green", "status-yellow", "status-gray", "status-red", "status-orange", "is-inactive", "is-pending", "is-saved-on-device", "is-load-blocked", "is-shift-pending", "is-no-driver");
        card.classList.add("status-" + (data.status_key || "blue"));
        delete card.dataset.eoTruckInactive;
        card.dataset.eoCanLoad = "1";
        delete card.dataset.eoLoadBlockReasonCode;
        delete card.dataset.eoLoadBlockReasonLabel;
        delete card.dataset.eoOfflineEventId;
        delete card.dataset.eoLocalOpenTripId;
        card.dataset.eoEquipmentState = data.equipment_state || "assigned";
        card.setAttribute("draggable", "true");
        card.removeAttribute("aria-disabled");
        var status = card.querySelector("span");
        if (status) status.textContent = data.status_label || "назначена";
        var targetText = card.querySelector("em");
        if (targetText) targetText.remove();
        card.classList.add("is-returned-from-dump");
        window.setTimeout(function () {
            card.classList.remove("is-returned-from-dump");
        }, 520);
        selectTruck(card);
    }

    function cardByTruckId(truckId) {
        return shell.querySelector('[data-eo-truck-card][data-truck-id="' + truckId + '"]');
    }

    function queueModalElements() {
        var modal = shell.querySelector("[data-eo-dump-queue-modal]");
        return {
            modal: modal,
            dialog: modal ? modal.querySelector(".eo-dump-queue-dialog") : null,
            title: modal ? modal.querySelector("[data-eo-dump-queue-title]") : null,
            list: modal ? modal.querySelector("[data-eo-dump-queue-list]") : null,
            note: modal ? modal.querySelector("[data-eo-dump-queue-note]") : null
        };
    }

    function showQueueNote(text, isError) {
        var parts = queueModalElements();
        if (!parts.note) return;
        parts.note.hidden = false;
        parts.note.textContent = text || "";
        parts.note.classList.toggle("is-error", !!isError);
    }

    function closeDumpQueueModal() {
        var parts = queueModalElements();
        if (!parts.modal) return;
        if (activeQueueDrag) {
            cleanupQueueDragState(activeQueueDrag);
            activeQueueDrag = null;
        }
        parts.modal.hidden = true;
        parts.modal.setAttribute("aria-hidden", "true");
        if (parts.dialog) parts.dialog.classList.remove("is-return-armed");
    }

    function queueItemFromBadge(badge, dumpTarget) {
        var item = document.createElement("button");
        item.className = "eo-dump-queue-item status-green";
        item.type = "button";
        item.dataset.truckId = badge.dataset.truckId || "";
        item.dataset.tripId = badge.dataset.tripId || "";
        item.dataset.dumpPointId = dumpTarget.dataset.eoDumpTarget || "";
        item.dataset.truckNumber = badge.dataset.eoTruckNumber || badge.textContent.trim();
        item.dataset.eoOfflineEventId = badge.dataset.eoOfflineEventId || "";
        item.dataset.eoLocalTripId = badge.dataset.eoLocalTripId || "";
        var icon = document.createElement("img");
        icon.src = truckGreenIcon;
        icon.alt = "";
        var number = document.createElement("strong");
        number.textContent = item.dataset.truckNumber;
        item.appendChild(icon);
        item.appendChild(number);
        item.addEventListener("pointerdown", beginQueueDrag);
        return item;
    }

    function openDumpQueueModal(dumpTarget) {
        var parts = queueModalElements();
        if (!parts.modal || !parts.title || !parts.list) return;
        var name = dumpTarget.dataset.eoDumpName || (dumpTarget.querySelector("strong") ? dumpTarget.querySelector("strong").textContent.trim() : "Точка разгрузки");
        parts.title.textContent = name;
        parts.list.innerHTML = "";
        if (parts.note) {
            parts.note.hidden = true;
            parts.note.textContent = "";
        }
        var badges = Array.prototype.slice.call(dumpTarget.querySelectorAll("[data-eo-queue-truck]"));
        if (!badges.length) {
            var empty = document.createElement("div");
            empty.className = "eo-empty-state eo-empty-state-compact";
            empty.innerHTML = "<strong>Очереди нет</strong><span>На эту точку пока никто не отправлен</span>";
            parts.list.appendChild(empty);
        } else {
            badges.forEach(function (badge) {
                parts.list.appendChild(queueItemFromBadge(badge, dumpTarget));
            });
        }
        parts.modal.hidden = false;
        parts.modal.setAttribute("aria-hidden", "false");
    }

    function beginQueueDrag(event) {
        if (event.button !== undefined && event.button !== 0) return;
        var item = event.currentTarget;
        activeQueueDrag = {
            item: item,
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            started: false,
            isReturnReady: false,
            preview: null
        };
        if (item.setPointerCapture) {
            try {
                item.setPointerCapture(event.pointerId);
            } catch (error) {}
        }
        item.addEventListener("pointermove", updateQueueDrag);
        item.addEventListener("pointerup", finishQueueDrag);
        item.addEventListener("pointercancel", cancelQueueDrag);
    }

    function createQueueDragPreview(state) {
        if (!state || !state.item || state.preview) return;
        var rect = state.item.getBoundingClientRect();
        var preview = state.item.cloneNode(true);
        preview.classList.add("eo-dump-queue-drag-preview");
        preview.disabled = true;
        preview.setAttribute("aria-hidden", "true");
        preview.removeAttribute("id");
        preview.style.position = "fixed";
        preview.style.left = rect.left + "px";
        preview.style.top = rect.top + "px";
        preview.style.setProperty("width", rect.width + "px", "important");
        preview.style.setProperty("height", rect.height + "px", "important");
        preview.style.setProperty("min-width", "0", "important");
        preview.style.setProperty("min-height", "0", "important");
        preview.style.setProperty("max-width", "none", "important");
        preview.style.setProperty("max-height", "none", "important");
        preview.style.pointerEvents = "none";
        preview.style.zIndex = "22000";
        preview.style.margin = "0";
        preview.style.transform = "translate3d(0, 0, 0)";
        preview.style.transformOrigin = "center center";
        preview.style.boxSizing = "border-box";
        document.body.appendChild(preview);
        state.preview = preview;
        state.item.classList.add("is-queue-dragging");
    }

    function moveQueueDragPreview(state, dx, dy) {
        if (!state || !state.preview) return;
        state.preview.style.transform = "translate3d(" + dx + "px, " + dy + "px, 0) scale(.98)";
    }

    function cleanupQueueDragState(state) {
        if (!state || !state.item) return;
        state.item.classList.remove("is-queue-dragging");
        state.item.style.transform = "";
        state.item.removeEventListener("pointermove", updateQueueDrag);
        state.item.removeEventListener("pointerup", finishQueueDrag);
        state.item.removeEventListener("pointercancel", cancelQueueDrag);
        if (state.preview) {
            state.preview.remove();
            state.preview = null;
        }
    }

    function updateQueueDrag(event) {
        if (!activeQueueDrag || event.pointerId !== activeQueueDrag.pointerId) return;
        var dx = event.clientX - activeQueueDrag.startX;
        var dy = event.clientY - activeQueueDrag.startY;
        if (!activeQueueDrag.started && Math.abs(dx) + Math.abs(dy) < 8) return;
        event.preventDefault();
        if (!activeQueueDrag.started) {
            activeQueueDrag.started = true;
            createQueueDragPreview(activeQueueDrag);
        }
        moveQueueDragPreview(activeQueueDrag, dx, dy);
        var parts = queueModalElements();
        var dialogRect = parts.dialog ? parts.dialog.getBoundingClientRect() : null;
        activeQueueDrag.isReturnReady = !!(dialogRect && dy <= -56 && event.clientY <= dialogRect.top + 12);
        if (parts.dialog) parts.dialog.classList.toggle("is-return-armed", activeQueueDrag.isReturnReady);
        if (activeQueueDrag.preview) activeQueueDrag.preview.classList.toggle("is-return-ready", activeQueueDrag.isReturnReady);
    }

    function finishQueueDrag(event) {
        if (!activeQueueDrag || event.pointerId !== activeQueueDrag.pointerId) return;
        var state = activeQueueDrag;
        activeQueueDrag = null;
        var parts = queueModalElements();
        cleanupQueueDragState(state);
        if (parts.dialog) parts.dialog.classList.remove("is-return-armed");
        if (!state.started) return;
        event.preventDefault();
        if (state.isReturnReady) {
            cancelLoadedTripFromQueue(state.item);
        }
    }

    function cancelQueueDrag(event) {
        if (!activeQueueDrag || event.pointerId !== activeQueueDrag.pointerId) return;
        cleanupQueueDragState(activeQueueDrag);
        activeQueueDrag = null;
        var parts = queueModalElements();
        if (parts.dialog) parts.dialog.classList.remove("is-return-armed");
    }

    function showLoadedCancelStatus(options, text, isError) {
        if (options && options.direct) {
            showExcavatorNotice(text || "");
            return;
        }
        showQueueNote(text, isError);
    }

    function cancelLoadedTripFromQueue(item, options) {
        options = options || {};
        if (typeof window.isAppRoleReadonly === "function" && window.isAppRoleReadonly()) {
            showLoadedCancelStatus(options, "Роль неактивна — доступен только просмотр.", true);
            return Promise.resolve(false);
        }
        if (item && fieldOutbox && (item.dataset.eoOfflineEventId || item.dataset.tripId)) {
            var loadEventId = item.dataset.eoOfflineEventId;
            var saveCancellation;
            if (loadEventId) {
                saveCancellation = fieldOutbox.discardUnsent(loadEventId).then(function (discarded) {
                    if (discarded) return {discarded: true};
                    return fieldOutbox.pending().then(function (events) {
                        var source = events.find(function (event) { return event.event_id === loadEventId; });
                        if (!source) throw new Error("Сохранённая погрузка уже подтверждена. Обновите экран.");
                        var deviceId = excavatorInstallDeviceId();
                        return fieldOutbox.allocateSequence(legacyExcavatorFieldSequence(shell.dataset.eoAccessId || "", deviceId)).then(function (sequence) {
                            var cancelEvent = {
                            event_id: newExcavatorFieldId("excavator-load-cancel"),
                            event_type: "excavator.trip.loaded.cancelled",
                            format_version: 1,
                            actor_id: Number(shell.dataset.eoEmployeeId || 0),
                            access_id: Number(shell.dataset.eoAccessId || 0),
                            role_code: shell.dataset.eoRoleCode || "excavator_operator",
                            device_id: deviceId,
                            shift_id: Number(source.shift_id || shell.dataset.nativeShiftId || 0),
                            equipment_id: Number(source.equipment_id || shell.dataset.eoCurrentExcavatorId || 0),
                            trip_id: null,
                            local_trip_id: source.local_trip_id,
                            occurred_at: new Date().toISOString(),
                            sequence: sequence,
                            depends_on: [source.event_id],
                            payload: {
                                truck_id: source.payload && source.payload.truck_id,
                                dump_point_id: source.payload && source.payload.dump_point_id,
                                local_trip_id: source.local_trip_id
                            }
                            };
                            return fieldOutbox.queue(cancelEvent).then(function () { return {discarded: false}; });
                        });
                    });
                });
            } else {
                saveCancellation = queueExcavatorFieldEvent("excavator.trip.loaded.cancelled", {
                    truck_id: Number(item.dataset.truckId || 0),
                    dump_point_id: Number(item.dataset.dumpPointId || 0),
                    trip_id: Number(item.dataset.tripId || 0)
                }, {
                    idPrefix: "excavator-load-cancel",
                    tripId: Number(item.dataset.tripId || 0),
                    noDependency: true
                });
            }
            return saveCancellation.then(function () {
                var truckId = item.dataset.truckId || "";
                var dumpTarget = shell.querySelector('[data-eo-dump-target="' + item.dataset.dumpPointId + '"]');
                removePendingTruckBadge(dumpTarget, truckId, "");
                restoreTruckAfterLoadedCancel(cardByTruckId(truckId), {
                    equipment_state: "assigned",
                    status_label: "Назначен",
                    status_key: "yellow"
                });
                item.remove();
                showLoadedCancelStatus(options, "Отмена сохранена на телефоне.", false);
                if (navigator.onLine !== false) fieldOutbox.flush();
                return true;
            }).catch(function (error) {
                showLoadedCancelStatus(options, error.message || "Отмена не сохранена", true);
                return false;
            });
        }
        if (!truckLoadedCancelUrl || !item || !item.dataset.tripId) {
            playExcavatorSound("action_error");
            showLoadedCancelStatus(options, "Серверная отмена для этого рейса недоступна.", true);
            return Promise.resolve(false);
        }
        var actionOwner = options.actionOwner || item;
        var cancelActionId = actionOwner.dataset.eoCancelActionId || generateClientActionId("truck-loaded-cancel");
        actionOwner.dataset.eoCancelActionId = cancelActionId;
        item.disabled = true;
        showLoadedCancelStatus(options, options.direct ? "Возвращаю последний самосвал..." : "Отменяю назначение на сервере...", false);
        return fetch(truckLoadedCancelUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-CSRFToken": csrfToken,
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify({
                client_action_id: cancelActionId,
                trip_id: item.dataset.tripId || "",
                truck_id: item.dataset.truckId || "",
                dump_point_id: item.dataset.dumpPointId || ""
            })
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (!response.ok || !data.ok) {
                    throw new Error(data.error || "Назначение не отменено");
                }
                return data;
            });
        }).then(function (data) {
            playExcavatorSound("action_ok");
            var dumpTarget = shell.querySelector('[data-eo-dump-target="' + (data.dump_point_id || item.dataset.dumpPointId) + '"]');
            removePendingTruckBadge(dumpTarget, data.truck_id || item.dataset.truckId, data.trip_id || item.dataset.tripId);
            restoreTruckAfterLoadedCancel(cardByTruckId(data.truck_id || item.dataset.truckId), data);
            item.remove();
            showLoadedCancelStatus(
                options,
                options.direct
                    ? "Самосвал " + (item.dataset.truckNumber || "") + " возвращен. Выберите новую точку разгрузки."
                    : "Самосвал возвращен в назначенные.",
                false
            );
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("truck_loaded_cancel");
            }
            return true;
        }).catch(function (error) {
            playExcavatorSound("action_error");
            item.disabled = false;
            showLoadedCancelStatus(options, error.message || "Назначение не отменено", true);
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("truck_loaded_cancel_error");
            }
            return false;
        }).then(function (result) {
            if (options.dumpTarget) options.dumpTarget.classList.remove("is-return-pending");
            if (options.actionOwner) options.actionOwner.classList.remove("is-returning");
            if (result) refreshExcavatorWorkFromServer({preserveTab: true});
            return result;
        });
    }

    function returnLastTruckFromDump(dumpTarget) {
        if (!dumpTarget || dumpTarget.classList.contains("is-return-pending")) return Promise.resolve(false);
        var badge = latestPendingTruckBadge(dumpTarget);
        if (!badge) {
            playExcavatorSound("action_error");
            showExcavatorNotice("На этой точке нет самосвала для возврата.");
            return Promise.resolve(false);
        }
        var item = queueItemFromBadge(badge, dumpTarget);
        dumpTarget.classList.add("is-return-pending");
        badge.classList.add("is-returning");
        return cancelLoadedTripFromQueue(item, {
            direct: true,
            dumpTarget: dumpTarget,
            actionOwner: badge
        }).then(function (result) {
            dumpTarget.classList.remove("is-return-pending");
            badge.classList.remove("is-returning");
            return result;
        });
    }

    var dumpChoiceButtons = Array.prototype.slice.call(shell.querySelectorAll("[data-eo-dump-select]"));
    var persistedDumpPointIds = Object.create(null);
    var faceDraftTouched = false;
    var faceSettingsPending = false;

    dumpChoiceButtons.forEach(function (button) {
        if (button.dataset.eoDumpPersisted === "true") {
            persistedDumpPointIds[String(button.dataset.eoDumpSelect || "")] = true;
        }
    });

    function normalizeDumpPointIds(ids) {
        return (Array.isArray(ids) ? ids : []).map(String).filter(Boolean);
    }

    function renderDumpChoiceState(button) {
        if (!button) return;
        var id = String(button.dataset.eoDumpSelect || "");
        var name = button.dataset.eoDumpName || "Точка разгрузки";
        var selected = button.classList.contains("is-selected");
        var persisted = !!persistedDumpPointIds[id];
        var state = selected && persisted
            ? "applied"
            : selected
                ? "pending-add"
                : persisted
                    ? "pending-remove"
                    : "idle";
        var stateLabel = state === "applied"
            ? "Активная точка разгрузки"
            : state === "pending-add"
                ? "Будет добавлена после применения"
                : state === "pending-remove"
                    ? "Будет отключена после применения"
                    : "Не выбрана";
        button.classList.toggle("is-dump-applied", state === "applied");
        button.classList.toggle("is-dump-pending", state === "pending-add");
        button.classList.toggle("is-dump-pending-remove", state === "pending-remove");
        button.dataset.eoDumpState = state;
        button.dataset.eoDumpPersisted = persisted ? "true" : "false";
        button.setAttribute("aria-pressed", selected ? "true" : "false");
        button.setAttribute("aria-label", name + ". " + stateLabel + ".");
    }

    function renderDumpChoiceStates() {
        dumpChoiceButtons.forEach(renderDumpChoiceState);
    }

    function replacePersistedDumpPointIds(ids) {
        persistedDumpPointIds = Object.create(null);
        normalizeDumpPointIds(ids).forEach(function (id) {
            persistedDumpPointIds[id] = true;
        });
        renderDumpChoiceStates();
    }

    function replaceDraftDumpPointIds(ids) {
        var selectedIds = Object.create(null);
        normalizeDumpPointIds(ids).forEach(function (id) {
            selectedIds[id] = true;
        });
        dumpChoiceButtons.forEach(function (button) {
            button.classList.toggle("is-selected", !!selectedIds[String(button.dataset.eoDumpSelect || "")]);
        });
        renderDumpChoiceStates();
    }

    function selectedDumpPointIds() {
        var ids = [];
        dumpChoiceButtons.forEach(function (button) {
            if (!button.classList.contains("is-selected")) return;
            var id = button.dataset.eoDumpSelect || "";
            if (id && ids.indexOf(id) === -1) {
                ids.push(id);
            }
        });
        return ids;
    }

    function syncDumpPointInputs() {
        var ids = selectedDumpPointIds();
        if (dumpPointsInput) {
            dumpPointsInput.value = ids.join(",");
        }
        if (dumpInput && ids.length && ids.indexOf(dumpInput.value) === -1) {
            dumpInput.value = ids[0];
        }
        return ids;
    }

    function toggleDumpChoice(button) {
        if (faceSettingsPending || !button || button.disabled || button.getAttribute("aria-disabled") === "true") return;
        var selectedButtons = dumpChoiceButtons.filter(function (item) {
            return item.classList.contains("is-selected");
        });
        var isSelected = button.classList.contains("is-selected");
        if (isSelected && selectedButtons.length <= 1) {
            playExcavatorSound("action_error");
            showExcavatorNotice("Оставьте хотя бы одну точку разгрузки.");
            return;
        }
        button.classList.toggle("is-selected", !isSelected);
        faceDraftTouched = true;
        renderDumpChoiceState(button);
        syncDumpPointInputs();
        updateApplySettingsState();
    }

    function normalizeFaceSetting(value) {
        var normalized = String(value == null ? "" : value).trim();
        return normalized.replace(/^0+(?=\d)/, "");
    }

    function currentExcavatorSettingsSnapshot() {
        var rockSelect = shell.querySelector("[data-eo-rock-select]");
        var horizonInput = shell.querySelector("[data-eo-face-horizon]");
        var blockInput = shell.querySelector("[data-eo-face-block]");
        return JSON.stringify({
            rockTypeId: rockSelect ? String(rockSelect.value || "") : "",
            loadingHorizon: normalizeFaceSetting(horizonInput ? horizonInput.value : ""),
            loadingBlock: normalizeFaceSetting(blockInput ? blockInput.value : ""),
            dumpPointIds: selectedDumpPointIds()
        });
    }

    var settingsApplyButton = shell.querySelector("[data-eo-apply-settings]");
    var appliedExcavatorSettingsSnapshot = settingsApplyButton && settingsApplyButton.dataset.eoSettingsApplied === "true"
        ? currentExcavatorSettingsSnapshot()
        : null;
    renderDumpChoiceStates();

    function setFaceSettingsPending(isPending) {
        faceSettingsPending = !!isPending;
        if (faceScreen) {
            faceScreen.dataset.eoFacePending = faceSettingsPending ? "true" : "false";
        }
        var isAvailable = !!(faceContent && faceContent.dataset.eoFaceSettingsAvailable === "true");
        dumpChoiceButtons.forEach(function (button) {
            button.disabled = faceSettingsPending || !isAvailable;
            button.setAttribute("aria-disabled", button.disabled ? "true" : "false");
        });
        shell.querySelectorAll("[data-eo-face-horizon], [data-eo-face-block], [data-eo-rock-select]").forEach(function (control) {
            control.disabled = faceSettingsPending || !isAvailable;
            control.setAttribute("aria-disabled", control.disabled ? "true" : "false");
        });
    }

    function updateApplySettingsState() {
        var button = shell.querySelector("[data-eo-apply-settings]");
        if (!button || button.classList.contains("is-pending")) {
            return;
        }
        var isAvailable = button.dataset.eoSettingsAvailable === "true";
        var isDirty = appliedExcavatorSettingsSnapshot === null
            || currentExcavatorSettingsSnapshot() !== appliedExcavatorSettingsSnapshot;
        if (faceScreen) {
            faceScreen.dataset.eoFaceDirty = faceDraftTouched && isDirty ? "true" : "false";
        }
        var isDisabled = !isAvailable || !isDirty;
        button.disabled = isDisabled;
        button.classList.toggle("is-disabled", isDisabled);
        button.setAttribute("aria-disabled", isDisabled ? "true" : "false");
        button.setAttribute(
            "aria-label",
            !isAvailable ? "Настройки недоступны: сначала начните смену" : isDirty ? "Применить настройки" : "Настройки уже применены"
        );
    }

    var truckLoadProjections = new WeakMap();

    function rememberTruckLoadProjection(card, eventId, snapshot) {
        var current = truckLoadProjections.get(card);
        if (current && current.eventId === eventId) return;
        truckLoadProjections.set(card, {
            eventId: eventId,
            snapshot: snapshot || snapshotTruckCard(card)
        });
    }

    function snapshotTruckCard(card) {
        return {
            className: card.className,
            inactive: card.dataset.eoTruckInactive || "",
            canLoad: card.dataset.eoCanLoad || "",
            loadBlockReasonCode: card.dataset.eoLoadBlockReasonCode || "",
            loadBlockReasonLabel: card.dataset.eoLoadBlockReasonLabel || "",
            equipmentState: card.dataset.eoEquipmentState || "",
            hidden: card.hidden,
            openTripId: card.dataset.eoOpenTripId,
            offlineEventId: card.dataset.eoOfflineEventId,
            localOpenTripId: card.dataset.eoLocalOpenTripId,
            draggable: card.getAttribute("draggable") || "true",
            statusText: card.querySelector("span") ? card.querySelector("span").textContent : "",
            targetText: card.querySelector("em") ? card.querySelector("em").textContent : "",
            icon: card.querySelector("img") ? card.querySelector("img").src : ""
        };
    }

    function applyTruckPending(card, dumpTarget) {
        var label = dumpTarget && dumpTarget.querySelector("strong") ? dumpTarget.querySelector("strong").textContent : "";
        card.classList.remove("status-yellow", "status-gray", "status-red", "status-blue", "status-orange", "is-selected");
        card.classList.remove("is-load-blocked", "is-shift-pending", "is-no-driver");
        card.classList.add("status-green", "is-inactive", "is-pending");
        card.dataset.eoTruckInactive = "1";
        card.dataset.eoCanLoad = "0";
        delete card.dataset.eoLoadBlockReasonCode;
        delete card.dataset.eoLoadBlockReasonLabel;
        card.dataset.eoEquipmentState = "loaded_waiting_unload";
        card.setAttribute("draggable", "false");
        var status = card.querySelector("span");
        if (status) status.textContent = "на разгрузку";
        var icon = card.querySelector("img");
        if (icon && truckGreenIcon) icon.src = truckGreenIcon;
        var targetText = card.querySelector("em");
        if (!targetText) {
            targetText = document.createElement("em");
            card.appendChild(targetText);
        }
        targetText.textContent = label;
        selectedTruck = null;
        if (assignmentInput) assignmentInput.value = "";
    }

    function confirmTruckLoaded(card, payload) {
        if (card.dataset.eoTransferDirection === "outgoing") {
            card.remove();
            selectedTruck = null;
            if (assignmentInput) assignmentInput.value = "";
            pickFirstActiveTruck();
            return;
        }
        card.dataset.eoOpenTripId = String(payload.trip_id || "");
        delete card.dataset.eoLoadActionKey;
        delete card.dataset.eoLoadActionId;
        card.classList.remove("is-pending");
        card.classList.remove("is-load-blocked", "is-shift-pending", "is-no-driver");
        card.classList.add("status-green", "is-inactive");
        card.dataset.eoTruckInactive = "1";
        card.dataset.eoCanLoad = "0";
        delete card.dataset.eoLoadBlockReasonCode;
        delete card.dataset.eoLoadBlockReasonLabel;
        card.dataset.eoEquipmentState = "loaded_waiting_unload";
        card.setAttribute("draggable", "false");
        var status = card.querySelector("span");
        if (status) status.textContent = payload.status_label || "на разгрузку";
        var targetText = card.querySelector("em");
        if (!targetText) {
            targetText = document.createElement("em");
            card.appendChild(targetText);
        }
        targetText.textContent = payload.dump_point || targetText.textContent || "";
        pickFirstActiveTruck();
    }

    function restoreTruckCard(card, snapshot, select) {
        card.className = snapshot.className;
        card.hidden = snapshot.hidden;
        [
            ["eoOpenTripId", snapshot.openTripId],
            ["eoOfflineEventId", snapshot.offlineEventId],
            ["eoLocalOpenTripId", snapshot.localOpenTripId]
        ].forEach(function (entry) {
            if (entry[1] === undefined) delete card.dataset[entry[0]];
            else card.dataset[entry[0]] = entry[1];
        });
        if (snapshot.inactive) {
            card.dataset.eoTruckInactive = snapshot.inactive;
        } else {
            delete card.dataset.eoTruckInactive;
        }
        if (snapshot.canLoad) {
            card.dataset.eoCanLoad = snapshot.canLoad;
        } else {
            delete card.dataset.eoCanLoad;
        }
        if (snapshot.loadBlockReasonCode) {
            card.dataset.eoLoadBlockReasonCode = snapshot.loadBlockReasonCode;
        } else {
            delete card.dataset.eoLoadBlockReasonCode;
        }
        if (snapshot.loadBlockReasonLabel) {
            card.dataset.eoLoadBlockReasonLabel = snapshot.loadBlockReasonLabel;
        } else {
            delete card.dataset.eoLoadBlockReasonLabel;
        }
        if (snapshot.equipmentState) {
            card.dataset.eoEquipmentState = snapshot.equipmentState;
        } else {
            delete card.dataset.eoEquipmentState;
        }
        card.setAttribute("draggable", snapshot.draggable);
        var status = card.querySelector("span");
        if (status) status.textContent = snapshot.statusText;
        var icon = card.querySelector("img");
        if (icon && snapshot.icon) icon.src = snapshot.icon;
        var targetText = card.querySelector("em");
        if (targetText) {
            if (snapshot.targetText) {
                targetText.textContent = snapshot.targetText;
            } else {
                targetText.remove();
            }
        }
        if (select !== false) selectTruck(card);
    }

    function postTruckLoaded(card, dumpTarget, confirmedPrevious) {
        if (typeof window.isAppRoleReadonly === "function" && window.isAppRoleReadonly()) {
            return;
        }
        // Погрузка не должна «тихо» пропадать: без карточки, точки разгрузки или
        // локальной очереди работник видит причину и может повторить действие.
        if (!card || !dumpTarget) {
            playExcavatorSound("action_error");
            showExcavatorNotice(!card
                ? "Самосвал не определён. Обновите экран и повторите погрузку."
                : "Точка разгрузки не определена. Обновите экран и повторите погрузку.");
            return;
        }
        if (!fieldOutbox) {
            playExcavatorVoice("action_error", "voice_truck_send_failed");
            renderExcavatorOfflineStatus({storage_failed: true, total: 0});
            showExcavatorNotice("Локальное сохранение недоступно. Погрузка не сохранена — обновите экран и повторите.");
            return;
        }
        if (!canTruckLoad(card) && !(confirmedPrevious && card.isConnected
            && card.dataset.eoManualAvailable === "1" && !card.classList.contains("is-pending"))) {
            playExcavatorSound("action_error");
            showExcavatorNotice(truckLoadBlockReason(card));
            return;
        }
        var snapshot = snapshotTruckCard(card);
        snapshot.className = snapshot.className.replace(/\bis-picked-up\b/g, "");
        var truckId = card.dataset.truckId || "";
        var truckNumber = card.dataset.eoTruckNumber || (card.querySelector("strong") ? card.querySelector("strong").textContent.trim() : "");
        var dumpPointId = Number(dumpTarget.dataset.eoDumpTarget || 0);
        var dumpPointName = dumpTarget.dataset.eoDumpName || (dumpTarget.querySelector("strong") ? dumpTarget.querySelector("strong").textContent.trim() : "");
        var freeBucketAcceptanceId = card.dataset.eoFreeBucketAcceptanceId || "";
        var freeBucketAcceptanceLocalId = card.dataset.eoFreeBucketAcceptanceLocalId || "";
        var freeBucketAcceptanceReference = freeBucketAcceptanceId || freeBucketAcceptanceLocalId;
        var isFreeBucketLoad = card.dataset.eoFreeBucket === "1" && Boolean(freeBucketAcceptanceReference);
        var markedAt = new Date().toISOString();
        var payload = {
            assignment_id: isFreeBucketLoad ? "" : (card.dataset.assignmentId || ""),
            // Свободный ковш определяет фактический экскаватор, но не отключает
            // уже подключенного водителя: при его активной смене сервер создаст
            // обычный рейс и передаст его водителю.
            manual_control: card.dataset.eoManualAvailable === "1",
            // Подтверждённый сервером приём может быть открыт на другом устройстве.
            // Тогда передаём его серверный id без device-scoped зависимости.
            free_bucket_acceptance_id: freeBucketAcceptanceId,
            free_bucket_acceptance_local_id: freeBucketAcceptanceId ? "" : freeBucketAcceptanceLocalId,
            expected_open_trip_id: card.dataset.eoOpenTripId || "",
            truck_id: card.dataset.truckId || "",
            excavator_id: shell.dataset.eoCurrentExcavatorId || "",
            dump_point_id: dumpTarget.dataset.eoDumpTarget || "",
            rock_type_id: shell.querySelector("select[name='rock_type']") ? shell.querySelector("select[name='rock_type']").value : "",
            loading_horizon: shell.querySelector("input[name='loading_horizon']") ? shell.querySelector("input[name='loading_horizon']").value : "",
            loading_block: shell.querySelector("input[name='loading_block']") ? shell.querySelector("input[name='loading_block']").value : "",
            planned_volume_m3: shell.querySelector("input[name='planned_volume_m3']") ? shell.querySelector("input[name='planned_volume_m3']").value : "",
            transport_distance_km: dumpTarget.dataset.eoDumpDistance || (transportDistanceInput ? transportDistanceInput.value : ""),
            downtime_text: downtimeInput ? downtimeInput.value : "",
            note: shell.querySelector("input[name='note']") ? shell.querySelector("input[name='note']").value : ""
        };
        if (isFreeBucketLoad) payload.truck_number = truckNumber;
        card.classList.add("is-pending");
        fieldOutbox.pending().then(function (events) {
            var previous = events.filter(function (event) {
                return (event.event_type === "excavator.trip.loaded" || event.event_type === "excavator.free_bucket.loaded")
                    && (isFreeBucketLoad || (event.event_type === "excavator.trip.loaded"
                        && String(event.shift_id) === shell.dataset.nativeShiftId
                        && String(event.equipment_id) === shell.dataset.eoCurrentExcavatorId))
                    && String(event.payload && event.payload.truck_id || "") === String(truckId)
                    && ["conflict", "invalid", "auth_required"].indexOf(event.sync_state) < 0;
            }).sort(function (left, right) { return Number(right.sequence || 0) - Number(left.sequence || 0); })[0];
            if (previous && previous.local_trip_id) {
                payload.expected_open_trip_id = "";
                payload.expected_open_trip_local_id = previous.local_trip_id;
            }
            if (!isFreeBucketLoad) {
                var previousReference = payload.expected_open_trip_local_id || payload.expected_open_trip_id;
                if (confirmedPrevious && previousReference !== confirmedPrevious) {
                    card.classList.remove("is-pending");
                    showExcavatorNotice("Предыдущий рейс изменился. Проверьте карточку и повторите погрузку.");
                    return null;
                }
                if (previousReference && !confirmedPrevious) {
                    card.classList.remove("is-pending");
                    window.openAppConfirmDialog(
                        "У самосвала №" + truckNumber + " уже записана погрузка. Если он вернулся и загружен снова, оформите новый рейс.",
                        function () {
                            if (!card.isConnected || !dumpTarget.isConnected) {
                                showExcavatorNotice("Карточка обновилась. Проверьте её и повторите погрузку.");
                                return;
                            }
                            postTruckLoaded(card, dumpTarget, previousReference);
                        },
                        0, "Оформить новую погрузку"
                    );
                    return null;
                }
                payload.load_intent = confirmedPrevious ? "new_cycle" : "standard";
            }
            var acceptanceEvent = isFreeBucketLoad ? events.find(function (event) {
                return event.event_id === freeBucketAcceptanceLocalId;
            }) : null;
            var eventId = newExcavatorFieldId(isFreeBucketLoad ? "free-bucket-load" : "excavator-load");
            var localTripId = newExcavatorFieldId("local-trip");
            var deviceId = excavatorInstallDeviceId();
            return fieldOutbox.allocateSequence(legacyExcavatorFieldSequence(shell.dataset.eoAccessId || "", deviceId)).then(function (sequence) {
                var event = {
                event_id: eventId,
                event_type: isFreeBucketLoad ? "excavator.free_bucket.loaded" : "excavator.trip.loaded",
                format_version: 1,
                actor_id: Number(shell.dataset.eoEmployeeId || 0),
                access_id: Number(shell.dataset.eoAccessId || 0),
                role_code: shell.dataset.eoRoleCode || "excavator_operator",
                device_id: deviceId,
                shift_id: Number(shell.dataset.nativeShiftId || 0),
                equipment_id: Number(shell.dataset.eoCurrentExcavatorId || 0),
                trip_id: null,
                local_trip_id: localTripId,
                occurred_at: markedAt,
                sequence: sequence,
                depends_on: [
                    isFreeBucketLoad
                        ? (freeBucketAcceptanceId ? "" : freeBucketAcceptanceLocalId)
                        : (acceptanceEvent ? acceptanceEvent.event_id : ""),
                    previous ? previous.event_id : ""
                ].filter(function (value, index, values) {
                    return value && values.indexOf(value) === index;
                }),
                payload: payload
                };
                return fieldOutbox.queue(event).then(function () { return event; });
            });
        }).then(function (event) {
            if (!event) return;
            invalidateExcavatorWorkRefresh();
            rememberTruckLoadProjection(card, event.event_id, snapshot);
            applyTruckPending(card, dumpTarget);
            var optimisticBadge = addPendingTruckBadge(dumpTarget, card, "");
            if (optimisticBadge) {
                optimisticBadge.dataset.eoOfflineEventId = event.event_id;
                optimisticBadge.dataset.eoLocalTripId = event.local_trip_id;
                if (isFreeBucketLoad) {
                    optimisticBadge.dataset.eoFreeBucketPreviewExpiresAt = new Date(
                        Date.parse(event.occurred_at) + 5 * 60 * 1000
                    ).toISOString();
                }
            }
            card.dataset.eoOfflineEventId = event.event_id;
            card.dataset.eoLocalOpenTripId = event.local_trip_id;
            card.classList.add("is-saved-on-device");
            if (isFreeBucketLoad && freeBucketController) {
                freeBucketController.markLoaded(card);
                bindFreeBucketDumpCardExpiry(shell);
            } else if (card.dataset.eoTransferDirection === "outgoing") {
                card.classList.remove("is-pending");
                card.hidden = true;
            } else {
                confirmTruckLoaded(card, {trip_id: "", status_label: "Сохранено на телефоне"});
            }
            playExcavatorEquipmentVoice("excavator_truck_sent", truckNumber, {
                cue: "action_ok",
                fallbackVoice: "voice_truck_sent",
                dumpPointId: dumpPointId,
                dumpPointName: dumpPointName,
                operationKey: "excavator-load:" + event.event_id,
                eventVersion: 0,
                eventKey: "excavator_operator_truck_sent"
            });
            markLastDumpTarget(dumpTarget);
            showExcavatorNotice(navigator.onLine === false ? "Погрузка сохранена на телефоне" : "Погрузка сохранена. Отправляем на сервер...");
            if (navigator.onLine !== false) fieldOutbox.flush();
        }).catch(function (error) {
            playExcavatorVoice("action_error", "voice_truck_send_failed");
            restoreTruckCard(card, snapshot);
            card.classList.remove("is-pending", "is-saved-on-device");
            showExcavatorNotice(error.message || "Не удалось сохранить погрузку на телефоне");
        });
    }

    window.applyExcavatorPendingFieldEvents = function (events) {
        var allCurrentEvents = (events || []).filter(function (event) {
            return String(event.shift_id || "") === String(shell.dataset.nativeShiftId || "")
                && String(event.equipment_id || "") === String(shell.dataset.eoCurrentExcavatorId || "");
        });
        if (freeBucketController) freeBucketController.reconcileEvents(allCurrentEvents);
        var currentEvents = allCurrentEvents.filter(function (event) {
            return ["pending", "syncing"].indexOf(event.sync_state) >= 0;
        }).sort(function (left, right) {
            return Number(left.sequence || 0) - Number(right.sequence || 0);
        });
        currentEvents.filter(function (event) {
            return (event.event_type === "excavator.trip.loaded" || event.event_type === "excavator.free_bucket.loaded")
                && ["pending", "syncing"].indexOf(event.sync_state) >= 0
        }).forEach(function (event) {
            var payload = event.payload || {};
            var card = cardByTruckId(payload.truck_id);
            var dumpTarget = shell.querySelector('[data-eo-dump-target="' + String(payload.dump_point_id || "") + '"]');
            if (!dumpTarget) return;
            var badgeSource = card;
            if (!badgeSource && event.event_type === "excavator.free_bucket.loaded") {
                badgeSource = document.createElement("span");
                badgeSource.dataset.truckId = String(payload.truck_id || "");
                var badgeNumber = document.createElement("strong");
                badgeNumber.textContent = payload.truck_number || payload.truck_id || "";
                badgeSource.appendChild(badgeNumber);
            }
            if (!badgeSource) return;
            var badge = addPendingTruckBadge(dumpTarget, badgeSource, "");
            if (badge) {
                badge.dataset.eoOfflineEventId = event.event_id;
                badge.dataset.eoLocalTripId = event.local_trip_id || "";
                if (event.event_type === "excavator.free_bucket.loaded") {
                    badge.dataset.eoFreeBucketPreviewExpiresAt = new Date(
                        Date.parse(event.occurred_at) + 5 * 60 * 1000
                    ).toISOString();
                }
            }
            if (event.event_type === "excavator.free_bucket.loaded") {
                if (card && freeBucketController) freeBucketController.markLoaded(card);
                bindFreeBucketDumpCardExpiry(shell);
                return;
            }
            if (!card) return;
            rememberTruckLoadProjection(card, event.event_id);
            card.dataset.eoOfflineEventId = event.event_id;
            card.dataset.eoLocalOpenTripId = event.local_trip_id || "";
            card.classList.add("is-saved-on-device");
            if (card.dataset.eoTransferDirection === "outgoing") {
                card.classList.remove("is-pending");
                card.hidden = true;
            } else {
                confirmTruckLoaded(card, {trip_id: "", status_label: "Сохранено на телефоне"});
            }
        });
        currentEvents.filter(function (event) {
            return event.event_type === "excavator.downtime.started" || event.event_type === "excavator.downtime.ended";
        }).forEach(function (event) {
            if (event.event_type === "excavator.downtime.ended") {
                clearActiveDowntime({active: false, shift_total_seconds: downtimeCard ? downtimeCard.dataset.eoShiftDowntimeSeconds || 0 : 0});
                return;
            }
            var payload = event.payload || {};
            var reasonButton = shell.querySelector('[data-eo-downtime-reason-id="' + String(payload.reason_id || "") + '"]');
            var statusMatch = reasonButton ? reasonButton.className.match(/status-([a-z]+)/) : null;
            applyActiveDowntime({
                active: true,
                event_id: event.event_id,
                reason_id: payload.reason_id,
                started_at: event.occurred_at,
                elapsed_seconds: Math.max(0, Math.floor((Date.now() - Date.parse(event.occurred_at || "")) / 1000)),
                shift_total_seconds: downtimeCard ? downtimeCard.dataset.eoShiftDowntimeSeconds || 0 : 0,
                active_counts_towards_shift: true,
                status_key: statusMatch ? statusMatch[1] : "yellow",
                equipment_state_code: reasonButton ? reasonButton.dataset.eoEquipmentState || "waiting" : "waiting"
            });
        });
        currentEvents.filter(function (event) {
            return event.event_type === "excavator.trip.loaded.cancelled";
        }).forEach(function (event) {
            var payload = event.payload || {};
            var dumpTarget = shell.querySelector('[data-eo-dump-target="' + String(payload.dump_point_id || "") + '"]');
            if (dumpTarget) removePendingTruckBadge(dumpTarget, payload.truck_id, "");
            restoreTruckAfterLoadedCancel(cardByTruckId(payload.truck_id), {
                equipment_state: "assigned", status_label: "Назначен", status_key: "yellow"
            });
        });
        if (currentEvents.some(function (event) { return event.event_type === "excavator.shift.closed"; })) {
            applyLocalShiftClose();
        }
    };

    window.confirmExcavatorFieldEvent = function (event, result) {
        if (!event) return Promise.resolve();
        if (freeBucketController) freeBucketController.handleConfirmed(event, result || {});
        if (event.event_type === "excavator.trip.loaded" || event.event_type === "excavator.free_bucket.loaded") {
            var tripId = result && result.server_ids ? result.server_ids.trip_id : "";
            var badge = document.querySelector('[data-eo-offline-event-id="' + event.event_id + '"]');
            if (badge && tripId) {
                badge.dataset.tripId = String(tripId);
                delete badge.dataset.eoOfflineEventId;
                delete badge.dataset.eoLocalTripId;
                if (event.event_type === "excavator.free_bucket.loaded") {
                    bindFreeBucketDumpCardExpiry(shell);
                }
            }
            var confirmedCard = cardByTruckId(event.payload && event.payload.truck_id);
            if (result && result.load_already_recorded) {
                showExcavatorNotice("Эта погрузка уже сохранена.");
            }
            if (confirmedCard && tripId && (
                !confirmedCard.dataset.eoOfflineEventId || confirmedCard.dataset.eoOfflineEventId === event.event_id
            )) {
                confirmedCard.dataset.eoOpenTripId = String(tripId);
                if (confirmedCard.dataset.eoOfflineEventId === event.event_id) {
                    truckLoadProjections.delete(confirmedCard);
                    delete confirmedCard.dataset.eoOfflineEventId;
                    delete confirmedCard.dataset.eoLocalOpenTripId;
                }
            }
        }
        if (event.event_type === "excavator.downtime.started") {
            var downtimeId = result && result.server_ids
                ? (result.server_ids.downtime_event_id || result.server_ids.downtime_id || "")
                : "";
            if (
                downtimeCard
                && downtimeId
                && String(downtimeCard.dataset.eoActiveDowntimeId || "") === String(event.local_downtime_id || event.event_id)
            ) {
                downtimeCard.dataset.eoActiveDowntimeId = String(downtimeId);
            }
        }
        var version = Number(result && (result.server_version || result.version) || 0);
        // POST подтверждает запись, но ещё не применение серверного HTML.
        // Единственный владелец сверки сохраняет pending при abort/фоне/жесте.
        if (window.AppRealtime && typeof window.AppRealtime.requestReconcile === "function") {
            window.AppRealtime.requestReconcile("offline_excavator_events_confirmed", version);
        }
        return Promise.resolve();
    };

    window.attendExcavatorFieldEvent = function (event, result) {
        if (freeBucketController) freeBucketController.markAttention(event, result || {});
        if (event && result && (result.status === "conflict" || result.status === "invalid") &&
            (event.event_type === "excavator.trip.loaded" || event.event_type === "excavator.free_bucket.loaded")) {
            var card = cardByTruckId(event.payload && event.payload.truck_id);
            var projection = card && truckLoadProjections.get(card);
            if (projection && projection.eventId === event.event_id && card.dataset.eoOfflineEventId === event.event_id) {
                truckLoadProjections.delete(card);
                restoreTruckCard(card, projection.snapshot, false);
            }
            shell.querySelectorAll("[data-eo-queue-truck][data-eo-offline-event-id]").forEach(function (badge) {
                if (badge.dataset.eoOfflineEventId !== event.event_id || badge.dataset.tripId) return;
                var list = badge.parentElement;
                badge.remove();
                if (list) {
                    markLatestPendingTruckBadge(list);
                    ensureMutedPendingBadge(list);
                }
            });
        }
        var message = result && (result.message || result.error);
        showExcavatorNotice(message || "Одно из сохранённых действий требует сверки.");
        invalidateExcavatorWorkRefresh();
        if (window.AppRealtime && typeof window.AppRealtime.requestReconcile === "function") {
            window.AppRealtime.requestReconcile("offline_excavator_event_needs_review");
            return;
        }
        window.setTimeout(function () {
            refreshExcavatorWorkFromServer({preserveTab: true});
        }, 120);
    };

    if (fieldOutbox) {
        fieldOutbox.pending().then(function (events) {
            window.applyExcavatorPendingFieldEvents(events);
        }).catch(function () {
            renderExcavatorOfflineStatus({storage_failed: true, total: 0});
        });
    }

    function isFreeBucketCancelSwipe(state, deltaX, deltaY) {
        return Boolean(
            state
            && state.started
            && state.card
            && state.card.dataset.eoFreeBucket === "1"
            && state.card.dataset.eoFreeBucketUsed !== "1"
            && state.card.dataset.eoFreeBucketCancelPending !== "1"
            && deltaY <= -56
            && Math.abs(deltaY) >= Math.max(56, Math.abs(deltaX) * 1.25)
        );
    }

    tabs.forEach(function (tab) {
        tab.addEventListener("click", function () {
            var tabName = tab.dataset.eoTab;
            activateTab(tabName);
            scheduleExcavatorTabSettle(tabName);
        });
    });

    var downtimeEdge = window.ExcavatorDowntimeEdge && window.ExcavatorDowntimeEdge.bind(shell, eventsScreen, function (name) {
        activateTab(name || "events");
        scheduleExcavatorTabSettle(name || "events");
    });

    var excavatorDashboardDrag = window.ExcavatorDashboardDrag.attach({
        shell: shell,
        showComet: false,
        canDrag: canTruckLoad,
        isInactive: isInactiveTruck,
        isBlocked: isTruckLoadBlocked,
        onOpenDetail: function (card) {
            return openTruckDetailCard(card.dataset.eoTruckDetailId || card.dataset.truckId);
        },
        onSelect: selectTruck,
        onDrop: function (card, target) {
            selectDump(target.dataset.eoDumpTarget);
            postTruckLoaded(card, target);
        },
        isSpecialCancel: isFreeBucketCancelSwipe,
        onSpecialCancel: function (card) {
            if (freeBucketController && typeof freeBucketController.cancelAccepted === "function") {
                freeBucketController.cancelAccepted(card);
            }
        },
        haptic: window.excavatorHaptic
    });

    function bindExcavatorTruckCard(card) {
        if (excavatorDashboardDrag) excavatorDashboardDrag.bind(card);
    }

    freeBucketController = window.ExcavatorFreeBucket && typeof window.ExcavatorFreeBucket.attachShell === "function"
        ? window.ExcavatorFreeBucket.attachShell({
            shell: shell,
            queueEvent: queueExcavatorFieldEvent,
            fieldOutbox: fieldOutbox,
            bindTruckCard: bindExcavatorTruckCard,
            showNotice: showExcavatorNotice,
            invalidateRefresh: invalidateExcavatorWorkRefresh
        })
        : null;
    shell.querySelectorAll("[data-eo-truck-card]").forEach(bindExcavatorTruckCard);

    shell.querySelectorAll("[data-eo-dump-select]").forEach(function (button) {
        button.addEventListener("click", function () {
            toggleDumpChoice(button);
        });
    });

    if (faceScreen && faceContent) {
        faceScreen.addEventListener("click", function (event) {
            if (faceContent.dataset.eoFaceSettingsAvailable === "true") return;
            var lockedRegion = event.target.closest(".mobile-face__content, .mobile-face__actions");
            if (!lockedRegion || !faceScreen.contains(lockedRegion)) return;
            event.preventDefault();
            playExcavatorSound("action_error");
            showExcavatorNotice("Смена не активна. Сначала начните смену.", { variant: "shift-lock" });
        });
    }

    if (eventsScreen) {
        eventsScreen.addEventListener("click", function (event) {
            if (eventsScreen.dataset.eoDowntimeAvailable === "true") return;
            var lockedRegion = event.target.closest(".eo-reason-grid, .mobile-downtime__actions");
            if (!lockedRegion || !eventsScreen.contains(lockedRegion)) return;
            event.preventDefault();
            playExcavatorSound("action_error");
            showExcavatorNotice("Смена не активна. Сначала начните смену.", { variant: "shift-lock" });
        });
    }

    if (workScreen) {
        workScreen.addEventListener("click", function (event) {
            if (workScreen.dataset.eoWorkAvailable === "true") return;
            var lockedRegion = event.target.closest(".eo-dashboard-truck-grid, .eo-dashboard-unload-grid");
            if (!lockedRegion || !workScreen.contains(lockedRegion)) return;
            event.preventDefault();
            playExcavatorSound("action_error");
            showExcavatorNotice("Смена не активна. Сначала начните смену.", { variant: "shift-lock" });
        });
    }

    shell.querySelectorAll("[data-eo-dump-target]").forEach(function (target) {
        target.addEventListener("dragover", function (event) {
            event.preventDefault();
            target.classList.add("is-drop-ready");
        });
        target.addEventListener("dragleave", function () {
            target.classList.remove("is-drop-ready");
        });
        target.addEventListener("drop", function (event) {
            event.preventDefault();
            target.classList.remove("is-drop-ready");
            if (!selectedTruck || !canTruckLoad(selectedTruck)) {
                return;
            }
            selectDump(target.dataset.eoDumpTarget);
            postTruckLoaded(selectedTruck, target);
        });
        target.addEventListener("click", function (event) {
            if (target.dataset.eoSuppressClick === "1") {
                event.preventDefault();
                return;
            }
        });
    });

    if (window.ExcavatorDumpReturnSwipe) {
        shell.__eoDumpReturnSwipeController = window.ExcavatorDumpReturnSwipe.attach({
            shell: shell,
            targetSelector: "[data-eo-dump-target]",
            holdMs: 560,
            onHold: openDumpQueueModal,
            onReturn: returnLastTruckFromDump
        });
    }

    shell.querySelectorAll("[data-eo-dump-queue-close]").forEach(function (button) {
        button.addEventListener("click", closeDumpQueueModal);
    });

    function postDowntimeAction(payload) {
        if (typeof window.isAppRoleReadonly === "function" && window.isAppRoleReadonly()) {
            return Promise.reject(new Error("Роль неактивна — доступен только просмотр"));
        }
        var action = String(payload && payload.action || "");
        var eventType = action === "close" ? "excavator.downtime.ended" : "excavator.downtime.started";
        var activeDowntimeReference = downtimeCard ? String(downtimeCard.dataset.eoActiveDowntimeId || "") : "";
        var serverDowntimeId = /^\d+$/.test(activeDowntimeReference) ? Number(activeDowntimeReference) : null;
        var localDowntimeId = action === "close" && !serverDowntimeId ? activeDowntimeReference : "";
        return queueExcavatorFieldEvent(eventType, {
            reason_id: payload && payload.reason_id ? Number(payload.reason_id) : null,
            comment: String(payload && payload.comment || ""),
            downtime_id: serverDowntimeId,
            local_downtime_id: localDowntimeId || null
        }, {
            idPrefix: action === "close" ? "excavator-downtime-end" : "excavator-downtime-start",
            dependencyTypes: ["excavator.downtime.started", "excavator.downtime.ended"],
            localDowntimeIdFromEvent: action !== "close",
            localDowntimeId: localDowntimeId || null
        }).then(function (event) {
            if (action === "close") {
                return {
                    ok: true,
                    active: false,
                    event_id: event.event_id,
                    shift_total_seconds: downtimeCard ? downtimeCard.dataset.eoShiftDowntimeSeconds || 0 : 0,
                    local_saved: true
                };
            }
            var reasonButton = shell.querySelector('[data-eo-downtime-reason-id="' + String(payload.reason_id || "") + '"]');
            var reasonStatusMatch = reasonButton ? reasonButton.className.match(/status-([a-z]+)/) : null;
            return {
                ok: true,
                active: true,
                event_id: event.event_id,
                reason_id: payload.reason_id,
                started_at: event.occurred_at,
                elapsed_seconds: 0,
                shift_total_seconds: downtimeCard ? downtimeCard.dataset.eoShiftDowntimeSeconds || 0 : 0,
                active_counts_towards_shift: true,
                status_key: reasonStatusMatch ? reasonStatusMatch[1] : "yellow",
                equipment_state_code: reasonButton ? reasonButton.dataset.eoEquipmentState || "waiting" : "waiting",
                local_saved: true
            };
        });
    }

    var downtimeStatusSyncGeneration = 0;
    function syncDowntimeStatus() {
        if (!eventsScreen || eventsScreen.dataset.eoDowntimeAvailable !== "true") {
            return Promise.resolve(null);
        }
        if (downtimeActionPending || (shell && shell.dataset && shell.dataset.eoHasPendingFieldEvents === "true")) return Promise.resolve(null);
        downtimeStatusSyncGeneration += 1;
        var generation = downtimeStatusSyncGeneration;
        return fetch(shell.dataset.eoDowntimeUrl + "?_eo_status=" + Date.now(), {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest"
            }
        }).then(function (response) {
            if (!response.ok) throw new Error("Не удалось обновить простой");
            return response.json();
        }).then(function (payload) {
            if (
                generation !== downtimeStatusSyncGeneration
                || downtimeActionPending
                || !shell.isConnected
                || document.querySelector("[data-eo-shell]") !== shell
            ) {
                return null;
            }
            if (payload.active) {
                applyActiveDowntime(payload);
            } else {
                clearActiveDowntime(payload);
            }
            return payload;
        }).catch(function () {
            return null;
        });
    }

    downtimeReasonButtons.forEach(function (button) {
        button.addEventListener("click", function (event) {
            event.preventDefault();
            if (downtimeActionPending || button.disabled || button.getAttribute("aria-disabled") === "true") return;
            var previousSelectedReason = shell.querySelector("[data-eo-downtime-reason-id].is-selected");
            var previousDowntimeValue = downtimeInput ? downtimeInput.value : "";
            downtimeReasonButtons.forEach(function (item) {
                item.classList.remove("is-selected");
            });
            button.classList.add("is-selected");
            if (downtimeInput) {
                downtimeInput.value = button.dataset.eoReason || "";
            }
            if (button.dataset.eoDowntimeReasonId) {
                invalidateExcavatorWorkRefresh();
                downtimeStatusSyncGeneration += 1;
                downtimeActionPending = true;
                button.disabled = true;
                button.classList.add("is-pending");
                var reasonLabel = button.querySelector("[data-eo-reason-label]");
                if (reasonLabel) reasonLabel.textContent = "Запускаю";
                postDowntimeAction({
                    action: "start",
                    reason_id: button.dataset.eoDowntimeReasonId,
                    client_action_id: generateClientActionId("excavator-downtime")
                }).then(function (payload) {
                    playExcavatorVoice("action_ok", "voice_downtime_started");
                    applyActiveDowntime(payload);
                    if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                        try {
                            window.AppRealtime.wake("excavator_downtime_saved");
                        } catch (error) {
                            /* Сохранение уже подтверждено сервером; realtime лишь ускоряет синхронизацию. */
                        }
                    }
                }, function (error) {
                    playExcavatorVoice("action_error", "voice_action_failed");
                    downtimeReasonButtons.forEach(function (item) {
                        item.classList.remove("is-selected");
                    });
                    if (previousSelectedReason) previousSelectedReason.classList.add("is-selected");
                    if (downtimeInput) downtimeInput.value = previousDowntimeValue;
                    showExcavatorNotice(error.message || "Простой не сохранен");
                }).finally(function () {
                    downtimeActionPending = false;
                    button.disabled = !eventsScreen || eventsScreen.dataset.eoDowntimeAvailable !== "true";
                    button.classList.remove("is-pending");
                    if (reasonLabel) reasonLabel.textContent = button.dataset.eoReasonName || button.dataset.eoReason || "Причина простоя";
                });
            }
        });
    });

    var applySettings = shell.querySelector("[data-eo-apply-settings]");
    if (applySettings) {
        function markFaceDraftChanged() {
            if (faceSettingsPending) return;
            faceDraftTouched = true;
            updateApplySettingsState();
        }

        shell.querySelectorAll("[data-eo-face-horizon], [data-eo-face-block]").forEach(function (input) {
            input.addEventListener("input", markFaceDraftChanged);
            input.addEventListener("change", markFaceDraftChanged);
        });
        var settingsRockSelect = shell.querySelector("[data-eo-rock-select]");
        if (settingsRockSelect) {
            settingsRockSelect.addEventListener("change", markFaceDraftChanged);
        }
        updateApplySettingsState();

        function setApplySettingsLabel(text) {
            var label = applySettings.querySelector("[data-mobile-shift-label]");
            if (label) {
                label.textContent = text;
            } else {
                applySettings.textContent = text;
            }
        }

        applySettings.addEventListener("click", function (event) {
            event.preventDefault();
            if (applySettings.disabled || applySettings.classList.contains("is-pending")) {
                return;
            }
            var ids = syncDumpPointInputs();
            var rockSelect = shell.querySelector("[data-eo-rock-select]");
            var horizonInput = shell.querySelector("[data-eo-face-horizon]");
            var blockInput = shell.querySelector("[data-eo-face-block]");
            if (!settingsUrl || !rockSelect || !ids.length) {
                playExcavatorSound("action_error");
                showExcavatorNotice("Выберите породу и хотя бы одну точку разгрузки");
                setApplySettingsLabel("Применить настройки");
                return;
            }
            var payload = {
                client_action_id: generateClientActionId("excavator-settings"),
                rock_type_id: rockSelect.value || "",
                dump_point_ids: ids,
                loading_horizon: horizonInput ? horizonInput.value : "",
                loading_block: blockInput ? blockInput.value : ""
            };
            invalidateExcavatorWorkRefresh();
            setFaceSettingsPending(true);
            applySettings.disabled = true;
            applySettings.classList.add("is-pending");
            applySettings.setAttribute("aria-disabled", "true");
            setApplySettingsLabel("Сохраняю");
            fetch(settingsUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-CSRFToken": csrfToken,
                    "X-Requested-With": "XMLHttpRequest"
                },
                body: JSON.stringify(payload)
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (data) {
                    if (!response.ok || !data.ok) {
                        throw new Error(data.error || "Настройки не сохранены");
                    }
                    return data;
                });
            }).then(function (data) {
                playExcavatorVoice("action_ok", "voice_face_settings_saved");
                var savedIds = normalizeDumpPointIds(data.dump_point_ids || ids);
                replaceDraftDumpPointIds(savedIds);
                replacePersistedDumpPointIds(savedIds);
                syncDumpPointInputs();
                faceDraftTouched = false;
                appliedExcavatorSettingsSnapshot = currentExcavatorSettingsSnapshot();
                if (faceScreen) faceScreen.dataset.eoFaceDirty = "false";
                var nextTab = data.work_context_changed && data.active_downtime_reason
                    ? "events"
                    : "trucks";
                shell.dataset.eoActiveTab = nextTab;
                return refreshExcavatorWorkFromServer({ preserveTab: true, pendingOwner: "face" }).then(function (applied) {
                    if (!applied) {
                        shell.dataset.eoActiveTab = "face";
                        activateTab("face");
                        setFaceSettingsPending(false);
                        applySettings.classList.remove("is-pending");
                        setApplySettingsLabel("Применить настройки");
                        updateApplySettingsState();
                        showExcavatorNotice("Настройки сохранены. Экран обновится автоматически.");
                    }
                    if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                        try {
                            window.AppRealtime.wake("excavator_settings_saved");
                        } catch (error) {
                            /* Сервер уже подтвердил сохранение; realtime только ускоряет синхронизацию. */
                        }
                    }
                });
            }, function (error) {
                playExcavatorVoice("action_error", "voice_action_failed");
                setFaceSettingsPending(false);
                applySettings.classList.remove("is-pending");
                setApplySettingsLabel("Применить настройки");
                updateApplySettingsState();
                showExcavatorNotice(error.message || "Настройки не сохранены");
            });
        });
    }

    function parseShiftNumber(value) {
        var normalized = String(value || "").trim().replace(/\s+/g, "").replace(",", ".");
        if (!normalized) return null;
        var parsed = Number(normalized);
        return Number.isFinite(parsed) ? parsed : NaN;
    }

    function clearShiftErrors() {
        if (shiftFormError) {
            shiftFormError.hidden = true;
            shiftFormError.textContent = "";
        }
        shell.querySelectorAll("[data-eo-shift-field]").forEach(function (field) {
            field.classList.remove("has-error");
        });
        shell.querySelectorAll("[data-eo-shift-field-error]").forEach(function (node) {
            node.textContent = "";
        });
    }

    function showShiftErrors(message, fieldErrors) {
        playExcavatorVoice("action_error", "voice_action_failed");
        clearShiftErrors();
        if (shiftFormError) {
            shiftFormError.textContent = compactExcavatorNotice(message || "Проверьте показания.");
            shiftFormError.hidden = false;
        }
        Object.keys(fieldErrors || {}).forEach(function (name) {
            var field = shell.querySelector('[data-eo-shift-field="' + name + '"]');
            var errorNode = shell.querySelector('[data-eo-shift-field-error="' + name + '"]');
            if (field) field.classList.add("has-error");
            if (errorNode) errorNode.textContent = fieldErrors[name];
        });
        var firstErrorField = shell.querySelector("[data-eo-shift-field].has-error");
        var firstErrorInput = firstErrorField ? firstErrorField.querySelector("input") : null;
        window.requestAnimationFrame(function () {
            if (firstErrorInput) {
                try {
                    firstErrorInput.focus({preventScroll: true});
                } catch (error) {
                    firstErrorInput.focus();
                }
            }
        });
    }

    function calculatedShiftFuelLiters() {
        var percent = parseShiftNumber(shiftFuelInput && shiftFuelInput.value);
        var capacity = parseShiftNumber(shiftFuelInput && shiftFuelInput.dataset.eoFuelCapacity);
        if (percent === null || Number.isNaN(percent) || capacity === null || Number.isNaN(capacity)) {
            return null;
        }
        return Math.round(capacity * percent / 100);
    }

    function updateShiftFuelLiters() {
        if (!shiftFuelLiters) return;
        var liters = calculatedShiftFuelLiters();
        shiftFuelLiters.textContent = liters === null ? "—" : String(liters);
    }

    function validateShiftReadings(showErrors) {
        var errors = {};
        var fuel = parseShiftNumber(shiftFuelInput && shiftFuelInput.value);
        var hours = parseShiftNumber(shiftHoursInput && shiftHoursInput.value);
        var startHours = parseShiftNumber(shiftHoursInput && shiftHoursInput.dataset.eoStartEngineHours);
        if (fuel === null || Number.isNaN(fuel)) errors.fuel = "Укажите корректное число.";
        else if (fuel < 0) errors.fuel = "Значение не может быть меньше нуля.";
        else if (!Number.isInteger(fuel)) errors.fuel = "Укажите целое количество процентов.";
        else if (fuel > 100 && !isShiftCloseAction()) errors.fuel = "Укажите значение от 0 до 100%.";
        if (hours === null || Number.isNaN(hours)) errors.engine_hours = "Укажите корректное число.";
        else if (hours < 0) errors.engine_hours = "Значение не может быть меньше нуля.";
        else if (!Number.isInteger(hours)) errors.engine_hours = "Укажите целое число без точки и запятой.";
        if (showErrors) {
            if (Object.keys(errors).length) showShiftErrors("Проверьте показания.", errors);
            else clearShiftErrors();
        }
        return { valid: Object.keys(errors).length === 0, fuel: fuel, fuelLiters: calculatedShiftFuelLiters(), hours: hours, startHours: startHours, errors: errors };
    }

    function updateShiftActionState() {
        var validation = validateShiftReadings(false);
        if (!shiftButton || shiftButton.classList.contains("is-pending")) return;
        if (typeof window.isAppRoleReadonly === "function" && window.isAppRoleReadonly()) return;
        var isSoftBlocked = shiftServerBlocked || !validation.valid;
        shiftButton.disabled = false;
        shiftButton.dataset.eoShiftReadingsValid = validation.valid ? "true" : "false";
        shiftButton.setAttribute("aria-disabled", isSoftBlocked ? "true" : "false");
        shiftButton.setAttribute(
            "aria-label",
            shiftServerBlocked
                ? (shiftBlockMessage || "Смена сейчас недоступна")
                : validation.valid
                    ? (isShiftCloseAction()
                        ? "Удерживайте 1 секунду, чтобы закрыть смену"
                        : "Удерживайте 1 секунду, чтобы начать смену")
                    : "Заполните показания. Нажмите, чтобы увидеть подсказку"
        );
    }

    var shiftConfirmation = null;
    var shiftConfirmationRequesting = false;

    function hideShiftConfirmation() {
        shiftConfirmation = null;
        shiftConfirmationRequesting = false;
        if (shiftConfirmationModal) shiftConfirmationModal.hidden = true;
        if (shiftConfirmationAccept) shiftConfirmationAccept.disabled = false;
        if (document.body) document.body.classList.remove("modal-open");
    }

    function renderShiftConfirmationWarnings(warnings) {
        if (!shiftConfirmationWarnings) return;
        while (shiftConfirmationWarnings.firstChild) {
            shiftConfirmationWarnings.removeChild(shiftConfirmationWarnings.firstChild);
        }
        (Array.isArray(warnings) ? warnings : []).forEach(function (warning) {
            var item = document.createElement("li");
            var title = document.createElement("strong");
            var message = document.createElement("span");
            item.dataset.warningCode = String(warning.code || "warning");
            title.textContent = String(warning.title || "Проверьте показание");
            message.textContent = String(warning.message || "Проверьте введённое значение.");
            item.appendChild(title);
            item.appendChild(message);
            shiftConfirmationWarnings.appendChild(item);
        });
    }

    function showShiftConfirmation(payload, actionKey) {
        if (!shiftConfirmationModal) {
            showShiftErrors(payload.error || "Проверьте показания.", {});
            return;
        }
        clearShiftErrors();
        shiftConfirmation = {
            token: String(payload.confirmation_token || ""),
            actionKey: actionKey,
            warnings: Array.isArray(payload.warnings) ? payload.warnings : []
        };
        renderShiftConfirmationWarnings(shiftConfirmation.warnings);
        shiftConfirmationModal.hidden = false;
        if (document.body) document.body.classList.add("modal-open");
        window.requestAnimationFrame(function () {
            if (shiftConfirmationBack) shiftConfirmationBack.focus({preventScroll: true});
        });
    }

    [shiftFuelInput, shiftHoursInput].forEach(function (input) {
        if (!input) return;
        input.addEventListener("input", function () {
            hideShiftConfirmation();
            if (shiftScreen) shiftScreen.dataset.eoShiftDirty = "true";
            clearShiftErrors();
            updateShiftFuelLiters();
            updateShiftActionState();
        });
    });

    updateShiftFuelLiters();
    updateShiftActionState();

    var shiftPendingActionKey = "";
    var shiftPendingActionId = "";
    var shiftOtherRoleConfirmed = false;

    function resetShiftSubmissionUi(action) {
        shiftConfirmationRequesting = false;
        if (shiftConfirmationAccept) shiftConfirmationAccept.disabled = false;
        shiftButton.disabled = false;
        shiftButton.classList.remove("is-pending");
        if (shiftLabel) {
            shiftLabel.textContent = action === "close" ? "Закрыть смену" : "Начать смену";
        }
        updateShiftActionState();
    }

    function submitExcavatorShiftAction(options) {
        options = options || {};
        if (!shiftButton) {
            return Promise.resolve(false);
        }
        if (
            !shiftUrl
            || shiftButton.disabled
            || shiftButton.classList.contains("is-pending")
            || shiftServerBlocked
            || (typeof window.isAppRoleReadonly === "function" && window.isAppRoleReadonly())
        ) {
            return Promise.resolve(false);
        }
        var action = shiftButton.dataset.eoShiftAction || "toggle";
        var validation = validateShiftReadings(true);
        if (!validation.valid) {
            return Promise.resolve(false);
        }
        /* Одна открытая смена на сотрудника: смена в другой роли ещё открыта —
           после удержания спрашиваем «Завершить её и начать?», как у водителя,
           диспетчера и мастера; «да» добавляет подтверждение в запрос. */
        var otherRoleQuestion = action !== "close" ? (shiftButton.dataset.otherRoleShiftQuestion || "") : "";
        if (otherRoleQuestion && !shiftOtherRoleConfirmed && typeof window.openAppConfirmDialog === "function") {
            resetShiftHold();
            window.openAppConfirmDialog(otherRoleQuestion, function () {
                shiftOtherRoleConfirmed = true;
                submitExcavatorShiftAction();
            }, 0, shiftButton.dataset.otherRoleShiftAccept || "Завершить и начать");
            return Promise.resolve(false);
        }
        var shiftActionKey = [
            action,
            shell.dataset.eoShiftExcavatorId || shell.dataset.eoCurrentExcavatorId || "",
            shell.querySelector("[data-eo-shift-fuel]") ? shell.querySelector("[data-eo-shift-fuel]").value : "",
            shell.querySelector("[data-eo-shift-engine-hours]") ? shell.querySelector("[data-eo-shift-engine-hours]").value : ""
        ].join(":");
        if (options.expectedActionKey && options.expectedActionKey !== shiftActionKey) {
            hideShiftConfirmation();
            showShiftErrors("Показания изменились. Проверьте их и повторите закрытие смены.", {});
            return Promise.resolve(false);
        }
        if (shiftPendingActionKey !== shiftActionKey || !shiftPendingActionId) {
            shiftPendingActionKey = shiftActionKey;
            shiftPendingActionId = generateClientActionId("excavator-shift");
        }
        var payload = {
            action: action,
            client_action_id: shiftPendingActionId,
            shift_id: shell.dataset.nativeShiftId || "",
            excavator_id: shell.dataset.eoShiftExcavatorId || shell.dataset.eoCurrentExcavatorId || "",
            fuel_percent: shell.querySelector("[data-eo-shift-fuel]") ? shell.querySelector("[data-eo-shift-fuel]").value : "",
            fuel: validation.fuelLiters === null ? "" : String(validation.fuelLiters),
            engine_hours: shell.querySelector("[data-eo-shift-engine-hours]") ? shell.querySelector("[data-eo-shift-engine-hours]").value : "",
            close_other_role_shift: !!shiftOtherRoleConfirmed
        };
        if (options.confirmationToken) {
            payload.confirmation_token = String(options.confirmationToken);
        }
        shiftOtherRoleConfirmed = false;
        resetShiftHold();
        shiftButton.disabled = true;
        shiftButton.classList.add("is-pending");
        if (shiftLabel) {
            shiftLabel.textContent = action === "close" ? "Закрываем смену" : "Открываем смену";
        }
        var shiftRequest = action === "close"
            ? queueExcavatorFieldEvent("excavator.shift.closed", {
                shift_id: Number(payload.shift_id || 0),
                excavator_id: Number(payload.excavator_id || 0),
                fuel_percent: payload.fuel_percent,
                fuel: payload.fuel,
                engine_hours: payload.engine_hours,
                confirmation_token: payload.confirmation_token || ""
            }, {
                idPrefix: "excavator-shift-close",
                dependsOnAll: true,
                dependencyTypes: [
                    "excavator.trip.loaded",
                    "excavator.trip.loaded.cancelled",
                    "excavator.downtime.started",
                    "excavator.downtime.ended",
                    "excavator.shift.closed"
                ]
            }).then(function (event) { return {ok: true, local_saved: true, event_id: event.event_id}; })
            : fetch(shiftUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-CSRFToken": csrfToken,
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify(payload)
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (!response.ok || !data.ok) {
                    throw data;
                }
                return data;
            });
        });
        return shiftRequest.then(function (result) {
            hideShiftConfirmation();
            playExcavatorVoice(
                action === "close" ? "shift_end" : "shift_start",
                action === "close" ? "voice_shift_closed" : "voice_shift_opened"
            );
            shiftPendingActionKey = "";
            shiftPendingActionId = "";
            if (shiftScreen) shiftScreen.dataset.eoShiftDirty = "false";
            clearShiftErrors();
            if (result && result.local_saved) {
                applyLocalShiftClose();
                showExcavatorNotice(navigator.onLine === false
                    ? "Закрытие смены сохранено на телефоне"
                    : "Закрытие смены сохранено. Отправляем на сервер...");
                return true;
            }
            if (action === "open") activateTab("trucks");
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                try {
                    window.AppRealtime.wake("excavator_shift_saved");
                } catch (error) {
                    /* Сервер уже сохранил смену; realtime только ускоряет обновление. */
                }
            }
            return refreshExcavatorWorkFromServer({
                preserveTab: true,
                pendingOwner: "shift",
                suppressAssignmentAlert: true
            }).then(function (applied) {
                if (!applied) {
                    window.location.reload();
                }
            }, function () {
                window.location.reload();
            });
        }, function (error) {
            if (error && error.confirmation_required === true && error.confirmation_token) {
                resetShiftSubmissionUi(action);
                showShiftConfirmation(error, shiftActionKey);
                return false;
            }
            hideShiftConfirmation();
            /* Экран отрисован до того, как в другой роли открыли смену:
               сервер сам прислал вопрос — задаём его и повторяем с ответом. */
            var otherRole = error && error.code === "other_role_shift_open" ? error.other_role_shift : null;
            if (otherRole && otherRole.question && typeof window.openAppConfirmDialog === "function") {
                resetShiftSubmissionUi(action);
                shiftButton.dataset.otherRoleShiftQuestion = otherRole.question;
                shiftButton.dataset.otherRoleShiftAccept = otherRole.accept_label || "Завершить и начать";
                window.openAppConfirmDialog(otherRole.question, function () {
                    shiftOtherRoleConfirmed = true;
                    submitExcavatorShiftAction();
                }, 0, otherRole.accept_label || "Завершить и начать");
                return false;
            }
            showShiftErrors(error.error || error.message || "Смена не сохранена", error.field_errors || {});
            resetShiftSubmissionUi(action);
            return false;
        });
    }

    if (shiftConfirmationBack) {
        shiftConfirmationBack.addEventListener("click", function () {
            var firstWarning = shiftConfirmation && shiftConfirmation.warnings.length
                ? shiftConfirmation.warnings[0]
                : null;
            var fieldName = firstWarning ? String(firstWarning.field || "") : "";
            var input = fieldName === "fuel" ? shiftFuelInput : shiftHoursInput;
            hideShiftConfirmation();
            if (input) input.focus({preventScroll: true});
        });
    }

    if (shiftConfirmationAccept) {
        shiftConfirmationAccept.addEventListener("click", function () {
            if (!shiftConfirmation || shiftConfirmationRequesting) return;
            var confirmation = shiftConfirmation;
            shiftConfirmationRequesting = true;
            shiftConfirmationAccept.disabled = true;
            shiftConfirmationModal.hidden = true;
            if (document.body) document.body.classList.remove("modal-open");
            submitExcavatorShiftAction({
                confirmationToken: confirmation.token,
                expectedActionKey: confirmation.actionKey
            });
        });
    }

    if (shiftButton) {
        shiftHoldController = window.MobileShiftHold.bind(shiftButton, {
            holdMs: currentShiftHoldMs,
            readyLabel: isShiftCloseAction() ? "Закрыть смену" : "Начать смену",
            canStart: function () {
                return !shiftServerBlocked && validateShiftReadings(false).valid;
            },
            onBlockedPress: function () {
                if (shiftServerBlocked) {
                    playExcavatorVoice("action_error", "voice_action_failed");
                    showExcavatorNotice(shiftBlockMessage || "Смена сейчас недоступна.");
                    return;
                }
                validateShiftReadings(true);
            },
            onShortPress: function () {
                showExcavatorNotice(
                    isShiftCloseAction()
                        ? "Удерживайте 1 секунду, чтобы закрыть смену."
                        : "Удерживайте 1 секунду, чтобы начать смену."
                );
            },
            onComplete: submitExcavatorShiftAction
        });
    }

    if (closeEvent) {
        closeEvent.addEventListener("click", function (event) {
            event.preventDefault();
            if (
                downtimeActionPending
                || closeEvent.disabled
                || closeEvent.getAttribute("aria-disabled") === "true"
                || !downtimeCard
                || !downtimeCard.dataset.eoActiveDowntimeId
            ) return;

            invalidateExcavatorWorkRefresh();
            downtimeStatusSyncGeneration += 1;
            downtimeActionPending = true;
            closeEvent.disabled = true;
            closeEvent.classList.add("is-pending");
            closeEvent.setAttribute("aria-disabled", "true");
            if (closeEventLabel) closeEventLabel.textContent = "Завершаю";

            postDowntimeAction({
                action: "close",
                client_action_id: generateClientActionId("excavator-downtime-close")
            }).then(function (payload) {
                playExcavatorVoice("action_ok", "voice_downtime_finished");
                clearActiveDowntime(payload);
                if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                    try {
                        window.AppRealtime.wake("excavator_downtime_closed");
                    } catch (error) {
                        /* Серверный результат не откатывается из-за realtime-уведомления. */
                    }
                }
            }, function (error) {
                playExcavatorVoice("action_error", "voice_action_failed");
                showExcavatorNotice(error.message || "Простой не завершен");
                var canRetry = !!(
                    downtimeCard
                    && downtimeCard.dataset.eoActiveDowntimeId
                    && eventsScreen
                    && eventsScreen.dataset.eoDowntimeAvailable === "true"
                );
                closeEvent.disabled = !canRetry;
                closeEvent.classList.remove("is-pending");
                closeEvent.setAttribute("aria-disabled", canRetry ? "false" : "true");
                closeEvent.setAttribute("aria-label", canRetry ? "Завершить активный простой" : "Нет активного простоя");
                if (closeEventLabel) closeEventLabel.textContent = "Завершить простой";
            }).finally(function () {
                downtimeActionPending = false;
            });
        });
    }

    startDowntimeTimer({
        active: !!(downtimeCard && downtimeCard.dataset.eoActiveDowntimeId),
        reason_id: downtimeCard ? downtimeCard.dataset.eoActiveReasonId : "",
        active_counts_towards_shift: !!(downtimeCard && downtimeCard.dataset.eoActiveCountsTowardsShift === "true"),
        shift_total_seconds: downtimeCard ? downtimeCard.dataset.eoShiftDowntimeSeconds : 0
    });
    if (logoutButton) {
        window.MobileShiftHold.bind(logoutButton, {
            holdMs: logoutHoldMs,
            readyLabel: "Выйти",
            onShortPress: function () {
                showExcavatorNotice("Удерживайте кнопку");
            },
            onComplete: function () {
                logoutButton.classList.add("is-pending");
                logoutButton.disabled = true;
                if (logoutLabel) logoutLabel.textContent = "Выходим";
                var logoutUrl = logoutButton.dataset.eoLogoutUrl || "/logout/";
                if (typeof window.navigateAfterNativeConnectionStop === "function") {
                    window.navigateAfterNativeConnectionStop(logoutUrl);
                    return;
                }
                var stopPromise = window.NativeBackgroundConnection
                    && typeof window.NativeBackgroundConnection.stop === "function"
                    ? window.NativeBackgroundConnection.stop()
                    : null;
                Promise.resolve(stopPromise).finally(function () {
                    window.location.href = logoutUrl;
                });
            }
        });
    }
    if (selectedTruck && !canTruckLoad(selectedTruck)) {
        selectedTruck.classList.remove("is-selected");
        selectedTruck = null;
        if (assignmentInput) {
            assignmentInput.value = "";
        }
    }
    pickFirstActiveTruck();
    activateTab(restoredExcavatorTab(shell.dataset.eoActiveTab || "trucks"));
};
})();
