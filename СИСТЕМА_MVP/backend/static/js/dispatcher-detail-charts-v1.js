/* Dispatcher detail-card charts and report tabs.
   Owns only presentation of the report payload inside the desktop detail card. */
(function (global, document) {
    "use strict";

    function createDispatcherDetailCharts(options) {
        options = options || {};
        var detailLayer = options.detailLayer || null;
        var detailShiftReport = options.detailShiftReport || null;
        var detailMetrics = options.detailMetrics || null;
        var detailTabs = options.detailTabs || null;
        var detailDashboard = options.detailDashboard || null;
        var escapeHtml = options.escapeHtml || function (value) {
            return String(value || "").replace(/[&<>"']/g, function (char) {
                return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char];
            });
        };

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

        return {
            renderChart: renderDetailChart,
            renderShiftReport: renderDetailShiftReport
        };
    }

    global.createDispatcherDetailCharts = createDispatcherDetailCharts;
})(window, document);
