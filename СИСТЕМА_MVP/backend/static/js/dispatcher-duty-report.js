(function () {
    "use strict";
    const root = document.querySelector("[data-duty-report]");
    if (!root) return;
    const dialog = root.querySelector(".duty-dialog");
    const filter = dialog.querySelector("[data-duty-filter]");
    const log = dialog.querySelector("[data-duty-log]");
    const selection = dialog.querySelector("[data-duty-selection]");
    const status = root.querySelector(".duty-refresh-status");
    const feed = root.querySelector("[data-duty-slots]");
    const paging = feed.querySelector("[data-duty-paging]");
    let fetching = false;
    let pointerDown = false;
    let autoLoad = true;

    function applyFilter() {
        let visible = 0;
        let heading;
        for (const item of log.children) {
            if (!item.dataset.kind) {
                heading = item;
                heading.hidden = true;
                continue;
            }
            item.hidden = filter.value !== "all" &&
                (filter.value === "operation" ? item.dataset.kind !== "operation" : item.dataset.kind === "operation");
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
        dialog.querySelector("[data-duty-operation-summary]").textContent = slot.querySelector("[data-duty-operations]").textContent;
        log.replaceChildren(slot.querySelector("template").content.cloneNode(true));
        filter.value = "all";
        applyFilter();
        const mark = event.target.closest("[data-duty-mark]");
        const selected = mark && Array.from(log.querySelectorAll("[data-at]")).find(row =>
            row.dataset.kind === "operation" && row.dataset.at === mark.dataset.dutyMark);
        const interval = event.target.closest("[data-duty-interval]");
        selection.textContent = interval ? interval.dataset.dutyInterval : "";
        selection.hidden = !interval;
        if (selected) selected.classList.add("is-selected");
        dialog.showModal();
        dialog.scrollTop = 0;
        if (selected) selected.scrollIntoView({block: "nearest"});
    });
    dialog.querySelector("[data-duty-close]").addEventListener("click", () => dialog.close());
    filter.addEventListener("change", applyFilter);
    dialog.addEventListener("click", event => {
        if (event.target !== dialog) return;
        const rect = dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });

    function canUpdate() {
        return !document.hidden && !dialog.open && !pointerDown && !window.getSelection()?.toString();
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
        url.searchParams.set("since", first.dataset.dutyDay);
        fetching = true;
        try {
            const next = await readPage(url);
            if (!next || !canUpdate()) return;
            const days = [...next.querySelectorAll("[data-duty-day]")];
            if (!days.length) return;
            const focus = document.activeElement;
            const focusedSlot = focus.closest("[data-duty-slot]");
            const focusIndex = focusedSlot ? [...focusedSlot.querySelectorAll("[data-open-duty]")].indexOf(focus) : -1;
            const focusedDate = focusedSlot?.closest("[data-duty-day]").dataset.dutyDay;
            if (days.some(day => day.dataset.dutyDay === first.dataset.dutyDay)) {
                first.replaceWith(...days);
            } else {
                // After a long suspension, resume from a contiguous page with its own archive cursor.
                feed.querySelectorAll("[data-duty-day]").forEach(day => day.remove());
                paging.before(...days);
                updatePaging(next);
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
        }
    }
    paging.addEventListener("click", event => {
        if (!event.target.closest("[data-duty-older]") || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        loadOlder(true);
    });
    if ("IntersectionObserver" in window) {
        const observer = new IntersectionObserver(entries => {
            if (entries.some(entry => entry.isIntersecting)) loadOlder();
        }, {rootMargin: "0px 0px 180px"});
        observer.observe(paging);
    }
    root.addEventListener("pointerdown", () => { pointerDown = true; });
    window.addEventListener("pointerup", () => { pointerDown = false; });
    window.addEventListener("pointercancel", () => { pointerDown = false; });
    window.addEventListener("blur", () => { pointerDown = false; });
    window.setInterval(refresh, 30000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    window.addEventListener("pageshow", event => { if (event.persisted) refresh(); });
})();
