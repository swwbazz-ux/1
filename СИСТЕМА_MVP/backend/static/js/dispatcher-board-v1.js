/* Dispatcher desktop board.
   Owns board layout, equipment search, drag-and-drop and optimistic DOM updates.
   Network transport, realtime reconciliation and detail rendering are injected. */
(function (global, document) {
    "use strict";

    function createDispatcherBoard(options) {
        options = options || {};
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

        function haulAssignmentStateId(node) {
            var value = node && node.dataset ? node.dataset.haulAssignmentStateId : "";
            return /^\d+$/.test(String(value || "")) ? String(value) : "0";
        }
        function collectComplexAssignmentStates(complexCard) {
            var states = {};
            if (!complexCard) return states;
            complexCard.querySelectorAll(
                "[data-complex-truck='true'][data-equipment-id], " +
                "[data-mm-mobile-home-truck-id]"
            ).forEach(function (truck) {
                var truckId = truck.dataset.equipmentId || truck.dataset.mmMobileHomeTruckId || "";
                if (truckId) states[String(truckId)] = haulAssignmentStateId(truck);
            });
            return states;
        }
        function applyHaulAssignmentState(response, truckNode) {
            if (!response || !truckNode || response.assignment_state_id === undefined) return;
            truckNode.dataset.haulAssignmentStateId = String(response.assignment_state_id || 0);
        }
        function applyHaulAssignmentStates(response, root) {
            var states = response && response.assignment_state_ids;
            if (!states || !root) return;
            root.querySelectorAll("[data-equipment-id], [data-mm-mobile-home-truck-id]").forEach(function (node) {
                var truckId = node.dataset.equipmentId || node.dataset.mmMobileHomeTruckId || "";
                if (truckId && Object.prototype.hasOwnProperty.call(states, truckId)) {
                    node.dataset.haulAssignmentStateId = String(states[truckId] || 0);
                }
            });
        }
        var draggedTile = null;
        var board = document.querySelector(".dispatcher-board");
        var excavatorGarage = document.querySelector("[data-dispatcher-excavator-garage]");
        var dragGhost = null;
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
        function clearDragGhost() {
            if (!dragGhost) return;
            dragGhost.remove();
            dragGhost = null;
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
        /* Плитки самосвалов в карточке комплекса.

           Размер считается от размеров САМОГО поля, а не от гаражной плитки
           справа: раньше плитка 73px заезжала в полосу высотой 10px и её срезал
           overflow карточки, а вёрсткой карточки управлял чужой элемент.

           Раскладка подбирается перебором, а не по лестнице фиксированных
           размеров. Лестница брала первый размер, при котором машины помещаются,
           и останавливалась — поэтому при двенадцати машинах в поле оставалась
           пустая колонка справа шириной в целую плитку. Теперь для каждого числа
           колонок считается своя ячейка, и выигрывает та раскладка, где плитка
           крупнее всех: диспетчеру нужно попадать по ним мышью и различать номера
           через комнату, поэтому пустое место всегда отдаётся плиткам.

           Пропорция ограничена коридором, иначе при одной машине на всё поле
           выходил бы вытянутый прямоугольник, а при двадцати — узкая полоска.
           Ниже COMPLEX_TILE_RICH_MIN_H картинка и подпись состояния всё равно
           нечитаемы, поэтому там плитка превращается в жетон номера; состояние
           читается цветом и кольцом плана. Если машин больше, чем ячеек даже при
           минимальном размере, хвост сворачивается в счётчик «+N». Скролла внутри
           карточки нет намеренно: прокручивать десять карточек по отдельности
           диспетчер не станет. */
        var COMPLEX_TILE_GAP = 6;
        /* Потолок держит форму при малом числе машин: исходник картинки 360x245,
           так что до 168px по ширине она не мылится. Нижняя граница коридора
           пропорций 1.15, иначе выигрывала вертикальная плитка (три машины в ряд
           давали 120x126), а самосвал на ней лежит поперёк. */
        var COMPLEX_TILE_MAX = { w: 168, h: 124 };
        var COMPLEX_TILE_MIN = { w: 34, h: 20 };
        /* Ниже этого размера доска не мельчает. Общий размер берётся по самому
           загруженному комплексу, и без нижней границы один комплекс с шестнадцатью
           машинами ужимал плитки на всей доске до нечитаемых. Двенадцать машин на
           один экскаватор — уже за гранью обычной смены, поэтому такой перегруз
           честнее показать счётчиком «+N», чем мельчить все десять карточек. */
        var COMPLEX_TILE_FLOOR = { w: 88, h: 63 };
        /* Минимум информационной панели слева; всё, что шире, отдаётся ей же,
           когда сетка плиток не занимает ширину целиком. */
        var COMPLEX_INFO_MIN_W = 176;
        var COMPLEX_TILE_ASPECT = { min: 1.15, max: 1.55 };
        var COMPLEX_TILE_RICH_MIN_H = 42;

        /* Ячейка при заданном числе колонок и строк, ужатая до коридора пропорций. */
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

        /* Лучшая раскладка для count машин: максимум площади плитки. */
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

        /* Применяет к полю готовый размер плитки и число колонок и раскладывает
           по ним машины. Число колонок общее на всю доску: тогда сетки всех
           карточек одной ширины, прижаты к правому краю, а информационные панели
           слева получают одинаковую ширину и выстраиваются по одной вертикали. */
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

            /* Пустые ячейки под недостающие машины: сколько нужно по составу
               сверх назначенных, но не больше свободных ячеек. Диспетчер видит,
               куда ещё ставить, а не просто пустое поле. Ячейки инертны и стоят
               сразу за плитками: сортировка вставляет плитки перед подписью
               пустого поля, поэтому и ячейки идут перед ней. На телефоне горного
               мастера у поля своя вёрстка, там ячеек нет. */
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

        /* Ширина, которую поле может занять: внутренняя ширина карточки минус
           минимум информационной панели и зазор между ними. Поле стоит в колонке
           auto и само не знает, сколько ему можно, — считаем от карточки. */
        function complexTruckFieldWidth(rack) {
            var card = rack.closest(".dispatcher-complex-card");
            if (!card) return rack.clientWidth || 0;
            var cs = getComputedStyle(card);
            var inner = card.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
            var gap = parseFloat(cs.columnGap) || 0;
            return Math.max(0, inner - COMPLEX_INFO_MIN_W - gap);
        }

        /* Готовит поле к расчёту: сортирует плитки, заводит подпись пустого поля,
           возвращает измерения или null, если карточка ещё не разложена. */
        function measureComplexTruckRack(rack) {
            if (!rack) return null;
            sortDesktopEquipmentList(rack, ".complex-truck-tile");
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
            /* До первой раскладки карточки поле ещё нулевое. Считать по таким
               размерам нельзя: ёмкость выходит в одну ячейку и все машины
               сворачиваются в счётчик, а через кадр пересчёт даёт другое.
               Ждём реальных размеров — ResizeObserver вызовет пересчёт. */
            if (width < COMPLEX_TILE_MIN.w || height < COMPLEX_TILE_MIN.h) return null;
            return { rack: rack, tiles: tiles, empty: empty, width: width, height: height };
        }

        function refreshComplexTruckRack(rack) {
            /* Одиночный пересчёт всё равно идёт через доску: размер и число
               колонок общие, иначе одна карточка выбьется из строя. */
            refreshAllComplexTruckRacks();
        }

        /* Плитки одного размера и сетка одной ширины на всей доске.

           Если считать размер по каждой карточке отдельно, рядом оказываются
           плитки 168px и 70px: поле каждой карточки заполнено, но доска выглядит
           коллажем, и по размеру плиток уже нельзя на глаз сравнить загрузку
           комплексов — а диспетчер смотрит именно на доску целиком. Поэтому
           размер выбирается один на всех — самый скромный из нужных, то есть по
           самому загруженному комплексу, но не мельче нижней границы. Число
           колонок тоже общее — по нему CSS задаёт ширину сетки, а всё, что
           осталось слева, достаётся информационной панели. */
        function refreshAllComplexTruckRacks() {
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

            /* Колонок столько, сколько влезает по ширине, но не больше, чем нужно
               самой загруженной карточке: при шести машинах и четырёх колонках
               получалось бы 4+2, а 3+3 ровнее и отдаёт лишнюю ширину панели. */
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

        /* Размер жетона зависит от размера полосы, поэтому следим именно за ним.
           Одного-двух кадров после DOMContentLoaded не хватает: карточки внутри
           холста раскладываются позже, и расчёт выходил на нулевых размерах, а
           жетоны до первого resize оставались гаражными. ResizeObserver закрывает
           и первую раскладку, и смену размера окна, и подгрузку шрифтов. */
        var complexRackResizeObserver = null;
        function watchComplexTruckRacks() {
            if (typeof ResizeObserver === "undefined") {
                requestAnimationFrame(function () {
                    requestAnimationFrame(refreshAllComplexTruckRacks);
                });
                window.addEventListener("load", refreshAllComplexTruckRacks, { once: true });
                return;
            }
            if (!complexRackResizeObserver) {
                complexRackResizeObserver = new ResizeObserver(function () {
                    refreshAllComplexTruckRacks();
                });
            }
            complexRackResizeObserver.disconnect();
            document.querySelectorAll(".complex-assigned-trucks").forEach(function (rack) {
                complexRackResizeObserver.observe(rack);
            });
        }
        /* Живой поиск техники: набранный номер подсвечивает все плитки этой
           машины — в комплексе, в гараже, карточку комплекса по экскаватору.
           Совпадение по началу номера, чтобы набор сужал круг; «к-2» и «k-2»
           одинаково находят комплекс по имени зоны. Доска после ответа сервера
           перерисовывается целиком — MutationObserver возвращает подсветку. */
        function bindDispatcherEquipmentSearch() {
            var input = document.querySelector("[data-dispatcher-equipment-search]");
            if (!input || document.body.classList.contains("mining-master-mobile-screen")) return;
            var box = input.closest("[data-dispatcher-equipment-search-box]") || input.parentElement;
            var count = document.querySelector("[data-dispatcher-equipment-search-count]");
            var query = "";

            function normalizeSearchText(value) {
                return String(value || "").trim().toLowerCase().replace(/k/g, "к").replace(/\s+/g, "");
            }

            function matchesSearch(node, needle) {
                var name = normalizeSearchText(node.getAttribute("data-equipment-name"));
                if (name && name.indexOf(needle) === 0) return true;
                var zone = normalizeSearchText(node.getAttribute("data-zone-label"));
                return !!zone && zone.indexOf(needle) === 0;
            }

            function applyEquipmentSearch() {
                var needle = normalizeSearchText(query);
                var hits = 0;
                var first = null;
                document.querySelectorAll(".dispatcher-shell [data-equipment-name]").forEach(function (node) {
                    var hit = needle !== "" && matchesSearch(node, needle);
                    node.classList.toggle("is-search-hit", hit);
                    if (hit) {
                        hits += 1;
                        if (!first) first = node;
                    }
                });
                document.body.classList.toggle("is-equipment-search", needle !== "");
                box.classList.toggle("has-query", needle !== "");
                if (count) {
                    count.hidden = needle === "";
                    count.textContent = String(hits);
                    count.classList.toggle("is-none", hits === 0);
                }
                /* Гараж прокручивается — первое совпадение подтягиваем в кадр. */
                if (first && typeof first.scrollIntoView === "function") {
                    first.scrollIntoView({ block: "nearest", inline: "nearest" });
                }
            }

            input.addEventListener("input", function () {
                query = input.value;
                applyEquipmentSearch();
            });
            input.addEventListener("keydown", function (event) {
                if (event.key === "Escape") clearEquipmentSearch();
            });

            function clearEquipmentSearch() {
                input.value = "";
                query = "";
                applyEquipmentSearch();
                if (document.activeElement === input) input.blur();
            }

            /* Набор без клика по полю: цифра или буква, нажатая когда фокус не в
               другом поле ввода и не открыт диалог, уходит в поиск — диспетчер
               просто начинает печатать номер. Backspace стирает так же. */
            function isTypingElsewhere() {
                var active = document.activeElement;
                if (!active || active === document.body || active === input) return false;
                var tag = active.tagName;
                return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || active.isContentEditable;
            }
            function isDialogOpen() {
                if (document.querySelector("dialog[open]")) return true;
                var modal = document.getElementById("app-confirm-modal");
                return !!(modal && !modal.hidden && getComputedStyle(modal).display !== "none");
            }
            document.addEventListener("keydown", function (event) {
                if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
                if (document.activeElement === input || isTypingElsewhere() || isDialogOpen()) return;
                var key = event.key;
                if (key.length === 1 && /[0-9a-zа-яё\-]/i.test(key)) {
                    if (input.maxLength > 0 && input.value.length >= input.maxLength) return;
                    input.value += key;
                } else if (key === "Backspace" && input.value) {
                    input.value = input.value.slice(0, -1);
                } else {
                    return;
                }
                event.preventDefault();
                query = input.value;
                applyEquipmentSearch();
                input.focus({ preventScroll: true });
                input.setSelectionRange(input.value.length, input.value.length);
            });

            /* Клик или захват мышью в любом месте вне поля снимает поиск:
               техника найдена, диспетчер тянет её или работает дальше. */
            document.addEventListener("pointerdown", function (event) {
                if (query === "" || box.contains(event.target)) return;
                clearEquipmentSearch();
            }, true);

            var pending = null;
            var observer = new MutationObserver(function () {
                if (query === "" || pending) return;
                pending = setTimeout(function () {
                    pending = null;
                    applyEquipmentSearch();
                }, 60);
            });
            observer.observe(document.querySelector(".dispatcher-shell") || document.body, { childList: true, subtree: true });
        }
        bindDispatcherEquipmentSearch();

        watchComplexTruckRacks();
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
            bindDragTile(tile);
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
            bindDragTile(tile);
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
            bindDragTile(tile);
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
            bindDragTile(targetCard);
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
            return refreshDispatcherDesktopBoardFromServer();
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
        function bindDragTile(tile) {
            if (tile.dataset.dragBound === "true") return;
            tile.dataset.dragBound = "true";
            tile.addEventListener("dragstart", function (event) {
                if (!dispatcherShiftIsOpen()) {
                    event.preventDefault();
                    draggedTile = null;
                    return;
                }
                if (tile.dataset.complexTruck === "true") event.stopPropagation();
                if (tile.classList.contains("is-assigned") || tile.classList.contains("is-placeholder")) {
                    event.preventDefault();
                    draggedTile = null;
                    return;
                }
                draggedTile = tile;
                tile.classList.add("dispatcher-dragging");
                if (board && tile.dataset.dispatcherDrag === "complex") {
                    board.classList.add("is-complex-dragging");
                    var progress = tile.querySelector(".equipment-progress-complex");
                    if (progress) {
                        dragGhost = progress.cloneNode(true);
                        dragGhost.className = "dispatcher-drag-ghost";
                        document.body.appendChild(dragGhost);
                        event.dataTransfer.setDragImage(dragGhost, 28, 28);
                    }
                }
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", tile.dataset.equipmentName || "");
            });
            tile.addEventListener("dragend", function (event) {
                if (tile.dataset.complexTruck === "true") event.stopPropagation();
                tile.classList.remove("dispatcher-dragging");
                if (board) board.classList.remove("is-complex-dragging");
                clearDragGhost();
                draggedTile = null;
                document.querySelectorAll(".dispatcher-drop-target").forEach(function (target) {
                    target.classList.remove("dispatcher-drop-target");
                });
            });
        }
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
                    if (!zone.dataset.equipmentId) {
                        return;
                    }
                    var assignedTruckTile = draggedTile;
                    var targetComplexCard = zone;
                    dispatcherPost(dispatcherAssignTruckUrl, {
                        action: "assign",
                        truck_id: assignedTruckTile.dataset.equipmentId,
                        excavator_id: zone.dataset.equipmentId,
                        expected_assignment_state_id: haulAssignmentStateId(assignedTruckTile)
                    }).then(function (response) {
                        return applyDesktopTruckAction(response, {
                            type: "assign",
                            truckTile: assignedTruckTile,
                            complexCard: targetComplexCard
                        });
                    }).catch(showDispatcherDnDError);
                    return;
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
                                moveDesktopComplexToExcavatorGarage(inactiveComplexCard);
                            });
                        }).catch(handleDesktopOptimisticBoardError);
                    }
                });
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
                    expected_assignment_state_id: haulAssignmentStateId(releasedTruckTile)
                }).then(function (response) {
                    return applyDesktopTruckAction(response, {
                        type: "release",
                        truckTile: releasedTruckTile
                    });
                }).catch(showDispatcherDnDError);
            });
        }
        function bindDispatcherDesktopInteractions() {
            board = document.querySelector(".dispatcher-board");
            excavatorGarage = document.querySelector("[data-dispatcher-excavator-garage]");
            document.querySelectorAll("[data-dispatcher-drag]").forEach(bindDragTile);
            document.querySelectorAll("[data-equipment-card-id]").forEach(bindEquipmentCardTrigger);
            normalizeComplexGrid();
            refreshExcavatorGarage();
            refreshTruckGarage();
            refreshAllComplexTruckRacks();
            if (!dispatcherShiftIsOpen()) return;
            document.querySelectorAll("[data-dispatcher-drop='complex']").forEach(bindDispatcherComplexDrop);
            document.querySelectorAll("[data-dispatcher-drop='excavator-garage']").forEach(bindDispatcherExcavatorGarageDrop);
            document.querySelectorAll("[data-dispatcher-drop='truck-garage']").forEach(bindDispatcherTruckGarageDrop);
        }

        return {
            bindInteractions: bindDispatcherDesktopInteractions,
            refreshIntegrity: refreshDesktopBoardIntegrity,
            refreshComplexTruckRacks: refreshAllComplexTruckRacks
        };
    }

    global.createDispatcherBoard = createDispatcherBoard;
})(window, document);
