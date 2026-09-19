/* Уровень виброотклика экрана водителя.

   Веб-платформа умеет задавать вибрации только длительность: силу (амплитуду)
   через navigator.vibrate выставить нельзя — это умеет только нативный модуль,
   которого в оболочке нет. Поэтому «сила» здесь — это длительность импульсов и
   рисунок из нескольких импульсов подряд. Уровень выбирает водитель на вкладке
   «Смена», хранится на телефоне; по умолчанию — «сильный»: на боевом Xiaomi
   (20.09.2026) короткие импульсы 14–32 мс не ощущались вовсе.

   Все вибрации круга и барабана идут через window.driverHaptic(pattern):
   импульсы («включено») умножаются на множитель уровня и не короче нижней
   планки, паузы между ними не трогаются — иначе рисунок расползётся.

   Ловушки: Chrome блокирует navigator.vibrate до первого касания страницы
   («user hasn't tapped on the frame»); если в настройках телефона выключен
   «Виброотклик при касании», Android принимает вызовы, но глушит их (в dumpsys
   vibrator_manager видно scale 0,00) — уровень в приложении тут бессилен. */
(function driverHapticsModule() {
    "use strict";

    var STORAGE_KEY = "driver-haptic-level";
    var LEVELS = {
        weak: {factor: 0.6, floor: 16, label: "Слабый", sample: [40]},
        normal: {factor: 1, floor: 20, label: "Средний", sample: [70]},
        strong: {factor: 1.8, floor: 30, label: "Сильный", sample: [110, 60, 110]}
    };
    var DEFAULT_LEVEL = "strong";

    function readLevel() {
        try {
            var stored = String(window.localStorage.getItem(STORAGE_KEY) || "");
            if (LEVELS[stored]) return stored;
        } catch (error) {}
        return DEFAULT_LEVEL;
    }

    function writeLevel(level) {
        try { window.localStorage.setItem(STORAGE_KEY, level); } catch (error) {}
    }

    function scalePattern(pattern, level) {
        var spec = LEVELS[level] || LEVELS[DEFAULT_LEVEL];
        var list = Array.isArray(pattern) ? pattern : [pattern];
        return list.map(function (value, index) {
            var ms = Number(value) || 0;
            if (index % 2 === 1) return ms;           // пауза между импульсами
            if (ms <= 0) return 0;                    // vibrate(0) — отмена
            return Math.round(Math.max(spec.floor, ms * spec.factor));
        });
    }

    function vibrate(pattern) {
        if (!window.navigator || typeof window.navigator.vibrate !== "function") return false;
        var scaled = scalePattern(pattern, readLevel());
        try { return window.navigator.vibrate(scaled.length === 1 ? scaled[0] : scaled) === true; } catch (error) { return false; }
    }

    function setLevel(level, options) {
        if (!LEVELS[level]) return readLevel();
        writeLevel(level);
        renderControls();
        if (!options || options.silent !== true) {
            // Пробный импульс сразу, чтобы водитель услышал разницу пальцем.
            vibrate(LEVELS[level].sample);
        }
        return level;
    }

    function renderControls() {
        var current = readLevel();
        var buttons = document.querySelectorAll("[data-driver-haptic-level]");
        Array.prototype.forEach.call(buttons, function (button) {
            var own = String(button.getAttribute("data-driver-haptic-level") || "");
            var active = own === current;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", active ? "true" : "false");
        });
    }

    /* Кнопки уровня появляются и пропадают вместе с вкладкой «Смена» при подмене
       разметки, поэтому слушаем документ, а не конкретные узлы. */
    document.addEventListener("click", function (event) {
        var target = event.target && event.target.closest
            ? event.target.closest("[data-driver-haptic-level]")
            : null;
        if (!target) return;
        event.preventDefault();
        setLevel(String(target.getAttribute("data-driver-haptic-level") || ""));
    });
    window.addEventListener("operational-state-refresh-applied", renderControls);
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", renderControls);
    } else {
        renderControls();
    }

    window.driverHaptic = vibrate;
    window.driverHaptics = {
        levels: LEVELS,
        getLevel: readLevel,
        setLevel: setLevel,
        scalePattern: scalePattern,
        renderControls: renderControls
    };
})();
