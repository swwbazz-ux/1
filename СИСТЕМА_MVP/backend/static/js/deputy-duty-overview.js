(() => {
    "use strict";
    const overview = document.querySelector("[data-duty-overview]");
    if (!overview) return;
    const categories = document.querySelector(".deputy-duty-page .deputy-category-nav");
    if (categories) categories.scrollLeft = categories.scrollWidth - categories.clientWidth;
    if (!window.fetch || !window.AbortController) return;

    const filters = overview.querySelector("[data-duty-filters]");
    const search = filters.elements.q;
    const profession = filters.elements.profession;
    const status = filters.elements.status;
    const reset = filters.querySelector("[data-duty-reset]");
    const notice = overview.querySelector("[data-duty-request-error]");
    let timer;
    let request;
    let typing = false;

    function filterUrl() {
        const url = new URL(filters.action);
        if (search.value.trim()) url.searchParams.set("q", search.value.trim());
        if (profession.value) url.searchParams.set("profession", profession.value);
        if (status.value !== "all") url.searchParams.set("status", status.value);
        return url;
    }

    function cancelPending() {
        clearTimeout(timer);
        if (request) request.abort();
        request = null;
    }

    function setBusy(busy) {
        const results = overview.querySelector("[data-duty-results]");
        results.setAttribute("aria-busy", String(busy));
        results.inert = busy;
    }

    async function show(url, historyMode) {
        cancelPending();
        const current = new AbortController();
        request = current;
        setBusy(true);
        notice.hidden = true;
        try {
            const response = await fetch(url, {headers: {"X-Duty-Overview": "1"}, signal: current.signal});
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            if (!response.ok) throw new Error();
            const next = new DOMParser().parseFromString(await response.text(), "text/html");
            const updated = next.querySelector("[data-duty-overview]");
            if (current.signal.aborted) return;
            if (!updated) throw new Error();

            const focusKey = document.activeElement?.dataset.dutyStatus;
            overview.querySelector(".duty-heading").replaceWith(updated.querySelector(".duty-heading"));
            overview.querySelector(".duty-metrics").replaceWith(updated.querySelector(".duty-metrics"));
            overview.querySelector("[data-duty-results]").replaceWith(updated.querySelector("[data-duty-results]"));
            const nextForm = updated.querySelector("[data-duty-filters]");
            const nextProfession = nextForm.elements.profession.value;
            profession.replaceChildren(...Array.from(nextForm.elements.profession.options));
            profession.value = nextProfession;
            status.value = nextForm.elements.status.value;
            reset.hidden = updated.querySelector("[data-duty-reset]").hidden;
            if (historyMode && url.href !== location.href) history[historyMode](null, "", url);
            if (focusKey) overview.querySelector('[data-duty-status="' + focusKey + '"]')?.focus({preventScroll: true});
        } catch (error) {
            if (current.signal.aborted) return;
            notice.textContent = "Показан предыдущий результат. Нажмите «Найти», чтобы повторить поиск.";
            notice.hidden = false;
        } finally {
            if (request === current) setBusy(false);
        }
    }

    function filterNow() {
        typing = false;
        show(filterUrl(), "pushState");
    }

    filters.addEventListener("submit", event => {
        event.preventDefault();
        filterNow();
    });
    profession.addEventListener("change", filterNow);
    search.addEventListener("input", event => {
        cancelPending();
        setBusy(true);
        if (event.isComposing) return;
        const mode = typing ? "replaceState" : "pushState";
        timer = setTimeout(() => {
            typing = true;
            show(filterUrl(), mode);
        }, 250);
    });
    overview.addEventListener("click", event => {
        const link = event.target.closest("a[data-duty-status], a[data-duty-reset]");
        if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        if (link === reset) {
            search.value = "";
            profession.value = "";
            status.value = "all";
            search.focus();
        } else {
            status.value = link.dataset.dutyStatus;
        }
        filterNow();
    });
    window.addEventListener("popstate", () => {
        typing = false;
        const url = new URL(location.href);
        search.value = url.searchParams.get("q") || "";
        show(url);
    });
})();
