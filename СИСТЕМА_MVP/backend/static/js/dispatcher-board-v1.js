/* Dispatcher desktop board.
   Owns board layout and optimistic DOM updates.
   Network transport, realtime reconciliation and detail rendering are injected. */
(function (global, document) {
    "use strict";

    function createDispatcherBoard(options) {
        options = options || {};
        if (typeof global.createDispatcherBoardDnD !== "function") {
            throw new Error("Dispatcher board drag-and-drop module is not loaded");
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
        function dispatcherShiftIsOpen() {
            return typeof options.getShiftOpen === "function"
                ? Boolean(options.getShiftOpen())
                : false;
        }
        function refreshDispatcherDesktopBoardFromServer(refreshOptions) {
            return options.refreshBoardFromServer(refreshOptions);
        }
        function markDispatcherLocalAssignmentApplied() {
            return options.markLocalAssignmentApplied();
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
        function dispatcherSelectorValue(value) {
            var stringValue = String(value || "");
            if (window.CSS && typeof window.CSS.escape === "function") {
                return window.CSS.escape(stringValue);
            }
            return stringValue.replace(/["\\]/g, "\\$&");
        }
        function removeDuplicateDesktopTruckTiles(truckId, keepTile) {
            if (!truckId) return;
            var selector = '[data-dispatcher-drag="truck"][data-equipment-id="' + dispatcherSelectorValue(truckId) + '"]';
            document.querySelectorAll(selector).forEach(function (tile) {
                if (tile !== keepTile) {
                    tile.remove();
                }
            });
        }
        function reconcileDesktopTruckUniqueness() {
            var seen = {};
            document.querySelectorAll('[data-dispatcher-drag="truck"][data-equipment-id]').forEach(function (tile) {
                var truckId = tile.dataset.equipmentId || "";
                if (!truckId) return;
                if (seen[truckId]) {
                    tile.remove();
                    return;
                }
                seen[truckId] = true;
            });
        }
        function refreshDesktopBoardIntegrity() {
            reconcileDesktopTruckUniqueness();
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
        function escapeHtml(value) {
            return String(value || "").replace(/[&<>"']/g, function (char) {
                return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char];
            });
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
        function findTruckGarageList() {
            return document.querySelector(".dispatcher-trucks");
        }
        function findComplexTruckRack(complexCard) {
            return complexCard ? complexCard.querySelector(".complex-assigned-trucks") : null;
        }
        function getFirstTruckGaragePlaceholder() {
            var garage = findTruckGarageList();
            return garage ? garage.querySelector(".dispatcher-truck-tile.is-placeholder") : null;
        }
        function moveDesktopTruckToGarage(tile) {
            var garage = findTruckGarageList();
            if (!garage || !tile) return false;
            removeDuplicateDesktopTruckTiles(tile.dataset.equipmentId, tile);
            tile.classList.remove("complex-truck-tile", "dispatcher-dragging");
            tile.removeAttribute("data-truck-tile");
            setDispatcherNodeEquipmentState(tile, "free", "truck");
            tile.dataset.dispatcherDrag = "truck";
            tile.dataset.garageItem = "truck";
            tile.setAttribute("draggable", "true");
            tile.removeAttribute("aria-disabled");
            delete tile.dataset.complexTruck;
            delete tile.dataset.assignedZone;
            var firstPlaceholder = getFirstTruckGaragePlaceholder();
            if (firstPlaceholder && firstPlaceholder.parentNode === garage) {
                garage.insertBefore(tile, firstPlaceholder);
            } else {
                garage.appendChild(tile);
            }
            bindDispatcherDragTile(tile);
            bindEquipmentCardTrigger(tile);
            refreshDesktopBoardIntegrity();
            return true;
        }
        function moveDesktopTruckToComplex(tile, complexCard) {
            var rack = findComplexTruckRack(complexCard);
            if (!rack || !tile) return false;
            var sourceRack = tile.closest(".complex-assigned-trucks");
            var zoneId = complexCard.dataset.zoneId || "";
            removeDuplicateDesktopTruckTiles(tile.dataset.equipmentId, tile);
            tile.classList.add("complex-truck-tile");
            tile.setAttribute("data-truck-tile", "");
            tile.classList.remove("dispatcher-dragging", "is-placeholder");
            setDispatcherNodeEquipmentState(tile, "assigned", "truck");
            tile.dataset.dispatcherDrag = "truck";
            tile.dataset.complexTruck = "true";
            tile.dataset.assignedZone = zoneId;
            tile.setAttribute("draggable", "true");
            tile.removeAttribute("aria-disabled");
            delete tile.dataset.garageItem;
            var empty = rack.querySelector(".complex-truck-empty");
            if (empty) empty.hidden = true;
            rack.appendChild(tile);
            if (sourceRack && sourceRack !== rack) {
                refreshComplexTruckRack(sourceRack);
            }
            bindDispatcherDragTile(tile);
            bindEquipmentCardTrigger(tile);
            refreshDesktopBoardIntegrity();
            return true;
        }
        function releaseDesktopComplexTrucks(complexCard) {
            var rack = findComplexTruckRack(complexCard);
            if (!rack) return false;
            var trucks = Array.from(rack.querySelectorAll(".complex-truck-tile"));
            if (!trucks.length) {
                refreshComplexTruckRack(rack);
                return true;
            }
            trucks.forEach(moveDesktopTruckToGarage);
            refreshComplexTruckRack(rack);
            refreshTruckGarage();
            return true;
        }
        function findExcavatorGarageList() {
            return document.querySelector(".dispatcher-excavators");
        }
        function getFirstExcavatorGaragePlaceholder() {
            var garage = findExcavatorGarageList();
            return garage ? garage.querySelector(".dispatcher-excavator-garage-tile.is-placeholder") : null;
        }
        function resetDesktopComplexCardToEmpty(complexCard) {
            if (!complexCard) return false;
            var zoneId = complexCard.dataset.zoneId || "";
            complexCard.className = "dispatcher-complex-card status-empty";
            complexCard.style.setProperty("--complex-progress", "0%");
            complexCard.dataset.dispatcherDrop = "complex";
            complexCard.dataset.zoneId = zoneId;
            delete complexCard.dataset.dispatcherDrag;
            delete complexCard.dataset.equipmentCardId;
            delete complexCard.dataset.sourceEquipmentCardId;
            delete complexCard.dataset.equipmentId;
            delete complexCard.dataset.equipmentName;
            delete complexCard.dataset.equipmentState;
            delete complexCard.dataset.excavatorSlot;
            delete complexCard.dataset.dragBound;
            delete complexCard.dataset.cardBound;
            complexCard.removeAttribute("role");
            complexCard.removeAttribute("tabindex");
            complexCard.removeAttribute("draggable");
            complexCard.innerHTML = '<div class="complex-empty" aria-hidden="true"></div>';
            return true;
        }
        function buildDesktopExcavatorGarageTile(complexCard) {
            var tile = document.createElement("article");
            var slot = complexCard.dataset.excavatorSlot || "";
            var name = complexCard.dataset.equipmentName || ("Экскаватор " + slot).trim();
            var cardId = complexCard.dataset.equipmentId || complexCard.dataset.equipmentCardId || complexCard.dataset.sourceEquipmentCardId || "";
            var stateCode = "garage";
            tile.className = "dispatcher-equipment-tile dispatcher-excavator-garage-tile " + dispatcherEquipmentStateClass(stateCode) + " is-sync-pending";
            tile.style.setProperty("--tile-progress", "0%");
            tile.setAttribute("role", "button");
            tile.setAttribute("tabindex", "0");
            tile.setAttribute("draggable", "true");
            tile.dataset.dispatcherDrag = "excavator";
            tile.dataset.equipmentCardId = cardId;
            tile.dataset.equipmentId = cardId;
            tile.dataset.equipmentName = name;
            tile.dataset.equipmentState = stateCode;
            tile.dataset.excavatorSlot = slot;
            tile.dataset.garageItem = "excavator";
            tile.innerHTML =
                (slot ? "<strong>" + escapeHtml(slot) + "</strong>" : "") +
                '<img src="' + escapeHtml(dispatcherNeutralEquipmentIcon("excavator")) + '" alt="">' +
                "<span>" + escapeHtml(dispatcherEquipmentStateLabel(stateCode)) + "</span>";
            return tile;
        }
        function moveDesktopComplexToExcavatorGarage(complexCard) {
            var garage = findExcavatorGarageList();
            if (!garage || !complexCard || complexCard.classList.contains("status-empty")) return false;
            releaseDesktopComplexTrucks(complexCard);
            var tile = buildDesktopExcavatorGarageTile(complexCard);
            var firstPlaceholder = getFirstExcavatorGaragePlaceholder();
            if (firstPlaceholder && firstPlaceholder.parentNode === garage) {
                garage.insertBefore(tile, firstPlaceholder);
            } else {
                garage.appendChild(tile);
            }
            resetDesktopComplexCardToEmpty(complexCard);
            bindDispatcherDragTile(tile);
            bindEquipmentCardTrigger(tile);
            refreshExcavatorGarage();
            refreshTruckGarage();
            refreshAllComplexTruckRacks();
            normalizeComplexGrid();
            return true;
        }
        function activateDesktopComplexFromExcavatorTile(tile, complexCard) {
            if (!tile || !complexCard) return false;
            var targetCard = complexCard.classList.contains("status-empty")
                ? complexCard
                : document.querySelector(".dispatcher-complex-card.status-empty");
            if (!targetCard) return false;
            var zoneId = targetCard.dataset.zoneId || "К";
            var cardId = tile.dataset.equipmentId || tile.dataset.equipmentCardId || "";
            var name = tile.dataset.equipmentName || "";
            var slot = tile.dataset.excavatorSlot || (tile.querySelector("strong") && tile.querySelector("strong").textContent) || "";
            var zoneLabel = slot ? "K-" + slot : (targetCard.dataset.zoneLabel || "K");
            var stateCode = "assigned";
            targetCard.className = "dispatcher-complex-card " + dispatcherEquipmentStateClass(stateCode) + " is-sync-pending";
            targetCard.style.setProperty("--complex-progress", "0%");
            targetCard.dataset.dispatcherDrop = "complex";
            targetCard.dataset.zoneId = zoneId;
            targetCard.dataset.zoneLabel = zoneLabel;
            targetCard.dataset.dispatcherDrag = "complex";
            targetCard.dataset.equipmentCardId = cardId;
            targetCard.dataset.sourceEquipmentCardId = cardId;
            targetCard.dataset.equipmentId = cardId;
            targetCard.dataset.equipmentName = name;
            targetCard.dataset.equipmentState = stateCode;
            targetCard.dataset.excavatorSlot = slot;
            delete targetCard.dataset.dragBound;
            delete targetCard.dataset.cardBound;
            targetCard.setAttribute("role", "button");
            targetCard.setAttribute("tabindex", "0");
            targetCard.setAttribute("draggable", "true");
            targetCard.innerHTML =
                '<div class="complex-work-head">' +
                    '<div class="complex-title-state">' +
                        "<h2>" + escapeHtml(zoneLabel) + "</h2>" +
                        '<span class="complex-state-chip">' + escapeHtml(dispatcherEquipmentStateLabel(stateCode)) + '</span>' +
                    '</div>' +
                    '<div class="complex-context">' +
                        '<span class="complex-info-chip chip-horizon">Гор. -</span>' +
                        '<span class="complex-info-chip chip-block">Блок -</span>' +
                        '<span class="complex-info-chip chip-rock">порода не указана</span>' +
                    "</div>" +
                "</div>" +
                '<div class="complex-assigned-trucks truck-fill-1" style="--complex-truck-cols: 6;" data-truck-need="0" aria-label="Активные самосвалы комплекса">' +
                    '<em class="complex-truck-empty">самосвалы не назначены</em>' +
                "</div>";
            if (!document.body.classList.contains("mining-master-mobile-screen")) {
                /* На настольном пульте карточка несёт полосу ключевых цифр, как в
                   шаблоне; значений до ответа сервера нет — нули и прочерк, как у
                   комплекса без плана. Единицу объёма берём у соседней карточки. */
                var unitNode = document.querySelector('.dispatcher-complex-card .complex-kpi[data-kpi="volume"] small');
                var unit = unitNode ? unitNode.textContent : "т";
                targetCard.querySelector(".complex-assigned-trucks").insertAdjacentHTML(
                    "beforebegin",
                    '<div class="complex-kpis" aria-label="Показатели комплекса">' +
                        '<span class="complex-kpi is-zero" data-kpi="trucks" title="Назначено самосвалов / нужно по составу"><i>Машин</i><b>0<small>/0</small></b></span>' +
                        '<span class="complex-kpi" data-kpi="volume" title="Погружено за смену"><i>Объём</i><b>0<small>' + escapeHtml(unit) + '</small></b></span>' +
                        '<span class="complex-kpi is-muted" data-kpi="plan" title="План не назначен"><i>План</i><b>&mdash;</b></span>' +
                    "</div>"
                );
            }
            tile.remove();
            bindDispatcherDragTile(targetCard);
            bindEquipmentCardTrigger(targetCard);
            refreshExcavatorGarage();
            refreshTruckGarage();
            refreshAllComplexTruckRacks();
            normalizeComplexGrid();
            return true;
        }
        function confirmDesktopOptimisticBoardAction(response) {
            if (response && response.queued) return response;
            markDispatcherLocalAssignmentApplied();
            return response;
        }
        function refreshDesktopBoardAfterStructuralAction(response, localFallback) {
            if (response && response.queued) {
                if (typeof localFallback === "function") {
                    localFallback();
                }
                markDispatcherLocalAssignmentApplied();
                return response;
            }
            return refreshDispatcherDesktopBoardFromServer().then(function (applied) {
                if (!applied) {
                    if (typeof localFallback === "function") {
                        localFallback();
                    }
                    markDispatcherLocalAssignmentApplied();
                }
                return response;
            }).catch(function (error) {
                if (typeof localFallback === "function") {
                    localFallback();
                    markDispatcherLocalAssignmentApplied();
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
            } else {
                markDispatcherLocalAssignmentApplied();
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
        var dispatcherDnD = global.createDispatcherBoardDnD({
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
        var bindDispatcherDragTile = dispatcherDnD.bindDragTile;
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
