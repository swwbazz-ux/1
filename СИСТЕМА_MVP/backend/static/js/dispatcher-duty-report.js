(function () {
    "use strict";
    const root = document.querySelector("[data-duty-report]");
    if (!root) return;
    const dialog = root.querySelector(".duty-dialog");
    const filter = dialog.querySelector("[data-duty-filter]");
    const overview = dialog.querySelector("[data-duty-overview]");
    const dialogContent = dialog.querySelector(".duty-dialog-content");
    const log = dialog.querySelector("[data-duty-log]");
    const selection = dialog.querySelector("[data-duty-selection]");
    const selectionText = selection.querySelector("[data-duty-selection-text]");
    const jump = selection.querySelector("[data-duty-jump]");
    const bandSelection = dialog.querySelector("[data-duty-band-selection]");
    const bandLabel = bandSelection.querySelector("[data-duty-band-label]");
    const status = root.querySelector(".duty-refresh-status");
    const history = root.querySelector(".duty-history");
    const feed = root.querySelector("[data-duty-slots]");
    const paging = feed.querySelector("[data-duty-paging]");
    let fetching = false;
    let pointerDown = false;
    let autoLoad = true;
    let pagingInView = false;
    let selectedBand = null;
    let selectedEvent = null;

    function setBand(button) {
        selectedBand = button?.dataset.dutyBand ?? null;
        for (const band of overview.querySelectorAll("[data-duty-band]")) {
            band.setAttribute("aria-pressed", String(band === button));
        }
        bandSelection.hidden = !button;
        bandLabel.textContent = button ? "Операции · " + button.dataset.bandHours : "";
    }

    function applyFilter() {
        let visible = 0;
        let heading;
        for (const item of log.children) {
            if (!item.dataset.kind) {
                heading = item;
                heading.hidden = true;
                continue;
            }
            const isOperation = item.dataset.kind === "operation";
            item.hidden = (filter.value === "operation" && !isOperation) ||
                (filter.value === "duty" && isOperation) ||
                (selectedBand !== null && item.dataset.operationBand !== selectedBand);
            if (!item.hidden) {
                if (heading) heading.hidden = false;
                visible++;
            }
        }
        dialog.querySelector("[data-duty-none]").hidden = Boolean(visible);
    }
    root.addEventListener("click", event => {
        const button = event.target.closest("[data-open-duty]");
        if (!button) return;
        const slot = button.closest("[data-duty-slot]");
        const date = slot.closest("[data-duty-day]").querySelector("time").textContent;
        dialog.querySelector("[data-duty-period]").textContent = slot.querySelector("h4").textContent + " · " + date;
        dialog.querySelector("[data-duty-staff]").textContent = Array.from(slot.querySelector(".duty-staff").children, row => row.textContent).join(" / ");
        overview.replaceChildren(slot.querySelector("template[data-duty-overview]").content.cloneNode(true));
        log.replaceChildren(slot.querySelector("template[data-duty-entries]").content.cloneNode(true));
        filter.value = "all";
        setBand(null);
        applyFilter();
        const mark = event.target.closest("[data-duty-mark]");
        const windowMark = event.target.closest("[data-duty-window-mark]");
        selectedEvent = windowMark
            ? Array.from(log.querySelectorAll("[data-window-event]")).find(row => row.dataset.windowEvent === windowMark.dataset.dutyWindowMark)
            : mark && Array.from(log.querySelectorAll("[data-at]")).find(row =>
                row.dataset.kind === "operation" && row.dataset.at === mark.dataset.dutyMark);
        const interval = event.target.closest("[data-duty-interval]");
        selectionText.textContent = selectedEvent
            ? selectedEvent.querySelector("time").textContent + " · " + selectedEvent.querySelector("strong").textContent
            : interval?.dataset.dutyInterval || "";
        selection.hidden = !selectionText.textContent;
        jump.hidden = !selectedEvent;
        selectedEvent?.classList.add("is-selected");
        dialog.showModal();
        dialogContent.scrollTop = 0;
    });
    dialog.querySelector("[data-duty-close]").addEventListener("click", () => dialog.close());
    filter.addEventListener("change", () => {
        setBand(null);
        applyFilter();
    });
    overview.addEventListener("click", event => {
        const button = event.target.closest("[data-duty-band]");
        if (!button) return;
        setBand(button.dataset.dutyBand === selectedBand ? null : button);
        filter.value = "operation";
        applyFilter();
        if (selectedBand !== null) bandSelection.scrollIntoView({block: "nearest"});
    });
    bandSelection.querySelector("[data-duty-clear-band]").addEventListener("click", () => {
        setBand(null);
        applyFilter();
        filter.focus({preventScroll: true});
    });
    jump.addEventListener("click", () => {
        if (!selectedEvent) return;
        if (selectedEvent.hidden) {
            filter.value = "all";
            setBand(null);
            applyFilter();
        }
        selectedEvent.focus({preventScroll: true});
        selectedEvent.scrollIntoView({block: "nearest"});
    });
    dialog.addEventListener("click", event => {
        if (event.target !== dialog) return;
        const rect = dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });

    function canUpdate() {
        return !document.hidden && !dialog.open && !pointerDown &&
            !history?.contains(document.activeElement) && !window.getSelection()?.toString();
    }
    async function readPage(url) {
        url.searchParams.set("partial", "1");
        const response = await fetch(url, {cache: "no-store", credentials: "same-origin", redirect: "manual"});
        if (response.status === 401 || response.status === 403 || response.type === "opaqueredirect") {
            if (dialog.open) dialog.close();
            log.replaceChildren();
            feed.replaceChildren();
            location.reload();
            return null;
        }
        if (!response.ok) throw new Error("refresh");
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const next = page.querySelector("[data-duty-slots]");
        if (!next || !next.querySelector("[data-duty-paging]")) throw new Error("format");
        return next;
    }
    function updatePaging(next) {
        paging.replaceChildren(...next.querySelector("[data-duty-paging]").children);
    }
    function loadVisibleHistory() {
        if (pagingInView) loadOlder();
    }
    async function loadOlder(manual = false) {
        const link = paging.querySelector("[data-duty-older]");
        if (fetching || !link || !canUpdate() || (!manual && !autoLoad)) return;
        fetching = true;
        try {
            const next = await readPage(new URL(link.href));
            if (!next || !canUpdate()) return;
            const days = [...next.querySelectorAll("[data-duty-day]")];
            paging.before(...days);
            const moveFocus = manual && document.activeElement === link;
            updatePaging(next);
            if (moveFocus) days[0]?.querySelector("[data-open-duty]")?.focus({preventScroll: true});
            autoLoad = true;
            status.textContent = "";
        } catch {
            autoLoad = false;
            status.textContent = "Не удалось загрузить предыдущие смены. Попробуйте ещё раз.";
        } finally {
            fetching = false;
        }
    }
    async function refresh() {
        const first = feed.querySelector("[data-duty-day]");
        if (!first || fetching || !canUpdate()) return;
        const url = new URL(location.pathname, location.origin);
        const visible = Array.from(feed.querySelectorAll("[data-duty-day]")).filter(day => {
            const rect = day.getBoundingClientRect();
            return rect.bottom > 0 && rect.top < window.innerHeight;
        });
        const before = visible[0]?.previousElementSibling?.dataset.dutyDay;
        if (before) url.searchParams.set("before", before);
        url.searchParams.set("since", (visible.at(-1) || first).dataset.dutyDay);
        fetching = true;
        try {
            let next = await readPage(url);
            if (!next || !canUpdate()) return;
            const historyChanged = feed.dataset.historyFrom !== next.dataset.historyFrom;
            if (historyChanged) {
                next = await readPage(new URL(location.pathname, location.origin));
                if (!next || !canUpdate()) return;
            }
            const days = [...next.querySelectorAll("[data-duty-day]")];
            if (!days.length) return;
            const focus = document.activeElement;
            const focusedSlot = focus.closest("[data-duty-slot]");
            const focusIndex = focusedSlot ? [...focusedSlot.querySelectorAll("[data-open-duty]")].indexOf(focus) : -1;
            const focusedDate = focusedSlot?.closest("[data-duty-day]").dataset.dutyDay;
            if (!historyChanged && (before || days.some(day => day.dataset.dutyDay === first.dataset.dutyDay))) {
                const existing = new Map(Array.from(feed.querySelectorAll("[data-duty-day]"), day => [day.dataset.dutyDay, day]));
                for (const day of days) {
                    const old = existing.get(day.dataset.dutyDay);
                    if (old) old.replaceWith(day);
                    else if (!before) first.before(day);
                }
            } else {
                // After a long suspension, resume from a contiguous page with its own archive cursor.
                feed.querySelectorAll("[data-duty-day]").forEach(day => day.remove());
                paging.before(...days);
                updatePaging(next);
            }
            feed.dataset.historyFrom = next.dataset.historyFrom;
            if (history) {
                const field = history.querySelector('input[name="history_from"]');
                field.max = next.dataset.date;
                if (historyChanged) field.value = next.dataset.historyFrom;
            }
            if (focusIndex >= 0 && !focus.isConnected) {
                const day = days.find(item => item.dataset.dutyDay === focusedDate);
                day?.querySelector(`[data-duty-slot="${focusedSlot.dataset.dutySlot}"]`)
                    ?.querySelectorAll("[data-open-duty]")[focusIndex]?.focus({preventScroll: true});
            }
            root.querySelector("[data-duty-as-of]").textContent = next.dataset.asOf;
            if (feed.dataset.date !== next.dataset.date) {
                const header = root.querySelector("[data-management-workplace-header]");
                header.querySelector("[data-management-overview-date]").textContent = next.dataset.date.split("-").reverse().join(".");
                for (const link of header.querySelectorAll("nav a[href*='?']")) {
                    const target = new URL(link.href);
                    for (const [key, value] of target.searchParams) {
                        if (value === feed.dataset.date) target.searchParams.set(key, next.dataset.date);
                    }
                    link.href = target.href;
                }
                feed.dataset.date = next.dataset.date;
            }
            status.textContent = "";
        } catch {
            status.textContent = "Не удалось обновить данные. На экране последний полученный срез.";
        } finally {
            fetching = false;
            loadVisibleHistory();
        }
    }
    paging.addEventListener("click", event => {
        if (!event.target.closest("[data-duty-older]") || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        loadOlder(true);
    });
    if ("IntersectionObserver" in window) {
        const observer = new IntersectionObserver(entries => {
            pagingInView = entries[entries.length - 1].isIntersecting;
            loadVisibleHistory();
        }, {rootMargin: "0px 0px 180px"});
        observer.observe(paging);
    }
    function endPointerInteraction() {
        pointerDown = false;
        loadVisibleHistory();
    }
    root.addEventListener("pointerdown", () => { pointerDown = true; });
    window.addEventListener("pointerup", endPointerInteraction);
    window.addEventListener("pointercancel", endPointerInteraction);
    window.addEventListener("blur", () => { pointerDown = false; });
    dialog.addEventListener("close", loadVisibleHistory);
    window.setInterval(refresh, 30000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    window.addEventListener("pageshow", event => { if (event.persisted) refresh(); });
})();
