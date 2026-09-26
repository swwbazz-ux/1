/* Dispatcher complex truck racks.
   Owns the visual layout of trucks assigned to complex cards. Network actions,
   drag-and-drop and assignment state remain owned by dispatcher-board-v1.js. */
(function (global, document) {
    "use strict";

    function createDispatcherComplexTruckRacks(options) {
        options = options || {};
        var sortEquipmentList = options.sortEquipmentList || function () {};

        /* Размер считается от размеров САМОГО поля, а не от гаражной плитки:
           поле в карточке комплекса живёт независимо от гаража справа. */
        var COMPLEX_TILE_GAP = 6;
        var COMPLEX_TILE_MAX = { w: 168, h: 124 };
        var COMPLEX_TILE_MIN = { w: 34, h: 20 };
        /* Один перегруженный комплекс не должен мельчить плитки всей доски. */
        var COMPLEX_TILE_FLOOR = { w: 88, h: 63 };
        var COMPLEX_INFO_MIN_W = 176;
        var COMPLEX_TILE_ASPECT = { min: 1.15, max: 1.55 };
        var COMPLEX_TILE_RICH_MIN_H = 42;

        function complexTileForGrid(rackWidth, rackHeight, cols, rows) {
            var cellW = (rackWidth - (COMPLEX_TILE_GAP * (cols - 1))) / cols;
            var cellH = (rackHeight - (COMPLEX_TILE_GAP * (rows - 1))) / rows;
            if (cellW < COMPLEX_TILE_MIN.w || cellH < COMPLEX_TILE_MIN.h) return null;
            var w = Math.min(cellW, COMPLEX_TILE_MAX.w);
            var h = Math.min(cellH, COMPLEX_TILE_MAX.h);
            if (w / h > COMPLEX_TILE_ASPECT.max) w = h * COMPLEX_TILE_ASPECT.max;
            if (w / h < COMPLEX_TILE_ASPECT.min) h = w / COMPLEX_TILE_ASPECT.min;
            w = Math.floor(w);
            h = Math.floor(h);
            if (w < COMPLEX_TILE_MIN.w || h < COMPLEX_TILE_MIN.h) return null;
            return { w: w, h: h, cols: cols, rows: rows, area: w * h };
        }

        function complexTruckLayout(rackWidth, rackHeight, count) {
            var best = null;
            for (var cols = 1; cols <= count; cols += 1) {
                var fit = complexTileForGrid(rackWidth, rackHeight, cols, Math.ceil(count / cols));
                if (!fit) continue;
                if (!best || fit.area > best.area || (fit.area === best.area && fit.rows < best.rows)) {
                    best = fit;
                }
            }
            return best;
        }

        function applyComplexTruckLayout(rack, tiles, empty, size, cols, rackHeight) {
            var rows = Math.max(1, Math.floor((rackHeight + COMPLEX_TILE_GAP) / (size.h + COMPLEX_TILE_GAP)));
            var capacity = cols * rows;
            var visible = tiles.length <= capacity ? tiles.length : Math.max(1, capacity - 1);

            tiles.forEach(function (tile, index) {
                tile.hidden = index >= visible;
            });

            var more = rack.querySelector(".complex-truck-more");
            var hiddenCount = tiles.length - visible;
            if (hiddenCount > 0) {
                if (!more) {
                    more = document.createElement("b");
                    more.className = "complex-truck-more";
                    rack.appendChild(more);
                }
                more.hidden = false;
                more.textContent = "+" + hiddenCount;
                more.title = "Ещё самосвалов: " + hiddenCount;
            } else if (more) {
                more.hidden = true;
            }

            var need = parseInt(rack.getAttribute("data-truck-need"), 10) || 0;
            var mobile = document.body.classList.contains("mining-master-mobile-screen");
            var free = capacity - visible - (hiddenCount > 0 ? 1 : 0);
            var slots = (mobile || !tiles.length) ? 0 : Math.max(0, Math.min(need - tiles.length, free));
            var ghosts = Array.from(rack.querySelectorAll(".complex-truck-slot"));
            while (ghosts.length < slots) {
                var ghost = document.createElement("i");
                ghost.className = "complex-truck-slot";
                ghost.setAttribute("aria-hidden", "true");
                ghost.textContent = "+";
                ghosts.push(ghost);
            }
            ghosts.forEach(function (ghost, index) {
                ghost.hidden = index >= slots;
                rack.insertBefore(ghost, empty && empty.parentNode === rack ? empty : null);
            });

            rack.classList.remove("truck-fill-1", "truck-fill-2", "truck-fill-3", "truck-fill-4");
            rack.classList.toggle("is-rich-trucks", size.h >= COMPLEX_TILE_RICH_MIN_H);
            rack.style.setProperty("--complex-truck-cols", String(cols));
            rack.style.setProperty("--complex-truck-gap", COMPLEX_TILE_GAP + "px");
            rack.style.setProperty("--complex-truck-w", size.w + "px");
            rack.style.setProperty("--complex-truck-h", size.h + "px");
            rack.style.setProperty("--complex-truck-font", Math.max(10, Math.round(size.h * 0.5)) + "px");
            rack.style.setProperty("--complex-truck-justify", "start");
            if (empty) empty.hidden = tiles.length > 0;
        }

        function complexTruckFieldWidth(rack) {
            var card = rack.closest(".dispatcher-complex-card");
            if (!card) return rack.clientWidth || 0;
            var cs = getComputedStyle(card);
            var inner = card.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
            var gap = parseFloat(cs.columnGap) || 0;
            return Math.max(0, inner - COMPLEX_INFO_MIN_W - gap);
        }

        function measureComplexTruckRack(rack) {
            if (!rack) return null;
            sortEquipmentList(rack, ".complex-truck-tile");
            var tiles = Array.from(rack.querySelectorAll(".complex-truck-tile"));
            var empty = rack.querySelector(".complex-truck-empty");
            if (!empty && tiles.length === 0) {
                empty = document.createElement("em");
                empty.className = "complex-truck-empty";
                empty.textContent = "самосвалы не назначены";
                rack.appendChild(empty);
            }
            var width = complexTruckFieldWidth(rack);
            var height = rack.clientHeight || rack.getBoundingClientRect().height || 0;
            if (width < COMPLEX_TILE_MIN.w || height < COMPLEX_TILE_MIN.h) return null;
            return { rack: rack, tiles: tiles, empty: empty, width: width, height: height };
        }

        function refreshRack() {
            refreshAll();
        }

        function refreshAll() {
            var measured = [];
            document.querySelectorAll(".complex-assigned-trucks").forEach(function (rack) {
                var m = measureComplexTruckRack(rack);
                if (m) measured.push(m);
            });
            if (!measured.length) return;

            var common = null;
            var maxCount = 0;
            measured.forEach(function (m) {
                if (!m.tiles.length) return;
                maxCount = Math.max(maxCount, m.tiles.length);
                var size = complexTruckLayout(m.width, m.height, m.tiles.length);
                if (!size) size = COMPLEX_TILE_MIN;
                if (!common || (size.w * size.h) < (common.w * common.h)) common = size;
            });
            if (!common) common = COMPLEX_TILE_MAX;
            if ((common.w * common.h) < (COMPLEX_TILE_FLOOR.w * COMPLEX_TILE_FLOOR.h)) {
                common = COMPLEX_TILE_FLOOR;
            }

            var width = measured[0].width;
            var height = measured[0].height;
            var fitCols = Math.max(1, Math.floor((width + COMPLEX_TILE_GAP) / (common.w + COMPLEX_TILE_GAP)));
            var fitRows = Math.max(1, Math.floor((height + COMPLEX_TILE_GAP) / (common.h + COMPLEX_TILE_GAP)));
            var needCols = Math.max(1, Math.ceil(Math.max(1, maxCount) / fitRows));
            var cols = Math.max(1, Math.min(fitCols, needCols));

            measured.forEach(function (m) {
                applyComplexTruckLayout(m.rack, m.tiles, m.empty, common, cols, m.height);
            });
        }

        var complexRackResizeObserver = null;
        function watch() {
            if (typeof ResizeObserver === "undefined") {
                requestAnimationFrame(function () {
                    requestAnimationFrame(refreshAll);
                });
                global.addEventListener("load", refreshAll, { once: true });
                return;
            }
            if (!complexRackResizeObserver) {
                complexRackResizeObserver = new ResizeObserver(function () {
                    refreshAll();
                });
            }
            complexRackResizeObserver.disconnect();
            document.querySelectorAll(".complex-assigned-trucks").forEach(function (rack) {
                complexRackResizeObserver.observe(rack);
            });
        }

        watch();
        return {
            refreshAll: refreshAll,
            refreshRack: refreshRack,
            watch: watch
        };
    }

    global.createDispatcherComplexTruckRacks = createDispatcherComplexTruckRacks;
})(window, document);
