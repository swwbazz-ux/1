(function (root) {
    "use strict";

    var currentWorkspace = null;
    var currentController = null;
    var currentReturnController = null;
    var currentTripTimer = null;
    var tripTimerInterval = null;
    var currentTripProjection = null;
    var savingLocal = false;
    var dismissedRejectedManualLoadKey = "";
    var lastShownRejectedManualLoadKey = "";
    var workspaceRequestedOpen = false;
    var workspacePreferenceKnown = false;
    var automaticTripRefreshKey = "";
    var manualCancelRefreshKey = "";
    var manualCompletionPendingKey = "";

    function clone(value) {
        return JSON.parse(JSON.stringify(value));
    }

    function positive(value) {
        value = Number(value);
        return Number.isInteger(value) && value > 0 ? value : null;
    }

    function readWorkspaceContext() {
        var workspace = currentWorkspace || (
            root.document && root.document.querySelector("[data-driver-manual-workspace]")
        );
        var node = root.document && root.document.getElementById(
            "driver-manual-workspace-base-context-data"
        );
        if (!node && root.document) {
            node = root.document.getElementById("driver-manual-workspace-context-data");
        }
        if (node) {
            try {
                var parsed = JSON.parse(node.textContent || "{}");
                if (parsed && Object.keys(parsed).length) {
                    if (workspace) workspace.__driverManualBaseContext = clone(parsed);
                    return parsed;
                }
            } catch (error) {}
        }
        if (!workspace) return {};
        if (workspace.__driverManualBaseContext) return clone(workspace.__driverManualBaseContext);
        var reroutePoints = Array.prototype.slice.call(
            workspace.querySelectorAll("[data-driver-manual-reroute-point]")
        ).map(function (point) {
            return {
                id: positive(point.dataset.driverManualReroutePointId),
                name: String(point.dataset.driverManualReroutePointName || "")
            };
        }).filter(function (point) {
            return point.id && point.name;
        });
        if (positive(workspace.dataset.driverManualPrimaryExcavatorId)) {
            var primaryPoints = Array.prototype.slice.call(
                workspace.querySelectorAll("[data-driver-manual-primary-point]")
            ).map(function (point) {
                return {
                    id: positive(point.dataset.driverManualPrimaryPointId),
                    name: String(point.dataset.driverManualPrimaryPointName || ""),
                    transport_distance_km: String(point.dataset.driverManualPrimaryPointDistance || ""),
                    completed_count: Math.max(0, Number(point.dataset.driverManualPrimaryPointCount) || 0),
                    is_last_sent: point.dataset.driverManualPrimaryPointLast === "true",
                    one_off: false
                };
            }).filter(function (point) {
                return point.id && point.name;
            });
            var catalog = root.DriverFreeBucket
                && typeof root.DriverFreeBucket.currentCatalog === "function"
                ? root.DriverFreeBucket.currentCatalog()
                : null;
            var primaryItem = catalog && Array.isArray(catalog.excavators)
                ? catalog.excavators.find(function (item) {
                    return item && (
                        item.is_primary === true
                        || positive(item.id) === positive(workspace.dataset.driverManualPrimaryExcavatorId)
                    );
                })
                : null;
            var primaryContext = {
                source: "driver_manual",
                authority_type: "assignment",
                truck_id: positive(workspace.dataset.driverManualPrimaryTruckId),
                excavator_id: positive(workspace.dataset.driverManualPrimaryExcavatorId),
                excavator_label: String(workspace.dataset.driverManualPrimaryExcavatorLabel || ""),
                complex_label: String(workspace.dataset.driverManualPrimaryComplexLabel || ""),
                assignment_id: positive(workspace.dataset.driverManualPrimaryAssignmentId),
                free_bucket_acceptance_id: null,
                free_bucket_acceptance_local_id: "",
                placement_id: positive(workspace.dataset.driverManualPrimaryPlacementId),
                placement_updated_at: String(workspace.dataset.driverManualPrimaryPlacementUpdatedAt || ""),
                rock_type_id: positive(workspace.dataset.driverManualPrimaryRockTypeId),
                rock_type_name: String(workspace.dataset.driverManualPrimaryRockTypeName || ""),
                loading_horizon: String(workspace.dataset.driverManualPrimaryLoadingHorizon || ""),
                loading_block: String(workspace.dataset.driverManualPrimaryLoadingBlock || ""),
                dump_points: clone(
                    primaryPoints.length
                        ? primaryPoints
                        : primaryItem && primaryItem.dump_points || []
                ),
                reroute_points: clone(reroutePoints)
            };
            workspace.__driverManualBaseContext = clone(primaryContext);
            return primaryContext;
        }
        var source = workspace.querySelector("[data-driver-manual-source]");
        var context = {
            source: "driver_manual",
            authority_type: String(workspace.dataset.driverManualAuthorityType || ""),
            truck_id: positive(workspace.dataset.driverManualTruckId),
            excavator_id: positive(workspace.dataset.driverManualExcavatorId),
            excavator_label: String(workspace.dataset.driverManualExcavatorLabel || ""),
            complex_label: String(workspace.dataset.driverManualComplexLabel || ""),
            assignment_id: positive(workspace.dataset.driverManualAssignmentId || source && source.dataset.assignmentId),
            free_bucket_acceptance_id: positive(workspace.dataset.driverManualAcceptanceId),
            free_bucket_acceptance_local_id: String(workspace.dataset.driverManualAcceptanceLocalId || ""),
            placement_id: positive(workspace.dataset.driverManualPlacementId),
            placement_updated_at: String(workspace.dataset.driverManualPlacementUpdatedAt || ""),
            rock_type_id: positive(workspace.dataset.driverManualRockTypeId),
            rock_type_name: String(workspace.dataset.driverManualRockTypeName || ""),
            loading_horizon: String(workspace.dataset.driverManualLoadingHorizon || ""),
            loading_block: String(workspace.dataset.driverManualLoadingBlock || ""),
            dump_points: Array.from(workspace.querySelectorAll("[data-driver-manual-dump-target]")).map(function (target) {
                return {
                    id: positive(target.dataset.eoDumpTarget),
                    name: String(target.dataset.eoDumpName || ""),
                    transport_distance_km: String(target.dataset.eoDumpDistance || ""),
                    one_off: target.dataset.driverManualOneOff === "true"
                };
            }).filter(function (point) { return !!point.id; }),
            reroute_points: clone(reroutePoints)
        };
        workspace.__driverManualBaseContext = clone(context);
        return context;
    }

    function readWorkspaceTripContext() {
        var node = root.document && root.document.getElementById(
            "driver-manual-workspace-context-data"
        );
        if (node) {
            try {
                var parsed = JSON.parse(node.textContent || "{}");
                if (parsed && Object.keys(parsed).length) return parsed;
            } catch (error) {}
        }
        return readWorkspaceContext();
    }

    function activeContext() {
        var base = readWorkspaceContext();
        var freeBucketState = root.DriverFreeBucket && typeof root.DriverFreeBucket.currentState === "function"
            ? root.DriverFreeBucket.currentState()
            : null;
        if (!freeBucketState || !freeBucketState.active || !freeBucketState.selection || currentTripProjection) {
            return base;
        }
        var selection = freeBucketState.selection;
        return Object.assign({}, base, {
            authority_type: "free_bucket",
            assignment_id: null,
            free_bucket_acceptance_id: positive(freeBucketState.acceptance_id),
            free_bucket_acceptance_local_id: String(freeBucketState.acceptance_local_id || ""),
            excavator_id: positive(selection.id),
            excavator_label: String(selection.label || ""),
            complex_label: String(selection.complex_label || ""),
            placement_id: null,
            placement_updated_at: String(selection.placement_updated_at || ""),
            rock_type_id: positive(selection.rock_type_id),
            rock_type_name: String(selection.rock_type || ""),
            loading_horizon: String(selection.loading_horizon || ""),
            loading_block: String(selection.loading_block || ""),
            dump_points: clone(selection.dump_points || [])
        });
    }

    function resultText(state, detail) {
        if (state === "saving") return "Сохраняем на телефоне…";
        if (state === "pending") return root.navigator && root.navigator.onLine === false
            ? "Без сети · сохранено на телефоне"
            : "Сохранено на телефоне · отправляем";
        if (state === "confirmed") return "Подтверждено · рейс №" + String(detail || "");
        if (state === "cancel-pending") return root.navigator && root.navigator.onLine === false
            ? "ОТМЕНА СОХРАНЕНА · БЕЗ СЕТИ"
            : "ОТМЕНА СОХРАНЕНА · ОТПРАВЛЯЕМ";
        if (state === "cancelled") return "ПОСЛЕДНИЙ РЕЙС ОТМЕНЁН";
        if (state === "complete-pending") return root.navigator && root.navigator.onLine === false
            ? "РЕЙС ЗАВЕРШЁН НА ТЕЛЕФОНЕ · БЕЗ СЕТИ"
            : "РЕЙС ЗАВЕРШЁН · ОТПРАВЛЯЕМ";
        if (state === "completed") return "РЕЙС ЗАВЕРШЁН";
        if (state === "review") {
            var reviewMessage = String(detail || "Настройки места погрузки изменились после отметки.");
            return reviewMessage + " Отметьте погрузку заново.";
        }
        if (state === "storage-error") return "Не сохранено · повторите отправку";
        return "";
    }

    function playManualTone(context, startAt, startFrequency, endFrequency, duration) {
        if (!context) return false;
        try {
            var oscillator = context.createOscillator();
            var gain = context.createGain();
            oscillator.type = "sine";
            oscillator.frequency.setValueAtTime(startFrequency, startAt);
            oscillator.frequency.exponentialRampToValueAtTime(endFrequency, startAt + duration);
            gain.gain.setValueAtTime(.0001, startAt);
            gain.gain.exponentialRampToValueAtTime(.16, startAt + .018);
            gain.gain.exponentialRampToValueAtTime(.0001, startAt + duration);
            oscillator.connect(gain);
            gain.connect(context.destination);
            oscillator.start(startAt);
            oscillator.stop(startAt + duration);
            oscillator.addEventListener("ended", function () {
                oscillator.disconnect();
                gain.disconnect();
            }, {once: true});
            return true;
        } catch (error) {
            return false;
        }
    }

    function playManualFeedback(kind) {
        var patterns = {
            created: [55, 36, 95],
            completed: [75, 42, 135],
            cancelled: [58, 38, 92]
        };
        if (typeof root.driverHaptic === "function") {
            root.driverHaptic(patterns[kind] || patterns.created, kind === "completed" ? 210 : 180);
        } else if (root.navigator && typeof root.navigator.vibrate === "function") {
            try { root.navigator.vibrate(patterns[kind] || patterns.created); } catch (error) {}
        }
        var context = root.ExcavatorDashboardDrag
            && typeof root.ExcavatorDashboardDrag.preparePickupAudio === "function"
            ? root.ExcavatorDashboardDrag.preparePickupAudio()
            : null;
        if (!context) {
            if (typeof root.playDriverSound === "function") {
                root.playDriverSound(kind === "cancelled" ? "action_error" : "action_ok");
            }
            return false;
        }
        var now = context.currentTime;
        if (kind === "completed") {
            playManualTone(context, now, 610, 860, .12);
            playManualTone(context, now + .15, 760, 1180, .2);
        } else if (kind === "cancelled") {
            playManualTone(context, now, 720, 390, .2);
        } else {
            playManualTone(context, now, 520, 980, .2);
        }
        return true;
    }

    function playGestureHaptic(kind) {
        var patterns = {
            tap: [42],
            target: [48, 28, 48],
            returnArmed: [62, 30, 105],
            completeArmed: [78, 30, 135]
        };
        var pattern = patterns[kind] || patterns.tap;
        if (typeof root.driverHaptic === "function") return root.driverHaptic(pattern, 255);
        if (root.navigator && typeof root.navigator.vibrate === "function") {
            try { return root.navigator.vibrate(pattern); } catch (error) {}
        }
        return false;
    }

    function formatElapsedTime(totalSeconds) {
        var safeSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        var hours = Math.floor(safeSeconds / 3600);
        var minutes = Math.floor((safeSeconds % 3600) / 60);
        var seconds = safeSeconds % 60;
        return [hours, minutes, seconds].map(function (value) {
            return String(value).padStart(2, "0");
        }).join(":");
    }

    function tripTimerLabel(pointName) {
        return "С ПОГРУЗКИ · " + String(pointName || "ТОЧКА НЕ УКАЗАНА").trim();
    }

    function renderTripTimer(workspace, nowValue) {
        if (!workspace) return null;
        var timer = workspace.querySelector("[data-driver-manual-trip-timer]");
        if (!timer) return null;
        var label = timer.querySelector("[data-driver-manual-trip-timer-label]");
        var state = timer.querySelector("[data-driver-manual-trip-timer-state]");
        var destination = timer.querySelector("[data-driver-manual-trip-timer-destination]");
        var value = timer.querySelector("[data-driver-manual-trip-timer-value]");
        if (!currentTripTimer) {
            timer.classList.remove("is-active");
            timer.dataset.driverManualTimerActive = "false";
            delete timer.dataset.driverManualTimerStartedAt;
            delete timer.dataset.driverManualTimerPointName;
            if (state) state.textContent = "ОЖИДАЕТ ОТПРАВКИ";
            if (destination) destination.textContent = "ТОЧКА НЕ ВЫБРАНА";
            if (label && !state && !destination) label.textContent = "ОЖИДАЕТ ОТПРАВКИ";
            if (value) value.textContent = "00:00:00";
            timer.setAttribute("aria-label", "Таймер ожидает отправки в точку разгрузки");
            return {active: false, elapsedSeconds: 0, formatted: "00:00:00"};
        }
        currentTripTimer.workspace = workspace;
        var now = Number(nowValue);
        if (!Number.isFinite(now)) now = Date.now();
        var elapsedSeconds = Math.max(0, Math.floor((now - currentTripTimer.startedAt) / 1000));
        var formatted = formatElapsedTime(elapsedSeconds);
        var copy = tripTimerLabel(currentTripTimer.pointName);
        timer.classList.add("is-active");
        timer.dataset.driverManualTimerActive = "true";
        timer.dataset.driverManualTimerStartedAt = String(currentTripTimer.startedAt);
        timer.dataset.driverManualTimerPointName = currentTripTimer.pointName;
        if (state) state.textContent = "С ПОГРУЗКИ";
        if (destination) destination.textContent = currentTripTimer.pointName || "ТОЧКА НЕ УКАЗАНА";
        if (label && !state && !destination) label.textContent = copy;
        if (value) value.textContent = formatted;
        timer.setAttribute("aria-label", copy + ", прошло " + formatted);
        return {active: true, elapsedSeconds: elapsedSeconds, formatted: formatted, pointName: currentTripTimer.pointName};
    }

    function ensureTripTimerTick() {
        if (tripTimerInterval || !currentTripTimer || typeof root.setInterval !== "function") return;
        tripTimerInterval = root.setInterval(function () {
            if (!currentTripTimer) return;
            renderTripTimer(currentTripTimer.workspace || currentWorkspace);
        }, 1000);
    }

    function startTripTimer(workspace, pointName, startedAt) {
        var safeStartedAt = Number(startedAt);
        if (!Number.isFinite(safeStartedAt)) safeStartedAt = Date.now();
        currentTripTimer = {
            workspace: workspace || currentWorkspace,
            pointName: String(pointName || "").trim(),
            startedAt: safeStartedAt
        };
        renderTripTimer(currentTripTimer.workspace, safeStartedAt);
        ensureTripTimerTick();
        return currentTripTimer;
    }

    function stopTripTimer(workspace) {
        currentTripTimer = null;
        if (tripTimerInterval && typeof root.clearInterval === "function") {
            root.clearInterval(tripTimerInterval);
            tripTimerInterval = null;
        }
        return renderTripTimer(workspace || currentWorkspace);
    }

    function fitSourceTitle(source) {
        var title = source && source.querySelector ? source.querySelector("strong") : null;
        if (!title) return null;
        title.classList.remove("is-driver-manual-title-wrapped");
        title.style.removeProperty("font-size");
        var computed = root.getComputedStyle ? root.getComputedStyle(title) : null;
        var maxSize = Math.max(10, parseFloat(computed && computed.fontSize || "30") || 30);
        var available = Number(title.clientWidth || 0);
        if (!available) return maxSize;
        title.style.setProperty("font-size", maxSize + "px", "important");
        var natural = Math.max(1, Number(title.scrollWidth || available));
        var fitted = Math.max(10, Math.min(maxSize, maxSize * available / natural));
        title.style.setProperty("font-size", fitted.toFixed(2) + "px", "important");
        title.classList.toggle(
            "is-driver-manual-title-wrapped",
            Number(title.scrollWidth || 0) > Number(title.clientWidth || 0) + 1
        );
        return fitted;
    }

    /* Строка «ЭКС-1 · Гор. 75/Бл. 52 · Окисленная руда» заменяет собой
       раздел, который раньше был отдельной шапкой (eo-topbar, убрана по
       просьбе пользователя) — тот же общий модуль equipment-label-fit-v1.js,
       который уже вмещает номера техники и точки разгрузки без обрезки:
       кегль вниз, перенос на вторую строку (тут высоты хватает), сжатие,
       и только в крайнем случае — кегль ниже привычного пола. Обрезки не
       бывает никогда. */
    function fitFaceSummary(workspace) {
        var summary = workspace && workspace.querySelector ? workspace.querySelector("[data-driver-manual-face-summary]") : null;
        if (!summary) return null;
        var fitter = root.EquipmentLabelFit;
        if (!fitter || typeof fitter.fit !== "function") return null;
        return fitter.fit(summary, {allowWrap: true});
    }

    function updateManualTripCount(workspace, pointId, delta, adjustmentId) {
        if (!workspace || !positive(pointId) || !delta) return null;
        var key = String(adjustmentId || "");
        if (!workspace.__driverManualCountAdjustments) {
            workspace.__driverManualCountAdjustments = Object.create(null);
        }
        if (key && workspace.__driverManualCountAdjustments[key]) return null;
        var selector = '[data-driver-manual-dump-target][data-eo-dump-target="' + String(pointId) + '"]';
        var target = workspace.querySelector(selector);
        if (!target) return null;
        var next = Math.max(0, Number(target.dataset.driverManualCompletedCount || 0) + Number(delta));
        function applyCount(node) {
            if (!node) return;
            node.dataset.driverManualCompletedCount = String(next);
            var count = node.querySelector && node.querySelector(".eo-dashboard-unload-top small");
            if (count) {
                count.textContent = String(next);
                count.setAttribute("aria-label", "Рейсов: " + String(next));
            }
            node.setAttribute(
                "aria-label",
                String(node.dataset.eoDumpName || "") + ": рейсов " + String(next)
                    + (node.classList.contains("is-last-dump") ? "; последняя точка отправки" : "")
            );
        }
        applyCount(target);
        workspace.querySelectorAll('[data-driver-manual-primary-point-id="' + String(pointId) + '"]').forEach(function (point) {
            point.dataset.driverManualPrimaryPointCount = String(next);
        });
        if (workspace.__driverManualBaseContext && Array.isArray(workspace.__driverManualBaseContext.dump_points)) {
            workspace.__driverManualBaseContext.dump_points.forEach(function (point) {
                if (positive(point && point.id) === positive(pointId)) point.completed_count = next;
            });
        }
        Object.keys(workspace.__driverManualTargetCache || {}).forEach(function (cacheKey) {
            (workspace.__driverManualTargetCache[cacheKey] || []).forEach(function (cached) {
                if (positive(cached && cached.dataset && cached.dataset.eoDumpTarget) === positive(pointId)) {
                    applyCount(cached);
                }
            });
        });
        if (key) workspace.__driverManualCountAdjustments[key] = true;
        return next;
    }

    function markLastDump(workspace, pointId) {
        if (!workspace) return null;
        var selectedId = String(pointId || "");
        var selected = null;
        workspace.querySelectorAll("[data-driver-manual-dump-target]").forEach(function (target) {
            var isLast = !!selectedId && String(target.dataset.eoDumpTarget || "") === selectedId;
            target.classList.toggle("is-last-dump", isLast);
            target.classList.toggle("is-active-manual-trip", isLast && !!currentTripProjection);
            target.dataset.driverManualLastSent = isLast ? "true" : "false";
            if (isLast && currentTripProjection) {
                target.dataset.eoHasPendingTrucks = "true";
                target.dataset.eoReturnEnabled = "true";
            } else {
                delete target.dataset.eoHasPendingTrucks;
                delete target.dataset.eoReturnEnabled;
            }
            if (isLast) {
                target.setAttribute("aria-current", "true");
                selected = target;
            } else {
                target.removeAttribute("aria-current");
            }
            var pointName = String(target.dataset.eoDumpName || "");
            var count = String(target.dataset.driverManualCompletedCount || "0");
            target.setAttribute(
                "aria-label",
                pointName + ": рейсов " + count + (isLast ? "; последняя точка отправки" : "")
            );
        });
        return selected;
    }

    function isTerminalState(state) {
        return ["conflict", "auth_required", "invalid"].indexOf(String(state || "")) >= 0;
    }

    function sourceShouldBeLocked(isSaving, projection) {
        return Boolean(isSaving || projection);
    }

    function setManualExitAvailability(workspace) {
        if (!workspace) return true;
        var blocked = Boolean(savingLocal || currentTripProjection);
        workspace.querySelectorAll("[data-driver-manual-close]").forEach(function (control) {
            control.disabled = blocked;
            control.setAttribute("aria-disabled", blocked ? "true" : "false");
            var hint = control.querySelector("em");
            if (hint) {
                if (!hint.dataset.driverManualDefaultText) {
                    hint.dataset.driverManualDefaultText = hint.textContent;
                }
                hint.textContent = blocked
                    ? "Сначала завершите рейс свайпом вниз"
                    : hint.dataset.driverManualDefaultText;
            }
        });
        return !blocked;
    }

    /* Отклонённая (conflict/invalid/auth_required) отметка НЕ становится
       текущей проекцией: раньше она попадала сюда наравне с обычной и
       дальше currentTripProjection держал источник и выход заблокированными
       навсегда — активной плитки нет, свайп завершать нечего, а снять запись
       было нечем. Тот же приём уже стоял рядом, в latestManualCancel и
       completionWins — здесь его просто не было. */
    function manualLoadFromEvents(events) {
        return (Array.isArray(events) ? events : [])
            .filter(function (event) {
                return event && event.event_type === "driver.trip.loaded" && !isTerminalState(event.state);
            })
            .slice()
            .sort(function (left, right) { return Number(left.sequence || 0) - Number(right.sequence || 0); })
            .pop() || null;
    }

    /* Тот же самый последний driver.trip.loaded, но БЕЗ фильтра по
       состоянию — нужен только чтобы показать сообщение об отказе, а не
       чтобы решать, блокировать ли экран. Если реальный последний load
       отклонён, а manualLoadFromEvents вернул более раннюю живую запись
       или ничего — сравнение по sequence отличит «есть свежий отказ,
       который стоит показать» от «отказ устарел, поверх него уже есть
       новая попытка». */
    function latestManualLoadEventIncludingRejected(events) {
        return (Array.isArray(events) ? events : [])
            .filter(function (event) { return event && event.event_type === "driver.trip.loaded"; })
            .slice()
            .sort(function (left, right) { return Number(left.sequence || 0) - Number(right.sequence || 0); })
            .pop() || null;
    }

    function rejectedManualLoadNotice(events, projected) {
        var latest = latestManualLoadEventIncludingRejected(events);
        if (!latest || !isTerminalState(latest.state)) return null;
        if (projected && Number(projected.sequence || 0) >= Number(latest.sequence || 0)) return null;
        return latest;
    }

    function latestManualCancel(events) {
        return (Array.isArray(events) ? events : [])
            .filter(function (event) {
                return event
                    && event.event_type === "driver.trip.loaded.cancelled"
                    && !isTerminalState(event.state);
            })
            .slice()
            .sort(function (left, right) { return Number(left.sequence || 0) - Number(right.sequence || 0); })
            .pop() || null;
    }

    function manualCancelMatches(cancelEvent, projection, serverTripId) {
        if (!cancelEvent) return false;
        var cancelTripId = positive(cancelEvent.trip_id)
            || positive(cancelEvent.server_ids && cancelEvent.server_ids.trip_id);
        var cancelLocalId = String(cancelEvent.local_trip_id || "");
        if (serverTripId && cancelTripId === positive(serverTripId)) return true;
        if (projection && cancelTripId && cancelTripId === positive(projection.trip_id)) return true;
        return !!(
            projection
            && cancelLocalId
            && cancelLocalId === String(projection.local_trip_id || "")
        );
    }

    function manualCancelWins(cancelEvent, confirmedCancel, projection, serverTripId) {
        if (!cancelEvent) return false;
        var matchesCurrent = manualCancelMatches(cancelEvent, projection, serverTripId)
            || (!!confirmedCancel && !serverTripId);
        if (!matchesCurrent) return false;
        return !projection
            || Date.parse(cancelEvent.occurred_at || 0) >= Date.parse(projection.occurred_at || 0);
    }

    function setResult(workspace, state, detail, sticky) {
        var result = workspace && workspace.querySelector("[data-driver-manual-result]");
        if (!result) return;
        var nextText = resultText(state, detail);
        var nextKey = String(state || "") + "|" + nextText;
        if (state === "confirmed" && result.dataset.driverManualResultKey === nextKey) return;
        root.clearTimeout(result.__driverManualHideTimer);
        result.dataset.driverManualResultKey = nextKey;
        result.dataset.driverManualResultState = String(state || "");
        result.textContent = nextText;
        result.title = nextText;
        result.hidden = !result.textContent;
        if (!sticky && !result.hidden) {
            result.__driverManualHideTimer = root.setTimeout(
                function () { result.hidden = true; },
                state === "confirmed" ? 1200 : 3200
            );
        }
    }

    /* Отклонённая отметка (conflict/invalid/auth_required) с 25.09.2026
       НИКОГДА не блокирует источник и выход — см. manualLoadFromEvents
       выше: она попросту не становится currentTripProjection, поэтому
       экран у следующего же водителя разблокирован сам, без единого
       касания, сразу как только применится свежая разметка (в том числе
       после перезапуска приложения на новой оболочке — водитель мог быть
       за рулём и физически не иметь возможности нажимать на экран).
       «Понятно» здесь — не выход из блокировки (блокировки нет), а просто
       способ убрать с экрана прочитанное сообщение об отказе. */
    function toggleRejectedTripAck(workspace, show) {
        var button = workspace && workspace.querySelector("[data-driver-manual-dismiss-rejected]");
        if (!button) return;
        button.hidden = !show;
    }

    function showRejectedManualLoadNotice(workspace, rejectedEvent) {
        var key = String(
            rejectedEvent.event_id || rejectedEvent.local_trip_id || rejectedEvent.occurred_at || ""
        );
        if (key && key === dismissedRejectedManualLoadKey) {
            toggleRejectedTripAck(workspace, false);
            setResult(workspace, "", null, false);
            return;
        }
        lastShownRejectedManualLoadKey = key;
        setResult(workspace, "review", rejectedEvent.last_error && rejectedEvent.last_error.message, true);
        toggleRejectedTripAck(workspace, true);
    }

    function dismissRejectedTripProjection(workspace) {
        dismissedRejectedManualLoadKey = lastShownRejectedManualLoadKey;
        toggleRejectedTripAck(workspace, false);
        setResult(workspace, "", null, false);
    }
    /* Смена точки разгрузки в ручном режиме отклоняется по своим причинам
       (точка деактивирована, рейс уже не редактируется, точку уже меняли
       позже) — сервер тут забой не проверяет вовсе, путь отдельный от
       погрузки. Блокировки здесь и не было (источник и выход держит только
       currentTripProjection погрузки, не эта запись) — не хватало только
       того, чтобы водитель узнал, что выбор не применился. Короткое
       сообщение само пропадает через несколько секунд: держать его на
       экране постоянно незачем, кнопка «Понятно» тут не нужна.
       lastShownRejectedDumpPointKey не даёт заново показывать ту же самую
       запись на каждом повторном рендере (проекция перерисовывается часто,
       событие в очереди остаётся тем же). */
    var lastShownRejectedDumpPointKey = "";
    function showRejectedDumpPointChangeNotice(workspace, rejectedEvent) {
        var notice = workspace && workspace.querySelector("[data-driver-manual-point-notice]");
        if (!notice) return;
        var key = String(
            rejectedEvent.event_id || rejectedEvent.local_trip_id || rejectedEvent.occurred_at || ""
        );
        if (key && key === lastShownRejectedDumpPointKey) return;
        lastShownRejectedDumpPointKey = key;
        var reason = String(
            (rejectedEvent.last_error && rejectedEvent.last_error.message) || "Точка не изменена."
        );
        notice.textContent = reason + " Выберите точку снова.";
        notice.hidden = false;
        root.clearTimeout(notice.__driverManualPointNoticeTimer);
        notice.__driverManualPointNoticeTimer = root.setTimeout(function () {
            notice.hidden = true;
        }, 4200);
    }

    function setSourceLocked(workspace, locked) {
        var source = workspace && workspace.querySelector("[data-driver-manual-source]");
        if (source) {
            source.disabled = !!locked;
            source.setAttribute("aria-disabled", locked ? "true" : "false");
            source.classList.toggle("is-load-blocked", !!locked);
            source.dataset.eoCanLoad = locked ? "0" : "1";
            source.draggable = !locked;
        }
        setManualExitAvailability(workspace);
    }

    function projectionPointName(projection) {
        var snapshot = projection && projection.context_snapshot || {};
        var payload = projection && projection.payload || {};
        var pointId = String(payload.dump_point_id || snapshot.selected_dump_point_id || "");
        var points = Array.isArray(snapshot.dump_points) ? snapshot.dump_points : [];
        var point = points.find(function (item) { return String(item && item.id || "") === pointId; });
        return String(point && point.name || snapshot.selected_dump_point_name || "");
    }

    function serverTripProjectionContext(shell, context) {
        context = clone(context || {});
        var pointId = positive(shell && shell.dataset && shell.dataset.driverActualDumpPointId)
            || positive(context.selected_dump_point_id);
        var assignedPointId = positive(shell && shell.dataset && shell.dataset.driverAssignedDumpPointId)
            || positive(context.assigned_dump_point_id)
            || pointId;
        var pointName = String(
            shell && shell.dataset && shell.dataset.driverActualDumpPointName
            || context.selected_dump_point_name
            || ""
        );
        if (pointId) context.selected_dump_point_id = pointId;
        if (assignedPointId) context.assigned_dump_point_id = assignedPointId;
        if (pointName) context.selected_dump_point_name = pointName;
        return {
            context_snapshot: context,
            payload: {dump_point_id: pointId, assigned_dump_point_id: assignedPointId}
        };
    }

    function requestAutomaticTripRefresh(receipt) {
        var tripId = positive(receipt && receipt.server_ids && receipt.server_ids.trip_id);
        var eventId = String(receipt && receipt.event_id || "");
        var refreshKey = tripId ? "trip:" + tripId : (eventId ? "event:" + eventId : "");
        if (!refreshKey || refreshKey === automaticTripRefreshKey) return false;
        if (!root.AppRealtime || typeof root.AppRealtime.requestReconcile !== "function") return false;
        var requested = root.AppRealtime.requestReconcile(
            "driver_manual_automatic_trip_confirmed",
            Number(receipt && receipt.version || 0)
        );
        if (requested === false) return false;
        automaticTripRefreshKey = refreshKey;
        return true;
    }

    function requestManualCancellationRefresh(receipt) {
        var refreshKey = String(receipt && receipt.event_id || "");
        if (!refreshKey || refreshKey === manualCancelRefreshKey) return false;
        if (!root.AppRealtime || typeof root.AppRealtime.requestReconcile !== "function") return false;
        var requested = root.AppRealtime.requestReconcile(
            "driver_manual_trip_cancelled",
            Number(receipt && receipt.version || 0)
        );
        if (requested === false) return false;
        manualCancelRefreshKey = refreshKey;
        return true;
    }

    function latestManualCompletion(events) {
        return (Array.isArray(events) ? events : []).filter(function (event) {
            return event && event.event_type === "driver.trip.manual_completed";
        }).slice().sort(function (left, right) {
            return Number(left.sequence || 0) - Number(right.sequence || 0);
        }).pop() || null;
    }

    function renderProjection(workspace, events, receipt) {
        workspace = workspace || currentWorkspace || root.document.querySelector("[data-driver-manual-workspace]");
        if (!workspace) return null;
        var shell = workspace.closest("[data-driver-shell]");
        var serverOrigin = shell && String(shell.dataset.driverActiveTripOrigin || "");
        var serverTripId = shell && positive(shell.dataset.driverActiveTripId);
        var serverLoadedAt = shell && String(shell.dataset.driverActiveTripLoadedAt || "");
        var projected = manualLoadFromEvents(events);
        var completion = latestManualCompletion(events);
        var queuedCancel = latestManualCancel(events);
        var confirmedCancel = receipt && receipt.event_type === "driver.trip.loaded.cancelled"
            ? receipt
            : null;
        var activeCancel = queuedCancel || confirmedCancel;
        var completionWins = completion
            && ["conflict", "auth_required", "invalid"].indexOf(String(completion.state || "pending")) < 0
            && (!projected || Number(completion.sequence || 0) > Number(projected.sequence || 0));
        if (completionWins) {
            manualCompletionPendingKey = String(completion.event_id || manualCompletionPendingKey || "pending");
            currentTripProjection = null;
            stopTripTimer(workspace);
            markLastDump(workspace, positive(completion.payload && completion.payload.dump_point_id));
            toggleRejectedTripAck(workspace, false);
            setSourceLocked(workspace, savingLocal);
            setManualExitAvailability(workspace);
            setResult(workspace, "complete-pending", null, true);
            updatePointAction(workspace);
            return {state: "completing"};
        }
        if (serverOrigin !== "driver_manual") manualCompletionPendingKey = "";
        if (manualCompletionPendingKey && serverOrigin === "driver_manual" && !projected) {
            currentTripProjection = null;
            stopTripTimer(workspace);
            setSourceLocked(workspace, savingLocal);
            setManualExitAvailability(workspace);
            setResult(workspace, "complete-pending", null, true);
            return {state: "completing"};
        }
        if (manualCancelWins(activeCancel, confirmedCancel, projected, serverTripId)) {
            var cancelledPointId = positive(
                activeCancel.context_snapshot && activeCancel.context_snapshot.assigned_dump_point_id
            ) || positive(
                projected && projected.payload && projected.payload.assigned_dump_point_id
            ) || positive(
                projected && projected.context_snapshot && projected.context_snapshot.assigned_dump_point_id
            ) || positive(
                activeCancel.context_snapshot && activeCancel.context_snapshot.selected_dump_point_id
            ) || positive(projected && projected.payload && projected.payload.dump_point_id);
            updateManualTripCount(
                workspace,
                cancelledPointId,
                -1,
                "cancel:" + String(activeCancel.event_id || "")
            );
            currentTripProjection = null;
            stopTripTimer(workspace);
            markLastDump(workspace, null);
            restoreStandardTargets(workspace);
            toggleRejectedTripAck(workspace, false);
            setSourceLocked(workspace, sourceShouldBeLocked(savingLocal, null));
            setResult(workspace, queuedCancel ? "cancel-pending" : "cancelled", null, !!queuedCancel);
            if (confirmedCancel) requestManualCancellationRefresh(confirmedCancel);
            updatePointAction(workspace);
            return {state: queuedCancel ? "cancelling" : "cancelled"};
        }
        var unload = serverOrigin === "driver_manual" ? null : (Array.isArray(events) ? events : []).find(function (event) {
            return event.event_type === "driver.trip.unloaded" && (
                (serverTripId && positive(event.trip_id) === serverTripId)
                || (projected && String(event.local_trip_id || "") === String(projected.local_trip_id || ""))
            );
        });
        if (unload && !isTerminalState(unload.state)) {
            currentTripProjection = null;
            stopTripTimer(workspace);
            setSourceLocked(workspace, true);
            closeWorkspace(workspace, {preserveRequest: false});
            return {state: "unloading"};
        }
        if (serverOrigin === "excavator") {
            currentTripProjection = null;
            stopTripTimer(workspace);
            setSourceLocked(workspace, true);
            closeWorkspace(workspace, {preserveRequest: false});
            return {state: "automatic", tripId: serverTripId};
        }
        if (serverOrigin === "driver_manual" && serverTripId) {
            var serverContext = readWorkspaceTripContext();
            var serverProjectionContext = serverTripProjectionContext(shell, serverContext);
            currentTripProjection = {
                state: "confirmed",
                event_id: String(serverContext.event_id || ""),
                trip_id: serverTripId,
                local_trip_id: receipt && receipt.local_trip_id || String(serverContext.local_trip_id || ""),
                can_depend_on_prior: !!receipt,
                occurred_at: serverLoadedAt || serverContext.active_trip_loaded_at,
                payload: serverProjectionContext.payload,
                context_snapshot: serverProjectionContext.context_snapshot
            };
            syncWorkspaceContext(workspace);
            startTripTimer(workspace, projectionPointName(currentTripProjection), Date.parse(currentTripProjection.occurred_at));
            markLastDump(workspace, currentTripProjection.payload.dump_point_id);
            setSourceLocked(workspace, sourceShouldBeLocked(savingLocal, currentTripProjection));
            setResult(workspace, "confirmed", serverTripId, false);
            updatePointAction(workspace);
            return currentTripProjection;
        }
        if (!projected && receipt && receipt.trip_origin === "excavator") {
            currentTripProjection = null;
            stopTripTimer(workspace);
            setSourceLocked(workspace, true);
            closeWorkspace(workspace, {preserveRequest: false});
            requestAutomaticTripRefresh(receipt);
            return {state: "automatic", tripId: positive(receipt.server_ids && receipt.server_ids.trip_id)};
        }
        if (
            !projected
            && receipt
            && receipt.event_type === "driver.trip.loaded"
            && receipt.trip_origin === "driver_manual"
        ) {
            projected = {
                state: "confirmed",
                event_id: receipt.event_id,
                local_trip_id: receipt.local_trip_id,
                can_depend_on_prior: true,
                occurred_at: receipt.occurred_at,
                payload: receipt.payload || {},
                context_snapshot: receipt.context_snapshot || {},
                trip_id: positive(receipt.server_ids && receipt.server_ids.trip_id)
            };
        }
        currentTripProjection = projected;
        if (!projected) {
            stopTripTimer(workspace);
            var rejectedNotice = rejectedManualLoadNotice(events, projected);
            if (rejectedNotice) {
                showRejectedManualLoadNotice(workspace, rejectedNotice);
            } else {
                toggleRejectedTripAck(workspace, false);
            }
            /* Источник и выход разблокируются независимо от того, есть ли
               сообщение об отказе на экране: currentTripProjection пуст, а
               значит держать их нечем — ровно так, будто рейса никогда не
               было. */
            setSourceLocked(workspace, sourceShouldBeLocked(savingLocal, null));
            updatePointAction(workspace);
            return null;
        }
        var latestPoint = (Array.isArray(events) ? events : [])
            .filter(function (event) {
                return event.event_type === "driver.trip.dump_point_changed" && (
                    (projected.trip_id && positive(event.trip_id) === positive(projected.trip_id))
                    || (
                        projected.local_trip_id
                        && String(event.local_trip_id || "") === String(projected.local_trip_id)
                    )
                );
            })
            .slice()
            .sort(function (left, right) { return Number(left.sequence || 0) - Number(right.sequence || 0); })
            .pop();
        if (latestPoint && !isTerminalState(latestPoint.state)) {
            var assignedPointId = positive(projected.payload && projected.payload.assigned_dump_point_id)
                || positive(projected.context_snapshot && projected.context_snapshot.assigned_dump_point_id)
                || positive(projected.payload && projected.payload.dump_point_id);
            projected.payload = Object.assign({}, projected.payload || {}, {
                assigned_dump_point_id: assignedPointId,
                dump_point_id: positive(latestPoint.payload && latestPoint.payload.dump_point_id)
            });
            projected.context_snapshot = Object.assign({}, projected.context_snapshot || {}, {
                assigned_dump_point_id: assignedPointId,
                selected_dump_point_id: positive(latestPoint.payload && latestPoint.payload.dump_point_id),
                selected_dump_point_name: String(
                    latestPoint.context_snapshot
                    && latestPoint.context_snapshot.selected_dump_point_name
                    || ""
                )
            });
        } else if (latestPoint && isTerminalState(latestPoint.state)) {
            // Выбор не применился: прежняя точка в payload/context_snapshot
            // остаётся как есть — это и есть правильное поведение, просто
            // теперь водитель об этом узнаёт, а не молчит вместе с экраном.
            showRejectedDumpPointChangeNotice(workspace, latestPoint);
        }
        var pointName = projectionPointName(projected);
        // projected здесь никогда не бывает terminal-состояния: отклонённые
        // записи manualLoadFromEvents отфильтровывает, прежде чем они дойдут
        // досюда — см. rejectedManualLoadNotice ниже, где отказ только
        // показывается, но не блокирует.
        projected.can_depend_on_prior = projected.can_depend_on_prior !== false;
        syncWorkspaceContext(workspace);
        if (projected.state !== "confirmed" && !serverTripId) {
            updateManualTripCount(
                workspace,
                projected.payload && projected.payload.dump_point_id,
                1,
                "load:" + String(projected.event_id || projected.local_trip_id || "")
            );
        }
        markLastDump(workspace, projected.payload && projected.payload.dump_point_id);
        startTripTimer(workspace, pointName, Date.parse(projected.occurred_at));
        toggleRejectedTripAck(workspace, false);
        setSourceLocked(workspace, sourceShouldBeLocked(savingLocal, projected));
        setResult(
            workspace,
            projected.state === "confirmed" ? "confirmed" : "pending",
            projected.trip_id || (receipt && receipt.server_ids && receipt.server_ids.trip_id),
            projected.state !== "confirmed"
        );
        updatePointAction(workspace);
        return projected;
    }

    function restoreProjection(outbox, workspace) {
        workspace = workspace || currentWorkspace || root.document.querySelector("[data-driver-manual-workspace]");
        var shell = workspace && workspace.closest("[data-driver-shell]");
        if (
            !workspace
            || !shell
            || !outbox
            || typeof outbox.getManualTripProjectionReceipt !== "function"
        ) return Promise.resolve(renderProjection(workspace, root.driverOfflineEvents || []));
        return outbox.getManualTripProjectionReceipt(
            shell.dataset.driverShiftId,
            shell.dataset.driverCurrentTruckId
        ).then(function (receipt) {
            return renderProjection(workspace, root.driverOfflineEvents || [], receipt);
        });
    }

    function pointModeForShell(shell) {
        if (currentTripProjection) return "current";
        return shell && shell.dataset
            && shell.dataset.driverActiveTripOrigin === "driver_manual"
            && String(shell.dataset.driverActiveTripId || "")
            ? "current"
            : "unavailable";
    }

    function pointActionCopy(mode) {
        return mode === "current"
            ? {label: "ИЗМЕНИТЬ ТОЧКУ", hint: "Текущий рейс", aria: "Изменить точку разгрузки текущего ручного рейса"}
            : {label: "ИЗМЕНИТЬ ТОЧКУ", hint: "Сначала создайте рейс", aria: "Изменение точки доступно после создания ручного рейса"};
    }

    function updatePointAction(workspace) {
        if (!workspace) return null;
        var shell = workspace.closest("[data-driver-shell]");
        var action = workspace.querySelector("[data-driver-manual-point-open]");
        if (!action) return null;
        var mode = pointModeForShell(shell);
        var copy = pointActionCopy(mode);
        var label = action.querySelector("[data-driver-manual-point-label]");
        var hint = action.querySelector("[data-driver-manual-point-hint]");
        if (label) label.textContent = copy.label;
        if (hint) hint.textContent = copy.hint;
        action.setAttribute("aria-label", copy.aria);
        action.dataset.driverManualPointMode = mode;
        action.disabled = mode !== "current" || savingLocal
            || !currentTripProjection || isTerminalState(currentTripProjection.state);
        action.setAttribute("aria-disabled", action.disabled ? "true" : "false");
        return mode;
    }

    function manualRerouteCandidates(catalog, standardPoints, currentPointId) {
        var excluded = Object.create(null);
        (Array.isArray(standardPoints) ? standardPoints : []).forEach(function (point) {
            var id = positive(point && point.id);
            if (id) excluded[String(id)] = true;
        });
        currentPointId = positive(currentPointId);
        if (currentPointId) excluded[String(currentPointId)] = true;
        var seen = Object.create(null);
        return (Array.isArray(catalog) ? catalog : []).filter(function (point) {
            var id = positive(point && point.id);
            if (!id || excluded[String(id)] || seen[String(id)]) return false;
            seen[String(id)] = true;
            return true;
        }).map(function (point) {
            return {id: positive(point.id), name: String(point.name || "")};
        });
    }

    function dumpNameSizeClass(name) {
        var length = String(name || "").trim().length;
        if (length > 18) return "is-name-long";
        if (length > 10) return "is-name-medium";
        return "is-name-short";
    }

    function applyDumpNameSize(target, name) {
        if (!target || !target.classList) return target;
        target.classList.remove("is-name-short", "is-name-medium", "is-name-long");
        target.classList.add(dumpNameSizeClass(name));
        return target;
    }

    function setManualTargetOneOff(target, isOneOff) {
        isOneOff = isOneOff === true;
        target.classList.toggle("is-driver-manual-one-off", isOneOff);
        if (isOneOff) {
            target.dataset.driverManualOneOff = "true";
        } else {
            delete target.dataset.driverManualOneOff;
        }
        return target;
    }

    function manualContextKey(context) {
        context = context || {};
        var points = Array.isArray(context.dump_points) ? context.dump_points : [];
        return [
            String(context.authority_type || ""),
            String(positive(context.excavator_id) || ""),
            points.map(function (point) { return String(positive(point && point.id) || ""); }).join(",")
        ].join(":");
    }

    function renderedContextKey(workspace) {
        if (!workspace) return "::";
        return [
            String(workspace.dataset.driverManualAuthorityType || ""),
            String(positive(workspace.dataset.driverManualExcavatorId) || ""),
            Array.from(workspace.querySelectorAll("[data-driver-manual-dump-target]"))
                .map(function (target) { return String(positive(target.dataset.eoDumpTarget) || ""); })
                .join(",")
        ].join(":");
    }

    function shouldRebuildWorkspaceContext(workspace, context) {
        var key = manualContextKey(context);
        if (!workspace.__driverManualContextKey) {
            /* The server-rendered cards carry authoritative per-shift counters
               and the last destination.  Treat that DOM as the first rendered
               context instead of cloning it into zeroed client-only cards. */
            workspace.__driverManualContextKey = renderedContextKey(workspace);
        }
        return workspace.__driverManualContextKey !== key;
    }

    function rememberWorkspaceTargets(workspace, key) {
        if (!workspace || !key) return [];
        var targets = Array.from(workspace.querySelectorAll("[data-driver-manual-dump-target]"));
        if (!workspace.__driverManualTargetCache) {
            workspace.__driverManualTargetCache = Object.create(null);
        }
        workspace.__driverManualTargetCache[key] = targets.map(function (target) {
            return target.cloneNode(true);
        });
        return targets;
    }

    function cachedWorkspaceTargets(workspace, key, points) {
        var cached = workspace && workspace.__driverManualTargetCache
            ? workspace.__driverManualTargetCache[key]
            : null;
        if (!Array.isArray(cached)) return null;
        var expectedIds = (Array.isArray(points) ? points : []).map(function (point) {
            return String(positive(point && point.id) || "");
        });
        var cachedIds = cached.map(function (target) {
            return String(positive(target && target.dataset && target.dataset.eoDumpTarget) || "");
        });
        if (expectedIds.join(",") !== cachedIds.join(",")) return null;
        return cached.map(function (target) { return target.cloneNode(true); });
    }

    function createManualDumpTarget(doc, pointId, pointName, prototype, isOneOff) {
        var target = prototype ? prototype.cloneNode(true) : doc.createElement("button");
        if (!prototype) {
            target.type = "button";
            target.className = "eo-unload-card eo-dashboard-unload-card driver-manual-workspace__dump-card status-yellow";
            target.innerHTML = '<span class="eo-dashboard-unload-top"><strong></strong><small aria-label="Рейсов: 0">0</small></span>'
                + '<span class="driver-manual-workspace__swipe-cue driver-manual-workspace__swipe-cue--cancel" aria-hidden="true"><b>▲</b> ОТМЕНИТЬ</span>'
                + '<span class="driver-manual-workspace__swipe-cue driver-manual-workspace__swipe-cue--complete" aria-hidden="true">ЗАВЕРШИТЬ <b>▼</b></span>';
        }
        target.classList.remove("is-last-dump", "is-active-manual-trip", "status-green", "status-red");
        target.classList.add("status-yellow");
        applyDumpNameSize(target, pointName);
        target.removeAttribute("aria-current");
        target.dataset.eoDumpTarget = String(pointId);
        target.dataset.eoDumpName = String(pointName || "");
        target.dataset.eoDumpDistance = "";
        target.dataset.eoHasPendingTrucks = "false";
        target.dataset.driverManualDumpTarget = "";
        setManualTargetOneOff(target, isOneOff !== false);
        target.dataset.driverManualCompletedCount = "0";
        target.dataset.driverManualLastSent = "false";
        target.setAttribute("aria-label", String(pointName || "") + ": рейсов 0");
        var title = target.querySelector(".eo-dashboard-unload-top strong");
        var count = target.querySelector(".eo-dashboard-unload-top small");
        if (title) title.textContent = String(pointName || "");
        if (count) count.textContent = "0";
        return target;
    }

    function standardPointIds(context) {
        return (Array.isArray(context && context.dump_points) ? context.dump_points : [])
            .map(function (point) { return positive(point && point.id); })
            .filter(Boolean);
    }

    function projectionUsesAlternatePoint(context) {
        var pointId = positive(
            currentTripProjection
            && currentTripProjection.payload
            && currentTripProjection.payload.dump_point_id
        );
        return !!pointId && standardPointIds(context).indexOf(pointId) < 0;
    }

    function setGridCount(grid, count) {
        Array.from(grid.classList).forEach(function (name) {
            if (/^is-count-\d+$/.test(name)) grid.classList.remove(name);
        });
        grid.classList.add("is-count-" + String(count));
        grid.classList.toggle("is-single", count === 1);
    }

    function showOnlyCurrentAlternateTarget(workspace, pointId, pointName) {
        var grid = workspace && workspace.querySelector(".eo-dashboard-unload-grid");
        if (!grid || !pointId) return null;
        var context = activeContext();
        var contextKey = manualContextKey(context);
        if (workspace.dataset.driverManualAlternateOnly !== "true") {
            rememberWorkspaceTargets(workspace, contextKey);
        }
        var selector = '[data-driver-manual-dump-target][data-eo-dump-target="' + String(pointId) + '"]';
        var target = grid.querySelector(selector);
        var prototype = target || grid.querySelector("[data-driver-manual-dump-target]");
        if (!target) {
            target = createManualDumpTarget(
                workspace.ownerDocument || root.document,
                pointId,
                pointName,
                prototype,
                true
            );
        }
        grid.querySelectorAll("[data-driver-manual-dump-target]").forEach(function (item) {
            if (item !== target) item.remove();
        });
        if (target.parentNode !== grid) grid.appendChild(target);
        target.dataset.driverManualCurrentOnly = "true";
        setManualTargetOneOff(target, true);
        workspace.dataset.driverManualAlternateOnly = "true";
        setGridCount(grid, 1);
        markLastDump(workspace, pointId);
        if (currentReturnController) currentReturnController.bindAll();
        return target;
    }

    function restoreStandardTargets(workspace) {
        if (!workspace) return activeContext();
        workspace.__driverManualContextKey = "__restore_standard_targets__";
        var context = syncWorkspaceContext(workspace);
        delete workspace.dataset.driverManualAlternateOnly;
        return context;
    }

    function ensureCurrentProjectionTarget(workspace) {
        var grid = workspace && workspace.querySelector(".eo-dashboard-unload-grid");
        if (!grid) return null;
        var context = activeContext();
        var currentPointId = positive(
            currentTripProjection
            && currentTripProjection.payload
            && currentTripProjection.payload.dump_point_id
        );
        if (currentPointId && projectionUsesAlternatePoint(context)) {
            return showOnlyCurrentAlternateTarget(
                workspace,
                currentPointId,
                projectionPointName(currentTripProjection) || "ТЕКУЩАЯ ТОЧКА"
            );
        }
        delete workspace.dataset.driverManualAlternateOnly;
        grid.querySelectorAll('[data-driver-manual-current-only="true"]').forEach(function (target) {
            if (!currentPointId || positive(target.dataset.eoDumpTarget) !== currentPointId) target.remove();
        });
        if (!currentPointId) return null;
        var selector = '[data-driver-manual-dump-target][data-eo-dump-target="' + String(currentPointId) + '"]';
        var target = grid.querySelector(selector);
        if (target) return target;
        var pointName = projectionPointName(currentTripProjection) || "ТЕКУЩАЯ ТОЧКА";
        var prototype = grid.querySelector("[data-driver-manual-dump-target]");
        target = createManualDumpTarget(workspace.ownerDocument || root.document, currentPointId, pointName, prototype, false);
        target.dataset.driverManualCurrentOnly = "true";
        target.setAttribute("aria-label", pointName + ": текущая точка последнего ручного рейса");
        grid.appendChild(target);
        return target;
    }

    function syncWorkspaceContext(workspace) {
        if (!workspace) return activeContext();
        var context = activeContext();
        var points = Array.isArray(context.dump_points) ? context.dump_points : [];
        var showingAlternateOnly = workspace.dataset.driverManualAlternateOnly === "true";
        var needsAlternateOnly = projectionUsesAlternatePoint(context);
        if (!shouldRebuildWorkspaceContext(workspace, context) && showingAlternateOnly === needsAlternateOnly) {
            fitSourceTitle(workspace.querySelector("[data-driver-manual-source]"));
            ensureCurrentProjectionTarget(workspace);
            if (currentReturnController) currentReturnController.bindAll();
            return context;
        }
        var previousKey = workspace.__driverManualContextKey;
        var nextKey = manualContextKey(context);
        if (!showingAlternateOnly) rememberWorkspaceTargets(workspace, previousKey);
        workspace.__driverManualContextKey = nextKey;
        var source = workspace.querySelector("[data-driver-manual-source]");
        if (source) {
            source.dataset.driverManualExcavatorId = String(context.excavator_id || "");
            source.dataset.assignmentId = String(context.assignment_id || "");
            var title = source.querySelector("strong");
            var status = source.querySelector("span");
            var rock = source.querySelector("em");
            if (title) title.textContent = String(context.excavator_label || "ЭКСКАВАТОР");
            if (status) status.textContent = String(context.complex_label || "");
            if (rock) rock.textContent = String(context.rock_type_name || "");
            fitSourceTitle(source);
        }
        var faceSummary = workspace.querySelector("[data-driver-manual-face-summary]");
        if (faceSummary) {
            var faceText = String(context.excavator_label || "—")
                + " · Гор. " + String(context.loading_horizon || "—")
                + "/Бл. " + String(context.loading_block || "—")
                + (context.rock_type_name ? " · " + String(context.rock_type_name) : "");
            faceSummary.textContent = faceText;
            fitFaceSummary(workspace);
        }
        var grid = workspace.querySelector(".eo-dashboard-unload-grid");
        if (grid && points.length && !needsAlternateOnly) {
            var prototype = grid.querySelector("[data-driver-manual-dump-target]");
            var targets = cachedWorkspaceTargets(workspace, nextKey, points);
            if (!targets) {
                targets = points.map(function (point) {
                    var target = createManualDumpTarget(
                        workspace.ownerDocument || root.document,
                        point.id,
                        point.name,
                        prototype,
                        point.one_off === true
                    );
                    target.dataset.eoDumpDistance = String(point.transport_distance_km || "");
                    var completedCount = Math.max(0, Number(point.completed_count) || 0);
                    var isLastSent = point.is_last_sent === true;
                    target.dataset.driverManualCompletedCount = String(completedCount);
                    target.dataset.driverManualLastSent = isLastSent ? "true" : "false";
                    target.classList.toggle("is-last-dump", isLastSent);
                    var count = target.querySelector(".eo-dashboard-unload-top small");
                    if (count) {
                        count.textContent = String(completedCount);
                        count.setAttribute("aria-label", "Рейсов: " + String(completedCount));
                    }
                    target.setAttribute(
                        "aria-label",
                        String(point.name || "") + ": рейсов " + String(completedCount)
                            + (isLastSent ? "; последняя использованная точка" : "")
                    );
                    return target;
                });
            }
            grid.querySelectorAll("[data-driver-manual-dump-target]").forEach(function (target) { target.remove(); });
            targets.forEach(function (target) { grid.appendChild(target); });
            setGridCount(grid, points.length);
            delete workspace.dataset.driverManualAlternateOnly;
        } else if (grid && !points.length && !needsAlternateOnly) {
            grid.querySelectorAll("[data-driver-manual-dump-target]").forEach(function (target) { target.remove(); });
            setGridCount(grid, 0);
        }
        ensureCurrentProjectionTarget(workspace);
        if (currentReturnController) currentReturnController.bindAll();
        return context;
    }

    function buildManualLoadEvent(workspace, target, events) {
        if (typeof root.createDriverManualLoadEvent !== "function") {
            throw new Error("offline_runtime_unavailable");
        }
        var shell = workspace.closest("[data-driver-shell]");
        var context = clone(syncWorkspaceContext(workspace) || {});
        var pointId = positive(target && target.dataset.eoDumpTarget);
        var pointName = String(target && target.dataset.eoDumpName || "");
        var points = Array.isArray(context.dump_points) ? context.dump_points : [];
        var selected = points.find(function (point) { return positive(point && point.id) === pointId; });
        if (!selected) {
            selected = {
                id: pointId,
                name: pointName,
                transport_distance_km: String(target && target.dataset.eoDumpDistance || ""),
                one_off: true
            };
            points.push(selected);
        }
        context.dump_points = points;
        context.selected_dump_point_id = pointId;
        context.selected_dump_point_name = pointName;
        context.selected_one_off = selected.one_off === true || target.dataset.driverManualOneOff === "true";
        var dependency = (Array.isArray(events) ? events : [])
            .filter(function (event) {
                if (!event || ["conflict", "auth_required", "invalid"].indexOf(String(event.state || "")) >= 0) return false;
                return currentTripProjection && (
                    String(event.event_id || "") === String(currentTripProjection.event_id || "")
                    || (
                        currentTripProjection.local_trip_id
                        && String(event.local_trip_id || "") === String(currentTripProjection.local_trip_id)
                    )
                );
            })
            .sort(function (left, right) { return Number(left.sequence || 0) - Number(right.sequence || 0); })
            .pop();
        var dependsOn = dependency
            ? [dependency.event_id]
            : (currentTripProjection && currentTripProjection.can_depend_on_prior && currentTripProjection.event_id
                ? [currentTripProjection.event_id]
                : []);
        var pendingCompletion = latestManualCompletion(events);
        if (
            !currentTripProjection
            && pendingCompletion
            && !isTerminalState(pendingCompletion.state)
            && dependsOn.indexOf(String(pendingCompletion.event_id || "")) < 0
        ) {
            dependsOn.push(String(pendingCompletion.event_id));
        }
        return root.createDriverManualLoadEvent({
            truckId: positive(shell && shell.dataset.driverCurrentTruckId) || positive(context.truck_id),
            excavatorId: positive(context.excavator_id),
            dumpPointId: pointId,
            rockTypeId: positive(context.rock_type_id),
            placementId: positive(context.placement_id),
            placementUpdatedAt: context.placement_updated_at,
            loadingHorizon: context.loading_horizon,
            loadingBlock: context.loading_block,
            transportDistanceKm: selected.transport_distance_km,
            assignmentId: positive(context.assignment_id),
            acceptanceId: positive(context.free_bucket_acceptance_id),
            acceptanceLocalId: context.free_bucket_acceptance_local_id,
            dependsOn: dependsOn,
            contextSnapshot: context
        });
    }

    function buildManualLoadCancelEvent(workspace, target, events) {
        if (!currentTripProjection || typeof root.createDriverManualLoadCancelledEvent !== "function") {
            throw new Error("offline_runtime_unavailable");
        }
        var shell = workspace.closest("[data-driver-shell]");
        var projection = currentTripProjection;
        var context = clone(projection.context_snapshot || {});
        var pointId = positive(target && target.dataset.eoDumpTarget)
            || positive(projection.payload && projection.payload.dump_point_id);
        return root.createDriverManualLoadCancelledEvent({
            tripId: positive(projection.trip_id),
            localTripId: positive(projection.trip_id) ? "" : String(projection.local_trip_id || ""),
            loadEventId: String(projection.event_id || projection.local_trip_id || ""),
            truckId: positive(shell && shell.dataset.driverCurrentTruckId) || positive(context.truck_id),
            excavatorId: positive(context.excavator_id) || positive(workspace.dataset.driverManualExcavatorId),
            dumpPointId: pointId,
            events: events,
            contextSnapshot: {
                source: "driver_manual",
                assigned_dump_point_id: positive(projection.payload && projection.payload.assigned_dump_point_id)
                    || positive(context.assigned_dump_point_id)
                    || pointId,
                selected_dump_point_id: pointId,
                selected_dump_point_name: String(target && target.dataset.eoDumpName || projectionPointName(projection))
            }
        });
    }

    function buildManualCompletedEvent(workspace, target, events) {
        if (!currentTripProjection || typeof root.createDriverManualCompletedEvent !== "function") {
            throw new Error("offline_runtime_unavailable");
        }
        var shell = workspace.closest("[data-driver-shell]");
        var projection = currentTripProjection;
        var context = clone(projection.context_snapshot || {});
        return root.createDriverManualCompletedEvent({
            tripId: positive(projection.trip_id),
            localTripId: positive(projection.trip_id) ? "" : String(projection.local_trip_id || ""),
            loadEventId: String(projection.event_id || projection.local_trip_id || ""),
            truckId: positive(shell && shell.dataset.driverCurrentTruckId) || positive(context.truck_id),
            excavatorId: positive(context.excavator_id) || positive(workspace.dataset.driverManualExcavatorId),
            dumpPointId: positive(target && target.dataset.eoDumpTarget)
                || positive(projection.payload && projection.payload.dump_point_id),
            events: events,
            contextSnapshot: {
                source: "driver_manual",
                action: "manual_completed",
                selected_dump_point_id: positive(projection.payload && projection.payload.dump_point_id),
                selected_dump_point_name: projectionPointName(projection)
            }
        });
    }

    function completeManualLoad(workspace, target) {
        var outbox = root.driverOfflineOutbox;
        var projection = currentTripProjection;
        if (
            savingLocal
            || !projection
            || !outbox
            || !target
            || target.dataset.eoReturnEnabled !== "true"
        ) return Promise.resolve(false);
        savingLocal = true;
        target.classList.add("is-complete-pending");
        setSourceLocked(workspace, true);
        return outbox.pending().then(function (events) {
            return buildManualCompletedEvent(workspace, target, events);
        }).then(function (event) {
            return outbox.enqueue(event);
        }).then(function (saved) {
            savingLocal = false;
            manualCompletionPendingKey = String(saved.event_id || "pending");
            currentTripProjection = null;
            stopTripTimer(workspace);
            markLastDump(workspace, positive(projection.payload && projection.payload.dump_point_id));
            restoreStandardTargets(workspace);
            setSourceLocked(workspace, false);
            setResult(workspace, "complete-pending", null, true);
            updatePointAction(workspace);
            playManualFeedback("completed");
            return saved;
        }).catch(function (error) {
            savingLocal = false;
            currentTripProjection = projection;
            setSourceLocked(workspace, sourceShouldBeLocked(false, projection));
            setResult(workspace, "storage-error", null, true);
            throw error;
        }).finally(function () {
            target.classList.remove("is-complete-pending");
        });
    }

    function cancelManualLoad(workspace, target) {
        var projection = currentTripProjection;
        var outbox = root.driverOfflineOutbox;
        if (
            savingLocal
            || !projection
            || !outbox
            || !target
            || target.dataset.eoReturnEnabled !== "true"
        ) return Promise.resolve(false);
        savingLocal = true;
        target.classList.add("is-return-pending");
        setSourceLocked(workspace, true);
        return outbox.pending().then(function (events) {
            return buildManualLoadCancelEvent(workspace, target, events);
        }).then(function (event) {
            return outbox.enqueue(event);
        }).then(function (saved) {
            savingLocal = false;
            updateManualTripCount(
                workspace,
                positive(projection.payload && projection.payload.assigned_dump_point_id)
                    || positive(projection.context_snapshot && projection.context_snapshot.assigned_dump_point_id)
                    || positive(projection.payload && projection.payload.dump_point_id),
                -1,
                "cancel:" + String(saved.event_id || "")
            );
            currentTripProjection = null;
            stopTripTimer(workspace);
            markLastDump(workspace, null);
            restoreStandardTargets(workspace);
            setSourceLocked(workspace, false);
            setResult(workspace, "cancel-pending", null, true);
            updatePointAction(workspace);
            playManualFeedback("cancelled");
            return saved;
        }).catch(function (error) {
            savingLocal = false;
            currentTripProjection = projection;
            target.classList.remove("is-return-pending");
            setSourceLocked(workspace, sourceShouldBeLocked(false, projection));
            setResult(workspace, "storage-error", null, true);
            throw error;
        }).finally(function () {
            target.classList.remove("is-return-pending");
        });
    }

    function selectManualPoint(workspace, pointId, pointName) {
        if (!workspace || !pointId) return null;
        var grid = workspace.querySelector(".eo-dashboard-unload-grid");
        if (!grid) return null;
        pointId = positive(pointId);
        var context = activeContext();
        var isAlternate = standardPointIds(context).indexOf(pointId) < 0;
        if (isAlternate) {
            var isolated = showOnlyCurrentAlternateTarget(workspace, pointId, pointName);
            var isolatedAction = workspace.querySelector("[data-driver-manual-point-open]");
            if (isolatedAction) {
                isolatedAction.dataset.driverManualSelectedPointId = String(pointId);
                isolatedAction.dataset.driverManualSelectedPointName = String(pointName || "");
            }
            return isolated;
        }
        var selector = '[data-driver-manual-dump-target][data-eo-dump-target="' + String(pointId) + '"]';
        var target = grid.querySelector(selector);
        if (!target) {
            var prototype = grid.querySelector("[data-driver-manual-dump-target]");
            target = createManualDumpTarget(workspace.ownerDocument || root.document, pointId, pointName, prototype);
            grid.appendChild(target);
            Array.from(grid.classList).forEach(function (name) {
                if (/^is-count-\d+$/.test(name)) grid.classList.remove(name);
            });
            grid.classList.add("is-count-" + grid.querySelectorAll("[data-driver-manual-dump-target]").length);
        }
        grid.querySelectorAll("[data-driver-manual-dump-target]").forEach(function (item) {
            item.classList.toggle("is-driver-manual-selected-point", item === target);
            if (item === target) item.setAttribute("aria-current", "true");
            else item.removeAttribute("aria-current");
        });
        var action = workspace.querySelector("[data-driver-manual-point-open]");
        if (action) {
            var hint = action.querySelector("[data-driver-manual-point-hint]");
            if (hint) hint.textContent = String(pointName || "");
            action.dataset.driverManualSelectedPointId = String(pointId);
            action.dataset.driverManualSelectedPointName = String(pointName || "");
        }
        return target;
    }

    function rememberPointSheet(sheet) {
        if (!sheet || sheet.__driverManualPointOriginal) return;
        var head = sheet.querySelector(".driver-unload-head");
        var paragraphs = head ? head.querySelectorAll("p") : [];
        var current = sheet.querySelector(".driver-unload-current");
        sheet.__driverManualPointOriginal = {
            title: head && head.querySelector("h2") ? head.querySelector("h2").textContent : "",
            first: paragraphs[0] ? paragraphs[0].textContent : "",
            second: paragraphs[1] ? paragraphs[1].textContent : "",
            secondHidden: paragraphs[1] ? paragraphs[1].hidden : false,
            currentHidden: current ? current.hidden : false
        };
    }

    function closePointChooser(workspace) {
        workspace = workspace || currentWorkspace || (root.document && root.document.querySelector("[data-driver-manual-workspace]"));
        if (!workspace) return;
        var shell = workspace.closest("[data-driver-shell]");
        var sheet = shell && shell.querySelector("[data-driver-point-sheet]");
        if (!sheet || sheet.dataset.driverManualPointMode !== "current-local") return;
        var original = sheet.__driverManualPointOriginal;
        var head = sheet.querySelector(".driver-unload-head");
        var paragraphs = head ? head.querySelectorAll("p") : [];
        var title = head && head.querySelector("h2");
        var current = sheet.querySelector(".driver-unload-current");
        if (original) {
            if (title) title.textContent = original.title;
            if (paragraphs[0]) paragraphs[0].textContent = original.first;
            if (paragraphs[1]) {
                paragraphs[1].textContent = original.second;
                paragraphs[1].hidden = original.secondHidden;
            }
            if (current) current.hidden = original.currentHidden;
        }
        sheet.querySelectorAll(".driver-unload-tile").forEach(function (button) {
            if (button.__driverManualWasDisabled !== undefined) {
                button.disabled = button.__driverManualWasDisabled;
                delete button.__driverManualWasDisabled;
            }
        });
        sheet.querySelectorAll('[data-driver-manual-reroute-generated]').forEach(function (node) {
            node.remove();
        });
        sheet.querySelectorAll(".driver-unload-tile-form").forEach(function (form) {
            if (form.__driverManualWasHidden !== undefined) {
                form.hidden = form.__driverManualWasHidden;
                delete form.__driverManualWasHidden;
            }
        });
        delete sheet.dataset.driverManualPointMode;
        sheet.hidden = true;
        if (shell) shell.classList.remove("is-point-sheet-open");
        var action = workspace.querySelector("[data-driver-manual-point-open]");
        if (action) {
            action.setAttribute("aria-expanded", "false");
            if (typeof action.focus === "function") action.focus({preventScroll: true});
        }
    }

    function appendManualRerouteTile(sheet, point) {
        var grid = sheet.querySelector(".driver-unload-grid");
        if (!grid) return null;
        var doc = sheet.ownerDocument || root.document;
        var prototype = grid.querySelector(".driver-unload-tile-form");
        var form = prototype ? prototype.cloneNode(true) : doc.createElement("form");
        form.removeAttribute("action");
        form.method = "post";
        form.hidden = false;
        form.dataset.driverManualRerouteGenerated = "true";
        form.classList.add("driver-unload-tile-form");
        var pointInput = form.querySelector('[name="dump_point"]');
        if (!pointInput) {
            pointInput = doc.createElement("input");
            pointInput.type = "hidden";
            pointInput.name = "dump_point";
            form.appendChild(pointInput);
        }
        pointInput.value = String(point.id);
        var button = form.querySelector(".driver-unload-tile");
        if (!button) {
            button = doc.createElement("button");
            button.type = "submit";
            button.className = "driver-unload-tile";
            button.innerHTML = '<strong></strong><span class="driver-unload-tile-status" data-driver-point-tile-status></span>';
            form.appendChild(button);
        }
        button.disabled = false;
        button.classList.remove("is-current");
        button.removeAttribute("aria-current");
        button.dataset.driverPointName = String(point.name || "");
        var title = button.querySelector("strong");
        if (title) {
            title.textContent = String(point.name || "");
            title.classList.toggle("is-long", String(point.name || "").length > 8);
            title.classList.toggle("is-extra-long", String(point.name || "").length > 15);
        }
        var status = button.querySelector("[data-driver-point-tile-status]");
        if (status) status.textContent = "";
        grid.appendChild(form);
        return form;
    }

    function populateManualRerouteSheet(sheet) {
        var base = readWorkspaceContext();
        var catalog = Array.isArray(base.reroute_points) ? base.reroute_points : [];
        var currentPointId = positive(
            currentTripProjection && currentTripProjection.payload && currentTripProjection.payload.dump_point_id
        );
        var candidates = manualRerouteCandidates(catalog, activeContext().dump_points, currentPointId);
        sheet.querySelectorAll('[data-driver-manual-reroute-generated]').forEach(function (node) { node.remove(); });
        sheet.querySelectorAll(".driver-unload-tile-form").forEach(function (form) {
            form.__driverManualWasHidden = form.hidden;
            form.hidden = true;
        });
        candidates.forEach(function (point) { appendManualRerouteTile(sheet, point); });
        if (!candidates.length) {
            var empty = (sheet.ownerDocument || root.document).createElement("p");
            empty.className = "driver-unload-empty";
            empty.dataset.driverManualRerouteGenerated = "true";
            empty.textContent = "Других активных точек разгрузки нет.";
            var grid = sheet.querySelector(".driver-unload-grid");
            if (grid) grid.appendChild(empty);
        }
        return candidates;
    }

    function openPointChooser(workspace) {
        if (!workspace) return false;
        if (currentController) currentController.cancel();
        var shell = workspace.closest("[data-driver-shell]");
        if (!shell) return false;
        if (pointModeForShell(shell) !== "current" || !currentTripProjection) return false;
        var sheet = shell.querySelector("[data-driver-point-sheet]");
        if (!sheet) return false;
        rememberPointSheet(sheet);
        var head = sheet.querySelector(".driver-unload-head");
        var paragraphs = head ? head.querySelectorAll("p") : [];
        var title = head && head.querySelector("h2");
        var current = sheet.querySelector(".driver-unload-current");
        if (title) title.textContent = "Изменить точку текущего рейса";
        if (paragraphs[0]) paragraphs[0].textContent = "Выберите другую активную точку из справочника.";
        if (paragraphs[1]) paragraphs[1].hidden = true;
        if (current) current.hidden = true;
        populateManualRerouteSheet(sheet);
        sheet.dataset.driverManualPointMode = "current-local";
        sheet.hidden = false;
        shell.classList.add("is-point-sheet-open");
        var action = workspace.querySelector("[data-driver-manual-point-open]");
        if (action) action.setAttribute("aria-expanded", "true");
        root.requestAnimationFrame(function () {
            var focusTarget = sheet.querySelector(".driver-unload-tile, [data-driver-point-close]");
            if (focusTarget) focusTarget.focus();
        });
        return true;
    }

    function closeWorkspace(workspace, options) {
        options = options || {};
        if (!options.preserveRequest) {
            workspaceRequestedOpen = false;
            workspacePreferenceKnown = true;
        }
        workspace = workspace || currentWorkspace || root.document.querySelector("[data-driver-manual-workspace]");
        if (!workspace) return;
        if (currentController) currentController.cancel();
        if (currentReturnController) currentReturnController.cancel();
        closePointChooser(workspace);
        var result = workspace.querySelector("[data-driver-manual-result]");
        if (result) {
            root.clearTimeout(result.__driverManualHideTimer);
            result.hidden = true;
        }
        workspace.hidden = true;
        var shell = workspace.closest("[data-driver-shell]");
        if (shell) shell.classList.remove("is-driver-manual-workspace-open");
        root.document.body.classList.remove("excavator-operator-screen");
        root.document.querySelectorAll("[data-driver-manual-open]").forEach(function (control) {
            control.setAttribute("aria-expanded", "false");
        });
    }

    function bindWorkspace(workspace) {
        if (!workspace || !root.ExcavatorDashboardDrag) return null;
        if (currentWorkspace === workspace && currentController) {
            currentController.bindAll();
            if (currentReturnController) currentReturnController.bindAll();
            syncWorkspaceContext(workspace);
            restoreProjection(root.driverOfflineOutbox, workspace).catch(function () {
                renderProjection(workspace, root.driverOfflineEvents || []);
            });
            return currentController;
        }
        if (currentController) currentController.destroy();
        if (currentReturnController) currentReturnController.destroy();
        currentReturnController = null;
        currentWorkspace = workspace;
        var excavatorShell = workspace.querySelector("[data-driver-manual-eo-shell]");
        if (!excavatorShell) return null;
        var result = workspace.querySelector("[data-driver-manual-result]");
        currentController = root.ExcavatorDashboardDrag.attach({
            shell: excavatorShell,
            sourceSelector: "[data-driver-manual-source]",
            targetSelector: '[data-driver-manual-dump-target]:not([data-driver-manual-current-only="true"])',
            gradientId: "driver-manual-drag-comet-light",
            canDrag: function () {
                return !sourceShouldBeLocked(savingLocal, currentTripProjection);
            },
            isManual: function () { return false; },
            isInactive: function () { return false; },
            isBlocked: function () { return false; },
            onDrop: function (card, target) {
                if (sourceShouldBeLocked(savingLocal, currentTripProjection)) return;
                var outbox = root.driverOfflineOutbox;
                if (!outbox) {
                    setResult(workspace, "storage-error", null, true);
                    return;
                }
                var previousProjection = currentTripProjection;
                savingLocal = true;
                setSourceLocked(workspace, true);
                setResult(workspace, "saving", null, true);
                outbox.pending().then(function (events) {
                    return buildManualLoadEvent(workspace, target, events);
                }).then(function (event) {
                    return outbox.enqueue(event);
                }).then(function (saved) {
                    savingLocal = false;
                    manualCompletionPendingKey = "";
                    delete workspace.dataset.driverManualLastError;
                    currentTripProjection = saved;
                    updateManualTripCount(
                        workspace,
                        target.dataset.eoDumpTarget,
                        1,
                        "load:" + String(saved.event_id || "")
                    );
                    startTripTimer(workspace, target.dataset.eoDumpName, Date.parse(saved.occurred_at));
                    markLastDump(workspace, target.dataset.eoDumpTarget);
                    syncWorkspaceContext(workspace);
                    setSourceLocked(workspace, sourceShouldBeLocked(false, saved));
                    setResult(workspace, "pending", null, true);
                    updatePointAction(workspace);
                    playManualFeedback("created");
                }).catch(function (error) {
                    savingLocal = false;
                    workspace.dataset.driverManualLastError = String(error && error.message || "manual_load_failed");
                    currentTripProjection = previousProjection;
                    if (!previousProjection) stopTripTimer(workspace);
                    setSourceLocked(workspace, sourceShouldBeLocked(false, previousProjection));
                    setResult(workspace, "storage-error", null, true);
                });
            },
            haptic: function (pattern, amplitude) {
                if (typeof root.driverHaptic === "function") {
                    root.driverHaptic(pattern, amplitude);
                } else if (root.navigator && typeof root.navigator.vibrate === "function") {
                    try { root.navigator.vibrate(pattern); } catch (error) {}
                }
            },
            onTargetChange: function () {
                playGestureHaptic("target");
            }
        });
        if (root.ExcavatorDumpReturnSwipe) {
            currentReturnController = root.ExcavatorDumpReturnSwipe.attach({
                shell: excavatorShell,
                targetSelector: "[data-driver-manual-dump-target]",
                canStart: function (target) {
                    return !savingLocal
                        && !!currentTripProjection
                        && !isTerminalState(currentTripProjection.state)
                        && target.dataset.eoReturnEnabled === "true";
                },
                onReturn: function (target) {
                    cancelManualLoad(workspace, target).catch(function () {});
                },
                onComplete: function (target) {
                    completeManualLoad(workspace, target).catch(function () {});
                },
                onArm: function (target, direction) {
                    playGestureHaptic(direction === "complete" ? "completeArmed" : "returnArmed");
                }
            });
        }
        syncWorkspaceContext(workspace);
        restoreProjection(root.driverOfflineOutbox, workspace).catch(function () {
            renderProjection(workspace, root.driverOfflineEvents || []);
        });
        workspace.__driverManualClose = function () { closeWorkspace(workspace); };
        return currentController;
    }

    function openWorkspace(control) {
        var workspace = root.document.querySelector("[data-driver-manual-workspace]");
        if (!workspace) return;
        bindWorkspace(workspace);
        workspaceRequestedOpen = true;
        workspacePreferenceKnown = true;
        workspace.hidden = false;
        var shell = workspace.closest("[data-driver-shell]");
        if (shell && String(shell.dataset.activeTab || "work") !== "work") return;
        if (shell) shell.classList.add("is-driver-manual-workspace-open");
        root.document.body.classList.add("excavator-operator-screen");
        if (control) control.setAttribute("aria-expanded", "true");
        updatePointAction(workspace);
        renderTripTimer(workspace);
        setManualExitAvailability(workspace);
        /* До этой строки коробка строки «экскаватор/забой» скрыта
           (workspace.hidden было true) и имеет нулевую ширину — подгонка
           кегля, вызванная раньше из syncWorkspaceContext, ничего не
           считает. Пересчитываем заново теперь, когда ширина уже настоящая;
           кадром позже — чтобы браузер успел применить только что снятое
           hidden. */
        if (root.requestAnimationFrame) {
            root.requestAnimationFrame(function () { fitFaceSummary(workspace); });
        } else {
            fitFaceSummary(workspace);
        }
        var source = workspace.querySelector("[data-driver-manual-source]");
        var back = workspace.querySelector("[data-driver-manual-close]");
        if (source || back) (source || back).focus({preventScroll: true});
    }

    function onTabChange(tab) {
        var workspace = currentWorkspace || (root.document && root.document.querySelector("[data-driver-manual-workspace]"));
        if (!workspace) return;
        if (String(tab || "work") === "work") {
            if (workspaceRequestedOpen) openWorkspace(null);
            return;
        }
        closeWorkspace(workspace, {preserveRequest: true});
    }

    function openFreeBucket(workspace) {
        var shell = workspace && workspace.closest("[data-driver-shell]");
        var canonicalTrigger = shell && shell.querySelector('[data-mobile-dial-action="free-bucket"]');
        if (!canonicalTrigger || canonicalTrigger.disabled) return false;
        canonicalTrigger.click();
        return true;
    }

    function enqueueLocalPointChange(workspace, pointId, pointName) {
        var outbox = root.driverOfflineOutbox;
        var projection = currentTripProjection;
        if (!outbox || !projection || typeof root.createDriverPointChangeEvent !== "function") {
            return Promise.reject(new Error("offline_runtime_unavailable"));
        }
        return outbox.pending().then(function (events) {
            var change = root.createDriverPointChangeEvent({
                tripId: positive(projection.trip_id),
                localTripId: positive(projection.trip_id) ? "" : projection.local_trip_id,
                loadEventId: projection.event_id || projection.local_trip_id,
                pointId: pointId,
                pointName: pointName,
                currentPointId: projection.payload && projection.payload.dump_point_id,
                events: events
            });
            return outbox.enqueue(change).then(function (saved) {
                var originalPointId = positive(projection.payload && projection.payload.assigned_dump_point_id)
                    || positive(projection.context_snapshot && projection.context_snapshot.assigned_dump_point_id)
                    || positive(projection.payload && projection.payload.dump_point_id);
                projection.payload = Object.assign({}, projection.payload || {}, {dump_point_id: positive(pointId)});
                projection.context_snapshot = Object.assign({}, projection.context_snapshot || {}, {
                    assigned_dump_point_id: originalPointId,
                    selected_dump_point_id: positive(pointId),
                    selected_dump_point_name: String(pointName || "")
                });
                if (currentTripTimer) currentTripTimer.pointName = String(pointName || "");
                markLastDump(workspace, pointId);
                renderTripTimer(workspace);
                setResult(workspace, "pending", null, true);
                return saved;
            });
        });
    }

    function bindAll() {
        var workspace = root.document.querySelector("[data-driver-manual-workspace]");
        if (!workspace) return;
        bindWorkspace(workspace);
        var shell = workspace.closest("[data-driver-shell]");
        if (!workspacePreferenceKnown) {
            workspaceRequestedOpen = !!(
                shell && shell.dataset.driverActiveTripOrigin === "driver_manual"
            );
            workspacePreferenceKnown = true;
        }
        if (
            workspaceRequestedOpen
            && shell
            && String(shell.dataset.activeTab || "work") === "work"
            && shell.dataset.driverActiveTripOrigin !== "excavator"
        ) {
            workspace.hidden = false;
            shell.classList.add("is-driver-manual-workspace-open");
            root.document.body.classList.add("excavator-operator-screen");
            updatePointAction(workspace);
        } else if (shell && shell.dataset.driverActiveTripOrigin === "excavator") {
            closeWorkspace(workspace, {preserveRequest: false});
        }
    }

    if (root.document && !root.__driverManualExcavatorWorkspaceDelegated) {
        root.__driverManualExcavatorWorkspaceDelegated = true;
        root.document.addEventListener("click", function (event) {
            var dismissRejected = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-dismiss-rejected]")
                : null;
            if (dismissRejected) {
                event.preventDefault();
                event.stopPropagation();
                playGestureHaptic("tap");
                dismissRejectedTripProjection(dismissRejected.closest("[data-driver-manual-workspace]"));
                return;
            }
            var freeBucket = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-free-bucket-open]")
                : null;
            if (freeBucket) {
                event.preventDefault();
                event.stopPropagation();
                playGestureHaptic("tap");
                openFreeBucket(freeBucket.closest("[data-driver-manual-workspace]"));
                return;
            }
            var pointOpen = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-point-open]")
                : null;
            if (pointOpen) {
                event.preventDefault();
                event.stopPropagation();
                playGestureHaptic("tap");
                openPointChooser(pointOpen.closest("[data-driver-manual-workspace]"));
                return;
            }
            var pointSheet = event.target && event.target.closest
                ? event.target.closest("[data-driver-point-sheet]")
                : null;
            if (pointSheet && pointSheet.dataset.driverManualPointMode === "current-local") {
                var pointButton = event.target.closest(".driver-unload-tile");
                if (pointButton) {
                    event.preventDefault();
                    event.stopPropagation();
                    var pointForm = pointButton.closest("form");
                    var pointInput = pointForm && pointForm.querySelector('[name="dump_point"]');
                    var pointWorkspace = root.document.querySelector("[data-driver-manual-workspace]");
                    var chosenId = pointInput && pointInput.value;
                    var chosenName = pointButton.dataset.driverPointName;
                    pointButton.disabled = true;
                    enqueueLocalPointChange(pointWorkspace, chosenId, chosenName).then(function () {
                        selectManualPoint(pointWorkspace, chosenId, chosenName);
                        closePointChooser(pointWorkspace);
                    }).catch(function () {
                        pointButton.disabled = false;
                        setResult(pointWorkspace, "storage-error", null, true);
                    });
                    return;
                }
                if (event.target.closest("[data-driver-point-close]") || event.target === pointSheet) {
                    event.preventDefault();
                    closePointChooser(root.document.querySelector("[data-driver-manual-workspace]"));
                    return;
                }
            }
            var close = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-close]")
                : null;
            if (close) {
                event.preventDefault();
                var closeRoot = close.closest("[data-driver-manual-workspace]");
                if (!savingLocal && !currentTripProjection && !close.disabled) {
                    playGestureHaptic("tap");
                    closeWorkspace(closeRoot);
                }
                return;
            }
            var tab = event.target && event.target.closest
                ? event.target.closest("[data-driver-tab-open]")
                : null;
            if (tab) {
                onTabChange(tab.dataset.driverTabOpen);
                return;
            }
            var open = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-open]")
                : null;
            if (!open || open.disabled) return;
            event.preventDefault();
            openWorkspace(open);
        });
        if (typeof root.addEventListener === "function") {
            root.addEventListener("operational-state-refresh-applied", bindAll);
            root.addEventListener("driver-free-bucket-state-changed", function () {
                if (currentWorkspace && !currentTripProjection) syncWorkspaceContext(currentWorkspace);
            });
            root.addEventListener("pageshow", function () {
                if (currentWorkspace) {
                    renderTripTimer(currentWorkspace);
                    restoreProjection(root.driverOfflineOutbox, currentWorkspace).catch(function () {});
                }
            });
            root.addEventListener("blur", function () {
                if (currentController) currentController.cancel();
                if (currentReturnController) currentReturnController.cancel();
            });
            root.addEventListener("resize", function () {
                fitSourceTitle(currentWorkspace && currentWorkspace.querySelector("[data-driver-manual-source]"));
                fitFaceSummary(currentWorkspace);
            });
            if (root.document && root.document.addEventListener) {
                root.document.addEventListener("visibilitychange", function () {
                    if (!root.document.hidden && currentWorkspace) renderTripTimer(currentWorkspace);
                });
            }
        }
        root.document.addEventListener("keydown", function (event) {
            if (event.key !== "Escape") return;
            var sheet = root.document.querySelector('[data-driver-point-sheet][data-driver-manual-point-mode="current-local"]');
            if (!sheet || sheet.hidden) return;
            event.preventDefault();
            closePointChooser(root.document.querySelector("[data-driver-manual-workspace]"));
        });
    }

    root.bindDriverManualExcavatorWorkspace = bindAll;
    root.DriverManualExcavatorWorkspace = {
        bindAll: bindAll,
        readWorkspaceContext: readWorkspaceContext,
        readWorkspaceTripContext: readWorkspaceTripContext,
        open: openWorkspace,
        close: closeWorkspace,
        onTabChange: onTabChange,
        buildManualCompletedEvent: buildManualCompletedEvent,
        completeManualLoad: completeManualLoad,
        openFreeBucket: openFreeBucket,
        openPointChooser: openPointChooser,
        closePointChooser: closePointChooser,
        selectManualPoint: selectManualPoint,
        createManualDumpTarget: createManualDumpTarget,
        ensureCurrentProjectionTarget: ensureCurrentProjectionTarget,
        setManualTargetOneOff: setManualTargetOneOff,
        manualContextKey: manualContextKey,
        renderedContextKey: renderedContextKey,
        shouldRebuildWorkspaceContext: shouldRebuildWorkspaceContext,
        rememberWorkspaceTargets: rememberWorkspaceTargets,
        cachedWorkspaceTargets: cachedWorkspaceTargets,
        serverTripProjectionContext: serverTripProjectionContext,
        requestAutomaticTripRefresh: requestAutomaticTripRefresh,
        requestManualCancellationRefresh: requestManualCancellationRefresh,
        sourceShouldBeLocked: sourceShouldBeLocked,
        playManualFeedback: playManualFeedback,
        playGestureHaptic: playGestureHaptic,
        dumpNameSizeClass: dumpNameSizeClass,
        formatElapsedTime: formatElapsedTime,
        tripTimerLabel: tripTimerLabel,
        renderTripTimer: renderTripTimer,
        startTripTimer: startTripTimer,
        stopTripTimer: stopTripTimer,
        markLastDump: markLastDump,
        manualCancelWins: manualCancelWins,
        fitSourceTitle: fitSourceTitle,
        fitFaceSummary: fitFaceSummary,
        updateManualTripCount: updateManualTripCount,
        buildManualLoadCancelEvent: buildManualLoadCancelEvent,
        cancelManualLoad: cancelManualLoad,
        renderProjection: renderProjection,
        restoreProjection: restoreProjection,
        buildManualLoadEvent: buildManualLoadEvent,
        pointModeForShell: pointModeForShell,
        pointActionCopy: pointActionCopy,
        manualRerouteCandidates: manualRerouteCandidates,
        standardPointIds: standardPointIds,
        showOnlyCurrentAlternateTarget: showOnlyCurrentAlternateTarget,
        restoreStandardTargets: restoreStandardTargets,
        resultText: resultText,
        dismissRejectedTripProjection: dismissRejectedTripProjection
    };
    if (typeof root.document !== "undefined") {
        if (root.document.readyState === "loading") {
            root.document.addEventListener("DOMContentLoaded", bindAll, {once: true});
        } else {
            bindAll();
        }
    }
    if (typeof module !== "undefined" && module.exports) module.exports = root.DriverManualExcavatorWorkspace;
})(typeof window !== "undefined" ? window : globalThis);
