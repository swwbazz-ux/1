/* Барабан точек разгрузки над циферблатом водителя и ручной режим на самом круге.
   Цилиндр, вращение с инерцией, щелчок фиксации и летящая копия грани повторяют
   барабан простоев (driver-downtime-drum-v1.js); отличие — направление: передняя грань
   тянется вниз, в круг («отправиться на точку»), а назначенная — вверх, из круга
   («отменить»). Жесты работают только в ручном режиме; его включает угловая кнопка
   с рукой. Признак режима — класс на <html>: экран водителя периодически подменяется
   свежей копией с сервера (submitDriverFormInPlace), корень документа — нет. Поэтому,
   как и у барабана простоев, элементы ищутся в момент события. */
(function (root) {
    "use strict";

    if (root.__driverPointDrumBound) return;
    root.__driverPointDrumBound = true;

    var SEND_START = 10;    // px вертикального хода, после которого грань едет за пальцем
    var SEND_ARM = 44;      // px, с которых отпускание уже отправит на точку
    var SEND_TRIGGER = 64;  // px, отпускание ниже — отправка на точку
    var SEND_MAX = 96;
    var RECALL_ARM = 36;    // px вверх, с которых назначенная грань «готова» отмениться
    var RECALL_TRIGGER = 56;
    var RECALL_MAX = 64;
    var MIN_FACES = 12;
    var FACE_GAP = 10;      // тот же просвет под обводку, что у барабана простоев
    var MANUAL_CLASS = "is-driver-dial-manual";
    var doc = root.document;

    function q(sel, base) { return (base || doc).querySelector(sel); }
    function all(sel, base) { return Array.prototype.slice.call((base || doc).querySelectorAll(sel)); }
    function drum() { return q("[data-driver-point-drum]"); }
    function cylinder() { return q("[data-driver-point-drum-track]"); }
    function cards() { var c = cylinder(); return c ? all("[data-driver-point-card]:not([hidden])", c) : []; }
    function dial() { return q(".driver-work-dial"); }
    function shell() { return q("[data-driver-shell]"); }

    // --- ручной режим ---
    function isManual() { return doc.documentElement.classList.contains(MANUAL_CLASS); }
    function storageKey() { var s = shell(); return "driver-dial-manual:" + (s ? s.dataset.driverAccessId : "x"); }
    function readStoredMode() { try { return root.localStorage.getItem(storageKey()) === "1"; } catch (e) { return false; } }
    function storeMode(on) { try { root.localStorage.setItem(storageKey(), on ? "1" : "0"); } catch (e) {} }

    function engine() { return root.DriverManualExcavatorWorkspace || null; }

    // Назначенная точка ручного рейса: пока она в круге, выйти из ручного режима нельзя.
    function assignedPointId() {
        var api = engine();
        if (api && typeof api.activeManualPointId === "function") return String(api.activeManualPointId() || "");
        return "";
    }

    function syncModeControls() {
        var on = isManual();
        all("[data-driver-dial-manual-toggle]").forEach(function (button) {
            if (button.classList.contains("is-on") !== on) button.classList.toggle("is-on", on);
            var pressed = on ? "true" : "false";
            if (button.getAttribute("aria-pressed") !== pressed) button.setAttribute("aria-pressed", pressed);
        });
        var d = drum();
        if (d) {
            var assigned = assignedPointId() !== "";
            if (d.classList.contains("is-assigned") !== assigned) d.classList.toggle("is-assigned", assigned);
        }
    }

    function setManual(on) {
        doc.documentElement.classList.toggle(MANUAL_CLASS, !!on);
        storeMode(!!on);
        syncModeControls();
    }

    function toast(message) {
        if (typeof root.showDriverToast === "function") root.showDriverToast(message);
    }

    doc.addEventListener("click", function (event) {
        var button = event.target && event.target.closest ? event.target.closest("[data-driver-dial-manual-toggle]") : null;
        if (!button || button.disabled) return;
        event.preventDefault();
        if (isManual() && assignedPointId() !== "") {
            toast("Сначала завершите или отмените ручной рейс");
            haptic([45, 60, 45]);
            return;
        }
        var next = !isManual();
        haptic(next ? [30, 40, 60] : [60, 40, 30]);
        click(next ? 1.2 : 0.9);
        setManual(next);
        render(false);
    }, true);

    // --- геометрия цилиндра (как у барабана простоев) ---
    var geo = { n: 0, step: 0, radius: 0, cardW: 0, theta: 0, built: null, signature: "" };

    function mod(a, b) { return ((a % b) + b) % b; }

    function signatureOf(c) {
        return all("[data-driver-point-card]:not([data-driver-point-clone])", c).map(function (card) {
            return card.dataset.driverPointId;
        }).join(",");
    }

    function build() {
        var c = cylinder();
        if (!c) return false;
        var selected = all("[data-driver-point-card]:not([data-driver-point-clone])", c);
        var signature = signatureOf(c);
        if (geo.built === c && geo.signature === signature && cards().length === geo.n && geo.n) return true;
        all("[data-driver-point-card][data-driver-point-clone]", c).forEach(function (clone) { clone.parentNode.removeChild(clone); });
        if (!selected.length) { geo.n = 0; geo.built = c; geo.signature = signature; return false; }
        // Кольцо всегда полное: точки повторяются по кругу целое число раз.
        var points = selected.length;
        var faces = points;
        while (faces < MIN_FACES) faces += points;
        for (var f = points; f < faces; f++) {
            var copy = selected[f % points].cloneNode(true);
            copy.setAttribute("data-driver-point-clone", "1");
            copy.setAttribute("aria-hidden", "true");
            copy.tabIndex = -1;
            c.appendChild(copy);
        }
        var list = cards();
        var cardW = list[0].offsetWidth || list[0].getBoundingClientRect().width || 150;
        var n = list.length;
        var step = 360 / n;
        var radius = (cardW + FACE_GAP) / 2 / Math.tan((step / 2) * Math.PI / 180);
        // Свежая копия экрана с тем же набором точек: поворот барабана остаётся прежним.
        var keepTheta = geo.signature === signature && geo.n === n;
        geo.n = n; geo.step = step; geo.radius = radius; geo.cardW = cardW; geo.built = c; geo.signature = signature;
        var d = drum();
        if (d) {
            d.style.setProperty("--drum-radius", radius.toFixed(1) + "px");
            d.style.setProperty("--drum-step", step.toFixed(3) + "deg");
        }
        list.forEach(function (card, index) {
            card.dataset.driverPointIndex = String(index);
            card.style.setProperty("--card-angle", (index * step).toFixed(3) + "deg");
        });
        if (!keepTheta) geo.theta = 0;
        lastFront = -1;
        return true;
    }

    function frontIndex(theta) {
        if (!geo.n) return -1;
        return mod(Math.round(-theta / geo.step), geo.n);
    }

    function centerCard() {
        var list = cards();
        var front = frontIndex(geo.theta);
        return front >= 0 ? list[front] : null;
    }

    function frontPointId() {
        var card = centerCard();
        return card ? String(card.dataset.driverPointId || "") : "";
    }

    function indexOfPoint(pointId) {
        var list = cards();
        for (var i = 0; i < list.length; i++) {
            if (String(list[i].dataset.driverPointId) === String(pointId)) return i;
        }
        return -1;
    }

    function nearestTheta(index) {
        var target = -index * geo.step;
        var k = Math.round((geo.theta - target) / 360);
        return target + k * 360;
    }

    var lastFront = -1;

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
        if (typeof root.driverHaptic === "function") { root.driverHaptic(pattern); return; }
        if (root.navigator && typeof root.navigator.vibrate === "function") {
            try { root.navigator.vibrate(pattern); } catch (e) {}
        }
    }

    function render(snapping) {
        var c = cylinder();
        if (!c || !geo.n) return;
        c.classList.toggle("is-snapping", !!snapping);
        if (snapping) {
            // Пока грань доезжает, контур не сверяется (driver-downtime-drum-v1.js).
            c.__snapUntil = Date.now() + 360;
            root.clearTimeout(c.__snapTimer);
            c.__snapTimer = root.setTimeout(function () { c.__snapUntil = 0; syncLink(); }, 370);
        }
        var liveCardW = cards()[0] ? cards()[0].offsetWidth : 0;
        if (liveCardW && geo.step && Math.abs(liveCardW - (geo.cardW || 0)) >= 2) {
            geo.cardW = liveCardW;
            geo.radius = (liveCardW + FACE_GAP) / 2 / Math.tan((geo.step / 2) * Math.PI / 180);
            var drumNode = drum();
            if (drumNode) drumNode.style.setProperty("--drum-radius", geo.radius.toFixed(1) + "px");
        }
        c.style.transform = "translateZ(" + (-geo.radius).toFixed(1) + "px) rotateY(" + geo.theta.toFixed(3) + "deg)";
        var front = frontIndex(geo.theta);
        cards().forEach(function (card, index) {
            var rel = mod(index * geo.step + geo.theta + 180, 360) - 180;
            var a = Math.abs(rel);
            var isCenter = index === front;
            if (card.classList.contains("is-center") !== isCenter) card.classList.toggle("is-center", isCenter);
            var isBack = a > 100;
            if (card.classList.contains("is-back") !== isBack) card.classList.toggle("is-back", isBack);
            var fade = Math.max(0.22, 1 - Math.max(0, a - 6) / 48).toFixed(2);
            if (card.__fade !== fade) { card.__fade = fade; card.style.setProperty("--drum-fade", fade); }
        });
        if (front !== lastFront) {
            if (lastFront !== -1) { haptic(32); click(1); }
            lastFront = front;
        }
    }

    function rotateTo(index, animate) {
        if (!geo.n) return;
        geo.theta = nearestTheta(mod(index, geo.n));
        render(animate);
    }

    function snap(animate) { rotateTo(frontIndex(geo.theta), animate !== false); }

    // Назначенная точка всегда стоит на передней грани.
    function syncAssigned() {
        var id = assignedPointId();
        if (!id || !geo.n || drag) return;
        if (frontPointId() === id) return;
        var index = indexOfPoint(id);
        if (index >= 0) rotateTo(index, true);
    }

    // --- контур: горлышко от кольца циферблата вверх к передней грани ---
    function screenOf(el) {
        while (el && !(el.classList && el.classList.contains("driver-work-screen"))) el = el.parentElement;
        return el;
    }

    // Контур целиком (кольцо и оба горлышка) — одна кривая барабана простоев: она же
    // мигает при простое. Верхнее горлышко зависит от ширины передней грани этого
    // барабана, поэтому после её сдвига контур пересчитывается.
    function syncLink() {
        if (root.DriverDowntimeDrum && typeof root.DriverDowntimeDrum.syncLink === "function") {
            root.DriverDowntimeDrum.syncLink();
        }
    }

    // --- действия ручного рейса: их выполняет движок ручного режима ---
    function blockingDowntime() {
        var active = q("[data-driver-downtime-drum] .driver-drum-card.is-active-downtime");
        if (!active) return false;
        var reason = q('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + active.dataset.driverDrumReasonId + '"]');
        return !(reason && reason.dataset.driverDowntimeFlow === "waiting_loading");
    }

    function sendToPoint(card) {
        var api = engine();
        var pointId = card.dataset.driverPointId;
        if (!api || typeof api.startManualLoadAtPoint !== "function") {
            toast("Ручной режим недоступен: обновите экран");
            return;
        }
        // «Ожидание погрузки» погрузку не держит — сервер закроет его вместе с погрузкой.
        // Любой другой простой (обед, ремонт) погрузку не пускает.
        if (blockingDowntime()) {
            haptic([45, 60, 45]);
            toast("Сначала завершите простой");
            return;
        }
        haptic([35, 45, 70]); click(1.6);
        api.startManualLoadAtPoint(pointId);
    }

    function recallPoint() {
        var api = engine();
        if (!api || typeof api.cancelActiveManualLoad !== "function") {
            toast("Ручной режим недоступен: обновите экран");
            return;
        }
        haptic([70, 45, 35]); click(1.3);
        api.cancelActiveManualLoad();
    }

    /* Круг с ручным рейсом. Сервер рисует круг только для рейса экскаваторщика; ручной
       рейс живёт в очереди телефона, поэтому круг включает этот модуль: та же кнопка,
       то же удержание (driver-shift-v1.js), только итог удержания — завершение ручного
       рейса. Пишем в разметку лишь при расхождении: наблюдатель ниже видит каждую правку. */
    function holdButton() { return q("[data-driver-hold-button]"); }

    function setDialLabel(text) {
        var label = q("[data-driver-dial-label]");
        if (!label || String(label.dataset.driverDialRaw || "") === text) return;
        label.textContent = text;
        label.dataset.driverDialRaw = text;
        label.setAttribute("aria-label", text);
        delete label.dataset.driverDialFitKey;
        /* Подгонка кегля — синхронно, в этом же кадре: асинхронная (через rAF) на один
           кадр рисовала новый текст ещё старым, слишком крупным кеглем от прежней
           подписи — «РАЗГРУЗКА СОХРАНЕНА» вспышкой вылезала за круг (пойман на телефоне
           26.09.2026). Geometry круга к этому моменту уже точно готова: сама смена
           классов круга (is-empty и т.п.) идёт раньше этого вызова, в том же тике. */
        if (typeof root.fitDriverDialLabelNow === "function") {
            root.fitDriverDialLabelNow(label);
        } else if (typeof root.scheduleDriverDialLabelFit === "function") {
            root.scheduleDriverDialLabelFit(true);
        }
    }

    function pointName(id) {
        var api = engine();
        var name = api && typeof api.activeManualPointName === "function" ? String(api.activeManualPointName() || "") : "";
        if (name) return name;
        var card = q('[data-driver-point-card][data-driver-point-id="' + id + '"]');
        return card ? String(card.dataset.driverPointName || "") : "";
    }

    // Ожидание разгрузки: круг жёлтый, как у обычного рейса; разгрузка — удержанием со шкалой.
    function setUnloadWait(button, on) {
        if (button.classList.contains("is-waiting-unload") !== !!on) button.classList.toggle("is-waiting-unload", !!on);
    }

    function syncDial() {
        var button = holdButton();
        var wrap = dial();
        if (!button || !wrap) return;
        var id = isManual() ? assignedPointId() : "";
        if (id) {
            var name = pointName(id) || "РУЧНОЙ РЕЙС";
            button.dataset.driverManualDial = "true";
            if (button.dataset.driverManualDialLabel !== name) button.dataset.driverManualDialLabel = name;
            // Идёт отправка завершения: круг показывает «ОТПРАВКА», не трогаем.
            if (button.classList.contains("is-pending")) return;
            if (button.disabled) button.disabled = false;
            if (button.hasAttribute("aria-disabled")) button.removeAttribute("aria-disabled");
            setUnloadWait(button, wrap.classList.contains("is-waiting-unload"));
            var aria = "Завершить ручной рейс на " + name + ". Удерживайте 1 секунду.";
            if (button.getAttribute("aria-label") !== aria) button.setAttribute("aria-label", aria);
            if (button.classList.contains("is-empty")) button.classList.remove("is-empty");
            if (!button.classList.contains("is-loaded") && !button.classList.contains("is-holding")) button.classList.add("is-loaded");
            if (wrap.classList.contains("is-empty")) wrap.classList.remove("is-empty");
            if (!wrap.classList.contains("is-loaded")) wrap.classList.add("is-loaded");
            setDialLabel(name);
            return;
        }
        if (button.dataset.driverManualDial !== "true") return;
        /* Рейс завершён или отменён. Куда ехать дальше телефон знает сам: назначение
           на экскаватор ручной разгрузкой не снимается. Раньше здесь держали
           заглушку «ожидание синхронизации» до ответа сервера (до минуты на
           нестабильной связи) — берём номер экскаватора не с экрана (тот текст мог
           быть устаревшим от предыдущего цикла — так родился баг с «—» вместо
           номера, пойманный на телефоне 26.09.2026), а из свежего атрибута карточки
           ручного режима, который сервер обновляет при каждой отрисовке. */
        delete button.dataset.driverManualDial;
        delete button.dataset.driverManualDialLabel;
        button.disabled = true;
        button.setAttribute("aria-disabled", "true");
        button.setAttribute("aria-label", "Разгрузка недоступна: нет загруженного рейса");
        button.classList.remove("is-loaded", "is-holding", "is-pending");
        button.classList.add("is-empty");
        setUnloadWait(button, false);
        wrap.classList.remove("is-loaded");
        wrap.classList.add("is-empty");
        var manualWorkspace = q("[data-driver-manual-workspace]");
        var nextExcavatorLabel = manualWorkspace
            ? String(
                manualWorkspace.dataset.driverManualExcavatorLabel
                || manualWorkspace.dataset.driverManualPrimaryExcavatorLabel
                || ""
            )
            : "";
        var savedLabel = nextExcavatorLabel || "НА ЗАГРУЗКУ";
        var savedNote = q(".driver-work-note");
        if (savedNote && savedNote.textContent.trim() !== "НА ЗАГРУЗКУ") {
            savedNote.textContent = "НА ЗАГРУЗКУ";
        }
        setDialLabel(savedLabel);
        // Страховка от того же кадра: если geometry круга ещё не готова прямо сейчас
        // (driverDialCoreHasVisibleGeometry вернёт false и молча пропустит), кадром позже
        // она уже готова почти наверняка — пересчитываем ещё раз явно.
        root.setTimeout(function () {
            if (typeof root.scheduleDriverDialLabelFit === "function") root.scheduleDriverDialLabelFit(true);
        }, 120);
    }

    // Удержание круга с ручным рейсом (вызывает driver-shift-v1.js вместо разгрузки).
    function completeFromDial() {
        var api = engine();
        if (!api || typeof api.completeActiveManualLoad !== "function") {
            toast("Ручной режим недоступен: обновите экран");
            return false;
        }
        function undoPending() {
            var button = holdButton();
            if (button) button.classList.remove("is-pending");
            refresh();
        }
        api.completeActiveManualLoad().then(function (saved) {
            if (!saved) { toast("Рейс ещё сохраняется, повторите"); undoPending(); }
        }).catch(function () {
            toast("Не удалось сохранить завершение рейса на телефоне");
            undoPending();
        });
        return true;
    }

    // --- жесты: горизонталь вращает, вертикаль на передней грани — в круг / из круга ---
    var drag = null;
    var inertia = 0;
    var ghost = null;
    var ghostRaf = 0, ghostY = 0;
    var sendMax = SEND_MAX;

    function stopInertia() { if (inertia) { root.cancelAnimationFrame(inertia); inertia = 0; } }

    function moveGhost(y) {
        ghostY = y;
        if (ghostRaf || !ghost) return;
        ghostRaf = root.requestAnimationFrame(function () {
            ghostRaf = 0;
            if (ghost) ghost.style.transform = "translateY(" + ghostY.toFixed(1) + "px)";
        });
    }

    function makeGhost(card, isRecall) {
        var screen = screenOf(card);
        if (!screen) return null;
        var box = screen.getBoundingClientRect();
        var r = card.getBoundingClientRect();
        var g = card.cloneNode(true);
        g.classList.add("driver-drum-ghost");
        g.classList.remove("is-lifting", "is-dropping");
        if (isRecall) g.classList.add("is-drop");
        g.removeAttribute("data-driver-point-card");
        g.removeAttribute("data-driver-point-clone");
        g.setAttribute("aria-hidden", "true");
        g.tabIndex = -1;
        g.style.left = (r.left - box.left) + "px";
        g.style.top = (r.top - box.top) + "px";
        g.style.width = r.width + "px";
        g.style.height = r.height + "px";
        g.style.transform = "translateY(0px)";
        screen.appendChild(g);
        var w = dial();
        var dr = w ? w.getBoundingClientRect() : null;
        g.__sendMax = dr ? Math.max(SEND_MAX, (dr.top + dr.height * 0.45) - r.bottom) : SEND_MAX;
        return g;
    }

    function resetGhost(card) {
        if (card) card.classList.remove("is-lifting", "is-dropping");
        if (!ghost) return;
        var g = ghost; ghost = null;
        if (ghostRaf) { root.cancelAnimationFrame(ghostRaf); ghostRaf = 0; }
        g.classList.add("is-settling");
        g.style.transform = "translateY(0px)";
        root.setTimeout(function () { if (g.parentNode) g.parentNode.removeChild(g); }, 240);
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
        var d = event.target && event.target.closest ? event.target.closest("[data-driver-point-drum]") : null;
        if (!d || event.button > 0 || !isManual()) return;
        stopInertia();
        unlockAudio();
        drag = {
            pointerId: event.pointerId, target: d, x0: event.clientX, y0: event.clientY,
            lastX: event.clientX, lastT: event.timeStamp, vx: 0, dy: 0,
            mode: "", card: event.target.closest("[data-driver-point-card]"), captured: false, theta0: geo.theta
        };
    }, true);

    doc.addEventListener("pointermove", function (event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        var dx = event.clientX - drag.x0;
        var dy = event.clientY - drag.y0;
        var assigned = assignedPointId() !== "";
        if (!drag.mode) {
            if (Math.abs(dx) < 6 && Math.abs(dy) < SEND_START) return;
            var isCenter = drag.card && drag.card.classList.contains("is-center");
            if (Math.abs(dx) >= Math.abs(dy)) {
                // Пока точка в круге, барабан зафиксирован.
                if (assigned) { drag = null; return; }
                drag.mode = "spin";
            } else if (dy > 0 && isCenter && !assigned) {
                drag.mode = "send";
                ghost = makeGhost(drag.card, false);
                sendMax = (ghost && ghost.__sendMax) || SEND_MAX;
                drag.card.classList.add("is-lifting");
            } else if (dy < 0 && isCenter && assigned && String(drag.card.dataset.driverPointId) === assignedPointId()) {
                drag.mode = "recall";
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
        if (drag.mode === "spin") {
            geo.theta = drag.theta0 + dx * (geo.step / Math.max(geo.cardW, 1));
            var dt = Math.max(1, event.timeStamp - drag.lastT);
            drag.vx = 0.8 * drag.vx + 0.2 * ((event.clientX - drag.lastX) / dt);
            drag.lastX = event.clientX; drag.lastT = event.timeStamp;
            render(false);
        } else if (drag.mode === "send") {
            var down = Math.max(0, Math.min(sendMax, dy));
            moveGhost(down);
            var armed = dy >= SEND_ARM;
            if (armed !== drag.armed) {
                drag.armed = armed;
                if (armed) { haptic(25); click(0.7); }
                if (ghost) ghost.classList.toggle("is-armed", armed);
            }
            drag.dy = dy;
        } else if (drag.mode === "recall") {
            var up = Math.max(-RECALL_MAX, Math.min(0, dy));
            moveGhost(up);
            var recallArmed = -dy >= RECALL_ARM;
            if (recallArmed !== drag.armed) {
                drag.armed = recallArmed;
                if (recallArmed) { haptic(25); click(0.7); }
                if (ghost) ghost.classList.toggle("is-armed", recallArmed);
            }
            drag.dy = dy;
        }
        event.preventDefault();
    }, { passive: false, capture: true });

    function finishSpin(state) {
        var v = state.vx * (geo.step / Math.max(geo.cardW, 1)) * 16;
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
        } else if (state.mode === "send") {
            resetGhost(state.card);
            if (state.dy >= SEND_TRIGGER) sendToPoint(state.card);
        } else if (state.mode === "recall") {
            resetGhost(state.card);
            if (-state.dy >= RECALL_TRIGGER) recallPoint();
        } else if (state.card && !state.card.classList.contains("is-center") && assignedPointId() === "") {
            rotateTo(Number(state.card.dataset.driverPointIndex), true);
        }
    }, true);

    doc.addEventListener("pointercancel", function (event) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        var state = endDrag();
        if (state.mode === "send" || state.mode === "recall") resetGhost(state.card);
        else snap(true);
    }, true);

    function abortGesture() {
        if (!drag) return;
        var state = endDrag();
        if (state.mode === "send" || state.mode === "recall") resetGhost(state.card);
        else snap(true);
    }
    root.addEventListener("blur", abortGesture);
    doc.addEventListener("visibilitychange", function () { if (doc.hidden) abortGesture(); });

    doc.addEventListener("wheel", function (event) {
        var d = event.target && event.target.closest ? event.target.closest("[data-driver-point-drum]") : null;
        if (!d || !geo.n || !isManual() || assignedPointId() !== "") return;
        var delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
        if (!delta) return;
        event.preventDefault();
        rotateTo(frontIndex(geo.theta) + (delta > 0 ? 1 : -1), true);
    }, { passive: false, capture: true });

    // --- состояние после подмены экрана и смены рейса ---
    function refresh() {
        var c = cylinder();
        // Новая копия экрана: грани собираются заново, поворот сохраняется (см. build).
        var fresh = !!(c && geo.built !== c);
        if (fresh) healStaleStyles();
        if (build()) render(false);
        syncModeControls();
        syncAssigned();
        syncDial();
        // Контур меняется только вместе с геометрией: на каждую мелкую правку экрана
        // (секунды таймера) замерять размеры не нужно.
        if (fresh) root.requestAnimationFrame(syncLink);
    }

    var observer = new MutationObserver(function (mutations) {
        var relevant = mutations.some(function (m) {
            if (m.target && m.target.closest && m.target.closest("[data-driver-point-drum]")) return false;
            return m.type === "childList" || m.type === "attributes";
        });
        if (relevant) refresh();
    });

    /* Разметка с барабаном пришла, а стили экрана остались из кэша от прежней
       версии (тот же адрес ?v=, пока версия оболочки не поднята): сетка без строки
       «pointdrum» сжимает экран в узкую колонку. Пойманный на телефоне 26.09.2026
       случай — лечим сами: один раз перечитываем стили в обход кэша и заново
       подгоняем экран под окно. */
    var staleStylesHealed = false;
    function healStaleStyles() {
        if (staleStylesHealed || !drum()) return;
        var screen = q(".driver-work-screen");
        if (!screen) return;
        var areas = root.getComputedStyle(screen).gridTemplateAreas || "";
        if (areas.indexOf("pointdrum") >= 0) return;
        staleStylesHealed = true;
        var stamp = String(Date.now());
        all('link[rel="stylesheet"][href*="/static/css/driver-"]').forEach(function (link) {
            var fresh = link.cloneNode();
            fresh.href = link.href + (link.href.indexOf("?") >= 0 ? "&" : "?") + "heal=" + stamp;
            fresh.addEventListener("load", function () {
                if (link.parentNode) link.parentNode.removeChild(link);
                geo.built = null;
                refresh();
                if (typeof root.driverScheduleViewportFit === "function") root.driverScheduleViewportFit();
            }, { once: true });
            link.parentNode.insertBefore(fresh, link.nextSibling);
        });
        if (typeof root.fetch === "function") {
            try { root.fetch("/client-error/", { method: "POST", credentials: "same-origin", keepalive: true,
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ kind: "driver-stale-styles-healed", areas: areas }) }).catch(function () {}); } catch (e) {}
        }
    }

    function init() {
        healStaleStyles();
        var s = shell();
        // Незавершённый ручной рейс — ручной режим включён, что бы ни было сохранено.
        var manualTrip = !!(s && s.dataset.driverActiveTripOrigin === "driver_manual");
        // Режим запоминается: после завершения рейса барабан остаётся в ручном режиме.
        if (manualTrip) storeMode(true);
        doc.documentElement.classList.toggle(MANUAL_CLASS, manualTrip || readStoredMode());
        refresh();
        observer.observe(doc.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-driver-active-trip-origin", "data-driver-manual-active-point-id"] });
        root.addEventListener("resize", function () { geo.built = null; refresh(); });
        // Движок ручного рейса сообщает о каждой погрузке, отмене и завершении.
        root.addEventListener("driver-manual-trip-changed", function (event) {
            if (event.detail && event.detail.pointId && !isManual()) setManual(true);
            refresh();
        });
        if (root.ResizeObserver) {
            var w = dial();
            if (w) new root.ResizeObserver(function () { syncLink(); }).observe(w);
        }
        root.setTimeout(refresh, 300);
    }

    root.DriverPointDrum = Object.freeze({
        refresh: refresh, isManual: isManual, setManual: setManual, completeFromDial: completeFromDial
    });

    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
    else init();
})(window);
