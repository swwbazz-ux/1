/* Подпись на грани барабана (простой снизу, точка разгрузки сверху): максимально
   крупно, насколько позволяет карточка, перенос только по пробелу, никогда не
   рвём слово посередине. Приоритет чтения в карьере, с самого крупного:
   1) главное название/причина — .driver-drum-card-label-main, вся доступная
      высота карточки за вычетом мелких строк;
   2) второстепенное слово «Ожидание» (если есть) — .driver-drum-card-label-minor,
      один фиксированный мелкий кегль на всех карточках;
   3) таймер простоя — свой фиксированный мелкий кегль (CSS,
      .driver-drum-card-total), к этому модулю не относится.

   Раньше кегль решался по числу символов во всей подписи (driver_downtime_drum.html,
   is-drum-label-medium/-long) — короткое слово вроде «ККД» вставало крупно, а
   «Ожидание погрузки» держали мелким, хотя карточка та же ширина; overflow-wrap:
   anywhere (унаследован от .driver-drum-card) резал длинное слово посередине,
   если класс всё равно не помещался («ОЖИДАН/ИЕ», владелец, 27.09.2026).

   Подгонка — четыре ступени, тот же порядок уступок, что у EquipmentLabelFit
   (equipment-label-fit-v1.js), но с полом покрупнее (карточка барабана видна
   мельком, за рулём, а не разглядывается) и с шириной коробки под название,
   а не всей карточки:
   1) одна строка, кегль от потолка вниз, пока не ляжет по ширине;
   2) если на полу FLOOR_PX не легла — перенос по словам (высота ряда уже
      резервирует 1-2 строки), снова кегль от потолка вниз;
   3) если и на полу с переносом не легла — лёгкое сжатие по ширине (scaleX),
      не глубже MIN_SQUEEZE;
   4) крайняя мера: кегль ниже пола, но подпись остаётся целой — обрезки и
      разрыва слова нет никогда. */
(function (root) {
    "use strict";

    if (!root) return;

    // Слова, для которых название всегда стоит из двух частей: второстепенное
    // сверху мелко, главное — крупно. Список короткий и явный: угадывать по
    // длине слова, как раньше угадывали по длине всей подписи, снова дало бы
    // «пляшущий» кегль на словах, которые сюда не просились.
    var MINOR_LEAD_WORDS = ["ожидание", "ожидания"];
    // Пол для плохого зрения в карьере: название не мельче этого кегля, пока
    // не исчерпаны перенос и сжатие — тогда лучше две строки, чем мелкий текст.
    var FLOOR_PX = 22;
    // Крайняя мера, когда даже перенос и сжатие не помогли (очень длинное слово
    // без пробелов на узкой карточке): целая подпись важнее пола.
    var LAST_RESORT_FONT_PX = 12;
    var MIN_SQUEEZE = 0.85;
    var MIN_LETTER_SPACING_EM = -0.04;
    var FONT_STEP_PX = 0.5;
    // Соседние карточки не должны прыгать по размеру сильнее чем на четверть:
    // если разброс fitAll больше, все карточки текущего набора выравниваются
    // вниз до наибольшего общего (наименьшего среди них) размера.
    var ALIGN_RATIO = 1.25;

    function px(value) {
        var number = parseFloat(value);
        return isFinite(number) ? number : 0;
    }

    function styleOf(element) {
        if (!root.getComputedStyle) return null;
        try {
            return root.getComputedStyle(element);
        } catch (error) {
            return null;
        }
    }

    function clearFitting(element) {
        var style = element.style;
        if (!style) return;
        ["font-size", "letter-spacing", "transform", "transform-origin", "white-space", "height", "width"].forEach(function (name) {
            style.removeProperty(name);
        });
        if (element.classList) {
            element.classList.remove("is-drum-label-wrapped");
            element.classList.remove("is-drum-label-squeezed");
        }
    }

    function setImportant(element, name, value) {
        if (element.style && element.style.setProperty) {
            element.style.setProperty(name, value, "important");
        }
    }

    function fitsOneLine(element) {
        return Number(element.scrollWidth || 0) <= Number(element.clientWidth || 0) + 0.5
            && Number(element.scrollHeight || 0) <= Number(element.clientHeight || 0) + 0.5;
    }

    // С переносом строка может занять всю отведённую высоту (1-2 строки) — тот же
    // тест по обеим осям, но white-space уже normal (перенос разрешён).
    function fitsWrapped(element) {
        return Number(element.scrollWidth || 0) <= Number(element.clientWidth || 0) + 0.5
            && Number(element.scrollHeight || 0) <= Number(element.clientHeight || 0) + 0.5;
    }

    function fitsSqueezed(element, ratio) {
        return Number(element.scrollWidth || 0) * ratio <= Number(element.clientWidth || 0) + 0.5
            && Number(element.scrollHeight || 0) <= Number(element.clientHeight || 0) + 0.5;
    }

    function largestFittingFont(element, low, high, fits) {
        setImportant(element, "font-size", high + "px");
        if (fits(element)) return high;
        while (high - low > FONT_STEP_PX) {
            var middle = (low + high) / 2;
            setImportant(element, "font-size", middle + "px");
            if (fits(element)) low = middle;
            else high = middle;
        }
        setImportant(element, "font-size", low + "px");
        return low;
    }

    /* Бюджет высоты под главное слово: полоса таймера теперь свой отдельный,
       фиксированный ряд грида (.driver-drum-card-body, CSS) — она уже вычтена из
       высоты labelRoot самим грид-лейаутом, здесь остаётся вычесть только
       второстепенное слово «ОЖИДАНИЕ» (если оно есть, свой фиксированный кегль,
       можно замерить ДО подбора шрифта главного слова). */
    function computeMainBudget(labelRoot) {
        var labelHeight = Number(labelRoot.clientHeight || 0);
        if (labelHeight <= 0) return null;

        var labelStyle = styleOf(labelRoot);
        var labelGap = px(labelStyle && (labelStyle.rowGap || labelStyle.gap)) || 2;
        var minorEl = labelRoot.querySelector(".driver-drum-card-label-minor");
        var minorSpace = minorEl ? (Number(minorEl.offsetHeight || 0) + labelGap) : 0;

        var budget = labelHeight - minorSpace;
        return budget > 0 ? budget : null;
    }

    /* Боковая грань барабана повёрнута в 3D (rotateY + перспектива): её реальная
       видимая на экране ширина МЕНЬШЕ, чем плоская (до-transform) ширина, по
       которой раньше только и мерился текст — «ОКИСЛЕННОЙ»/«ОТВАЛ» обрезались
       краем, хотя scrollWidth ≤ clientWidth в собственных (плоских) координатах
       карточки (владелец, 28.09.2026). Плюс сцена барабана маскирует свои
       крайние ~8% по ширине (-webkit-mask, driver-downtime-drum-v1.css) — грань
       у самого края сцены частично гаснет независимо от поворота. Меряем то,
       что реально видно (getBoundingClientRect — уже после transform), пересекаем
       с немаскированной зоной сцены и переводим обратно в плоские координаты
       через коэффициент масштаба самой грани (та же 3D-трансформация действует
       и на карточку, и на текст внутри нее одинаково, так что их отношение —
       чистый common-mode масштаб). Центральную грань (is-center) не трогаем —
       её ширины эта проверка не должна урезать, там результат совпадает с
       обычным clientWidth (владелец: «центральная карточка — как сейчас»). */
    /* НЕ подгоняем боковые грани под их видимую в перспективе ширину. Такая
       попытка была (бюджет от getBoundingClientRect грани с вычетом маски
       сцены): она делала размер текста зависимым от УГЛОВОГО СЛОТА, в котором
       грань стоит в момент подгонки, — после каждой фиксации барабана те же
       «Отвал»/«Склад…» получали то 26px (в центре), то 12px (сбоку, узкая
       проекция), и кегль «прыгал» при каждом повороте, а сама подгонка гонялась
       на каждую фиксацию (владелец, 28.09.2026: «текст постоянно прыгает в
       размере», «во время вращения подписи не пересчитываются вообще»). Грань и
       текст в ней сжимает ОДНА и та же 3D-трансформация — их отношение не
       меняется, из собственной рамки текст не выходит; подгоняем один раз под
       центральный слот, боковые грани несут тот же кегль. Обрезка у самого края
       сцены её маской — отдельный, не текстовый эффект. */

    /* Подгонка одного главного слова. Возвращает {fontPx, deferred} — deferred,
       если у элемента ещё нет реальной ширины/бюджета высоты (карточка скрыта/не
       отрисована): модуль не сдаётся, повторный вызов после появления размеров
       подгонит заново. Бюджет высоты (budgetHeightPx) — временный: он нужен только
       двоичному поиску как потолок, а перед возвратом снимается, чтобы подпись
       вернулась к естественной высоте и группа центрировалась в карточке целиком
       (владелец, 28.09.2026), а не прижималась к верхней кромке своего бюджета. */
    /* Сжатие по ширине на уже выставленном кегле (font-size ставит вызывающий код
       ДО этого вызова): scaleX сам по себе не даёт тексту больше места для
       раскладки (overflow:hidden обрезает контент до применения transform, не
       после — классическая ловушка), поэтому бокс сначала РАСШИРЯЕТСЯ до ширины,
       на которой текст укладывается без переноса, а затем сжимается transform:
       scaleX обратно до исходной видимой ширины. Общий код для основной ступени
       3/4 (fitOne) и для повторного выравнивания в fitAll — там до этого фикса
       выравнивание сбрасывало уже найденное сжатие голым font-size и снова резало
       слово («ПОЛОМК…», выравненная карточка мельче своей естественной, но
       текст всё равно нужно сжимать — владелец, 28.09.2026). */
    function applyWidthSqueeze(element) {
        setImportant(element, "letter-spacing", MIN_LETTER_SPACING_EM + "em");
        var available = Number(element.clientWidth || 0);
        var needed = Math.max(1, Number(element.scrollWidth || available || 1));
        var ratio = Math.max(MIN_SQUEEZE, available / needed);
        if (ratio < 1 && available > 0) {
            setImportant(element, "width", (available / ratio) + "px");
            setImportant(element, "transform", "scaleX(" + ratio.toFixed(3) + ")");
            setImportant(element, "transform-origin", "center");
        }
        if (element.classList) element.classList.add("is-drum-label-squeezed");
        return ratio;
    }

    function fitOne(element, budgetHeightPx) {
        clearFitting(element);
        var style = styleOf(element);
        var maxFont = Math.max(FLOOR_PX, px(style && style.fontSize) || FLOOR_PX);
        if (Number(element.clientWidth || 0) <= 0 || !budgetHeightPx) {
            return {deferred: true, fontPx: null};
        }
        setImportant(element, "height", budgetHeightPx + "px");

        // Ступень 1: одна строка, без переноса.
        setImportant(element, "white-space", "nowrap");
        var fontPx = largestFittingFont(element, LAST_RESORT_FONT_PX, maxFont, fitsOneLine);
        if (fontPx >= FLOOR_PX - FONT_STEP_PX / 2) {
            element.style.removeProperty("height");
            return {deferred: false, fontPx: fontPx};
        }

        // Ступень 2: перенос по словам — доступен, если в тексте вообще есть
        // пробел (иначе переносить нечего, и это уже длинное одно слово).
        var hasSpace = /\s/.test(String(element.textContent || ""));
        if (hasSpace) {
            setImportant(element, "white-space", "normal");
            if (element.classList) element.classList.add("is-drum-label-wrapped");
            fontPx = largestFittingFont(element, LAST_RESORT_FONT_PX, maxFont, fitsWrapped);
            if (fontPx >= FLOOR_PX - FONT_STEP_PX / 2) {
                element.style.removeProperty("height");
                return {deferred: false, fontPx: fontPx};
            }
        }

        // Ступень 3: сжатие по ширине на полу.
        setImportant(element, "font-size", FLOOR_PX + "px");
        applyWidthSqueeze(element);
        if (fitsOneLine(element)) {
            element.style.removeProperty("height");
            return {deferred: false, fontPx: FLOOR_PX};
        }

        // Ступень 4: крайняя мера — ниже пола, но подпись целая. Тот же приём
        // (расширить бокс под текст, сжать transform: scaleX обратно), но ratio
        // пересчитывается на каждом кегле заново — на более мелком шрифте нужно
        // меньше сжатия, старое (более агрессивное) не нужно.
        var low = LAST_RESORT_FONT_PX;
        var high = FLOOR_PX;
        while (high - low > FONT_STEP_PX) {
            var middle = (low + high) / 2;
            element.style.removeProperty("width");
            element.style.removeProperty("transform");
            setImportant(element, "font-size", middle + "px");
            if (fitsOneLine(element)) { low = middle; }
            else { applyWidthSqueeze(element); if (fitsOneLine(element)) low = middle; else high = middle; }
        }
        element.style.removeProperty("width");
        element.style.removeProperty("transform");
        setImportant(element, "font-size", low + "px");
        if (!fitsOneLine(element)) applyWidthSqueeze(element);
        element.style.removeProperty("height");
        return {deferred: false, fontPx: low, belowFloor: true};
    }

    function splitLeadWord(text) {
        var trimmed = String(text || "").trim().replace(/\s+/g, " ");
        var spaceAt = trimmed.indexOf(" ");
        if (spaceAt === -1) return null;
        var lead = trimmed.slice(0, spaceAt).toLowerCase();
        if (MINOR_LEAD_WORDS.indexOf(lead) === -1) return null;
        return { minor: trimmed.slice(0, spaceAt), main: trimmed.slice(spaceAt + 1) };
    }

    // Разметка строится один раз по исходному тексту (сохранённому в
    // data-driver-drum-label-text при первом заходе) и всегда содержит отдельный
    // .driver-drum-card-label-main (второстепенное слово — второй, необязательный
    // ребёнок): единая структура проще в CSS, чем «корень — то контейнер, то сама
    // главная строка» в зависимости от того, разбита подпись или нет.
    function ensureMarkup(labelRoot) {
        if (labelRoot.dataset.driverDrumLabelText === undefined) {
            labelRoot.dataset.driverDrumLabelText = labelRoot.textContent;
        }
        var raw = labelRoot.dataset.driverDrumLabelText;
        var split = splitLeadWord(raw);
        var minorEl = labelRoot.querySelector(".driver-drum-card-label-minor");
        var mainEl = labelRoot.querySelector(".driver-drum-card-label-main");
        if (!mainEl) {
            labelRoot.textContent = "";
            mainEl = labelRoot.ownerDocument.createElement("span");
            mainEl.className = "driver-drum-card-label-main";
            labelRoot.appendChild(mainEl);
        }
        if (split) {
            if (!minorEl) {
                minorEl = labelRoot.ownerDocument.createElement("span");
                minorEl.className = "driver-drum-card-label-minor";
                labelRoot.insertBefore(minorEl, mainEl);
            }
            if (minorEl.textContent !== split.minor) minorEl.textContent = split.minor;
            if (mainEl.textContent !== split.main) mainEl.textContent = split.main;
            if (!labelRoot.classList.contains("is-drum-label-split")) {
                labelRoot.classList.add("is-drum-label-split");
            }
        } else {
            if (minorEl) minorEl.parentNode.removeChild(minorEl);
            if (mainEl.textContent !== raw) mainEl.textContent = raw;
            labelRoot.classList.remove("is-drum-label-split");
        }
        return mainEl;
    }

    /* Повторный вызов с теми же входами (тот же текст, та же карточка того же
       размера, в том же положении — центр/бок) ничего не переписывает в DOM:
       раньше build()/refresh() дёргались на любую относящуюся мутацию (что на
       телефоне бывает часто) и КАЖДЫЙ раз заново гоняли двоичный поиск и
       множественные reflow по всем карточкам — барабан тормозил, а кегль
       «прыгал» без всякой видимой причины (владелец, 28.09.2026: «тормозит,
       текст прыгает»). Подпись берём из входов, которые сама подгонка не
       трогает (ширина/высота КАРТОЧКИ, не подписи — её ширину мы же и пишем). */
    function fitSignature(labelRoot, card) {
        // Только текст и размер КАРТОЧКИ: положение грани (центр/бок) в подпись
        // намеренно не входит — иначе каждая фиксация барабана перекраивала бы
        // кегль (см. комментарий выше).
        return [
            labelRoot.dataset.driverDrumLabelText || "",
            card ? card.clientWidth : 0,
            card ? card.clientHeight : 0
        ].join("|");
    }

    function fit(labelRoot) {
        if (!labelRoot) return null;
        var main = ensureMarkup(labelRoot);
        var card = labelRoot.closest ? labelRoot.closest(".driver-drum-card") : null;
        var sig = fitSignature(labelRoot, card);
        // Кэш засчитываем, только если результат подгонки ФИЗИЧЕСКИ на месте
        // (inline font-size у главного слова). Подмена фрагмента с сервера
        // переносит data-атрибуты (в т.ч. эти метки кэша), а inline-стиль
        // берёт серверный — пустой: метка говорила «уже подогнано», а кегль
        // стоял на сыром CSS-потолке (39px), и длинные названия снова вылезали
        // из карточки после каждого ~20-секундного обновления экрана (поймано
        // на телефоне 28.09.2026 счётчиком: 7 вызовов подгонки без единой записи).
        if (labelRoot.dataset.driverDrumFitSig === sig && labelRoot.dataset.driverDrumFitOk === "1"
            && main.style && main.style.fontSize) {
            return { deferred: false, fontPx: null, cached: true };
        }
        if (labelRoot.style) labelRoot.style.removeProperty("width");
        var budget = computeMainBudget(labelRoot);
        var result = fitOne(main, budget);
        labelRoot.dataset.driverDrumFitSig = sig;
        labelRoot.dataset.driverDrumFitOk = result && !result.deferred ? "1" : "0";
        return result;
    }

    /* Подгоняет каждую подпись контейнера независимо, затем выравнивает набор:
       если разброс между самой мелкой и самой крупной превышает ALIGN_RATIO,
       все карточки текущего вызова пересчитываются заново с потолком, равным
       самой мелкой (соседние карточки не должны прыгать по размеру сильнее чем
       на четверть — владелец, 28.09.2026). Карточки без реальной ширины
       (скрытые, деферред) в выравнивание не входят. */
    function fitAll(container, selector) {
        var nodes = container ? Array.prototype.slice.call(container.querySelectorAll(selector || "[data-driver-drum-label]")) : [];
        var results = nodes.map(fit);
        /* Часть карточек могла не иметь размера в момент вызова (подмена фрагмента
           с сервера: новые узлы уже в DOM, но ещё не разложены — подпись кэша
           «ККД|0|0», ok=0, поймано на телефоне 28.09.2026: после каждого
           ~20-секундного обновления экрана длинные названия стояли на сыром
           CSS-потолке и вылезали из карточки, а повторить подгонку было некому).
           Одна повторная попытка кадром позже — не цикл: если и она отложена,
           дальше ждём следующего настоящего события (появление барабана, ресайз). */
        if (results.some(function (r) { return r && r.deferred; }) && !container.__driverDrumFitRetry && root.requestAnimationFrame) {
            container.__driverDrumFitRetry = true;
            root.requestAnimationFrame(function () {
                container.__driverDrumFitRetry = false;
                fitAll(container, selector);
            });
        }
        var settled = [];
        results.forEach(function (result, index) {
            if (result && !result.deferred && !result.belowFloor) settled.push({node: nodes[index], fontPx: result.fontPx});
        });
        if (settled.length < 2) return;
        var min = settled[0].fontPx, max = settled[0].fontPx;
        settled.forEach(function (entry) {
            if (entry.fontPx < min) min = entry.fontPx;
            if (entry.fontPx > max) max = entry.fontPx;
        });
        if (min <= 0 || max / min <= ALIGN_RATIO) return;
        settled.forEach(function (entry) {
            var main = entry.node.querySelector(".driver-drum-card-label-main") || entry.node;
            clearFitting(main);
            setImportant(main, "white-space", entry.node.classList.contains("is-drum-label-split") || /\s/.test(main.textContent) ? "normal" : "nowrap");
            setImportant(main, "font-size", min + "px");
            // Выровненный вниз кегль — свой для каждой карточки, но слово может
            // остаться ШИРЕ своей карточки при этом кегле (у соседей естественный
            // fit был крупнее не из-за ширины, а из-за высоты/переноса): голый
            // font-size без проверки снова резал слово по краю («ПОЛОМК…», после
            // выравнивания карточка мельче своей естественной, но сжатие уже
            // сброшено clearFitting — владелец, 28.09.2026). Тот же приём сжатия,
            // что и в основной подгонке (fitOne), если после выравнивания влезает.
            if (!(main.style.whiteSpace === "normal" ? fitsWrapped(main) : fitsOneLine(main))) {
                applyWidthSqueeze(main);
            }
        });
    }

    root.DriverDrumLabelFit = Object.freeze({ fit: fit, fitAll: fitAll });
}(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this)));
