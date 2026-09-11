(function () {
    "use strict";

    var navigationRequest = null;
    var loadingTimer = null;
    var retryNavigation = null;

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

    function scheduleLoading(root) {
        if (loadingTimer) window.clearTimeout(loadingTimer);
        loadingTimer = window.setTimeout(function () {
            loadingTimer = null;
            setLoading(root);
        }, 180);
    }

    function clearLoading(root) {
        if (loadingTimer) {
            window.clearTimeout(loadingTimer);
            loadingTimer = null;
        }
        if (!root) return;
        root.classList.remove("is-loading");
        root.setAttribute("aria-busy", "false");
        var indicator = root.querySelector("[data-adoption-loading]");
        if (indicator) indicator.hidden = true;
    }

    function showNavigationError(root, retry) {
        clearLoading(root);
        var panel = root && root.querySelector("[data-adoption-navigation-error]");
        var button = panel && panel.querySelector("[data-adoption-navigation-retry]");
        retryNavigation = retry;
        if (!panel) return;
        panel.hidden = false;
        if (button) {
            button.onclick = function () {
                panel.hidden = true;
                if (typeof retryNavigation === "function") retryNavigation();
            };
            try { button.focus({ preventScroll: true }); }
            catch (error) { button.focus(); }
        }
    }

    function supportsEnhancedNavigation() {
        return typeof window.fetch === "function"
            && typeof window.DOMParser === "function"
            && typeof window.URL === "function";
    }

    function initAdminNavigation() {
        var wrap = document.querySelector("[data-admin-registration-nav-wrap]");
        var toggle = document.querySelector("[data-admin-registration-nav-toggle]");
        var body = document.querySelector("[data-admin-registration-nav-body]");
        if (!wrap || !toggle || !body || wrap.dataset.navigationReady === "true") return;

        wrap.dataset.navigationReady = "true";
        var compactQuery = window.matchMedia("(max-width: 720px)");
        var compactExpanded = false;

        function setExpanded(expanded) {
            body.hidden = !expanded;
            toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
        }

        function syncNavigation() {
            wrap.classList.toggle("is-collapsible", compactQuery.matches);
            setExpanded(compactQuery.matches ? compactExpanded : true);
        }

        toggle.addEventListener("click", function () {
            compactExpanded = toggle.getAttribute("aria-expanded") !== "true";
            setExpanded(compactExpanded);
        });

        if (typeof compactQuery.addEventListener === "function") {
            compactQuery.addEventListener("change", syncNavigation);
        } else if (typeof compactQuery.addListener === "function") {
            compactQuery.addListener(syncNavigation);
        }
        syncNavigation();
    }

    function initManagementLayout(root) {
        if (!root.classList.contains("management-registration-page")) return;
        var analytics = root.querySelector("[data-adoption-analytics]");
        var people = root.querySelector("#people-list");
        if (analytics && people && people.previousElementSibling !== analytics) {
            root.insertBefore(analytics, people);
        }
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

    function focusDescriptor(element) {
        if (!element || !element.closest) return null;
        var donut = element.closest("[data-donut-code]");
        if (donut) return { type: "donut", value: donut.getAttribute("data-donut-code") };
        var state = element.closest("[data-state-code]");
        if (state) return { type: "state", value: state.getAttribute("data-state-code") };
        if (element.closest("[data-adoption-state-select]")) return { type: "reason" };
        if (element.closest("[data-adoption-filter-form]")) return { type: "filters" };
        if (element.closest("[data-adoption-analytics-trigger]")) return { type: "analytics" };
        if (element.closest("[data-server-filter]")) return { type: "destination" };
        return null;
    }

    function normalizeCanonicalUrl(root) {
        if (!root || !window.history || typeof window.history.replaceState !== "function") return;
        var canonical = root.getAttribute("data-canonical-query-url");
        if (canonical === null) return;
        var expectedSearch = canonical ? (canonical.charAt(0) === "?" ? canonical : "?" + canonical) : "";
        if (window.location.search === expectedSearch) return;
        var meaningfulHash = /^(#people-list|#adoption-growth-title)$/.test(window.location.hash) ? window.location.hash : "";
        window.history.replaceState(window.history.state, "", window.location.pathname + expectedSearch + meaningfulHash);
    }

    function focusAfterNavigation(root, url, descriptor) {
        if (!root || !descriptor) return;
        var target = null;
        if (descriptor.type === "donut") {
            target = Array.prototype.slice.call(root.querySelectorAll("[data-donut-legend]")).find(function (item) {
                return item.getAttribute("data-donut-code") === descriptor.value;
            });
        } else if (descriptor.type === "state") {
            target = Array.prototype.slice.call(root.querySelectorAll("[data-state-code]")).find(function (item) {
                return item.getAttribute("data-state-code") === descriptor.value;
            });
        } else if (descriptor.type === "reason") {
            target = root.querySelector("[data-adoption-state-select]");
        } else if (descriptor.type === "filters") {
            target = root.querySelector("[data-adoption-filter-toggle]")
                || root.querySelector("[data-adoption-filter-form] select");
        } else if (descriptor.type === "analytics") {
            target = root.querySelector("[data-adoption-analytics-trigger]");
        }
        if (!target && url.hash === "#people-list") target = root.querySelector("#people-list-title");
        if (!target && url.hash === "#adoption-growth-title") target = root.querySelector("#adoption-growth-title");
        if (!target && descriptor.type === "destination") {
            target = root.querySelector("#adoption-summary-title") || root.querySelector("h1, h2");
        }
        if (!target) return;
        if (!target.matches("a, button, input, select, textarea, [tabindex]")) target.setAttribute("tabindex", "-1");
        try { target.focus({ preventScroll: true }); }
        catch (error) { target.focus(); }
    }

    function scrollAfterNavigation(root, url) {
        if (!root) return;
        var target = url.hash ? root.querySelector(url.hash) : null;
        if (!target && url.hash === "#adoption-growth-title") {
            target = root.querySelector("[data-adoption-analytics]");
        }
        if (target) {
            window.requestAnimationFrame(function () {
                target.scrollIntoView({ block: "start" });
            });
        }
    }

    function enhancedNavigate(root, destination, options) {
        var settings = options || {};
        var url = new URL(destination, window.location.href);
        var restoreFocus = settings.focus
            || focusDescriptor(document.activeElement)
            || (settings.fromHistory ? null : { type: "destination" });
        if (!supportsEnhancedNavigation() || url.origin !== window.location.origin) {
            window.location.assign(url.toString());
            return;
        }

        if (navigationRequest) navigationRequest.abort();
        var controller = typeof window.AbortController === "function" ? new window.AbortController() : null;
        navigationRequest = controller;
        scheduleLoading(root);
        var timeout = window.setTimeout(function () {
            if (controller) controller.abort();
        }, 12000);

        window.fetch(url.toString(), {
            method: "GET",
            credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest" },
            signal: controller ? controller.signal : undefined
        }).then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.text();
        }).then(function (html) {
            var documentCopy = new window.DOMParser().parseFromString(html, "text/html");
            var replacement = documentCopy.querySelector("[data-registration-dashboard]");
            if (!replacement) throw new Error("Dashboard fragment missing");
            if (root._adoptionDestroy) root._adoptionDestroy();
            root.replaceWith(replacement);
            if (!settings.fromHistory) {
                window.history.pushState({ adoptionDashboard: true }, "", url.toString());
            }
            initDashboard(replacement);
            document.title = documentCopy.title || document.title;
            clearLoading(replacement);
            focusAfterNavigation(replacement, url, restoreFocus);
            scrollAfterNavigation(replacement, url);
        }).catch(function (error) {
            if (error && error.name === "AbortError" && navigationRequest !== controller) return;
            var retryCount = Number(settings.retryCount || 0);
            showNavigationError(root, function () {
                if (retryCount >= 1) {
                    window.location.assign(url.toString());
                    return;
                }
                enhancedNavigate(root, url, {
                    fromHistory: settings.fromHistory,
                    retryCount: retryCount + 1,
                    focus: restoreFocus
                });
            });
        }).finally(function () {
            window.clearTimeout(timeout);
            if (navigationRequest === controller) navigationRequest = null;
        });
    }

    function initServerNavigation(root) {
        var filterForm = root.querySelector("[data-adoption-filter-form]");
        if (filterForm) {
            filterForm.addEventListener("submit", function (event) {
                if (!supportsEnhancedNavigation()) {
                    setLoading(root);
                    return;
                }
                event.preventDefault();
                var targetUrl = new URL(filterForm.action || window.location.href, window.location.href);
                targetUrl.search = new URLSearchParams(new window.FormData(filterForm)).toString();
                enhancedNavigate(root, targetUrl, { focus: focusDescriptor(document.activeElement) });
            });
        }

        var stateSelect = root.querySelector("[data-adoption-state-select]");
        if (stateSelect) {
            stateSelect.addEventListener("change", function () {
                if (!stateSelect.value) return;
                enhancedNavigate(root, stateSelect.value, { focus: { type: "reason" } });
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
                enhancedNavigate(root, targetUrl, { focus: focusDescriptor(stateButton) });
                return;
            }

            var link = event.target.closest("a[data-server-filter]");
            if (!link || !root.contains(link)) return;
            if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            if (link.target && link.target !== "_self") return;
            var url = new URL(link.href, window.location.href);
            if (url.origin !== window.location.origin) return;
            if (url.pathname === window.location.pathname && url.search === window.location.search) {
                event.preventDefault();
                scrollAfterNavigation(root, url);
                return;
            }
            if (!supportsEnhancedNavigation()) {
                setLoading(root);
                return;
            }
            event.preventDefault();
            enhancedNavigate(root, url, { focus: focusDescriptor(link) });
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

    function initDonut(root) {
        var panel = root.querySelector("[data-adoption-donut]");
        if (!panel) return;
        var segments = Array.prototype.slice.call(panel.querySelectorAll("[data-donut-segment]"));
        var legend = Array.prototype.slice.call(panel.querySelectorAll("[data-donut-legend]"));
        var sectorLinks = Array.prototype.slice.call(panel.querySelectorAll("[data-donut-interactive]"));
        var centerCount = panel.querySelector("[data-donut-center-count]");
        var centerLabel = panel.querySelector("[data-donut-center-label]");
        var total = Number(panel.getAttribute("data-donut-total")) || 0;
        var ready = Number(panel.getAttribute("data-donut-ready")) || 0;
        var readyPercent = Number(panel.getAttribute("data-donut-ready-percent")) || 0;
        var offsetCount = 0;

        function polarPoint(radius, percent) {
            var angle = ((percent / 100) * Math.PI * 2) - (Math.PI / 2);
            return {
                x: 60 + (radius * Math.cos(angle)),
                y: 60 + (radius * Math.sin(angle))
            };
        }

        function segmentPath(start, end) {
            var safeEnd = Math.min(99.9999, end);
            var outerStart = polarPoint(50, start);
            var outerEnd = polarPoint(50, safeEnd);
            var innerEnd = polarPoint(34, safeEnd);
            var innerStart = polarPoint(34, start);
            var largeArc = safeEnd - start > 50 ? 1 : 0;
            return [
                "M", outerStart.x, outerStart.y,
                "A", 50, 50, 0, largeArc, 1, outerEnd.x, outerEnd.y,
                "L", innerEnd.x, innerEnd.y,
                "A", 34, 34, 0, largeArc, 0, innerStart.x, innerStart.y,
                "Z"
            ].join(" ");
        }

        segments.forEach(function (segment) {
            var count = Math.max(0, Number(segment.getAttribute("data-donut-count")) || 0);
            var exactPercent = total > 0 ? (count / total) * 100 : 0;
            var exactStart = total > 0 ? (offsetCount / total) * 100 : 0;
            var exactEnd = total > 0 ? ((offsetCount + count) / total) * 100 : 0;
            var gap = segments.length > 1 ? Math.min(0.35, exactPercent * 0.08) : 0;
            var start = exactStart + gap;
            var end = exactEnd - gap;
            segment.setAttribute("d", segmentPath(start, Math.max(start, end)));
            var midpoint = exactStart + (exactPercent / 2);
            var shift = polarPoint(4.5, midpoint);
            segment.style.setProperty("--donut-shift-x", (shift.x - 60).toFixed(2) + "px");
            segment.style.setProperty("--donut-shift-y", (shift.y - 60).toFixed(2) + "px");
            offsetCount += count;
        });
        panel.setAttribute("data-donut-geometry-percent", total > 0 ? ((offsetCount / total) * 100).toFixed(6) : "0");

        function showSelection(item) {
            if (!item) {
                if (centerCount) centerCount.textContent = String(ready);
                if (centerLabel) centerLabel.textContent = Math.round(readyPercent) + "% активировали";
                return;
            }
            var code = item.getAttribute("data-donut-code");
            var matchingSegment = segments.find(function (segment) {
                return segment.getAttribute("data-donut-code") === code;
            });
            segments.forEach(function (segment) {
                segment.classList.toggle("is-preview", segment === matchingSegment && !segment.classList.contains("is-selected"));
            });
            if (centerCount) centerCount.textContent = item.getAttribute("data-donut-count") || "0";
            if (centerLabel) {
                centerLabel.textContent = (Math.round(Number(item.getAttribute("data-donut-percent")) || 0))
                    + "% · " + (item.getAttribute("data-donut-label") || "сотрудников");
            }
        }

        function restoreSelection() {
            segments.forEach(function (segment) { segment.classList.remove("is-preview"); });
            showSelection(null);
        }

        legend.concat(sectorLinks).forEach(function (item) {
            item.addEventListener("pointerenter", function () { showSelection(item); });
            item.addEventListener("pointerleave", restoreSelection);
            item.addEventListener("focus", function () { showSelection(item); });
            item.addEventListener("blur", restoreSelection);
        });
        sectorLinks.forEach(function (item) {
            item.addEventListener("keydown", function (event) {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                enhancedNavigate(root, item.getAttribute("href"), {
                    focus: { type: "donut", value: item.getAttribute("data-donut-code") }
                });
            });
        });
        restoreSelection();
    }

    function initAnalyticsTrigger(root) {
        var trigger = root.querySelector("[data-adoption-analytics-trigger]");
        var analytics = root.querySelector("[data-adoption-analytics]");
        if (!trigger || !analytics) return function () {};
        var isDisclosure = analytics.tagName.toLowerCase() === "details";
        var highlightTimer = null;

        function syncState() {
            trigger.setAttribute("aria-expanded", isDisclosure ? (analytics.open ? "true" : "false") : "true");
        }

        function reveal() {
            if (isDisclosure) analytics.open = !analytics.open;
            syncState();
            if (!isDisclosure || analytics.open) {
                analytics.setAttribute("data-analytics-highlight", "true");
                window.requestAnimationFrame(function () {
                    var reduceMotion = window.matchMedia
                        && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
                    analytics.scrollIntoView({ block: "start", behavior: reduceMotion ? "auto" : "smooth" });
                });
                if (highlightTimer) window.clearTimeout(highlightTimer);
                highlightTimer = window.setTimeout(function () {
                    analytics.removeAttribute("data-analytics-highlight");
                }, 900);
            }
        }

        trigger.addEventListener("click", reveal);
        if (isDisclosure) analytics.addEventListener("toggle", syncState);
        syncState();
        return function () {
            if (highlightTimer) window.clearTimeout(highlightTimer);
            if (isDisclosure) analytics.removeEventListener("toggle", syncState);
        };
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
                + ": активировали доступ " + data.newCount
                + "; с подтверждённой датой накоплено " + data.cumulative + " из " + total
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
                        ? "В выбранном интервале новых активаций нет"
                        : "В выбранный день новых активаций нет");
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

        function handleChartEscape(event) {
            if (event.key !== "Escape") return;
            if (hoverFrame) {
                window.cancelAnimationFrame(hoverFrame);
                hoverFrame = null;
            }
            if (tooltip && typeof tooltip.hide === "function") tooltip.hide();
            setChartPosition(committedIndex, false);
        }
        document.addEventListener("keydown", handleChartEscape);

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
                label.hidden = false;
                label.classList.toggle(
                    "is-suppressed",
                    !visible[Number(label.getAttribute("data-chart-index"))]
                );
            });
        }

        function measureAlignment() {
            if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
            resizeFrame = window.requestAnimationFrame(function () {
                syncVisibleLabels();
                var svgRect = svg.getBoundingClientRect();
                if (!svgRect.width) return;
                var plotError = 0;
                var labelError = 0;
                buckets.forEach(function (bucket) {
                    var projectedCenter = svgRect.left
                        + (numberFrom(bucket, "data-chart-x") / viewBoxWidth) * svgRect.width;
                    var bucketRect = bucket.getBoundingClientRect();
                    var bucketCenter = bucketRect.left + bucketRect.width / 2;
                    plotError = Math.max(plotError, Math.abs(projectedCenter - bucketCenter));
                });
                labels.forEach(function (label) {
                    if (label.classList.contains("is-suppressed")) return;
                    var index = Number(label.getAttribute("data-chart-index"));
                    var bucket = buckets[index];
                    if (!bucket) return;
                    var labelRect = label.getBoundingClientRect();
                    var bucketRect = bucket.getBoundingClientRect();
                    var labelCenter = labelRect.left + labelRect.width / 2;
                    var bucketCenter = bucketRect.left + bucketRect.width / 2;
                    labelError = Math.max(labelError, Math.abs(labelCenter - bucketCenter));
                });
                var maxError = Math.max(plotError, labelError);
                chart.setAttribute("data-chart-plot-error", plotError.toFixed(2));
                chart.setAttribute("data-chart-label-error", labelError.toFixed(2));
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
        var observer = null;
        var analyticsDisclosure = chart.closest("details");
        function handleAnalyticsToggle() {
            if (analyticsDisclosure && analyticsDisclosure.open) measureAlignment();
        }
        if (typeof window.ResizeObserver === "function") {
            observer = new window.ResizeObserver(measureAlignment);
            observer.observe(control);
            observer.observe(histogram);
        } else {
            window.addEventListener("resize", measureAlignment, { passive: true });
        }
        if (analyticsDisclosure) {
            analyticsDisclosure.addEventListener("toggle", handleAnalyticsToggle);
        }
        return function () {
            document.removeEventListener("keydown", handleChartEscape);
            if (observer) observer.disconnect();
            else window.removeEventListener("resize", measureAlignment);
            if (analyticsDisclosure) analyticsDisclosure.removeEventListener("toggle", handleAnalyticsToggle);
            if (hoverFrame) window.cancelAnimationFrame(hoverFrame);
            if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
        };
    }

    function initTooltips(root) {
        var popup = root.querySelector("[data-adoption-tooltip-popup]");
        if (!popup) return { showAt: function () {}, hide: function () {}, destroy: function () {} };
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
        function handleTooltipEscape(event) {
            if (event.key === "Escape") hideTooltip();
        }
        document.addEventListener("keydown", handleTooltipEscape);
        window.addEventListener("resize", hideTooltip, { passive: true });
        window.addEventListener("scroll", hideTooltip, { passive: true, capture: true });
        return {
            showAt: showAt,
            hide: hideTooltip,
            destroy: function () {
                document.removeEventListener("keydown", handleTooltipEscape);
                window.removeEventListener("resize", hideTooltip);
                window.removeEventListener("scroll", hideTooltip, true);
                if (showFrame) window.cancelAnimationFrame(showFrame);
            }
        };
    }

    function initDashboard(root) {
        if (!root || root.dataset.registrationDashboardReady === "true") return;
        root.dataset.registrationDashboardReady = "true";
        normalizeCanonicalUrl(root);
        clearLoading(root);
        initManagementLayout(root);
        initFilterDisclosure(root);
        initServerNavigation(root);
        initTableSearch(root);
        initBreakdownDisclosure(root);
        initDonut(root);
        var destroyAnalytics = initAnalyticsTrigger(root);
        var tooltip = initTooltips(root);
        var destroyChart = initUnifiedChart(root, tooltip);
        root._adoptionDestroy = function () {
            if (typeof destroyAnalytics === "function") destroyAnalytics();
            if (typeof destroyChart === "function") destroyChart();
            if (tooltip && typeof tooltip.destroy === "function") tooltip.destroy();
        };
        window.requestAnimationFrame(function () {
            root.classList.add("is-ready");
        });
    }

    function initAllDashboards() {
        initAdminNavigation();
        document.querySelectorAll("[data-registration-dashboard]").forEach(initDashboard);
    }

    window.addEventListener("popstate", function () {
        var root = document.querySelector("[data-registration-dashboard]");
        if (!root) return;
        if (!supportsEnhancedNavigation()) {
            window.location.reload();
            return;
        }
        enhancedNavigate(root, window.location.href, { fromHistory: true });
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initAllDashboards);
    } else {
        initAllDashboards();
    }
    window.addEventListener("pageshow", function () {
        document.querySelectorAll("[data-registration-dashboard]").forEach(clearLoading);
    });
})();
