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

    function initChartKeyboard(root) {
        var links = Array.prototype.slice.call(root.querySelectorAll("[data-chart-day]"));
        if (!links.length) return;
        var current = links.find(function (link) { return link.classList.contains("is-selected"); }) || links[links.length - 1];
        links.forEach(function (link) { link.setAttribute("tabindex", link === current ? "0" : "-1"); });

        links.forEach(function (link, index) {
            link.addEventListener("keydown", function (event) {
                var targetIndex = index;
                if (event.key === "ArrowLeft") targetIndex = Math.max(0, index - 1);
                else if (event.key === "ArrowRight") targetIndex = Math.min(links.length - 1, index + 1);
                else if (event.key === "Home") targetIndex = 0;
                else if (event.key === "End") targetIndex = links.length - 1;
                else return;
                event.preventDefault();
                links.forEach(function (item, itemIndex) { item.setAttribute("tabindex", itemIndex === targetIndex ? "0" : "-1"); });
                links[targetIndex].focus();
            });
        });
    }

    function initChartAlignmentCheck(root) {
        var stage = root.querySelector(".adoption-chart-stage");
        var line = stage ? stage.querySelector(".adoption-cumulative-line") : null;
        var days = stage ? Array.prototype.slice.call(stage.querySelectorAll("[data-chart-day]")) : [];
        var points = line ? Array.prototype.slice.call(line.querySelectorAll(".adoption-line-point")) : [];
        if (!stage || !line || !days.length || days.length !== points.length) return;

        var checkFrame = null;
        function measureAlignment() {
            if (checkFrame) window.cancelAnimationFrame(checkFrame);
            checkFrame = window.requestAnimationFrame(function () {
                var lineRect = line.getBoundingClientRect();
                var viewBox = line.viewBox && line.viewBox.baseVal;
                if (!viewBox || !lineRect.width || !viewBox.width) return;
                var maxError = 0;
                points.forEach(function (point, index) {
                    var svgX = Number(point.getAttribute("cx"));
                    var projectedCenter = lineRect.left + ((svgX - viewBox.x) / viewBox.width) * lineRect.width;
                    var dayRect = days[index].getBoundingClientRect();
                    var barCenter = dayRect.left + dayRect.width / 2;
                    maxError = Math.max(maxError, Math.abs(projectedCenter - barCenter));
                });
                root.dataset.chartAlignmentError = maxError.toFixed(2);
                root.dataset.chartAlignment = maxError <= 0.75 ? "aligned" : "misaligned";
            });
        }

        measureAlignment();
        if (typeof window.ResizeObserver === "function") {
            var observer = new window.ResizeObserver(measureAlignment);
            observer.observe(stage);
        } else {
            window.addEventListener("resize", measureAlignment, { passive: true });
        }
    }

    function isHistoryNavigation() {
        try {
            var entries = window.performance && window.performance.getEntriesByType
                ? window.performance.getEntriesByType("navigation")
                : [];
            if (entries.length) return entries[0].type === "back_forward";
            return Boolean(window.performance && window.performance.navigation && window.performance.navigation.type === 2);
        } catch (error) {
            return false;
        }
    }

    function revealCurrentChartDay(root) {
        var viewport = root.querySelector(".adoption-chart-scroll");
        if (!viewport || isHistoryNavigation()) return;

        var days = Array.prototype.slice.call(viewport.querySelectorAll("[data-chart-day]"));
        if (!days.length) return;
        var target = days.find(function (day) { return day.classList.contains("is-selected"); }) || days[days.length - 1];

        window.requestAnimationFrame(function () {
            if (viewport.scrollWidth <= viewport.clientWidth) return;
            var viewportRect = viewport.getBoundingClientRect();
            var targetRect = target.getBoundingClientRect();
            var targetLeft = viewport.scrollLeft
                + targetRect.left
                - viewportRect.left
                - ((viewport.clientWidth - targetRect.width) / 2);
            var maxLeft = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
            viewport.scrollLeft = Math.max(0, Math.min(maxLeft, targetLeft));
        });
    }

    function initTooltips(root) {
        var popup = root.querySelector("[data-adoption-tooltip-popup]");
        if (!popup) return;
        var activeTarget = null;
        var showFrame = null;

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
    }

    function initDashboard(root) {
        if (!root || root.dataset.registrationDashboardReady === "true") return;
        root.dataset.registrationDashboardReady = "true";
        clearLoading(root);
        initFilterDisclosure(root);
        initServerNavigation(root);
        initTableSearch(root);
        initBreakdownDisclosure(root);
        initChartKeyboard(root);
        initChartAlignmentCheck(root);
        revealCurrentChartDay(root);
        initTooltips(root);
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
