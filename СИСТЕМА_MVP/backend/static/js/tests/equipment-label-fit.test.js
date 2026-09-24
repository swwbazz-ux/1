const test = require("node:test");
const assert = require("node:assert/strict");

// Ширина знака относительно кегля на шрифте карточки — замер с живого телефона
// (экскаваторная сессия, 1080x2400, плотность 440): примерно 0.56 кегля на знак.
const CHAR_RATIO = 0.56;

function createLabel(options) {
    const text = options.text;
    const width = options.width;
    const height = options.height;
    const cssFont = options.cssFont || 31.4;
    const lineHeightFactor = options.lineHeightFactor || 1.05;
    const declarations = new Map();

    function currentFont() {
        const own = declarations.get("font-size");
        return own ? parseFloat(own) : cssFont;
    }

    function letterSpacing() {
        const own = declarations.get("letter-spacing");
        return own ? parseFloat(own) * currentFont() : 0;
    }

    function widthOf(piece) {
        return piece.length * currentFont() * CHAR_RATIO + Math.max(0, piece.length - 1) * letterSpacing();
    }

    function lines() {
        if (declarations.get("white-space") !== "normal") return [text];
        // Перенос разрешён только после дефиса и по пробелу — внутри слова нет.
        const pieces = text.split(/(?<=[-\s])/);
        const out = [];
        let current = "";
        for (const piece of pieces) {
            const candidate = current + piece;
            if (current && widthOf(candidate.trim()) > width) {
                out.push(current.trim());
                current = piece;
            } else {
                current = candidate;
            }
        }
        if (current) out.push(current.trim());
        return out;
    }

    const element = {
        textContent: text,
        clientWidth: width,
        clientHeight: height,
        classList: {
            names: new Set(),
            add(name) { this.names.add(name); },
            remove(name) { this.names.delete(name); },
            contains(name) { return this.names.has(name); },
        },
        style: {
            setProperty(name, value) { declarations.set(name, value); },
            removeProperty(name) { declarations.delete(name); },
            getPropertyValue(name) { return declarations.get(name) || ""; },
        },
        get scrollWidth() {
            return Math.max(...lines().map(widthOf));
        },
        get scrollHeight() {
            return lines().length * currentFont() * lineHeightFactor;
        },
        declarations,
        currentFont,
        lines,
    };
    if (options.card) {
        // Коробка подписи у водителя растёт под содержимое: сама подпись
        // всегда «ровно по себе», а запас высоты лежит в карточке.
        Object.defineProperty(element, "clientHeight", {
            get() { return element.scrollHeight; },
        });
        element.parentElement = {
            clientHeight: options.card,
            get scrollHeight() { return options.cardUsed + element.scrollHeight; },
        };
    }
    return element;
}

function withFakeDom(run) {
    const previous = globalThis.getComputedStyle;
    globalThis.getComputedStyle = (element) => ({
        fontSize: element.currentFont() + "px",
        lineHeight: element.currentFont() * 1.05 + "px",
    });
    try {
        return run();
    } finally {
        if (previous) globalThis.getComputedStyle = previous;
        else delete globalThis.getComputedStyle;
    }
}

const fitModule = require("../equipment-label-fit-v1.js");

test("короткая подпись остаётся крупной и не трогается", () => {
    withFakeDom(() => {
        const label = createLabel({text: "ЭКС-1", width: 102, height: 28});
        const result = fitModule.fit(label);
        assert.equal(result.wrapped, false);
        assert.equal(result.squeezed, 1);
        assert.ok(result.fontPx > 30, "кегль остался крупным: " + result.fontPx);
        assert.ok(label.scrollWidth <= 102.5, "подпись помещается по ширине");
    });
});

test("подпись подлиннее ужимается кеглем, но остаётся читаемой", () => {
    withFakeDom(() => {
        const label = createLabel({text: "ЭКС-99", width: 102, height: 28});
        const result = fitModule.fit(label);
        assert.equal(result.wrapped, false);
        assert.ok(result.fontPx > fitModule.MIN_FONT_PX, "до пола дело не дошло: " + result.fontPx);
        assert.ok(label.scrollWidth <= 102.5, "подпись помещается по ширине");
    });
});

test("длинная подпись переносится по дефису, когда есть вторая строка", () => {
    withFakeDom(() => {
        const label = createLabel({text: "ЭКСКАВАТОР-123", width: 102, height: 64});
        const result = fitModule.fit(label);
        assert.equal(result.wrapped, true);
        assert.equal(result.squeezed, 1);
        const lines = label.lines();
        assert.equal(lines.length, 2);
        assert.equal(lines[0], "ЭКСКАВАТОР-", "разрыв только после дефиса");
        assert.equal(lines[1], "123");
        assert.ok(label.scrollWidth <= 102.5);
        assert.ok(label.scrollHeight <= 64.5);
    });
});

test("на тесной карточке переноса нет — подпись сжимается, но не обрезается", () => {
    withFakeDom(() => {
        const label = createLabel({text: "САМОСВАЛ-1234567", width: 102, height: 24});
        const result = fitModule.fit(label);
        assert.equal(result.wrapped, false, "второй строке негде встать");
        assert.ok(result.squeezed < 1, "подпись сжата по горизонтали");
        assert.ok(result.squeezed >= fitModule.MIN_SQUEEZE, "сжатие не переходит предел читаемости");
        assert.ok(result.fontPx <= fitModule.MIN_FONT_PX, "кегль опущен до пола или ниже: " + result.fontPx);
        assert.equal(label.declarations.get("overflow"), undefined, "обрезки не появилось");
    });
});

test("у скрытой карточки подгонка откладывается, а не пропадает", () => {
    withFakeDom(() => {
        const label = createLabel({text: "ЭКС-99", width: 0, height: 0});
        const result = fitModule.fit(label);
        assert.equal(result.deferred, true);
        assert.equal(label.declarations.get("font-size"), undefined, "кегль не выставлен вслепую");

        label.clientWidth = 102;
        label.clientHeight = 28;
        const second = fitModule.fit(label);
        assert.equal(second.deferred, false);
        assert.ok(second.fontPx > fitModule.MIN_FONT_PX);
    });
});

test("на растущей строке перенос берётся из запаса высоты карточки", () => {
    withFakeDom(() => {
        // Строка под номер у водителя не фиксирована: подпись всегда ровно
        // своей высоты, поэтому «влезет ли вторая строка» видно только по
        // карточке. Без этого длинная подпись зря уходила бы в сжатие.
        const label = createLabel({
            text: "ЭКСКАВАТОР-123",
            width: 102,
            height: 0,
            card: 90,
            cardUsed: 26,
        });
        const result = fitModule.fit(label);
        assert.equal(result.wrapped, true, "вторая строка нашлась в карточке");
        assert.equal(result.squeezed, 1, "до сжатия дело не дошло");
        assert.deepEqual(label.lines(), ["ЭКСКАВАТОР-", "123"]);
    });
});

test("номер техники не переносится, даже когда высота позволяет", () => {
    withFakeDom(() => {
        // Правило пользователя: номер техники всегда одной строкой. Пробел
        // внутри номера («ТМС 528», «Тест 1») переносом не считается, иначе
        // номер читается как два разных.
        const label = createLabel({text: "САМОСВАЛ 1234567", width: 102, height: 64});
        const result = fitModule.fit(label, {allowWrap: false});
        assert.equal(result.wrapped, false, "перенос запрещён явно");
        assert.deepEqual(label.lines(), ["САМОСВАЛ 1234567"], "подпись осталась одной строкой");

        const same = createLabel({text: "САМОСВАЛ 1234567", width: 102, height: 64});
        assert.equal(fitModule.fit(same).wrapped, true, "по умолчанию перенос остался доступен");
    });
});

test("когда уступок не осталось, подпись мельчает, но не режется", () => {
    withFakeDom(() => {
        // Восемнадцать знаков в 104 px: даже на полу и с предельным сжатием
        // подпись шире карточки. Обрезать нельзя — уходим ниже пола.
        const label = createLabel({text: "ЭКСКАВАТОР-1234567", width: 104, height: 28});
        const result = fitModule.fit(label, {allowWrap: false});
        assert.equal(result.belowFloor, true, "пришлось опуститься ниже пола");
        assert.ok(result.fontPx >= fitModule.LAST_RESORT_FONT_PX, "но не ниже крайней меры");
        assert.ok(label.scrollWidth <= 104.5, "подпись целиком помещается: " + label.scrollWidth);
    });
});
