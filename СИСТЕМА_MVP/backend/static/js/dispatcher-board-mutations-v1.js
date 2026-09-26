/* Dispatcher desktop local board mutations.
   Owns only DOM placement after a confirmed command or local fallback.
   Transport, assignment-state tokens, realtime reconciliation and layout policy
   stay in dispatcher-board-v1.js and are passed here as narrow callbacks. */
(function (global, document) {
    "use strict";

    function createDispatcherBoardMutations(options) {
        options = options || {};
        var rootDocument = options.document || document;
        var rootWindow = options.window || global;
        var dispatcherEquipmentStateClass = options.equipmentStateClass || function () { return ""; };
        var dispatcherEquipmentStateLabel = options.equipmentStateLabel || function () { return ""; };
        var dispatcherNeutralEquipmentIcon = options.neutralEquipmentIcon || function () { return ""; };
        var setDispatcherNodeEquipmentState = options.setNodeEquipmentState || function () {};
        var bindDispatcherDragTile = options.bindDragTile || function () {};
        var bindEquipmentCard = options.bindEquipmentCard || function () {};
        var refreshDesktopBoardIntegrity = options.refreshIntegrity || function () {};
        var refreshTruckGarage = options.refreshTruckGarage || function () {};
        var refreshExcavatorGarage = options.refreshExcavatorGarage || function () {};
        var refreshComplexTruckRack = options.refreshComplexTruckRack || function () {};
        var refreshAllComplexTruckRacks = options.refreshComplexTruckRacks || function () {};
        var normalizeComplexGrid = options.normalizeComplexGrid || function () {};

        function dispatcherSelectorValue(value) {
            var stringValue = String(value || "");
            if (rootWindow.CSS && typeof rootWindow.CSS.escape === "function") {
                return rootWindow.CSS.escape(stringValue);
            }
            return stringValue.replace(/["\\]/g, "\\$&");
        }
        function removeDuplicateDesktopTruckTiles(truckId, keepTile) {
            if (!truckId) return;
            var selector = '[data-dispatcher-drag="truck"][data-equipment-id="' + dispatcherSelectorValue(truckId) + '"]';
            rootDocument.querySelectorAll(selector).forEach(function (tile) {
                if (tile !== keepTile) {
                    tile.remove();
                }
            });
        }
        function reconcileTruckUniqueness() {
            var seen = {};
            rootDocument.querySelectorAll('[data-dispatcher-drag="truck"][data-equipment-id]').forEach(function (tile) {
                var truckId = tile.dataset.equipmentId || "";
                if (!truckId) return;
                if (seen[truckId]) {
                    tile.remove();
                    return;
                }
                seen[truckId] = true;
            });
        }
        function findTruckGarageList() {
            return rootDocument.querySelector(".dispatcher-trucks");
        }
        function findComplexTruckRack(complexCard) {
            return complexCard ? complexCard.querySelector(".complex-assigned-trucks") : null;
        }
        function getFirstTruckGaragePlaceholder() {
            var garage = findTruckGarageList();
            return garage ? garage.querySelector(".dispatcher-truck-tile.is-placeholder") : null;
        }
        function moveTruckToGarage(tile) {
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
            bindEquipmentCard(tile);
            refreshDesktopBoardIntegrity();
            return true;
        }
        function moveTruckToComplex(tile, complexCard) {
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
            bindEquipmentCard(tile);
            refreshDesktopBoardIntegrity();
            return true;
        }
        function releaseComplexTrucks(complexCard) {
            var rack = findComplexTruckRack(complexCard);
            if (!rack) return false;
            var trucks = Array.from(rack.querySelectorAll(".complex-truck-tile"));
            if (!trucks.length) {
                refreshComplexTruckRack(rack);
                return true;
            }
            trucks.forEach(moveTruckToGarage);
            refreshComplexTruckRack(rack);
            refreshTruckGarage();
            return true;
        }
        function findExcavatorGarageList() {
            return rootDocument.querySelector(".dispatcher-excavators");
        }
        function getFirstExcavatorGaragePlaceholder() {
            var garage = findExcavatorGarageList();
            return garage ? garage.querySelector(".dispatcher-excavator-garage-tile.is-placeholder") : null;
        }
        function clearComplexPlanPresentation(complexCard) {
            if (!complexCard) return;
            complexCard.style.setProperty("--complex-progress", "0%");
            complexCard.style.setProperty("--complex-total-progress", "0%");
            delete complexCard.dataset.placementZone;
            delete complexCard.dataset.planStatus;
            delete complexCard.dataset.planPercent;
            delete complexCard.dataset.planLoopPercent;
            delete complexCard.dataset.planCompletedLoops;
            delete complexCard.dataset.planProgressPhase;
            delete complexCard.dataset.planMode;
            delete complexCard.dataset.planValue;
            delete complexCard.dataset.planFact;
            delete complexCard.dataset.planUnit;
            delete complexCard.dataset.planGroup;
            complexCard.classList.remove("is-plan-overrun");
        }
        function resetDesktopComplexCardToEmpty(complexCard) {
            if (!complexCard) return false;
            var zoneId = complexCard.dataset.zoneId || "";
            complexCard.className = "dispatcher-complex-card status-empty";
            clearComplexPlanPresentation(complexCard);
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
        function escapeHtml(value) {
            return String(value || "").replace(/[&<>"']/g, function (char) {
                return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char];
            });
        }
        function buildExcavatorGarageTile(complexCard) {
            var tile = rootDocument.createElement("article");
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
        function moveComplexToExcavatorGarage(complexCard) {
            var garage = findExcavatorGarageList();
            if (!garage || !complexCard || complexCard.classList.contains("status-empty")) return false;
            releaseComplexTrucks(complexCard);
            var tile = buildExcavatorGarageTile(complexCard);
            var firstPlaceholder = getFirstExcavatorGaragePlaceholder();
            if (firstPlaceholder && firstPlaceholder.parentNode === garage) {
                garage.insertBefore(tile, firstPlaceholder);
            } else {
                garage.appendChild(tile);
            }
            resetDesktopComplexCardToEmpty(complexCard);
            bindDispatcherDragTile(tile);
            bindEquipmentCard(tile);
            refreshExcavatorGarage();
            refreshTruckGarage();
            refreshAllComplexTruckRacks();
            normalizeComplexGrid();
            return true;
        }
        function activateComplexFromExcavatorTile(tile, complexCard) {
            if (!tile || !complexCard) return false;
            var targetCard = complexCard.classList.contains("status-empty")
                ? complexCard
                : rootDocument.querySelector(".dispatcher-complex-card.status-empty");
            if (!targetCard) return false;
            var zoneId = targetCard.dataset.zoneId || "К";
            var cardId = tile.dataset.equipmentId || tile.dataset.equipmentCardId || "";
            var name = tile.dataset.equipmentName || "";
            var slot = tile.dataset.excavatorSlot || (tile.querySelector("strong") && tile.querySelector("strong").textContent) || "";
            var zoneLabel = slot ? "K-" + slot : (targetCard.dataset.zoneLabel || "K");
            var stateCode = "assigned";
            targetCard.className = "dispatcher-complex-card " + dispatcherEquipmentStateClass(stateCode) + " is-sync-pending";
            clearComplexPlanPresentation(targetCard);
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
                        '<span class="complex-state-chip">' + escapeHtml(dispatcherEquipmentStateLabel(stateCode)) + "</span>" +
                    "</div>" +
                    '<div class="complex-context">' +
                        '<span class="complex-info-chip chip-horizon">Гор. -</span>' +
                        '<span class="complex-info-chip chip-block">Блок -</span>' +
                        '<span class="complex-info-chip chip-rock">порода не указана</span>' +
                    "</div>" +
                "</div>" +
                '<div class="complex-assigned-trucks truck-fill-1" style="--complex-truck-cols: 6;" data-truck-need="0" aria-label="Активные самосвалы комплекса">' +
                    '<em class="complex-truck-empty">самосвалы не назначены</em>' +
                "</div>";
            if (!rootDocument.body.classList.contains("mining-master-mobile-screen")) {
                /* До authoritative fragment показываем лишь нейтральные цифры, не
                   выдумывая значения или фазу сменного плана. */
                var unitNode = rootDocument.querySelector('.dispatcher-complex-card .complex-kpi[data-kpi="volume"] small');
                var unit = unitNode ? unitNode.textContent : "т";
                var truckRack = targetCard.querySelector(".complex-assigned-trucks");
                if (truckRack) {
                    truckRack.insertAdjacentHTML(
                        "beforebegin",
                        '<div class="complex-kpis" aria-label="Показатели комплекса">' +
                            '<span class="complex-kpi is-zero" data-kpi="trucks" title="Назначено самосвалов / нужно по составу"><i>Машин</i><b>0<small>/0</small></b></span>' +
                            '<span class="complex-kpi" data-kpi="volume" title="Погружено за смену"><i>Объём</i><b>0<small>' + escapeHtml(unit) + "</small></b></span>" +
                            '<span class="complex-kpi is-muted" data-kpi="plan" title="План не назначен"><i>План</i><b>&mdash;</b></span>' +
                        "</div>"
                    );
                }
            }
            tile.remove();
            bindDispatcherDragTile(targetCard);
            bindEquipmentCard(targetCard);
            refreshExcavatorGarage();
            refreshTruckGarage();
            refreshAllComplexTruckRacks();
            normalizeComplexGrid();
            return true;
        }

        return {
            reconcileTruckUniqueness: reconcileTruckUniqueness,
            moveTruckToGarage: moveTruckToGarage,
            moveTruckToComplex: moveTruckToComplex,
            releaseComplexTrucks: releaseComplexTrucks,
            moveComplexToExcavatorGarage: moveComplexToExcavatorGarage,
            activateComplexFromExcavatorTile: activateComplexFromExcavatorTile
        };
    }

    global.createDispatcherBoardMutations = createDispatcherBoardMutations;
})(window, document);
