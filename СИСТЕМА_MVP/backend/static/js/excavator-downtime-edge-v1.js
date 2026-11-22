(function () {
    "use strict";

    function syncNative(hidden) {
        var plugins = window.Capacitor && window.Capacitor.Plugins;
        var plugin = plugins && plugins.ExcavatorNavigation;
        if (!plugin || typeof plugin.setDowntimeEdge !== "function") return;
        var edge = document.querySelector("[data-eo-downtime-edge]");
        var rect = !hidden && edge && edge.getBoundingClientRect();
        var visible = rect && rect.height > 0 && window.innerHeight > 0;
        plugin.setDowntimeEdge(visible ? {
            top: Math.max(0, rect.top / window.innerHeight),
            bottom: Math.min(1, rect.bottom / window.innerHeight)
        } : {}).catch(function () {});
    }

    window.addEventListener("resize", function () { syncNative(); });
    window.addEventListener("pageshow", function () { syncNative(); });
    window.addEventListener("pagehide", function () { syncNative(true); });

    function bind(shell, panel, switchTab) {
        var work = shell.querySelector('[data-eo-screen="trucks"]');
        if (!work || !panel) return null;
        var drag = null;
        var settle = null;
        var suppressClick = false;

        function reset() {
            if (settle) settle.cancel();
            settle = null;
            drag = null;
            var origin = shell.dataset.eoDowntimePreview;
            if (!origin) return;
            shell.dataset.eoActiveTab = origin;
            work.hidden = origin !== "trucks";
            work.removeAttribute("inert");
            shell.removeAttribute("data-eo-downtime-preview");
            panel.style.removeProperty("--eo-downtime-offset");
            panel.removeAttribute("inert");
            panel.removeAttribute("aria-hidden");
            panel.hidden = origin !== "events";
        }

        shell.addEventListener("pointerdown", function (event) {
            if (!event.isPrimary || event.button !== 0 || drag || settle) return;
            suppressClick = false;
            if (event.pointerType === "mouse") return;
            var origin = shell.dataset.eoActiveTab;
            if (origin !== "trucks" && origin !== "events") return;
            var opening = origin === "trucks";
            if (!(opening ? work : panel).contains(event.target)) return;
            if (opening && event.target.closest('button, a, input, select, textarea, [role="button"], [data-eo-truck-card], [data-eo-dump-target]')) return;
            if (document.body.classList.contains("modal-open") ||
                Array.from(document.querySelectorAll('[aria-modal="true"], dialog[open]')).some(function (dialog) {
                    return dialog.getClientRects().length > 0;
                })) return;
            drag = {id: event.pointerId, x: event.clientX, y: event.clientY, opening: opening, offset: 0};
        });

        shell.addEventListener("pointermove", function (event) {
            if (!drag || event.pointerId !== drag.id) return;
            var dx = (event.clientX - drag.x) * (drag.opening ? -1 : 1);
            var dy = Math.abs(event.clientY - drag.y);
            if (!shell.hasAttribute("data-eo-downtime-preview")) {
                if (Math.max(dy, Math.abs(dx)) >= 8) suppressClick = true;
                if ((dy > 8 && dy >= Math.abs(dx)) || dx < -8) { reset(); return; }
                if (dx < 8 || dx < dy * 1.3) return;
                // The live Work dashboard is the backdrop in both directions.
                // Tab selection and persistence only change after a completed swipe.
                shell.dataset.eoDowntimePreview = shell.dataset.eoActiveTab;
                shell.dataset.eoActiveTab = "trucks";
                work.hidden = false;
                work.setAttribute("inert", "");
                panel.setAttribute("inert", "");
                panel.setAttribute("aria-hidden", "true");
                panel.hidden = false;
            }
            var distance = Math.min(shell.clientWidth, Math.max(0, dx));
            drag.offset = drag.opening ? shell.clientWidth - distance : distance;
            panel.style.setProperty("--eo-downtime-offset", drag.offset + "px");
        });

        shell.addEventListener("pointerup", function (event) {
            if (!drag || event.pointerId !== drag.id) return;
            if (!shell.hasAttribute("data-eo-downtime-preview")) { reset(); return; }
            var width = shell.clientWidth;
            var distance = (event.clientX - drag.x) * (drag.opening ? -1 : 1);
            var completed = distance >= Math.min(84, width * 0.23);
            var origin = shell.dataset.eoDowntimePreview;
            var destination = drag.opening ? "events" : "trucks";
            var finalTab = completed ? destination : origin;
            var end = finalTab === "events" ? 0 : width;
            var start = drag.offset;
            drag = null;
            function finish() {
                reset();
                if (completed && shell.isConnected) switchTab(destination);
            }
            if (!panel.animate || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
                finish();
                return;
            }
            settle = panel.animate([
                {transform: "translateX(" + start + "px)"},
                {transform: "translateX(" + end + "px)"}
            ], {duration: 150, easing: "ease-out", fill: "forwards"});
            settle.onfinish = finish;
        });

        function cancelPointer(event) {
            if (drag && event.pointerId === drag.id) reset();
        }
        shell.addEventListener("pointercancel", cancelPointer);
        shell.addEventListener("lostpointercapture", cancelPointer);
        shell.addEventListener("click", function (event) {
            if (!suppressClick || event.detail === 0) return;
            suppressClick = false;
            event.preventDefault();
            event.stopImmediatePropagation();
        }, true);
        return {reset: reset, syncNative: syncNative};
    }

    window.ExcavatorDowntimeEdge = {bind: bind};
})();
