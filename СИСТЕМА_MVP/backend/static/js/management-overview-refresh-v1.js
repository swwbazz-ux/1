(function (window, document) {
    "use strict";

    const selector = "[data-management-overview-live]";

    window.applyOperationalStateRefresh = async function (context) {
        const live = document.querySelector(selector);
        if (!live || document.hidden) return {deferred: true, reason: "overview_not_visible"};

        const url = new URL(window.location.href);
        const date = document.querySelector(".management-dashboard-filters [name=date]");
        if (date && date.value) url.searchParams.set("date", date.value);
        url.searchParams.set("_operational_fragment", "management-overview");

        try {
            const response = await window.fetch(url, {
                credentials: "same-origin", cache: "no-store", headers: {"Accept": "application/json"}
            });
            if (!response.ok || response.redirected) throw new Error("overview response");
            const payload = await response.json();
            if (payload.contract !== "operational-fragment-v1" || payload.screen !== "management-overview"
                || !Number.isSafeInteger(payload.version) || !context.isCurrentSnapshot(payload.version)) {
                return {deferred: true, reason: "overview_stale_fragment"};
            }
            const fragment = new DOMParser().parseFromString(payload.html, "text/html").querySelector(selector);
            if (!fragment) throw new Error("overview root");
            if (!live.isConnected || document.hidden) return {deferred: true, reason: "overview_not_visible"};

            const scrollContainer = live.closest(".dispatcher-analytics-shell");
            const scrollPosition = scrollContainer ? scrollContainer.scrollTop : 0;
            live.replaceChildren(...fragment.childNodes);
            if (scrollContainer) scrollContainer.scrollTop = scrollPosition;
            return {applied: true, version: payload.version};
        } catch (error) {
            return {deferred: true, reason: "overview_fragment_failed"};
        }
    };
})(window, document);
