"use strict";
/* T-029 B2: подготовка места, временное проживание, резерв и административный
   перехват управления на карте расселения. Проверяем контракт шаблона, нового
   скрипта и стилей по исходникам: серверные маршруты берутся из data-атрибутов
   корня, бейджи на койках и разметку списка создаёт только JS, `_room_card.html`
   не меняется, состояние «Свободно/Занято» (`data-bed-state`) не переписывается,
   причина уходит на сервер вместе с каждым действием. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const read = (relative) => fs.readFileSync(path.resolve(__dirname, relative), "utf8").replace(/\r\n/g, "\n");
const TEMPLATE = read("../../../templates/settlement/clerk_map.html");
const ROOM_CARD = read("../../../templates/settlement/_room_card.html");
const CSS = read("../../css/settlement-clerk.css");
const JS = read("../settlement-temporary-placement.js");

const URL_ATTRIBUTE_NAMES = [
    "temporary-state-url",
    "resident-search-url",
    "bed-preparation-prepare-url",
    "bed-preparation-suggest-url",
    "bed-preparation-confirm-url",
    "bed-preparation-cancel-url",
    "bed-preparation-extend-url",
    "temporary-relocate-url",
    "temporary-release-url",
    "bed-reserve-url",
    "control-takeover-url"
];

test("all server routes come from data attributes of the settlement root", () => {
    URL_ATTRIBUTE_NAMES.forEach((name) => {
        assert.match(
            TEMPLATE,
            new RegExp(`data-${name}="\\{% url 'settlement_[a-z_]+'`),
            name
        );
    });
    assert.match(TEMPLATE, /data-current-date="\{\{ current_date\|date:'Y-m-d' \}\}"/);
});

test("the preparation, temporary and takeover routes are wired in the script", () => {
    [
        "temporaryStateUrl",
        "residentSearchUrl",
        "bedPreparationPrepareUrl",
        "bedPreparationSuggestUrl",
        "bedPreparationConfirmUrl",
        "bedPreparationCancelUrl",
        "bedPreparationExtendUrl",
        "temporaryRelocateUrl",
        "temporaryReleaseUrl",
        "bedReserveUrl",
        "controlTakeoverUrl"
    ].forEach((key) => {
        assert.match(JS, new RegExp(`root\\.dataset\\.${key}`), key);
    });
    assert.match(JS, /function bedUrl\(template, identifier\)/);
    assert.match(JS, /replace\("\/0\/", "\/" \+ identifier \+ "\/"\)/);
});

test("bed badge and state markings are created by the script only", () => {
    assert.doesNotMatch(ROOM_CARD, /settlement-bed-tag/);
    assert.doesNotMatch(ROOM_CARD, /data-bed-prepared|data-bed-reserve|data-bed-temporary/);
    assert.doesNotMatch(ROOM_CARD, /is-prepared|is-reserve|is-temporary/);
    assert.match(JS, /\["Рез\.", "is-reserve"\]/);
    assert.match(JS, /\["Подг\.", "is-prepared"\]/);
    assert.match(JS, /\["Врем\.", "is-temporary"\]/);
    assert.match(JS, /bed\.dataset\.bedPrepared =/);
    assert.match(JS, /bed\.dataset\.bedReserve =/);
    assert.match(JS, /bed\.dataset\.bedTemporary =/);
    // «Свободно/Занято» остаётся за картой: состояние койки скрипт не пишет.
    assert.doesNotMatch(JS, /dataset\.bedState/);
});

test("preparation form asks for a resident, an arrival date and a required basis", () => {
    [
        "data-bed-preparation-open",
        "data-bed-preparation-form",
        "data-bed-preparation-resident-search",
        "data-bed-preparation-suggest",
        "data-bed-preparation-arrival",
        "data-bed-preparation-basis",
        "data-bed-preparation-submit"
    ].forEach((attribute) => {
        assert.match(TEMPLATE, new RegExp(attribute), attribute);
    });
    assert.match(TEMPLATE, /data-bed-preparation-submit>\s+Подготовить/);
    assert.match(TEMPLATE, /Подготовить место/);
    assert.match(JS, /if \(!basis\) \{\s+showFeedback\("Основание обязательно\.", true\);/);
    assert.match(JS, /if \(!state\.draft\.resident\) \{/);
    assert.match(JS, /resident_id: state\.draft\.resident\.resident_id/);
    assert.match(JS, /bed_stable_id: bed\.dataset\.bedId/);
    assert.match(JS, /arrival_date: arrivalDate/);
});

test("prepared bed panel offers confirm, extend and cancel with a reason", () => {
    assert.match(TEMPLATE, /data-bed-preparation-resident>/);
    assert.match(TEMPLATE, /data-bed-preparation-hold>/);
    assert.match(TEMPLATE, /data-bed-preparation-confirm>/);
    assert.match(TEMPLATE, /data-bed-preparation-extend-open/);
    assert.match(TEMPLATE, /data-bed-preparation-extend-submit/);
    assert.match(TEMPLATE, /data-bed-preparation-cancel>/);
    assert.match(TEMPLATE, /Подтвердить приезд/);
    assert.match(TEMPLATE, /Срок истёк — койка освободится при следующем действии\./);
    assert.match(JS, /"Подготовлено для: " \+ \(preparation\.resident_name \|\| ""\)/);
    assert.match(JS, /if \(!value\) \{\s+showFeedback\("Причина обязательна\.", true\);\s+return null;/);
    assert.match(JS, /{reason: reason}/);
});

test("drawer gets a temporary tab without touching the unsettled search and filters", () => {
    assert.match(TEMPLATE, /data-temporary-scope="unsettled"[^>]*aria-pressed="true">Нерасселённые/);
    assert.match(TEMPLATE, /data-temporary-scope="temporary"[^>]*aria-pressed="false">Временные/);
    assert.match(TEMPLATE, /data-unsettled-fields/);
    assert.match(TEMPLATE, /data-temporary-body/);
    assert.match(TEMPLATE, /data-temporary-list/);
    assert.match(TEMPLATE, /data-temporary-empty/);
    // Существующие поиск и фильтр смен остаются на месте.
    assert.match(TEMPLATE, /data-unsettled-search/);
    assert.match(TEMPLATE, /data-unsettled-shift-filter="all"/);
    assert.match(TEMPLATE, /data-unsettled-visible-count/);
    assert.match(JS, /if \(unsettledFields\) unsettledFields\.hidden = temporary;/);
    assert.match(JS, /if \(unsettledBody\) unsettledBody\.hidden = temporary;/);
    assert.match(JS, /"Подготовлено", state\.preparationRows/);
    assert.match(JS, /"Временно \(ждёт утверждения\)", state\.temporaryRows/);
    assert.match(JS, /"Резерв", state\.reserveRows/);
    assert.match(JS, /label: "Переселить"/);
    assert.match(JS, /label: "Освободить"/);
});

test("drawer has its own visible feedback for actions made with the panel closed", () => {
    assert.match(TEMPLATE, /data-temporary-feedback[^>]*role="status"[^>]*aria-live="polite"/);
    assert.match(CSS, /\.settlement-temporary-feedback \{/);
    assert.match(CSS, /\.settlement-temporary-feedback\.is-error \{/);
    assert.match(CSS, /\.settlement-temporary-feedback\[hidden\]/);
    assert.match(JS, /function showDrawerMessage\(message, isError, kind\)/);
    // Сообщение дублируется в ящик, пока он открыт, — ошибки release/relocate/
    // reserve и «Причина обязательна.» из списка остаются видимыми.
    assert.match(
        JS,
        /if \(message && unsettledPanel && !unsettledPanel\.hidden\) \{\s+showDrawerMessage\(message, isError, "action"\);/
    );
    assert.match(JS, /var temporaryFeedback = root\.querySelector\("\[data-temporary-feedback\]"\)/);
});

test("failed state load is shown instead of an empty temporary list", () => {
    const loadSource = JS.match(/function loadState\(\) \{[\s\S]*?\n    \}\n/)[0];
    assert.doesNotMatch(loadSource, /\.catch\(function \(\) \{\s*\/\*/);
    assert.match(loadSource, /response\.redirected \|\| response\.status === 401/);
    assert.match(loadSource, /showDrawerMessage\(message, true, "load"\)/);
    assert.match(loadSource, /setHidden\(temporaryEmpty, true\)/);
    assert.match(loadSource, /if \(!state\.loaded\) \{/);
    assert.match(JS, /state\.loaded = true;/);
    assert.match(JS, /temporaryEmpty\.hidden = !state\.loaded \|\| total !== 0/);
    assert.match(JS, /scopeCount\.textContent = state\.loaded \? String\(total\) : ""/);
    assert.match(JS, /clearDrawerMessage\("load"\)/);
});

test("panel visibility stabilizes after selecting a free bed or opening relocation", () => {
    const syncSource = JS.match(/function syncPanel\(\) \{[\s\S]*?\n    \}\n/)[0];
    const setHiddenSource = JS.match(/function setHidden\(node, hidden\) \{[\s\S]*?\n    \}\n/)[0];
    let writes = 0;
    function control(initial) {
        let hidden = initial;
        return {
            get hidden() { return hidden; },
            set hidden(value) { writes++; hidden = value; },
        };
    }
    const context = {
        state: {formOpen: false, panelBedId: "some-bed", reserves: {}},
        checkinForm: Object.assign(control(true), {dataset: {targetBed: "some-bed"}}),
        selectedBed: () => ({dataset: {bedId: "some-bed", occupied: "false"}}),
        preparationForBed: () => null,
        controlHeld: () => true,
        isTransferredBed: () => true,
        panel: {hidden: false},
        captureDraft() {},
        prepBlock: control(true),
        prepActions: control(true),
        reserveActions: control(true),
        prepForm: control(true),
        settlementForm: control(true),
        prepExtendBlock: null,
        prepExtendOpen: null,
        prepExtendReasonInput: null,
        prepExtendHoldInput: null,
        reserveButton: null,
    };
    const sync = vm.runInNewContext(setHiddenSource + syncSource + "syncPanel", context);
    sync();
    assert.equal(context.prepActions.hidden, false);
    assert.equal(context.settlementForm.hidden, false);
    writes = 0;
    sync();
    assert.equal(writes, 0, "the observer's second pass must make no changes");

    context.state.formOpen = true;
    sync();
    assert.equal(context.prepForm.hidden, false);
    assert.equal(context.settlementForm.hidden, true);
    context.state.formOpen = false;
    sync();
    assert.equal(context.settlementForm.hidden, false);

    context.panel.hidden = true;
    sync();
    assert.equal(context.prepActions.hidden, true);
    assert.equal(context.reserveActions.hidden, true);
    assert.equal(context.settlementForm.hidden, false, "the relocated form belongs to the dialog");
    writes = 0;
    sync();
    assert.equal(writes, 0);
});

test("relocation suggestion is shown for a temporary row", () => {
    // Ключ state читается целиком и индексируется по occupancy_id.
    assert.match(JS, /state\.relocationRows = payload\.relocation_suggestions \|\| \[\];/);
    assert.match(JS, /function indexRelocationRows\(rows\)/);
    assert.match(JS, /index\[String\(row\.occupancy_id\)\] = row;/);
    assert.match(JS, /state\.relocationByOccupancy = indexRelocationRows\(state\.relocationRows\);/);
    assert.match(JS, /function relocationSuggestionFor\(row\)/);
    assert.match(JS, /function relocationStatusLines\(suggestion\)/);
    // Строка получает блок подсказки только при наличии предложения; без него
    // рендер остаётся прежним.
    const rowSource = JS.match(/function temporaryRowNode\(row\) \{[\s\S]*?\n    \}\n/)[0];
    assert.match(rowSource, /var suggestion = relocationSuggestionFor\(row\);/);
    assert.match(rowSource, /suggestionLines: suggestion \? relocationStatusLines\(suggestion\) : \[\]/);
    // Тексты статусов из задания.
    assert.match(JS, /"Плановая койка: " \+ label/);
    assert.match(JS, /"Плановая койка занята: " \+ label/);
    assert.match(JS, /lines\.push\("Нет в утверждённом списке"\);/);
    assert.match(JS, /lines\.push\("Плановая койка не определена"\);/);
});

test("planned bed relocation is a button with a reason, never automatic", () => {
    const rowSource = JS.match(/function temporaryRowNode\(row\) \{[\s\S]*?\n    \}\n/)[0];
    assert.match(
        rowSource,
        /if \(suggestion && suggestion\.status === "ready" && suggestion\.planned_bed_stable_id\) \{[\s\S]*?label: "Переселить на плановую"/
    );
    const relocateSource = JS.match(/function relocateTemporaryToPlanned\(suggestion\) \{[\s\S]*?\n    \}/)[0];
    assert.match(relocateSource, /var reason = askReason\("Причина переселения"\);/);
    assert.match(relocateSource, /if \(!reason\) return;/);
    assert.match(relocateSource, /root\.dataset\.temporaryRelocateUrl/);
    assert.match(relocateSource, /occupancy_id: Number\(suggestion\.occupancy_id\)/);
    assert.match(relocateSource, /bed_stable_id: suggestion\.planned_bed_stable_id/);
    assert.match(relocateSource, /reason: reason/);
    // Авто-переселения нет: загрузка состояния и рендер списка не вызывают
    // переселение, кнопка — единственный вход в relocateTemporaryToPlanned.
    const applySource = JS.match(/function applyState\(payload\) \{[\s\S]*?\n    \}\n/)[0];
    assert.doesNotMatch(applySource, /temporaryRelocateUrl|relocateTemporaryToPlanned|relocateTemporary\(|armRelocation/);
    const renderSource = JS.match(/function renderTemporaryList\(\) \{[\s\S]*?\n    \}\n/)[0];
    assert.doesNotMatch(renderSource, /temporaryRelocateUrl|relocateTemporaryToPlanned\(/);
    assert.doesNotMatch(JS, /setTimeout\([^)]*relocate/i);
    assert.equal(
        JS.split("relocateTemporaryToPlanned(").length - 1,
        2,
        "planned relocation is called only from its own definition and the button handler"
    );
});

test("suggestion line style is added without touching the row geometry", () => {
    assert.match(CSS, /\.settlement-temporary-row-copy small\.is-suggestion \{/);
    assert.match(JS, /index === 0 \? "is-suggestion" : ""/);
});

test("temporary release sends the action reason without prompting", () => {
    const release = JS.match(/function releaseTemporary\(occupancyId\) \{[\s\S]*?\n    \}/)[0];
    assert.doesNotMatch(release, /askReason|window\.prompt/);
    assert.match(release, /occupancy_id: Number\(occupancyId\), reason: "Освобождение койки с карты"/);
});

test("reserve toggle sends the action reason without prompting", () => {
    assert.match(TEMPLATE, /data-bed-reserve-toggle/);
    assert.match(TEMPLATE, /В резерв/);
    assert.match(JS, /reserveButton\.textContent = reserve \? "Снять резерв" : "В резерв"/);
    const reserve = JS.match(/function toggleReserve\(bedStableId, mode\) \{[\s\S]*?\n    \}/)[0];
    assert.doesNotMatch(reserve, /askReason|window\.prompt/);
    assert.match(reserve, /action: mode, reason: reason/);
    assert.match(reserve, /mode === "set" \? "Постановка койки в резерв с карты" : "Снятие резерва с карты"/);
});

test("takeover is rendered for the admin role only", () => {
    const adminBlock = TEMPLATE.slice(
        TEMPLATE.indexOf("{% if access.role.code == 'admin' %}"),
        TEMPLATE.indexOf("{% endif %}", TEMPLATE.indexOf("{% if access.role.code == 'admin' %}"))
    );
    assert.ok(adminBlock.length > 0, "admin-only block is present");
    assert.match(adminBlock, /data-control-takeover>Забрать управление/);
    assert.match(JS, /askReason\("Причина перехвата управления"\)/);
    assert.match(JS, /root\.dataset\.controlTakeoverUrl, \{reason: reason\}/);
    assert.equal(
        TEMPLATE.split("data-control-takeover>").length - 1,
        1,
        "single takeover button in the template"
    );
});

test("server errors are shown as returned and do not drop the entered data", () => {
    assert.match(JS, /body && body\.error\s+\? body\.error/);
    assert.match(JS, /error\.httpStatus = response\.status;/);
    assert.match(JS, /if \(error && error\.httpStatus === 409\) \{/);
    assert.match(JS, /response\.redirected \|\| response\.status === 401/);
    assert.doesNotMatch(JS, /style\.display/);
    assert.doesNotMatch(JS, /innerHTML/);
});

test("new styles exist and the map geometry is not redefined", () => {
    assert.match(CSS, /\.settlement-bed-tag \{/);
    assert.match(CSS, /\.settlement-bed-tags \{/);
    assert.match(CSS, /\.settlement-bed\.is-prepared:not\(\.is-occupied\)/);
    assert.match(CSS, /\.settlement-bed\.is-reserve:not\(\.is-occupied\)/);
    assert.match(CSS, /\.settlement-bed-prep \{/);
    assert.match(CSS, /\.settlement-temporary-list \{/);
    assert.match(CSS, /\.settlement-temporary-row \{/);
    assert.match(CSS, /\.settlement-control-takeover \{/);
    assert.match(CSS, /\.settlement-bed-prep\[hidden\]/);
    // Геометрия карты остаётся прежней.
    assert.match(CSS, /min-height: var\(--settlement-bed-card-min-height\)/);
    assert.doesNotMatch(CSS, /--settlement-bed-card-min-height:\s*\d+px/);
    assert.doesNotMatch(CSS, /--settlement-room-min-height:\s*\d+px/);
    assert.doesNotMatch(CSS, /zoom\s*:/i);
});

test("scripts are loaded in order with the v39 cache buster", () => {
    assert.equal(TEMPLATE.split("settlement-map-v39").length - 1, 4);
    assert.equal(TEMPLATE.split("settlement-map-v37").length - 1, 0);
    const mapIndex = TEMPLATE.indexOf("js/settlement-clerk.js");
    const temporaryIndex = TEMPLATE.indexOf("js/settlement-temporary-placement.js");
    const externalIndex = TEMPLATE.indexOf("js/settlement-external-residents.js");
    assert.ok(mapIndex > 0 && temporaryIndex > mapIndex, "temporary placement loads after the map");
    assert.ok(externalIndex > temporaryIndex, "external residents stay last");
    assert.match(TEMPLATE, /js\/settlement-temporary-placement\.js[\s\S]{0,90}settlement-map-v39/);
});

/* T-036 frontend-7: подготовка места для ВИП-гостя (комната 38). */

test("preparation form switches between employee and guest", () => {
    assert.match(TEMPLATE, /data-bed-preparation-kind/);
    assert.match(TEMPLATE, /data-preparation-kind="employee" aria-pressed="true">Сотрудник</);
    assert.match(TEMPLATE, /data-preparation-kind="guest" aria-pressed="false">Гость</);
    assert.doesNotMatch(TEMPLATE, /data-preparation-kind="business_trip"/);
    assert.match(TEMPLATE, /data-bed-preparation-resident-label>Жилец по пропуску</);
    assert.match(JS, /var RESIDENT_KIND_EMPLOYEE = "employee";/);
    assert.match(JS, /var RESIDENT_KIND_GUEST = "guest";/);
    assert.match(JS, /function setResidentKind\(kind\) \{/);
    assert.match(JS, /button\.setAttribute\("aria-pressed", String\(active\)\);/);
    assert.match(JS, /prepKindButtons\.forEach\(function \(button\) \{/);
    // Переключение сбрасывает прежнего жильца и повторяет поиск.
    const setKind = JS.match(/function setResidentKind\(kind\) \{[\s\S]*?\n    \}\n/)[0];
    assert.match(setKind, /state\.draft\.resident = null;/);
    assert.match(setKind, /if \(query\.length >= 2\) runResidentSearch\(\);/);
});

test("guest search covers business trip and other external guests only", () => {
    assert.match(JS, /function residentMatchesKind\(resident\)/);
    assert.match(JS, /String\(resident && resident\.resident_type \? resident\.resident_type : ""\)\.toUpperCase\(\)/);
    assert.match(JS, /var GUEST_RESIDENT_TYPES = \{\s+BUSINESS_TRIP: true,\s+EXTERNAL_OTHER: true\s+\};/);
    assert.match(JS, /return GUEST_RESIDENT_TYPES\[type\] === true;/);
    assert.match(JS, /return type === "EMPLOYEE";/);
    assert.match(JS, /var rows = \(results \|\| \[\]\)\.filter\(residentMatchesKind\);/);
    assert.match(JS, /"Гости не найдены\. Проверьте ФИО, организацию или телефон\."/);
    assert.match(JS, /Среди найденных карточек гостей нет\. Уточните ФИО или организацию\./);
    assert.match(JS, /if \(meta\.organization\) parts\.push\(meta\.organization\);/);
    // Тип гостя показывается первой частью строки.
    assert.match(JS, /var parts = \[resident\.resident_type_label \|\| ""\];/);
});

test("guest preparation sends the resident id and explains special 409s", () => {
    assert.match(JS, /resident_id: state\.draft\.resident\.resident_id/);
    assert.match(JS, /bed_stable_id: bed\.dataset\.bedId/);
    assert.match(JS, /arrival_date: arrivalDate/);
    assert.match(JS, /basis: basis/);
    assert.match(JS, /resident_id: state\.draft\.resident\.resident_id,\s+arrival_date: arrivalDate/);
    assert.match(JS, /protected_dispatch_only: "38 — ВИП-резерв: только командированный из центрального офиса, вручную и с основанием\."/);
    assert.match(JS, /protected_room_forbidden: "Комната 36\/37 защищённого фонда: через «Подготовить место» недоступна\."/);
    assert.match(JS, /itr_room_requires_itr: "Комната на 2 места: заселение только ручное \(ИТР\) — используйте «Заселить» на карте\."/);
    assert.match(JS, /showFeedback\(actionErrorMessage\(error\), true\);/);
    assert.match(JS, /function specialFundHint\(error\)/);
    // Тип жильца уходит в поиск: GUEST для гостя, EMPLOYEE для сотрудника.
    assert.match(JS, /var RESIDENT_KIND_SEARCH_TYPE = \{\s+employee: "EMPLOYEE",\s+guest: "GUEST"\s+\};/);
    assert.match(JS, /residentSearchRequest\(query, searchType\)/);
    assert.match(JS, /var searchType = RESIDENT_KIND_SEARCH_TYPE\[residentKind\(\)\];/);
    // Новых маршрутов нет.
    assert.doesNotMatch(JS, /fetch\("\/|fetch\('\/|fetch\("http/);
    // Метка типа жильца не перетирает подпись подготовленного места.
    assert.match(JS, /var prepResidentKindLabel = root\.querySelector\("\[data-bed-preparation-resident-label\]"\);/);
    assert.equal((JS.match(/var prepResidentLabel = /g) || []).length, 1);
});

/* --- Исполнение в минимальной DOM-заглушке -------------------------------
   Проверяем фактическое поведение: поиск фильтруется по выбранному типу,
   командированный показывает организацию. */

function runtimeNode(tag, attributes) {
    const classes = new Set();
    const listeners = {};
    const node = {
        tagName: String(tag || "div").toUpperCase(),
        className: "",
        textContent: "",
        value: "",
        checked: false,
        disabled: false,
        hidden: false,
        children: [],
        attributes: Object.assign({}, attributes || {}),
        dataset: {},
        listeners,
        focus() {},
        appendChild(child) {
            this.children.push(child);
            return child;
        },
        replaceChildren() {
            this.children = [];
        },
        setAttribute(name, value) {
            this.attributes[name] = String(value);
        },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this.attributes, name)
                ? this.attributes[name]
                : null;
        },
        addEventListener(name, handler) {
            (listeners[name] = listeners[name] || []).push(handler);
        },
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            contains: (name) => classes.has(name),
            toggle: (name, force) => (force ? classes.add(name) : classes.delete(name))
        }
    };
    Object.keys(node.attributes).forEach((name) => {
        const parts = name.split("-");
        if (parts[0] !== "data") return;
        const key = parts.slice(1).map((part, index) => (
            index === 0 ? part : part.charAt(0).toUpperCase() + part.slice(1)
        )).join("");
        node.dataset[key] = node.attributes[name];
    });
    return node;
}

function runtimeText(node) {
    const parts = [];
    (function walk(current) {
        if (!current) return;
        if (current.textContent) parts.push(String(current.textContent));
        (current.children || []).forEach(walk);
    }(node));
    return parts.join(" | ");
}

async function settleRuntime(rounds = 6) {
    for (let index = 0; index < rounds; index += 1) {
        await new Promise((resolve) => setImmediate(resolve));
    }
}

const MIXED_SEARCH_RESULTS = [
    {
        resident_id: 11,
        resident_type: "EMPLOYEE",
        resident_type_label: "Штатный сотрудник",
        full_name: "Петров Пётр",
        meta: { position: "Машинист", organization: "Участок 1", personnel_number: "42" }
    },
    {
        resident_id: 22,
        resident_type: "BUSINESS_TRIP",
        resident_type_label: "Командированный",
        full_name: "Сидоров Сидор",
        meta: { position: "Инженер", organization: "ООО «Подряд»", personnel_number: "" }
    },
    {
        resident_id: 44,
        resident_type: "EXTERNAL_OTHER",
        resident_type_label: "Другой внешний жилец",
        full_name: "Орлов Олег",
        meta: { position: "Консультант", organization: "ООО «Гость-Сервис»", personnel_number: "" }
    },
    {
        resident_id: 33,
        resident_type: "CONTRACTOR",
        resident_type_label: "Сотрудник подрядчика",
        full_name: "Кузнецов Кузьма",
        meta: { position: "Слесарь", organization: "ООО «Смежник»", personnel_number: "" }
    }
];

function mountPreparationSearch(searchResults, options) {
    const settings = options || {};
    const root = runtimeNode("div");
    const search = runtimeNode("input");
    const results = runtimeNode("div");
    const label = runtimeNode("span");
    const selectedCard = runtimeNode("div");
    const selectedName = runtimeNode("strong");
    const selectedMeta = runtimeNode("small");
    const form = runtimeNode("form");
    const csrf = runtimeNode("input");
    csrf.value = "csrf-token";
    const kindButtons = [
        runtimeNode("button", { "data-preparation-kind": "employee" }),
        runtimeNode("button", { "data-preparation-kind": "guest" })
    ];
    const known = {
        "[data-bed-preparation-resident-search]": search,
        "[data-bed-preparation-resident-results]": results,
        "[data-bed-preparation-resident-label]": label,
        "[data-bed-preparation-selected]": selectedCard,
        "[data-bed-preparation-selected-name]": selectedName,
        "[data-bed-preparation-selected-meta]": selectedMeta,
        "[data-bed-preparation-form]": form,
        "input[name='csrfmiddlewaretoken']": csrf
    };
    root.querySelector = (selector) => known[selector] || null;
    root.querySelectorAll = (selector) => (
        selector === "[data-preparation-kind]" ? kindButtons : []
    );
    root.dataset = {
        residentSearchUrl: "/clerk/settlement/residents/search/",
        currentDate: "2026-11-20"
    };

    const searchBodies = [];
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
            querySelector: (selector) => (selector === "[data-settlement-root]" ? root : null),
            createElement: (tag) => runtimeNode(tag)
        },
        window: {
            setTimeout: (callback) => { callback(); return 1; },
            clearTimeout: () => undefined,
            MutationObserver: undefined,
            confirm: () => true,
            prompt: () => null
        },
        fetch: (url, init) => {
            const body = init && init.body ? JSON.parse(init.body) : {};
            searchBodies.push({ url, body });
            if (settings.rejectGuest && body.resident_type === "GUEST") {
                /* Сервер без backend-20: GUEST — неизвестный тип. */
                return Promise.resolve({
                    ok: false,
                    status: 400,
                    redirected: false,
                    json: () => Promise.resolve({
                        ok: false,
                        error: "Некорректный тип жильца.",
                        code: "settlement.placement.invalid_payload"
                    })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                redirected: false,
                json: () => Promise.resolve({ ok: true, results: searchResults })
            });
        }
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../settlement-personnel-number.js"), "utf8"), sandbox);
    vm.runInNewContext(JS, sandbox, { filename: "settlement-temporary-placement.js" });
    return { root, search, results, label, kindButtons, searchBodies };
}

test("runtime: guest mode shows business trip and other external guests", async () => {
    const app = mountPreparationSearch(MIXED_SEARCH_RESULTS);
    app.search.value = "Ив";
    app.search.listeners.input[0]();
    await settleRuntime();
    const employeeText = runtimeText(app.results);
    assert.match(employeeText, /Петров Пётр/, "сотрудник должен быть в списке");
    assert.doesNotMatch(employeeText, /Сидоров Сидор/, "гость не должен попадать в режим «Сотрудник»");
    assert.doesNotMatch(employeeText, /Орлов Олег/, "другой гость не должен попадать в режим «Сотрудник»");
    assert.doesNotMatch(employeeText, /Кузнецов Кузьма/, "подрядчик не должен попадать в режим «Сотрудник»");
    assert.equal(app.kindButtons[0].getAttribute("aria-pressed"), "true");

    app.kindButtons[1].listeners.click[0]();
    await settleRuntime();
    const guestText = runtimeText(app.results);
    assert.match(guestText, /Сидоров Сидор/, "командированный должен быть в списке гостей");
    assert.match(guestText, /ООО «Подряд»/, "организация командированного должна показываться");
    assert.match(guestText, /Орлов Олег/, "другой внешний гость должен быть в списке гостей");
    assert.match(guestText, /ООО «Гость-Сервис»/, "организация другого гостя должна показываться");
    assert.match(guestText, /Командированный/);
    assert.match(guestText, /Другой внешний жилец/);
    assert.doesNotMatch(guestText, /Петров Пётр/, "сотрудник не должен попадать в режим «Гость»");
    assert.doesNotMatch(guestText, /Кузнецов Кузьма/, "подрядчик не должен попадать в режим «Гость»");
    assert.equal(app.kindButtons[1].getAttribute("aria-pressed"), "true");
    assert.equal(app.kindButtons[0].getAttribute("aria-pressed"), "false");
    assert.match(app.label.textContent, /Гость по пропуску/);
    assert.ok(app.searchBodies.length >= 2, "поиск должен повториться при смене типа");
});

test("runtime: resident_type is sent and the client filter covers an old server", async () => {
    const app = mountPreparationSearch(MIXED_SEARCH_RESULTS);
    app.search.value = "Ив";
    app.search.listeners.input[0]();
    await settleRuntime();
    assert.equal(app.searchBodies[0].body.q, "Ив");
    assert.equal(app.searchBodies[0].body.resident_type, "EMPLOYEE");

    app.kindButtons[1].listeners.click[0]();
    await settleRuntime();
    const lastBody = app.searchBodies[app.searchBodies.length - 1].body;
    assert.equal(lastBody.resident_type, "GUEST");
    assert.equal(lastBody.q, "Ив");

    /* Старый сервер без параметра вернул смешанную выдачу — клиентский фильтр
       по-прежнему показывает только гостей. */
    const guestText = runtimeText(app.results);
    assert.match(guestText, /Сидоров Сидор/);
    assert.match(guestText, /Орлов Олег/);
    assert.doesNotMatch(guestText, /Петров Пётр/);
    assert.doesNotMatch(guestText, /Кузнецов Кузьма/);
});

test("runtime: GUEST on a server without backend-20 falls back to an unfiltered search", async () => {
    const app = mountPreparationSearch(MIXED_SEARCH_RESULTS, { rejectGuest: true });
    app.search.value = "Ив";
    app.search.listeners.input[0]();
    await settleRuntime();
    assert.equal(app.searchBodies[0].body.resident_type, "EMPLOYEE");

    app.kindButtons[1].listeners.click[0]();
    await settleRuntime();
    const bodies = app.searchBodies.map((row) => row.body);
    assert.equal(bodies[1].resident_type, "GUEST", "первый запрос — по контракту GUEST");
    assert.equal("resident_type" in bodies[2], false, "повтор без параметра для старого сервера");
    assert.equal(bodies[2].q, "Ив");

    /* Даже если сервер ответил 400 на GUEST, поиск не сломан: повтор без
       параметра плюс клиентский фильтр показывают гостей. */
    const guestText = runtimeText(app.results);
    assert.match(guestText, /Сидоров Сидор/);
    assert.match(guestText, /Орлов Олег/);
    assert.doesNotMatch(guestText, /Петров Пётр/);
});
