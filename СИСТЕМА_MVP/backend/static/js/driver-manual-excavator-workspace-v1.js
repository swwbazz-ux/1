(function (root) {
    "use strict";

    var currentWorkspace = null;
    var currentController = null;
    var currentTripTimer = null;
    var tripTimerInterval = null;
    var currentTripProjection = null;
    var savingLocal = false;
    var workspaceRequestedOpen = false;
    var workspacePreferenceKnown = false;
    var automaticTripRefreshKey = "";

    function clone(value) {
        return JSON.parse(JSON.stringify(value));
    }

    function positive(value) {
        value = Number(value);
        return Number.isInteger(value) && value > 0 ? value : null;
    }

    function readWorkspaceContext() {
        var node = root.document && root.document.getElementById("driver-manual-workspace-context-data");
        if (node) {
            try {
                var parsed = JSON.parse(node.textContent || "{}");
                if (parsed && Object.keys(parsed).length) return parsed;
            } catch (error) {}
        }
        var workspace = currentWorkspace || (
            root.document && root.document.querySelector("[data-driver-manual-workspace]")
        );
        if (!workspace) return {};
        var source = workspace.querySelector("[data-driver-manual-source]");
        return {
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
            }).filter(function (point) { return !!point.id; })
        };
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
        if (state === "review") return String(detail || "Не принято · нужна сверка");
        if (state === "storage-error") return "Не сохранено · повторите отправку";
        return "";
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
        return "В ПУТИ · " + String(pointName || "ТОЧКА НЕ УКАЗАНА").trim();
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
        if (state) state.textContent = "В ПУТИ";
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

    function markLastDump(workspace, pointId) {
        if (!workspace) return null;
        var selectedId = String(pointId || "");
        var selected = null;
        workspace.querySelectorAll("[data-driver-manual-dump-target]").forEach(function (target) {
            var isLast = !!selectedId && String(target.dataset.eoDumpTarget || "") === selectedId;
            target.classList.toggle("is-last-dump", isLast);
            target.dataset.driverManualLastSent = isLast ? "true" : "false";
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

    function manualLoadFromEvents(events) {
        return (Array.isArray(events) ? events : [])
            .filter(function (event) { return event && event.event_type === "driver.trip.loaded"; })
            .slice()
            .sort(function (left, right) { return Number(left.sequence || 0) - Number(right.sequence || 0); })
            .pop() || null;
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

    function setSourceLocked(workspace, locked) {
        var source = workspace && workspace.querySelector("[data-driver-manual-source]");
        if (!source) return;
        source.disabled = !!locked;
        source.setAttribute("aria-disabled", locked ? "true" : "false");
        source.classList.toggle("is-load-blocked", !!locked);
        source.dataset.eoCanLoad = locked ? "0" : "1";
        source.draggable = !locked;
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
        var pointName = String(
            shell && shell.dataset && shell.dataset.driverActualDumpPointName
            || context.selected_dump_point_name
            || ""
        );
        if (pointId) context.selected_dump_point_id = pointId;
        if (pointName) context.selected_dump_point_name = pointName;
        return {
            context_snapshot: context,
            payload: {dump_point_id: pointId}
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

    function renderProjection(workspace, events, receipt) {
        workspace = workspace || currentWorkspace || root.document.querySelector("[data-driver-manual-workspace]");
        if (!workspace) return null;
        var shell = workspace.closest("[data-driver-shell]");
        var serverOrigin = shell && String(shell.dataset.driverActiveTripOrigin || "");
        var serverTripId = shell && positive(shell.dataset.driverActiveTripId);
        var serverLoadedAt = shell && String(shell.dataset.driverActiveTripLoadedAt || "");
        var projected = manualLoadFromEvents(events);
        var unload = (Array.isArray(events) ? events : []).find(function (event) {
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
            var serverContext = readWorkspaceContext();
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
            startTripTimer(workspace, projectionPointName(currentTripProjection), Date.parse(currentTripProjection.occurred_at));
            markLastDump(workspace, currentTripProjection.payload.dump_point_id);
            setSourceLocked(workspace, false);
            setResult(workspace, "confirmed", serverTripId, false);
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
        if (!projected && receipt && receipt.trip_origin === "driver_manual") {
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
            setSourceLocked(workspace, savingLocal);
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
            projected.payload = Object.assign({}, projected.payload || {}, {
                dump_point_id: positive(latestPoint.payload && latestPoint.payload.dump_point_id)
            });
            projected.context_snapshot = Object.assign({}, projected.context_snapshot || {}, {
                selected_dump_point_id: positive(latestPoint.payload && latestPoint.payload.dump_point_id),
                selected_dump_point_name: String(
                    latestPoint.context_snapshot
                    && latestPoint.context_snapshot.selected_dump_point_name
                    || ""
                )
            });
        }
        var pointName = projectionPointName(projected);
        projected.can_depend_on_prior = projected.can_depend_on_prior !== false;
        if (isTerminalState(projected.state)) {
            stopTripTimer(workspace);
            setSourceLocked(workspace, true);
            setResult(workspace, "review", projected.last_error && projected.last_error.message, true);
            return projected;
        }
        markLastDump(workspace, projected.payload && projected.payload.dump_point_id);
        startTripTimer(workspace, pointName, Date.parse(projected.occurred_at));
        setSourceLocked(workspace, false);
        setResult(
            workspace,
            projected.state === "confirmed" ? "confirmed" : "pending",
            projected.trip_id || (receipt && receipt.server_ids && receipt.server_ids.trip_id),
            projected.state !== "confirmed"
        );
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
        return shell && shell.dataset && shell.dataset.driverHasOpenTrip === "true" &&
            String(shell.dataset.driverActiveTripId || "") ? "current" : "next";
    }

    function pointActionCopy(mode) {
        return mode === "current"
            ? {label: "ТОЧКА РАЗГРУЗКИ", hint: "Изменить текущую", aria: "Изменить точку разгрузки текущего рейса"}
            : {label: "ТОЧКА РАЗГРУЗКИ", hint: "Для следующего рейса", aria: "Выбрать точку разгрузки для следующего рейса"};
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
        return mode;
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

    function createManualDumpTarget(doc, pointId, pointName, prototype, isOneOff) {
        var target = prototype ? prototype.cloneNode(true) : doc.createElement("button");
        if (!prototype) {
            target.type = "button";
            target.className = "eo-unload-card eo-dashboard-unload-card driver-manual-workspace__dump-card status-yellow";
            target.innerHTML = '<span class="eo-dashboard-unload-top"><strong></strong><small aria-label="Рейсов: 0">0</small></span>';
        }
        target.classList.remove("is-last-dump", "status-green", "status-red");
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

    function syncWorkspaceContext(workspace) {
        if (!workspace || currentTripProjection) return activeContext();
        var context = activeContext();
        var points = Array.isArray(context.dump_points) ? context.dump_points : [];
        var key = [
            context.authority_type,
            context.excavator_id,
            points.map(function (point) { return point.id; }).join(",")
        ].join(":");
        if (workspace.__driverManualContextKey === key) return context;
        workspace.__driverManualContextKey = key;
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
        }
        var topTitle = workspace.querySelector(".driver-manual-workspace__back strong");
        if (topTitle) topTitle.textContent = String(context.excavator_label || "Ручной режим");
        var coordinates = workspace.querySelectorAll(".eo-face-coordinates span");
        if (coordinates[0]) coordinates[0].textContent = "Гор. " + String(context.loading_horizon || "—");
        if (coordinates[1]) coordinates[1].textContent = "Бл. " + String(context.loading_block || "—");
        var topRock = workspace.querySelector(".eo-face-rock");
        if (topRock) topRock.textContent = String(context.rock_type_name || "");
        var grid = workspace.querySelector(".eo-dashboard-unload-grid");
        if (grid && points.length) {
            var prototype = grid.querySelector("[data-driver-manual-dump-target]");
            grid.querySelectorAll("[data-driver-manual-dump-target]").forEach(function (target) { target.remove(); });
            points.forEach(function (point) {
                var target = createManualDumpTarget(
                    workspace.ownerDocument || root.document,
                    point.id,
                    point.name,
                    prototype,
                    point.one_off === true
                );
                target.dataset.eoDumpDistance = String(point.transport_distance_km || "");
                grid.appendChild(target);
            });
            Array.from(grid.classList).forEach(function (name) {
                if (/^is-count-\d+$/.test(name)) grid.classList.remove(name);
            });
            grid.classList.add("is-count-" + points.length);
        }
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

    function selectManualPoint(workspace, pointId, pointName) {
        if (!workspace || !pointId) return null;
        var grid = workspace.querySelector(".eo-dashboard-unload-grid");
        if (!grid) return null;
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
        if (!sheet || ["next", "current-local"].indexOf(sheet.dataset.driverManualPointMode) < 0) return;
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
        delete sheet.dataset.driverManualPointMode;
        sheet.hidden = true;
        if (shell) shell.classList.remove("is-point-sheet-open");
        var action = workspace.querySelector("[data-driver-manual-point-open]");
        if (action) {
            action.setAttribute("aria-expanded", "false");
            if (typeof action.focus === "function") action.focus({preventScroll: true});
        }
    }

    function openPointChooser(workspace) {
        if (!workspace) return false;
        if (currentController) currentController.cancel();
        var shell = workspace.closest("[data-driver-shell]");
        if (!shell) return false;
        if (pointModeForShell(shell) === "current") {
            var serverTripId = positive(shell.dataset.driverActiveTripId)
                || positive(currentTripProjection && currentTripProjection.trip_id);
            if (serverTripId) {
                var canonical = Array.from(shell.querySelectorAll("[data-driver-point-open]")).find(function (control) {
                    return !control.hasAttribute("data-driver-manual-point-open");
                });
                if (canonical && !canonical.disabled) {
                    canonical.click();
                    return true;
                }
            }
        }
        var sheet = shell.querySelector("[data-driver-point-sheet]");
        if (!sheet) return false;
        rememberPointSheet(sheet);
        var head = sheet.querySelector(".driver-unload-head");
        var paragraphs = head ? head.querySelectorAll("p") : [];
        var title = head && head.querySelector("h2");
        var current = sheet.querySelector(".driver-unload-current");
        var currentMode = pointModeForShell(shell) === "current";
        if (title) title.textContent = currentMode ? "Точка текущего рейса" : "Другая точка разгрузки";
        if (paragraphs[0]) paragraphs[0].textContent = currentMode
            ? "Выберите новую точку для этого же рейса."
            : "Выберите разовую точку для следующего ручного рейса.";
        if (paragraphs[1]) paragraphs[1].hidden = true;
        if (current) current.hidden = true;
        sheet.querySelectorAll(".driver-unload-tile").forEach(function (button) {
            button.__driverManualWasDisabled = button.disabled;
            button.disabled = false;
        });
        sheet.dataset.driverManualPointMode = currentMode ? "current-local" : "next";
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
            syncWorkspaceContext(workspace);
            restoreProjection(root.driverOfflineOutbox, workspace).catch(function () {
                renderProjection(workspace, root.driverOfflineEvents || []);
            });
            return currentController;
        }
        if (currentController) currentController.destroy();
        currentWorkspace = workspace;
        var excavatorShell = workspace.querySelector("[data-driver-manual-eo-shell]");
        if (!excavatorShell) return null;
        var result = workspace.querySelector("[data-driver-manual-result]");
        currentController = root.ExcavatorDashboardDrag.attach({
            shell: excavatorShell,
            sourceSelector: "[data-driver-manual-source]",
            targetSelector: "[data-driver-manual-dump-target]",
            gradientId: "driver-manual-drag-comet-light",
            canDrag: function () {
                return !savingLocal && !(
                    currentTripProjection && isTerminalState(currentTripProjection.state)
                );
            },
            isManual: function () { return false; },
            isInactive: function () { return false; },
            isBlocked: function () { return false; },
            onDrop: function (card, target) {
                if (savingLocal || (currentTripProjection && isTerminalState(currentTripProjection.state))) return;
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
                    delete workspace.dataset.driverManualLastError;
                    currentTripProjection = saved;
                    startTripTimer(workspace, target.dataset.eoDumpName, Date.parse(saved.occurred_at));
                    markLastDump(workspace, target.dataset.eoDumpTarget);
                    syncWorkspaceContext(workspace);
                    setSourceLocked(workspace, false);
                    setResult(workspace, "pending", null, true);
                    updatePointAction(workspace);
                }).catch(function (error) {
                    savingLocal = false;
                    workspace.dataset.driverManualLastError = String(error && error.message || "manual_load_failed");
                    currentTripProjection = previousProjection;
                    if (!previousProjection) stopTripTimer(workspace);
                    setSourceLocked(workspace, !!(
                        previousProjection && isTerminalState(previousProjection.state)
                    ));
                    setResult(workspace, "storage-error", null, true);
                });
            },
            haptic: function (pattern, amplitude) {
                if (typeof root.driverHaptic === "function") {
                    root.driverHaptic(pattern, amplitude);
                } else if (root.navigator && typeof root.navigator.vibrate === "function") {
                    try { root.navigator.vibrate(pattern); } catch (error) {}
                }
            }
        });
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
        if (shell) shell.classList.add("is-driver-manual-workspace-open");
        root.document.body.classList.add("excavator-operator-screen");
        if (control) control.setAttribute("aria-expanded", "true");
        updatePointAction(workspace);
        renderTripTimer(workspace);
        var source = workspace.querySelector("[data-driver-manual-source]");
        var back = workspace.querySelector("[data-driver-manual-close]");
        if (source || back) (source || back).focus({preventScroll: true});
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
                projection.payload = Object.assign({}, projection.payload || {}, {dump_point_id: positive(pointId)});
                projection.context_snapshot = Object.assign({}, projection.context_snapshot || {}, {
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
        if (workspaceRequestedOpen && shell && shell.dataset.driverActiveTripOrigin !== "excavator") {
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
            var freeBucket = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-free-bucket-open]")
                : null;
            if (freeBucket) {
                event.preventDefault();
                event.stopPropagation();
                openFreeBucket(freeBucket.closest("[data-driver-manual-workspace]"));
                return;
            }
            var pointOpen = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-point-open]")
                : null;
            if (pointOpen) {
                event.preventDefault();
                event.stopPropagation();
                openPointChooser(pointOpen.closest("[data-driver-manual-workspace]"));
                return;
            }
            var pointSheet = event.target && event.target.closest
                ? event.target.closest("[data-driver-point-sheet]")
                : null;
            if (pointSheet && ["next", "current-local"].indexOf(pointSheet.dataset.driverManualPointMode) >= 0) {
                var pointButton = event.target.closest(".driver-unload-tile");
                if (pointButton) {
                    event.preventDefault();
                    event.stopPropagation();
                    var pointForm = pointButton.closest("form");
                    var pointInput = pointForm && pointForm.querySelector('[name="dump_point"]');
                    var pointWorkspace = root.document.querySelector("[data-driver-manual-workspace]");
                    var chosenId = pointInput && pointInput.value;
                    var chosenName = pointButton.dataset.driverPointName;
                    if (pointSheet.dataset.driverManualPointMode === "current-local") {
                        pointButton.disabled = true;
                        enqueueLocalPointChange(pointWorkspace, chosenId, chosenName).then(function () {
                            selectManualPoint(pointWorkspace, chosenId, chosenName);
                            closePointChooser(pointWorkspace);
                        }).catch(function () {
                            pointButton.disabled = false;
                            setResult(pointWorkspace, "storage-error", null, true);
                        });
                    } else {
                        selectManualPoint(pointWorkspace, chosenId, chosenName);
                        closePointChooser(pointWorkspace);
                    }
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
                closeWorkspace(close.closest("[data-driver-manual-workspace]"));
                return;
            }
            var tab = event.target && event.target.closest
                ? event.target.closest("[data-driver-tab-open]")
                : null;
            if (tab) {
                closeWorkspace();
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
            });
            if (root.document && root.document.addEventListener) {
                root.document.addEventListener("visibilitychange", function () {
                    if (!root.document.hidden && currentWorkspace) renderTripTimer(currentWorkspace);
                });
            }
        }
        root.document.addEventListener("keydown", function (event) {
            if (event.key !== "Escape") return;
            var sheet = root.document.querySelector('[data-driver-point-sheet][data-driver-manual-point-mode="next"]');
            if (!sheet || sheet.hidden) return;
            event.preventDefault();
            closePointChooser(root.document.querySelector("[data-driver-manual-workspace]"));
        });
    }

    root.bindDriverManualExcavatorWorkspace = bindAll;
    root.DriverManualExcavatorWorkspace = {
        bindAll: bindAll,
        open: openWorkspace,
        close: closeWorkspace,
        openFreeBucket: openFreeBucket,
        openPointChooser: openPointChooser,
        closePointChooser: closePointChooser,
        selectManualPoint: selectManualPoint,
        createManualDumpTarget: createManualDumpTarget,
        setManualTargetOneOff: setManualTargetOneOff,
        serverTripProjectionContext: serverTripProjectionContext,
        requestAutomaticTripRefresh: requestAutomaticTripRefresh,
        dumpNameSizeClass: dumpNameSizeClass,
        formatElapsedTime: formatElapsedTime,
        tripTimerLabel: tripTimerLabel,
        renderTripTimer: renderTripTimer,
        startTripTimer: startTripTimer,
        stopTripTimer: stopTripTimer,
        markLastDump: markLastDump,
        renderProjection: renderProjection,
        restoreProjection: restoreProjection,
        buildManualLoadEvent: buildManualLoadEvent,
        pointModeForShell: pointModeForShell,
        pointActionCopy: pointActionCopy,
        resultText: resultText
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
