/* Dispatcher desktop board.
   Owns action policy and joins the interaction modules.
   Local DOM mutations, layout, transport, realtime reconciliation and detail rendering are injected. */
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
        var dispatcherLayout = options.layout;
        if (!dispatcherLayout ||
            typeof dispatcherLayout.sortEquipmentList !== "function" ||
            typeof dispatcherLayout.refreshTruckGarage !== "function" ||
            typeof dispatcherLayout.refreshExcavatorGarage !== "function" ||
            typeof dispatcherLayout.normalizeComplexGrid !== "function") {
            throw new Error("Dispatcher board layout module is not configured");
        }
        var sortDesktopEquipmentList = dispatcherLayout.sortEquipmentList;
        var refreshTruckGarage = dispatcherLayout.refreshTruckGarage;
        var refreshExcavatorGarage = dispatcherLayout.refreshExcavatorGarage;
        var normalizeComplexGrid = dispatcherLayout.normalizeComplexGrid;
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
