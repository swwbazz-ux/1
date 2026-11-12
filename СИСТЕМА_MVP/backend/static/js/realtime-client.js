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
            sseUrl: config.sseUrl || "/realtime/stream/",
            sseEnabled: config.sseEnabled === true,
            sseHeartbeatTimeoutMs: Math.max(15000, Number(config.sseHeartbeatTimeoutMs || 30000)),
            sseReconnectMaxMs: Math.max(5000, Number(config.sseReconnectMaxMs || 30000)),
            sseTruthRefreshIntervalMs: Math.max(20000, Number(config.sseTruthRefreshIntervalMs || 60000)),
            voiceHandoffTimeoutMs: Math.max(1000, Number(config.voiceHandoffTimeoutMs || 5000)),
            voiceHandoffFailureLimit: Math.max(1, Number(config.voiceHandoffFailureLimit || 3)),
            screens: Array.isArray(config.screens) ? config.screens : [],
            customRefreshPaths: Array.isArray(config.customRefreshPaths) ? config.customRefreshPaths : [],
            initialVersion: Number(config.initialVersion || 0),
            workPollIntervalMs: config.workPollIntervalMs || config.pollIntervalMs || 5000,
            observerPollIntervalMs: config.observerPollIntervalMs || 15000,
            idleDelayMs: config.idleDelayMs || 2500,
            pollTimeoutMs: config.pollTimeoutMs || 8000,
            maxSilentMs: config.maxSilentMs || 15000,
            truthRefreshIntervalMs: config.truthRefreshIntervalMs || 20000,
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
        /* The delivery cursor and the pixels currently on screen are separate
           facts. A persistent cursor can prove that voice work was accepted,
           but it cannot prove that a newly opened/cached DOM contains the same
           snapshot. */
        var domAppliedVersion = Math.max(0, Number(config.initialVersion || 0));
        var observedVersion = currentVersion || 0;
        var pendingVersion = null;
        var pendingPreviousVersion = 0;
        var pendingVersionSince = 0;
        var pendingEvents = [];
        var pendingEventsTruncated = false;
        var pendingSnapshotRequired = false;
        var pendingSnapshotUrl = "";
        var pendingSignalsAfterApply = false;
        var pendingRequireStreamThroughSnapshot = false;
        var pendingVoiceHandoff = null;
        var pendingVoiceHandoffVersion = 0;
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
        var realtimeMaxSilentMs = Math.max(15000, config.maxSilentMs, pollIntervalMs * 2 + 2000);
        var realtimeStatus = document.querySelector("[data-app-realtime-status]");
        var realtimeUpdateNotice = document.querySelector("[data-app-realtime-update]");
        var realtimeUpdateText = document.querySelector("[data-app-realtime-update-text]");
        var realtimeUpdateButton = document.querySelector("[data-app-realtime-update-button]");
        var manualRefreshMode = screen && screen.mode === "manual";
        var manualRefreshVersion = null;
        var foregroundReconcileEnabled = !!(screen && screen.foregroundReconcile === true);
        var foregroundReconcileRequested = false;
        var foregroundReconcileReason = "";
        var foregroundReconcileRevision = 0;
        var connectionState = "unknown";
        var connectionStateSince = Date.now();
        var lastNativeSuccessAt = 0;
        var auxiliarySuccessAt = {web_heartbeat: 0, fragment: 0};
        var transportInterrupted = false;
        var lastSuccessfulTruthAt = Date.now();
        var lastReconciledWakeAt = 0;
        var activeSince = Date.now();
        var lastWakeAt = 0;
        var lastWakeRevision = -1;
        var refreshFailures = 0;
        var refreshRetryAt = 0;
        var pollTimeoutId = null;
        var pendingOutboxCount = Number(window.operationalOutboxPendingCount || window.driverOfflinePendingCount || 0);
        var authEnded = false;
        var pollIntervalId = null;
        var pendingUpdateTimeoutId = null;
        var watchdogIntervalId = null;
        var lastWatchdogAt = Date.now();
        var wakeTimeoutId = null;
        var authRedirectScheduled = false;
        var pagePaused = false;
        var pageFocused = typeof document.hasFocus === "function" ? document.hasFocus() : true;
        var visibilityOnlyActivity = document.body.dataset.realtimeVisibilityOnly === "true";
        var sseEligible = config.sseEnabled
            && !!window.EventSource
            && !!screen
            && (screen.role === "driver" || screen.role === "excavator_operator");
        var sseSource = null;
        var sseGeneration = 0;
        var sseState = sseEligible ? "standby" : "disabled";
        var sseLastDataAt = 0;
        var sseFailureCount = 0;
        var sseReconnectTimer = null;
        var sseConnectStartedAt = 0;
        var ssePrimaryActive = false;
        var sseServerHighWater = 0;
        var sseCatchupEvents = [];
        var sseCatchupTruncated = false;
        var sseCatchupSettleTimer = null;
        var sseSnapshotBarrierVersion = 0;
        var wakeHintVersion = 0;
        var deliveryContextKey = "";
        var journalEpoch = "";
        var deliveryStoragePrefix = "operational-delivery-v1:";
        var deliveryContextHydratedKey = "";
        var refreshGeneration = 0;
        var pendingWorkRevision = 0;
        var pendingJournalReplayRequired = false;
        var voiceHandoffFailures = 0;
        var voiceHandoffWindowStart = 0;
        var deferredVoiceBatch = null;
        var authoritativeResetRevision = 0;
        var activeAuthoritativeReset = null;

        function appliedCursor() {
            return currentVersion === null ? Math.max(0, Number(config.initialVersion || 0)) : Number(currentVersion);
        }

        function isNativeShell() {
            return !!(document.body && document.body.dataset.nativeApp === "true");
        }

        /* The server invalidates older sessions of the same access on every new
           login (session generation = access.last_login_at). Until the role is
           restored the stream answers 409 role_context_inactive and operational
           data is not applicable. Retrying the stream in a tight loop helps
           nobody, so the client parks itself and probes rarely. */
        var roleInactive = false;
        var roleInactiveProbeId = null;

        function markRoleInactive(reason) {
            if (roleInactive) return;
            roleInactive = true;
            pagePaused = false;
            closeSse(reason || "role_context_inactive", false);
            clearPollSchedule();
            clearSseReconnect();
            clearPendingUpdateSchedule();
            updateConnectionState("role_inactive");
            if (roleInactiveProbeId !== null) window.clearTimeout(roleInactiveProbeId);
            roleInactiveProbeId = window.setTimeout(function () {
                roleInactiveProbeId = null;
                if (!roleInactive) return;
                roleInactive = false;
                pollOperationalState({force: true});
                scheduleNextPoll();
            }, 60000);
        }

        function clearRoleInactive() {
            if (!roleInactive) return;
            roleInactive = false;
            if (roleInactiveProbeId !== null) {
                window.clearTimeout(roleInactiveProbeId);
                roleInactiveProbeId = null;
            }
        }

        function isPageActive() {
            if (isNativeShell()) return true;
            return !pagePaused && document.hidden !== true && (visibilityOnlyActivity || pageFocused);
        }

        function resumePageActivity() {
            if (document.hidden === true && !isNativeShell()) return;
            if (pagePaused || !isPageActive()) activeSince = Date.now();
            pagePaused = false;
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
            if (authEnded || pendingVersion === null || !isPageActive()) {
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
            if (authEnded || !isPageActive() || ssePrimaryActive) {
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
            }, Math.min(30000, pollIntervalMs * Math.pow(2, Math.min(realtimeConsecutiveFailures, 4))));
        }

        function requestForegroundReconcile(reason) {
            if (!foregroundReconcileEnabled || authEnded) return false;
            if (!foregroundReconcileRequested) foregroundReconcileRevision += 1;
            foregroundReconcileRequested = true;
            if (reason && !foregroundReconcileReason) foregroundReconcileReason = String(reason);
            return true;
        }

        function requestReconcile(reason, version) {
            if (authEnded || !foregroundReconcileEnabled) return false;
            var parsed = Number(version);
            // Native heartbeat and FCM carry a global high-water mark, not an
            // acknowledgement that this WebView processed every lower event.
            // Keep it as a wake hint and let the addressed catch-up request
            // advance observed/applied positions in order.
            if (Number.isSafeInteger(parsed) && parsed >= 0) wakeHintVersion = Math.max(wakeHintVersion, parsed);
            requestForegroundReconcile(reason || "explicit_reconcile");
            updateConnectionState(reason || "explicit_reconcile");
            wakeRealtimeConnection(reason || "explicit_reconcile", {explicit: true});
            return true;
        }

        function queueReconcileVersion(version, payload) {
            var target = Math.max(Number(version || 0), Number(pendingVersion || 0));
            if (pendingVersion === null || target > pendingVersion) {
                pendingVersionSince = Date.now();
                pendingPreviousVersion = appliedCursor();
            }
            pendingVersion = target;
            if (payload && Array.isArray(payload.events)) {
                var eventVersions = {};
                pendingEvents.concat(payload.events).forEach(function (event) {
                    if (event && Number(event.version) > 0) eventVersions[String(event.version)] = event;
                });
                var collectedEvents = Object.keys(eventVersions).map(function (key) { return eventVersions[key]; });
                collectedEvents.sort(function (left, right) { return Number(left.version) - Number(right.version); });
                pendingEvents = collectedEvents.slice(-100);
                // Bound retained deltas when fragments keep failing. Server truth can recover
                // the final state even after older events have been discarded.
                pendingEventsTruncated = pendingEventsTruncated || payload.events_truncated === true || collectedEvents.length > 100;
            }
            if (payload && payload.snapshot_required === true) {
                pendingSnapshotRequired = true;
                if (payload.snapshot_url) pendingSnapshotUrl = String(payload.snapshot_url);
            }
            if (payload && payload.defer_signals_until_apply === true) {
                pendingSignalsAfterApply = true;
            }
            if (payload && payload.require_stream_through_snapshot === true) {
                pendingRequireStreamThroughSnapshot = true;
            }
            pendingWorkRevision += 1;
            persistDeliveryState();
        }

        function clearForegroundReconcile(expectedRevision) {
            if (
                Number(expectedRevision || 0) > 0
                && Number(expectedRevision) !== foregroundReconcileRevision
            ) {
                return false;
            }
            foregroundReconcileRequested = false;
            foregroundReconcileReason = "";
            return true;
        }

        function pauseRealtimeConnection(reason) {
            pagePaused = true;
            // A new background cycle invalidates any older fragment's reconciliation revision.
            if (foregroundReconcileEnabled) foregroundReconcileRevision += 1;
            requestForegroundReconcile(reason || "background");
            clearPollSchedule();
            clearPendingUpdateSchedule();
            if (wakeTimeoutId !== null) {
                window.clearTimeout(wakeTimeoutId);
                wakeTimeoutId = null;
            }
            lastWakeAt = 0;
            refreshRetryAt = 0;
            syncNativeOperationalState("background_handoff");
            closeSse("background_handoff", false);
            cancelCurrentPoll("planned_abort", false);
            diagnoseConnection("background_pause", connectionState, connectionState);
        }

        function cancelCurrentPoll(reason, countFailure) {
            if (!realtimePollInFlight) return;
            var controller = realtimePollController;
            // Invalidate before abort: the rejected old promise cannot count twice or clear a new owner.
            realtimePollGeneration += 1;
            realtimePollInFlight = false;
            realtimePollController = null;
            window.clearTimeout(pollTimeoutId);
            pollTimeoutId = null;
            if (controller) {
                try { controller.abort(); } catch (error) {}
            }
            if (countFailure) recordPollFailure(reason || "timeout");
            else diagnoseConnection(reason || "planned_abort", connectionState, connectionState);
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
            closeSse("authentication_ended", false);
            pendingVersion = null;
            pendingPreviousVersion = 0;
            pendingVersionSince = 0;
            pendingEvents = [];
            pendingEventsTruncated = false;
            pendingSnapshotRequired = false;
            pendingSnapshotUrl = "";
            pendingSignalsAfterApply = false;
            pendingRequireStreamThroughSnapshot = false;
            pendingVoiceHandoff = null;
            pendingVoiceHandoffVersion = 0;
            voiceHandoffFailures = 0;
            voiceHandoffWindowStart = 0;
            deferredVoiceBatch = null;
            sseSnapshotBarrierVersion = 0;
            activeAuthoritativeReset = null;
            refreshGeneration += 1;
            applyingUpdate = false;
            manualRefreshVersion = null;
            clearForegroundReconcile();
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

        function nativeHeartbeatFresh(now) {
            return lastNativeSuccessAt > 0 && now - lastNativeSuccessAt <= 15000;
        }

        function auxiliaryChannelFresh(now) {
            var latest = Math.max(auxiliarySuccessAt.web_heartbeat, auxiliarySuccessAt.fragment);
            return nativeHeartbeatFresh(now) || (latest > 0 && now - latest <= 15000);
        }

        function reportTransportSuccess(evidence) {
            evidence = evidence || {};
            var channel = evidence.channel;
            var occurredAt = Number(evidence.occurredAtMs);
            var now = Date.now();
            if (authEnded || !Object.prototype.hasOwnProperty.call(auxiliarySuccessAt, channel)
                || !Number.isFinite(occurredAt) || occurredAt <= auxiliarySuccessAt[channel]
                || occurredAt <= 0 || now - occurredAt > 15000 || occurredAt > now + 1000) return false;
            var needsRecovery = realtimeConsecutiveFailures > 0 || connectionState === "lost" || hasPendingReconcile();
            auxiliarySuccessAt[channel] = occurredAt;
            realtimeLastSuccessAt = Math.max(realtimeLastSuccessAt, occurredAt);
            realtimeConsecutiveFailures = 0;
            transportInterrupted = false;
            if (channel === "web_heartbeat") {
                if (needsRecovery) {
                    refreshRetryAt = 0;
                    requestReconcile("web_heartbeat_success", evidence.serverVersion);
                } else if (Number(evidence.serverVersion) > observedVersion) {
                    wakeRealtimeConnection("web_heartbeat_version");
                }
            }
            updateConnectionState(channel + "_success");
            return true;
        }

        function hasPendingReconcile() {
            return pendingVersion !== null || foregroundReconcileRequested || applyingUpdate || pendingOutboxCount > 0;
        }

        function diagnoseConnection(cause, before, after) {
            dispatchWindowEvent("app:connectiondiagnostic", {
                cause: cause || "state_change", owner: "realtime_poll",
                generation: realtimePollGeneration, failureCount: realtimeConsecutiveFailures,
                lastSuccessAt: realtimeLastSuccessAt, durationMs: Date.now() - connectionStateSince,
                nativeHeartbeatFresh: nativeHeartbeatFresh(Date.now()), pendingOutboxCount: pendingOutboxCount,
                fromState: before, toState: after
            });
        }

        function publishRealtimeConnectionState(state, detail) {
            var previousState = connectionState;
            connectionState = state;
            document.body.classList.toggle("is-realtime-stale", state === "lost");
            document.body.dataset.connectionState = state;
            document.body.dataset.operationalObservedVersion = String(observedVersion);
            document.body.dataset.operationalAppliedVersion = String(appliedCursor());
            document.body.dataset.operationalDomVersion = String(domAppliedVersion);
            document.body.dataset.operationalPendingVersion = pendingVersion === null ? "" : String(pendingVersion);
            var text = {
                unknown: "Проверяем связь…", ok: "Связь есть", weak: "Переподключение…",
                lost: "Связь с сервером потеряна", recovering: "Восстанавливаем данные…"
            }[state];
            if (realtimeStatus) {
                realtimeStatus.textContent = text;
                var visible = state !== "ok" && state !== "unknown" && !document.querySelector("[data-connection-indicator]");
                realtimeStatus.hidden = !visible;
                realtimeStatus.classList.toggle("is-visible", visible);
            }
            var payload = Object.assign({
                state: state, previousState: previousState,
                connected: state === "ok" || state === "recovering",
                failures: realtimeConsecutiveFailures, lastSuccessAt: realtimeLastSuccessAt,
                nativeHeartbeatFresh: nativeHeartbeatFresh(Date.now()),
                pendingOutboxCount: pendingOutboxCount, observedVersion: observedVersion,
                appliedVersion: appliedCursor(), domAppliedVersion: domAppliedVersion,
                pendingReconcileVersion: pendingVersion,
                screen: screen ? screen.name : null, mode: screen ? screen.mode : null, role: screen ? screen.role : null
            }, detail || {});
            dispatchWindowEvent("operational-state-connection", payload);
            if (previousState !== state) {
                diagnoseConnection(payload.reason || "state_change", previousState, state);
                connectionStateSince = Date.now();
            }
        }

        function updateConnectionState(reason) {
            if (authEnded || !isPageActive()) return;
            var now = Date.now();
            var since = Math.max(realtimeLastSuccessAt, activeSince);
            var silent = now - since >= realtimeMaxSilentMs;
            var supportingChannelFresh = auxiliaryChannelFresh(now);
            var state;
            if (silent || (realtimeConsecutiveFailures >= 3 && !supportingChannelFresh)) state = "lost";
            else if ((realtimeConsecutiveFailures > 0 || transportInterrupted) && !supportingChannelFresh) state = "weak";
            else if (realtimeLastSuccessAt > 0) state = hasPendingReconcile() ? "recovering" : "ok";
            else state = connectionState === "weak" ? "weak" : "unknown";
            publishRealtimeConnectionState(state, {reason: reason || "state_change"});
        }

        function recordPollFailure(reason) {
            realtimeConsecutiveFailures += 1;
            requestForegroundReconcile(reason || "network_error");
            if (foregroundReconcileEnabled) queueReconcileVersion(observedVersion, null);
            if (window.AppPwaContractGuard && typeof window.AppPwaContractGuard.markServerUnavailable === "function") {
                window.AppPwaContractGuard.markServerUnavailable();
            }
            updateConnectionState(reason || "network_error");
            diagnoseConnection(reason || "network_error", connectionState, connectionState);
        }

        function inspectRealtimeWatchdog() {
            if (authEnded || !isPageActive()) return;
            var now = Date.now();
            var watchdogGapMs = now - lastWatchdogAt;
            lastWatchdogAt = now;
            if (ssePrimaryActive && sseLastDataAt > 0
                    && now - sseLastDataAt >= config.sseHeartbeatTimeoutMs) {
                enterSseFallback("sse_heartbeat_timeout", true);
                return;
            }
            if (sseEligible && sseState === "connecting" && sseConnectStartedAt > 0
                    && now - sseConnectStartedAt >= config.sseHeartbeatTimeoutMs) {
                // A blocked or hung EventSource may never fire onerror; without this
                // guard the client stays "connecting" and never tries again.
                enterSseFallback("sse_connect_timeout", true);
                return;
            }
            if (sseEligible && !ssePrimaryActive && !sseSource && sseReconnectTimer === null) {
                /* Re-arming the stream must not depend on a successful
                   /realtime/state/ poll: in an outage both channels are down, and
                   after the stream returns the poll may stay unavailable
                   (T-016 e2e §10.3). The watchdog knows the retry timer is gone
                   (background pause, pagehide, lost schedule) and re-arms it.
                   Exponential backoff still applies to every failed connect. */
                scheduleSseReconnect("sse_watchdog", {immediate: true});
            }
            if (screen && screen.role === "excavator_operator"
                    && watchdogGapMs > Math.max(30000, realtimeMaxSilentMs * 2)) {
                // Android can freeze a visible WebView without delivering visibility/pagehide.
                // A delayed timer is not network evidence: start a fresh active window and probe.
                activeSince = now;
                transportInterrupted = true;
                cancelCurrentPoll("planned_abort", false);
                requestForegroundReconcile("watchdog_resume");
                if (connectionState !== "lost") {
                    publishRealtimeConnectionState("weak", {reason: "watchdog_resume"});
                }
                diagnoseConnection("watchdog_resume", connectionState, connectionState);
                wakeRealtimeConnection("watchdog_resume", {reconcile: true, reconcileReason: "watchdog_resume"});
                return;
            }
            if (realtimePollInFlight && now - realtimeLastPollStartedAt >= realtimePollTimeoutMs) {
                cancelCurrentPoll("timeout", true);
                scheduleNextPoll();
            }
            var previous = connectionState;
            updateConnectionState("silent_timeout");
            if (connectionState === "lost" && previous !== "lost") {
                requestForegroundReconcile("silent_timeout");
                if (foregroundReconcileEnabled) queueReconcileVersion(observedVersion, null);
            }
            var truthInterval = ssePrimaryActive
                ? config.sseTruthRefreshIntervalMs
                : config.truthRefreshIntervalMs;
            if (foregroundReconcileEnabled && now - lastSuccessfulTruthAt >= truthInterval) {
                requestForegroundReconcile("periodic_server_truth");
                if (ssePrimaryActive) pendingSnapshotRequired = true;
                queueReconcileVersion(currentVersion || observedVersion, null);
                if (!realtimeConsecutiveFailures && realtimeLastSuccessAt) applyPendingUpdate();
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

        function normalizeDeliveryContext(payload) {
            payload = payload || {};
            var context = payload.context && typeof payload.context === "object" ? payload.context : {};
            var equipment = Array.isArray(context.worker_equipment_ids)
                ? context.worker_equipment_ids.slice()
                : (Array.isArray(payload.worker_equipment_ids) ? payload.worker_equipment_ids.slice() : []);
            equipment = equipment.map(function (value) { return String(value); }).sort();
            var roleCode = String(context.role_app_code || payload.role_app_code || (screen && screen.role) || "");
            var sessionRevision = String(context.session_revision || payload.session_revision || "");
            var cursorScope = String(context.cursor_scope || "");
            var epoch = String(
                payload.journal_epoch
                || (payload.journal && (payload.journal.epoch || payload.journal.generation))
                || ""
            );
            var key = cursorScope || [
                String(context.access_id || ""),
                String(context.employee_id || ""),
                roleCode,
                equipment.join(","),
                String(context.active_shift_id || ""),
                sessionRevision
            ].join("|");
            return {key: key, epoch: epoch, roleCode: roleCode, equipment: equipment};
        }

        function deliveryStorageKey(contextKey, epoch) {
            if (!contextKey) return "";
            return deliveryStoragePrefix + encodeURIComponent(contextKey) + ":" + encodeURIComponent(epoch || "legacy");
        }

        function persistDeliveryState() {
            var key = deliveryStorageKey(deliveryContextKey, journalEpoch);
            if (!key) return;
            var value = {
                contextKey: deliveryContextKey,
                journalEpoch: journalEpoch,
                appliedVersion: appliedCursor(),
                pendingVersion: pendingVersion === null ? 0 : Number(pendingVersion),
                pendingPreviousVersion: Number(pendingPreviousVersion || 0),
                pendingEvents: pendingEvents.slice(-100),
                pendingEventsTruncated: pendingEventsTruncated === true,
                pendingSnapshotRequired: pendingSnapshotRequired === true,
                pendingSnapshotUrl: String(pendingSnapshotUrl || ""),
                pendingSignalsAfterApply: pendingSignalsAfterApply === true,
                pendingRequireStreamThroughSnapshot: pendingRequireStreamThroughSnapshot === true,
                pendingJournalReplayRequired: pendingJournalReplayRequired === true,
                authoritativeReset: activeAuthoritativeReset ? {
                    targetVersion: Number(activeAuthoritativeReset.targetVersion),
                    contextKey: String(activeAuthoritativeReset.contextKey || ""),
                    journalEpoch: String(activeAuthoritativeReset.journalEpoch || "")
                } : null,
                savedAt: Date.now()
            };
            try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (error) {}
            syncNativeOperationalState("cursor_persisted");
        }

        function acceptDeliveryContext(payload) {
            var next = normalizeDeliveryContext(payload);
            if (!next.key) return false;
            var changed = !!deliveryContextKey && deliveryContextKey !== next.key;
            var epochChanged = !!journalEpoch && !!next.epoch && journalEpoch !== next.epoch;
            deliveryContextKey = next.key;
            if (next.epoch) journalEpoch = next.epoch;
            document.body.dataset.operationalCursorScope = deliveryContextKey;
            document.body.dataset.operationalJournalEpoch = journalEpoch;
            var scopedStorageKey = deliveryStorageKey(deliveryContextKey, journalEpoch);
            if (!changed && !epochChanged && deliveryContextHydratedKey !== scopedStorageKey) {
                deliveryContextHydratedKey = scopedStorageKey;
                try {
                    var stored = JSON.parse(window.localStorage.getItem(
                        scopedStorageKey
                    ) || "null");
                    var serverHighWater = Number(payload.version || 0);
                    var storedApplied = Number(stored && stored.appliedVersion || 0);
                    var storedPending = Number(stored && stored.pendingVersion || 0);
                    var storedReset = stored && stored.authoritativeReset;
                    var storedResetValid = !!(
                        storedReset
                        && Number(storedReset.targetVersion) === storedPending
                        && String(storedReset.contextKey || "") === deliveryContextKey
                        && String(storedReset.journalEpoch || "") === journalEpoch
                    );
                    if (stored && Number.isSafeInteger(storedApplied) && storedApplied >= 0
                            && (!serverHighWater || storedApplied <= serverHighWater)) {
                        /* HTML proves only the rendered snapshot. For an already
                           known scope/epoch, the persistent applied value is the
                           last proven delivery cursor and may be below a freshly
                           rendered document. */
                        currentVersion = storedApplied;
                    }
                    var storedHasPending = !!(
                        stored
                        && Number.isSafeInteger(storedPending)
                        && (storedPending > storedApplied || storedResetValid)
                    );
                    if (storedHasPending
                            && (!serverHighWater || storedPending <= serverHighWater || storedResetValid)) {
                        if (storedResetValid) {
                            var resetBridge = window.NativeBackgroundConnection;
                            activeAuthoritativeReset = {
                                revision: ++authoritativeResetRevision,
                                targetVersion: storedPending,
                                contextKey: deliveryContextKey,
                                journalEpoch: journalEpoch,
                                nativeResetRequired: !!(
                                    resetBridge && typeof resetBridge.syncOperationalState === "function"
                                ),
                                nativeResetPromise: null
                            };
                        }
                        pendingPreviousVersion = Number(stored && stored.pendingPreviousVersion);
                        if (!Number.isSafeInteger(pendingPreviousVersion) || pendingPreviousVersion < 0) {
                            pendingPreviousVersion = storedApplied;
                        }
                        pendingJournalReplayRequired = storedResetValid !== true && !!(
                            stored.pendingJournalReplayRequired === true
                            || domAppliedVersion > storedApplied
                        );
                        queueReconcileVersion(storedPending, {
                            events: Array.isArray(stored && stored.pendingEvents) ? stored.pendingEvents : [],
                            events_truncated: !!(stored && stored.pendingEventsTruncated),
                            snapshot_required: !!(stored && stored.pendingSnapshotRequired),
                            snapshot_url: String(stored && stored.pendingSnapshotUrl || ""),
                            defer_signals_until_apply: !!(stored && stored.pendingSignalsAfterApply),
                            require_stream_through_snapshot: !!(stored && stored.pendingRequireStreamThroughSnapshot)
                        });
                    }
                    if (domAppliedVersion !== appliedCursor()) {
                        requestForegroundReconcile("cached_dom_cursor_mismatch");
                        queueReconcileVersion(Math.max(appliedCursor(), serverHighWater), {
                            snapshot_required: true,
                            defer_signals_until_apply: pendingSignalsAfterApply
                        });
                    }
                } catch (error) {}
            }
            if (changed || epochChanged) {
                deliveryContextHydratedKey = scopedStorageKey;
                refreshGeneration += 1;
                applyingUpdate = false;
                activeAuthoritativeReset = null;
                // A cursor is never reused across user/role/equipment or journal reset.
                // The following addressed poll/snapshot establishes the new baseline.
                observedVersion = appliedCursor();
                pendingEvents = [];
                pendingEventsTruncated = true;
                pendingSnapshotRequired = true;
                pendingSnapshotUrl = "";
                pendingSignalsAfterApply = false;
                pendingRequireStreamThroughSnapshot = false;
                pendingVoiceHandoff = null;
                pendingVoiceHandoffVersion = 0;
                pendingJournalReplayRequired = false;
                pendingWorkRevision += 1;
                sseSnapshotBarrierVersion = 0;
                requestForegroundReconcile(epochChanged ? "journal_epoch_changed" : "role_context_changed");
            }
            persistDeliveryState();
            return changed || epochChanged;
        }

        function syncNativeOperationalState(reason, options) {
            options = options || {};
            var bridge = window.NativeBackgroundConnection;
            if (!bridge || typeof bridge.syncOperationalState !== "function") return Promise.resolve({});
            return Promise.resolve(bridge.syncOperationalState({
                contextKey: deliveryContextKey,
                journalEpoch: journalEpoch,
                appliedVersion: appliedCursor(),
                pendingVersion: pendingVersion === null ? 0 : Number(pendingVersion),
                authoritativeReset: options.authoritativeReset === true,
                resetVersion: Number(options.resetVersion || 0),
                reason: String(reason || "webview_sync")
            })).catch(function () { return {}; });
        }

        function storeOperationalVersion(version) {
            if (document.body) {
                document.body.dataset.operationalStateVersion = String(domAppliedVersion);
            }
            try {
                window.sessionStorage.setItem("operational-state-version", String(domAppliedVersion));
            } catch (error) {
                // Session storage is optional; the realtime flow must keep working without it.
            }
            persistDeliveryState();
        }

        function markOperationalStateApplied(version) {
            var parsedVersion = Number(version);
            if (!Number.isSafeInteger(parsedVersion) || parsedVersion < 0) return;
            domAppliedVersion = Math.max(domAppliedVersion, parsedVersion);
            if (applyingUpdate) {
                storeOperationalVersion(appliedCursor());
                return;
            }
            currentVersion = Math.max(appliedCursor(), parsedVersion);
            observedVersion = Math.max(observedVersion, parsedVersion);
            // Role code may call this inside the currently owned refresh. Only its resolved
            // {applied:true} promise can release that owner and clear the reconciliation revision.
            if (!applyingUpdate && !foregroundReconcileRequested && pendingVersion !== null && parsedVersion >= pendingVersion) {
                pendingVersion = null;
                pendingPreviousVersion = 0;
                pendingVersionSince = 0;
                pendingEvents = [];
                pendingEventsTruncated = false;
                pendingSnapshotRequired = false;
                pendingSnapshotUrl = "";
                pendingSignalsAfterApply = false;
                pendingRequireStreamThroughSnapshot = false;
                sseSnapshotBarrierVersion = 0;
            }
            storeOperationalVersion(currentVersion);
            updateConnectionState("dom_applied");
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

        function withVoiceHandoffDeadline(handoff) {
            var promise = handoff && typeof handoff.then === "function"
                ? Promise.resolve(handoff)
                : Promise.resolve({accepted: false, reason: "voice_handoff_missing"});
            var timeoutMs = config.voiceHandoffTimeoutMs;
            return new Promise(function (resolve) {
                var settled = false;
                var timer = window.setTimeout(function () {
                    if (settled) return;
                    settled = true;
                    resolve({accepted: false, reason: "voice_handoff_timeout"});
                }, timeoutMs);
                promise.then(function (value) {
                    if (settled) return;
                    settled = true;
                    window.clearTimeout(timer);
                    resolve(value);
                }, function () {
                    if (settled) return;
                    settled = true;
                    window.clearTimeout(timer);
                    resolve({accepted: false, reason: "voice_handoff_error"});
                });
            });
        }

        function keepDeferredVoice(refreshContext) {
            if (!refreshContext || !Array.isArray(refreshContext.events) || !refreshContext.events.length) {
                return;
            }
            var merged = {};
            if (deferredVoiceBatch && Array.isArray(deferredVoiceBatch.events)) {
                deferredVoiceBatch.events.forEach(function (event) {
                    if (event && Number(event.version || 0) > 0) merged[String(event.version)] = event;
                });
            }
            refreshContext.events.forEach(function (event) {
                if (event && Number(event.version || 0) > 0) merged[String(event.version)] = event;
            });
            var events = Object.keys(merged).map(function (key) { return merged[key]; });
            events.sort(function (left, right) {
                return Number(left.version) - Number(right.version);
            });
            deferredVoiceBatch = {
                version: Math.max(
                    Number(refreshContext.version || 0),
                    deferredVoiceBatch ? Number(deferredVoiceBatch.version || 0) : 0
                ),
                previousVersion: deferredVoiceBatch
                    ? deferredVoiceBatch.previousVersion
                    : Number(refreshContext.previousVersion || 0),
                events: events.slice(-100),
                eventsTruncated: refreshContext.eventsTruncated === true
                    || !!(deferredVoiceBatch && deferredVoiceBatch.eventsTruncated)
            };
        }

        function flushDeferredVoice() {
            if (!deferredVoiceBatch) return;
            var batch = deferredVoiceBatch;
            deferredVoiceBatch = null;
            if (!batch.events.length) return;
            /* Fire-and-forget on the next successful handoff: the role guard
               deduplicates already announced operations, and a failure here can
               never block the screen cursor again. */
            try {
                dispatchOperationalSignals({
                    version: batch.version,
                    events: batch.events,
                    eventsTruncated: batch.eventsTruncated,
                    relevant: true
                }, batch.previousVersion);
            } catch (error) { /* deferred voice is best-effort */ }
        }

        function retryPendingRefresh(reason, deferred) {
            applyingUpdate = false;
            if (!deferred) refreshFailures += 1;
            var delay = deferred ? 1000 : Math.min(15000, 1000 * Math.pow(2, Math.min(refreshFailures, 4)));
            refreshRetryAt = Date.now() + delay;
            dispatchWindowEvent("operational-state-refresh-deferred", {
                version: pendingVersion, reason: reason || "fragment_failed", role: screen ? screen.role : null
            });
            updateConnectionState(reason || "fragment_failed");
            schedulePendingUpdate(delay);
        }

        function applyPendingUpdate() {
            if (authEnded || !isPageActive() || pendingVersion === null || applyingUpdate) return;
            if (pendingJournalReplayRequired) return;
            if (sseSnapshotBarrierVersion > 0 && observedVersion < sseSnapshotBarrierVersion) {
                // A consistent fragment can observe commits that the open SSE
                // stream has not delivered yet. Keep the explicit journal
                // cursor at the last consumed page and wait for that range.
                return;
            }
            if (Date.now() < refreshRetryAt) {
                schedulePendingUpdate(refreshRetryAt - Date.now());
                return;
            }
            var versionToApply = pendingVersion;
            var refreshContext = {
                version: versionToApply,
                previousVersion: pendingPreviousVersion || appliedCursor(),
                screen: screen ? screen.name : null, mode: screen ? screen.mode : null, role: screen ? screen.role : null,
                events: pendingEvents.slice(), eventsTruncated: pendingEventsTruncated,
                snapshotRequired: pendingSnapshotRequired,
                snapshotUrl: pendingSnapshotUrl,
                signalsAfterApply: pendingSignalsAfterApply,
                requireStreamThroughSnapshot: pendingRequireStreamThroughSnapshot,
                foregroundReconcile: foregroundReconcileRequested, reconcileReason: foregroundReconcileReason,
                reconcileRevision: foregroundReconcileRequested ? foregroundReconcileRevision : 0,
                authoritativeResetRevision: activeAuthoritativeReset
                    ? Number(activeAuthoritativeReset.revision || 0)
                    : 0,
                pendingWorkRevision: pendingWorkRevision
            };
            refreshContext.isCurrentSnapshot = function (snapshotVersion) {
                var parsed = Number(snapshotVersion);
                if (!Number.isSafeInteger(parsed) || parsed < versionToApply) return false;
                if (refreshContext.requireStreamThroughSnapshot && parsed !== versionToApply) return false;
                if (Number(pendingVersion || 0) > parsed) return false;
                return !foregroundReconcileRequested
                    || (refreshContext.foregroundReconcile
                        && refreshContext.reconcileRevision === foregroundReconcileRevision);
            };
            var busyReason = shouldDeferPendingUpdate();
            if (busyReason) { retryPendingRefresh(busyReason, true); return; }
            applyingUpdate = true;
            var refreshOwner = ++refreshGeneration;
            updateConnectionState("fragment_refresh");
            if (manualRefreshMode) { revealRealtimeUpdateNotice(refreshContext); return; }
            if (typeof window.applyOperationalStateRefresh !== "function") {
                if (requiresCustomRefresh) { retryPendingRefresh("custom_handler_missing", false); return; }
                reloadForOperationalUpdate(versionToApply);
                return;
            }
            // Capture synchronous throws too. An exception never acknowledges a version.
            Promise.resolve().then(function () {
                return window.applyOperationalStateRefresh(refreshContext);
            }).then(function (result) {
                if (authEnded || refreshOwner !== refreshGeneration) return;
                if (result && result.deferred) { retryPendingRefresh(result.reason || "custom_deferred", !/failed|error/.test(result.reason || "")); return; }
                if (result && result.applied === true) {
                    var applied = typeof result.snapshotVersion !== "undefined"
                        ? Number(result.snapshotVersion)
                        : (typeof result.version === "undefined" ? versionToApply : Number(result.version));
                    if (!Number.isSafeInteger(applied) || applied < versionToApply) {
                        retryPendingRefresh("stale_fragment_version", false); return;
                    }
                    // A verified network-only fragment is also fresh authenticated transport.
                    // It can clear lost even if the separate realtime request has failed.
                    reportTransportSuccess({channel: "fragment", occurredAtMs: Date.now(), serverVersion: applied});
                    var snapshotAhead = refreshContext.requireStreamThroughSnapshot && applied > versionToApply;
                    if (snapshotAhead) {
                        /* Do not jump the replay cursor from H to snapshot S.
                           S may contain a cancellation/replacement not yet read
                           from SSE. Retain the H batch, consume H+1..S, then run
                           a second snapshot-bound arbitration. */
                        domAppliedVersion = Math.max(domAppliedVersion, applied);
                        observedVersion = Math.max(observedVersion, versionToApply);
                        sseServerHighWater = Math.max(sseServerHighWater, applied);
                        sseSnapshotBarrierVersion = Math.max(
                            sseSnapshotBarrierVersion,
                            applied,
                            Number(pendingVersion || 0)
                        );
                        pendingVersion = sseSnapshotBarrierVersion;
                        pendingSnapshotRequired = true;
                        pendingSignalsAfterApply = true;
                        pendingRequireStreamThroughSnapshot = true;
                        /* H has been received, but its voice work has not crossed
                           the durable handoff boundary. Keep the delivery cursor
                           before H while retaining H..S as pending work. */
                        storeOperationalVersion(appliedCursor());
                        refreshFailures = 0; refreshRetryAt = 0; lastSuccessfulTruthAt = Date.now();
                        applyingUpdate = false;
                        refreshContext.appliedVersion = applied;
                        refreshContext.cursorAppliedVersion = currentVersion;
                        refreshContext.snapshotAhead = true;
                        refreshContext.superseded = true;
                        dispatchWindowEvent("operational-state-refresh-applied", refreshContext);
                        updateConnectionState("snapshot_waiting_for_stream");
                        if (observedVersion >= sseSnapshotBarrierVersion) schedulePendingUpdate(0);
                        return;
                    }
                    function refreshIsSuperseded() {
                        if (pendingVersion > applied) return true;
                        if (foregroundReconcileRequested && (
                            !refreshContext.foregroundReconcile
                            || refreshContext.reconcileRevision !== foregroundReconcileRevision
                        )) return true;
                        if (pendingWorkRevision === refreshContext.pendingWorkRevision) return false;
                        var hasLaterEvent = pendingEvents.some(function (event) {
                            return event && Number(event.version || 0) > Number(refreshContext.version || 0);
                        });
                        return hasLaterEvent || (pendingEventsTruncated && pendingSignalsAfterApply);
                    }
                    var superseded = refreshIsSuperseded();
                    var resetForRefresh = activeAuthoritativeReset
                        && Number(activeAuthoritativeReset.revision) === Number(refreshContext.authoritativeResetRevision || 0);
                    domAppliedVersion = resetForRefresh ? applied : Math.max(domAppliedVersion, applied);
                    observedVersion = Math.max(observedVersion, applied);
                    refreshFailures = 0; refreshRetryAt = 0; lastSuccessfulTruthAt = Date.now();
                    refreshContext.appliedVersion = applied;
                    refreshContext.superseded = superseded;
                    persistDeliveryState();
                    var handoff = Promise.resolve({accepted: true, reason: "no_voice_work"});
                    /* Deltas become acknowledgeable only after the role handler
                       confirms that every current voice operation crossed its
                       durable native queue/ledger boundary. The pending event
                       batch stays in localStorage until that promise succeeds. */
                    if (!superseded && refreshContext.signalsAfterApply) {
                        handoff = pendingVoiceHandoff && pendingVoiceHandoffVersion === refreshContext.version
                            ? pendingVoiceHandoff
                            : dispatchOperationalSignals({
                                version: applied,
                                events: refreshContext.events,
                                events_truncated: refreshContext.eventsTruncated,
                                relevant: refreshContext.events.length > 0 || refreshContext.eventsTruncated
                            }, refreshContext.previousVersion);
                    }
                    if (superseded && refreshContext.signalsAfterApply) {
                        /* This snapshot was overtaken before its voice arbitration
                           began. The next refresh owns the combined current batch. */
                        applyingUpdate = false;
                        refreshContext.superseded = true;
                        persistDeliveryState();
                        dispatchWindowEvent("operational-state-refresh-applied", refreshContext);
                        schedulePendingUpdate(0);
                        return;
                    }
                    var handoffDegraded = false;
                    return withVoiceHandoffDeadline(handoff).then(function (voiceResult) {
                        if (authEnded || refreshOwner !== refreshGeneration) return;
                        var voiceAccepted = voiceResult && voiceResult.accepted === true;
                        if (!voiceAccepted) {
                            var failureAt = Date.now();
                            if (!voiceHandoffFailures) voiceHandoffWindowStart = failureAt;
                            voiceHandoffFailures += 1;
                            if (voiceHandoffFailures < config.voiceHandoffFailureLimit) {
                                /* A handoff that timed out or threw is kept for the
                                   retry instead of being re-dispatched: the role
                                   handler must not be invoked with the same batch
                                   again just because its audio bridge is stuck. */
                                var timedOut = voiceResult && (
                                    voiceResult.reason === "voice_handoff_timeout"
                                    || voiceResult.reason === "voice_handoff_error"
                                );
                                if (timedOut) {
                                    pendingVoiceHandoff = handoff;
                                    pendingVoiceHandoffVersion = refreshContext.version;
                                } else {
                                    pendingVoiceHandoff = null;
                                    pendingVoiceHandoffVersion = 0;
                                }
                                pendingSignalsAfterApply = true;
                                persistDeliveryState();
                                retryPendingRefresh("voice_handoff_failed", false);
                                return;
                            }
                            /* A handoff that never resolves (suspended AudioContext,
                               broken native bridge) must not freeze the screen
                               forever. After the bounded number of failures the
                               screen/DOM update proceeds with diagnostics, while the
                               unconfirmed voice batch is kept for the next event or
                               snapshot. The role guard still prevents a second
                               announcement of the same operation. */
                            keepDeferredVoice(refreshContext);
                            handoffDegraded = true;
                            voiceHandoffFailures = 0;
                            voiceHandoffWindowStart = 0;
                            refreshContext.voiceHandoffDegraded = true;
                            refreshContext.voiceHandoffReason = voiceResult && voiceResult.reason
                                ? String(voiceResult.reason)
                                : "voice_handoff_failed";
                            pendingVoiceHandoff = null;
                            pendingVoiceHandoffVersion = 0;
                            pendingSignalsAfterApply = false;
                            persistDeliveryState();
                            dispatchWindowEvent("operational-voice-handoff-degraded", {
                                version: refreshContext.version,
                                previousVersion: refreshContext.previousVersion,
                                failures: config.voiceHandoffFailureLimit,
                                reason: "voice_handoff_failed",
                                cause: refreshContext.voiceHandoffReason,
                                timedOut: refreshContext.voiceHandoffReason === "voice_handoff_timeout",
                                screen: screen ? screen.name : null,
                                role: screen ? screen.role : null
                            });
                            updateConnectionState("voice_handoff_degraded");
                            voiceResult = {accepted: true, degraded: true};
                        } else {
                            voiceHandoffFailures = 0;
                            voiceHandoffWindowStart = 0;
                        }
                        /* Native resetApplied is a final cursor commit, not a
                           prepare. Sequence it strictly after durable voice
                           acceptance so a crash cannot make heartbeat skip
                           uncommitted voice truth. */
                        if (resetForRefresh && activeAuthoritativeReset.nativeResetRequired) {
                            activeAuthoritativeReset.nativeResetPromise = syncNativeOperationalState(
                                "authoritative_reset_commit",
                                {authoritativeReset: true, resetVersion: activeAuthoritativeReset.targetVersion}
                            );
                            return activeAuthoritativeReset.nativeResetPromise.then(function (resetResult) {
                                if (!resetResult || resetResult.resetAccepted !== true) {
                                    pendingVoiceHandoff = null;
                                    pendingVoiceHandoffVersion = 0;
                                    pendingSignalsAfterApply = true;
                                    persistDeliveryState();
                                    retryPendingRefresh("native_reset_handoff_failed", false);
                                    return;
                                }
                                return {voiceAccepted: true, degraded: handoffDegraded};
                            });
                        }
                        return {voiceAccepted: true, degraded: handoffDegraded};
                    }).then(function (barrier) {
                        if (!barrier || barrier.voiceAccepted !== true) return;
                        if (authEnded || refreshOwner !== refreshGeneration) return;
                        if (!barrier.degraded) flushDeferredVoice();
                        var supersededNow = refreshIsSuperseded();
                        currentVersion = resetForRefresh ? applied : Math.max(appliedCursor(), applied);
                        if (!supersededNow) {
                            pendingVersion = null; pendingPreviousVersion = 0; pendingVersionSince = 0;
                            pendingEvents = []; pendingEventsTruncated = false;
                            pendingSnapshotRequired = false; pendingSnapshotUrl = ""; pendingSignalsAfterApply = false;
                            pendingRequireStreamThroughSnapshot = false;
                            pendingVoiceHandoff = null; pendingVoiceHandoffVersion = 0;
                            if (refreshContext.foregroundReconcile) clearForegroundReconcile(refreshContext.reconcileRevision);
                            sseSnapshotBarrierVersion = 0;
                            if (resetForRefresh) activeAuthoritativeReset = null;
                        } else {
                            /* Remove only the portion proven by this completed
                               handoff; later work remains owned by its revision. */
                            pendingEvents = pendingEvents.filter(function (event) {
                                return event && Number(event.version || 0) > applied;
                            });
                            pendingPreviousVersion = applied;
                            pendingVoiceHandoff = null;
                            pendingVoiceHandoffVersion = 0;
                            if (resetForRefresh) activeAuthoritativeReset = null;
                        }
                        pendingWorkRevision += 1;
                        storeOperationalVersion(currentVersion);
                        lastReconciledWakeAt = lastWakeAt;
                        applyingUpdate = false;
                        refreshContext.superseded = supersededNow;
                        dispatchWindowEvent("operational-state-refresh-applied", refreshContext);
                        updateConnectionState("dom_applied");
                        if (supersededNow) schedulePendingUpdate(0);
                        else {
                            activateSsePrimaryIfSynchronized();
                            maybeStartSse("snapshot_applied");
                        }
                    });
                }
                if (requiresCustomRefresh) { retryPendingRefresh("custom_refresh_not_applied", false); return; }
                reloadForOperationalUpdate(versionToApply);
            }).catch(function () {
                if (refreshOwner !== refreshGeneration) return;
                retryPendingRefresh("fragment_failed", false);
            });
        }

        function reloadForOperationalUpdate(version) {
            if (requiresCustomRefresh) {
                retryPendingRefresh("custom_handler_missing", false);
                return;
            }
            domAppliedVersion = Math.max(domAppliedVersion, Number(version || 0));
            currentVersion = Math.max(appliedCursor(), Number(version || 0));
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
            // The request cursor is the last successfully applied snapshot.
            // observedVersion may be ahead while a fragment/voice operation is pending.
            var afterVersion = appliedCursor();
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
            /* Версия возвращается вместе с адресом: она принадлежит конкретному
               запросу, а не текущему состоянию клиента, которое к моменту ответа
               может уйти вперёд. */
            return {url: url.toString(), afterVersion: afterVersion};
        }

        /* Рабочее событие уходит на озвучку раньше решения об обновлении экрана.
           Голос не должен ждать ни занятого интерфейса, ни загрузки фрагмента:
           это сигнал «смотри сюда», а экран его лишь подтверждает.

           Версия сверяется с той, с которой ушёл именно этот запрос. Поэтому
           первый ответ без «after» не проигрывает накопленную историю, а ответ,
           пришедший не по порядку, не озвучивает уже пройденное.

           Усечённую дельту пропускаем сознательно: сервер отдаёт из неё самые
           старые события, значит нужного, самого свежего, в ней может не быть. */
        function dispatchOperationalSignals(payload, requestedAfterVersion) {
            if (typeof window.handleOperationalStateSignals !== "function") {
                return Promise.resolve({accepted: true, reason: "no_handler"});
            }
            if (!(requestedAfterVersion > 0)) {
                return Promise.resolve({accepted: true, reason: "initial_truth"});
            }
            if (Number(payload.version || 0) <= requestedAfterVersion) {
                return Promise.resolve({accepted: true, reason: "not_newer"});
            }
            if (payload.relevant === false || payload.events_truncated === true) {
                /* A truncated delta cannot safely name the current operation;
                   the authoritative fragment/DOM truth owns that recovery. */
                return Promise.resolve({accepted: true, reason: "not_dispatchable"});
            }
            if (!Array.isArray(payload.events) || !payload.events.length) {
                return Promise.resolve({accepted: true, reason: "no_events"});
            }
            var freshEvents = payload.events.filter(function (event) {
                return event && Number(event.version || 0) > requestedAfterVersion;
            });
            if (!freshEvents.length) {
                return Promise.resolve({accepted: true, reason: "no_fresh_events"});
            }
            var pending;
            try {
                pending = window.handleOperationalStateSignals({
                    events: freshEvents,
                    stateVersion: Number(payload.version || 0),
                    requestedAfterVersion: requestedAfterVersion,
                    screen: screen ? screen.name : null,
                    role: screen ? screen.role : null
                });
            } catch (error) {
                return Promise.resolve({accepted: false, reason: "voice_handler_throw"});
            }
            return Promise.resolve(pending).then(function (result) {
                if (result === true) return {accepted: true, reason: "accepted"};
                if (result && result.accepted === true) return result;
                return {accepted: false, reason: result && result.reason ? String(result.reason) : "voice_not_accepted"};
            }, function () {
                return {accepted: false, reason: "voice_handler_rejected"};
            });
        }

        function clearSseReconnect() {
            if (sseReconnectTimer !== null) {
                window.clearTimeout(sseReconnectTimer);
                sseReconnectTimer = null;
            }
        }

        function clearSseCatchupSettle() {
            if (sseCatchupSettleTimer !== null) {
                window.clearTimeout(sseCatchupSettleTimer);
                sseCatchupSettleTimer = null;
            }
        }

        function closeSse(reason, scheduleReconnect) {
            clearSseReconnect();
            clearSseCatchupSettle();
            sseGeneration += 1;
            if (sseSource) {
                try { sseSource.close(); } catch (error) {}
            }
            sseSource = null;
            sseConnectStartedAt = 0;
            ssePrimaryActive = false;
            sseCatchupEvents = [];
            sseCatchupTruncated = false;
            sseState = sseEligible ? "standby" : "disabled";
            document.body.dataset.realtimeTransport = "poll";
            if (scheduleReconnect && sseEligible && !authEnded && isPageActive()) {
                scheduleSseReconnect(reason || "closed");
            }
        }

        function scheduleSseReconnect(reason, options) {
            options = options || {};
            if (!sseEligible || authEnded || roleInactive || !isPageActive() || sseReconnectTimer !== null) return;
            var exponent = Math.min(sseFailureCount, 5);
            var base = Math.min(config.sseReconnectMaxMs, 1000 * Math.pow(2, exponent));
            /* The watchdog re-arms a stream whose retry timer was lost (background
               pause, pagehide, failed reconnect scheduling). That is not a storm:
               the next failure schedules the ordinary exponential retry again. */
            if (options.immediate === true) base = 1000;
            var jitter = Math.floor(base * (0.15 + Math.random() * 0.2));
            sseState = "fallback";
            document.body.dataset.realtimeTransport = "poll";
            sseReconnectTimer = window.setTimeout(function () {
                sseReconnectTimer = null;
                connectSse(reason || "retry");
            }, Math.min(config.sseReconnectMaxMs, base + jitter));
        }

        function ssePayload(event) {
            try {
                var parsed = JSON.parse(String(event && event.data || "{}"));
                return parsed && typeof parsed === "object" ? parsed : null;
            } catch (error) {
                return null;
            }
        }

        function recordSseData(payload, reason) {
            var now = Date.now();
            sseLastDataAt = now;
            realtimeLastSuccessAt = Math.max(realtimeLastSuccessAt, now);
            realtimeConsecutiveFailures = 0;
            transportInterrupted = false;
            acceptDeliveryContext(payload);
            applyActiveRoleState(payload);
            updateConnectionState(reason || "sse_data");
        }

        function activateSsePrimaryIfSynchronized() {
            if (sseState !== "open" || ssePrimaryActive || authEnded) return false;
            if (pendingVersion !== null || applyingUpdate || foregroundReconcileRequested) return false;
            if (appliedCursor() < Number(sseServerHighWater || 0)) return false;
            ssePrimaryActive = true;
            document.body.dataset.realtimeTransport = "sse";
            clearPollSchedule();
            cancelCurrentPoll("sse_handoff_complete", false);
            updateConnectionState("sse_handoff_complete");
            syncNativeOperationalState("sse_handoff_complete");
            return true;
        }

        function finalizeSseCatchup() {
            sseCatchupSettleTimer = null;
            if (authEnded || sseState !== "open") return;
            var byVersion = {};
            sseCatchupEvents.forEach(function (item) {
                if (item && Number(item.version) > 0) byVersion[String(item.version)] = item;
            });
            var events = Object.keys(byVersion).map(function (key) { return byVersion[key]; });
            events.sort(function (left, right) { return Number(left.version) - Number(right.version); });
            var targetVersion = Number(sseServerHighWater || observedVersion || appliedCursor());
            var payload = {
                version: targetVersion,
                events: events,
                events_truncated: sseCatchupTruncated,
                relevant: events.length > 0 || sseCatchupTruncated,
                snapshot_required: true,
                defer_signals_until_apply: true,
                require_stream_through_snapshot: true
            };
            var requestedAfterVersion = appliedCursor();
            sseCatchupEvents = [];
            sseCatchupTruncated = false;
            if (payload.relevant === false && pendingVersion === null && !foregroundReconcileRequested) {
                markOperationalStateApplied(targetVersion);
                activateSsePrimaryIfSynchronized();
                return;
            }
            queueReconcileVersion(targetVersion, payload);
            applyPendingUpdate();
        }

        function enterSseFallback(reason, reconcile) {
            if (!sseEligible || authEnded) return;
            closeSse(reason || "sse_error", false);
            sseFailureCount += 1;
            transportInterrupted = true;
            if (reconcile !== false) requestForegroundReconcile(reason || "sse_error");
            publishRealtimeConnectionState("weak", {reason: reason || "sse_error", transport: "poll"});
            pollOperationalState({force: true, reconcile: reconcile !== false, reconcileReason: reason || "sse_error"});
            scheduleNextPoll();
            scheduleSseReconnect(reason || "sse_error");
        }

        function handleSseOperational(event, generation) {
            if (generation !== sseGeneration || authEnded) return;
            var payload = ssePayload(event);
            if (!payload || !Number.isSafeInteger(Number(payload.version)) || Number(payload.version) < 0) {
                enterSseFallback("sse_invalid_payload", true);
                return;
            }
            payload.version = Number(payload.version);
            pendingJournalReplayRequired = false;
            recordSseData(payload, "sse_operational");
            observedVersion = Math.max(observedVersion, payload.version);
            var highWater = Number(
                payload.high_water_version
                || (payload.journal && payload.journal.max_version)
                || payload.version
            );
            if (Number.isSafeInteger(highWater) && highWater >= 0) {
                sseServerHighWater = Math.max(sseServerHighWater, highWater);
            }
            if (Array.isArray(payload.events)) {
                sseCatchupEvents = sseCatchupEvents.concat(payload.events).slice(-500);
                if (sseCatchupEvents.length >= 500) sseCatchupTruncated = true;
            }
            sseCatchupTruncated = sseCatchupTruncated || payload.events_truncated === true;
            clearSseCatchupSettle();
            var caughtUp = payload.caught_up === true
                || payload.page_final === true
                || payload.version >= highWater;
            if (caughtUp) {
                // Coalesce adjacent pages/commits. Voice is selected only from the
                // final combined batch, so a later cancel/replacement wins.
                sseCatchupSettleTimer = window.setTimeout(finalizeSseCatchup, 150);
            }
        }

        function handleSseHeartbeat(event, generation) {
            if (generation !== sseGeneration || authEnded) return;
            var payload = ssePayload(event);
            if (!payload) {
                enterSseFallback("sse_invalid_heartbeat", true);
                return;
            }
            recordSseData(payload, "sse_heartbeat");
            var serverVersion = Number(payload.version || 0);
            if (Number.isSafeInteger(serverVersion) && serverVersion >= 0) {
                sseServerHighWater = Math.max(sseServerHighWater, serverVersion);
            }
            if (serverVersion > appliedCursor()) {
                // A heartbeat is only a high-water hint. Reconnect/catch up from
                // the applied cursor instead of acknowledging this number.
                wakeHintVersion = Math.max(wakeHintVersion, serverVersion);
            }
            activateSsePrimaryIfSynchronized();
            if (!ssePrimaryActive && pendingVersion !== null && !applyingUpdate) {
                /* A poll that failed during the outage queued its observed
                   version. Apply the screen fragment now so the recovered
                   stream can become primary without waiting for the periodic
                   truth refresh. */
                applyPendingUpdate();
            }
        }

        function beginAuthoritativeReset(payload, reason, resetVersion) {
            refreshGeneration += 1;
            applyingUpdate = false;
            cancelCurrentPoll("authoritative_reset", false);
            clearPendingUpdateSchedule();
            pendingEvents = [];
            pendingEventsTruncated = true;
            pendingSnapshotRequired = true;
            pendingSnapshotUrl = String(payload.snapshot_url || "");
            pendingSignalsAfterApply = true;
            pendingRequireStreamThroughSnapshot = false;
            pendingPreviousVersion = resetVersion;
            pendingVersion = resetVersion;
            pendingVersionSince = Date.now();
            sseSnapshotBarrierVersion = 0;
            sseServerHighWater = resetVersion;
            wakeHintVersion = resetVersion;
            currentVersion = resetVersion;
            observedVersion = resetVersion;
            var bridge = window.NativeBackgroundConnection;
            activeAuthoritativeReset = {
                revision: ++authoritativeResetRevision,
                targetVersion: resetVersion,
                contextKey: deliveryContextKey,
                journalEpoch: journalEpoch,
                nativeResetRequired: !!(bridge && typeof bridge.syncOperationalState === "function"),
                nativeResetPromise: null
            };
            persistDeliveryState();
            requestForegroundReconcile(reason);
        }

        function handleSseReset(event, generation) {
            if (generation !== sseGeneration || authEnded) return;
            var payload = ssePayload(event) || {};
            var reason = String(payload.reason || "sse_reset");
            if (reason === "authentication_ended" || payload.authenticated === false) {
                // EventSource hides HTTP status on reconnect. Confirm through the
                // existing credentialed JSON endpoint before redirecting.
                closeSse(reason, false);
                pollOperationalState({force: true});
                return;
            }
            var hasVersion = Object.prototype.hasOwnProperty.call(payload, "version");
            var resetVersion = hasVersion ? Number(payload.version) : appliedCursor();
            if (!Number.isSafeInteger(resetVersion) || resetVersion < 0) {
                enterSseFallback("sse_invalid_reset", true);
                return;
            }
            var incoming = normalizeDeliveryContext(payload);
            var hasExplicitContext = !!(
                payload.context
                && (
                    payload.context.cursor_scope
                    || payload.context.access_id
                    || payload.context.employee_id
                )
            );
            if (deliveryContextKey && hasExplicitContext && incoming.key !== deliveryContextKey) {
                /* Never let a delayed response for a previous login/equipment
                   lower the active cursor. A credentialed JSON poll resolves
                   the new context instead. */
                enterSseFallback("sse_reset_context_mismatch", true);
                return;
            }
            recordSseData(payload, "sse_reset");
            closeSse(reason, false);
            beginAuthoritativeReset(payload, reason, resetVersion);
            applyPendingUpdate();
            scheduleNextPoll();
        }

        function connectSse(reason) {
            if (!sseEligible || authEnded || !isPageActive() || sseSource || sseState === "connecting") return;
            clearSseReconnect();
            var generation = ++sseGeneration;
            var url = new URL(config.sseUrl, window.location.origin);
            // Always send our explicit applied cursor. Backend gives this query
            // priority over EventSource's received-only Last-Event-ID header.
            var streamCursor = String(appliedCursor());
            url.searchParams.set("applied", streamCursor);
            url.searchParams.set("after", streamCursor);
            var roleAppCode = String(document.body.dataset.appRoleCode || "");
            if (roleAppCode) url.searchParams.set("role_app_code", roleAppCode);
            sseState = "connecting";
            sseConnectStartedAt = Date.now();
            document.body.dataset.realtimeTransport = "sse-connecting";
            var source;
            try { source = new window.EventSource(url.toString(), {withCredentials: true}); }
            catch (error) { enterSseFallback("sse_constructor", true); return; }
            sseSource = source;
            source.onopen = function () {
                if (generation !== sseGeneration || authEnded) return;
                sseState = "open";
                sseFailureCount = 0;
                sseLastDataAt = Date.now();
                document.body.dataset.realtimeTransport = "sse-syncing";
                updateConnectionState(reason || "sse_open");
            };
            source.addEventListener("operational", function (event) { handleSseOperational(event, generation); });
            source.addEventListener("heartbeat", function (event) { handleSseHeartbeat(event, generation); });
            source.addEventListener("reset", function (event) { handleSseReset(event, generation); });
            source.onerror = function () {
                if (generation !== sseGeneration || authEnded) return;
                enterSseFallback("sse_error", true);
            };
        }

        function maybeStartSse(reason) {
            if (!sseEligible || authEnded || !isPageActive()) return;
            if (sseSource || sseState === "connecting") return;
            connectSse(reason || "poll_sync_complete");
        }

        function pollOperationalState(options) {
            options = options || {};
            if (options.reconcile === true) requestForegroundReconcile(options.reconcileReason || "foreground");
            if (authEnded || roleInactive || !isPageActive()) return;
            // force means prompt catch-up, never replacing a healthy in-flight owner.
            if (realtimePollInFlight) return;
            if (navigator && navigator.onLine === false) {
                transportInterrupted = true;
                requestForegroundReconcile("offline");
                if (connectionState === "unknown" || connectionState === "ok") publishRealtimeConnectionState("weak", {reason: "offline"});
                inspectRealtimeWatchdog();
                scheduleNextPoll();
                return;
            }
            clearPollSchedule();
            var pollGeneration = ++realtimePollGeneration;
            var pollRequest = buildOperationalStatePollUrl();
            var capturedAfterVersion = Number(pollRequest.afterVersion || 0);
            var requestDeliveryReplay = false;
            realtimePollInFlight = true;
            realtimeLastPollStartedAt = Date.now();
            var pollController = window.AbortController ? new AbortController() : null;
            realtimePollController = pollController;
            var timeoutId = window.setTimeout(function () {
                if (pollGeneration !== realtimePollGeneration) return;
                cancelCurrentPoll("timeout", true);
                scheduleNextPoll();
            }, realtimePollTimeoutMs);
            pollTimeoutId = timeoutId;
            var fetchOptions = {
                method: "GET", credentials: "same-origin", cache: "no-store", headers: {"Accept": "application/json"}
            };
            if (pollController) fetchOptions.signal = pollController.signal;
            var failureReason = "network_error";
            var requestPromise;
            try { requestPromise = window.fetch(pollRequest.url, fetchOptions); }
            catch (error) { requestPromise = Promise.reject(error); }
            Promise.resolve(requestPromise).then(function (response) {
                if (pollGeneration !== realtimePollGeneration) return null;
                if (response.status === 401) { terminateAuthentication(); return null; }
                if (!response.ok) { failureReason = "http_" + Number(response.status || 0); throw new Error(failureReason); }
                var contentType = response.headers && response.headers.get("Content-Type");
                if (response.redirected || (response.headers && !/application\/json/i.test(contentType || ""))) {
                    failureReason = "invalid_payload"; throw new Error(failureReason);
                }
                failureReason = "invalid_payload";
                return response.json();
            }).then(function (payload) {
                if (authEnded || pollGeneration !== realtimePollGeneration) return;
                if (!payload || !Number.isSafeInteger(payload.version) || payload.version < 0) {
                    failureReason = "invalid_payload"; throw new Error(failureReason);
                }
                acceptDeliveryContext(payload);
                var hadFailure = realtimeConsecutiveFailures > 0 || connectionState === "lost" || connectionState === "weak";
                realtimeConsecutiveFailures = 0;
                transportInterrupted = false;
                realtimeLastSuccessAt = Date.now();
                if (hadFailure) requestForegroundReconcile("transport_recovered");
                refreshRetryAt = hadFailure ? 0 : refreshRetryAt;
                try {
                    applyActiveRoleState(payload);
                    if (window.AppPwaContractGuard && typeof window.AppPwaContractGuard.acceptServerContract === "function") {
                        window.AppPwaContractGuard.acceptServerContract(payload);
                    }
                } catch (error) {
                    // A UI/contract callback failure is not a failed HTTP request.
                    requestForegroundReconcile("role_state_apply_failed");
                }
                if (payload.role_active === false) {
                    /* Server truth says this session is read-only. Nothing from
                       this payload may be treated as applied. */
                    markRoleInactive("state_role_context_inactive");
                    return;
                }
                clearRoleInactive();
                var version = payload.version;
                observedVersion = Math.max(observedVersion, version);
                if (pendingJournalReplayRequired && capturedAfterVersion > appliedCursor()) {
                    /* This request was built from fresh HTML before the scoped
                       durable cursor was restored. Repeat from that cursor before
                       applying or acknowledging any persisted voice work. */
                    payload.defer_signals_until_apply = true;
                    queueReconcileVersion(observedVersion, payload);
                    requestDeliveryReplay = true;
                    updateConnectionState("delivery_replay_required");
                    return;
                }
                if (pendingJournalReplayRequired) {
                    pendingJournalReplayRequired = false;
                    persistDeliveryState();
                }
                if (payload.relevant !== false && Array.isArray(payload.events) && payload.events.length) {
                    /* Polling keeps its prompt voice path even while the UI is
                       busy. The returned durable-handoff promise is retained,
                       though, and the shared cursor cannot advance until both
                       this promise and the truth fragment have completed. */
                    pendingVoiceHandoffVersion = version;
                    pendingVoiceHandoff = dispatchOperationalSignals(
                        payload,
                        Math.max(capturedAfterVersion, appliedCursor())
                    );
                    payload.defer_signals_until_apply = true;
                }
                if (currentVersion === null && !foregroundReconcileRequested) {
                    if (foregroundReconcileEnabled) requestForegroundReconcile("initial_server_truth");
                    else currentVersion = version;
                }
                if (foregroundReconcileRequested || pendingVersion !== null || (version > appliedCursor() && payload.relevant !== false)) {
                    queueReconcileVersion(observedVersion, payload);
                    updateConnectionState("transport_success");
                    applyPendingUpdate();
                } else if (version > appliedCursor()) {
                    var previousVersion = appliedCursor();
                    // Only an irrelevant delta without older unapplied work is safe to acknowledge.
                    markOperationalStateApplied(version);
                    dispatchWindowEvent("operational-state-refresh-skipped", {
                        version: version, previousVersion: previousVersion, reason: "irrelevant",
                        screen: screen ? screen.name : null, mode: screen ? screen.mode : null, role: screen ? screen.role : null
                    });
                } else updateConnectionState("transport_success");
                maybeStartSse("poll_sync_complete");
            }).catch(function () {
                if (authEnded || pollGeneration !== realtimePollGeneration) return;
                recordPollFailure(failureReason);
            }).finally(function () {
                window.clearTimeout(timeoutId);
                if (authEnded || pollGeneration !== realtimePollGeneration) return;
                realtimePollInFlight = false;
                realtimePollController = null;
                pollTimeoutId = null;
                scheduleNextPoll();
                if (requestDeliveryReplay) {
                    wakeRealtimeConnection("delivery_replay", {explicit: true});
                }
            });
        }

        function wakeRealtimeConnection(reason, options) {
            options = options || {};
            var withinBurst = lastWakeAt && Date.now() - lastWakeAt < 750;
            var burstAlreadyReconciled = withinBurst && !foregroundReconcileRequested && pendingVersion === null
                && lastReconciledWakeAt === lastWakeAt;
            if (options.reconcile === true && !burstAlreadyReconciled) {
                requestForegroundReconcile(options.reconcileReason || reason || "foreground");
            }
            if (authEnded || !isPageActive()) return;
            if (ssePrimaryActive && !options.explicit) {
                if (options.reconcile === true) {
                    queueReconcileVersion(currentVersion || observedVersion, null);
                    applyPendingUpdate();
                }
                return;
            }
            if (realtimePollInFlight || wakeTimeoutId !== null) return;
            // Lifecycle signals can arrive after the first HTTP response. Reuse that fresh
            // result and its pending fragment instead of starting another poll/abort cycle.
            if (withinBurst && !options.explicit) {
                if (foregroundReconcileRequested && realtimeLastSuccessAt >= lastWakeAt) {
                    queueReconcileVersion(observedVersion, null);
                    applyPendingUpdate();
                }
                return;
            }
            clearPollSchedule();
            wakeTimeoutId = window.setTimeout(function () {
                wakeTimeoutId = null;
                if (authEnded || !isPageActive()) return;
                lastWakeAt = Date.now(); lastWakeRevision = foregroundReconcileRevision;
                pollOperationalState({reconcile: foregroundReconcileRequested, reconcileReason: foregroundReconcileReason || reason || "wake"});
            }, 0);
        }

        function getDebugState() {
            return {
                isWatching: true,
                screen: screen,
                mode: screen ? screen.mode : null,
                currentVersion: currentVersion,
                observedVersion: observedVersion,
                appliedVersion: appliedCursor(),
                domAppliedVersion: domAppliedVersion,
                pendingReconcileVersion: pendingVersion,
                connectionState: connectionState,
                pollGeneration: realtimePollGeneration,
                nativeLastSuccessAt: lastNativeSuccessAt,
                webHeartbeatLastSuccessAt: auxiliarySuccessAt.web_heartbeat,
                fragmentLastSuccessAt: auxiliarySuccessAt.fragment,
                pendingOutboxCount: pendingOutboxCount,
                refreshFailures: refreshFailures,
                refreshRetryAt: refreshRetryAt,
                pendingVersion: pendingVersion,
                pendingPreviousVersion: pendingPreviousVersion,
                pendingVersionSince: pendingVersionSince,
                pendingEvents: pendingEvents.slice(),
                pendingEventsTruncated: pendingEventsTruncated,
                pendingWorkRevision: pendingWorkRevision,
                pendingJournalReplayRequired: pendingJournalReplayRequired,
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
                foregroundReconcileEnabled: foregroundReconcileEnabled,
                foregroundReconcileRequested: foregroundReconcileRequested,
                foregroundReconcileReason: foregroundReconcileReason,
                foregroundReconcileRevision: foregroundReconcileRevision,
                authEnded: authEnded,
                wakeHintVersion: wakeHintVersion,
                deliveryContextKey: deliveryContextKey,
                journalEpoch: journalEpoch,
                sseEligible: sseEligible,
                sseState: sseState,
                ssePrimaryActive: ssePrimaryActive,
                sseLastDataAt: sseLastDataAt,
                sseFailureCount: sseFailureCount,
                sseConnectStartedAt: sseConnectStartedAt,
                voiceHandoffFailures: voiceHandoffFailures,
                voiceHandoffWindowStart: voiceHandoffWindowStart,
                deferredVoiceVersion: deferredVoiceBatch ? deferredVoiceBatch.version : 0
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
            requestReconcile: requestReconcile,
            reportTransportSuccess: reportTransportSuccess,
            observeVersion: function (version, options) { return requestReconcile(options && options.reason, version); },
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

        publishRealtimeConnectionState("unknown", {reason: "initial"});
        if (window.AppSessionHeartbeat && Number(window.AppSessionHeartbeat.lastSuccessAtMs) > 0) {
            reportTransportSuccess({channel: "web_heartbeat", occurredAtMs: window.AppSessionHeartbeat.lastSuccessAtMs});
        }
        if (isPageActive()) {
            pollOperationalState({force: true});
            scheduleNextPoll();
        }
        watchdogIntervalId = window.setInterval(function () {
            inspectRealtimeWatchdog("slow_watchdog");
        }, 2000);
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) {
                if (isNativeShell()) {
                    pagePaused = false;
                    schedulePendingUpdate(0);
                    pollOperationalState({force: true});
                    scheduleNextPoll();
                    return;
                }
                pauseRealtimeConnection("visibility_hidden");
                return;
            }
            resumePageActivity();
            pageFocused = typeof document.hasFocus !== "function" || document.hasFocus();
            if (visibilityOnlyActivity || pageFocused) {
                wakeRealtimeConnection("visibilitychange");
            }
        });
        window.addEventListener("focus", function () {
            resumePageActivity();
            pageFocused = true;
            wakeRealtimeConnection("focus");
        });
        window.addEventListener("blur", function () {
            pageFocused = false;
            if (isNativeShell()) {
                scheduleNextPoll();
                return;
            }
            if (visibilityOnlyActivity && document.hidden !== true) {
                scheduleNextPoll();
                return;
            }
            pauseRealtimeConnection("window_blur");
        });
        window.addEventListener("pagehide", function () {
            pageFocused = false;
            if (isNativeShell()) return;
            pauseRealtimeConnection("pagehide");
        });
        window.addEventListener("pageshow", function (event) {
            resumePageActivity();
            pageFocused = typeof document.hasFocus !== "function" || document.hasFocus();
            var persisted = !!(event && event.persisted);
            wakeRealtimeConnection(persisted ? "pageshow_persisted" : "pageshow", {
                reconcile: true,
                reconcileReason: persisted ? "pageshow_persisted" : ""
            });
        });
        document.addEventListener("resume", function () {
            resumePageActivity();
            pageFocused = true;
            wakeRealtimeConnection("resume", {reconcile: true, reconcileReason: "resume"});
        });
        window.addEventListener("resume", function () {
            resumePageActivity();
            pageFocused = true;
            wakeRealtimeConnection("window_resume", {reconcile: true, reconcileReason: "window_resume"});
        });
        /* The Android startup cover deliberately takes native focus away from
           the WebView until its first stable frame is committed. WebView may
           not emit a DOM focus event when that cover disappears, so consume
           the native signal as a foreground catch-up as well. Browser pages
           never dispatch this event and keep their ordinary focus contract. */
        window.addEventListener("native-connectivity-resume", function () {
            resumePageActivity();
            pageFocused = true;
            wakeRealtimeConnection("native_connectivity_resume", {
                reconcile: true,
                reconcileReason: "native_connectivity_resume"
            });
        });
        window.addEventListener("web-heartbeat-success", function (event) {
            var detail = event && event.detail || {};
            if (detail.source !== "application-session-heartbeat" || detail.status !== 204) return;
            reportTransportSuccess({channel: "web_heartbeat", occurredAtMs: detail.occurredAtMs});
        });
        window.addEventListener("native-connection-state", function (event) {
            if (authEnded) return;
            var detail = event && event.detail || {};
            var successAt = Number(detail.lastSuccessAtMs || 0);
            var occurredAt = Number(detail.occurredAtMs || 0);
            var now = Date.now();
            // Only fresh, monotonic evidence from an authenticated heartbeat is a success.
            if (detail.status !== "success" || !Number.isFinite(successAt) || !Number.isFinite(occurredAt)
                || successAt <= lastNativeSuccessAt || successAt <= 0 || now - successAt > 15000
                || successAt > now + 1000 || occurredAt < successAt || occurredAt > now + 1000) return;
            var needsRecovery = connectionState === "lost" || realtimeConsecutiveFailures > 0 || hasPendingReconcile();
            lastNativeSuccessAt = successAt;
            transportInterrupted = false;
            realtimeLastSuccessAt = Math.max(realtimeLastSuccessAt, successAt);
            realtimeConsecutiveFailures = 0;
            if (detail.operationalResetRequired === true) {
                // A cursor-ahead reset can move the authoritative version down,
                // which the ordinary monotonic delta reconciler must never fake.
                // Reloading this rare reset path obtains a fresh role snapshot and
                // initial cursor while keeping the authenticated Capacitor session.
                requestForegroundReconcile(
                    "native_operational_reset_" + String(detail.operationalResetReason || "unknown")
                );
                if (window.location && typeof window.location.reload === "function") {
                    window.location.reload();
                }
                return;
            }
            if (needsRecovery) {
                refreshRetryAt = 0;
                requestReconcile("native_heartbeat_success", detail.serverVersion);
            } else if (Number(detail.serverVersion) > observedVersion) {
                // Native version is global. Let the role-addressed poll decide relevance
                // instead of turning unrelated heartbeats into extra fragment refreshes.
                wakeHintVersion = Math.max(wakeHintVersion, Number(detail.serverVersion));
                wakeRealtimeConnection("native_version");
            }
            updateConnectionState("native_heartbeat_success");
        });
        window.addEventListener("operational-outbox-state", function (event) {
            var count = Number(event && event.detail && event.detail.pendingCount);
            if (!Number.isFinite(count) || count < 0) return;
            var previous = pendingOutboxCount;
            pendingOutboxCount = count;
            if (previous > 0 && count === 0) requestReconcile("outbox_drained", observedVersion);
            updateConnectionState("outbox_state");
        });
        window.addEventListener("online", function () {
            if (authEnded) return;
            wakeRealtimeConnection("online", {reconcile: true});
        });
        window.addEventListener("offline", function () {
            if (authEnded) return;
            transportInterrupted = true;
            requestForegroundReconcile("offline");
            if (isPageActive()) publishRealtimeConnectionState("weak", {reason: "offline"});
        });
        ["pointerdown", "pointerup", "touchstart", "touchend", "mousedown", "click", "keydown"].forEach(function (eventName) {
            document.addEventListener(eventName, function () {
                var silentTooLong = !!realtimeLastSuccessAt && Date.now() - realtimeLastSuccessAt > realtimeMaxSilentMs;
                if (!realtimeLastSuccessAt || silentTooLong) {
                    wakeRealtimeConnection(eventName, {
                        reconcile: silentTooLong,
                        reconcileReason: silentTooLong ? "silent_interaction" : ""
                    });
                }
            }, {passive: true});
        });
    }

    document.addEventListener("DOMContentLoaded", initRealtimeClient);
})(window, document);
