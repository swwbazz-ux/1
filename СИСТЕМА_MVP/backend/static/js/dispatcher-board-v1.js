/* Dispatcher desktop board.
   Owns action policy and board-layout coordination.
   Local DOM mutations, transport, realtime reconciliation and detail rendering are injected. */
(function (global, document) {
    "use strict";

    function createDispatcherBoard(options) {
        options = options || {};
        if (typeof global.createDispatcherBoardDnD !== "function" ||
            typeof global.createDispatcherBoardMutations !== "function") {
            throw new Error("Dispatcher board interaction modules are not loaded");
        }
        var dispatcherPost = options.post;
        var dispatcherMoveExcavatorUrl = options.moveExcavatorUrl || "";
        var dispatcherAssignTruckUrl = options.assignTruckUrl || "";
        var showDispatcherDnDError = options.showError || function () {};
        var reloadDispatcherBoardAsFallback = options.reloadFallback || function () {};
        var openEquipmentCard = options.openEquipmentCard || function () { return false; };
        var dispatcherEquipmentStateClass = options.equipmentStateClass;
        var dispatcherEquipmentStateLabel = options.equipmentStateLabel;
        var dispatcherNeutralEquipmentIcon = options.neutralEquipmentIcon;
        var setDispatcherNodeEquipmentState = options.setNodeEquipmentState;
        var rebindEquipmentSearch = options.rebindEquipmentSearch || function () {};
        var complexTruckRacks = options.complexTruckRacks || {};
        var dispatcherHaulAssignmentState = options.assignmentState || {};
        var haulAssignmentStateId = typeof dispatcherHaulAssignmentState.getStateId === "function"
            ? dispatcherHaulAssignmentState.getStateId
            : function () { return "0"; };
        var collectComplexAssignmentStates = typeof dispatcherHaulAssignmentState.collectComplexStates === "function"
            ? dispatcherHaulAssignmentState.collectComplexStates
            : function () { return {}; };
        var applyHaulAssignmentState = typeof dispatcherHaulAssignmentState.applyState === "function"
            ? dispatcherHaulAssignmentState.applyState
            : function () {};
        var applyHaulAssignmentStates = typeof dispatcherHaulAssignmentState.applyStates === "function"
            ? dispatcherHaulAssignmentState.applyStates
            : function () {};
        var dispatcherMutations = null;
        var dispatcherDnD = null;
        var moveDesktopTruckToGarage = function () { return false; };
        var moveDesktopTruckToComplex = function () { return false; };
        var releaseDesktopComplexTrucks = function () { return false; };
        var moveDesktopComplexToExcavatorGarage = function () { return false; };
        var activateDesktopComplexFromExcavatorTile = function () { return false; };
        function bindDispatcherDragTile(tile) {
            if (!dispatcherDnD || typeof dispatcherDnD.bindDragTile !== "function") return;
            dispatcherDnD.bindDragTile(tile);
        }
        function dispatcherShiftIsOpen() {
            return typeof options.getShiftOpen === "function"
                ? Boolean(options.getShiftOpen())
                : false;
        }
        function refreshDispatcherDesktopBoardFromServer(refreshOptions) {
            return options.refreshBoardFromServer(refreshOptions);
        }
        var board = document.querySelector(".dispatcher-board");
        var excavatorGarage = document.querySelector("[data-dispatcher-excavator-garage]");
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
        var refreshComplexTruckRack = typeof complexTruckRacks.refreshRack === "function"
            ? complexTruckRacks.refreshRack
            : function () {};
        var refreshAllComplexTruckRacks = typeof complexTruckRacks.refreshAll === "function"
            ? complexTruckRacks.refreshAll
            : function () {};
        function refreshDesktopBoardIntegrity() {
            if (dispatcherMutations) dispatcherMutations.reconcileTruckUniqueness();
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
        function refreshDesktopBoardAfterStructuralAction(response, localFallback) {
            if (response && response.queued) {
                if (typeof localFallback === "function") {
                    localFallback();
                }
                return response;
            }
            return refreshDispatcherDesktopBoardFromServer().then(function (applied) {
                if (!applied) {
                    if (typeof localFallback === "function") {
                        localFallback();
                    }
                }
                return response;
            }).catch(function (error) {
                if (typeof localFallback === "function") {
                    localFallback();
                } else {
                    throw error;
                }
                return response;
            });
        }
        function handleDesktopOptimisticBoardError(error) {
            showDispatcherDnDError(error);
            return refreshDispatcherDesktopBoardFromServer().catch(function () {
                /* Первичная ошибка уже показана. При отсутствии сети повторный fragment
                   не должен становиться необработанным Promise в drag-and-drop. */
                return null;
            });
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
            }
            return response;
        }
        refreshExcavatorGarage();
        refreshTruckGarage();
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
        dispatcherMutations = global.createDispatcherBoardMutations({
            document: document,
            window: global,
            equipmentStateClass: dispatcherEquipmentStateClass,
            equipmentStateLabel: dispatcherEquipmentStateLabel,
            neutralEquipmentIcon: dispatcherNeutralEquipmentIcon,
            setNodeEquipmentState: setDispatcherNodeEquipmentState,
            bindDragTile: bindDispatcherDragTile,
            bindEquipmentCard: bindEquipmentCardTrigger,
            refreshIntegrity: refreshDesktopBoardIntegrity,
            refreshTruckGarage: refreshTruckGarage,
            refreshExcavatorGarage: refreshExcavatorGarage,
            refreshComplexTruckRack: refreshComplexTruckRack,
            refreshComplexTruckRacks: refreshAllComplexTruckRacks,
            normalizeComplexGrid: normalizeComplexGrid
        });
        moveDesktopTruckToGarage = dispatcherMutations.moveTruckToGarage;
        moveDesktopTruckToComplex = dispatcherMutations.moveTruckToComplex;
        releaseDesktopComplexTrucks = dispatcherMutations.releaseComplexTrucks;
        moveDesktopComplexToExcavatorGarage = dispatcherMutations.moveComplexToExcavatorGarage;
        activateDesktopComplexFromExcavatorTile = dispatcherMutations.activateComplexFromExcavatorTile;
        dispatcherDnD = global.createDispatcherBoardDnD({
            document: document,
            post: dispatcherPost,
            moveExcavatorUrl: dispatcherMoveExcavatorUrl,
            assignTruckUrl: dispatcherAssignTruckUrl,
            getShiftOpen: dispatcherShiftIsOpen,
            getBoard: function () { return board; },
            getAssignmentStateId: haulAssignmentStateId,
            collectComplexAssignmentStates: collectComplexAssignmentStates,
            applyHaulAssignmentStates: applyHaulAssignmentStates,
            applyDesktopTruckAction: applyDesktopTruckAction,
            refreshDesktopBoardAfterStructuralAction: refreshDesktopBoardAfterStructuralAction,
            activateDesktopComplexFromExcavatorTile: activateDesktopComplexFromExcavatorTile,
            moveDesktopComplexToExcavatorGarage: moveDesktopComplexToExcavatorGarage,
            handleStructuralError: handleDesktopOptimisticBoardError,
            showError: showDispatcherDnDError,
            confirmDanger: requestDispatcherDesktopDangerConfirmation
        });
        function bindDispatcherDesktopInteractions() {
            board = document.querySelector(".dispatcher-board");
            excavatorGarage = document.querySelector("[data-dispatcher-excavator-garage]");
            rebindEquipmentSearch();
            dispatcherDnD.bind();
            document.querySelectorAll("[data-equipment-card-id]").forEach(bindEquipmentCardTrigger);
            normalizeComplexGrid();
            refreshExcavatorGarage();
            refreshTruckGarage();
            refreshAllComplexTruckRacks();
        }

        return {
            bindInteractions: bindDispatcherDesktopInteractions,
            resetDragSession: dispatcherDnD.resetSession,
            refreshIntegrity: refreshDesktopBoardIntegrity,
            refreshComplexTruckRacks: refreshAllComplexTruckRacks,
            sortEquipmentList: sortDesktopEquipmentList
        };
    }

    global.createDispatcherBoard = createDispatcherBoard;
})(window, document);
