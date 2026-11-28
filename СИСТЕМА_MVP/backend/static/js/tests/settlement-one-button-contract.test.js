"use strict";
/* T-036 W3: контракт блока «одна кнопка» авторасселения в панели
   делопроизводителя. Проверяем по исходникам:
   - разметка блока живёт в существующей панели «Следующий заезд», карта и
     комнаты не менялись;
   - маршруты берутся из уже существующих data-атрибутов корня, новых
     endpoints нет;
   - кнопка блокируется и показывает спиннер с прошедшим временем;
   - исключения показывают причину и rule_ids, пропуски различают
     in_plan=true (красным) и in_plan=false (серым);
   - применение идёт только после подтверждения, день и ночь последовательно,
     конфликт одной смены не отменяет вторую;
   - settlement-clerk.js и _room_card.html не тронуты. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const read = (relative) => fs.readFileSync(path.resolve(__dirname, relative), "utf8").replace(/\r\n/g, "\n");
const TEMPLATE = read("../../../templates/settlement/clerk_map.html");
const ROOM_CARD = read("../../../templates/settlement/_room_card.html");
const CSS = read("../../css/settlement-one-button-v1.css");
const JS = read("../settlement-one-button-v1.js");
const MAP_JS = read("../settlement-clerk.js");

test("block sits inside the existing next-arrival panel and keeps map/rooms untouched", () => {
    const panel = TEMPLATE.slice(
        TEMPLATE.indexOf('data-map-mode-panel="next"'),
        TEMPLATE.indexOf('data-plan-status')
    );
    assert.ok(panel.length > 0, "next-arrival panel is present");
    assert.match(panel, /data-settlement-one-button/);
    assert.match(panel, /<h2 id="settlement-one-button-title">Авторасселение вахты<\/h2>/);
    assert.match(panel, /data-one-button-calculate>Рассчитать расселение</);
    assert.equal((panel.match(/data-one-button-calculate/g) || []).length, 1);
    assert.equal((panel.match(/settlement-one-button-primary/g) || []).length, 1);
    assert.match(panel, /data-one-button-confirm hidden>Подтвердить</);
    assert.match(panel, /data-one-button-apply hidden>Применить</);
    // Карта текущей вахты и карточка комнаты не получили новых атрибутов.
    assert.doesNotMatch(ROOM_CARD, /one-button|one_button/);
    assert.doesNotMatch(MAP_JS, /one-button|oneButton/);
    const currentPanel = TEMPLATE.slice(
        TEMPLATE.indexOf('data-map-mode-panel="current"'),
        TEMPLATE.indexOf('data-map-mode-panel="next"')
    );
    assert.ok(currentPanel.length > 0, "current-watch panel is present");
    assert.doesNotMatch(currentPanel, /data-one-button|data-settlement-one-button/);
});

test("no new endpoints: routes come from existing root data attributes", () => {
    ["autoStateUrl", "autoCreateUrl", "autoConfirmUrl", "autoApplyDayUrl", "autoApplyNightUrl"].forEach((key) => {
        assert.match(JS, new RegExp("root\\.dataset\\." + key));
    });
    [
        "data-auto-state-url=\"{% url 'settlement_auto_state' %}\"",
        "data-auto-create-url=\"{% url 'settlement_auto_preview_create' %}\"",
        "data-auto-confirm-url=\"{% url 'settlement_auto_preview_confirm' %}\"",
        "data-auto-apply-day-url=\"{% url 'settlement_auto_preview_apply_day' %}\"",
        "data-auto-apply-night-url=\"{% url 'settlement_auto_preview_apply_night' %}\""
    ].forEach((attribute) => {
        assert.ok(TEMPLATE.includes(attribute), attribute);
    });
    assert.doesNotMatch(JS, /\/clerk\/settlement\/auto\//);
    assert.doesNotMatch(JS, /fetch\("\/|fetch\('\/|fetch\("http/);
});

test("calculate blocks the button and shows a spinner with elapsed time", () => {
    assert.match(JS, /if \(calculateButton\) calculateButton\.disabled = !allowed;/);
    assert.match(JS, /state\.busy = true;/);
    assert.match(JS, /function startProgress\(label\)/);
    assert.match(JS, /state\.timer = window\.setInterval\(/);
    assert.match(JS, /progressElapsed\.textContent = seconds \+ " с";/);
    assert.match(TEMPLATE, /class="settlement-one-button-spinner" aria-hidden="true"/);
    assert.match(CSS, /\.settlement-one-button-spinner \{/);
    assert.match(CSS, /@keyframes settlement-one-button-spin/);
    // Прогресса в auto_state нет: проценты не выдумываются.
    assert.doesNotMatch(JS, /progress\.percent|percent/);
});

test("summary shows placed, not placed and conflicts", () => {
    assert.match(JS, /"Размещено автоматически", preview\.placement_count/);
    assert.match(JS, /"Не размещено",[\s\S]{0,80}preview\.unresolved_count/);
    assert.match(JS, /"Конфликты"/);
    // review-7 note 1: сводка суммирует skipped_count применённых смен,
    // preview.application остаётся fallback'ом для старых ответов.
    assert.match(JS, /function conflictCount\(preview\)/);
    assert.match(JS, /typeof row\.skipped_count === "number"/);
    assert.match(JS, /return Number\(preview\.application\.skipped_count \|\| 0\);/);
});

test("exceptions show a human reason, rule ids and are never hidden", () => {
    assert.match(JS, /function exceptionRow\(row\)/);
    assert.match(JS, /row\.reason \|\| "Требуется уточнение данных\."/);
    assert.match(JS, /function ruleChips\(ruleIds\)/);
    assert.match(JS, /chip\.title = full;/);
    assert.match(JS, /var unresolved = \(preview && preview\.unresolved\) \|\| \[\];/);
    assert.match(JS, /unresolved\.map\(exceptionRow\)/);
    // Никакого обрезания списка: рендерим все строки.
    assert.doesNotMatch(JS, /\.slice\(0,\s*\d+\)/);
});

test("skipped rows distinguish in_plan=true (red) and in_plan=false (grey)", () => {
    assert.match(JS, /inPlan === true/);
    assert.match(JS, /"Плановая строка не применена"/);
    assert.match(JS, /inPlan === false/);
    assert.match(JS, /"Внеплановая занятость"/);
    assert.match(JS, /is-plan-skip/);
    assert.match(JS, /is-out-of-plan-skip/);
    assert.match(JS, /error\.details && error\.details\.skipped_rows/);
    assert.match(JS, /server\.status === "applied" && Array\.isArray\(server\.skipped\)/);
    assert.match(JS, /function allAppliedShiftsHaveSkipped\(preview\)/);
    assert.match(JS, /function unlabelledSkippedRows\(preview\)/);
    assert.match(JS, /"Пропущено при применении: " \+ unlabelledSkips\.length/);
    assert.match(CSS, /\.settlement-one-button-skip\.is-plan-skip \{/);
    assert.match(CSS, /\.settlement-one-button-skip\.is-plan-skip \.settlement-one-button-skip-state \{\s+color: var\(--admin-red\);/);
    assert.match(CSS, /\.settlement-one-button-skip\.is-out-of-plan-skip \{/);
});

test("apply requires an explicit confirmation and a confirmed preview", () => {
    assert.match(JS, /if \(preview\.status !== "confirmed"\) \{/);
    assert.match(JS, /if \(!confirmApplyDialog\(preview\)\) return;/);
    assert.match(JS, /function confirmApplyDialog\(preview\)/);
    assert.match(JS, /return window\.confirm\(lines\.join\("\\n"\)\);/);
    assert.match(JS, /window\.confirm\("Подтвердить расчёт\?/);
    // Расчёт не применяет план сам.
    const calculateBody = JS.match(/function calculate\(\) \{[\s\S]*?\n    \}\n/)[0];
    assert.doesNotMatch(calculateBody, /autoApply/);
    assert.doesNotMatch(calculateBody, /applyRequest/);
    assert.match(JS, /if \(applyButton\) applyButton\.addEventListener\("click", applyAll\);/);
});

test("day and night are applied sequentially and independently", () => {
    assert.match(JS, /var SHIFTS = \["day", "night"\];/);
    assert.match(JS, /SHIFTS\.forEach\(function \(shift\) \{\s+chain = chain\.then/);
    assert.match(JS, /root\.dataset\.autoApplyNightUrl/);
    assert.match(JS, /root\.dataset\.autoApplyDayUrl/);
    // Ошибка одной смены не прерывает цепочку: catch возвращает результат.
    const catchBlock = JS.match(/result\.status = "error";[\s\S]{0,520}?return result;/)[0];
    assert.match(catchBlock, /result\.error = errorMessage\(error\);/);
    assert.match(JS, /"Применение завершено с конфликтами: "/);
    assert.match(JS, /counters\.appendChild\(el\("span", "", "Пропущено: " \+ skipped\.rows\.length\)\);/);
});

test("errors 403/409/500 are explained in text, never silent", () => {
    assert.match(JS, /if \(status === 403\) \{/);
    assert.match(JS, /if \(status === 409\) \{/);
    assert.match(JS, /if \(status >= 500\) \{/);
    assert.match(JS, /"Нет доступа к авторасселению \(403\)\. "/);
    assert.match(JS, /"Сервер не смог выполнить действие \(ошибка " \+ status/);
    assert.match(JS, /code === "not_held"/);
    assert.match(JS, /code === "session_mismatch"/);
    assert.match(JS, /showFeedback\(errorMessage\(error\), "error"\);/);
    assert.match(JS, /manual_replacement_confirmation_required/);
});

test("no innerHTML and no inline style juggling", () => {
    assert.doesNotMatch(JS, /innerHTML/);
    assert.doesNotMatch(JS, /style\.display/);
    assert.doesNotMatch(JS, /insertAdjacentHTML/);
});

test("new assets carry their own version token and load last", () => {
    assert.match(TEMPLATE, /css\/settlement-one-button-v1\.css[\s\S]{0,90}-settlement-one-button-v1/);
    assert.match(TEMPLATE, /js\/settlement-one-button-v1\.js[\s\S]{0,90}-settlement-one-button-v1/);
    assert.ok(
        TEMPLATE.indexOf("js/settlement-one-button-v1.js") > TEMPLATE.indexOf("js/settlement-external-residents.js"),
        "one-button script loads after the existing panel scripts"
    );
    // T-036 frontend-10: карта перешла на v39 из-за категории рабочего/ИТР.
    assert.equal(TEMPLATE.split("settlement-map-v39").length - 1, 4);
    assert.equal(TEMPLATE.split("settlement-map-v37").length - 1, 0);
});

/* --- Исполнение в минимальной DOM-заглушке -------------------------------
   T-036 frontend-2: проверяем не текст исходника, а отрисовку пропусков по
   сменам. Ответ backend-12 отдаёт shift_apply[shift].skipped и skipped_count;
   старый ответ без поля должен по-прежнему работать. */

function makeNode(tag) {
    const node = {
        tagName: String(tag || "div").toUpperCase(),
        className: "",
        textContent: "",
        title: "",
        value: "",
        hidden: false,
        dataset: {},
        children: [],
        attributes: {},
        listeners: {},
        appendChild(child) {
            this.children.push(child);
            return child;
        },
        replaceChildren() {
            this.children = [];
        },
        setAttribute(name, value) {
            this.attributes[name] = value;
        },
        addEventListener(name, handler) {
            this.listeners[name] = handler;
        }
    };
    const classes = new Set();
    node.classList = {
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
        toggle: (name, force) => (force ? classes.add(name) : classes.delete(name))
    };
    return node;
}

function textOfNode(node) {
    const parts = [];
    (function walk(current) {
        if (!current) return;
        if (current.textContent) parts.push(String(current.textContent));
        (current.children || []).forEach(walk);
    }(node));
    return parts.join(" | ");
}

const RUNTIME_SELECTORS = [
    "[data-settlement-root]",
    "[data-settlement-one-button]",
    "[data-one-button-feedback]",
    "[data-one-button-empty]",
    "[data-one-button-lease-hint]",
    "[data-one-button-cohort-row]",
    "[data-one-button-cohort]",
    "[data-one-button-cohort-copy]",
    "[data-one-button-progress]",
    "[data-one-button-progress-label]",
    "[data-one-button-progress-elapsed]",
    "[data-one-button-summary]",
    "[data-one-button-exceptions]",
    "[data-one-button-shifts]",
    "[data-one-button-calculate]",
    "[data-one-button-confirm]",
    "[data-one-button-apply]",
    "[data-one-button-refresh]"
];

function mountOneButton(payload) {
    const nodes = {};
    RUNTIME_SELECTORS.forEach((selector) => {
        nodes[selector] = makeNode("div");
    });
    const root = nodes["[data-settlement-root]"];
    root.dataset.controlState = "held";
    root.dataset.autoStateUrl = "/clerk/settlement/auto/state/";
    root.dataset.autoCreateUrl = "/clerk/settlement/auto/preview/create/";
    root.dataset.autoConfirmUrl = "/clerk/settlement/auto/preview/confirm/";
    root.dataset.autoApplyDayUrl = "/clerk/settlement/auto/preview/apply/day/";
    root.dataset.autoApplyNightUrl = "/clerk/settlement/auto/preview/apply/night/";
    const block = nodes["[data-settlement-one-button]"];
    const lookup = (selector) => nodes[selector] || null;
    root.querySelector = (selector) => (
        selector === "input[name='csrfmiddlewaretoken']"
            ? { value: "csrf-token" }
            : lookup(selector)
    );
    block.querySelector = lookup;

    const sandbox = {
        console,
        Promise,
        Date,
        Number,
        String,
        Boolean,
        Array,
        Object,
        Math,
        JSON,
        Set,
        Error,
        document: {
            cookie: "",
            querySelector: (selector) => (
                selector === "[data-settlement-root]" ? root : null
            ),
            createElement: (tag) => makeNode(tag)
        },
        window: {
            setInterval: () => 1,
            clearInterval: () => undefined,
            confirm: () => true,
            MutationObserver: undefined
        },
        Option: function Option(text, value, defaultSelected, selected) {
            const option = makeNode("option");
            option.textContent = String(text);
            option.value = String(value);
            option.selected = Boolean(selected);
            return option;
        },
        fetch: () => Promise.resolve({
            ok: true,
            status: 200,
            redirected: false,
            json: () => Promise.resolve(payload)
        })
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../settlement-personnel-number.js"), "utf8"), sandbox);
    vm.runInNewContext(JS, sandbox, { filename: "settlement-one-button-v1.js" });
    return nodes;
}

async function settleRuntime() {
    for (let index = 0; index < 6; index += 1) {
        await new Promise((resolve) => setImmediate(resolve));
    }
}

const RUNTIME_WATCH = {
    id: 2,
    name: "Вахта 20/20",
    starts_on: "2026-11-20",
    ends_on: "2026-12-10"
};

function runtimePreview(overrides) {
    return Object.assign({
        id: 11,
        version: 4,
        status: "confirmed",
        stale: false,
        member_count: 250,
        placement_count: 115,
        unresolved_count: 0,
        created_at: "2026-11-16T10:00:00Z",
        created_by: "Иванова И.И.",
        unresolved: [],
        application: null,
        shift_apply: {
            day: {
                status: "not_ready",
                allowed_date: "2026-11-20",
                applied_at: null,
                counts: null,
                skipped_count: 0,
                skipped: []
            },
            night: {
                status: "not_ready",
                allowed_date: "2026-11-19",
                applied_at: null,
                counts: null,
                skipped_count: 0,
                skipped: []
            }
        },
        effective_plan: null
    }, overrides || {});
}

function runtimeState(preview) {
    return {
        ok: true,
        state: "ready",
        selected_cohort_id: 7,
        cohorts: [{
            id: 7,
            version: 3,
            member_count: 250,
            watch_period: RUNTIME_WATCH
        }],
        preview
    };
}

const DAY_SKIP = {
    resident_id: 5,
    resident_name: "Кузнецов К.К.",
    bed_stable_id: "KIS5-F1-R01-A1",
    in_plan: true,
    reason_codes: ["bed_prepared_hold"],
    rule_ids: ["SET-R095"],
    message: "Койка придержана подготовкой за другим жильцом."
};

const NIGHT_SKIP = {
    resident_id: 6,
    resident_name: "Морозов М.М.",
    bed_stable_id: "",
    in_plan: false,
    reason_codes: ["hard_rule_conflict"],
    rule_ids: ["SET-R033", "SET-R034"],
    message: "Пересечение с посторонней занятостью."
};

test("runtime: each applied shift shows its own shift_apply.skipped", async () => {
    const nodes = mountOneButton(runtimeState(runtimePreview({
        application: {
            applied_at: "2026-11-16T11:00:00Z",
            skipped_count: 1,
            skipped: [DAY_SKIP]
        },
        shift_apply: {
            day: {
                status: "applied",
                allowed_date: "2026-11-20",
                applied_at: "2026-11-16T11:00:00Z",
                counts: { created: 69, reused: 0, replaced_auto: 0, replaced_manual: 0, unresolved: 0 },
                skipped_count: 1,
                skipped: [DAY_SKIP]
            },
            night: {
                status: "applied",
                allowed_date: "2026-11-19",
                applied_at: "2026-11-16T11:05:00Z",
                counts: { created: 46, reused: 0, replaced_auto: 0, replaced_manual: 0, unresolved: 0 },
                skipped_count: 1,
                skipped: [NIGHT_SKIP]
            }
        }
    })));
    await settleRuntime();
    const shifts = nodes["[data-one-button-shifts]"];
    assert.equal(shifts.children.length, 2, "две карточки смен");
    const dayText = textOfNode(shifts.children[0]);
    const nightText = textOfNode(shifts.children[1]);
    assert.match(dayText, /Дневная смена/);
    assert.match(dayText, /Кузнецов К\.К\./);
    assert.match(dayText, /Плановая строка не применена/);
    assert.match(dayText, /R095/);
    assert.match(dayText, /Пропущено: 1/);
    assert.doesNotMatch(dayText, /Морозов/);
    assert.match(nightText, /Ночная смена/);
    assert.match(nightText, /Морозов М\.М\./);
    assert.match(nightText, /Внеплановая занятость/);
    assert.match(nightText, /R033/);
    assert.match(nightText, /Пропущено: 1/);
    assert.doesNotMatch(nightText, /Кузнецов/);
    // Свои пропуски у обеих смен — отдельный блок без разбивки не нужен.
    assert.doesNotMatch(
        textOfNode(nodes["[data-one-button-exceptions]"]),
        /Пропущено при применении/
    );
});

test("runtime: response without shift skipped falls back to preview.application", async () => {
    /* Старый ответ: shift_apply[shift].skipped нет, применены обе смены,
       preview.application описывает только первую — второй смене пропуски
       не приписываются, строки выводятся отдельным блоком. */
    const nodes = mountOneButton(runtimeState(runtimePreview({
        application: {
            applied_at: "2026-11-16T11:00:00Z",
            skipped_count: 1,
            skipped: [DAY_SKIP]
        },
        shift_apply: {
            day: {
                status: "applied",
                allowed_date: "2026-11-20",
                applied_at: "2026-11-16T11:00:00Z",
                counts: { created: 69, reused: 0, replaced_auto: 0, replaced_manual: 0, unresolved: 0 }
            },
            night: {
                status: "applied",
                allowed_date: "2026-11-19",
                applied_at: "2026-11-16T11:05:00Z",
                counts: { created: 46, reused: 0, replaced_auto: 0, replaced_manual: 0, unresolved: 0 }
            }
        }
    })));
    await settleRuntime();
    const shifts = nodes["[data-one-button-shifts]"];
    assert.match(textOfNode(shifts.children[0]), /Пропущено: сервер не отдал список отдельно/);
    assert.match(textOfNode(shifts.children[1]), /Пропущено: сервер не отдал список отдельно/);
    const exceptionsText = textOfNode(nodes["[data-one-button-exceptions]"]);
    assert.match(exceptionsText, /Пропущено при применении: 1/);
    assert.match(exceptionsText, /Плановая строка не применена/);
    assert.match(exceptionsText, /R095/);
});

test("runtime: single applied shift keeps the old preview.application fallback", async () => {
    const nodes = mountOneButton(runtimeState(runtimePreview({
        application: {
            applied_at: "2026-11-16T11:05:00Z",
            skipped_count: 1,
            skipped: [NIGHT_SKIP]
        },
        shift_apply: {
            day: {
                status: "not_ready",
                allowed_date: "2026-11-20",
                applied_at: null,
                counts: null
            },
            night: {
                status: "applied",
                allowed_date: "2026-11-19",
                applied_at: "2026-11-16T11:05:00Z",
                counts: { created: 46, reused: 0, replaced_auto: 0, replaced_manual: 0, unresolved: 0 }
            }
        }
    })));
    await settleRuntime();
    const shifts = nodes["[data-one-button-shifts]"];
    const nightText = textOfNode(shifts.children[1]);
    assert.match(nightText, /Морозов М\.М\./);
    assert.match(nightText, /Внеплановая занятость/);
    assert.match(nightText, /Пропущено: 1/);
    assert.doesNotMatch(
        textOfNode(nodes["[data-one-button-exceptions]"]),
        /Пропущено при применении/
    );
});

/* Значение карточки сводки: article → [span(подпись), strong(число)]. */
function summaryValue(nodes, index) {
    const card = nodes["[data-one-button-summary]"].children[index];
    assert.ok(card, "карточка сводки №" + index + " отсутствует");
    return String(card.children[1].textContent);
}

function skipRows(prefix, count, inPlan) {
    const rows = [];
    for (let index = 0; index < count; index += 1) {
        rows.push({
            resident_id: 100 + index,
            resident_name: prefix + " " + (index + 1),
            bed_stable_id: "",
            in_plan: inPlan,
            reason_codes: ["hard_rule_conflict"],
            rule_ids: ["SET-R033"],
            message: "Конфликт " + (index + 1)
        });
    }
    return rows;
}

test("runtime: summary conflicts sum shift skipped_count, old payload keeps fallback", async () => {
    /* review-7 note 1: день 2 пропуска, ночь 3 → сводка 5, а не 2
       (preview.application описывает только первое применение). */
    const daySkipped = skipRows("День", 2, true);
    const nightSkipped = skipRows("Ночь", 3, false);
    const summed = mountOneButton(runtimeState(runtimePreview({
        application: {
            applied_at: "2026-11-16T11:00:00Z",
            skipped_count: 2,
            skipped: daySkipped
        },
        shift_apply: {
            day: {
                status: "applied",
                allowed_date: "2026-11-20",
                applied_at: "2026-11-16T11:00:00Z",
                counts: { created: 69, reused: 0, replaced_auto: 0, replaced_manual: 0, unresolved: 0 },
                skipped_count: 2,
                skipped: daySkipped
            },
            night: {
                status: "applied",
                allowed_date: "2026-11-19",
                applied_at: "2026-11-16T11:05:00Z",
                counts: { created: 46, reused: 0, replaced_auto: 0, replaced_manual: 0, unresolved: 0 },
                skipped_count: 3,
                skipped: nightSkipped
            }
        }
    })));
    await settleRuntime();
    assert.equal(summaryValue(summed, 2), "5", "сводка должна суммировать пропуски смен, а не брать первую");
    // Карточки смен не сломаны: у каждой своё число.
    const summedShifts = summed["[data-one-button-shifts]"];
    assert.match(textOfNode(summedShifts.children[0]), /Пропущено: 2/);
    assert.match(textOfNode(summedShifts.children[1]), /Пропущено: 3/);

    /* Старый ответ: shift_apply отсутствует — прежнее поведение по
       preview.application. */
    const oldPayload = runtimePreview({
        application: {
            applied_at: "2026-11-16T11:00:00Z",
            skipped_count: 4,
            skipped: skipRows("Старый", 4, true)
        }
    });
    delete oldPayload.shift_apply;
    const legacy = mountOneButton(runtimeState(oldPayload));
    await settleRuntime();
    assert.equal(summaryValue(legacy, 2), "4", "старый ответ должен использовать preview.application.skipped_count");

    /* Историческое применение всего плана отдаётся обеим сменам одной
       записью (одинаковый applied_at) — сумма не должна удвоиться. */
    const legacyRun = mountOneButton(runtimeState(runtimePreview({
        application: {
            applied_at: "2026-11-01T09:00:00Z",
            skipped_count: 4,
            skipped: skipRows("Историческое", 4, true)
        },
        shift_apply: {
            day: {
                status: "applied",
                allowed_date: "2026-11-20",
                applied_at: "2026-11-01T09:00:00Z",
                counts: null,
                skipped_count: 4,
                skipped: skipRows("Историческое", 4, true)
            },
            night: {
                status: "applied",
                allowed_date: "2026-11-19",
                applied_at: "2026-11-01T09:00:00Z",
                counts: null,
                skipped_count: 4,
                skipped: skipRows("Историческое", 4, true)
            }
        }
    })));
    await settleRuntime();
    assert.equal(summaryValue(legacyRun, 2), "4", "одно применение на две смены не должно считаться дважды");
});
