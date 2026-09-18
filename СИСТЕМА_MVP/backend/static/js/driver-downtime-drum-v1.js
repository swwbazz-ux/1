/* Барабан простоев под циферблатом водителя — вертикальный цилиндр.
   Карточки причин расставлены по окружности вокруг вертикальной оси и
   вращаются как одно тело; передняя грань смотрит на водителя. Делегирование
   на document: разметка оболочки может подменяться целиком
   (submitDriverFormInPlace), поэтому элементы ищутся в момент события.
   Запуск простоя — клик по скрытой кнопке причины на вкладке «Простои»: там
   уже живут офлайн-очередь, озвучка, тосты и переключение на вкладку «Работа». */
(function (root) {
    "use strict";

    if (root.__driverDowntimeDrumBound) return;
    root.__driverDowntimeDrumBound = true;

    var LIFT_START = 10;   // px вертикального хода, после которого карточка едет за пальцем
    var LIFT_ARM = 44;     // px, с которых круг подсвечивается «готово»
    var LIFT_TRIGGER = 64; // px, отпускание выше — старт простоя
    var LIFT_MAX = 96;
    var STRIPS = 9;        // полосок в грани карточки
    var SLATS = 72;        // пластин в стенке цилиндра (по 5 градусов)
    var TILT = -20;        // наклон барабана от зрителя, градусов: видны крышка и ободья
    var doc = root.document;

    function q(sel, base) { return (base || doc).querySelector(sel); }
    function all(sel, base) { return Array.prototype.slice.call((base || doc).querySelectorAll(sel)); }
    function drum() { return q("[data-driver-downtime-drum]"); }
    function cylinder() { return q("[data-driver-drum-track]"); }
    function cards() { var c = cylinder(); return c ? all("[data-driver-drum-card]", c) : []; }
    function dial() { return q(".driver-work-dial"); }
    function stateCard() { return q("[data-driver-active-downtime-id]"); }
    function activeReasonId() {
        var card = stateCard();
        return card ? String(card.dataset.driverActiveReasonId || "") : "";
    }

    // --- геометрия цилиндра ---
    var geo = { n: 0, step: 0, radius: 0, cardW: 0, theta: 0, front: -1, built: null };

    function mod(a, b) { return ((a % b) + b) % b; }

    function build() {
        var c = cylinder();
        var list = cards();
        if (!c || !list.length) return false;
        if (geo.built === c && geo.n === list.length) return true;
        var cardW = list[0].offsetWidth || list[0].getBoundingClientRect().width || 150;
        var n = list.length;
        var step = 360 / Math.max(n, 1);
        // Радиус такой, чтобы соседние грани почти касались: полширины / tg(полшага).
        var radius = n > 1 ? (cardW / 2) / Math.tan((step / 2) * Math.PI / 180) * 1.04 : 0;
        geo.n = n; geo.step = step; geo.radius = radius; geo.cardW = cardW; geo.built = c;
        var d = drum();
        if (d) {
            d.style.setProperty("--drum-radius", radius.toFixed(1) + "px");
            d.style.setProperty("--drum-step", step.toFixed(3) + "deg");
            // Стенка: узкие пластины по окружности того же радиуса и крышки сверху/снизу.
            var wallH = (list[0].offsetHeight || 130) + 26;
            var slatW = radius > 0 ? 2 * radius * Math.tan(Math.PI / SLATS) + 1.2 : cardW;
            d.style.setProperty("--wall-h", wallH.toFixed(1) + "px");
            d.style.setProperty("--slat-w", slatW.toFixed(2) + "px");
            var wall = q("[data-driver-drum-wall]", c);
            if (wall && wall.childElementCount !== SLATS + 2) {
                wall.innerHTML = "";
                for (var s = 0; s < SLATS; s++) {
                    var slat = doc.createElement("i");
                    slat.style.setProperty("--a", (s * 360 / SLATS).toFixed(3) + "deg");
                    wall.appendChild(slat);
                }
                var capTop = doc.createElement("b"); capTop.className = "driver-drum-cap is-top";
                var capBottom = doc.createElement("b"); capBottom.className = "driver-drum-cap is-bottom";
                wall.appendChild(capTop); wall.appendChild(capBottom);
            }
        }
        // Полоски грани лежат точно на окружности стенки (тот же радиус), чуть выше её поверхности.
        list.forEach(function (card) {
            all(".driver-drum-card-face i", card).forEach(function (strip, i) {
                var arc = cardW * (i - (STRIPS - 1) / 2) / STRIPS;
                var rad = radius > 0 ? arc / radius : 0;
                strip.style.setProperty("--i", String(i));
                strip.style.setProperty("--a", (rad * 180 / Math.PI).toFixed(3) + "deg");
                strip.style.setProperty("--z", (radius * (Math.cos(rad) - 1) + 1.5).toFixed(2) + "px");
            });
        });
        list.forEach(function (card, index) {
            card.dataset.driverDrumIndex = String(index);
            card.style.setProperty("--card-angle", (index * step).toFixed(3) + "deg");
        });
        return true;
    }

    function frontIndex(theta) {
        if (!geo.n) return -1;
        return mod(Math.round(-theta / geo.step), geo.n);
    }

    var lastFront = -1;

    function render(snapping) {
        var c = cylinder();
        if (!c || !geo.n) return;
        c.classList.toggle("is-snapping", !!snapping);
        if (snapping) {
            // Пока идёт анимация фиксации, грань ещё движется — контур дорисуем по её окончании.
            c.__snapUntil = Date.now() + 360;
            root.clearTimeout(c.__snapTimer);
            c.__snapTimer = root.setTimeout(function () { c.__snapUntil = 0; drawLink(); }, 370);
        }
        // Барабан наклонён от зрителя: видны крышка и ободья, кромки граней — дуги.
        c.style.transform = "rotateX(" + TILT + "deg) translateZ(" + (-geo.radius).toFixed(1) + "px) rotateY(" + geo.theta.toFixed(3) + "deg)";
        var front = frontIndex(geo.theta);
        // Освещение стенки: пластина ярче, когда смотрит на зрителя.
        all("[data-driver-drum-wall] i", c).forEach(function (slat, s) {
            var relS = mod(s * 360 / SLATS + geo.theta + 180, 360) - 180;
            var k = Math.cos(relS * Math.PI / 180);
            slat.style.setProperty("--b", (0.42 + 0.58 * Math.max(0, k)).toFixed(3));
        });
        // Освещение полосок грани: по повороту к зрителю, как у стенки под ними.
        cards().forEach(function (card, index) {
            all(".driver-drum-card-face i", card).forEach(function (strip, i) {
                var arc = geo.cardW * (i - (STRIPS - 1) / 2) / STRIPS;
                var relI = mod(index * geo.step + (geo.radius > 0 ? arc / geo.radius * 180 / Math.PI : 0) + geo.theta + 180, 360) - 180;
                var kk = Math.cos(relI * Math.PI / 180);
                strip.style.setProperty("--b", (0.55 + 0.5 * Math.max(0, kk)).toFixed(3));
            });
        });
        cards().forEach(function (card, index) {
            // Угол грани относительно зрителя: 0 — прямо перед ним.
            var rel = mod(index * geo.step + geo.theta + 180, 360) - 180;
            var a = Math.abs(rel);
            card.classList.toggle("is-center", index === front);
            card.classList.toggle("is-back", a > 100);
            // Боковые грани погасшие: уже соседняя заметно темнее передней.
            card.style.setProperty("--drum-fade", Math.max(0.22, 1 - Math.max(0, a - 6) / 48).toFixed(3));
        });
        if (front !== lastFront) {
            if (lastFront !== -1 && root.navigator && typeof root.navigator.vibrate === "function") {
                try { root.navigator.vibrate(9); } catch (e) {} // щелчок фиксации
            }
            lastFront = front;
            geo.front = front;
        }
        // Контур статичен: пересчитываем его только когда барабан стоит на делении,
        // иначе он «дышал» бы вместе с поворачивающейся передней гранью.
        var offDetent = Math.abs(geo.theta / geo.step - Math.round(geo.theta / geo.step));
        if (offDetent < 0.002 && !snapping) drawLink();
    }

    function centerCard() {
        var list = cards();
        var front = frontIndex(geo.theta);
        return front >= 0 ? list[front] : null;
    }

    function nearestTheta(index) {
        // Ближайший по кругу поворот, при котором грань index окажется спереди.
        var target = -index * geo.step;
        var k = Math.round((geo.theta - target) / 360);
        return target + k * 360;
    }

    function rotateTo(index, animate) {
        if (!geo.n) return;
        geo.theta = nearestTheta(mod(index, geo.n));
        render(animate);
    }

    function snap(animate) {
        rotateTo(frontIndex(geo.theta), animate !== false);
    }

    // --- синхронизация с вкладкой «Простои» ---
    function syncActive() {
        var id = activeReasonId();
        var d = drum(), w = dial();
        var activeIndex = -1;
        cards().forEach(function (card, index) {
            var on = id !== "" && String(card.dataset.driverDrumReasonId) === id;
            card.classList.toggle("is-active-downtime", on);
            if (on) activeIndex = index;
        });
        if (d) d.classList.toggle("is-active", id !== "");
        if (w) w.classList.toggle("is-downtime-active", id !== "");
        if (activeIndex >= 0 && !drag) rotateTo(activeIndex, true);
        else render(false);
    }

    function syncTotals() {
        cards().forEach(function (card) {
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

    // --- жесты: горизонталь вращает барабан, вертикаль на передней грани — старт ---
    var drag = null;
    var inertia = 0;

    function stopInertia() {
        if (inertia) { root.cancelAnimationFrame(inertia); inertia = 0; }
    }

    // Подъём грани на круг: двигается сама грань внутри 3D-сцены (окно сцены на это время
    // открыто вверх), поэтому она сохраняет изгиб цилиндра до самого круга.
    var liftMax = LIFT_MAX;

    function liftLimit(card) {
        var w = dial();
        var dr = w ? w.getBoundingClientRect() : null;
        var r = card.getBoundingClientRect();
        return dr ? Math.max(LIFT_MAX, r.top - (dr.top + dr.height * 0.55)) : LIFT_MAX;
    }

    function resetLift(card) {
        card.classList.remove("is-lifting", "is-armed");
        card.classList.add("is-settling");
        card.style.setProperty("--lift-y", "0px");
        card.style.setProperty("--lift-z", "0px");
        card.style.setProperty("--lift-tilt", "0deg");
        root.setTimeout(function () {
            card.classList.remove("is-settling");
            var d = drum();
            if (d) d.classList.remove("is-lifting");
            drawLink();
        }, 260);
        var w = dial();
        if (w) w.classList.remove("is-drum-lifting");
    }

    function endDrag() {
        var state = drag;
        drag = null;
        if (state && state.captured && state.target && typeof state.target.releasePointerCapture === "function") {
            try { state.target.releasePointerCapture(state.pointerId); } catch (e) {}
        }
        return state;
    }

    doc.addEventListener("pointerdown", function (event) {
        var d = event.target && event.target.closest ? event.target.closest("[data-driver-downtime-drum]") : null;
        if (!d || event.button > 0) return;
        stopInertia();
        var card = event.target.closest("[data-driver-drum-card]");
        drag = {
            pointerId: event.pointerId, target: d, x0: event.clientX, y0: event.clientY,
            lastX: event.clientX, lastT: event.timeStamp, vx: 0, dy: 0,
            mode: "", card: card, captured: false, theta0: geo.theta
        };
    }, true);

    doc.addEventListener("pointermove", function (event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        var dx = event.clientX - drag.x0;
        var dy = event.clientY - drag.y0;
        if (!drag.mode) {
            if (Math.abs(dx) < 6 && Math.abs(dy) < LIFT_START) return;
            if (Math.abs(dx) >= Math.abs(dy)) {
                drag.mode = "spin";
            } else if (dy < 0 && drag.card && drag.card.classList.contains("is-center")) {
                drag.mode = "lift";
                liftMax = liftLimit(drag.card);
                drag.card.classList.add("is-lifting");
                var dd = drum();
                if (dd) dd.classList.add("is-lifting");
            } else {
                drag = null;
                return;
            }
            if (typeof drag.target.setPointerCapture === "function") {
                try { drag.target.setPointerCapture(event.pointerId); drag.captured = true; } catch (e) {}
            }
        }
        if (drag.mode === "spin") {
            // Ширина одной грани под пальцем = один шаг барабана.
            geo.theta = drag.theta0 + dx * (geo.step / Math.max(geo.cardW, 1));
            var dt = Math.max(1, event.timeStamp - drag.lastT);
            drag.vx = 0.8 * drag.vx + 0.2 * ((event.clientX - drag.lastX) / dt);
            drag.lastX = event.clientX; drag.lastT = event.timeStamp;
            render(false);
        } else {
            var lift = Math.max(-liftMax, Math.min(0, dy));
            var armed = -dy >= LIFT_ARM;
            // Грань отрывается от стенки: поднимается по экрану и одновременно идёт к зрителю
            // (D), чтобы всегда оставаться перед наклонённой стенкой, а не проваливаться в барабан.
            // Вектор (0, L, D) мира раскладываем на оси барабана, наклонённого на TILT градусов.
            var tilt = Math.abs(TILT) * Math.PI / 180;
            var D = -lift * Math.tan(tilt) + 24 * Math.min(1, -lift / 40);
            var ly = lift * Math.cos(tilt) - D * Math.sin(tilt);
            var lz = lift * Math.sin(tilt) + D * Math.cos(tilt);
            drag.card.style.setProperty("--lift-y", ly.toFixed(1) + "px");
            drag.card.style.setProperty("--lift-z", lz.toFixed(1) + "px");
            // По ходу подъёма грань разворачивается лицом к зрителю (снимает наклон барабана).
            drag.card.style.setProperty("--lift-tilt", (Math.abs(TILT) * Math.min(1, -lift / liftMax)).toFixed(2) + "deg");
            drag.card.classList.toggle("is-armed", armed);
            var w = dial();
            if (w) w.classList.toggle("is-drum-lifting", armed);
            drag.dy = dy;
        }
        event.preventDefault();
    }, { passive: false, capture: true });

    function finishSpin(state) {
        // Инерция: докручиваем по скорости, затем фиксируем на ближайшем делении.
        var v = state.vx * (geo.step / Math.max(geo.cardW, 1)) * 16; // градусов за кадр
        v = Math.max(-geo.step * 0.9, Math.min(geo.step * 0.9, v));
        function tick() {
            v *= 0.88;
            geo.theta += v;
            render(false);
            if (Math.abs(v) > 0.15) { inertia = root.requestAnimationFrame(tick); return; }
            inertia = 0;
            snap(true);
        }
        if (Math.abs(v) > 0.4) inertia = root.requestAnimationFrame(tick);
        else snap(true);
    }

    doc.addEventListener("pointerup", function (event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        var state = endDrag();
        if (state.mode === "spin") {
            finishSpin(state);
        } else if (state.mode === "lift") {
            resetLift(state.card);
            if (-state.dy >= LIFT_TRIGGER) startDowntime(state.card);
        } else if (state.card && !state.card.classList.contains("is-center")) {
            rotateTo(Number(state.card.dataset.driverDrumIndex), true); // тап по боковой грани — подвести её вперёд
        }
    }, true);

    doc.addEventListener("pointercancel", function (event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        var state = endDrag();
        if (state.mode === "lift") resetLift(state.card);
        else snap(true);
    }, true);

    // Колесо мыши на стенде: одно деление за прокрутку.
    doc.addEventListener("wheel", function (event) {
        var d = event.target && event.target.closest ? event.target.closest("[data-driver-downtime-drum]") : null;
        if (!d || !geo.n) return;
        var delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
        if (!delta) return;
        event.preventDefault();
        rotateTo(frontIndex(geo.theta) + (delta > 0 ? 1 : -1), true);
    }, { passive: false, capture: true });

    // --- контур: круг циферблата + горлышко к передней грани ---
    function c_snapping() {
        var c = cylinder();
        return !!(c && c.classList.contains("is-snapping") && c.__snapUntil && c.__snapUntil > Date.now());
    }

    function drawLink() {
        var svg = q("[data-driver-drum-link]");
        var path = svg ? q("[data-driver-drum-link-path]", svg) : null;
        var w = dial();
        var card = centerCard();
        var screen = svg ? svg.parentElement : null;
        while (screen && !screen.classList.contains("driver-work-screen")) screen = screen.parentElement;
        if (!svg || !path || !w || !card || !screen) return;
        if (c_snapping()) return;
        // Пока грань поднимают на круг, её прямоугольник не годится для контура — оставляем прежний.
        if (card.classList.contains("is-lifting") || (drag && drag.mode === "lift")) return;
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
        if (build()) { syncTotals(); syncActive(); }
    });

    function init() {
        if (!drum()) return;
        if (!build()) return;
        syncTotals();
        syncActive();
        observer.observe(doc.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-driver-active-reason-id", "data-driver-active-downtime-id", "hidden"] });
        root.addEventListener("resize", function () { geo.built = null; build(); render(false); });
        if (root.ResizeObserver) {
            var w = dial();
            if (w) new root.ResizeObserver(function () { drawLink(); }).observe(w);
        }
        root.setTimeout(function () { render(false); }, 300);
    }

    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
    else init();
})(window);
