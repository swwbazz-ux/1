"use strict";
/* Карточки барабанов (простои, точки разгрузки) на телефоне были нечитаемо мелкими:
   формула кегля не зависела от длины текста и почти всегда садилась на нижний
   предел (12px на карточке 131×92px). Порог — по числу символов, как и у карточек
   точки в ручном режиме (driver-manual-excavator-workspace-v1.js, dumpNameSizeClass).
   Отдельная находка: у подписи и тела карточки не было собственной ширины — грид без
   width сжимался под свой же текст (в 3D-трансформе и контейнерном запросе width:100%
   не решает — резолвится не от карточки, а к ~40% от неё), перенос слов ломался
   посередине слова. Таймер активного простоя (.driver-drum-card-total) — отдельная
   короткая цифровая строка, не завязан на длину названия причины.
   Пойман и проверен на телефоне 26.09.2026. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const CSS = fs.readFileSync(path.resolve(__dirname, "../../css/driver-downtime-drum-v1.css"), "utf8");
const DOWNTIME_TEMPLATE = fs.readFileSync(
    path.resolve(__dirname, "../../../templates/includes/driver_downtime_drum.html"),
    "utf8"
);
const POINT_TEMPLATE = fs.readFileSync(
    path.resolve(__dirname, "../../../templates/includes/driver_point_drum.html"),
    "utf8"
);

test("drum card label and body get an explicit pixel width, not a percentage", () => {
    // width:100% на гриде без явной ширины внутри 3D-карточки резолвился не от
    // карточки — только calc(var(--drum-card-w) - Npx) даёт настоящую ширину.
    assert.doesNotMatch(CSS, /\.driver-drum-card-body\s*\{[^}]*width:\s*100%/);
    assert.doesNotMatch(CSS, /\.driver-drum-card-label\s*\{[^}]*width:\s*100%/);
    assert.match(CSS, /\.driver-drum-card-body\s*\{[^}]*width:\s*calc\(var\(--drum-card-w\) - 20px\)/s);
    assert.match(CSS, /\.driver-drum-card-label\s*\{[^}]*width:\s*calc\(var\(--drum-card-w\) - 20px\)/s);
});

test("drum name size is pure CSS from the label container, not a JS measurement", () => {
    // Подгонка по живой раскладке (двоичный поиск по scrollWidth, ResizeObserver,
    // fonts.ready, пересчёт после вращения) на телефоне оставляла подпись на
    // сыром CSS-потолке после подмены фрагмента или простоя со скрытой вкладки —
    // огромный кегль, обрезанное слово — и давала лаги с «прыгающим» размером
    // (владелец, 28.09.2026). Теперь подпись — контейнер размеров, а кегль —
    // min(ширина / самая длинная строка в em, высота / число строк), браузер
    // применяет его сам при любой раскладке.
    assert.match(CSS, /\.driver-drum-card-label\s*\{[^}]*container-type:\s*size/s);
    assert.match(CSS, /\.driver-drum-card-label-main\s*\{[^}]*font-size:\s*min\(\s*calc\(100cqw \/ \(var\(--lw\) \* 1\.04\)\),\s*calc\(\(100cqh - 2px - var\(--has-minor\) \* \(clamp\(9px, 16cqh, 14px\) \+ 2px\)\) \/ \(var\(--ln\) \* 1\.02\)\),\s*46px\s*\)/s);
    // Мелкая строка «ОЖИДАНИЕ» — ровно та высота, которую вычитает главное слово.
    assert.match(CSS, /\.driver-drum-card-label-minor\s*\{[^}]*font-size:\s*clamp\(9px, 16cqh, 14px\)/s);
    // Переносит не браузер, а разметка по строкам: каждая строка — блок без
    // переноса, слово не разрывается никогда.
    assert.match(CSS, /\.driver-drum-card-label-line\s*\{[^}]*white-space:\s*nowrap[^}]*word-break:\s*keep-all[^}]*hyphens:\s*none/s);
    assert.doesNotMatch(CSS, /\.driver-drum-card-label\.is-drum-label-medium/);
    assert.doesNotMatch(CSS, /\.driver-drum-card-label\.is-drum-label-long/);
    // Модуль подписи не меряет раскладку страницы вовсе (проверяем код, не
    // комментарии — в шапке файла описано, почему от этого отказались).
    const fitJs = fs.readFileSync(path.resolve(__dirname, "../driver-drum-label-fit-v1.js"), "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    assert.doesNotMatch(fitJs, /scrollWidth|clientWidth|clientHeight|getBoundingClientRect|offsetWidth|ResizeObserver|fonts\.ready/);
});

test("line layout keeps every word whole and picks the split that gives the biggest name", () => {
    // Модуль без canvas (как в node) берёт запасную оценку ширины — для выбора
    // раскладки этого достаточно; размер всё равно решает CSS по фактической рамке.
    const sandbox = { globalThis: null };
    sandbox.globalThis = sandbox;
    const vm = require("node:vm");
    vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../driver-drum-label-fit-v1.js"), "utf8"), sandbox);
    const { chooseLayout } = sandbox.DriverDrumLabelFit;
    const cases = [
        ["Склад окисленной руды", 1.45],
        ["Чистка кузова", 2.2],
        ["погрузки", 3.0],
        ["Поломка", 2.2],
        ["ККД", 1.45],
    ];
    cases.forEach(([text, ratio]) => {
        const layout = chooseLayout(text, ratio, null);
        // Строки склеиваются обратно ровно в исходный текст: ни одно слово не
        // разорвано и не потеряно.
        assert.equal(layout.lines.join(" "), text);
        layout.lines.forEach((line) => assert.ok(line.length > 0));
        assert.ok(layout.ln >= 1 && layout.ln <= 3);
        assert.ok(layout.lw > 0);
    });
    // Длинное название точки на узкой карточке — по строке на слово, а не одной
    // строкой в микроскопическом кегле.
    assert.equal(chooseLayout("Склад окисленной руды", 1.45, null).ln, 3);
    assert.equal(chooseLayout("Поломка", 2.2, null).ln, 1);
});

test("active-downtime timer is not tied to the reason-name tier and reads clearly on its own", () => {
    // Таймер простоя — всегда короткая цифровая строка независимо от того, как длинно
    // называется причина; раньше кегль доходил до 22px — таймер читался крупнее
    // самого названия (владелец, 28.09.2026). Таймер стоит в своей отдельной полосе
    // фиксированной высоты (--drum-timer-strip-h) — крупнее прежнего (владелец,
    // 28.09.2026: «может быть крупнее — снизу места достаточно»), но кегль всё равно
    // от высоты полосы, а не от длины названия причины.
    assert.doesNotMatch(CSS, /\.driver-drum-card-total\.is-drum-label-medium/);
    assert.doesNotMatch(CSS, /\.driver-drum-card-total\.is-drum-label-long/);
    assert.match(CSS, /\.driver-drum-card-total\s*\{[^}]*font-size:\s*clamp\(13px, calc\(var\(--drum-timer-strip-h\) \* 0\.62\), 21px\)/s);
    assert.match(CSS, /--drum-timer-strip-h:\s*clamp\(22px, calc\(var\(--drum-card-h\) \* 0\.26\), 36px\)/);
});

test("the timer strip stays reserved along the card's bottom edge even without an active downtime", () => {
    // Полоса таймера — свой отдельный ряд грида фиксированной высоты, не общий с
    // названием: без него ряд бы схлопнулся ([hidden] обычно display:none), область
    // над ним выросла бы на его высоту, и текст прыгнул бы при каждом старте и
    // завершении простоя (владелец, 28.09.2026: «...ничего не прыгает»).
    assert.match(CSS, /\.driver-drum-card-body\s*\{[^}]*grid-template-rows:\s*minmax\(0, 1fr\) var\(--drum-timer-strip-h\)/s);
    assert.match(CSS, /\.driver-drum-card-total\[hidden\]\s*\{\s*display:\s*flex;\s*visibility:\s*hidden;\s*\}/);
});

test("dump-point tiles in the change-point sheet match the excavator face-settings card look", () => {
    const css = fs.readFileSync(path.resolve(__dirname, "../../css/mobile-dial-actions-v1.css"), "utf8");
    // Тот же плоский вид, что у .eo-unload-card в mobile-face-unified-v1.css: без
    // капслока, без прежнего градиента, зелёная рамка вместо свечения на выбранной.
    assert.match(css, /\.driver-unload-tile\s*\{[^}]*background:\s*rgba\(7, 18, 23, \.96\)/s);
    assert.match(css, /\.driver-unload-tile\s*\{[^}]*text-transform:\s*none/s);
    assert.doesNotMatch(css, /\.driver-unload-tile\s*\{[^}]*linear-gradient/s);
    // app.css задаёт uppercase и свой цвет ПРЯМО на driver-unload-tile strong —
    // наследование текст-transform/color от родителя это не отменяет, нужно
    // явно погасить и на самом strong (пойман на телефоне 26.09.2026).
    assert.match(css, /\.driver-unload-tile strong\s*\{[^}]*text-transform:\s*none/s);
    assert.match(css, /\.driver-unload-tile strong\s*\{[^}]*color:\s*inherit/s);
    assert.match(css, /\.driver-unload-tile\.is-current\s*\{[^}]*border-color:\s*#67e854/s);
    assert.match(css, /\.driver-unload-tile-status\s*\{\s*display:\s*none;/);
});

test("dial label multiline width stays large — the loaded trip's dump point name is read for the whole trip", () => {
    const css = fs.readFileSync(path.resolve(__dirname, "../../css/driver-shift-v1.css"), "utf8");
    // Первая правка узила коробку до 70%/64% ради края круга при коротком сообщении
    // «РАЗГРУЗКА СОХРАНЕНА» — но та же коробка держит и название точки разгрузки,
    // которое видно весь рейс, а не секунду. Пересчитано по запасу до края круга:
    // 85%/80% дают ещё 11–13px запаса и заметно крупнее (пойман на телефоне 26.09.2026).
    assert.match(css, /\.driver-work-label\.is-two-line\s*\{\s*width:\s*85%;\s*max-width:\s*85%/);
    assert.match(css, /\.driver-work-label\.is-three-line\s*\{\s*width:\s*80%;\s*max-width:\s*80%/);
});

test("templates mark drum labels for JS fitting instead of classifying by character length", () => {
    assert.doesNotMatch(DOWNTIME_TEMPLATE, /is-drum-label-long|is-drum-label-medium/);
    assert.doesNotMatch(POINT_TEMPLATE, /is-drum-label-long|is-drum-label-medium/);
    assert.match(DOWNTIME_TEMPLATE, /driver-drum-card-label"\s+data-driver-drum-label>\{\{ reason\.button_label \}\}/);
    assert.match(POINT_TEMPLATE, /driver-drum-card-label"\s+data-driver-drum-label>\{\{ point\.name \}\}/);
    // Цвет грани простоя — по статусу причины (та же палитра, что во вкладке
    // «Простои»), не хардкод по названию.
    assert.match(DOWNTIME_TEMPLATE, /driver-drum-card status-\{\{ reason\.effective_color_group \}\}/);
});

test("downtime link/halo blink color follows the active reason's status, not a hardcoded yellow", () => {
    // Красная причина (поломка, авария) должна мигать красным, а не жёлтым —
    // иначе мигание само по себе не отличает «жду» от «сломался» (владелец,
    // 28.09.2026). Цвет ставит JS на <html> заранее (тот же приём, что и
    // --link-path) — кадр мигания меняет только прозрачность готового слоя.
    const drumJs = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");
    assert.match(drumJs, /DOWNTIME_ACCENT_BY_STATUS\s*=\s*\{\s*orange:\s*"#fb923c",\s*red:\s*"#ff5a47"\s*\}/);
    assert.match(drumJs, /function syncDowntimeAccent\(active\)/);
    assert.match(drumJs, /documentElement\.style\.setProperty\("--driver-downtime-accent", accent\)/);
    assert.match(CSS, /\.driver-drum-link-live\s*\{[^}]*stroke:\s*var\(--driver-downtime-accent,\s*var\(--driver-yellow\)\)/s);
    assert.match(CSS, /\.driver-drum-link-glow\s*\{[^}]*stroke:\s*color-mix\(in srgb, var\(--driver-downtime-accent, var\(--driver-yellow\)\)/s);
    assert.match(CSS, /\.driver-work-dial\.is-downtime-active \.driver-work-wait-tint\s*\{[^}]*color-mix\(in srgb, var\(--driver-downtime-accent, var\(--driver-yellow\)\)/s);
});

test("without an open shift, the top drum keeps its full three-card shape and outline instead of collapsing", () => {
    // Первая попытка чинить это оставляла одну серую грань без окантовки — координатор
    // поймал по скриншоту: у барабана точек пропадала рамка-«горлышко» к кругу (она
    // ищет .is-center у настоящей карточки), и было видно только одну грань вместо
    // трёх (центр + два бока), хотя со сменой барабан всегда так и выглядит. Теперь
    // пустых граней три, на тех же углах (--drum-step), и у центральной свой класс
    // is-drum-empty-center — не .is-center, чтобы не словить жёлтую CSS-подсветку,
    // но контур её всё равно находит (driver-downtime-drum-v1.js). Пойман на реальном
    // полевом тесте 26-27.09.2026.
    const emptyBlock = POINT_TEMPLATE.split("{% empty %}")[1].split("{% endfor %}")[0];
    const emptyCardCount = (emptyBlock.match(/driver-drum-card driver-drum-card-empty/g) || []).length;
    assert.equal(emptyCardCount, 3);
    assert.match(emptyBlock, /is-drum-empty-center/);
    assert.doesNotMatch(emptyBlock, /driver-drum-card-empty is-center/);
    assert.match(emptyBlock, /--card-angle:\s*calc\(-1 \* var\(--drum-step\)\)/);
    assert.match(emptyBlock, /--card-angle:\s*var\(--drum-step\)/);
    assert.match(POINT_TEMPLATE, /\{% if open_shift %\}[\s\S]*?Экскаватору не назначены точки разгрузки/);
});

test("the keyhole outline still finds the top drum's empty center card", () => {
    const drumJs = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");
    assert.match(drumJs, /\[data-driver-point-drum\]\s*\.driver-drum-card-empty\.is-drum-empty-center/);
});

test("downtime drum front card is never highlighted yellow without an open shift", () => {
    // is-center — это «эта грань сейчас смотрит на водителя», не «простой идёт»,
    // но выглядит одинаково ярко-жёлто в обоих случаях. Без смены простоя быть не
    // может вообще, поэтому и переднюю грань подсвечивать нечем — иначе водитель
    // видит ровно то, на что жаловался («ОФР горит без смены»). Пойман на реальном
    // полевом тесте 26.09.2026.
    const drumJs = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");
    assert.match(drumJs, /var isCenter = index === front && hasOpenShift\(\);/);
    assert.match(drumJs, /function hasOpenShift\(\)/);
});

test("labels are laid out only when a card is built, never on spin, resize observers or font loads", () => {
    // Кегль считает CSS сам, поэтому никаких повторных «подгонок» больше нет:
    // ни ResizeObserver на барабане, ни document.fonts.ready, ни пересчёта после
    // фиксации вращения, ни на раннем выходе build()/refresh() (который
    // дёргается на любую относящуюся мутацию). Разметка строк строится при сборке
    // барабана и при подмене фрагмента (reapply) — один раз на текст.
    const drumJs = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");
    const pointJs = fs.readFileSync(path.resolve(__dirname, "../driver-point-drum-v1.js"), "utf8");
    [drumJs, pointJs].forEach((src) => {
        const earlyReturn = src.match(/if \(geo\.built === c[\s\S]{0,160}\)\s*\{([\s\S]{0,900}?)return true;/);
        assert.ok(earlyReturn, "early-return branch of build() not found");
        assert.doesNotMatch(earlyReturn[1], /fitLabels\(/);
        // Файлы с CRLF — конец функции ищем с необязательным \r.
        const render = src.match(/function render\(snapping\) \{([\s\S]*?)\r?\n    \}\r?\n/);
        assert.ok(render, "render() not found");
        assert.doesNotMatch(render[1], /fitLabels\(/);
        const pointerMove = src.match(/doc\.addEventListener\("pointermove"[\s\S]*?\}, \{ passive: false, capture: true \}\);/);
        assert.ok(pointerMove, "pointermove handler not found");
        assert.doesNotMatch(pointerMove[0], /fitLabels\(/);
        assert.doesNotMatch(src, /fonts\.ready/);
        assert.doesNotMatch(src, /lastFitW|roDrum/);
        // Ресайз окна — с задержкой, не серией полных пересборок подряд.
        assert.match(src, /root\.addEventListener\("resize", function \(\) \{\s*root\.clearTimeout\(resizeTimer\);/);
        // Летящая копия грани живёт вне барабана — ей переносятся размеры грани,
        // иначе подпись-контейнер схлопнулась бы и текст в копии пропал.
        assert.match(src, /g\.style\.setProperty\("--drum-card-w", r\.width \+ "px"\);/);
    });
});

test("the shared outline is stationary: never recomputed while either drum is being dragged", () => {
    // Контур — стационарный элемент, как большая круглая кнопка: во время
    // вращения is-center уже мог перескочить на соседнюю грань (по угловой
    // близости, не по фактическому положению), и контур, посчитанный от неё,
    // «прыгал» вместе с прокруткой (владелец, 28.09.2026).
    const drumJs = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");
    const pointJs = fs.readFileSync(path.resolve(__dirname, "../driver-point-drum-v1.js"), "utf8");
    assert.match(drumJs, /function syncLinkVars\(\) \{[\s\S]{0,900}if \(drag\) return;[\s\S]{0,200}root\.DriverPointDrum\.isDragging\(\)\) return;/);
    assert.match(pointJs, /isDragging: function \(\) \{ return !!drag; \}/);
});
