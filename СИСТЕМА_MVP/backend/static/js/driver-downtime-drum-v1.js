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
    var DROP_ARM = 36;     // px вниз, с которых грань «готова» выключить простой
    var DROP_TRIGGER = 56; // px вниз, отпускание ниже — завершение простоя
    var DROP_MAX = 64;
    var MIN_FACES = 12;    // барабан всегда полноразмерный, как минимум на 12 граней
    var TILT = 0;          // барабан смотрит строго в лоб: при наклоне боковые грани поднимаются и заходят на угловые кнопки
    var doc = root.document;

    function q(sel, base) { return (base || doc).querySelector(sel); }
    function all(sel, base) { return Array.prototype.slice.call((base || doc).querySelectorAll(sel)); }
    function drum() { return q("[data-driver-downtime-drum]"); }
    function cylinder() { return q("[data-driver-drum-track]"); }
    // Грани барабана: выбранные причины и их копии по кругу (см. build). Оригиналы —
    // по одному на причину, из них считается набор быстрого доступа.
    function cards() { var c = cylinder(); return c ? all("[data-driver-drum-card]:not([hidden])", c) : []; }
    function allCards() { var c = cylinder(); return c ? all("[data-driver-drum-card]:not([data-driver-drum-clone])", c) : []; }
    function dial() { return q(".driver-work-dial"); }
    function stateCard() { return q("[data-driver-active-downtime-id]"); }
    function activeReasonId() {
        var card = stateCard();
        return card ? String(card.dataset.driverActiveReasonId || "") : "";
    }

    // --- геометрия цилиндра ---
    var geo = { n: 0, step: 0, radius: 0, cardW: 0, theta: 0, front: -1, built: null, full: true, reasons: 0, signature: "" };

    function mod(a, b) { return ((a % b) + b) % b; }

    // Подпись состава барабана: пока она не меняется, пересобирать нечего. Экран водителя
    // периодически подменяется свежей копией с сервера — на слабом телефоне полная пересборка
    // (клонирование граней и замеры размеров) давала заметное замирание.
    function drumSignature(c) {
        var ids = [];
        all("[data-driver-drum-card]:not([data-driver-drum-clone]):not([hidden])", c).forEach(function (card) {
            ids.push(card.dataset.driverDrumReasonId);
        });
        return ids.join(",");
    }

    function reapply(c) {
        // Тот же состав в новой копии экрана: восстанавливаем углы и поворот без замеров.
        var list = cards();
        if (list.length !== geo.n) return false;
        list.forEach(function (card, index) {
            card.dataset.driverDrumIndex = String(index);
            card.style.setProperty("--card-angle", (index * geo.step).toFixed(3) + "deg");
        });
        var d = drum();
        if (d) {
            d.style.setProperty("--drum-radius", geo.radius.toFixed(1) + "px");
            d.style.setProperty("--drum-step", geo.step.toFixed(3) + "deg");
        }
        geo.built = c;
        lastFront = -1;
        render(false);
        return true;
    }

    function build() {
        var c = cylinder();
        if (!c) return false;
        // Копии от прошлой сборки убираем и собираем кольцо заново из выбранных причин.
        var selected = all("[data-driver-drum-card]:not([data-driver-drum-clone]):not([hidden])", c);
        if (geo.built === c && geo.reasons === selected.length && geo.n === cards().length && cards().length) return true;
        var signature = drumSignature(c);
        if (geo.n && geo.signature === signature && !all("[data-driver-drum-card][data-driver-drum-clone]", c).length) {
            // Новая копия экрана с тем же составом: клонируем грани заново, но без замеров.
            var need = geo.n - selected.length;
            for (var k = 0; k < need; k++) {
                var copy = selected[k % selected.length].cloneNode(true);
                copy.setAttribute("data-driver-drum-clone", "1");
                copy.setAttribute("aria-hidden", "true");
                copy.tabIndex = -1;
                copy.hidden = false;
                c.appendChild(copy);
            }
            if (reapply(c)) return true;
        }
        all("[data-driver-drum-card][data-driver-drum-clone]", c).forEach(function (clone) { clone.parentNode.removeChild(clone); });
        if (!selected.length) return false;
        // Кольцо всегда полное: если причин меньше MIN_FACES, они повторяются по кругу —
        // граней столько, чтобы набор уложился целое число раз (нет пустых мест и «швов»).
        var reasons = selected.length;
        var faces = reasons;
        while (faces < MIN_FACES) faces += reasons;
        for (var f = reasons; f < faces; f++) {
            var src = selected[f % reasons];
            var clone = src.cloneNode(true);
            clone.setAttribute("data-driver-drum-clone", "1");
            clone.setAttribute("aria-hidden", "true");
            clone.tabIndex = -1;
            clone.hidden = false;
            c.appendChild(clone);
        }
        var list = cards();
        var cardW = list[0].offsetWidth || list[0].getBoundingClientRect().width || 150;
        var n = list.length;
        var step = 360 / n;
        // Радиус чуть больше «касания» соседних граней: между карточками остаётся просвет,
        // сквозь который проходит обводка контура (множитель 1.22 ≈ зазор в 18% ширины грани).
        var radius = (cardW / 2) / Math.tan((step / 2) * Math.PI / 180) * 1.22;
        geo.n = n; geo.step = step; geo.radius = radius; geo.cardW = cardW; geo.built = c;
        geo.reasons = reasons;
        geo.signature = drumSignature(c);
        geo.full = true;   // полное кольцо — крутится бесконечно в обе стороны
        var d = drum();
        if (d) {
            d.style.setProperty("--drum-radius", radius.toFixed(1) + "px");
            d.style.setProperty("--drum-step", step.toFixed(3) + "deg");
        }
        // Выпуклость грани рисует градиент в CSS — считать при сборке нечего.
        list.forEach(function (card, index) {
            card.dataset.driverDrumIndex = String(index);
            card.style.setProperty("--card-angle", (index * step).toFixed(3) + "deg");
        });
        return true;
    }

    function frontIndex(theta) {
        if (!geo.n) return -1;
        var index = Math.round(-theta / geo.step);
        if (geo.full) return mod(index, geo.n);
        return Math.max(0, Math.min(geo.n - 1, index));
    }

    // Неполное кольцо: поворот ограничен первой и последней гранью (с небольшим упором).
    function clampTheta(theta, slack) {
        if (geo.full) return theta;
        var lo = -(geo.n - 1) * geo.step - (slack || 0), hi = (slack || 0);
        return Math.max(lo, Math.min(hi, theta));
    }

    var lastFront = -1;

    // Звуковой щелчок фиксации: короткий «тик» синтезируется на месте, без файлов.
    // Аудио разрешается браузером только после касания — контекст создаём на первом pointerdown.
    var audio = null;
    function unlockAudio() {
        if (audio || !(root.AudioContext || root.webkitAudioContext)) return;
        try { audio = new (root.AudioContext || root.webkitAudioContext)(); } catch (e) { audio = null; }
    }
    function click(strength) {
        if (!audio) return;
        try {
            if (audio.state === "suspended") audio.resume();
            var t0 = audio.currentTime;
            var osc = audio.createOscillator();
            var gain = audio.createGain();
            osc.type = "square";
            osc.frequency.setValueAtTime(1900, t0);
            osc.frequency.exponentialRampToValueAtTime(700, t0 + 0.02);
            gain.gain.setValueAtTime(0.0001, t0);
            gain.gain.exponentialRampToValueAtTime(0.09 * (strength || 1), t0 + 0.002);
            gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.028);
            osc.connect(gain); gain.connect(audio.destination);
            osc.start(t0); osc.stop(t0 + 0.03);
        } catch (e) {}
    }
    function haptic(pattern) {
        if (root.navigator && typeof root.navigator.vibrate === "function") {
            try { root.navigator.vibrate(pattern); } catch (e) {}
        }
    }

    function render(snapping) {
        var c = cylinder();
        if (!c || !geo.n) return;
        c.classList.toggle("is-snapping", !!snapping);
        if (snapping) {
            // Пока идёт анимация фиксации, грань ещё движется — контур дорисуем по её окончании.
            c.__snapUntil = Date.now() + 360;
            root.clearTimeout(c.__snapTimer);
            c.__snapTimer = root.setTimeout(function () { c.__snapUntil = 0; syncLinkVars(); }, 370);
        }
        // Ось барабана строго вертикальна (TILT = 0): грани остаются на одной высоте,
        // боковые не уезжают вверх и не наползают на угловые кнопки круга.
        c.style.transform = "rotateX(" + TILT + "deg) translateZ(" + (-geo.radius).toFixed(1) + "px) rotateY(" + geo.theta.toFixed(3) + "deg)";
        var front = frontIndex(geo.theta);

        cards().forEach(function (card, index) {
            // Угол грани относительно зрителя: 0 — прямо перед ним.
            var rel = mod(index * geo.step + geo.theta + 180, 360) - 180;
            var a = Math.abs(rel);
            var isCenter = index === front;
            if (card.classList.contains("is-center") !== isCenter) card.classList.toggle("is-center", isCenter);
            var isBack = a > 100;
            if (card.classList.contains("is-back") !== isBack) card.classList.toggle("is-back", isBack);
            // Боковые грани погасшие: одно значение на карточку, и только если оно изменилось
            // (запись в стиль дороже самой анимации — на слабых телефонах это заметно).
            var fade = Math.max(0.22, 1 - Math.max(0, a - 6) / 48).toFixed(2);
            if (card.__fade !== fade) { card.__fade = fade; card.style.setProperty("--drum-fade", fade); }
        });
        if (front !== lastFront) {
            // Щелчок фиксации: вибро + звук. 9 мс мотор телефона почти не отрабатывает,
            // короткий и отчётливый отклик начинается примерно с 18 мс.
            if (lastFront !== -1) { haptic(18); click(1); }
            lastFront = front;
            geo.front = front;
        }
    }

    function centerCard() {
        var list = cards();
        var front = frontIndex(geo.theta);
        return front >= 0 ? list[front] : null;
    }

    function nearestTheta(index) {
        // Ближайший по кругу поворот, при котором грань index окажется спереди.
        var target = -index * geo.step;
        if (!geo.full) return target;
        var k = Math.round((geo.theta - target) / 360);
        return target + k * 360;
    }

    function rotateTo(index, animate) {
        if (!geo.n) return;
        var target = geo.full ? mod(index, geo.n) : Math.max(0, Math.min(geo.n - 1, index));
        geo.theta = nearestTheta(target);
        render(animate);
    }

    function snap(animate) {
        rotateTo(frontIndex(geo.theta), animate !== false);
    }

    // --- синхронизация с вкладкой «Простои» ---
    var rebuilding = false;

    function rebuildDrum(keepReasonId) {
        // Пересобрать цилиндр после смены набора граней; переднюю грань по возможности сохранить.
        if (rebuilding) return;
        rebuilding = true;
        try {
            geo.built = null; slot = null; lastFront = -1;
            if (!build()) return;
            var index = 0, found = false;
            cards().forEach(function (card, i) { if (!found && keepReasonId && card.dataset.driverDrumReasonId === keepReasonId) { index = i; found = true; } });
            geo.theta = -index * geo.step;
            render(false);
        } finally { rebuilding = false; }
    }

    function syncActive() {
        var id = activeReasonId();
        var d = drum(), w = dial();
        var activeIndex = -1, activeDist = Infinity;
        // Простой запущен причиной, которой нет в барабане, — она встаёт в барабан на время простоя.
        var changed = false;
        allCards().forEach(function (card) {
            var isActive = id !== "" && String(card.dataset.driverDrumReasonId) === id;
            if (isActive && card.hidden) { card.dataset.driverDrumTemp = "1"; card.hidden = false; changed = true; }
            if (!isActive && card.dataset.driverDrumTemp === "1") { delete card.dataset.driverDrumTemp; card.hidden = card.dataset.driverDrumQuick !== "1"; changed = true; }
        });
        if (changed) rebuildDrum(id !== "" ? id : null);
        cards().forEach(function (card, index) {
            var on = id !== "" && String(card.dataset.driverDrumReasonId) === id;
            card.classList.toggle("is-active-downtime", on);
            if (on && geo.n) {
                // Причина может стоять на нескольких гранях (копии) — подводим ближайшую.
                var dist = Math.abs(nearestTheta(index) - geo.theta);
                if (dist < activeDist) { activeDist = dist; activeIndex = index; }
            }
        });
        if (d) {
            d.classList.toggle("is-active", id !== "");
            d.classList.toggle("is-locked", id !== "");   // барабан зафиксирован, пока идёт простой
        }
        // Подводим активную причину вперёд только если она не спереди: иначе каждое
        // обновление таймера запускало бы анимацию фиксации и блокировало контур.
        if (activeIndex >= 0 && !drag && frontIndex(geo.theta) !== activeIndex) rotateTo(activeIndex, true);
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
        haptic([16, 40, 24]); click(1.6);
        button.click();
    }

    function stopDowntime() {
        var button = q("[data-driver-close-downtime]");
        if (!button || button.disabled) {
            if (typeof root.showDriverToast === "function") root.showDriverToast("Активного простоя нет");
            return;
        }
        haptic([24, 30, 12]); click(1.3);
        button.click();
    }

    // --- жесты: горизонталь вращает барабан, вертикаль на передней грани — старт (вверх) / стоп (вниз) ---
    var drag = null;
    var inertia = 0;

    function stopInertia() {
        if (inertia) { root.cancelAnimationFrame(inertia); inertia = 0; }
    }

    // Подъём грани на круг: двигается сама грань внутри 3D-сцены (окно сцены на это время
    // открыто вверх), поэтому она сохраняет изгиб цилиндра до самого круга.
    var liftMax = LIFT_MAX;
    var liftRaf = 0;

    var ghost = null;
    var ghostRaf = 0, ghostY = 0;

    // Позиция копии пишется напрямую в transform и не чаще одного раза за кадр:
    // запись CSS-переменной стоит втрое дороже (16,5 против 5 микросекунд на телефоне)
    // и пересчитывает стили всего поддерева копии, а событий движения приходит больше,
    // чем кадров успевает отрисоваться.
    function moveGhost(y) {
        ghostY = y;
        if (ghostRaf || !ghost) return;
        ghostRaf = root.requestAnimationFrame(function () {
            ghostRaf = 0;
            if (ghost) ghost.style.transform = "translateY(" + ghostY.toFixed(1) + "px)";
        });
    }

    function screenOf(el) {
        while (el && !(el.classList && el.classList.contains("driver-work-screen"))) el = el.parentElement;
        return el;
    }

    // Копия передней грани для жеста: сцена барабана всегда обрезана по своей рамке (иначе
    // боковые грани наползают на угловые кнопки круга), а переключать обрезку в момент жеста
    // нельзя — на телефоне это стоит ~1,4 с. Поэтому вверх/вниз летит копия поверх экрана.
    function makeGhost(card, isDrop) {
        var screen = screenOf(card);
        if (!screen) return null;
        var box = screen.getBoundingClientRect();
        var r = card.getBoundingClientRect();
        var g = card.cloneNode(true);
        g.classList.add("driver-drum-ghost");
        g.classList.remove("is-lifting", "is-armed", "is-dropping", "is-drop-armed");
        if (isDrop) g.classList.add("is-drop");
        g.removeAttribute("data-driver-drum-card");
        g.setAttribute("aria-hidden", "true");
        g.tabIndex = -1;
        g.hidden = false;
        g.style.left = (r.left - box.left) + "px";
        g.style.top = (r.top - box.top) + "px";
        g.style.width = r.width + "px";
        g.style.height = r.height + "px";
        g.style.transform = "translateY(0px)";
        screen.appendChild(g);
        var w = dial();
        var dr = w ? w.getBoundingClientRect() : null;
        g.__liftMax = dr ? Math.max(LIFT_MAX, r.top - (dr.top + dr.height * 0.55)) : LIFT_MAX;
        return g;
    }

    function resetLift(card) {
        card.classList.remove("is-lifting", "is-armed", "is-dropping", "is-drop-armed");
        var link0 = q("[data-driver-drum-link]");
        if (link0) link0.classList.remove("is-armed", "is-drop-armed");
        if (ghost) {
            var g = ghost; ghost = null;
            if (ghostRaf) { root.cancelAnimationFrame(ghostRaf); ghostRaf = 0; }
            g.classList.add("is-settling");
            g.style.transform = "translateY(0px)";
            root.setTimeout(function () { if (g.parentNode) g.parentNode.removeChild(g); }, 240);
        }
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
        unlockAudio();
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
                // При активном простое барабан зафиксирован: крутить нельзя.
                if (activeReasonId() !== "") { drag = null; return; }
                drag.mode = "spin";
            } else if (dy < 0 && drag.card && drag.card.classList.contains("is-center")) {
                drag.mode = "lift";
                ghost = makeGhost(drag.card, false);
                liftMax = (ghost && ghost.__liftMax) || LIFT_MAX;
                drag.card.classList.add("is-lifting");
            } else if (dy > 0 && drag.card && drag.card.classList.contains("is-center") && drag.card.classList.contains("is-active-downtime")) {
                // Активную причину тянут вниз, «в барабан» — принудительное выключение простоя.
                drag.mode = "drop";
                ghost = makeGhost(drag.card, true);
                drag.card.classList.add("is-dropping");
            } else {
                drag = null;
                return;
            }
            if (typeof drag.target.setPointerCapture === "function") {
                try { drag.target.setPointerCapture(event.pointerId); drag.captured = true; } catch (e) {}
            }
        }
        if (drag.mode === "drop") {
            var down = Math.min(DROP_MAX, Math.max(0, dy));
            moveGhost(down);
            var dropArmed = dy >= DROP_ARM;
            if (dropArmed !== drag.armed) {
                drag.armed = dropArmed;
                if (dropArmed) { haptic(12); click(0.7); }
                if (ghost) ghost.classList.toggle("is-armed", dropArmed);
            }
            var linkD = q("[data-driver-drum-link]");
            if (linkD) linkD.classList.toggle("is-drop-armed", dropArmed);
            drag.dy = dy;
            event.preventDefault();
            return;
        }
        if (drag.mode === "spin") {
            // Ширина одной грани под пальцем = один шаг барабана.
            geo.theta = clampTheta(drag.theta0 + dx * (geo.step / Math.max(geo.cardW, 1)), geo.step * 0.35);
            var dt = Math.max(1, event.timeStamp - drag.lastT);
            drag.vx = 0.8 * drag.vx + 0.2 * ((event.clientX - drag.lastX) / dt);
            drag.lastX = event.clientX; drag.lastT = event.timeStamp;
            render(false);
        } else {
            var lift = Math.max(-liftMax, Math.min(0, dy));
            var armed = -dy >= LIFT_ARM;
            if (armed && !drag.armed) { haptic(12); click(0.7); }
            // Состояние «готово» показывает ТОЛЬКО копия. Ни обводку контура, ни оригинал грани
            // трогать нельзя: замер на телефоне — 1651 мс на перекраску контура и 594 мс на
            // смену цвета грани внутри 3D-сцены против 17 мс у копии.
            moveGhost(lift);
            if (armed !== drag.armed) {
                drag.armed = armed;
                if (ghost) ghost.classList.toggle("is-armed", armed);
            }
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
            geo.theta = clampTheta(geo.theta + v, geo.step * 0.2);
            if (!geo.full && (geo.theta >= 0 || geo.theta <= -(geo.n - 1) * geo.step)) v *= 0.5; // упор
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
        } else if (state.mode === "drop") {
            resetLift(state.card);
            if (state.dy >= DROP_TRIGGER) stopDowntime();
        } else if (state.card && !state.card.classList.contains("is-center") && activeReasonId() === "") {
            rotateTo(Number(state.card.dataset.driverDrumIndex), true); // тап по боковой грани — подвести её вперёд
        }
    }, true);

    doc.addEventListener("pointercancel", function (event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        var state = endDrag();
        if (state.mode === "lift" || state.mode === "drop") resetLift(state.card);
        else snap(true);
    }, true);

    // Страховка: если указатель потерян (сворачивание, звонок), подъём и вращение сбрасываются.
    function abortGesture() {
        if (!drag) return;
        var state = endDrag();
        if (state.mode === "lift" || state.mode === "drop") resetLift(state.card);
        else snap(true);
    }
    root.addEventListener("blur", abortGesture);
    doc.addEventListener("visibilitychange", function () { if (doc.hidden) abortGesture(); });

    // Колесо мыши на стенде: одно деление за прокрутку.
    doc.addEventListener("wheel", function (event) {
        var d = event.target && event.target.closest ? event.target.closest("[data-driver-downtime-drum]") : null;
        if (!d || !geo.n || activeReasonId() !== "") return;
        var delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
        if (!delta) return;
        event.preventDefault();
        rotateTo(frontIndex(geo.theta) + (delta > 0 ? 1 : -1), true);
    }, { passive: false, capture: true });

    // --- быстрый доступ: какие причины крутятся в барабане ---
    function quickMin() { var d = drum(); return Math.max(1, Number(d && d.dataset.driverQuickMin) || 3); }
    function quickStorageKey() { var shell = q("[data-driver-shell]"); return "driver-quick-reasons:" + (shell ? shell.dataset.driverAccessId : "x"); }
    function readLocalQuick() { try { var raw = root.localStorage.getItem(quickStorageKey()); return raw ? JSON.parse(raw) : null; } catch (e) { return null; } }
    function writeLocalQuick(ids, updatedAt) { try { root.localStorage.setItem(quickStorageKey(), JSON.stringify({ ids: ids, updated_at: updatedAt })); } catch (e) {} }
    function reasonOrder() { return allCards().map(function (c) { return String(c.dataset.driverDrumReasonId); }); }
    function currentQuickIds() { return allCards().filter(function (c) { return c.dataset.driverDrumQuick === "1"; }).map(function (c) { return String(c.dataset.driverDrumReasonId); }); }

    function toast(message) {
        if (typeof root.showDriverToast === "function") { root.showDriverToast(message); return; }
        var el = q("[data-driver-toast]");
        if (!el) return;
        el.textContent = message; el.hidden = false;
        root.clearTimeout(el.__drumToast);
        el.__drumToast = root.setTimeout(function () { el.hidden = true; }, 2600);
    }

    function updateCount() {
        var el = q("[data-driver-drum-count]");
        if (!el) return;
        var total = allCards().length, on = currentQuickIds().length;
        el.textContent = total ? ("В барабане " + on + " из " + total) : "";
    }

    function applyQuick(ids, rebuild) {
        var min = quickMin();
        var useAll = !ids || ids.length < min;
        var frontId = (function () { var c = centerCard(); return c ? String(c.dataset.driverDrumReasonId) : null; })();
        allCards().forEach(function (card) {
            var rid = String(card.dataset.driverDrumReasonId);
            var on = useAll || ids.indexOf(rid) !== -1;
            card.dataset.driverDrumQuick = on ? "1" : "0";
            card.hidden = !on && card.dataset.driverDrumTemp !== "1";
        });
        all("[data-driver-reason-star]").forEach(function (star) {
            var on = useAll || ids.indexOf(String(star.dataset.driverReasonStarId)) !== -1;
            star.classList.toggle("is-on", on);
            star.setAttribute("aria-pressed", on ? "true" : "false");
        });
        updateCount();
        if (rebuild) rebuildDrum(frontId);
    }

    function saveQuick(ids, stamp) {
        var d = drum();
        var url = d && d.dataset.driverQuickUrl;
        if (!url || typeof root.fetch !== "function") return;
        var csrf = q('meta[name="csrf-token"]');
        root.fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf ? csrf.content : "" },
            body: JSON.stringify({ reason_ids: ids.map(Number), updated_at: stamp })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (data && data.ok) {
                writeLocalQuick((data.reason_ids || []).map(String), data.updated_at || stamp);
            } else if (data && data.error) {
                toast(data.error);
            }
        }).catch(function () { /* без сети: набор сохранён в телефоне, отправим при следующей загрузке */ });
    }

    doc.addEventListener("click", function (event) {
        var star = event.target && event.target.closest ? event.target.closest("[data-driver-reason-star]") : null;
        if (!star) return;
        event.preventDefault();
        event.stopPropagation();
        var min = quickMin();
        var ids = currentQuickIds();
        var id = String(star.dataset.driverReasonStarId);
        var at = ids.indexOf(id);
        if (at === -1) {
            ids.push(id);
        } else {
            if (ids.length <= min) { toast("В барабане должно быть не меньше " + min + " причин"); haptic([20, 40, 20]); return; }
            ids.splice(at, 1);
        }
        var order = reasonOrder();
        ids.sort(function (a, b) { return order.indexOf(a) - order.indexOf(b); });
        var stamp = new Date().toISOString();
        haptic(10); click(0.8);
        applyQuick(ids, true);
        writeLocalQuick(ids, stamp);
        saveQuick(ids, stamp);
    }, true);

    function reconcileQuick() {
        var d = drum();
        if (!d) return;
        var serverStamp = String(d.dataset.driverQuickUpdatedAt || "");
        var local = readLocalQuick();
        if (local && Array.isArray(local.ids) && local.updated_at && (!serverStamp || local.updated_at > serverStamp)) {
            // В телефоне набор новее (меняли без сети) — применяем его и досылаем на сервер.
            applyQuick(local.ids.map(String), true);
            saveQuick(local.ids.map(String), local.updated_at);
        } else {
            writeLocalQuick(currentQuickIds(), serverStamp);
        }
        updateCount();
    }

    // --- контур: круг циферблата + горлышко к передней грани ---
    // Контур рисует CSS. Здесь только три размера гнезда, и ставятся они на <html> —
    // элемент, который не подменяется при обновлении экрана, поэтому контур переживает
    // любое обновление и не пересчитывается на кадрах вращения.
    function syncLinkVars() {
        var w = dial();
        var card = centerCard();
        var link = q("[data-driver-drum-link]");
        if (!w || !card || !link) return;
        var screen = link.parentElement;
        while (screen && !screen.classList.contains("driver-work-screen")) screen = screen.parentElement;
        if (!screen) return;
        var dr = w.getBoundingClientRect();
        var cr = card.getBoundingClientRect();
        var box = screen.getBoundingClientRect();
        if (!dr.width || !cr.width) return;
        var pad = 5, rr = 10;
        var half = cr.width / 2 + pad;
        // Кольцо — в зазоре между кольцом циферблата (48.5% стороны) и угловыми кнопками (51%).
        var r = dr.width * 0.4975;
        if (half >= r) return;
        var cx = dr.left + dr.width / 2 - box.left, cy = dr.top + dr.height / 2 - box.top;
        var xL = cx - half, xR = cx + half;
        var yN = cy + Math.sqrt(r * r - half * half);   // линии начинаются точно на кольце
        var y1 = cr.bottom - box.top + pad;
        function p(v) { return Number(v).toFixed(1); }
        // Одна кривая: от левой точки кольца по большой дуге вправо, вниз по правой линии,
        // по нижней рамке с двумя скруглениями, вверх по левой линии — и замыкание на кольцо.
        var keyhole = [
            "M", p(xL), p(yN),
            "A", p(r), p(r), "0 1 1", p(xR), p(yN),
            "L", p(xR), p(y1 - rr),
            "Q", p(xR), p(y1), p(xR - rr), p(y1),
            "L", p(xL + rr), p(y1),
            "Q", p(xL), p(y1), p(xL), p(y1 - rr),
            "Z"
        ].join(" ");
        var root_ = doc.documentElement.style;
        root_.setProperty("--link-path", 'path("' + keyhole + '")');
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
        reconcileQuick();
        syncActive();
        observer.observe(doc.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-driver-active-reason-id", "data-driver-active-downtime-id", "hidden"] });
        root.addEventListener("resize", function () { geo.built = null; slot = null; build(); render(false); });
        if (root.ResizeObserver) {
            var w = dial();
            if (w) new root.ResizeObserver(function () { syncLinkVars(); }).observe(w);
        }
        root.setTimeout(function () { render(false); }, 300);
    }

    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
    else init();
})(window);
