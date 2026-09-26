/* Dispatcher desktop drag session.
   Owns one drag source, its visual state and all drop targets. Board DOM moves,
   server response application and realtime reconciliation stay injected. */
(function (global, document) {
    "use strict";

    function createDispatcherBoardDnD(options) {
        options = options || {};
        var rootDocument = options.document || document;
        var dispatcherPost = options.post;
        var dispatcherMoveExcavatorUrl = options.moveExcavatorUrl || "";
        var dispatcherAssignTruckUrl = options.assignTruckUrl || "";
        var getShiftOpen = options.getShiftOpen || function () { return false; };
        var getBoard = options.getBoard || function () { return null; };
        var getAssignmentStateId = options.getAssignmentStateId || function () { return "0"; };
        var collectComplexAssignmentStates = options.collectComplexAssignmentStates || function () { return {}; };
        var applyHaulAssignmentStates = options.applyHaulAssignmentStates || function () {};
        var applyDesktopTruckAction = options.applyDesktopTruckAction || function (response) { return response; };
        var refreshDesktopBoardAfterStructuralAction = options.refreshDesktopBoardAfterStructuralAction ||
            function (response, localFallback) {
                if (typeof localFallback === "function") localFallback();
                return Promise.resolve(response);
            };
        var activateDesktopComplexFromExcavatorTile = options.activateDesktopComplexFromExcavatorTile || function () {};
        var moveDesktopComplexToExcavatorGarage = options.moveDesktopComplexToExcavatorGarage || function () {};
        var handleDesktopOptimisticBoardError = options.handleStructuralError || function () {};
        var showDispatcherDnDError = options.showError || function () {};
        var requestDispatcherDesktopDangerConfirmation = options.confirmDanger || function () {};
        var draggedTile = null;
        var dragGhost = null;

        function dispatcherShiftIsOpen() {
            return Boolean(getShiftOpen());
        }
        function clearDragGhost() {
            if (!dragGhost) return;
            dragGhost.remove();
            dragGhost = null;
        }
        function resetSession() {
            if (draggedTile) {
                draggedTile.classList.remove("dispatcher-dragging");
            }
            var board = getBoard();
            if (board) board.classList.remove("is-complex-dragging");
            clearDragGhost();
            draggedTile = null;
            rootDocument.querySelectorAll(".dispatcher-drop-target").forEach(function (target) {
                target.classList.remove("dispatcher-drop-target");
            });
        }
        function bindDragTile(tile) {
            if (!tile || tile.dataset.dragBound === "true") return;
            tile.dataset.dragBound = "true";
            tile.addEventListener("dragstart", function (event) {
                if (!dispatcherShiftIsOpen()) {
                    event.preventDefault();
                    resetSession();
                    return;
                }
                if (tile.dataset.complexTruck === "true") event.stopPropagation();
                if (tile.classList.contains("is-assigned") || tile.classList.contains("is-placeholder")) {
                    event.preventDefault();
                    resetSession();
                    return;
                }
                draggedTile = tile;
                tile.classList.add("dispatcher-dragging");
                var board = getBoard();
                if (board && tile.dataset.dispatcherDrag === "complex") {
                    board.classList.add("is-complex-dragging");
                    var progress = tile.querySelector(".equipment-progress-complex");
                    if (progress) {
                        dragGhost = progress.cloneNode(true);
                        dragGhost.className = "dispatcher-drag-ghost";
                        rootDocument.body.appendChild(dragGhost);
                        event.dataTransfer.setDragImage(dragGhost, 28, 28);
                    }
                }
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", tile.dataset.equipmentName || "");
            });
            tile.addEventListener("dragend", function (event) {
                if (tile.dataset.complexTruck === "true") event.stopPropagation();
                resetSession();
            });
        }
        function bindDispatcherComplexDrop(zone) {
            if (!zone || zone.dataset.dispatcherDropBound === "true") return;
            zone.dataset.dispatcherDropBound = "true";
            zone.addEventListener("dragover", function (event) {
                if (!draggedTile) return;
                if (draggedTile.dataset.dispatcherDrag === "complex") return;
                event.preventDefault();
                zone.classList.add("dispatcher-drop-target");
            });
            zone.addEventListener("dragleave", function () {
                zone.classList.remove("dispatcher-drop-target");
            });
            zone.addEventListener("drop", function (event) {
                event.preventDefault();
                zone.classList.remove("dispatcher-drop-target");
                if (!draggedTile) return;
                if (draggedTile.dataset.dispatcherDrag === "complex") return;
                if (draggedTile.dataset.dispatcherDrag === "excavator") {
                    var activatedExcavatorTile = draggedTile;
                    var targetComplexCard = zone;
                    dispatcherPost(dispatcherMoveExcavatorUrl, {
                        excavator_id: activatedExcavatorTile.dataset.equipmentId,
                        zone: "active",
                        expected_zone: "inactive"
                    }, { queueOnNetworkFailure: false }).then(function (response) {
                        return refreshDesktopBoardAfterStructuralAction(response, function () {
                            activateDesktopComplexFromExcavatorTile(activatedExcavatorTile, targetComplexCard);
                        });
                    }).catch(handleDesktopOptimisticBoardError);
                    return;
                }
                if (draggedTile.dataset.dispatcherDrag === "truck") {
                    if (!zone.dataset.equipmentId) return;
                    var assignedTruckTile = draggedTile;
                    var targetComplexCard = zone;
                    dispatcherPost(dispatcherAssignTruckUrl, {
                        action: "assign",
                        truck_id: assignedTruckTile.dataset.equipmentId,
                        excavator_id: zone.dataset.equipmentId,
                        expected_assignment_state_id: getAssignmentStateId(assignedTruckTile)
                    }).then(function (response) {
                        return applyDesktopTruckAction(response, {
                            type: "assign",
                            truckTile: assignedTruckTile,
                            complexCard: targetComplexCard
                        });
                    }).catch(showDispatcherDnDError);
                }
            });
        }
        function bindDispatcherExcavatorGarageDrop(garage) {
            if (!garage || garage.dataset.dispatcherDropBound === "true") return;
            garage.dataset.dispatcherDropBound = "true";
            garage.addEventListener("dragover", function (event) {
                if (!draggedTile || draggedTile.dataset.dispatcherDrag !== "complex") return;
                event.preventDefault();
                garage.classList.add("dispatcher-drop-target");
            });
            garage.addEventListener("dragleave", function () {
                garage.classList.remove("dispatcher-drop-target");
            });
            garage.addEventListener("drop", function (event) {
                event.preventDefault();
                garage.classList.remove("dispatcher-drop-target");
                if (!draggedTile || draggedTile.dataset.dispatcherDrag !== "complex") return;
                var inactiveComplexCard = draggedTile;
                var inactiveExcavatorId = inactiveComplexCard.dataset.equipmentId;
                var inactiveComplexName = (inactiveComplexCard.dataset.equipmentName || "комплекс").trim();
                var inactiveTruckCount = inactiveComplexCard.querySelectorAll("[data-complex-truck='true']").length;
                requestDispatcherDesktopDangerConfirmation({
                    title: "Расформировать комплекс?",
                    message: "Экскаватор " + inactiveComplexName + " уйдет в гараж экскаваторов, а все самосвалы " +
                        "комплекса (" + inactiveTruckCount + " шт.) — в гараж самосвалов через 5 минут.",
                    acceptLabel: "Расформировать",
                    action: function () {
                        dispatcherPost(dispatcherMoveExcavatorUrl, {
                            excavator_id: inactiveExcavatorId,
                            zone: "inactive",
                            expected_zone: "active",
                            expected_assignment_states: collectComplexAssignmentStates(inactiveComplexCard)
                        }, { queueOnNetworkFailure: false }).then(function (response) {
                            return refreshDesktopBoardAfterStructuralAction(response, function () {
                                applyHaulAssignmentStates(response, inactiveComplexCard);
                                moveDesktopComplexToExcavatorGarage(inactiveComplexCard);
                            });
                        }).catch(handleDesktopOptimisticBoardError);
                    }
                });
            });
        }
        function bindDispatcherTruckGarageDrop(garage) {
            if (!garage || garage.dataset.dispatcherDropBound === "true") return;
            garage.dataset.dispatcherDropBound = "true";
            garage.addEventListener("dragover", function (event) {
                if (!draggedTile || (draggedTile.dataset.complexTruck !== "true" && draggedTile.dataset.dispatcherDrag !== "complex")) return;
                event.preventDefault();
                garage.classList.add("dispatcher-drop-target");
            });
            garage.addEventListener("dragleave", function () {
                garage.classList.remove("dispatcher-drop-target");
            });
            garage.addEventListener("drop", function (event) {
                event.preventDefault();
                garage.classList.remove("dispatcher-drop-target");
                if (!draggedTile) return;
                if (draggedTile.dataset.dispatcherDrag === "complex") {
                    var complexCard = draggedTile;
                    var complexName = (complexCard.dataset.equipmentName || "комплекса").trim();
                    var truckCount = complexCard.querySelectorAll("[data-complex-truck='true']").length;
                    requestDispatcherDesktopDangerConfirmation({
                        title: "Снять все самосвалы?",
                        message: "Снять все самосвалы комплекса " + complexName + " (" + truckCount +
                            " шт.)? Экскаватор останется на месте, самосвалы уйдут в гараж через 5 минут.",
                        acceptLabel: "Снять самосвалы",
                        action: function () {
                            dispatcherPost(dispatcherAssignTruckUrl, {
                                action: "release_complex",
                                excavator_id: complexCard.dataset.equipmentId,
                                expected_assignment_states: collectComplexAssignmentStates(complexCard)
                            }, { queueOnNetworkFailure: false }).then(function (response) {
                                return applyDesktopTruckAction(response, {
                                    type: "release_complex",
                                    complexCard: complexCard
                                });
                            }).catch(showDispatcherDnDError);
                        }
                    });
                    return;
                }
                if (draggedTile.dataset.complexTruck !== "true") return;
                var releasedTruckTile = draggedTile;
                dispatcherPost(dispatcherAssignTruckUrl, {
                    action: "release",
                    truck_id: releasedTruckTile.dataset.equipmentId,
                    expected_assignment_state_id: getAssignmentStateId(releasedTruckTile)
                }).then(function (response) {
                    return applyDesktopTruckAction(response, {
                        type: "release",
                        truckTile: releasedTruckTile
                    });
                }).catch(showDispatcherDnDError);
            });
        }
        function bind() {
            rootDocument.querySelectorAll("[data-dispatcher-drag]").forEach(bindDragTile);
            if (!dispatcherShiftIsOpen()) return;
            rootDocument.querySelectorAll("[data-dispatcher-drop='complex']").forEach(bindDispatcherComplexDrop);
            rootDocument.querySelectorAll("[data-dispatcher-drop='excavator-garage']").forEach(bindDispatcherExcavatorGarageDrop);
            rootDocument.querySelectorAll("[data-dispatcher-drop='truck-garage']").forEach(bindDispatcherTruckGarageDrop);
        }

        return {
            bind: bind,
            bindDragTile: bindDragTile,
            resetSession: resetSession
        };
    }

    global.createDispatcherBoardDnD = createDispatcherBoardDnD;
})(window, document);
