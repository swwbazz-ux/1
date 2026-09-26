/* Dispatcher fragment DOM reconciler.
   Owns state capture/restore and keyed board reconciliation only.
   Network requests, operational versions and refresh safety stay in dispatcher-realtime. */
(function (global) {
    "use strict";

    function createDispatcherFragmentReconciler(options) {
        options = options || {};
        var hostWindow = options.window || global;
        var hostDocument = options.document || hostWindow.document;

        if (!hostDocument) {
            throw new Error("Dispatcher fragment reconciler document is not configured");
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

        return {
            captureState: captureDispatcherDesktopState,
            restoreState: restoreDispatcherDesktopState,
            reconcileBoard: reconcileDispatcherDesktopBoard,
            seedBoardFingerprints: seedDispatcherBoardFingerprints
        };
    }

    global.createDispatcherFragmentReconciler = createDispatcherFragmentReconciler;
})(window);
