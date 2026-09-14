(function () {
    "use strict";

    var modal = null;
    var content = null;
    var subtitle = null;
    var opener = null;
    var inFlight = null;
    var requestGeneration = 0;
    var refreshQueued = false;
    var hourTimer = 0;
    var historyOwned = false;
    var OPEN_STATE_KEY = "eoHourlyReport";

    function shell() {
        return document.querySelector("[data-eo-shell]");
    }

    function currentExcavatorId() {
        var current = shell();
        return current ? String(current.dataset.eoCurrentExcavatorId || "") : "";
    }

    function cacheKey() {
        return "eo-hourly-report-v1:" + currentExcavatorId();
    }

    function safeCacheRead() {
        try {
            var value = localStorage.getItem(cacheKey());
            return value ? JSON.parse(value) : null;
        } catch (error) {
            return null;
        }
    }

    function safeCacheWrite(payload) {
        try {
            localStorage.setItem(cacheKey(), JSON.stringify(payload));
        } catch (error) {}
    }

    function clearNode(node) {
        while (node && node.firstChild) node.removeChild(node.firstChild);
    }

    function element(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = String(text);
        return node;
    }

    function renderState(message) {
        clearNode(content);
        content.appendChild(element("div", "eo-hourly-report__state", message));
    }

    function appendCell(row, tag, text, className, scope) {
        var cell = element(tag, className || "", text);
        if (scope) cell.scope = scope;
        row.appendChild(cell);
        return cell;
    }

    function renderPayload(payload, offline) {
        if (!modal || modal.hidden || !payload || !payload.periods || !payload.totals) return;
        clearNode(content);
        subtitle.textContent = (payload.excavator ? payload.excavator.name : "Экскаватор")
            + " · " + (payload.work_date || "");

        if (offline) {
            var cachedAt = payload.freshness_label || "последнее обновление";
            content.appendChild(element("p", "eo-hourly-report__offline", "Нет связи · данные " + cachedAt.toLowerCase()));
        }

        if (payload.is_empty) {
            content.appendChild(element(
                "div",
                "eo-hourly-report__state",
                "За прошлый и текущий час погрузок нет"
            ));
        }

        var table = element("table", "eo-hourly-report__table");
        table.setAttribute("aria-label", "Рейсы по точкам разгрузки и типам самосвалов");
        var colgroup = document.createElement("colgroup");
        colgroup.appendChild(document.createElement("col"));
        colgroup.appendChild(document.createElement("col"));
        colgroup.appendChild(document.createElement("col"));
        table.appendChild(colgroup);

        var thead = document.createElement("thead");
        var header = document.createElement("tr");
        appendCell(header, "th", "Точка разгрузки", "", "col");
        appendCell(header, "th", "Прошлый\n" + payload.periods.previous.label, "", "col");
        appendCell(header, "th", "Текущий\n" + payload.periods.current.label, "is-current", "col");
        thead.appendChild(header);
        table.appendChild(thead);

        (payload.groups || []).forEach(function (group) {
            var tbody = document.createElement("tbody");
            var groupRow = element("tr", "eo-hourly-report__group");
            var groupCell = appendCell(groupRow, "th", group.dump_point, "", "rowgroup");
            groupCell.colSpan = 3;
            tbody.appendChild(groupRow);
            (group.rows || []).forEach(function (fleet) {
                var row = element("tr", "eo-hourly-report__fleet");
                appendCell(row, "th", fleet.label, "", "row");
                appendCell(row, "td", fleet.previous);
                appendCell(row, "td", fleet.current, "is-current");
                tbody.appendChild(row);
            });
            table.appendChild(tbody);
        });

        var tfoot = element("tfoot", "eo-hourly-report__totals");
        (payload.totals.rows || []).forEach(function (fleet) {
            var row = document.createElement("tr");
            appendCell(row, "th", fleet.label, "", "row");
            appendCell(row, "td", fleet.previous);
            appendCell(row, "td", fleet.current, "is-current");
            tfoot.appendChild(row);
        });
        var grand = element("tr", "eo-hourly-report__grand");
        appendCell(grand, "th", "Всего рейсов", "", "row");
        appendCell(grand, "td", payload.totals.grand.previous);
        appendCell(grand, "td", payload.totals.grand.current, "is-current");
        tfoot.appendChild(grand);
        table.appendChild(tfoot);
        content.appendChild(table);

        var meta = element("div", "eo-hourly-report__meta");
        meta.appendChild(element("span", "", "По времени погрузки"));
        var freshness = element("time", "", payload.freshness_label || "");
        freshness.dateTime = payload.generated_at || "";
        meta.appendChild(freshness);
        content.appendChild(meta);
        if (offline) {
            content.appendChild(element(
                "p",
                "eo-hourly-report__note",
                "Локально сохранённые, но ещё не синхронизированные погрузки появятся после подтверждения сервера."
            ));
        }
        scheduleHourRefresh(payload);
    }

    function scheduleHourRefresh(payload) {
        window.clearTimeout(hourTimer);
        hourTimer = 0;
        if (!payload || !payload.periods || !payload.periods.current) return;
        var start = Date.parse(payload.periods.current.start || "");
        if (!Number.isFinite(start)) return;
        var delay = Math.max(1000, Math.min((start + 3600000) - Date.now() + 750, 3600750));
        hourTimer = window.setTimeout(function () {
            hourTimer = 0;
            requestReport("hour-rollover");
        }, delay);
    }

    function requestReport() {
        if (!modal || modal.hidden) return Promise.resolve(false);
        if (inFlight) {
            refreshQueued = true;
            return inFlight;
        }
        var url = modal.dataset.eoHourlyReportUrl;
        if (!url) {
            renderState("Почасовой отчёт временно недоступен");
            return Promise.resolve(false);
        }
        var generation = ++requestGeneration;
        var controller = typeof AbortController === "function" ? new AbortController() : null;
        modal._eoHourlyAbortController = controller;
        modal.dataset.eoHourlyLoading = "true";
        inFlight = fetch(url, {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            signal: controller ? controller.signal : undefined
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (payload) {
                if (!response.ok || !payload.ok) {
                    var error = new Error(payload.error || "Не удалось загрузить отчёт");
                    error.status = response.status;
                    throw error;
                }
                return payload;
            });
        }).then(function (payload) {
            if (generation !== requestGeneration || modal.hidden) return false;
            safeCacheWrite(payload);
            renderPayload(payload, false);
            modal.dataset.eoHourlyState = "ready";
            return true;
        }).catch(function (error) {
            if (error && error.name === "AbortError") return false;
            if (generation !== requestGeneration || modal.hidden) return false;
            var cached = safeCacheRead();
            if (cached) {
                renderPayload(cached, true);
                modal.dataset.eoHourlyState = "offline";
            } else {
                renderState(navigator.onLine === false
                    ? "Нет связи. Сохранённого отчёта пока нет."
                    : (error.message || "Почасовой отчёт временно недоступен"));
                modal.dataset.eoHourlyState = "error";
            }
            return false;
        }).finally(function () {
            if (generation === requestGeneration) {
                modal.dataset.eoHourlyLoading = "false";
                modal._eoHourlyAbortController = null;
            }
            inFlight = null;
            if (refreshQueued && modal && !modal.hidden) {
                refreshQueued = false;
                window.setTimeout(requestReport, 250);
            }
        });
        return inFlight;
    }

    function setUnderlyingBlocked(blocked) {
        var current = shell();
        if (!current) return;
        if (blocked) {
            current.setAttribute("inert", "");
            current.setAttribute("aria-hidden", "true");
        } else {
            current.removeAttribute("inert");
            current.removeAttribute("aria-hidden");
        }
    }

    function onLiveUpdate() {
        if (modal && !modal.hidden) requestReport("live-update");
    }

    function bindOpenLifecycle() {
        window.addEventListener("online", onLiveUpdate);
        window.addEventListener("native-connectivity-resume", onLiveUpdate);
        window.addEventListener("operational-state-update-available", onLiveUpdate);
        window.addEventListener("operational-state-refresh-applied", onLiveUpdate);
    }

    function unbindOpenLifecycle() {
        window.removeEventListener("online", onLiveUpdate);
        window.removeEventListener("native-connectivity-resume", onLiveUpdate);
        window.removeEventListener("operational-state-update-available", onLiveUpdate);
        window.removeEventListener("operational-state-refresh-applied", onLiveUpdate);
    }

    function finishClose() {
        if (!modal || modal.hidden) return;
        requestGeneration += 1;
        refreshQueued = false;
        if (modal._eoHourlyAbortController) modal._eoHourlyAbortController.abort();
        window.clearTimeout(hourTimer);
        hourTimer = 0;
        modal.hidden = true;
        modal.setAttribute("aria-hidden", "true");
        document.body.classList.remove("eo-hourly-report-open");
        setUnderlyingBlocked(false);
        unbindOpenLifecycle();
        historyOwned = false;
        var focusTarget = document.querySelector("[data-eo-hourly-report-open]") || opener;
        if (focusTarget && typeof focusTarget.focus === "function") focusTarget.focus();
        opener = null;
    }

    function requestClose() {
        if (!modal || modal.hidden) return;
        if (historyOwned && history.state && history.state[OPEN_STATE_KEY]) {
            history.back();
        } else {
            finishClose();
        }
    }

    function openReport(button) {
        if (!modal || !modal.hidden) {
            if (modal) modal.querySelector("[data-eo-hourly-report-close]").focus();
            return;
        }
        opener = button;
        modal.hidden = false;
        modal.setAttribute("aria-hidden", "false");
        document.body.classList.add("eo-hourly-report-open");
        setUnderlyingBlocked(true);
        bindOpenLifecycle();
        var cached = safeCacheRead();
        if (cached) renderPayload(cached, navigator.onLine === false);
        else renderState("Загружаем рейсы…");
        if (!(history.state && history.state[OPEN_STATE_KEY])) {
            var state = Object.assign({}, history.state || {});
            state[OPEN_STATE_KEY] = true;
            history.pushState(state, "", location.href);
            historyOwned = true;
        }
        modal.querySelector("[data-eo-hourly-report-close]").focus();
        requestReport("open");
    }

    function init() {
        modal = document.querySelector("[data-eo-hourly-report-modal]");
        if (!modal || modal.dataset.eoHourlyBound === "true") return;
        modal.dataset.eoHourlyBound = "true";
        content = modal.querySelector("[data-eo-hourly-report-content]");
        subtitle = modal.querySelector("[data-eo-hourly-report-subtitle]");

        document.addEventListener("click", function (event) {
            var openButton = event.target.closest && event.target.closest("[data-eo-hourly-report-open]");
            if (openButton) {
                event.preventDefault();
                openReport(openButton);
                return;
            }
            if (!modal.hidden && event.target.closest && event.target.closest("[data-eo-hourly-report-dismiss]")) {
                event.preventDefault();
                requestClose();
            }
        });
        document.addEventListener("keydown", function (event) {
            if (!modal.hidden && event.key === "Escape") {
                event.preventDefault();
                requestClose();
            }
        });
        window.addEventListener("popstate", function () {
            if (!modal.hidden) finishClose();
        });
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, {once: true});
    else init();

    window.ExcavatorHourlyReport = {
        init: init,
        refresh: requestReport,
        isOpen: function () { return Boolean(modal && !modal.hidden); }
    };
})();
