/* Подпись на грани барабана (простой снизу, точка разгрузки сверху).

   Кегль считает БРАУЗЕР, а не этот модуль: подпись — CSS-контейнер
   (container-type: size, driver-downtime-drum-v1.css), и размер главного слова —
   чистый CSS от размеров контейнера (cqw/cqh) и двух статичных чисел, которые
   модуль ставит ОДИН раз, когда известен текст:
     --lw  ширина самой длинной строки названия в em (в «ширинах кегля»);
     --ln  число строк (перенос только по словам — раскладку по строкам
           решает этот модуль, CSS больше ничего не переносит).
   Отсюда font-size = min(ширина / --lw, высота / --ln) — текст физически не
   может выйти за рамку, и браузер применяет это при КАЖДОЙ раскладке сам:
   скрытая вкладка, вращение, подмена фрагмента с сервера, смена ориентации —
   без единого замера и без повторных вызовов.

   Раньше здесь был двоичный поиск по живой раскладке (scrollWidth/clientWidth,
   ResizeObserver, повтор после document.fonts.ready, пересчёт после фиксации
   вращения). На телефоне он был ненадёжен: подмена фрагмента или простой,
   запущенный со скрытой вкладки «Простои», оставляли подпись на сыром CSS-
   потолке (огромный кегль, обрезанное слово), а повторные подгонки давали лаги
   и «прыгающий» размер (владелец, 28.09.2026). Ширину слова в em модуль берёт
   из метрик шрифта (canvas measureText — это не раскладка страницы, ответ не
   зависит от видимости, поворота и состояния DOM), по одному разу на текст. */
(function (root) {
    "use strict";

    if (!root) return;

    // Слова, для которых название стоит из двух частей: второстепенное сверху
    // мелко, главное — крупно. Список короткий и явный.
    var MINOR_LEAD_WORDS = ["ожидание", "ожидания"];
    // Больше строк не делим: у карточки мало высоты, четыре строки мельче трёх.
    var MAX_LINES = 3;
    // Межстрочный интервал главного слова (совпадает с line-height в CSS).
    var LINE_HEIGHT = 1.02;
    // Ширина пробела в em для «Roboto Condensed» 900 — нужна только для выбора
    // раскладки по строкам, в размер не идёт (каждая строка меряется целиком).
    var SPACE_EM = 0.25;
    // Номинальное отношение ширины к высоте области главного слова — только
    // чтобы выбрать, на сколько строк бить название (размер всё равно решает
    // CSS по фактическим размерам). Верхний барабан: 114×78 → ~1,45. Нижний: над
    // полосой таймера ~114×52 → ~2,2; с мелкой строкой «ОЖИДАНИЕ» ещё ниже → ~3.
    var NOMINAL_RATIO = { point: 1.45, downtime: 2.2, downtimeSplit: 3.0 };
    // Запасная оценка, если canvas недоступен: средняя ширина прописной
    // кириллицы жирного узкого начертания с запасом (Ш/Щ/Ж ≈ 1,0–1,05 em,
    // большинство ≈ 0,63–0,70 em).
    var FALLBACK_CHAR_EM = 0.78;

    var emCache = Object.create(null);
    var measureCtx = null;
    var measureFont = "";

    function fontStackOf(element) {
        if (measureFont) return measureFont;
        var family = "";
        try {
            family = root.getComputedStyle ? root.getComputedStyle(element).fontFamily : "";
        } catch (error) {
            family = "";
        }
        measureFont = "900 100px " + (family || "\"Roboto Condensed\", \"Arial Narrow\", Arial, sans-serif");
        return measureFont;
    }

    // Ширина строки в em (при кегле 1). Прописными — так текст и рисуется
    // (text-transform: uppercase на карточке).
    function lineEm(text, element) {
        var upper = String(text || "").toUpperCase();
        if (upper in emCache) return emCache[upper];
        var em = 0;
        if (!measureCtx && root.document && root.document.createElement) {
            try {
                var canvas = root.document.createElement("canvas");
                measureCtx = canvas.getContext ? canvas.getContext("2d") : null;
            } catch (error) {
                measureCtx = null;
            }
        }
        if (measureCtx) {
            measureCtx.font = fontStackOf(element);
            em = measureCtx.measureText(upper).width / 100;
        }
        if (!(em > 0)) em = upper.length * FALLBACK_CHAR_EM;
        emCache[upper] = em;
        return em;
    }

    // Все разбиения слов на 1..MAX_LINES строк подряд, без разрыва слова.
    function partitions(words, maxLines) {
        var out = [];
        function walk(start, lines) {
            if (start === words.length) { out.push(lines.slice()); return; }
            if (lines.length === maxLines) return;
            for (var end = start + 1; end <= words.length; end++) {
                lines.push(words.slice(start, end).join(" "));
                walk(end, lines);
                lines.pop();
            }
        }
        walk(0, []);
        return out;
    }

    // Раскладка, дающая самый крупный кегль при номинальной пропорции области:
    // кегль ∝ min(ratio / самая длинная строка, 1 / (строк × интервал)).
    function chooseLayout(text, ratio, element) {
        var words = String(text || "").trim().split(/\s+/).filter(Boolean);
        if (!words.length) return { lines: [""], lw: 1, ln: 1 };
        var best = null;
        partitions(words, Math.min(MAX_LINES, words.length)).forEach(function (lines) {
            var widest = 0;
            lines.forEach(function (line) { widest = Math.max(widest, lineEm(line, element)); });
            var score = Math.min(ratio / widest, 1 / (lines.length * LINE_HEIGHT));
            if (!best || score > best.score + 1e-9) best = { score: score, lines: lines, lw: widest };
        });
        return { lines: best.lines, lw: best.lw, ln: best.lines.length };
    }

    function splitLeadWord(text) {
        var trimmed = String(text || "").trim().replace(/\s+/g, " ");
        var spaceAt = trimmed.indexOf(" ");
        if (spaceAt === -1) return null;
        var lead = trimmed.slice(0, spaceAt).toLowerCase();
        if (MINOR_LEAD_WORDS.indexOf(lead) === -1) return null;
        return { minor: trimmed.slice(0, spaceAt), main: trimmed.slice(spaceAt + 1) };
    }

    function drumKind(labelRoot) {
        return labelRoot.closest && labelRoot.closest("[data-driver-point-drum]") ? "point" : "downtime";
    }

    /* Строит разметку подписи один раз на текст: [мелкая строка «ОЖИДАНИЕ»] +
       главное слово, разбитое по строкам (каждая строка — свой блок без
       переноса), и ставит --lw/--ln. Повторный вызов с тем же текстом ничего
       не пишет в DOM. Замеров раскладки нет — только метрики шрифта. */
    function fit(labelRoot) {
        if (!labelRoot || !labelRoot.dataset) return null;
        if (labelRoot.dataset.driverDrumLabelText === undefined) {
            labelRoot.dataset.driverDrumLabelText = labelRoot.textContent;
        }
        var raw = String(labelRoot.dataset.driverDrumLabelText || "").trim();
        var kind = drumKind(labelRoot);
        var key = kind + "|" + raw;
        if (labelRoot.dataset.driverDrumLabelKey === key && labelRoot.querySelector(".driver-drum-card-label-main")) {
            return { cached: true };
        }

        var split = splitLeadWord(raw);
        var mainText = split ? split.main : raw;
        var ratio = kind === "point" ? NOMINAL_RATIO.point : (split ? NOMINAL_RATIO.downtimeSplit : NOMINAL_RATIO.downtime);
        var layout = chooseLayout(mainText, ratio, labelRoot);

        var doc = labelRoot.ownerDocument;
        labelRoot.textContent = "";
        if (split) {
            var minor = doc.createElement("span");
            minor.className = "driver-drum-card-label-minor";
            minor.textContent = split.minor;
            labelRoot.appendChild(minor);
        }
        var main = doc.createElement("span");
        main.className = "driver-drum-card-label-main";
        layout.lines.forEach(function (line) {
            var row = doc.createElement("span");
            row.className = "driver-drum-card-label-line";
            row.textContent = line;
            main.appendChild(row);
        });
        labelRoot.appendChild(main);

        labelRoot.classList.toggle("is-drum-label-split", !!split);
        labelRoot.style.setProperty("--lw", layout.lw.toFixed(3));
        labelRoot.style.setProperty("--ln", String(layout.ln));
        labelRoot.style.setProperty("--has-minor", split ? "1" : "0");
        labelRoot.dataset.driverDrumLabelKey = key;
        return { lw: layout.lw, ln: layout.ln, lines: layout.lines };
    }

    function fitAll(container, selector) {
        if (!container) return;
        Array.prototype.forEach.call(container.querySelectorAll(selector || "[data-driver-drum-label]"), fit);
    }

    root.DriverDrumLabelFit = Object.freeze({ fit: fit, fitAll: fitAll, chooseLayout: chooseLayout });
}(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this)));
