/* Барабан простоев под циферблатом водителя.
   Делегирование на document: разметка оболочки может подменяться целиком
   (submitDriverFormInPlace), поэтому элементы ищутся в момент события, а не
   запоминаются при загрузке. Запуск простоя — клик по скрытой кнопке причины
   на вкладке «Простои»: там уже живут офлайн-очередь, озвучка, тосты и
   переключение на вкладку «Работа». */
(function (root) {
    "use strict";

    if (root.__driverDowntimeDrumBound) return;
    root.__driverDowntimeDrumBound = true;

    var LIFT_START = 10;   // px вертикального хода, после которого карточка едет за пальцем
    var LIFT_ARM = 44;     // px, с которых круг подсвечивается «готово»
    var LIFT_TRIGGER = 64; // px, отпускание выше — старт простоя
    var LIFT_MAX = 96;
    var doc = root.document;

    function q(sel, base) { return (base || doc).querySelector(sel); }
    function all(sel, base) { return Array.prototype.slice.call((base || doc).querySelectorAll(sel)); }
    function drum() { return q("[data-driver-downtime-drum]"); }
    function track() { return q("[data-driver-drum-track]"); }
    function dial() { return q(".driver-work-dial"); }
    function stateCard() { return q("[data-driver-active-downtime-id]"); }
    function activeReasonId() {
        var card = stateCard();
        return card ? String(card.dataset.driverActiveReasonId || "") : "";
    }

    function centerCard() {
        var t = track();
        if (!t) return null;
        var mid = t.getBoundingClientRect().left + t.clientWidth / 2;
        var best = null, bestDist = Infinity;
        all("[data-driver-drum-card]", t).forEach(function (card) {
            var r = card.getBoundingClientRect();
            var d = Math.abs(r.left + r.width / 2 - mid);
            if (d < bestDist) { bestDist = d; best = card; }
        });
        return best;
    }

    // Объём барабана: чем дальше карточка от центра, тем сильнее она повёрнута
    // вокруг вертикальной оси и утоплена вглубь, как грань револьверного барабана.
    var ROT_DEG = 34, DEPTH_PX = 80, SCALE_DROP = .08;
    var lastCenter = null;

    function markCenter() {
        var t = track();
        if (!t) return;
        var mid = t.getBoundingClientRect().left + t.clientWidth / 2;
        var half = Math.max(1, t.clientWidth / 2);
        var best = null, bestDist = Infinity;
        all("[data-driver-drum-card]", t).forEach(function (card) {
            var r = card.getBoundingClientRect();
            var offset = (r.left + r.width / 2 - mid) / half;
            var k = Math.max(-1.6, Math.min(1.6, offset));
            var a = Math.abs(k);
            card.style.setProperty("--drum-rot", (k * ROT_DEG).toFixed(2) + "deg");
            card.style.setProperty("--drum-depth", (-a * DEPTH_PX).toFixed(1) + "px");
            card.style.setProperty("--drum-scale", (1 - Math.min(1, a) * SCALE_DROP).toFixed(3));
            card.style.setProperty("--drum-fade", (Math.max(0, 1 - a * .42)).toFixed(3));
            var d = Math.abs(offset);
            if (d < bestDist) { bestDist = d; best = card; }
        });
        all("[data-driver-drum-card]", t).forEach(function (card) {
            card.classList.toggle("is-center", card === best);
        });
        // Щелчок фиксации: новая карточка встала в центр.
        if (best && lastCenter && best !== lastCenter && realCardFor(best) !== realCardFor(lastCenter)) {
            if (root.navigator && typeof root.navigator.vibrate === "function") {
                try { root.navigator.vibrate(9); } catch (e) {}
            }
        }
        lastCenter = best;
    }

    function scrollToCard(card, smooth) {
        var t = track();
        if (!t || !card) return;
        var target = card.offsetLeft + card.offsetWidth / 2 - t.clientWidth / 2;
        if (typeof t.scrollTo === "function") {
            try { t.scrollTo({ left: target, behavior: smooth ? "smooth" : "auto" }); return; } catch (e) {}
        }
        t.scrollLeft = target;
    }

    function syncActive() {
        var id = activeReasonId();
        var d = drum(), w = dial();
        var activeCard = null, activeDist = Infinity;
        var t = track();
        var mid = t ? t.getBoundingClientRect().left + t.clientWidth / 2 : 0;
        all("[data-driver-drum-card]").forEach(function (card) {
            var on = id !== "" && String(card.dataset.driverDrumReasonId) === id;
            card.classList.toggle("is-active-downtime", on);
            if (on) {
                // Кольцо содержит копии: ведём к ближайшей, чтобы не крутить барабан через весь круг.
                var r = card.getBoundingClientRect();
                var dist = Math.abs(r.left + r.width / 2 - mid);
                if (dist < activeDist) { activeDist = dist; activeCard = card; }
            }
        });
        if (d) d.classList.toggle("is-active", id !== "");
        if (w) w.classList.toggle("is-downtime-active", id !== "");
        if (activeCard) scrollToCard(activeCard, true);
        markCenter();
        drawLink();
    }

    function syncTotals() {
        all("[data-driver-drum-card]").forEach(function (card) {
            var src = q('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + card.dataset.driverDrumReasonId + '"]');
            var total = q("[data-driver-drum-total]", card);
            var srcTotal = src ? q("[data-driver-reason-duration]", src) : null;
            if (!total || !srcTotal) return;
            // Пишем только при изменении: сама запись — мутация, которую видит наблюдатель.
            if (total.textContent !== srcTotal.textContent) total.textContent = srcTotal.textContent;
            if (total.hidden !== srcTotal.hidden) total.hidden = srcTotal.hidden;
        });
    }

    function startDowntime(card) {
        card = realCardFor(card);
        var id = card.dataset.driverDrumReasonId;
        var button = q('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + id + '"]');
        if (!button) {
            if (typeof root.showDriverToast === "function") root.showDriverToast("Причина простоя недоступна");
            return;
        }
        if (root.navigator && typeof root.navigator.vibrate === "function") {
            try { root.navigator.vibrate([16, 40, 24]); } catch (e) {}
        }
        button.click();
    }

    // --- жест: вверх на круг ---
    var drag = null;

    function resetDrag() {
        if (!drag) return;
        var card = drag.card;
        card.classList.remove("is-lifting", "is-armed");
        card.classList.add("is-settling");
        card.style.setProperty("--drum-lift", "0px");
        root.setTimeout(function () { card.classList.remove("is-settling"); }, 260);
        var w = dial();
        if (w) w.classList.remove("is-drum-lifting");
        var t = track();
        if (t) t.classList.remove("is-locked");
        if (drag.captured && typeof card.releasePointerCapture === "function") {
            try { card.releasePointerCapture(drag.pointerId); } catch (e) {}
        }
        drag = null;
    }

    doc.addEventListener("pointerdown", function (event) {
        var card = event.target && event.target.closest ? event.target.closest("[data-driver-drum-card]") : null;
        if (!card || event.button > 0) return;
        drag = { card: card, pointerId: event.pointerId, x0: event.clientX, y0: event.clientY, dy: 0, captured: false, moved: false };
    }, true);

    doc.addEventListener("pointermove", function (event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        var dx = event.clientX - drag.x0;
        var dy = event.clientY - drag.y0;
        drag.dy = dy;
        if (!drag.moved) {
            // Горизонтальный ход отдаём нативной прокрутке барабана.
            if (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) { drag = null; return; }
            if (-dy < LIFT_START) return;
            if (!drag.card.classList.contains("is-center")) { drag = null; return; }
            drag.moved = true;
            drag.card.classList.add("is-lifting");
            var t = track();
            if (t) t.classList.add("is-locked");
            if (typeof drag.card.setPointerCapture === "function") {
                try { drag.card.setPointerCapture(event.pointerId); drag.captured = true; } catch (e) {}
            }
        }
        var lift = Math.max(-LIFT_MAX, Math.min(0, dy));
        drag.card.style.setProperty("--drum-lift", lift + "px");
        var armed = -dy >= LIFT_ARM;
        drag.card.classList.toggle("is-armed", armed);
        var w = dial();
        if (w) w.classList.toggle("is-drum-lifting", armed);
        event.preventDefault();
    }, { passive: false, capture: true });

    doc.addEventListener("pointerup", function (event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        var card = drag.card, moved = drag.moved, dy = drag.dy;
        resetDrag();
        if (moved) {
            if (-dy >= LIFT_TRIGGER) startDowntime(card);
        } else if (!card.classList.contains("is-center")) {
            scrollToCard(card, true); // тап по боковой карточке подводит её в центр
        }
    }, true);

    doc.addEventListener("pointercancel", function (event) {
        if (drag && event.pointerId === drag.pointerId) resetDrag();
    }, true);

    // --- кольцо: копии крайних карточек с обеих сторон, чтобы барабан крутился по кругу ---
    var RING_COPIES = 4;

    function buildRing() {
        var t = track();
        if (!t || t.dataset.driverDrumRing === "1") return;
        var cards = all("[data-driver-drum-card]", t);
        if (cards.length < 2) return;
        var n = Math.min(RING_COPIES, cards.length);
        cards.forEach(function (card, index) { card.dataset.driverDrumIndex = String(index); });
        function copyOf(card) {
            var clone = card.cloneNode(true);
            clone.dataset.driverDrumCloneOf = card.dataset.driverDrumIndex;
            clone.setAttribute("aria-hidden", "true");
            clone.tabIndex = -1;
            return clone;
        }
        var head = doc.createDocumentFragment();
        cards.slice(cards.length - n).forEach(function (card) { head.appendChild(copyOf(card)); });
        t.insertBefore(head, cards[0]);
        var tail = doc.createDocumentFragment();
        cards.slice(0, n).forEach(function (card) { tail.appendChild(copyOf(card)); });
        t.appendChild(tail);
        t.dataset.driverDrumRing = "1";
    }

    function realCardFor(card) {
        var t = track();
        if (!card || !t) return card;
        var idx = card.dataset.driverDrumCloneOf;
        if (idx === undefined) return card;
        return q('[data-driver-drum-card][data-driver-drum-index="' + idx + '"]:not([data-driver-drum-clone-of])', t) || card;
    }

    // Когда прокрутка остановилась на копии — мгновенно переносимся на оригинал.
    // Позиция та же с точностью до пикселя, поэтому глазу перескок не виден.
    function normalizeRing() {
        var t = track();
        if (!t || drag) return;
        var card = centerCard();
        if (!card || card.dataset.driverDrumCloneOf === undefined) return;
        var real = realCardFor(card);
        if (real === card) return;
        var prev = t.style.scrollBehavior;
        t.style.scrollBehavior = "auto";
        t.scrollLeft += real.offsetLeft - card.offsetLeft;
        t.style.scrollBehavior = prev;
        markCenter();
        drawLink();
    }

    // --- прокрутка: подсветка центральной карточки и замыкание кольца ---
    var raf = 0, settle = 0;
    doc.addEventListener("scroll", function (event) {
        if (!event.target || !event.target.hasAttribute || !event.target.hasAttribute("data-driver-drum-track")) return;
        if (!raf) raf = root.requestAnimationFrame(function () { raf = 0; markCenter(); });
        root.clearTimeout(settle);
        settle = root.setTimeout(normalizeRing, 140);
    }, true);
    doc.addEventListener("scrollend", function (event) {
        if (!event.target || !event.target.hasAttribute || !event.target.hasAttribute("data-driver-drum-track")) return;
        root.clearTimeout(settle);
        normalizeRing();
    }, true);

    // --- состояние активного простоя: та же карточка, что и на вкладке «Простои» ---
    var observer = new MutationObserver(function (mutations) {
        var relevant = mutations.some(function (m) {
            // Собственные изменения барабана (классы, текст итогов) не считаем.
            if (m.target && m.target.closest && m.target.closest("[data-driver-downtime-drum]")) return false;
            return m.type === "childList" || (m.target && m.target.hasAttribute && (
                m.target.hasAttribute("data-driver-active-downtime-id")
                || m.target.hasAttribute("data-driver-reason-duration")
            ));
        });
        if (!relevant) return;
        buildRing();
        syncTotals();
        syncActive();
    });

    // --- контур: круг циферблата + горлышко к центральной карточке ---
    function drawLink() {
        var svg = q("[data-driver-drum-link]");
        var path = svg ? q("[data-driver-drum-link-path]", svg) : null;
        var w = dial();
        var card = centerCard();
        var screen = svg ? svg.parentElement : null;
        while (screen && !screen.classList.contains("driver-work-screen")) screen = screen.parentElement;
        if (!svg || !path || !w || !card || !screen) return;
        var box = screen.getBoundingClientRect();
        var d = w.getBoundingClientRect();
        var c = card.getBoundingClientRect();
        var cx = d.left + d.width / 2 - box.left, cy = d.top + d.height / 2 - box.top;
        // Ровно в зазор между кольцом (48.5% стороны) и дугой угловых кнопок (51%).
        var r = d.width * .4975;
        var n = Math.min(c.width * .3, r * .7);  // полуширина горлышка
        var yN = cy + Math.sqrt(Math.max(0, r * r - n * n));
        var pad = 7, rr = 26, f = 12;
        var x0 = c.left - box.left - pad, x1 = c.right - box.left + pad;
        var y0 = c.top - box.top - pad, y1 = c.bottom - box.top + pad;
        function p(v) { return Number(v).toFixed(1); }
        var dd = [
            "M", p(cx - n), p(yN),
            "A", p(r), p(r), "0 1 1", p(cx + n), p(yN),
            "L", p(cx + n), p(y0 - f),
            "Q", p(cx + n), p(y0), p(cx + n + f), p(y0),
            "L", p(x1 - rr), p(y0),
            "Q", p(x1), p(y0), p(x1), p(y0 + rr),
            "L", p(x1), p(y1 - rr),
            "Q", p(x1), p(y1), p(x1 - rr), p(y1),
            "L", p(x0 + rr), p(y1),
            "Q", p(x0), p(y1), p(x0), p(y1 - rr),
            "L", p(x0), p(y0 + rr),
            "Q", p(x0), p(y0), p(x0 + rr), p(y0),
            "L", p(cx - n - f), p(y0),
            "Q", p(cx - n), p(y0), p(cx - n), p(y0 - f),
            "Z"
        ].join(" ");
        svg.setAttribute("viewBox", "0 0 " + p(box.width) + " " + p(box.height));
        path.setAttribute("d", dd);
    }

    function init() {
        if (!drum()) return;
        buildRing();
        syncTotals();
        if (activeReasonId() === "") {
            var first = q("[data-driver-drum-card]:not([data-driver-drum-clone-of])");
            if (first) scrollToCard(first, false);
        }
        syncActive();
        observer.observe(doc.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-driver-active-reason-id", "data-driver-active-downtime-id", "hidden"] });
        root.addEventListener("resize", function () { markCenter(); drawLink(); });
        if (root.ResizeObserver) {
            var w = dial();
            if (w) new root.ResizeObserver(function () { drawLink(); }).observe(w);
        }
        drawLink();
        root.setTimeout(drawLink, 300);
    }

    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
    else init();
})(window);
