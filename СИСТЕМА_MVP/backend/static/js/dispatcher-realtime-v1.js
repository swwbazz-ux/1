/* Dispatcher realtime fragment reconciliation.
   The module owns version tracking and narrow DOM reconciliation. The main
   control runtime supplies board-specific callbacks so this file can remain
   independent from equipment-card and drag-and-drop implementation details. */
(function (global) {
    "use strict";

    function createDispatcherRealtime(options) {
        options = options || {};
        var hostWindow = options.window || global;
        var hostDocument = options.document || hostWindow.document;
        var hostNavigator = options.navigator || hostWindow.navigator;
        var transport = options.transport;
        var storageKey = "operational-state-version";
        var hardLagLimit = 150;
        var syncQueueWakeThrottleMs = 1500;
        var lastSyncQueueWakeAt = 0;

        if (!hostDocument || !transport) {
            throw new Error("Dispatcher realtime dependencies are not configured");
        }
        if (typeof global.createDispatcherFragmentReconciler !== "function") {
            throw new Error("Dispatcher fragment reconciler module is not loaded");
        }
        var fragmentReconciler = global.createDispatcherFragmentReconciler({
            window: hostWindow,
            document: hostDocument,
            getEquipmentCards: options.getEquipmentCards,
            getDetailLayer: options.getDetailLayer,
            openEquipmentCard: options.openEquipmentCard
        });

        function readRenderedOperationalStateVersion() {
            var parsed = parseInt(
                hostDocument.body ? hostDocument.body.dataset.operationalStateVersion || "0" : "0",
                10
            );
            return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
        }

        function readDispatcherRealtimeVersion() {
            try {
                var raw = hostWindow.sessionStorage.getItem(storageKey);
                var parsed = parseInt(raw || "0", 10);
                return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
            } catch (error) {
                return 0;
            }
        }

        var lastVersion = readRenderedOperationalStateVersion() || readDispatcherRealtimeVersion();
        /* Direct fragment refreshes can be started by realtime, optimistic
           recovery and conflict handling at the same time. Freshness belongs
           to the server payload version, never to network response order. */
        var dispatcherDesktopRefreshGeneration = 0;
        var lastAppliedFragmentVersion = readRenderedOperationalStateVersion();
        var lastAppliedFragmentGeneration = 0;

        function storeDispatcherRealtimeVersion(version) {
            var parsed = parseInt(version || "0", 10);
            if (!Number.isFinite(parsed) || parsed <= 0) return lastVersion;
            if (lastVersion && parsed < lastVersion) return lastVersion;
            lastVersion = parsed;
            if (hostDocument.body) {
                hostDocument.body.dataset.operationalStateVersion = String(parsed);
            }
            try {
                hostWindow.sessionStorage.setItem(storageKey, String(parsed));
            } catch (error) {}
            return lastVersion;
        }

        function wakeDispatcherSyncQueueForRefresh() {
            var now = Date.now();
            if (now - lastSyncQueueWakeAt < syncQueueWakeThrottleMs) return;
            lastSyncQueueWakeAt = now;
            transport.scheduleFlush(0);
        }

        function isDispatcherSyncQueueBlockingRefresh() {
            if (transport.boardRefreshToken() !== null) return false;
            wakeDispatcherSyncQueueForRefresh();
            return true;
        }

        function isElementRendered(node) {
            if (!node) return false;
            var style = hostWindow.getComputedStyle(node);
            if (!style || style.display === "none" || style.visibility === "hidden") return false;
            return node.getClientRects().length > 0;
        }

        function isDispatcherDesktopPage() {
            return isElementRendered(hostDocument.querySelector(".dispatcher-board"));
        }

        function hasDispatcherRelevantEvents(events) {
            return Array.isArray(events) && events.length > 0;
        }

        function isDispatcherOperationalRefreshUnsafe() {
            if (!isDispatcherDesktopPage()) return false;
            var active = hostDocument.activeElement;
            var activeTag = active && active.tagName ? active.tagName.toLowerCase() : "";
            if (
                active
                && (active.isContentEditable || activeTag === "input" || activeTag === "textarea" || activeTag === "select")
            ) {
                return true;
            }
            if (isDispatcherSyncQueueBlockingRefresh()) {
                return true;
            }
            if (hostDocument.body.classList.contains("modal-open")) {
                return true;
            }
            if (hostDocument.querySelector(
                ".app-confirm-modal:not([hidden]), .dispatcher-notice-modal:not([hidden]), "
                + ".mm-mobile-update-modal:not([hidden]), [data-gd-equipment-detail]:not([hidden])"
            )) {
                return true;
            }
            if (hostDocument.querySelector(".dispatcher-dragging, .dispatcher-drop-target, .is-dragging")) {
                return true;
            }
            return false;
        }

        var captureDispatcherDesktopState = fragmentReconciler.captureState;
        var restoreDispatcherDesktopState = fragmentReconciler.restoreState;
        var reconcileDispatcherDesktopBoard = fragmentReconciler.reconcileBoard;
        var seedDispatcherBoardFingerprints = fragmentReconciler.seedBoardFingerprints;

        function refreshDispatcherDesktopBoardFromServer(refreshOptions) {
            refreshOptions = refreshOptions || {};
            if (!isDispatcherDesktopPage()) return Promise.resolve(false);
            var currentBoard = hostDocument.querySelector(".dispatcher-board");
            var desktopState = captureDispatcherDesktopState(currentBoard);
            if (!hostWindow.AppOperationalFragment) return Promise.resolve(false);
            var commandToken = transport.boardRefreshToken();
            if (commandToken === null) return Promise.resolve(false);
            var requestedVersion = Number(refreshOptions.version || 0);
            var requestGeneration = ++dispatcherDesktopRefreshGeneration;
            return hostWindow.AppOperationalFragment.request(
                "dispatcher",
                requestedVersion
            ).then(function (payload) {
                if (commandToken !== transport.boardRefreshToken()) return false;
                var payloadVersion = Number(payload && payload.version);
                if (!Number.isSafeInteger(payloadVersion) || payloadVersion < requestedVersion) {
                    return false;
                }
                var renderedBoardCoversRequest = lastAppliedFragmentVersion >= requestedVersion;
                if (payloadVersion < lastAppliedFragmentVersion || payloadVersion < lastVersion) {
                    return renderedBoardCoversRequest && lastAppliedFragmentVersion >= payloadVersion;
                }
                if (payloadVersion === lastAppliedFragmentVersion) {
                    if (!refreshOptions.forceFullBoard || requestGeneration < lastAppliedFragmentGeneration) {
                        return renderedBoardCoversRequest;
                    }
                }
                if (isDispatcherOperationalRefreshUnsafe()) return false;
                var freshBoard = hostWindow.AppOperationalFragment.parseRoot(
                    payload.html,
                    ".dispatcher-board"
                );
                currentBoard = hostDocument.querySelector(".dispatcher-board");
                if (!freshBoard || !currentBoard) return false;
                if (typeof options.syncShiftRuntime === "function") {
                    options.syncShiftRuntime(freshBoard);
                }
                if (payload.equipment_cards && typeof options.setEquipmentCards === "function") {
                    options.setEquipmentCards(payload.equipment_cards);
                }
                var refreshedBoard = refreshOptions.forceFullBoard
                    ? null
                    : reconcileDispatcherDesktopBoard(currentBoard, freshBoard);
                if (!refreshedBoard) {
                    seedDispatcherBoardFingerprints(freshBoard);
                    currentBoard.replaceWith(freshBoard);
                    refreshedBoard = freshBoard;
                }
                lastAppliedFragmentVersion = payloadVersion;
                lastAppliedFragmentGeneration = requestGeneration;
                storeDispatcherRealtimeVersion(payloadVersion);
                /* A fragment can replace the drag source without a native dragend.
                   Clear that stale session only at this replacement boundary, never
                   during an ordinary interaction rebind. */
                if (typeof options.resetBoardDragSession === "function") {
                    options.resetBoardDragSession();
                }
                if (typeof options.bindBoardInteractions === "function") {
                    options.bindBoardInteractions();
                }
                if (typeof hostWindow.initAppConfirmForms === "function") {
                    hostWindow.initAppConfirmForms();
                }
                if (typeof hostWindow.initSharedShiftLogin === "function") {
                    hostWindow.initSharedShiftLogin(document);
                }
                if (typeof hostWindow.initDispatcherThemeControls === "function") {
                    hostWindow.initDispatcherThemeControls();
                }
                if (typeof hostWindow.initDispatcherRadialClocks === "function") {
                    hostWindow.initDispatcherRadialClocks();
                }
                restoreDispatcherDesktopState(refreshedBoard, desktopState);
                if (typeof options.refreshBoardIntegrity === "function") {
                    options.refreshBoardIntegrity();
                }
                if (typeof options.updateSyncIndicator === "function") {
                    options.updateSyncIndicator();
                }
                return true;
            });
        }

        function applyDispatcherOperationalStateRefresh(context) {
            if (!isDispatcherDesktopPage()) return false;
            if (isDispatcherOperationalRefreshUnsafe()) {
                return Promise.resolve({deferred: true, reason: "dispatcher_busy"});
            }
            var targetVersion = context && context.version;
            var events = context && context.events;
            var currentStoredVersion = lastVersion || readDispatcherRealtimeVersion();
            var versionGap = targetVersion && currentStoredVersion ? targetVersion - currentStoredVersion : 0;
            if (context && context.eventsTruncated || versionGap > hardLagLimit) {
                return refreshDispatcherDesktopBoardFromServer({
                    version: targetVersion,
                    forceFullBoard: true
                }).then(function (applied) {
                    if (!applied) return {deferred: true, reason: "dispatcher_refresh_failed"};
                    return {applied: true, version: lastVersion};
                }).catch(function () {
                    return {deferred: true, reason: "dispatcher_refresh_error"};
                });
            }
            if (!hasDispatcherRelevantEvents(events)) {
                return Promise.resolve({
                    applied: true,
                    version: storeDispatcherRealtimeVersion(targetVersion)
                });
            }
            return refreshDispatcherDesktopBoardFromServer({version: targetVersion}).then(function (applied) {
                if (!applied) return {deferred: true, reason: "dispatcher_refresh_failed"};
                return {applied: true, version: lastVersion};
            }).catch(function () {
                return {deferred: true, reason: "dispatcher_refresh_error"};
            });
        }

        return {
            applyOperationalStateRefresh: applyDispatcherOperationalStateRefresh,
            isOperationalRefreshUnsafe: isDispatcherOperationalRefreshUnsafe,
            refreshBoardFromServer: refreshDispatcherDesktopBoardFromServer,
            seedBoardFingerprints: seedDispatcherBoardFingerprints
        };
    }

    global.createDispatcherRealtime = createDispatcherRealtime;
})(window);
