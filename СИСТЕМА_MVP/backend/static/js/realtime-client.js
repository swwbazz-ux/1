(function (window, document) {
    "use strict";

    var APP_CONTRACT_VERSION = "pwa-contract-v1";
    if (
        window.AppPwaContractGuard
        && typeof window.AppPwaContractGuard.registerJavaScript === "function"
    ) {
        window.AppPwaContractGuard.registerJavaScript(APP_CONTRACT_VERSION);
    }

    function buildPattern(pattern) {
        if (!pattern) return null;
        if (pattern instanceof RegExp) return pattern;
        try {
            return new RegExp(pattern);
        } catch (error) {
            return null;
        }
    }

    function mergeConfig(rawConfig) {
        var config = rawConfig || {};
        return {
            stateUrl: config.stateUrl || "",
            screens: Array.isArray(config.screens) ? config.screens : [],
            customRefreshPaths: Array.isArray(config.customRefreshPaths) ? config.customRefreshPaths : [],
            initialVersion: Number(config.initialVersion || 0),
            workPollIntervalMs: config.workPollIntervalMs || config.pollIntervalMs || 5000,
            observerPollIntervalMs: config.observerPollIntervalMs || 15000,
            idleDelayMs: config.idleDelayMs || 2500,
            pollTimeoutMs: config.pollTimeoutMs || 8000,
            maxSilentMs: config.maxSilentMs || 7000,
            mobileQueueKey: config.mobileQueueKey || "mining-master-mobile-sync-queue-v1",
            loginUrl: config.loginUrl || "/"
        };
    }

    function pathMatches(pattern, path) {
        var compiled = buildPattern(pattern);
        return !!compiled && compiled.test(path);
    }

    function findRealtimeScreen(config, path) {
        var matched = null;
        config.screens.some(function (screen) {
            if (!screen || screen.enabled === false) return false;
            if (pathMatches(screen.path, path)) {
                matched = screen;
                return true;
            }
            return false;
        });
        return matched;
    }

    function screenRequiresCustomRefresh(config, screen, path) {
        if (screen && screen.customRefresh) return true;
        return config.customRefreshPaths.some(function (pattern) {
            return pathMatches(pattern, path);
        });
    }

    function screenPollInterval(config, screen) {
        if (screen && Number(screen.pollIntervalMs) > 0) {
            return Number(screen.pollIntervalMs);
        }
        if (
            screen
            && (
                screen.mode === "observer"
                || screen.mode === "manual"
                || screen.role === "system_admin"
                || screen.role === "management"
            )
        ) {
            return config.observerPollIntervalMs;
        }
        return config.workPollIntervalMs;
    }

    function markRealtimeScreen(screen, isWatching) {
        if (!document.body) return;
        if (screen) {
            document.body.dataset.realtimeScreen = screen.name || "";
            document.body.dataset.realtimeMode = screen.mode || "";
            document.body.dataset.realtimeRole = screen.role || "";
        } else if (!document.body.dataset.realtimeScreen) {
            document.body.dataset.realtimeScreen = "none";
        }
        document.body.dataset.realtimeActive = isWatching ? "true" : "false";
    }

    function dispatchWindowEvent(name, detail) {
        window.dispatchEvent(new CustomEvent(name, {detail: detail || {}}));
    }

    function applyActiveRoleState(payload) {
        if (!payload || typeof payload.role_active === "undefined" || !document.body) {
            return;
        }
        var isActive = payload.role_active !== false;
        document.body.dataset.roleAccessActive = isActive ? "true" : "false";
        if (typeof window.applyAppRoleReadonlyState === "function") {
            window.applyAppRoleReadonlyState(isActive);
        }
        dispatchWindowEvent("active-role-state-changed", {
            active: isActive,
            activeRoleCode: payload.active_role_code || "",
            changedAt: payload.active_role_changed_at || ""
        });
    }

    function initRealtimeClient() {
        var config = mergeConfig(window.AppRealtimeConfig);
        var currentPath = window.location.pathname || "/";
        var screen = findRealtimeScreen(config, currentPath);
        var shouldWatch = !!screen;

        markRealtimeScreen(screen, shouldWatch);

        if (!shouldWatch || !window.fetch || !config.stateUrl) {
            if (
                window.AppPwaContractGuard
                && typeof window.AppPwaContractGuard.markServerUnavailable === "function"
            ) {
                window.AppPwaContractGuard.markServerUnavailable();
            }
            window.AppRealtime = {
                config: config,
                screen: screen,
                isWatching: false
            };
            return;
        }

        var stateUrl = config.stateUrl;
        var pollIntervalMs = screenPollInterval(config, screen);
        var idleDelayMs = config.idleDelayMs;
        var currentVersion = config.initialVersion > 0 ? config.initialVersion : null;
        var pendingVersion = null;
        var pendingPreviousVersion = 0;
        var pendingVersionSince = 0;
        var pendingEvents = [];
        var pendingEventsTruncated = false;
        var lastInteractionAt = Date.now();
        var applyingUpdate = false;
        var requiresCustomRefresh = screenRequiresCustomRefresh(config, screen, currentPath);
        var realtimeConsecutiveFailures = 0;
        var realtimeLastSuccessAt = 0;
        var realtimeLastPollStartedAt = 0;
        var realtimePollInFlight = false;
        var realtimePollController = null;
        var realtimePollGeneration = 0;
        var realtimePollTimeoutMs = config.pollTimeoutMs;
        var realtimeMaxSilentMs = Math.max(config.maxSilentMs, pollIntervalMs * 2 + 2000);
        var realtimeStatus = document.querySelector("[data-app-realtime-status]");
        var realtimeUpdateNotice = document.querySelector("[data-app-realtime-update]");
        var realtimeUpdateText = document.querySelector("[data-app-realtime-update-text]");
        var realtimeUpdateButton = document.querySelector("[data-app-realtime-update-button]");
        var manualRefreshMode = screen && screen.mode === "manual";
        var manualRefreshVersion = null;
        var authEnded = false;
        var pollIntervalId = null;
        var pendingUpdateTimeoutId = null;
        var watchdogIntervalId = null;
        var wakeTimeoutId = null;
        var authRedirectScheduled = false;
        var pageFocused = typeof document.hasFocus === "function" ? document.hasFocus() : true;
        var visibilityOnlyActivity = document.body.dataset.realtimeVisibilityOnly === "true";

        function isPageActive() {
            return document.hidden !== true && (visibilityOnlyActivity || pageFocused);
        }

        function clearPollSchedule() {
            if (pollIntervalId !== null) {
                window.clearTimeout(pollIntervalId);
                pollIntervalId = null;
            }
        }

        function clearPendingUpdateSchedule() {
            if (pendingUpdateTimeoutId !== null) {
                window.clearTimeout(pendingUpdateTimeoutId);
                pendingUpdateTimeoutId = null;
            }
        }

        function schedulePendingUpdate(delay) {
            clearPendingUpdateSchedule();
            if (authEnded || !pendingVersion || !isPageActive()) {
                return;
            }
            pendingUpdateTimeoutId = window.setTimeout(function () {
                pendingUpdateTimeoutId = null;
                if (authEnded || !isPageActive()) {
                    return;
                }
                applyPendingUpdate();
            }, delay);
        }

        function scheduleNextPoll() {
            clearPollSchedule();
            if (authEnded || !isPageActive()) {
                return;
            }
            pollIntervalId = window.setTimeout(function () {
                pollIntervalId = null;
                if (authEnded || !isPageActive()) {
                    return;
                }
                inspectRealtimeWatchdog("poll_timer");
                pollOperationalState();
                scheduleNextPoll();
            }, pollIntervalMs);
        }

        function pauseRealtimeConnection() {
            clearPollSchedule();
            clearPendingUpdateSchedule();
            if (wakeTimeoutId !== null) {
                window.clearTimeout(wakeTimeoutId);
                wakeTimeoutId = null;
            }
            if (realtimePollInFlight) {
                realtimePollGeneration += 1;
                if (realtimePollController) {
                    try {
                        realtimePollController.abort();
                    } catch (error) {}
                }
                realtimePollInFlight = false;
                realtimePollController = null;
            }
        }

        function clearPendingMobileQueue() {
            try {
                window.localStorage.removeItem(config.mobileQueueKey);
            } catch (error) {
                // Storage can be unavailable in private or locked-down browser modes.
            }
        }

        function terminateAuthentication() {
            if (authEnded) {
                return;
            }
            authEnded = true;
            pendingVersion = null;
            pendingPreviousVersion = 0;
            pendingVersionSince = 0;
            pendingEvents = [];
            pendingEventsTruncated = false;
            applyingUpdate = false;
            manualRefreshVersion = null;
            if (realtimePollController) {
                try {
                    realtimePollController.abort();
                } catch (error) {}
            }
            realtimePollGeneration += 1;
            realtimePollInFlight = false;
            realtimePollController = null;
            clearPollSchedule();
            clearPendingUpdateSchedule();
            if (wakeTimeoutId !== null) {
                window.clearTimeout(wakeTimeoutId);
                wakeTimeoutId = null;
            }
            if (watchdogIntervalId !== null) {
                window.clearInterval(watchdogIntervalId);
                watchdogIntervalId = null;
            }
            clearPendingMobileQueue();
            applyActiveRoleState({
                role_active: false,
                active_role_code: "",
                active_role_changed_at: ""
            });
            if (realtimeStatus) {
                realtimeStatus.hidden = false;
                realtimeStatus.textContent = "Сессия завершена. Открываем экран входа…";
                realtimeStatus.classList.add("is-visible");
            }
            dispatchWindowEvent("app-authentication-ended", {
                loginUrl: config.loginUrl
            });
            if (!authRedirectScheduled) {
                authRedirectScheduled = true;
                window.setTimeout(function () {
                    if (window.location && typeof window.location.replace === "function") {
                        window.location.replace(config.loginUrl);
                    } else {
                        window.location.href = config.loginUrl;
                    }
                }, 0);
            }
        }

        function publishRealtimeConnectionState(isConnected, detail) {
            var payload = Object.assign({
                connected: !!isConnected,
                failures: realtimeConsecutiveFailures,
                lastSuccessAt: realtimeLastSuccessAt,
                screen: screen ? screen.name : null,
                mode: screen ? screen.mode : null,
                role: screen ? screen.role : null
            }, detail || {});
            document.body.classList.toggle("is-realtime-stale", !isConnected);
            if (realtimeStatus) {
                if (isConnected) {
                    realtimeStatus.classList.remove("is-visible");
                    window.setTimeout(function () {
                        if (!realtimeStatus.classList.contains("is-visible")) {
                            realtimeStatus.hidden = true;
                        }
                    }, 180);
                } else {
                    realtimeStatus.hidden = false;
                    var reveal = window.requestAnimationFrame || function (callback) {
                        return window.setTimeout(callback, 0);
                    };
                    reveal(function () {
                        realtimeStatus.classList.add("is-visible");
                    });
                }
            }
            dispatchWindowEvent("operational-state-connection", payload);
        }

        function inspectRealtimeWatchdog(reason) {
            if (authEnded) {
                return;
            }
            var now = Date.now();
            if (realtimePollInFlight && realtimeLastPollStartedAt && now - realtimeLastPollStartedAt > realtimePollTimeoutMs + 1500) {
                if (realtimePollController) {
                    try {
                        realtimePollController.abort();
                    } catch (error) {}
                }
                realtimePollInFlight = false;
                realtimePollController = null;
                realtimeConsecutiveFailures = Math.max(realtimeConsecutiveFailures, 1);
                publishRealtimeConnectionState(false, {reason: reason || "poll_stuck"});
                dispatchWindowEvent("operational-state-poll-reset", {
                    reason: reason || "poll_stuck",
                    lastPollStartedAt: realtimeLastPollStartedAt,
                    screen: screen ? screen.name : null,
                    mode: screen ? screen.mode : null,
                    role: screen ? screen.role : null
                });
                return;
            }
            if (realtimeLastSuccessAt && now - realtimeLastSuccessAt > realtimeMaxSilentMs) {
                realtimeConsecutiveFailures = Math.max(realtimeConsecutiveFailures, 1);
                publishRealtimeConnectionState(false, {reason: reason || "silent_timeout"});
            } else if (!realtimeLastSuccessAt && realtimeLastPollStartedAt && now - realtimeLastPollStartedAt > realtimePollTimeoutMs) {
                realtimeConsecutiveFailures = Math.max(realtimeConsecutiveFailures, 1);
                publishRealtimeConnectionState(false, {reason: reason || "initial_timeout"});
            }
        }

        function markInteraction() {
            lastInteractionAt = Date.now();
        }

        ["mousedown", "touchstart", "keydown", "input", "change", "dragstart", "pointerdown"].forEach(function (eventName) {
            document.addEventListener(eventName, markInteraction, {passive: true});
        });

        function hasPendingMobileQueue() {
            try {
                var queue = JSON.parse(window.localStorage.getItem(config.mobileQueueKey) || "[]");
                return Array.isArray(queue) && queue.length > 0;
            } catch (error) {
                return false;
            }
        }

        function getUserBusyReason() {
            var active = document.activeElement;
            var activeTag = active && active.tagName ? active.tagName.toLowerCase() : "";
            if (active && (active.isContentEditable || activeTag === "input" || activeTag === "textarea" || activeTag === "select")) {
                return "input_focus";
            }
            if (document.body.classList.contains("modal-open")) {
                return "modal_open";
            }
            if (document.querySelector(".dispatcher-dragging, .is-dragging, .is-hold-pending, .is-hold-ready")) {
                return "active_gesture";
            }
            if (hasPendingMobileQueue()) {
                return "pending_mobile_queue";
            }
            if (Date.now() - lastInteractionAt < idleDelayMs) {
                return "recent_interaction";
            }
            return "";
        }

        function shouldDeferPendingUpdate() {
            var busyReason = getUserBusyReason();
            if (!busyReason) return "";
            if (requiresCustomRefresh && (busyReason === "pending_mobile_queue" || busyReason === "recent_interaction")) {
                return "";
            }
            return busyReason;
        }

        function storeOperationalVersion(version) {
            if (document.body) {
                document.body.dataset.operationalStateVersion = String(version);
            }
            try {
                window.sessionStorage.setItem("operational-state-version", String(version));
            } catch (error) {
                // Session storage is optional; the realtime flow must keep working without it.
            }
        }

        function markOperationalStateApplied(version) {
            var parsedVersion = Number(version || 0);
            if (!Number.isFinite(parsedVersion) || parsedVersion <= 0) {
                return;
            }
            currentVersion = parsedVersion;
            if (pendingVersion && parsedVersion >= pendingVersion) {
                pendingVersion = null;
                pendingPreviousVersion = 0;
                pendingVersionSince = 0;
                pendingEvents = [];
                pendingEventsTruncated = false;
                applyingUpdate = false;
            }
            storeOperationalVersion(parsedVersion);
        }

        function revealRealtimeUpdateNotice(context) {
            var alreadyAnnounced = manualRefreshVersion === context.version;
            manualRefreshVersion = context.version;
            applyingUpdate = false;
            document.body.classList.add("has-realtime-update");
            document.body.dataset.realtimeUpdateAvailable = "true";
            document.body.dataset.realtimePendingVersion = String(context.version);
            if (realtimeUpdateText) {
                realtimeUpdateText.textContent = "На сервере появились новые данные.";
            }
            if (realtimeUpdateNotice) {
                realtimeUpdateNotice.hidden = false;
                var reveal = window.requestAnimationFrame || function (callback) {
                    return window.setTimeout(callback, 0);
                };
                reveal(function () {
                    realtimeUpdateNotice.classList.add("is-visible");
                });
            }
            if (!alreadyAnnounced) {
                dispatchWindowEvent("operational-state-update-available", context);
            }
        }

        function refreshManualRealtimeScreen() {
            var versionToApply = manualRefreshVersion || pendingVersion || currentVersion;
            if (versionToApply) {
                storeOperationalVersion(versionToApply);
            }
            if (typeof window.showAppSyncOverlay === "function") {
                window.showAppSyncOverlay({
                    title: "Обновляем отчет",
                    text: "Загружаем свежие данные с сервера."
                });
            }
            window.location.reload();
        }

        function applyPendingUpdate() {
            if (authEnded || !isPageActive() || !pendingVersion || applyingUpdate) {
                return;
            }
            var versionToApply = pendingVersion;
            var refreshContext = {
                version: versionToApply,
                previousVersion: pendingPreviousVersion || currentVersion || config.initialVersion || 0,
                screen: screen ? screen.name : null,
                mode: screen ? screen.mode : null,
                role: screen ? screen.role : null,
                events: pendingEvents.slice(),
                eventsTruncated: pendingEventsTruncated
            };
            var busyReason = shouldDeferPendingUpdate();
            if (busyReason) {
                dispatchWindowEvent("operational-state-refresh-deferred", Object.assign({}, refreshContext, {
                    reason: busyReason
                }));
                schedulePendingUpdate(1000);
                return;
            }
            applyingUpdate = true;
            if (manualRefreshMode) {
                revealRealtimeUpdateNotice(refreshContext);
                return;
            }
            var customRefresh = typeof window.applyOperationalStateRefresh === "function"
                ? window.applyOperationalStateRefresh(refreshContext)
                : null;
            if (customRefresh) {
                Promise.resolve(customRefresh).then(function (result) {
                    if (result && result.deferred) {
                        applyingUpdate = false;
                        dispatchWindowEvent("operational-state-refresh-deferred", Object.assign({}, refreshContext, {
                            reason: result.reason || "custom_deferred"
                        }));
                        schedulePendingUpdate(1000);
                        return;
                    }
                    if (result && result.applied) {
                        currentVersion = versionToApply;
                        pendingVersion = null;
                        pendingPreviousVersion = 0;
                        pendingVersionSince = 0;
                        pendingEvents = [];
                        pendingEventsTruncated = false;
                        storeOperationalVersion(versionToApply);
                        dispatchWindowEvent("operational-state-refresh-applied", refreshContext);
                        applyingUpdate = false;
                        return;
                    }
                    if (result === false && requiresCustomRefresh) {
                        applyingUpdate = false;
                        dispatchWindowEvent("operational-state-refresh-deferred", Object.assign({}, refreshContext, {
                            reason: "custom_refresh_not_applied"
                        }));
                        schedulePendingUpdate(2000);
                        return;
                    }
                    reloadForOperationalUpdate(versionToApply);
                }).catch(function () {
                    applyingUpdate = false;
                    schedulePendingUpdate(2000);
                });
                return;
            }
            reloadForOperationalUpdate(versionToApply);
        }

        function reloadForOperationalUpdate(version) {
            if (requiresCustomRefresh) {
                currentVersion = version;
                pendingVersion = null;
                pendingPreviousVersion = 0;
                pendingVersionSince = 0;
                pendingEvents = [];
                pendingEventsTruncated = false;
                applyingUpdate = false;
                storeOperationalVersion(version);
                dispatchWindowEvent("operational-state-refresh-skipped", {
                    version: version,
                    screen: screen ? screen.name : null,
                    mode: screen ? screen.mode : null,
                    role: screen ? screen.role : null
                });
                return;
            }
            storeOperationalVersion(version);
            dispatchWindowEvent("operational-state-refresh", {
                version: version,
                screen: screen ? screen.name : null,
                mode: screen ? screen.mode : null,
                role: screen ? screen.role : null
            });
            if (typeof window.showAppSyncOverlay === "function") {
                window.showAppSyncOverlay({
                    title: "Обновляем рабочий экран",
                    text: "Поступили новые данные смены. Загружаем свежую версию без ожидания вслепую."
                });
            }
            window.location.reload();
        }

        function buildOperationalStatePollUrl() {
            var url = new URL(stateUrl, window.location.origin);
            var afterVersion = currentVersion || config.initialVersion || 0;
            if (afterVersion > 0) {
                url.searchParams.set("after", String(afterVersion));
            }
            url.searchParams.set("include_events", "1");
            var roleAppCode = document.body
                ? String(document.body.dataset.appRoleCode || "")
                : "";
            if (roleAppCode) {
                url.searchParams.set("role_app_code", roleAppCode);
            }
            return url.toString();
        }

        function pollOperationalState(options) {
            options = options || {};
            if (authEnded || !isPageActive()) {
                return;
            }
            if (navigator && navigator.onLine === false) {
                realtimeConsecutiveFailures += 1;
                publishRealtimeConnectionState(false, {reason: "offline"});
                if (
                    window.AppPwaContractGuard
                    && typeof window.AppPwaContractGuard.markServerUnavailable === "function"
                ) {
                    window.AppPwaContractGuard.markServerUnavailable();
                }
                return;
            }
            if (realtimePollInFlight && !options.force) {
                inspectRealtimeWatchdog("poll_in_flight");
                return;
            }
            if (realtimePollInFlight && options.force && realtimePollController) {
                try {
                    realtimePollController.abort();
                } catch (error) {}
            }
            var pollGeneration = ++realtimePollGeneration;
            realtimePollInFlight = true;
            realtimeLastPollStartedAt = Date.now();
            var pollController = window.AbortController ? new AbortController() : null;
            realtimePollController = pollController;
            var timeoutId = window.setTimeout(function () {
                if (pollGeneration === realtimePollGeneration && pollController) {
                    try {
                        pollController.abort();
                    } catch (error) {}
                }
            }, realtimePollTimeoutMs);
            var fetchOptions = {
                method: "GET",
                credentials: "same-origin",
                cache: "no-store",
                headers: {"Accept": "application/json"}
            };
            if (pollController) {
                fetchOptions.signal = pollController.signal;
            }
            window.fetch(buildOperationalStatePollUrl(), fetchOptions)
                .then(function (response) {
                    if (pollGeneration !== realtimePollGeneration) {
                        return null;
                    }
                    if (response.status === 401) {
                        terminateAuthentication();
                        return null;
                    }
                    if (!response.ok) {
                        throw new Error("state status " + response.status);
                    }
                    return response.json();
                })
                .then(function (payload) {
                    if (authEnded || pollGeneration !== realtimePollGeneration) {
                        return;
                    }
                    applyActiveRoleState(payload);
                    if (!payload || typeof payload.version === "undefined") {
                        return;
                    }
                    if (
                        window.AppPwaContractGuard
                        && typeof window.AppPwaContractGuard.acceptServerContract === "function"
                    ) {
                        window.AppPwaContractGuard.acceptServerContract(payload);
                    }
                    realtimeConsecutiveFailures = 0;
                    realtimeLastSuccessAt = Date.now();
                    publishRealtimeConnectionState(true);
                    var version = Number(payload.version || 0);
                    if (currentVersion === null) {
                        currentVersion = version;
                        return;
                    }
                    if (version > currentVersion) {
                        if (payload.relevant === false) {
                            var irrelevantPreviousVersion = currentVersion || config.initialVersion || 0;
                            markOperationalStateApplied(version);
                            dispatchWindowEvent("operational-state-refresh-skipped", {
                                version: version,
                                previousVersion: irrelevantPreviousVersion,
                                reason: "irrelevant",
                                screen: screen ? screen.name : null,
                                mode: screen ? screen.mode : null,
                                role: screen ? screen.role : null
                            });
                            return;
                        }
                        if (pendingVersion !== version) {
                            pendingVersionSince = Date.now();
                            pendingPreviousVersion = currentVersion || config.initialVersion || 0;
                        }
                        pendingVersion = version;
                        pendingEvents = Array.isArray(payload.events) ? payload.events.slice() : [];
                        pendingEventsTruncated = payload.events_truncated === true;
                        applyPendingUpdate();
                    }
                })
                .catch(function () {
                    if (authEnded || pollGeneration !== realtimePollGeneration) {
                        return;
                    }
                    realtimeConsecutiveFailures += 1;
                    if (
                        window.AppPwaContractGuard
                        && typeof window.AppPwaContractGuard.markServerUnavailable === "function"
                    ) {
                        window.AppPwaContractGuard.markServerUnavailable();
                    }
                    if (realtimeConsecutiveFailures >= 2 || (options && options.force)) {
                        publishRealtimeConnectionState(false, {reason: "fetch_failed"});
                    }
                    return;
                })
                .finally(function () {
                    window.clearTimeout(timeoutId);
                    if (authEnded || pollGeneration !== realtimePollGeneration) {
                        return;
                    }
                    realtimePollInFlight = false;
                    realtimePollController = null;
                });
        }

        function wakeRealtimeConnection(reason) {
            if (authEnded || !isPageActive()) {
                return;
            }
            inspectRealtimeWatchdog(reason || "wake");
            if (wakeTimeoutId !== null) {
                return;
            }
            clearPollSchedule();
            wakeTimeoutId = window.setTimeout(function () {
                wakeTimeoutId = null;
                if (authEnded || !isPageActive()) {
                    return;
                }
                pollOperationalState({force: true});
                scheduleNextPoll();
            }, 0);
        }

        function getDebugState() {
            return {
                isWatching: true,
                screen: screen,
                mode: screen ? screen.mode : null,
                currentVersion: currentVersion,
                pendingVersion: pendingVersion,
                pendingPreviousVersion: pendingPreviousVersion,
                pendingVersionSince: pendingVersionSince,
                pendingEvents: pendingEvents.slice(),
                pendingEventsTruncated: pendingEventsTruncated,
                initialVersion: config.initialVersion,
                applyingUpdate: applyingUpdate,
                busyReason: getUserBusyReason(),
                pollInFlight: realtimePollInFlight,
                consecutiveFailures: realtimeConsecutiveFailures,
                pageActive: isPageActive(),
                pollIntervalMs: pollIntervalMs,
                lastSuccessAt: realtimeLastSuccessAt,
                lastPollStartedAt: realtimeLastPollStartedAt,
                manualRefreshMode: manualRefreshMode,
                manualRefreshVersion: manualRefreshVersion,
                authEnded: authEnded
            };
        }

        window.AppRealtime = {
            config: config,
            screen: screen,
            isWatching: true,
            poll: pollOperationalState,
            wake: wakeRealtimeConnection,
            refresh: refreshManualRealtimeScreen,
            markApplied: markOperationalStateApplied,
            getDebugState: getDebugState
        };
        if (realtimeUpdateButton) {
            realtimeUpdateButton.addEventListener("click", refreshManualRealtimeScreen);
        }
        dispatchWindowEvent("app-realtime-ready", {
            screen: screen ? screen.name : null,
            mode: screen ? screen.mode : null,
            role: screen ? screen.role : null
        });

        if (isPageActive()) {
            pollOperationalState({force: true});
            scheduleNextPoll();
        }
        watchdogIntervalId = window.setInterval(function () {
            inspectRealtimeWatchdog("slow_watchdog");
        }, 2000);
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) {
                pauseRealtimeConnection();
                return;
            }
            pageFocused = typeof document.hasFocus !== "function" || document.hasFocus();
            if (visibilityOnlyActivity || pageFocused) {
                wakeRealtimeConnection("visibilitychange");
            }
        });
        window.addEventListener("focus", function () {
            pageFocused = true;
            wakeRealtimeConnection("focus");
        });
        window.addEventListener("blur", function () {
            pageFocused = false;
            if (visibilityOnlyActivity && document.hidden !== true) {
                scheduleNextPoll();
                return;
            }
            pauseRealtimeConnection();
        });
        window.addEventListener("pageshow", function (event) {
            pageFocused = typeof document.hasFocus !== "function" || document.hasFocus();
            wakeRealtimeConnection(event && event.persisted ? "pageshow_persisted" : "pageshow");
        });
        document.addEventListener("resume", function () {
            pageFocused = true;
            wakeRealtimeConnection("resume");
        });
        window.addEventListener("resume", function () {
            pageFocused = true;
            wakeRealtimeConnection("window_resume");
        });
        window.addEventListener("online", function () {
            if (authEnded) {
                return;
            }
            realtimeConsecutiveFailures = 0;
            wakeRealtimeConnection("online");
        });
        window.addEventListener("offline", function () {
            if (authEnded) {
                return;
            }
            realtimeConsecutiveFailures += 1;
            publishRealtimeConnectionState(false, {reason: "offline"});
            if (
                window.AppPwaContractGuard
                && typeof window.AppPwaContractGuard.markServerUnavailable === "function"
            ) {
                window.AppPwaContractGuard.markServerUnavailable();
            }
        });
        ["pointerdown", "pointerup", "touchstart", "touchend", "mousedown", "click", "keydown"].forEach(function (eventName) {
            document.addEventListener(eventName, function () {
                if (!realtimeLastSuccessAt || Date.now() - realtimeLastSuccessAt > realtimeMaxSilentMs) {
                    wakeRealtimeConnection(eventName);
                }
            }, {passive: true});
        });
    }

    document.addEventListener("DOMContentLoaded", initRealtimeClient);
})(window, document);
