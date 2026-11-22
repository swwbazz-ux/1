/* T-036 frontend-4: ручное заселение в двухместные и специальные комнаты.
 *
 * Правила владельца 20.11.2026 (state/T-036-OWNER-DECISIONS.md §11):
 *   - комнаты на 2 места авторасселение не использует, заселение только ручное,
 *     только ИТР и только по подтверждённому основанию;
 *   - защищённые/резервные комнаты (36/37/38) решает сервер по якорям и
 *     привязкам (settlement/special_fund.py), клиент их не угадывает.
 *
 * Что делает этот файл:
 *   1) при ручном заселении/переселении в комнату вместимостью <= 2 показывает
 *      в существующей форме обязательное подтверждение «сотрудник — ИТР» и поле
 *      «Основание»;
 *   2) не даёт отправить запрос без них и передаёт confirm_itr=true и
 *      placement_reason в существующий запрос ручного заселения; маршрут
 *      берётся из data-атрибута корня карты;
 *   3) дополняет серверные 409 по кодам спецфонда понятной подсказкой.
 *
 * Ограничение T-029: _room_card.html и settlement-clerk.js не изменяются.
 * Поэтому форма не перерисовывается: поля добавляются внутрь существующей
 * [data-settlement-form] (она же переезжает в окно переселения), а два поля
 * подмешиваются в уже собираемый запрос обёрткой window.fetch. Никаких новых
 * endpoints, шаблонных правок и версии v37.
 */
(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-root]");
    if (!root) return;

    var form = root.querySelector("[data-settlement-form]");
    if (!form) return;

    var panel = root.querySelector("[data-room-panel]");
    var relocationModal = root.querySelector("[data-relocation-modal]");
    var feedback = root.querySelector("[data-placement-feedback]");

    var ITR_ROOM_MAX_CAPACITY = 2;
    var REASON_MAX_LENGTH = 255;

    var REQUIRED_MESSAGE = "Комната на 2 места: отметьте «Подтверждаю: сотрудник — ИТР» и укажите основание.";

    /* Подсказки к серверным 409. Текст сервера уже человекочитаемый
       (settlement/special_fund.py:40-55); здесь только следующий шаг. */
    var SERVER_HINTS = {
        itr_room_requires_itr: "Отметьте «Подтверждаю: сотрудник — ИТР» и укажите основание.",
        itr_room_manual_only: "Двухместные комнаты заселяются только вручную.",
        protected_room_forbidden: "В защищённую комнату можно заселить только держателя места.",
        protected_dispatch_only: "Выбор командированного (внешнего жильца) в этой панели недоступен."
    };

    var controls = null;
    var refreshTimer = null;
    var lastRoomKey = "";

    /* --- Мелкие помощники -------------------------------------------------- */

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function setHidden(node, hidden) {
        if (!node) return;
        if (node.hidden === Boolean(hidden)) return;
        node.hidden = Boolean(hidden);
    }

    function selectedBed() {
        var beds = root.querySelectorAll("[data-bed]");
        for (var index = 0; index < beds.length; index += 1) {
            var bed = beds[index];
            if (
                bed.classList.contains("is-selected")
                || bed.getAttribute("aria-pressed") === "true"
            ) {
                return bed;
            }
        }
        return null;
    }

    function bedByStableId(value) {
        var wanted = String(value || "");
        if (!wanted) return null;
        var beds = root.querySelectorAll("[data-bed]");
        for (var index = 0; index < beds.length; index += 1) {
            if (beds[index].getAttribute("data-bed-id") === wanted) return beds[index];
        }
        return null;
    }

    function roomOf(bed) {
        if (!bed || typeof bed.closest !== "function") return null;
        return bed.closest("[data-room-card]");
    }

    function roomCapacity(room) {
        if (!room) return 0;
        return room.querySelectorAll("[data-bed]").length;
    }

    /* Вместимость берётся из разметки комнаты: карточка содержит все койки
       (занятые и свободные), поэтому их число и есть capacity. */
    function isManualOnlyRoom(room) {
        var capacity = roomCapacity(room);
        return capacity > 0 && capacity <= ITR_ROOM_MAX_CAPACITY;
    }

    function roomKey(room) {
        if (!room) return "";
        return String(
            room.getAttribute("data-room-id")
            || room.getAttribute("data-room-number")
            || ""
        );
    }

    function confirmedItr() {
        return Boolean(controls && controls.confirm && controls.confirm.checked);
    }

    function placementReason() {
        if (!controls || !controls.reason) return "";
        return String(controls.reason.value || "").trim();
    }

    function missingConfirmation() {
        return !confirmedItr() || !placementReason();
    }

    /* --- Поля формы -------------------------------------------------------- */

    function clearError() {
        if (!controls || !controls.error) return;
        controls.error.textContent = "";
        setHidden(controls.error, true);
    }

    function showError(message) {
        if (controls && controls.error) {
            controls.error.textContent = message;
            setHidden(controls.error, false);
        }
        if (feedback) {
            feedback.textContent = message;
            feedback.classList.toggle("is-error", Boolean(message));
            setHidden(feedback, !message);
        }
    }

    function buildControls() {
        var section = el("section", "settlement-special-fund");
        section.setAttribute("data-special-fund", "");
        setHidden(section, true);

        section.appendChild(el(
            "p",
            "settlement-special-fund-note",
            "Комната на 2 места: авторасселение её не использует, заселение только ручное."
        ));

        var reasonLabel = el("label", "settlement-special-fund-field");
        reasonLabel.appendChild(el("span", "", "Основание"));
        var reasonInput = document.createElement("input");
        reasonInput.type = "text";
        reasonInput.autocomplete = "off";
        reasonInput.maxLength = REASON_MAX_LENGTH;
        reasonInput.placeholder = "Приказ, служебная записка, номер и дата";
        reasonInput.setAttribute("data-special-fund-reason", "");
        reasonInput.setAttribute("aria-required", "true");
        reasonLabel.appendChild(reasonInput);

        var confirmLabel = el("label", "settlement-special-fund-check");
        var confirmInput = document.createElement("input");
        confirmInput.type = "checkbox";
        confirmInput.setAttribute("data-special-fund-confirm", "");
        confirmInput.setAttribute("aria-required", "true");
        confirmLabel.appendChild(confirmInput);
        confirmLabel.appendChild(el("span", "", "Подтверждаю: сотрудник — ИТР"));

        var errorNode = el("p", "settlement-special-fund-error", "");
        errorNode.setAttribute("data-special-fund-error", "");
        errorNode.setAttribute("role", "alert");
        setHidden(errorNode, true);

        section.appendChild(reasonLabel);
        section.appendChild(confirmLabel);
        section.appendChild(errorNode);

        var submit = form.querySelector("[data-settle-button]");
        if (submit) {
            form.insertBefore(section, submit);
        } else {
            form.appendChild(section);
        }

        controls = {
            section: section,
            reason: reasonInput,
            confirm: confirmInput,
            error: errorNode
        };

        reasonInput.addEventListener("input", clearError);
        confirmInput.addEventListener("change", clearError);
    }

    /* --- Показ полей ------------------------------------------------------- */

    function refresh() {
        if (!controls) return;
        var room = roomOf(selectedBed());
        var special = Boolean(room && isManualOnlyRoom(room) && !form.hidden);
        var key = roomKey(room);
        if (key !== lastRoomKey) {
            /* Другая комната — прежнее подтверждение не переносим. */
            lastRoomKey = key;
            controls.confirm.checked = false;
            controls.reason.value = "";
            clearError();
        }
        setHidden(controls.section, !special);
        if (!special) clearError();
    }

    function scheduleRefresh() {
        if (refreshTimer !== null) return;
        refreshTimer = window.setTimeout(function () {
            refreshTimer = null;
            refresh();
        }, 0);
    }

    /* --- Запрос ------------------------------------------------------------ */

    function requestBody(url, init) {
        if (!init || String(init.method || "GET").toUpperCase() !== "POST") return null;
        if (!root.dataset.occupancyCreateUrl || url !== root.dataset.occupancyCreateUrl) return null;
        if (typeof init.body !== "string") return null;
        var parsed;
        try {
            parsed = JSON.parse(init.body);
        } catch (error) {
            return null;
        }
        if (!parsed || (parsed.action !== "settle" && parsed.action !== "relocate")) return null;
        return parsed;
    }

    /* 409 по спецфонду: серверный текст оставляем, добавляем следующий шаг. */
    function explainSpecialConflict(response) {
        if (!response || response.status !== 409 || typeof response.clone !== "function") {
            return response;
        }
        return response.clone().json()
            .then(function (body) {
                var code = body && body.code ? String(body.code) : "";
                var hint = SERVER_HINTS[code];
                if (!hint) return response;
                var message = body.error ? String(body.error) : "";
                if (message.indexOf(hint) !== -1) return response;
                body.error = message ? message + " " + hint : hint;
                return new Response(JSON.stringify(body), {
                    status: 409,
                    statusText: response.statusText || "Conflict",
                    headers: {"Content-Type": "application/json"}
                });
            })
            .catch(function () { return response; });
    }

    /* Обёртка нужна потому, что settlement-clerk.js собирает тело запроса сам,
       а менять его запрещено (ограничение T-029). Обёртка узкая: только POST
       на существующий occupancy_create и только settle/relocate в комнату,
       где ручное размещение обязательно. */
    function installRequestGuard() {
        if (typeof window.fetch !== "function") return;
        if (window.fetch.__specialFundGuard) return;
        var originalFetch = window.fetch;

        var guard = function (input, init) {
            if (typeof input !== "string") return originalFetch(input, init);
            var body = requestBody(input, init);
            if (!body) return originalFetch(input, init);
            var room = roomOf(bedByStableId(body.bed_stable_id));
            if (!room || !isManualOnlyRoom(room)) return originalFetch(input, init);
            if (missingConfirmation()) {
                showError(REQUIRED_MESSAGE);
                if (controls) {
                    if (!placementReason()) controls.reason.focus();
                    else controls.confirm.focus();
                }
                return Promise.reject(new Error(REQUIRED_MESSAGE));
            }
            if (body.confirm_itr !== true) body.confirm_itr = true;
            if (!body.placement_reason) body.placement_reason = placementReason();
            var guardedInit = {};
            var key;
            for (key in init) {
                if (Object.prototype.hasOwnProperty.call(init, key)) guardedInit[key] = init[key];
            }
            guardedInit.body = JSON.stringify(body);
            return originalFetch(input, guardedInit).then(explainSpecialConflict);
        };
        guard.__specialFundGuard = true;
        window.fetch = guard;
    }

    /* --- Проверка перед отправкой ----------------------------------------- */

    /* Клик по «Заселить»/«Переселить» отправляет форму; если комната ручная, а
       подтверждения нет, событие гасится до обработчика карты (capture). */
    function onSubmit(event) {
        if (!controls) return;
        if (event.target !== form) return;
        var room = roomOf(selectedBed());
        if (!room || !isManualOnlyRoom(room) || form.hidden) return;
        if (!missingConfirmation()) return;
        event.preventDefault();
        event.stopPropagation();
        showError(REQUIRED_MESSAGE);
        if (!placementReason()) controls.reason.focus();
        else controls.confirm.focus();
    }

    /* --- Запуск ------------------------------------------------------------ */

    buildControls();
    installRequestGuard();

    document.addEventListener("submit", onSubmit, true);
    root.addEventListener("click", scheduleRefresh, true);
    root.addEventListener("drop", scheduleRefresh, true);
    root.addEventListener("change", scheduleRefresh, true);

    if (typeof window.MutationObserver === "function") {
        var observer = new window.MutationObserver(scheduleRefresh);
        [panel, relocationModal, form].forEach(function (node) {
            if (node) observer.observe(node, {attributes: true, attributeFilter: ["hidden"]});
        });
    }

    refresh();
}());
