/* Dispatcher equipment detail card.
   Owns detail-card data, rendering, forms and equipment-state presentation.
   Server routes and DOM hooks remain part of the existing dispatcher contract. */
(function (global, document) {
    "use strict";

    function createDispatcherDetail(options) {
        options = options || {};
        var runtimeConfig = options.runtimeConfig || {};
        var staticPrefix = options.staticPrefix || "/static/";
        var getCsrfToken = options.getCsrfToken || function () { return ""; };
        var dispatcherRoleIsReadonly = options.roleIsReadonly || function () { return false; };
        function dispatcherShiftIsOpen() {
            return typeof options.getShiftOpen === "function"
                ? Boolean(options.getShiftOpen())
                : false;
        }

        var equipmentCardsNode = document.getElementById("gd-equipment-cards-data");
        var equipmentCards = equipmentCardsNode ? JSON.parse(equipmentCardsNode.textContent) : {};
        var equipmentStatesNode = document.getElementById("gd-equipment-states-data");
        var equipmentStates = equipmentStatesNode ? JSON.parse(equipmentStatesNode.textContent) : {};
        var detailLayer = document.querySelector("[data-gd-equipment-detail]");
        var detailIconSlot = document.querySelector("[data-gd-detail-icon-slot]");
        var detailType = document.querySelector("[data-gd-detail-type]");
        var detailTitle = document.querySelector("[data-gd-detail-title]");
        var detailStatus = document.querySelector("[data-gd-detail-status]");
        var detailZone = document.querySelector("[data-gd-detail-zone]");
        var detailList = document.querySelector("[data-gd-detail-list]");
        var detailEmployee = document.querySelector("[data-gd-detail-employee]");
        var detailEmployeeImg = document.querySelector("[data-gd-detail-employee-img]");
        var detailEmployeeInitials = document.querySelector("[data-gd-detail-employee-initials]");
        var detailEmployeeName = document.querySelector("[data-gd-detail-employee-name]");
        var detailEmployeePhone = document.querySelector("[data-gd-detail-employee-phone]");
        var detailEmployeePresence = document.querySelector("[data-gd-detail-employee-presence]");
        var detailDowntime = document.querySelector("[data-gd-detail-downtime]");
        var detailDowntimeReason = document.querySelector("[data-gd-detail-downtime-reason]");
        var detailDowntimeStarted = document.querySelector("[data-gd-detail-downtime-started]");
        var detailDowntimeTimer = document.querySelector("[data-gd-detail-downtime-timer]");
        var detailDowntimeClose = document.querySelector("[data-gd-detail-downtime-close]");
        var detailDowntimeResult = document.querySelector("[data-gd-detail-downtime-result]");
        var detailSettings = document.querySelector("[data-gd-detail-settings]");
        var detailSettingsTitle = document.querySelector("[data-gd-detail-settings-title]");
        var detailSettingsHint = document.querySelector("[data-gd-detail-settings-hint]");
        var detailSettingsStatus = document.querySelector("[data-gd-detail-settings-status]");
        var detailSettingHorizon = document.querySelector("[data-gd-setting-horizon]");
        var detailSettingBlock = document.querySelector("[data-gd-setting-block]");
        var detailSettingRock = document.querySelector("[data-gd-setting-rock]");
        var detailDestinationList = document.querySelector("[data-gd-destination-list]");
        var detailDestinationAdd = document.querySelector("[data-gd-destination-add]");
        var detailDestinationCount = document.querySelector("[data-gd-destination-count]");
        var detailSettingSave = document.querySelector("[data-gd-setting-save]");
        var detailDumpPointOptions = [];
        var detailShiftReport = document.querySelector("[data-gd-detail-shift-report]");
        var detailMetrics = document.querySelector("[data-gd-detail-metrics]");
        var detailMeta = document.querySelector("[data-gd-detail-meta]");
        var detailPlanBox = document.querySelector("[data-gd-detail-plan]");
        var detailPlanPercent = document.querySelector("[data-gd-detail-plan-percent]");
        var detailPlanFact = document.querySelector("[data-gd-detail-plan-fact]");
        var detailShiftBox = document.querySelector("[data-gd-detail-shift]");
        var detailShiftType = document.querySelector("[data-gd-detail-shift-type]");
        var detailShiftOpened = document.querySelector("[data-gd-detail-shift-opened]");
        var detailShiftPresence = document.querySelector("[data-gd-detail-shift-presence]");
        var detailShiftSeen = document.querySelector("[data-gd-detail-shift-seen]");
        var detailServiceClose = document.querySelector("[data-gd-detail-service-close]");
        var detailServiceCloseToggle = document.querySelector("[data-gd-detail-service-close-toggle]");
        var detailServiceCloseBody = document.querySelector("[data-gd-detail-service-close-body]");
        var detailServiceCloseCancel = document.querySelector("[data-gd-detail-service-close-cancel]");
        var detailServiceCloseMileage = document.querySelector("[data-gd-detail-service-close-mileage]");
        var detailServiceCloseNeglect = document.querySelector("[data-gd-detail-service-close-neglect]");
        var detailServiceCloseKind = document.querySelector("[data-gd-detail-service-close-kind]");
        var detailShiftAutoClose = document.querySelector("[data-gd-detail-shift-autoclose]");
        var detailServiceCloseHint = document.querySelector("[data-gd-detail-service-close-hint]");
        var detailCrewTitle = document.querySelector("[data-gd-detail-crew-title]");
        var detailShiftVerdict = document.querySelector("[data-gd-detail-shift-verdict]");
        var detailShiftAlert = document.querySelector("[data-gd-detail-shift-alert]");
        var detailShiftPeriod = document.querySelector("[data-gd-detail-shift-period]");
        var detailShiftDuration = document.querySelector("[data-gd-detail-shift-duration]");
        var detailManualTrip = document.querySelector("[data-gd-detail-manual-trip]");
        var detailManualTripHint = document.querySelector("[data-gd-detail-manual-trip-hint]");
        var detailManualTripBlocked = document.querySelector("[data-gd-detail-manual-trip-blocked]");
        var detailManualTripForm = document.querySelector("[data-gd-detail-manual-trip-form]");
        var detailManualTripToggle = document.querySelector("[data-gd-detail-manual-trip-toggle]");
        var detailManualTripBody = document.querySelector("[data-gd-detail-manual-trip-body]");
        var detailManualTripCancel = document.querySelector("[data-gd-detail-manual-trip-cancel]");
        var detailManualTripDump = document.querySelector("[data-gd-detail-manual-trip-dump]");
        var detailManualTripRock = document.querySelector("[data-gd-detail-manual-trip-rock]");
        var detailManualTripTime = document.querySelector("[data-gd-detail-manual-trip-time]");
        var detailScrollHint = document.querySelector("[data-gd-detail-scroll-hint]");
        var detailScrollPanel = detailLayer ? detailLayer.querySelector(".mm-equipment-detail-panel") : null;
        /* План показан крупно в шапке — те же строки в общем списке не повторяем. */
        var DETAIL_PLAN_LABELS = ["Статус плана", "Факт / план", "Выполнение плана", "План смены", "Группа плана"];

        /* Карточка выше окна листается внутри; без подсказки обрез внизу выглядит
           как оторванный блок. Полоска видна, пока есть что листать. */
        function syncDetailScrollHint() {
            if (!detailScrollHint || !detailScrollPanel) return;
            var rest = detailScrollPanel.scrollHeight - detailScrollPanel.clientHeight - detailScrollPanel.scrollTop;
            detailScrollHint.hidden = rest <= 12;
        }
        if (detailScrollPanel) {
            detailScrollPanel.addEventListener("scroll", syncDetailScrollHint, { passive: true });
            window.addEventListener("resize", syncDetailScrollHint);
            if (typeof ResizeObserver === "function") {
                new ResizeObserver(syncDetailScrollHint).observe(detailScrollPanel);
            }
        }
        var detailTrucks = document.querySelector("[data-gd-detail-trucks]");
        var detailTrucksCount = document.querySelector("[data-gd-detail-trucks-count]");
        var detailTrucksList = document.querySelector("[data-gd-detail-trucks-list]");
        var detailTrucksRemoved = document.querySelector("[data-gd-detail-trucks-removed]");
        /* Паспорт техники уходит в строку под именем, сведения о смене — в блок
           машиниста; в общем списке остаётся только то, чему нет своего места. */
        var DETAIL_META_LABELS = ["Экскаватор", "Модель", "ГП, т", "Кузов/ковш, м3", "Гаражный N"];
        var DETAIL_SHIFT_LABELS = ["Смена", "Смена открыта", "Связь", "Последняя связь", "Приложение", "В составе"];
        var detailTabs = document.querySelector("[data-gd-detail-tabs]");
        var detailDashboard = document.querySelector("[data-gd-detail-dashboard]");
        var detailLoadState = document.querySelector("[data-gd-detail-load-state]");
        var detailLoadMessage = document.querySelector("[data-gd-detail-load-message]");
        var detailRetry = document.querySelector("[data-gd-detail-retry]");
        var detailRequestController = null;
        var detailRequestToken = 0;
        var detailRetryAction = null;

        function escapeHtml(value) {
            return String(value || "").replace(/[&<>"']/g, function (char) {
                return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char];
            });
        }

        function formatDispatcherDowntimeDuration(totalSeconds) {
            totalSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
            var hours = Math.floor(totalSeconds / 3600);
            var minutes = Math.floor((totalSeconds % 3600) / 60);
            var seconds = totalSeconds % 60;
            return [hours, minutes, seconds].map(function (value) {
                return String(value).padStart(2, "0");
            }).join(":");
        }
        function updateDispatcherDowntimeTimers() {
            var now = Date.now();
            document.querySelectorAll("[data-gd-downtime-timer][data-started-at]").forEach(function (timer) {
                var startedAt = Date.parse(timer.dataset.startedAt || "");
                if (!Number.isFinite(startedAt)) return;
                timer.textContent = formatDispatcherDowntimeDuration((now - startedAt) / 1000);
            });
        }
        updateDispatcherDowntimeTimers();
        window.setInterval(updateDispatcherDowntimeTimers, 1000);
        function dispatcherEquipmentState(code) {
            var key = code || "inactive";
            return equipmentStates[key] || equipmentStates.inactive || {
                code: key,
                label: "",
                color_group: "gray",
                allows_assignment: false,
                allows_drag: false,
                blocks_operation: true
            };
        }
        function dispatcherEquipmentStateColor(code) {
            var color = dispatcherEquipmentState(code).color_group || "gray";
            return ["green", "yellow", "blue", "orange", "red", "gray"].indexOf(color) >= 0 ? color : "gray";
        }
        function dispatcherEquipmentStateClass(code) {
            return "status-" + dispatcherEquipmentStateColor(code);
        }
        function dispatcherEquipmentStateLabel(code) {
            return dispatcherEquipmentState(code).label || "";
        }
        function dispatcherEquipmentStateIconColor(code) {
            var color = dispatcherEquipmentStateColor(code);
            return color === "orange" ? "yellow" : color;
        }
        function dispatcherNeutralEquipmentIcon(equipmentType) {
            var prefix = equipmentType === "excavator" ? "excavator" : "truck";
            return staticPrefix + "img/equipment/" + prefix + "-gray.png";
        }
        function setDispatcherNodeEquipmentState(node, code, equipmentType) {
            if (!node) return;
            var state = dispatcherEquipmentState(code);
            node.classList.remove("status-red", "status-yellow", "status-green", "status-blue", "status-orange", "status-gray", "status-normal", "status-risk", "status-danger");
            node.classList.add(dispatcherEquipmentStateClass(state.code));
            node.dataset.equipmentState = state.code;
            if (node.dataset.mmMobileEquipmentState !== undefined) {
                node.dataset.mmMobileEquipmentState = state.code;
            }
            var label = node.querySelector("span");
            if (label && state.label) label.textContent = state.label;
            var img = node.querySelector("img");
            if (img && equipmentType) {
                img.src = dispatcherNeutralEquipmentIcon(equipmentType);
            }
        }

        function normalizeTileStatus(status) {
            var directColors = ["green", "yellow", "blue", "orange", "red", "gray"];
            var legacyCodes = {
                normal: "working",
                danger: "breakdown",
                risk: "waiting",
                reserved: "assigned",
                empty: "inactive"
            };
            if (directColors.indexOf(status) >= 0) return status;
            return dispatcherEquipmentStateColor(legacyCodes[status] || status || "inactive");
        }

        // Атрибуты фазы плана — это настройка заливки с доски: на них держатся
        // правила с мягким цветом. Снимешь — копия вернётся к непрозрачной
        // заливке, и подпись состояния на плитке перестанет читаться.
        var DETAIL_TILE_KEEP_DATA = {
            "data-plan-progress-phase": true,
            "data-plan-loop-percent": true,
            "data-plan-completed-loops": true
        };

        function cleanDetailTile(tile) {
            tile.classList.remove("is-assigned", "is-placeholder", "dispatcher-dragging");
            tile.classList.add("gd-detail-slot-clone");
            tile.removeAttribute("id");
            tile.removeAttribute("role");
            tile.removeAttribute("tabindex");
            tile.removeAttribute("draggable");
            Array.from(tile.attributes).forEach(function (attr) {
                if (attr.name.indexOf("data-") === 0 && !DETAIL_TILE_KEEP_DATA[attr.name]) {
                    tile.removeAttribute(attr.name);
                }
            });
            return tile;
        }

        function findSourceGarageTile(cardId) {
            if (!cardId || !window.CSS || !CSS.escape) return null;
            return document.querySelector("[data-equipment-card-id='" + CSS.escape(String(cardId)) + "'][data-garage-item]:not(.is-assigned):not(.is-placeholder)");
        }

        function buildDetailGarageTile(data) {
            var status = normalizeTileStatus(data.status_key);
            var isTruck = String(data.type || "").toLowerCase().indexOf("самосвал") !== -1;
            var plan = data.plan || {};
            var loopProgress = plan.progress_loop_percent;
            var completedLoops = Number(plan.progress_completed_loops || 0);
            var tile = document.createElement("article");
            tile.className = isTruck
                ? "dispatcher-truck-tile status-" + status
                : "dispatcher-equipment-tile dispatcher-excavator-garage-tile status-" + status;
            if (completedLoops > 0) tile.classList.add("is-plan-overrun");
            tile.style.setProperty("--tile-progress", String(loopProgress === null || loopProgress === undefined || loopProgress === "" ? data.percent || 0 : loopProgress) + "%");
            tile.style.setProperty("--tile-total-progress", String(data.percent || 0) + "%");
            if (loopProgress !== null && loopProgress !== undefined && loopProgress !== "") tile.dataset.planLoopPercent = String(loopProgress);
            tile.dataset.planCompletedLoops = String(completedLoops);
            /* Пустая фаза = рейсов по смене нет. Атрибут надо именно снять:
               иначе на перерисованной плитке останется заливка от прошлого
               обновления и пустой самосвал будет выглядеть работающим. */
            if (plan.progress_phase) tile.dataset.planProgressPhase = plan.progress_phase;
            else tile.removeAttribute("data-plan-progress-phase");
            tile.innerHTML =
                "<strong>" + escapeHtml(data.number || "") + "</strong>" +
                '<img src="' + escapeHtml(dispatcherNeutralEquipmentIcon(isTruck ? "truck" : "excavator")) + '" alt="">' +
                "<span>" + escapeHtml(data.status_label || "") + "</span>" +
                (completedLoops > 0 ? '<b class="dispatcher-plan-loop-badge" aria-label="Завершено циклов: ' + completedLoops + '">×' + completedLoops + '</b>' : "");
            return tile;
        }

        function renderDetailGarageIcon(cardId, data) {
            if (!detailIconSlot) return;
            detailIconSlot.innerHTML = "";
            var source = findSourceGarageTile(cardId);
            var tile = source ? source.cloneNode(true) : buildDetailGarageTile(data);
            detailIconSlot.appendChild(cleanDetailTile(tile));
        }

        function buildDetailChartShell(chart) {
            var card = document.createElement("div");
            card.className = "gd-detail-chart-card gd-detail-chart-" + (chart.type || "bar");
            var title = document.createElement("div");
            title.className = "gd-detail-report-title";
            title.textContent = chart.title || "";
            card.appendChild(title);
            if (chart.summary) {
                var summary = document.createElement("div");
                summary.className = "gd-detail-report-summary";
                summary.textContent = chart.summary;
                card.appendChild(summary);
            }
            if (chart.type === "donut-list") {
                return card;
            }
            var gauges = document.createElement("div");
            gauges.className = "gd-detail-gauge-strip";
            chart.rows.slice(0, 3).forEach(function (row) {
                var item = document.createElement("div");
                item.className = "gd-detail-gauge-summary accent-" + (row.accent || "green");
                item.style.setProperty("--gauge-pct", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
                item.innerHTML =
                    "<div class=\"gd-detail-gauge\"><strong>" + escapeHtml(row.value || "") + "</strong></div>" +
                    "<span>" + escapeHtml(row.target || row.label || row.source || "") + "</span>";
                gauges.appendChild(item);
            });
            card.appendChild(gauges);
            return card;
        }

        function renderDetailChart(chart) {
            if (!detailDashboard) return;
            detailDashboard.innerHTML = "";
            if (!chart || !chart.rows || !chart.rows.length) return;
            var card = buildDetailChartShell(chart);
            if (chart.type === "matrix") {
                var groupedRows = {};
                chart.rows.forEach(function (row) {
                    var key = row.label || "не указан";
                    if (!groupedRows[key]) {
                        groupedRows[key] = [];
                    }
                    groupedRows[key].push(row);
                });
                Object.keys(groupedRows).forEach(function (label) {
                    var rows = groupedRows[label];
                    var matrix = document.createElement("div");
                    matrix.className = "gd-detail-matrix-row is-grouped";
                    var face = document.createElement("div");
                    face.className = "gd-detail-matrix-face";
                    face.textContent = label;
                    var cell = document.createElement("div");
                    cell.className = "gd-detail-matrix-cell gd-detail-matrix-pie-cell";
                    var pie = document.createElement("div");
                    pie.className = "gd-detail-pie";
                    var cursor = 0;
                    var totalPercent = rows.reduce(function (sum, row) {
                        return sum + Math.max(0, Number(row.percent || 0));
                    }, 0) || 100;
                    var stops = rows.map(function (row) {
                        var raw = Math.max(0, Number(row.percent || 0));
                        var size = Math.max(4, Math.min(100, (raw / totalPercent) * 100));
                        var start = cursor;
                        cursor += size;
                        return "var(--pie-" + (row.accent || "green") + ") " + start + "% " + cursor + "%";
                    });
                    if (cursor < 100) {
                        stops.push("rgba(142, 158, 166, .16) " + cursor + "% 100%");
                    }
                    pie.style.backgroundImage = "radial-gradient(circle at center, var(--gd-detail-panel) 0 50%, transparent 51%), conic-gradient(" + stops.join(", ") + ")";
                    var total = document.createElement("strong");
                    total.textContent = rows.length + " напр.";
                    pie.appendChild(total);
                    var legend = document.createElement("div");
                    legend.className = "gd-detail-pie-legend";
                    rows.forEach(function (row) {
                        var item = document.createElement("div");
                        item.className = "gd-detail-pie-item accent-" + (row.accent || "green");
                        item.innerHTML = "<span></span><strong>" + escapeHtml(row.target || "") + "</strong><em>" + escapeHtml(row.value || "") + "</em><small>" + escapeHtml(row.meta || "") + "</small>";
                        legend.appendChild(item);
                    });
                    cell.appendChild(pie);
                    cell.appendChild(legend);
                    matrix.appendChild(face);
                    matrix.appendChild(cell);
                    card.appendChild(matrix);
                });
                detailDashboard.appendChild(card);
                return;
            }
            if (chart.type === "donut-list") {
                var breakdown = document.createElement("div");
                breakdown.className = "gd-detail-breakdown";
                var stack = document.createElement("div");
                stack.className = "gd-detail-stack";
                var totalPercent = chart.rows.reduce(function (sum, row) {
                    return sum + Math.max(0, Number(row.percent || 0));
                }, 0) || 100;
                chart.rows.forEach(function (row) {
                    var segment = document.createElement("i");
                    segment.className = "accent-" + (row.accent || "green");
                    segment.style.setProperty("--segment-share", Math.max(4, Math.min(100, (Math.max(0, Number(row.percent || 0)) / totalPercent) * 100)) + "%");
                    stack.appendChild(segment);
                });
                breakdown.appendChild(stack);
                var donutGrid = document.createElement("div");
                donutGrid.className = "gd-detail-breakdown-grid";
                chart.rows.forEach(function (row) {
                    var item = document.createElement("div");
                    item.className = "gd-detail-breakdown-row accent-" + (row.accent || "green");
                    item.style.setProperty("--bar-pct", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
                    item.innerHTML =
                        "<div class=\"gd-detail-breakdown-mark\"></div>" +
                        "<div class=\"gd-detail-breakdown-main\"><strong>" + escapeHtml(row.label || "") + "</strong><span>" + escapeHtml(row.meta || "") + "</span><em><i></i></em></div>" +
                        "<b>" + escapeHtml(row.value || "") + "</b>";
                    donutGrid.appendChild(item);
                });
                breakdown.appendChild(donutGrid);
                card.appendChild(breakdown);
                detailDashboard.appendChild(card);
                return;
            }
            if (chart.type === "truck-ledger") {
                var ledger = document.createElement("div");
                ledger.className = "gd-detail-truck-ledger";
                ["current", "removed"].forEach(function (stateKey) {
                    var stateRows = chart.rows.filter(function (row) { return row.state_key === stateKey; });
                    if (!stateRows.length) return;
                    var group = document.createElement("div");
                    group.className = "gd-detail-truck-group is-" + stateKey;
                    var groupTitle = document.createElement("strong");
                    groupTitle.textContent = stateKey === "current" ? "В составе сейчас" : "Работали и выведены";
                    group.appendChild(groupTitle);
                    stateRows.forEach(function (row) {
                        var item = document.createElement("div");
                        item.className = "gd-detail-truck-ledger-row accent-" + (row.accent || "green");
                        item.style.setProperty("--tile-progress", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
                        item.innerHTML =
                            "<div class=\"gd-detail-truck-mini\"><b>" + escapeHtml(row.truck || row.label || "") + "</b><span>" + escapeHtml(row.state || "") + "</span></div>" +
                            "<div class=\"gd-detail-truck-route\"><strong>" + escapeHtml(row.target || "") + "</strong><span>" + escapeHtml(row.rock || "") + "</span><em><i></i></em></div>" +
                            "<div class=\"gd-detail-truck-value\">" + escapeHtml(row.value || "") + "</div>";
                        group.appendChild(item);
                    });
                    ledger.appendChild(group);
                });
                card.appendChild(ledger);
                detailDashboard.appendChild(card);
                return;
            }
            chart.rows.forEach(function (row) {
                if (chart.type === "route") {
                    var route = document.createElement("div");
                    route.className = "gd-detail-route-row accent-" + (row.accent || "green");
                    route.style.setProperty("--gauge-pct", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
                    var source = document.createElement("div");
                    source.className = "gd-detail-route-node";
                    source.textContent = row.source || "";
                    var flow = document.createElement("div");
                    flow.className = "gd-detail-route-flow";
                    flow.innerHTML = "<i></i>";
                    var target = document.createElement("div");
                    target.className = "gd-detail-route-node";
                    target.textContent = row.target || "";
                    var gauge = document.createElement("div");
                    gauge.className = "gd-detail-gauge";
                    gauge.innerHTML = "<strong>" + escapeHtml(row.value || "") + "</strong>";
                    var meta = document.createElement("div");
                    meta.className = "gd-detail-route-meta";
                    meta.textContent = row.meta || "";
                    route.appendChild(source);
                    route.appendChild(flow);
                    route.appendChild(target);
                    route.appendChild(gauge);
                    route.appendChild(meta);
                    card.appendChild(route);
                    return;
                }
                var line = document.createElement("div");
                line.className = "gd-detail-chart-row accent-" + (row.accent || "green");
                line.style.setProperty("--bar-pct", Math.max(0, Math.min(100, Number(row.percent || 0))) + "%");
                var head = document.createElement("div");
                head.className = "gd-detail-chart-head";
                var label = document.createElement("strong");
                var value = document.createElement("span");
                label.textContent = row.label || "";
                value.textContent = row.value || "";
                head.appendChild(label);
                head.appendChild(value);
                var meta = document.createElement("div");
                meta.className = "gd-detail-chart-meta";
                meta.textContent = row.meta || "";
                var bar = document.createElement("div");
                bar.className = "gd-detail-chart-bar";
                bar.appendChild(document.createElement("i"));
                line.appendChild(head);
                line.appendChild(meta);
                line.appendChild(bar);
                card.appendChild(line);
            });
            detailDashboard.appendChild(card);
        }

        function renderDetailShiftReport(report) {
            if (!detailShiftReport || !detailMetrics || !detailTabs || !detailDashboard) return;
            var metrics = (report && report.metrics) || [];
            var charts = ((report && report.charts) || []).filter(function (chart) {
                return chart && chart.rows && chart.rows.length;
            });
            detailMetrics.innerHTML = "";
            detailTabs.innerHTML = "";
            detailDashboard.innerHTML = "";
            metrics.forEach(function (metric) {
                if (!metric || !metric.value) return;
                var item = document.createElement("div");
                var label = document.createElement("span");
                var value = document.createElement("strong");
                label.textContent = metric.label || "";
                value.textContent = metric.value || "";
                item.appendChild(label);
                item.appendChild(value);
                detailMetrics.appendChild(item);
            });
            charts.forEach(function (chart, index) {
                var button = document.createElement("button");
                button.type = "button";
                button.className = "gd-detail-tab" + (index === 0 ? " is-active" : "");
                button.textContent = chart.title || ("Отчет " + (index + 1));
                button.addEventListener("click", function () {
                    var panel = detailLayer ? detailLayer.querySelector(".mm-equipment-detail-panel") : null;
                    var savedScrollTop = panel ? panel.scrollTop : 0;
                    detailTabs.querySelectorAll(".gd-detail-tab").forEach(function (node) {
                        node.classList.remove("is-active");
                    });
                    button.classList.add("is-active");
                    renderDetailChart(chart);
                    if (panel) {
                        panel.scrollTop = savedScrollTop;
                    }
                });
                detailTabs.appendChild(button);
            });
            renderDetailChart(charts[0]);
            detailTabs.hidden = charts.length < 2;
            detailShiftReport.hidden = metrics.length === 0 && charts.length === 0;
        }

        function currentDispatcherBoardVersion() {
            var currentBoard = document.querySelector(".dispatcher-board");
            var parsed = Number(currentBoard && currentBoard.dataset
                ? currentBoard.dataset.operationalStateVersion
                : 0);
            return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
        }

        function setDetailLoadState(message, canRetry) {
            if (detailLoadMessage) detailLoadMessage.textContent = message || "";
            if (detailRetry) detailRetry.hidden = !canRetry;
            if (detailLoadState) detailLoadState.hidden = !message;
        }

        function resetDetailContent() {
            if (detailEmployee) detailEmployee.hidden = true;
            if (detailDowntime) detailDowntime.hidden = true;
            if (detailDowntimeTimer) detailDowntimeTimer.removeAttribute("data-started-at");
            if (detailDowntimeResult) detailDowntimeResult.textContent = "";
            if (detailDowntimeClose) detailDowntimeClose.disabled = false;
            if (detailLayer) delete detailLayer.dataset.gdDowntimeEventId;
            if (detailSettings) detailSettings.hidden = true;
            if (detailSettingsStatus) detailSettingsStatus.textContent = "";
            if (detailList) detailList.innerHTML = "";
            if (detailShiftReport) detailShiftReport.hidden = true;
            if (detailMeta) detailMeta.textContent = "";
            if (detailPlanBox) detailPlanBox.hidden = true;
            if (detailShiftBox) detailShiftBox.hidden = true;
            if (detailTrucks) detailTrucks.hidden = true;
            if (detailShiftAlert) detailShiftAlert.hidden = true;
            if (detailShiftVerdict) detailShiftVerdict.hidden = true;
            if (detailManualTrip) detailManualTrip.hidden = true;
            closeDetailManualTripForm();
            closeDetailServiceCloseForm();
            if (detailServiceClose) {
                detailServiceClose.hidden = true;
                detailServiceClose.removeAttribute("action");
            }
        }

        function closeDetailServiceCloseForm() {
            if (!detailServiceClose) return;
            detailServiceClose.classList.remove("is-open");
            if (detailServiceCloseBody) detailServiceCloseBody.hidden = true;
            Array.prototype.forEach.call(detailServiceClose.querySelectorAll("input:not([type=hidden])"), function (input) {
                input.value = "";
            });
        }

        function openDetailServiceCloseForm() {
            if (!detailServiceClose || !detailServiceCloseToggle || detailServiceCloseToggle.disabled) return;
            detailServiceClose.classList.add("is-open");
            if (detailServiceCloseBody) detailServiceCloseBody.hidden = false;
            var reason = detailServiceClose.querySelector("[name=reason]");
            if (reason) reason.focus();
        }

        /* Смена машиниста/водителя: сведения + служебное завершение. Форма
           обычная: POST на dispatcher_service_close_shift и редирект с сообщением,
           тот же путь, что у «Незакрытых смен» в журнале. */
        function renderDetailShift(shift, employee) {
            var presenceNode = detailEmployeePresence;
            var presenceStatus = (shift && shift.presence_status) || (employee && employee.presence_status) || "";
            if (presenceNode) {
                presenceNode.className = "gd-detail-crew-presence" + (presenceStatus ? " is-" + presenceStatus : "");
            }
            if (!shift) {
                if (detailShiftBox) detailShiftBox.hidden = true;
                if (detailServiceClose) detailServiceClose.hidden = true;
                if (detailShiftAlert) detailShiftAlert.hidden = true;
                if (detailShiftVerdict) detailShiftVerdict.hidden = true;
                return;
            }
            if (detailShiftType) detailShiftType.textContent = shift.type_label || "";
            if (detailShiftOpened) detailShiftOpened.textContent = shift.opened_at_label || "";
            if (detailShiftPresence) detailShiftPresence.textContent = shift.presence_label || "";
            if (detailShiftSeen) detailShiftSeen.textContent = shift.last_seen_label || "";
            if (detailShiftPeriod) detailShiftPeriod.textContent = shift.period_label || shift.type_label || "";
            if (detailShiftDuration) detailShiftDuration.textContent = shift.duration_label || "";
            /* Текущая смена — зелёная метка; хвост прошлой смены — красная и
               развёрнутое предупреждение: диспетчер сразу видит, что водитель
               прошлой смены не закрыл её, а не гадает по дате открытия. */
            if (detailShiftVerdict) {
                var verdict = shift.verdict || "";
                detailShiftVerdict.className = "gd-detail-shift-verdict" + (verdict ? " is-" + verdict : "");
                detailShiftVerdict.textContent = shift.verdict_label || "";
                detailShiftVerdict.hidden = !shift.verdict_label;
            }
            if (detailShiftAlert) {
                detailShiftAlert.textContent = shift.alert || "";
                detailShiftAlert.className = "gd-detail-shift-alert" + (shift.verdict ? " is-" + shift.verdict : "");
                detailShiftAlert.hidden = !shift.alert;
            }
            if (detailShiftBox) detailShiftBox.hidden = false;
            if (detailServiceClose) {
                closeDetailServiceCloseForm();
                detailServiceClose.hidden = !shift.service_close_url;
                if (shift.service_close_url) detailServiceClose.setAttribute("action", shift.service_close_url);
                if (detailServiceCloseMileage) {
                    detailServiceCloseMileage.hidden = !shift.is_truck;
                    var mileage = detailServiceCloseMileage.querySelector("input");
                    if (mileage) mileage.required = false;
                }
                var closeLocked = dispatcherRoleIsReadonly() || !dispatcherShiftIsOpen();
                if (detailServiceCloseToggle) detailServiceCloseToggle.disabled = closeLocked;
                if (detailServiceCloseNeglect) detailServiceCloseNeglect.disabled = closeLocked;
                if (detailShiftAutoClose) detailShiftAutoClose.textContent = shift.auto_close_at_label || "—";
                renderDetailShiftReadingBounds(shift);
            }
        }

        /* Подсказка по показаниям: сервер требует целые числа, моточасы не меньше
           начальных и не больше +12 за смену — говорим это до отправки, а не после. */
        function renderDetailShiftReadingBounds(shift) {
            if (!detailServiceClose) return;
            var hours = detailServiceClose.querySelector("[name=end_engine_hours]");
            var mileage = detailServiceClose.querySelector("[name=end_mileage]");
            var startHours = shift && shift.start_engine_hours ? Number(String(shift.start_engine_hours).replace(",", ".")) : NaN;
            var startMileage = shift && shift.start_mileage ? Number(String(shift.start_mileage).replace(",", ".")) : NaN;
            if (hours) {
                if (!shift.is_truck && isFinite(startHours)) {
                    hours.min = String(Math.round(startHours));
                    hours.max = String(Math.round(startHours) + 12);
                } else {
                    hours.min = "0";
                    hours.removeAttribute("max");
                }
            }
            if (mileage) {
                mileage.min = shift.is_truck && isFinite(startMileage) ? String(Math.floor(startMileage)) : "0";
            }
            if (!detailServiceCloseHint) return;
            var parts = [];
            if (shift.start_fuel) parts.push("топливо " + shift.start_fuel + " л");
            if (shift.is_truck && shift.start_mileage) parts.push("одометр " + shift.start_mileage + " км");
            if (shift.start_engine_hours) parts.push("моточасы " + shift.start_engine_hours);
            var text = parts.length ? "На начало смены: " + parts.join(" · ") + ". " : "";
            text += shift.is_truck
                ? "Если показания известны — целые числа; иначе оставьте пустыми."
                : "Если показания известны — целые числа, моточасы не меньше начальных и не более +12; иначе оставьте пустыми.";
            detailServiceCloseHint.textContent = text;
            detailServiceCloseHint.hidden = false;
        }

        /* Выполнение плана крупно в шапке: одна цифра, которую диспетчер ищет
           первой; факт/план и группа — строкой под ней. */
        function renderDetailPlan(plan) {
            if (!detailPlanBox) return;
            var hasPlan = plan && plan.progress_percent !== null && plan.progress_percent !== undefined;
            if (!plan || (!hasPlan && !plan.plan_status_label)) {
                detailPlanBox.hidden = true;
                return;
            }
            detailPlanBox.hidden = false;
            detailPlanBox.classList.toggle("is-muted", !hasPlan);
            if (detailPlanPercent) detailPlanPercent.textContent = hasPlan ? String(plan.progress_percent) + "%" : "—";
            if (detailPlanFact) {
                detailPlanFact.textContent = hasPlan
                    ? [plan.fact_plan_label, plan.plan_group_name].filter(Boolean).join(" · ")
                    : (plan.plan_status_label || "");
            }
        }

        function closeDetailManualTripForm() {
            if (!detailManualTripForm) return;
            detailManualTripForm.classList.remove("is-open");
            if (detailManualTripBody) detailManualTripBody.hidden = true;
            var reason = detailManualTripForm.querySelector("[name=reason]");
            if (reason) reason.value = "";
            if (detailManualTripTime) detailManualTripTime.value = "";
        }

        function detailLocalDateTimeValue(date) {
            var pad = function (value) { return (value < 10 ? "0" : "") + value; };
            return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
                + "T" + pad(date.getHours()) + ":" + pad(date.getMinutes());
        }

        function openDetailManualTripForm() {
            if (!detailManualTripForm || detailManualTripForm.hidden) return;
            detailManualTripForm.classList.add("is-open");
            if (detailManualTripBody) detailManualTripBody.hidden = false;
            if (detailManualTripTime) detailManualTripTime.max = detailLocalDateTimeValue(new Date());
            var reason = detailManualTripForm.querySelector("[name=reason]");
            if (reason) reason.focus();
        }

        function appendDetailOption(parent, value, label, selected) {
            var option = document.createElement("option");
            option.value = String(value);
            option.textContent = label;
            if (selected) option.selected = true;
            parent.appendChild(option);
        }

        /* Ручной рейс: сервер отдаёт точки забоя с плечом и остальные активные
           точки, породу по умолчанию из настроек экскаватора. Отправка — обычной
           формой на dispatcher_manual_trip с подтверждением. */
        function renderDetailManualTrip(manual) {
            if (!detailManualTrip) return;
            closeDetailManualTripForm();
            if (!manual) {
                detailManualTrip.hidden = true;
                return;
            }
            detailManualTrip.hidden = false;
            if (detailManualTripHint) {
                detailManualTripHint.textContent = manual.excavator_label
                    ? "На экскаватор " + manual.excavator_label + " от имени водителя открытой смены."
                    : "";
            }
            var blocked = manual.blocked_reason || "";
            if (!blocked && (dispatcherRoleIsReadonly() || !dispatcherShiftIsOpen())) {
                blocked = "Нужна открытая смена диспетчера.";
            }
            if (detailManualTripBlocked) {
                detailManualTripBlocked.textContent = blocked;
                detailManualTripBlocked.hidden = !blocked;
            }
            if (detailManualTripForm) {
                detailManualTripForm.hidden = !!blocked || !manual.url;
                if (manual.url) detailManualTripForm.setAttribute("action", manual.url);
                else detailManualTripForm.removeAttribute("action");
                var excavatorInput = detailManualTripForm.querySelector("[name=excavator_id]");
                if (excavatorInput) excavatorInput.value = manual.excavator_id || "";
                var countInput = detailManualTripForm.querySelector("[name=trips_count]");
                if (countInput) {
                    countInput.max = String(manual.max_count || 10);
                    countInput.value = "1";
                }
            }
            if (detailManualTripDump) {
                detailManualTripDump.innerHTML = "";
                var known = {};
                (manual.destinations || []).forEach(function (row, index) {
                    known[String(row.dump_point_id)] = true;
                    var distance = row.transport_distance_km ? " · " + String(row.transport_distance_km).replace(".", ",") + " км" : "";
                    appendDetailOption(detailManualTripDump, row.dump_point_id, row.name + distance, index === 0);
                });
                var others = (manual.dump_points || []).filter(function (point) { return !known[String(point.id)]; });
                if (others.length) {
                    var group = document.createElement("optgroup");
                    group.label = (manual.destinations || []).length ? "Другие точки" : "Точки разгрузки";
                    others.forEach(function (point) { appendDetailOption(group, point.id, point.name, false); });
                    detailManualTripDump.appendChild(group);
                }
            }
            if (detailManualTripRock) {
                detailManualTripRock.innerHTML = "";
                (manual.rock_types || []).forEach(function (rock) {
                    appendDetailOption(detailManualTripRock, rock.id, rock.name, String(manual.rock_type_id || "") === String(rock.id));
                });
            }
        }

        function renderDetailTruckChips(container, numbers) {
            if (!container) return;
            container.innerHTML = "";
            (numbers || []).forEach(function (number) {
                var chip = document.createElement("span");
                chip.textContent = String(number);
                container.appendChild(chip);
            });
        }

        function renderDetailTrucks(data) {
            if (!detailTrucks) return;
            var report = data.shift_report || {};
            var current = Array.isArray(report.current_trucks) ? report.current_trucks : [];
            var removed = Array.isArray(report.removed_trucks) ? report.removed_trucks : [];
            if (data.category !== "complex" || (!current.length && !removed.length)) {
                detailTrucks.hidden = true;
                return;
            }
            renderDetailTruckChips(detailTrucksList, current);
            if (!current.length && detailTrucksList) {
                var empty = document.createElement("em");
                empty.textContent = "самосвалы не назначены";
                detailTrucksList.appendChild(empty);
            }
            renderDetailTruckChips(detailTrucksRemoved, removed);
            if (detailTrucksRemoved) {
                detailTrucksRemoved.hidden = !removed.length;
                if (removed.length) {
                    var note = document.createElement("em");
                    note.textContent = "выведены за смену";
                    detailTrucksRemoved.insertBefore(note, detailTrucksRemoved.firstChild);
                }
            }
            if (detailTrucksCount) {
                detailTrucksCount.textContent = current.length
                    ? current.length + " " + (current.length === 1 ? "машина" : current.length < 5 ? "машины" : "машин")
                    : "";
            }
            detailTrucks.hidden = false;
        }

        function renderDetailDowntime(data) {
            var downtime = data || {};
            if (!detailDowntime || !downtime.active) {
                if (detailDowntime) detailDowntime.hidden = true;
                if (detailLayer) delete detailLayer.dataset.gdDowntimeEventId;
                return;
            }
            if (detailLayer) detailLayer.dataset.gdDowntimeEventId = String(downtime.event_id || "");
            if (detailDowntimeReason) detailDowntimeReason.textContent = downtime.reason || "Простой";
            if (detailDowntimeStarted) {
                detailDowntimeStarted.textContent = downtime.started_at_label
                    ? "С начала: " + downtime.started_at_label
                    : "";
            }
            if (detailDowntimeTimer) {
                detailDowntimeTimer.dataset.startedAt = downtime.started_at || "";
                detailDowntimeTimer.textContent = downtime.elapsed_label || "00:00:00";
            }
            if (detailDowntimeResult) detailDowntimeResult.textContent = "";
            if (detailDowntimeClose) {
                detailDowntimeClose.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftIsOpen();
                detailDowntimeClose.textContent = "Завершить простой";
            }
            detailDowntime.hidden = false;
            updateDispatcherDowntimeTimers();
        }

        function dispatcherDowntimeCloseUrl(eventId) {
            var template = String(runtimeConfig.dispatcherDowntimeCloseUrlTemplate || "");
            if (!/^\d+$/.test(String(eventId || "")) || !template) return "";
            var path = template.replace(/0\/close\/$/, String(eventId) + "/close/");
            return path === template ? "" : path;
        }

        function dispatcherDowntimeCloseError(error) {
            if (error && error.status === 401) return "Сессия завершена. Войдите в систему снова.";
            if (error && error.status === 403) return "Нет доступа к завершению простоя.";
            if (error && error.code === "dispatcher_shift_required") return "Смена горного диспетчера закрыта.";
            if (error && error.code === "inactive_role") return "Роль неактивна — доступен только просмотр.";
            if (error && error.status === 409) return "Состояние техники уже изменилось. Карточка будет обновлена.";
            if (error && error.status === 404) return "Этот простой больше не найден.";
            return "Не удалось завершить простой. Проверьте связь и повторите действие.";
        }

        function closeDetailDowntime() {
            if (!detailLayer || !detailDowntimeClose || detailDowntimeClose.disabled) return;
            var eventId = detailLayer.dataset.gdDowntimeEventId || "";
            var url = dispatcherDowntimeCloseUrl(eventId);
            var requestedVersion = Number(detailLayer.dataset.gdRequestedVersion || -1);
            if (!url || requestedVersion < 0) {
                if (detailDowntimeResult) detailDowntimeResult.textContent = "Карточка устарела. Откройте её снова.";
                return;
            }
            detailDowntimeClose.disabled = true;
            detailDowntimeClose.textContent = "Завершаю…";
            if (detailDowntimeResult) detailDowntimeResult.textContent = "";
            fetch(url, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCsrfToken(),
                    "X-Requested-With": "XMLHttpRequest"
                },
                body: JSON.stringify({state_version: requestedVersion})
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    if (!response.ok || !payload.ok) {
                        var requestError = new Error("downtime_close_failed");
                        requestError.status = response.status;
                        requestError.code = payload.error || "";
                        throw requestError;
                    }
                    return payload;
                });
            }).then(function () {
                if (detailDowntimeResult) detailDowntimeResult.textContent = "Простой завершён. Обновляем пульт…";
                detailDowntimeClose.textContent = "Простой завершён";
                if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                    window.AppRealtime.wake("dispatcher_downtime_closed");
                }
                window.setTimeout(closeEquipmentCard, 500);
            }).catch(function (error) {
                if (detailDowntimeResult) detailDowntimeResult.textContent = dispatcherDowntimeCloseError(error);
                detailDowntimeClose.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftIsOpen();
                detailDowntimeClose.textContent = "Завершить простой";
                if (error && error.status === 409 && window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                    window.AppRealtime.wake("dispatcher_downtime_stale");
                }
            });
        }

        function requestDetailDowntimeClose() {
            if (!detailLayer || !detailDowntimeClose || detailDowntimeClose.disabled) return;
            var card = equipmentCards[String(detailLayer.dataset.gdActiveCardId || "")] || {};
            var downtime = card.downtime || {};
            var equipmentLabel = card.label || "техника";
            var duration = detailDowntimeTimer ? detailDowntimeTimer.textContent : downtime.elapsed_label;
            var message = "Завершить простой " + equipmentLabel + " «" + (downtime.reason || "Простой") + "»? Длительность " + (duration || "00:00:00") + " будет зафиксирована в отчёте.";
            if (typeof window.openAppConfirmDialog !== "function") {
                if (detailDowntimeResult) detailDowntimeResult.textContent = "Подтверждение недоступно. Обновите страницу.";
                return;
            }
            window.openAppConfirmDialog(message, closeDetailDowntime, 0, "Завершить", {
                confirmTitle: "Завершить простой?",
                confirmDescription: message
            });
        }

        function fillDetailSettingSelect(select, options, selectedId) {
            if (!select) return;
            select.innerHTML = "";
            var placeholder = document.createElement("option");
            placeholder.value = "";
            placeholder.textContent = "Выберите";
            select.appendChild(placeholder);
            (options || []).forEach(function (item) {
                var option = document.createElement("option");
                option.value = String(item.id || "");
                option.textContent = item.name || "";
                option.selected = String(item.id || "") === String(selectedId || "");
                select.appendChild(option);
            });
        }

        function detailDestinationRows() {
            return detailDestinationList
                ? Array.prototype.slice.call(detailDestinationList.querySelectorAll("[data-gd-destination-row]"))
                : [];
        }

        function refreshDetailDestinationRows() {
            var rows = detailDestinationRows();
            var selectedIds = rows.map(function (row) {
                var select = row.querySelector("[data-gd-destination-select]");
                return select ? String(select.value || "") : "";
            }).filter(Boolean);
            rows.forEach(function (row) {
                var select = row.querySelector("[data-gd-destination-select]");
                var remove = row.querySelector("[data-gd-destination-remove]");
                if (select) {
                    Array.prototype.forEach.call(select.options, function (option) {
                        option.disabled = !!option.value
                            && option.value !== select.value
                            && selectedIds.indexOf(String(option.value)) !== -1;
                    });
                }
                if (remove) remove.disabled = rows.length <= 1;
            });
            if (detailDestinationCount) {
                detailDestinationCount.textContent = rows.length
                    ? rows.length + " " + (rows.length === 1 ? "точка" : rows.length < 5 ? "точки" : "точек")
                    : "не назначены";
            }
            if (detailDestinationAdd) {
                detailDestinationAdd.disabled = dispatcherRoleIsReadonly()
                    || !dispatcherShiftIsOpen()
                    || rows.length >= detailDumpPointOptions.length;
            }
        }

        function addDetailDestinationRow(destination) {
            if (!detailDestinationList) return;
            var row = document.createElement("div");
            row.className = "gd-detail-destination-row";
            row.setAttribute("data-gd-destination-row", "");

            var selectLabel = document.createElement("label");
            var selectCaption = document.createElement("span");
            selectCaption.textContent = "Точка";
            var select = document.createElement("select");
            select.setAttribute("data-gd-destination-select", "");
            fillDetailSettingSelect(select, detailDumpPointOptions, destination && destination.dump_point_id);
            selectLabel.appendChild(selectCaption);
            selectLabel.appendChild(select);

            var distanceLabel = document.createElement("label");
            distanceLabel.className = "gd-detail-destination-distance";
            var distanceCaption = document.createElement("span");
            distanceCaption.textContent = "Плечо, км";
            var distance = document.createElement("input");
            distance.type = "text";
            distance.inputMode = "decimal";
            distance.maxLength = 12;
            distance.placeholder = "—";
            distance.value = String(destination && destination.transport_distance_km || "").replace(".", ",");
            distance.setAttribute("data-gd-destination-distance", "");
            distanceLabel.appendChild(distanceCaption);
            distanceLabel.appendChild(distance);

            var remove = document.createElement("button");
            remove.type = "button";
            remove.className = "gd-detail-destination-remove";
            remove.setAttribute("data-gd-destination-remove", "");
            remove.setAttribute("aria-label", "Убрать точку разгрузки");
            remove.textContent = "×";

            select.addEventListener("change", refreshDetailDestinationRows);
            remove.addEventListener("click", function () {
                row.remove();
                refreshDetailDestinationRows();
            });
            row.appendChild(selectLabel);
            row.appendChild(distanceLabel);
            row.appendChild(remove);
            detailDestinationList.appendChild(row);
            refreshDetailDestinationRows();
        }

        function collectDetailDestinations() {
            var seen = Object.create(null);
            var destinations = [];
            detailDestinationRows().forEach(function (row) {
                var select = row.querySelector("[data-gd-destination-select]");
                var distance = row.querySelector("[data-gd-destination-distance]");
                var id = select ? String(select.value || "") : "";
                if (!id || seen[id]) return;
                seen[id] = true;
                destinations.push({
                    dump_point_id: id,
                    transport_distance_km: distance ? distance.value : ""
                });
            });
            return destinations;
        }

        function renderDetailSettings(settings) {
            if (!detailSettings) return;
            if (!settings || !settings.editable) {
                detailSettings.hidden = true;
                return;
            }
            detailSettings.hidden = false;
            if (detailSettingsTitle) detailSettingsTitle.textContent = settings.title || "Рабочие параметры комплекса";
            if (detailSettingsHint) detailSettingsHint.textContent = settings.hint || "";
            if (detailSettingsStatus) detailSettingsStatus.textContent = "";
            if (detailSettingHorizon) detailSettingHorizon.value = settings.loading_horizon || "";
            if (detailSettingBlock) detailSettingBlock.value = settings.loading_block || "";
            fillDetailSettingSelect(detailSettingRock, settings.rock_types, settings.rock_type_id);
            detailDumpPointOptions = settings.dump_points || [];
            if (detailDestinationList) detailDestinationList.innerHTML = "";
            var destinations = Array.isArray(settings.destinations) ? settings.destinations : [];
            if (!destinations.length && settings.dump_point_id) {
                destinations = [{
                    dump_point_id: settings.dump_point_id,
                    transport_distance_km: settings.transport_distance_km || ""
                }];
            }
            destinations.forEach(addDetailDestinationRow);
            if (!destinations.length && detailDumpPointOptions.length) {
                addDetailDestinationRow({dump_point_id: detailDumpPointOptions[0].id});
            }
            refreshDetailDestinationRows();
            if (detailSettingSave) detailSettingSave.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftIsOpen();
        }

        function detailSettingsErrorMessage(code) {
            if (code === "stale_board") return "Данные пульта уже изменились. Закройте карточку и откройте снова.";
            if (code === "dispatcher_shift_required") return "Сначала откройте смену Горного диспетчера.";
            if (code === "invalid_transport_distance") return "Плечо должно быть числом не меньше нуля.";
            if (code === "invalid_work_settings") return "Выберите действующие породу и точку разгрузки.";
            if (code === "inactive_role") return "Роль неактивна — доступен только просмотр.";
            return "Не удалось сохранить параметры.";
        }

        function saveDetailSettings() {
            if (!detailLayer || !detailSettingSave) return;
            var url = detailLayer.dataset.gdSettingsUrl || "";
            if (!url) return;
            var destinations = collectDetailDestinations();
            if (!detailSettingRock || !detailSettingRock.value || !destinations.length) {
                if (detailSettingsStatus) detailSettingsStatus.textContent = "Выберите породу и хотя бы одну точку.";
                return;
            }
            detailSettingSave.disabled = true;
            if (detailSettingsStatus) detailSettingsStatus.textContent = "Сохраняю…";
            fetch(url, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCsrfToken(),
                    "X-Requested-With": "XMLHttpRequest"
                },
                body: JSON.stringify({
                    state_version: currentDispatcherBoardVersion(),
                    loading_horizon: detailSettingHorizon ? detailSettingHorizon.value : "",
                    loading_block: detailSettingBlock ? detailSettingBlock.value : "",
                    rock_type_id: detailSettingRock.value,
                    dump_point_ids: destinations.map(function (row) { return row.dump_point_id; }),
                    destinations: destinations
                })
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    if (!response.ok) {
                        var error = new Error("settings_request_failed");
                        error.code = payload.error || "";
                        throw error;
                    }
                    return payload;
                });
            }).then(function (payload) {
                if (detailSettingsStatus) detailSettingsStatus.textContent = "Настройки сохранены";
                if (payload && payload.settings) renderDetailSettings(payload.settings);
                if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                    window.AppRealtime.wake("dispatcher_settings_saved");
                }
                window.setTimeout(closeEquipmentCard, 650);
            }).catch(function (error) {
                if (detailSettingsStatus) detailSettingsStatus.textContent = detailSettingsErrorMessage(error && error.code);
                detailSettingSave.disabled = dispatcherRoleIsReadonly() || !dispatcherShiftIsOpen();
            });
        }

        function renderEquipmentCard(cardId, data) {
            if (!data || !detailLayer) return false;
            detailLayer.dataset.gdActiveCardId = String(cardId || "");
            detailLayer.removeAttribute("aria-busy");
            setDetailLoadState("", false);
            renderDetailGarageIcon(cardId, data);
            if (detailType) detailType.textContent = data.type || "";
            if (detailTitle) detailTitle.textContent = data.label || "";
            if (detailStatus) detailStatus.textContent = data.status_label || "";
            if (detailZone) detailZone.textContent = data.zone || "";
            if (detailEmployee) {
                var employee = data.employee || {};
                detailEmployee.hidden = false;
                if (detailEmployeePresence) detailEmployeePresence.textContent = employee.presence_label || "Сотрудник не назначен";
                if (detailEmployeeName) detailEmployeeName.textContent = employee.name || "Сотрудник не назначен";
                if (detailEmployeePhone) {
                    detailEmployeePhone.textContent = employee.phone || "телефон не указан";
                    if (employee.phone) {
                        detailEmployeePhone.href = "tel:" + employee.phone.replace(/[^\d+]/g, "");
                        detailEmployeePhone.removeAttribute("aria-disabled");
                    } else {
                        detailEmployeePhone.removeAttribute("href");
                        detailEmployeePhone.setAttribute("aria-disabled", "true");
                    }
                }
                if (detailEmployeeImg && detailEmployeeInitials) {
                    if (employee.photo) {
                        detailEmployeeImg.src = employee.photo;
                        detailEmployeeImg.hidden = false;
                        detailEmployeeInitials.hidden = true;
                    } else {
                        detailEmployeeImg.removeAttribute("src");
                        detailEmployeeImg.hidden = true;
                        detailEmployeeInitials.textContent = employee.initials || "--";
                        detailEmployeeInitials.hidden = false;
                    }
                }
            }
            if (detailCrewTitle) {
                detailCrewTitle.textContent = (data.shift && data.shift.is_truck) || data.type === "Самосвал" ? "Водитель" : "Машинист";
            }
            renderDetailShift(data.shift || null, data.employee || null);
            renderDetailPlan(data.plan || null);
            renderDetailManualTrip(data.manual_trip || null);
            window.setTimeout(syncDetailScrollHint, 0);
            renderDetailDowntime(data.downtime || null);
            renderDetailSettings(data.settings || null);
            renderDetailTrucks(data);
            if (detailMeta) {
                var metaParts = [];
                DETAIL_META_LABELS.forEach(function (label) {
                    (data.details || []).forEach(function (row) {
                        if (!row || row.label !== label || !row.value) return;
                        if (label === "Гаражный N" && String(row.value) === String(data.label || "")) return;
                        metaParts.push(label === "ГП, т" ? "ГП " + row.value + " т"
                            : label === "Кузов/ковш, м3" ? "ковш " + row.value + " м³"
                            : label === "Гаражный N" ? "гаражный № " + row.value
                            : String(row.value));
                    });
                });
                detailMeta.textContent = metaParts.join(" · ");
            }
            if (detailList) {
                detailList.innerHTML = "";
                (data.details || []).forEach(function (row) {
                    if (!row || !row.value) return;
                    if (DETAIL_META_LABELS.indexOf(row.label) >= 0) return;
                    if (data.shift && DETAIL_SHIFT_LABELS.indexOf(row.label) >= 0) return;
                    if (data.category === "complex" && row.label === "В составе") return;
                    if (detailPlanBox && !detailPlanBox.hidden && DETAIL_PLAN_LABELS.indexOf(row.label) >= 0) return;
                    if (data.downtime && data.downtime.active && ["Простой", "С начала"].indexOf(row.label) >= 0) return;
                    var term = document.createElement("dt");
                    var value = document.createElement("dd");
                    term.textContent = row.label || "";
                    value.textContent = row.value || "";
                    detailList.appendChild(term);
                    detailList.appendChild(value);
                });
            }
            renderDetailShiftReport(data.shift_report || {});
            detailLayer.hidden = false;
            return true;
        }

        function dispatcherDetailUrl(trigger, boardVersion) {
            if (!trigger || !runtimeConfig.dispatcherDetailUrlTemplate) return "";
            var equipmentId = String(trigger.dataset.equipmentId || "");
            if (!/^\d+$/.test(equipmentId)) return "";
            var category = trigger.dataset.dispatcherDrag === "complex" ? "complex" : "equipment";
            var template = String(runtimeConfig.dispatcherDetailUrlTemplate || "");
            var path = template.replace(/equipment\/0\/$/, category + "/" + equipmentId + "/");
            if (path === template) return "";
            return path + "?state_version=" + encodeURIComponent(String(boardVersion));
        }

        function detailErrorMessage(status) {
            if (status === 401) return "Сессия завершена. Войдите в систему снова.";
            if (status === 403 || status === 404) return "Карточка недоступна.";
            if (status === 409) return "Данные изменились — закройте карточку и откройте её снова.";
            return "Нет связи с сервером.";
        }

        function openEquipmentCard(cardId, trigger) {
            if (!detailLayer) return false;
            var cardKey = String(cardId || "");
            trigger = trigger || document.querySelector(
                "[data-equipment-card-id='" + CSS.escape(cardKey) + "'][data-equipment-id]"
            );
            var boardVersion = currentDispatcherBoardVersion();
            var url = dispatcherDetailUrl(trigger, boardVersion);
            if (!url) return false;

            detailRequestToken += 1;
            var requestToken = detailRequestToken;
            if (detailRequestController) detailRequestController.abort();
            detailRequestController = new AbortController();
            delete equipmentCards[cardKey];
            detailLayer.dataset.gdActiveCardId = cardKey;
            detailLayer.dataset.gdRequestedVersion = String(boardVersion);
            detailLayer.dataset.gdSettingsUrl = url;
            detailLayer.setAttribute("aria-busy", "true");
            resetDetailContent();
            if (detailType) detailType.textContent = trigger.dataset.dispatcherDrag === "complex" ? "Комплекс" : "Техника";
            if (detailTitle) detailTitle.textContent = trigger.dataset.equipmentName || "Карточка";
            if (detailStatus) detailStatus.textContent = "";
            if (detailZone) detailZone.textContent = "";
            renderDetailGarageIcon(cardKey, {
                type: trigger.dataset.dispatcherDrag === "truck" ? "Самосвал" : "Экскаватор",
                status_key: "gray"
            });
            setDetailLoadState("Загружаю свежие данные…", false);
            detailLayer.hidden = false;
            detailRetryAction = function () {
                openEquipmentCard(cardKey, trigger);
            };

            fetch(url, {
                method: "GET",
                credentials: "same-origin",
                cache: "no-store",
                signal: detailRequestController.signal,
                headers: {
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest"
                }
            }).then(function (response) {
                if (!response.ok) {
                    var requestError = new Error("detail_request_failed");
                    requestError.status = response.status;
                    throw requestError;
                }
                return response.json();
            }).then(function (payload) {
                var debugState = window.AppRealtime && typeof window.AppRealtime.getDebugState === "function"
                    ? window.AppRealtime.getDebugState()
                    : null;
                var pendingVersion = Number(debugState && debugState.pendingVersion ? debugState.pendingVersion : 0);
                if (
                    requestToken !== detailRequestToken
                    || !payload
                    || payload.contract !== "dispatcher-equipment-detail-v1"
                    || String(payload.card_key || "") !== cardKey
                    || Number(payload.operational_state_version || 0) !== boardVersion
                    || currentDispatcherBoardVersion() !== boardVersion
                    || pendingVersion > boardVersion
                ) {
                    var staleError = new Error("stale_detail");
                    staleError.status = 409;
                    throw staleError;
                }
                equipmentCards[cardKey] = payload.card;
                renderEquipmentCard(cardKey, payload.card);
            }).catch(function (error) {
                if (error && error.name === "AbortError") return;
                if (requestToken !== detailRequestToken || detailLayer.hidden) return;
                resetDetailContent();
                detailLayer.removeAttribute("aria-busy");
                var status = Number(error && error.status ? error.status : 0);
                setDetailLoadState(detailErrorMessage(status), status === 0 || status >= 500);
            });
            return true;
        }

        function closeEquipmentCard() {
            detailRequestToken += 1;
            if (detailRequestController) detailRequestController.abort();
            detailRequestController = null;
            detailRetryAction = null;
            if (detailLayer) {
                detailLayer.hidden = true;
                detailLayer.removeAttribute("aria-busy");
                delete detailLayer.dataset.gdActiveCardId;
                delete detailLayer.dataset.gdRequestedVersion;
                delete detailLayer.dataset.gdSettingsUrl;
            }
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("dispatcher_detail_closed");
            }
        }

        if (detailRetry) {
            detailRetry.addEventListener("click", function () {
                if (typeof detailRetryAction === "function") detailRetryAction();
            });
        }
        if (detailDowntimeClose) {
            detailDowntimeClose.addEventListener("click", requestDetailDowntimeClose);
        }
        if (detailServiceCloseToggle) {
            detailServiceCloseToggle.addEventListener("click", openDetailServiceCloseForm);
        }
        if (detailServiceCloseCancel) {
            detailServiceCloseCancel.addEventListener("click", closeDetailServiceCloseForm);
        }
        if (detailManualTripToggle) {
            detailManualTripToggle.addEventListener("click", openDetailManualTripForm);
        }
        if (detailManualTripCancel) {
            detailManualTripCancel.addEventListener("click", closeDetailManualTripForm);
        }
        if (detailManualTripForm) {
            detailManualTripForm.addEventListener("submit", function (event) {
                event.preventDefault();
                if (!detailManualTripForm.getAttribute("action")) return;
                if (typeof detailManualTripForm.reportValidity === "function" && !detailManualTripForm.reportValidity()) return;
                var card = equipmentCards[String(detailLayer ? detailLayer.dataset.gdActiveCardId || "" : "")] || {};
                var countInput = detailManualTripForm.querySelector("[name=trips_count]");
                var count = parseInt(countInput ? countInput.value : "1", 10) || 1;
                var dumpOption = detailManualTripDump && detailManualTripDump.selectedOptions ? detailManualTripDump.selectedOptions[0] : null;
                var message = "Добавить " + count + " " + (count === 1 ? "рейс" : count < 5 ? "рейса" : "рейсов")
                    + " самосвалу " + (card.label || (detailTitle ? detailTitle.textContent : "")) + " → " + (dumpOption ? dumpOption.textContent : "точка")
                    + "? Рейс запишется выполненным, отменить его нельзя.";
                if (typeof window.openAppConfirmDialog !== "function") {
                    detailManualTripForm.submit();
                    return;
                }
                window.openAppConfirmDialog(message, function () { detailManualTripForm.submit(); }, 0, "Добавить рейс", {
                    confirmTitle: "Добавить рейс вручную?",
                    confirmDescription: message
                });
            });
        }
        /* Два исхода: «не закрыл сам» — одно нажатие без полей (в журнале это
           значит, что сотрудник не выполнил обязанность); «по согласованию» —
           форма с причиной и показаниями по желанию. Вид уходит в close_kind. */
        function submitDetailServiceClose(kind, title, message) {
            if (!detailServiceClose || !detailServiceClose.getAttribute("action")) return;
            if (detailServiceCloseKind) detailServiceCloseKind.value = kind;
            if (typeof window.openAppConfirmDialog !== "function") {
                detailServiceClose.submit();
                return;
            }
            window.openAppConfirmDialog(message, function () { detailServiceClose.submit(); }, 0, "Закрыть смену", {
                confirmTitle: title,
                confirmDescription: message
            });
        }
        function detailServiceCloseSubject() {
            var card = equipmentCards[String(detailLayer ? detailLayer.dataset.gdActiveCardId || "" : "")] || {};
            var employee = card.employee || {};
            return (employee.name || "сотрудника") + " на " + (card.label || (detailTitle ? detailTitle.textContent : "") || "технике");
        }
        if (detailServiceCloseNeglect) {
            detailServiceCloseNeglect.addEventListener("click", function () {
                if (detailServiceCloseNeglect.disabled) return;
                submitDetailServiceClose(
                    "neglected",
                    "Сотрудник не закрыл смену сам?",
                    "Закрыть смену " + detailServiceCloseSubject() + " как незакрытую сотрудником? Причина и показания не нужны; в журнале будет отмечено, что сотрудник не закрыл смену и не сообщил диспетчеру."
                );
            });
        }
        if (detailServiceClose) {
            detailServiceClose.addEventListener("submit", function (event) {
                event.preventDefault();
                if (!detailServiceClose.getAttribute("action")) return;
                if (typeof detailServiceClose.reportValidity === "function" && !detailServiceClose.reportValidity()) return;
                submitDetailServiceClose(
                    "coordinated",
                    "Закрыть смену по согласованию?",
                    "Закрыть смену " + detailServiceCloseSubject() + " по согласованию с сотрудником? Открытые рейсы уйдут в перенос."
                );
            });
        }

        document.querySelectorAll("[data-gd-detail-close]").forEach(function (node) {
            node.addEventListener("click", closeEquipmentCard);
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") closeEquipmentCard();
        });


        if (detailSettingSave) {
            detailSettingSave.addEventListener("click", saveDetailSettings);
        }
        if (detailDestinationAdd) {
            detailDestinationAdd.addEventListener("click", function () {
                var usedIds = collectDetailDestinations().map(function (row) {
                    return String(row.dump_point_id);
                });
                var nextPoint = detailDumpPointOptions.find(function (option) {
                    return usedIds.indexOf(String(option.id)) === -1;
                });
                if (nextPoint) addDetailDestinationRow({dump_point_id: nextPoint.id});
            });
        }

        return {
            getCards: function () {
                return equipmentCards;
            },
            setCards: function (freshCards) {
                if (!equipmentCardsNode) return;
                equipmentCardsNode.textContent = JSON.stringify(freshCards);
                try {
                    equipmentCards = JSON.parse(equipmentCardsNode.textContent || "{}");
                } catch (error) {
                    equipmentCards = {};
                }
            },
            getLayer: function () {
                return detailLayer;
            },
            openEquipmentCard: openEquipmentCard,
            equipmentStateClass: dispatcherEquipmentStateClass,
            equipmentStateLabel: dispatcherEquipmentStateLabel,
            neutralEquipmentIcon: dispatcherNeutralEquipmentIcon,
            setNodeEquipmentState: setDispatcherNodeEquipmentState
        };
    }

    global.createDispatcherDetail = createDispatcherDetail;
})(window, document);
