(function () {
    "use strict";
    const root = document.querySelector("[data-duty-report]");
    if (!root) return;
    const dialog = root.querySelector(".duty-dialog");
    const filter = dialog.querySelector("[data-duty-filter]");
    const log = dialog.querySelector("[data-duty-log]");
    const selection = dialog.querySelector("[data-duty-selection]");
    const status = root.querySelector(".duty-refresh-status");
    const slots = () => root.querySelector("[data-duty-slots]");
    let fetching = false;

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
        const date = slots().dataset.date.split("-").reverse().join(".");
        dialog.querySelector("[data-duty-period]").textContent = slot.querySelector("h3").textContent + " · " + date;
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

    function canRefresh() {
        return !document.hidden && !dialog.open && !root.querySelector(".duty-date-form").contains(document.activeElement);
    }
    async function refresh(force = false) {
        const old = slots();
        if (!old || fetching || !canRefresh() || (!force && old.dataset.live !== "true")) return;
        const url = new URL(location.href);
        url.searchParams.set("date", old.dataset.date);
        url.searchParams.set("partial", "1");
        fetching = true;
        try {
            const response = await fetch(url, {cache: "no-store", credentials: "same-origin", redirect: "manual"});
            if (response.status === 401 || response.status === 403 || response.type === "opaqueredirect") {
                if (dialog.open) dialog.close();
                log.replaceChildren();
                old.replaceChildren();
                location.reload();
                return;
            }
            if (!response.ok) throw new Error("refresh");
            const page = new DOMParser().parseFromString(await response.text(), "text/html");
            const next = page.querySelector("[data-duty-slots]");
            if (!next || next.dataset.date !== old.dataset.date) throw new Error("format");
            if (canRefresh()) {
                const focus = document.activeElement;
                const oldSlot = focus.closest("[data-duty-slot]");
                const controls = "[data-open-duty]";
                const index = oldSlot ? Array.from(oldSlot.querySelectorAll(controls)).indexOf(focus) : -1;
                old.replaceWith(next);
                if (index >= 0) {
                    const newSlot = Array.from(next.querySelectorAll("[data-duty-slot]")).find(item => item.dataset.dutySlot === oldSlot.dataset.dutySlot);
                    newSlot?.querySelectorAll(controls)[index]?.focus({preventScroll: true});
                }
                status.textContent = "";
            }
        } catch {
            status.textContent = "Не удалось обновить данные. На экране последний полученный срез.";
        } finally {
            fetching = false;
        }
    }
    window.setInterval(refresh, 30000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    window.addEventListener("pageshow", event => { if (event.persisted) refresh(true); });
})();
