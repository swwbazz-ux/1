/* Dispatcher desktop board.
   Composes the desktop board interaction modules.
   Action policy, local DOM mutations, layout, transport, realtime and detail rendering are injected. */
(function (global, document) {
    "use strict";

    function createDispatcherBoard(options) {
        options = options || {};
        if (typeof global.createDispatcherBoardDnD !== "function" ||
            typeof global.createDispatcherBoardMutations !== "function" ||
            typeof global.createDispatcherBoardActions !== "function") {
            throw new Error("Dispatcher board interaction modules are not loaded");
        }
        var dispatcherPost = options.post;
        var dispatcherMoveExcavatorUrl = options.moveExcavatorUrl || "";
        var dispatcherAssignTruckUrl = options.assignTruckUrl || "";
        var showDispatcherDnDError = options.showError || function () {};
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
        var equipmentCardTrigger = options.equipmentCardTrigger;
        if (!equipmentCardTrigger || typeof equipmentCardTrigger.bind !== "function") {
            throw new Error("Dispatcher equipment card trigger is not configured");
        }
        var bindEquipmentCardTrigger = equipmentCardTrigger.bind;
        var dispatcherMutations = null;
        var dispatcherActions = null;
        var dispatcherDnD = null;
        function bindDispatcherDragTile(tile) {
            if (!dispatcherDnD || typeof dispatcherDnD.bindDragTile !== "function") return;
            dispatcherDnD.bindDragTile(tile);
        }
        function dispatcherShiftIsOpen() {
            return typeof options.getShiftOpen === "function"
                ? Boolean(options.getShiftOpen())
                : false;
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
        refreshExcavatorGarage();
        refreshTruckGarage();
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
        dispatcherActions = global.createDispatcherBoardActions({
            showError: showDispatcherDnDError,
            refreshBoardFromServer: options.refreshBoardFromServer,
            reloadFallback: options.reloadFallback,
            assignmentState: dispatcherHaulAssignmentState,
            mutations: dispatcherMutations
        });
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
            applyDesktopTruckAction: dispatcherActions.applyTruckAction,
            refreshDesktopBoardAfterStructuralAction: dispatcherActions.refreshAfterStructuralAction,
            activateDesktopComplexFromExcavatorTile: dispatcherMutations.activateComplexFromExcavatorTile,
            moveDesktopComplexToExcavatorGarage: dispatcherMutations.moveComplexToExcavatorGarage,
            handleStructuralError: dispatcherActions.handleOptimisticError,
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
