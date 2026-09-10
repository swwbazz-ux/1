(function () {
    "use strict";

    function normalizeSearchValue(value) {
        return String(value || "")
            .toLocaleLowerCase("ru-RU")
            .normalize("NFKD")
            .replace(/[\u0300-\u036f]/g, "")
            .replace(/\s+/g, " ")
            .trim();
    }

    function setLoading(root) {
        if (!root || root.classList.contains("is-loading")) return;
        root.classList.add("is-loading");
        root.setAttribute("aria-busy", "true");
        var indicator = root.querySelector("[data-adoption-loading]");
        if (indicator) indicator.hidden = false;
    }

    function clearLoading(root) {
        if (!root) return;
        root.classList.remove("is-loading");
        root.setAttribute("aria-busy", "false");
        var indicator = root.querySelector("[data-adoption-loading]");
        if (indicator) indicator.hidden = true;
    }

    function initFilterDisclosure(root) {
        var panel = root.querySelector("[data-adoption-filters]");
        var toggle = root.querySelector("[data-adoption-filter-toggle]");
        var body = root.querySelector("[data-adoption-filter-body]");
        if (!panel || !toggle || !body) return;

        var compactQuery = window.matchMedia("(max-width: 1180px)");
        var compactExpanded = false;

        function setExpanded(expanded) {
            body.hidden = !expanded;
            toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
        }

        function syncDisclosure() {
            if (compactQuery.matches) {
                panel.classList.add("is-filter-collapsible");
                setExpanded(compactExpanded);
            } else {
                panel.classList.remove("is-filter-collapsible");
                setExpanded(true);
            }
        }

        toggle.addEventListener("click", function () {
            compactExpanded = toggle.getAttribute("aria-expanded") !== "true";
            setExpanded(compactExpanded);
            if (compactExpanded) {
                window.requestAnimationFrame(function () {
                    var firstControl = body.querySelector("select, input, button, a");
                    if (firstControl) firstControl.focus({ preventScroll: true });
                });
            }
        });

        if (typeof compactQuery.addEventListener === "function") {
            compactQuery.addEventListener("change", syncDisclosure);
        } else if (typeof compactQuery.addListener === "function") {
            compactQuery.addListener(syncDisclosure);
        }
        syncDisclosure();
    }

    function initServerNavigation(root) {
        var filterForm = root.querySelector("[data-adoption-filter-form]");
        if (filterForm) {
            filterForm.addEventListener("submit", function () {
                setLoading(root);
            });
        }

        root.addEventListener("click", function (event) {
            var stateButton = event.target.closest("[data-state-target]");
            if (stateButton && root.contains(stateButton)) {
                event.preventDefault();
                var stateCode = stateButton.getAttribute("data-state-target") || "all";
                var stateLinks = Array.prototype.slice.call(root.querySelectorAll("[data-state-code]"));
                var matchingLink = stateLinks.find(function (link) {
                    return link.getAttribute("data-state-code") === stateCode;
                });
                var targetUrl;
                if (matchingLink) {
                    targetUrl = new URL(matchingLink.href, window.location.href);
                } else {
                    targetUrl = new URL(window.location.href);
                    if (stateCode === "all") targetUrl.searchParams.delete("state");
                    else targetUrl.searchParams.set("state", stateCode);
                    targetUrl.searchParams.delete("ready_on");
                    targetUrl.searchParams.delete("ready_from");
                    targetUrl.searchParams.delete("ready_to");
                }
                targetUrl.hash = "people-list";
                if (targetUrl.pathname === window.location.pathname && targetUrl.search === window.location.search) {
                    var peopleList = root.querySelector("#people-list");
                    if (peopleList) peopleList.scrollIntoView({ block: "start" });
                    if (window.location.hash !== "#people-list") window.history.replaceState(null, "", targetUrl.toString());
                    return;
                }
                setLoading(root);
                window.location.assign(targetUrl.toString());
                return;
            }

            var link = event.target.closest("a[data-server-filter]");
            if (!link || !root.contains(link)) return;
            if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            if (link.target && link.target !== "_self") return;
            var url = new URL(link.href, window.location.href);
            if (url.origin !== window.location.origin || (url.pathname === window.location.pathname && url.search === window.location.search)) return;
            setLoading(root);
        });
    }

    function initTableSearch(root) {
        var input = root.querySelector("[data-adoption-search]");
        var rows = Array.prototype.slice.call(root.querySelectorAll("[data-adoption-person]"));
        var visibleCounter = root.querySelector("[data-visible-people]");
        var emptyState = root.querySelector("[data-adoption-search-empty]");
        if (!input || !rows.length) return;

        var searchLabel = input.closest(".adoption-search");
        if (searchLabel) searchLabel.hidden = false;

        rows.forEach(function (row) {
            row.dataset.searchValue = normalizeSearchValue(row.textContent);
        });

        function applySearch() {
            var query = normalizeSearchValue(input.value);
            var visible = 0;
            rows.forEach(function (row) {
                var matches = !query || row.dataset.searchValue.indexOf(query) !== -1;
                row.hidden = !matches;
                if (matches) visible += 1;
            });
            if (visibleCounter) visibleCounter.textContent = String(visible);
            if (emptyState) emptyState.hidden = !query || visible > 0;
        }

        input.addEventListener("input", applySearch);
        input.addEventListener("search", applySearch);
    }

    function initBreakdownDisclosure(root) {
        root.querySelectorAll("[data-breakdown-toggle]").forEach(function (toggle) {
            var panel = toggle.closest(".adoption-breakdown-panel");
            var list = panel ? panel.querySelector("[data-breakdown-list]") : null;
            if (!list) return;
            list.classList.add("is-collapsed");
            toggle.hidden = false;
            toggle.setAttribute("aria-expanded", "false");
            toggle.addEventListener("click", function () {
                var willExpand = list.classList.contains("is-collapsed");
                list.classList.toggle("is-collapsed", !willExpand);
                toggle.setAttribute("aria-expanded", willExpand ? "true" : "false");
                toggle.textContent = willExpand ? "Свернуть список" : (panel.querySelector("h2") && panel.querySelector("h2").textContent.indexOf("смен") !== -1 ? "Показать все смены" : "Показать все роли");
            });
        });
    }

    function initUnifiedChart(root, tooltip) {
        var chart = root.querySelector("[data-adoption-chart]");
        if (!chart) return;

        var control = chart.querySelector("[data-chart-control]");
        var histogram = chart.querySelector(".adoption-histogram");
        var svg = chart.querySelector(".adoption-readiness-svg");
        var endpoint = chart.querySelector("[data-chart-endpoint]");
        var buckets = Array.prototype.slice.call(chart.querySelectorAll("[data-chart-bucket]"));
        var labels = Array.prototype.slice.call(chart.querySelectorAll(".adoption-chart-dates [data-chart-label]"));
        var hitAreas = Array.prototype.slice.call(chart.querySelectorAll("[data-chart-hit-area]"));
        if (!control || !histogram || !svg || !buckets.length) return;

        var summaryDate = chart.querySelector("[data-chart-summary-date]");
        var summaryNew = chart.querySelector("[data-chart-summary-new]");
        var summaryCumulative = chart.querySelector("[data-chart-summary-cumulative]");
        var summaryPercent = chart.querySelector("[data-chart-summary-percent]");
        var drilldown = chart.querySelector("[data-chart-drilldown]");
        var noDrilldown = chart.querySelector("[data-chart-no-drilldown]");
        var granularity = chart.getAttribute("data-chart-granularity") === "week" ? "week" : "day";
        var total = Number(chart.getAttribute("data-chart-total")) || 0;
        var viewBox = svg.viewBox && svg.viewBox.baseVal;
        var viewBoxWidth = viewBox && viewBox.width ? viewBox.width : 1000;
        var viewBoxHeight = viewBox && viewBox.height ? viewBox.height : 240;
        var committedIndex = buckets.findIndex(function (bucket) { return bucket.classList.contains("is-selected"); });
        var hoverFrame = null;
        var resizeFrame = null;
        if (committedIndex < 0) committedIndex = buckets.length - 1;

        function clampIndex(index) {
            return Math.max(0, Math.min(buckets.length - 1, index));
        }

        function numberFrom(bucket, name) {
            var value = Number(bucket.getAttribute(name));
            return Number.isFinite(value) ? value : 0;
        }

        function bucketData(index) {
            var bucket = buckets[clampIndex(index)];
            return {
                element: bucket,
                label: bucket.getAttribute("data-chart-label") || "Интервал",
                newCount: numberFrom(bucket, "data-chart-new"),
                cumulative: numberFrom(bucket, "data-chart-cumulative"),
                percent: numberFrom(bucket, "data-chart-percent"),
                x: numberFrom(bucket, "data-chart-x"),
                y: numberFrom(bucket, "data-chart-y"),
                url: bucket.getAttribute("data-chart-url") || ""
            };
        }

        function tooltipText(data) {
            return data.label
                + ": стали готовы " + data.newCount
                + "; накоплено " + data.cumulative + " из " + total
                + " (" + Math.round(data.percent) + "%).";
        }

        function setChartPosition(index, preview) {
            var safeIndex = clampIndex(index);
            var data = bucketData(safeIndex);
            chart.style.setProperty("--chart-cursor-x", ((data.x / viewBoxWidth) * 100).toFixed(3) + "%");
            chart.style.setProperty("--chart-cursor-y", ((data.y / viewBoxHeight) * 100).toFixed(3) + "%");
            chart.setAttribute("data-chart-active", "true");
            buckets.forEach(function (bucket, bucketIndex) {
                bucket.classList.toggle("is-preview", Boolean(preview) && bucketIndex === safeIndex);
            });
        }

        function updateDrilldown(data) {
            var hasPeople = data.newCount > 0 && Boolean(data.url);
            if (drilldown) {
                if (data.url) {
                    try {
                        var targetUrl = new URL(data.url, window.location.href);
                        targetUrl.hash = "people-list";
                        drilldown.href = targetUrl.toString();
                    } catch (error) {
                        drilldown.href = data.url;
                    }
                }
                drilldown.hidden = !hasPeople;
                drilldown.setAttribute("aria-label", "Показать сотрудников: " + data.label);
            }
            if (noDrilldown) {
                noDrilldown.hidden = hasPeople;
                noDrilldown.textContent = data.newCount > 0
                    ? "Ссылка на выбранный интервал недоступна"
                    : (granularity === "week"
                        ? "В выбранном интервале новых готовых нет"
                        : "В выбранный день новых готовых нет");
            }
        }

        function updateSummary(data) {
            if (summaryDate) summaryDate.textContent = data.label;
            if (summaryNew) summaryNew.textContent = String(data.newCount);
            if (summaryCumulative) summaryCumulative.textContent = String(data.cumulative);
            if (summaryPercent) summaryPercent.textContent = String(Math.round(data.percent));
            updateDrilldown(data);
        }

        function commitIndex(index) {
            committedIndex = clampIndex(index);
            var data = bucketData(committedIndex);
            buckets.forEach(function (bucket, bucketIndex) {
                bucket.classList.toggle("is-selected", bucketIndex === committedIndex);
                bucket.classList.remove("is-preview");
            });
            labels.forEach(function (label) {
                label.classList.toggle("is-selected", Number(label.getAttribute("data-chart-index")) === committedIndex);
            });
            control.setAttribute("aria-valuenow", String(committedIndex + 1));
            control.setAttribute("aria-valuetext", tooltipText(data));
            chart.setAttribute("data-chart-selected-index", String(committedIndex));
            setChartPosition(committedIndex, false);
            updateSummary(data);
        }

        function indexFromPointer(event, area) {
            var rect = area.getBoundingClientRect();
            if (!rect.width) return committedIndex;
            var ratio = Math.max(0, Math.min(0.999999, (event.clientX - rect.left) / rect.width));
            return clampIndex(Math.floor(ratio * buckets.length));
        }

        function schedulePreview(event, area) {
            if (event.pointerType && event.pointerType !== "mouse" && event.pointerType !== "pen") return;
            var clientX = event.clientX;
            var clientY = event.clientY;
            var targetIndex = indexFromPointer(event, area);
            if (hoverFrame) window.cancelAnimationFrame(hoverFrame);
            hoverFrame = window.requestAnimationFrame(function () {
                var data = bucketData(targetIndex);
                setChartPosition(targetIndex, true);
                if (tooltip && typeof tooltip.showAt === "function") {
                    tooltip.showAt(tooltipText(data), clientX, clientY);
                }
            });
        }

        hitAreas.forEach(function (area) {
            area.addEventListener("pointermove", function (event) {
                schedulePreview(event, area);
            }, { passive: true });
            area.addEventListener("pointerleave", function (event) {
                if (event.pointerType && event.pointerType === "touch") return;
                if (hoverFrame) {
                    window.cancelAnimationFrame(hoverFrame);
                    hoverFrame = null;
                }
                if (tooltip && typeof tooltip.hide === "function") tooltip.hide();
                setChartPosition(committedIndex, false);
            });
            area.addEventListener("click", function (event) {
                commitIndex(indexFromPointer(event, area));
                try { control.focus({ preventScroll: true }); }
                catch (error) { control.focus(); }
            });
        });

        document.addEventListener("keydown", function (event) {
            if (event.key !== "Escape") return;
            if (hoverFrame) {
                window.cancelAnimationFrame(hoverFrame);
                hoverFrame = null;
            }
            if (tooltip && typeof tooltip.hide === "function") tooltip.hide();
            setChartPosition(committedIndex, false);
        });

        control.addEventListener("keydown", function (event) {
            var targetIndex = committedIndex;
            var intervalJump = granularity === "week" ? 1 : 7;
            if (event.key === "ArrowLeft" || event.key === "ArrowDown") targetIndex -= 1;
            else if (event.key === "ArrowRight" || event.key === "ArrowUp") targetIndex += 1;
            else if (event.key === "PageUp") targetIndex -= intervalJump;
            else if (event.key === "PageDown") targetIndex += intervalJump;
            else if (event.key === "Home") targetIndex = 0;
            else if (event.key === "End") targetIndex = buckets.length - 1;
            else if (event.key === "Enter") {
                if (drilldown && !drilldown.hidden) drilldown.click();
                event.preventDefault();
                return;
            } else return;
            event.preventDefault();
            commitIndex(targetIndex);
        });

        function syncVisibleLabels() {
            var plotWidth = control.getBoundingClientRect().width;
            var limit = plotWidth < 430 ? 4 : (plotWidth < 720 ? 5 : 7);
            var visible = {};
            if (buckets.length <= limit) {
                buckets.forEach(function (_, index) { visible[index] = true; });
            } else {
                for (var slot = 0; slot < limit; slot += 1) {
                    visible[Math.round((slot * (buckets.length - 1)) / (limit - 1))] = true;
                }
            }
            labels.forEach(function (label) {
                label.hidden = !visible[Number(label.getAttribute("data-chart-index"))];
            });
        }

        function measureAlignment() {
            if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
            resizeFrame = window.requestAnimationFrame(function () {
                syncVisibleLabels();
                var svgRect = svg.getBoundingClientRect();
                if (!svgRect.width) return;
                var maxError = 0;
                buckets.forEach(function (bucket) {
                    var projectedCenter = svgRect.left
                        + (numberFrom(bucket, "data-chart-x") / viewBoxWidth) * svgRect.width;
                    var bucketRect = bucket.getBoundingClientRect();
                    var bucketCenter = bucketRect.left + bucketRect.width / 2;
                    maxError = Math.max(maxError, Math.abs(projectedCenter - bucketCenter));
                });
                chart.setAttribute("data-chart-alignment-error", maxError.toFixed(2));
                chart.setAttribute("data-chart-alignment", maxError <= 0.75 ? "aligned" : "misaligned");
            });
        }

        if (endpoint) {
            var endpointData = bucketData(buckets.length - 1);
            endpoint.style.setProperty("--endpoint-x", ((endpointData.x / viewBoxWidth) * 100).toFixed(3) + "%");
            endpoint.style.setProperty("--endpoint-y", ((endpointData.y / viewBoxHeight) * 100).toFixed(3) + "%");
            endpoint.classList.toggle("is-near-top", endpointData.y / viewBoxHeight < 0.17);
        }

        commitIndex(committedIndex);
        measureAlignment();
        if (typeof window.ResizeObserver === "function") {
            var observer = new window.ResizeObserver(measureAlignment);
            observer.observe(control);
            observer.observe(histogram);
        } else {
            window.addEventListener("resize", measureAlignment, { passive: true });
        }
    }

    function initTooltips(root) {
        var popup = root.querySelector("[data-adoption-tooltip-popup]");
        if (!popup) return { showAt: function () {}, hide: function () {} };
        var activeTarget = null;
        var showFrame = null;
        var chartTarget = { type: "chart" };

        function positionPopup(target) {
            if (!target || popup.hidden) return;
            var targetRect = target.getBoundingClientRect();
            var popupRect = popup.getBoundingClientRect();
            var margin = 8;
            var left = targetRect.left + (targetRect.width - popupRect.width) / 2;
            left = Math.max(margin, Math.min(window.innerWidth - popupRect.width - margin, left));
            var top = targetRect.top - popupRect.height - 9;
            if (top < margin) top = Math.min(window.innerHeight - popupRect.height - margin, targetRect.bottom + 9);
            popup.style.left = Math.round(left) + "px";
            popup.style.top = Math.round(top) + "px";
        }

        function showTooltip(target) {
            var text = target && target.getAttribute("data-adoption-tooltip");
            if (!text) return;
            activeTarget = target;
            popup.textContent = text;
            popup.hidden = false;
            popup.classList.remove("is-visible");
            if (showFrame) window.cancelAnimationFrame(showFrame);
            showFrame = window.requestAnimationFrame(function () {
                positionPopup(target);
                popup.classList.add("is-visible");
            });
        }

        function showAt(text, clientX, clientY) {
            if (!text) return;
            activeTarget = chartTarget;
            popup.textContent = text;
            popup.hidden = false;
            popup.classList.remove("is-visible");
            if (showFrame) window.cancelAnimationFrame(showFrame);
            showFrame = window.requestAnimationFrame(function () {
                var popupRect = popup.getBoundingClientRect();
                var margin = 8;
                var left = clientX - popupRect.width / 2;
                left = Math.max(margin, Math.min(window.innerWidth - popupRect.width - margin, left));
                var top = clientY - popupRect.height - 15;
                if (top < margin) top = Math.min(window.innerHeight - popupRect.height - margin, clientY + 15);
                popup.style.left = Math.round(left) + "px";
                popup.style.top = Math.round(top) + "px";
                popup.classList.add("is-visible");
            });
        }

        function hideTooltip() {
            activeTarget = null;
            if (showFrame) {
                window.cancelAnimationFrame(showFrame);
                showFrame = null;
            }
            popup.classList.remove("is-visible");
            window.setTimeout(function () {
                if (!activeTarget) popup.hidden = true;
            }, 130);
        }

        root.addEventListener("pointerover", function (event) {
            var target = event.target.closest("[data-adoption-tooltip]");
            if (target && root.contains(target) && target !== activeTarget) showTooltip(target);
        });
        root.addEventListener("pointerout", function (event) {
            var target = event.target.closest("[data-adoption-tooltip]");
            if (target && (!event.relatedTarget || !target.contains(event.relatedTarget))) hideTooltip();
        });
        root.addEventListener("focusin", function (event) {
            var target = event.target.closest("[data-adoption-tooltip]");
            if (target) showTooltip(target);
        });
        root.addEventListener("focusout", function (event) {
            var target = event.target.closest("[data-adoption-tooltip]");
            if (target && (!event.relatedTarget || !target.contains(event.relatedTarget))) hideTooltip();
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") hideTooltip();
        });
        window.addEventListener("resize", hideTooltip, { passive: true });
        window.addEventListener("scroll", hideTooltip, { passive: true, capture: true });
        return { showAt: showAt, hide: hideTooltip };
    }

    function initDashboard(root) {
        if (!root || root.dataset.registrationDashboardReady === "true") return;
        root.dataset.registrationDashboardReady = "true";
        clearLoading(root);
        initFilterDisclosure(root);
        initServerNavigation(root);
        initTableSearch(root);
        initBreakdownDisclosure(root);
        var tooltip = initTooltips(root);
        initUnifiedChart(root, tooltip);
        window.requestAnimationFrame(function () {
            root.classList.add("is-ready");
        });
    }

    function initAllDashboards() {
        document.querySelectorAll("[data-registration-dashboard]").forEach(initDashboard);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initAllDashboards);
    } else {
        initAllDashboards();
    }
    window.addEventListener("pageshow", function () {
        document.querySelectorAll("[data-registration-dashboard]").forEach(clearLoading);
    });
})();
