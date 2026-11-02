/* This adapter only replaces the tablet's existing Reports link. */
(function (win, doc) {
    "use strict";
    if (win.top !== win || win.CopperTabletPlanningEntry) return;
    const path = "/mining-master/tablet/", hash = "#rasstanovka";
    const offline = doc.documentElement.hasAttribute("data-cr-saved-board");
    const isControl = location.pathname === path;
    const target = path + hash;
    if (!offline && (doc.body?.dataset.appRoleCode !== "mining_master" ||
            doc.body.dataset.observerMode === "true")) return;

    function links() {
        for (const link of doc.querySelectorAll(".dispatcher-nav a:not([data-cr-planning-tab])")) {
            if (!/^Отч[её]ты$/i.test(link.textContent.trim())) continue;
            link.dataset.crPlanningTab = "";
            link.removeAttribute("data-mm-tab");
            link.href = target;
            (link.querySelector("span") || link).textContent = "Расстановка";
        }
    }
    let opening = false;
    function leave() {
        if (location.hash === hash) history.replaceState(history.state, "", path + (offline ? "#cr-saved-board" : ""));
        if (offline) void win.CopperTabletPlanningCache.showActivated().catch(()=>{});
    }
    async function show() {
        if (opening || win.CopperTabletPlanning?.isOpen()) return;
        opening = true;
        try {
            await win.CopperTabletPlanning.open({ document: doc, offline, liveDocument: offline ? null : doc,
                onClose: leave });
        } catch (error) {
            leave();
            const msg = doc.createElement("div");
            msg.setAttribute("role","alert");
            msg.className = "dispatcher-messages";
            msg.textContent = error.message || "Не удалось открыть расстановку. Повторите попытку.";
            doc.querySelector(".dispatcher-shell")?.prepend(msg);
            setTimeout(() => msg.remove(), 6500);
        } finally { opening = false; }
    }
    function syncHash() {
        if (location.hash === hash && isControl) void show();
        else if (win.CopperTabletPlanning?.isOpen()) win.CopperTabletPlanning.close();
    }
    doc.addEventListener("click", event => {
        const link = event.target.closest?.("a[data-cr-planning-tab]");
        if (!link || !isControl || event.defaultPrevented) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (location.hash !== hash) history.pushState(history.state, "", target);
        void show();
    }, true);
    win.addEventListener("popstate", syncHash);
    win.addEventListener("hashchange", syncHash);
    win.addEventListener("copper-tablet-planning-applied", () => {
        // Use the same fragment update and busy guard as the regular pult.
        if (!offline && typeof win.applyOperationalStateRefresh === "function")
            void Promise.resolve(win.applyOperationalStateRefresh({eventsTruncated:true,events:[]})).catch(()=>{});
    });
    let observed = false;
    function install() {
        links();
        if (!observed) {
            observed = true;
            // The live fragment occasionally replaces the whole header/board.
            new MutationObserver(records => {
                if (records.some(row => [...row.addedNodes].some(node =>
                    node.nodeType === 1 && (node.matches(".dispatcher-nav,.dispatcher-nav a,.dispatcher-topbar,.dispatcher-board") ||
                    node.querySelector(".dispatcher-nav"))))) links();
            }).observe(doc.querySelector(".dispatcher-shell") || doc.body, {childList:true,subtree:true});
        }
        if (location.hash === hash) syncHash();
        if (!offline && isControl) {
            win.CopperTabletPlanningCache?.watch();
            void win.CopperTabletDraft?.resume({document:doc}).catch(()=>{});
        }
    }
    win.CopperTabletPlanningEntry = {install};
    if (offline) win.CopperTabletPlanningCache.restore().then(install).catch(() => { /* the native retry action stays visible */ });
    else install();
})(window, document);
