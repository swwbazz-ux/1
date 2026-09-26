/* Dispatcher desktop board layout.
   Owns only ordering, empty placeholders and responsive garage/grid presentation.
   Commands, assignment state, drag-and-drop and realtime remain outside this module. */
(function (global, document) {
    "use strict";

    function createDispatcherBoardLayout(options) {
        options = options || {};
        var hostDocument = options.document || document;

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

        function sortEquipmentList(container, selector) {
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

        function refreshExcavatorGarage() {
            var board = hostDocument.querySelector(".dispatcher-board");
            var excavatorGarage = hostDocument.querySelector("[data-dispatcher-excavator-garage]");
            if (!board || !excavatorGarage) return;
            sortEquipmentList(
                excavatorGarage.querySelector(".dispatcher-excavators"),
                ".dispatcher-excavator-garage-tile:not(.is-placeholder)"
            );
            var activeTiles = excavatorGarage.querySelectorAll("[data-garage-item='excavator']:not(.is-assigned)");
            board.classList.toggle("is-excavator-garage-empty", activeTiles.length === 0);
        }

        function refreshTruckGarage() {
            var board = hostDocument.querySelector(".dispatcher-board");
            if (!board) return;
            hostDocument.querySelectorAll(".dispatcher-truck-tile.is-placeholder").forEach(function (slot) {
                slot.remove();
            });
            var garage = hostDocument.querySelector(".dispatcher-trucks");
            sortEquipmentList(garage, "[data-garage-item='truck']:not(.is-placeholder)");
            var freeTrucks = hostDocument.querySelectorAll("[data-garage-item='truck']:not(.is-assigned)");
            var columns = Math.max(1, Math.min(3, Math.ceil(freeTrucks.length / 12)));
            var scrollNeeded = freeTrucks.length > columns * 12;
            board.style.setProperty("--truck-garage-columns", String(columns));
            board.style.setProperty("--truck-garage-scrollbar-w", scrollNeeded ? "14px" : "0px");
            board.classList.toggle("is-truck-garage-empty", freeTrucks.length === 0);
            if (!garage || freeTrucks.length === 0) return;
            var visibleCapacity = columns * 12;
            var placeholderCount = Math.max(0, visibleCapacity - freeTrucks.length);
            for (var index = 0; index < placeholderCount; index += 1) {
                var slot = hostDocument.createElement("article");
                slot.className = "dispatcher-truck-tile is-placeholder";
                slot.setAttribute("aria-hidden", "true");
                slot.innerHTML = '<img src="/static/img/equipment/truck-gray.png" alt="">';
                garage.appendChild(slot);
            }
        }

        function normalizeComplexGrid() {
            var grid = hostDocument.querySelector(".dispatcher-zone-grid");
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

        return {
            sortEquipmentList: sortEquipmentList,
            refreshTruckGarage: refreshTruckGarage,
            refreshExcavatorGarage: refreshExcavatorGarage,
            normalizeComplexGrid: normalizeComplexGrid
        };
    }

    global.createDispatcherBoardLayout = createDispatcherBoardLayout;
})(window, document);
