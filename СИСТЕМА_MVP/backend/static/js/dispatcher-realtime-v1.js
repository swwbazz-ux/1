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
        var localAssignmentAppliedUntil = 0;
        var incomingRefreshQueueGraceMs = 15000;
        var syncQueueWakeThrottleMs = 1500;
        var lastSyncQueueWakeAt = 0;

        if (!hostDocument || !transport) {
            throw new Error("Dispatcher realtime dependencies are not configured");
        }

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

        function storeDispatcherRealtimeVersion(version) {
            var parsed = parseInt(version || "0", 10);
            if (!Number.isFinite(parsed) || parsed <= 0) return;
            lastVersion = parsed;
            if (hostDocument.body) {
                hostDocument.body.dataset.operationalStateVersion = String(parsed);
            }
            try {
                hostWindow.sessionStorage.setItem(storageKey, String(parsed));
            } catch (error) {}
        }

        function wakeDispatcherSyncQueueForRefresh() {
            var now = Date.now();
            if (now - lastSyncQueueWakeAt < syncQueueWakeThrottleMs) return;
            lastSyncQueueWakeAt = now;
            transport.scheduleFlush(0);
        }

        function isDispatcherSyncQueueBlockingRefresh() {
            var syncState = transport.getQueueState();
            if (syncState.isFlushing || syncState.pendingCount > 0) {
                return true;
            }
            var queue = transport.readQueue();
            if (!queue.length) {
                return false;
            }
            wakeDispatcherSyncQueueForRefresh();
            if (hostNavigator && hostNavigator.onLine === false) {
                return true;
            }
            var now = Date.now();
            return queue.some(function (item) {
                var createdAt = Number(item && item.createdAt ? item.createdAt : 0);
                return !createdAt || now - createdAt < incomingRefreshQueueGraceMs;
            });
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

        function markDispatcherLocalAssignmentApplied() {
            localAssignmentAppliedUntil = Date.now() + 8000;
        }

        function hasDispatcherRelevantEvents(events) {
            return Array.isArray(events) && events.length > 0;
        }

        function canTrustLocalDispatcherAssignmentEvents(events) {
            if (!Array.isArray(events) || !events.length) return false;
            if (Date.now() > localAssignmentAppliedUntil) return false;
            return events.every(function (event) {
                return event && event.type === "assignment_changed";
            });
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
                scrollX: hostWindow.scrollX || 0,
                scrollY: hostWindow.scrollY || 0,
                scrolls: {},
                activeDetailCardId: "",
                detailScrollTop: 0
            };
            selectors.forEach(function (selector) {
                var node = currentBoard ? currentBoard.querySelector(selector) : hostDocument.querySelector(selector);
                if (!node) return;
                state.scrolls[selector] = {
                    top: node.scrollTop || 0,
                    left: node.scrollLeft || 0
                };
            });
            var detailLayer = options.getDetailLayer ? options.getDetailLayer() : null;
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
                var node = freshBoard ? freshBoard.querySelector(selector) : hostDocument.querySelector(selector);
                var saved = state.scrolls[selector];
                if (!node || !saved) return;
                node.scrollTop = saved.top || 0;
                node.scrollLeft = saved.left || 0;
            });
            hostWindow.scrollTo(state.scrollX || 0, state.scrollY || 0);
            var equipmentCards = options.getEquipmentCards ? options.getEquipmentCards() : {};
            if (state.activeDetailCardId && equipmentCards[String(state.activeDetailCardId || "")]) {
                if (typeof options.openEquipmentCard === "function") {
                    options.openEquipmentCard(state.activeDetailCardId);
                }
                var detailLayer = options.getDetailLayer ? options.getDetailLayer() : null;
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
                ".dispatcher-equipment-tile, .dispatcher-complex-card[data-zone-id], "
                + ".dispatcher-truck-tile[data-equipment-id]"
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
                {region: ".dispatcher-excavators", items: ".dispatcher-equipment-tile", key: "equipmentId"},
                {region: ".dispatcher-zone-grid", items: ".dispatcher-complex-card[data-zone-id]", key: "zoneId"},
                {region: ".dispatcher-trucks", items: ".dispatcher-truck-tile[data-equipment-id]", key: "equipmentId"}
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

        function refreshDispatcherDesktopBoardFromServer(refreshOptions) {
            refreshOptions = refreshOptions || {};
            if (!isDispatcherDesktopPage()) return Promise.resolve(false);
            var currentBoard = hostDocument.querySelector(".dispatcher-board");
            var desktopState = captureDispatcherDesktopState(currentBoard);
            if (!hostWindow.AppOperationalFragment) return Promise.resolve(false);
            return hostWindow.AppOperationalFragment.request(
                "dispatcher",
                Number(refreshOptions.version || 0)
            ).then(function (payload) {
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
                if (typeof options.bindBoardInteractions === "function") {
                    options.bindBoardInteractions();
                }
                if (typeof hostWindow.initAppConfirmForms === "function") {
                    hostWindow.initAppConfirmForms();
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
                    storeDispatcherRealtimeVersion(targetVersion);
                    return {applied: true};
                }).catch(function () {
                    return {deferred: true, reason: "dispatcher_refresh_error"};
                });
            }
            if (!hasDispatcherRelevantEvents(events) || canTrustLocalDispatcherAssignmentEvents(events)) {
                storeDispatcherRealtimeVersion(targetVersion);
                return Promise.resolve({applied: true});
            }
            return refreshDispatcherDesktopBoardFromServer({version: targetVersion}).then(function (applied) {
                if (!applied) return {deferred: true, reason: "dispatcher_refresh_failed"};
                storeDispatcherRealtimeVersion(targetVersion);
                return {applied: true};
            }).catch(function () {
                return {deferred: true, reason: "dispatcher_refresh_error"};
            });
        }

        return {
            applyOperationalStateRefresh: applyDispatcherOperationalStateRefresh,
            isOperationalRefreshUnsafe: isDispatcherOperationalRefreshUnsafe,
            markLocalAssignmentApplied: markDispatcherLocalAssignmentApplied,
            refreshBoardFromServer: refreshDispatcherDesktopBoardFromServer,
            seedBoardFingerprints: seedDispatcherBoardFingerprints
        };
    }

    global.createDispatcherRealtime = createDispatcherRealtime;
})(window);
