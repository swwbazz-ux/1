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

    function bind(shell, panel, openDowntime) {
        if (!panel) return null;
        var drag = null;
        var settle = null;
        var suppressClick = false;

        function reset() {
            if (settle) settle.cancel();
            settle = null;
            drag = null;
            if (!shell.hasAttribute("data-eo-downtime-preview")) return;
            shell.removeAttribute("data-eo-downtime-preview");
            panel.style.removeProperty("--eo-downtime-offset");
            panel.removeAttribute("inert");
            panel.removeAttribute("aria-hidden");
            panel.hidden = shell.dataset.eoActiveTab !== "events";
        }

        shell.addEventListener("pointerdown", function (event) {
            if (!event.isPrimary || event.button !== 0 || drag || settle) return;
            suppressClick = false;
            if (event.pointerType === "mouse" || shell.dataset.eoActiveTab !== "trucks") return;
            if (!event.target.closest('[data-eo-screen="trucks"]') ||
                event.target.closest('button, a, input, select, textarea, [role="button"], [data-eo-truck-card], [data-eo-dump-target]')) return;
            if (document.body.classList.contains("modal-open") ||
                Array.from(document.querySelectorAll('[aria-modal="true"], dialog[open]')).some(function (dialog) {
                    return dialog.getClientRects().length > 0;
                })) return;
            drag = {id: event.pointerId, x: event.clientX, y: event.clientY, distance: 0};
        });

        shell.addEventListener("pointermove", function (event) {
            if (!drag || event.pointerId !== drag.id) return;
            var dx = drag.x - event.clientX;
            var dy = Math.abs(event.clientY - drag.y);
            if (!shell.hasAttribute("data-eo-downtime-preview")) {
                if (dy > 8 && dy >= dx) { reset(); return; }
                if (dx < 8 || dx < dy * 1.3) return;
                shell.setAttribute("data-eo-downtime-preview", "");
                suppressClick = true;
                panel.setAttribute("inert", "");
                panel.setAttribute("aria-hidden", "true");
                panel.hidden = false;
            }
            drag.distance = Math.min(shell.clientWidth, Math.max(0, dx));
            panel.style.setProperty("--eo-downtime-offset", (shell.clientWidth - drag.distance) + "px");
        });

        shell.addEventListener("pointerup", function (event) {
            if (!drag || event.pointerId !== drag.id) return;
            if (!shell.hasAttribute("data-eo-downtime-preview")) { reset(); return; }
            var width = shell.clientWidth;
            var open = drag.x - event.clientX >= Math.min(84, width * 0.23);
            var start = width - drag.distance;
            drag = null;
            function finish() {
                reset();
                if (open && shell.isConnected) openDowntime();
            }
            if (!panel.animate || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
                finish();
                return;
            }
            settle = panel.animate([
                {transform: "translateX(" + start + "px)"},
                {transform: "translateX(" + (open ? 0 : width) + "px)"}
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
