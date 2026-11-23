/* T-029 этап C: поведение панели жилфонда делопроизводителя.
 *
 * Файл обслуживает только экран /clerk/settlement/fund/ и не трогает карту
 * КИС-5/6. Здесь переключение полей, открытие формы и защита от повтора:
 *
 *   1) в форме койко-места выбор общежития сужает список комнат (все комнаты
 *      уже отрисованы сервером, JS только скрывает чужие варианты);
 *   2) после POST с переходом назад раскрывается та форма, из которой ушёл
 *      запрос, чтобы сообщение об ошибке не терялось в закрытом блоке;
 *   3) повторная отправка формы блокируется до ответа сервера.
 *
 * Серверные правила (арендованный фонд, передача комнаты, лимиты места)
 * клиент не дублирует: он лишь подсказывает и не подменяет ответ сервера.
 */
(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-fund]");
    if (!root) return;

    var OPEN_FORM_KEY = "settlementFundOpenForm";
    var ACTION_PATTERN = /^[a-z_]+$/;

    var roomDormitory = root.querySelector("[data-fund-room-dormitory]");
    var rentedLayoutFields = root.querySelectorAll("[data-fund-rented-layout]");

    function syncRoomLayout() {
        var option = roomDormitory.options[roomDormitory.selectedIndex];
        var rented = option && option.hasAttribute("data-fund-rented");
        rentedLayoutFields.forEach(function (field) {
            field.hidden = !rented;
            field.querySelector("input, select").disabled = !rented;
        });
    }

    if (roomDormitory) {
        roomDormitory.addEventListener("change", syncRoomLayout);
        syncRoomLayout();
    }

    /* --- 1. Каскад: общежитие -> комнаты в форме койко-места -------------- */

    var bedDormitory = root.querySelector("[data-fund-bed-dormitory]");
    var bedRoom = root.querySelector("[data-fund-bed-room]");
    var bedHint = root.querySelector("[data-fund-bed-hint]");

    function syncBedRooms() {
        if (!bedDormitory || !bedRoom) return;
        var dormitoryId = bedDormitory.value;
        var available = 0;
        Array.prototype.forEach.call(bedRoom.options, function (option) {
            if (!option.value) return;
            var matches = !dormitoryId || option.getAttribute("data-dormitory-id") === dormitoryId;
            option.hidden = !matches;
            option.disabled = !matches;
            if (matches) available += 1;
        });
        var selected = bedRoom.options[bedRoom.selectedIndex];
        if (selected && selected.disabled) {
            bedRoom.value = "";
        }
        if (bedHint) {
            bedHint.hidden = available > 0;
        }
    }

    if (bedDormitory && bedRoom) {
        bedDormitory.addEventListener("change", syncBedRooms);
        syncBedRooms();
    }

    /* --- 2. Раскрыть форму, из которой ушёл последний запрос -------------- */

    function readOpenForm() {
        try {
            return window.sessionStorage.getItem(OPEN_FORM_KEY);
        } catch (error) {
            return null;
        }
    }

    function clearOpenForm() {
        try {
            window.sessionStorage.removeItem(OPEN_FORM_KEY);
        } catch (error) {
            /* приватный режим: подсказка не критична */
        }
    }

    var lastForm = readOpenForm();
    if (lastForm && ACTION_PATTERN.test(lastForm)) {
        var target = root.querySelector('[data-fund-form="' + lastForm + '"]');
        if (target) {
            target.open = true;
        }
        clearOpenForm();
    }

    /* --- 3. Одна отправка на форму ---------------------------------------- */

    root.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form || !form.getAttribute || !form.getAttribute("data-fund-action")) {
            return;
        }
        if (form.getAttribute("data-fund-submitting") === "1") {
            event.preventDefault();
            return;
        }
        form.setAttribute("data-fund-submitting", "1");

        var button = form.querySelector('button[type="submit"]');
        if (button) {
            button.disabled = true;
            button.setAttribute("aria-busy", "true");
        }

        try {
            window.sessionStorage.setItem(OPEN_FORM_KEY, form.getAttribute("data-fund-action"));
        } catch (error) {
            /* приватный режим: без подсказки, но отправка проходит */
        }
    });

    /* Возврат из кеша браузера не должен оставлять форму заблокированной. */
    window.addEventListener("pageshow", function () {
        if (roomDormitory) syncRoomLayout();
        var blocked = root.querySelectorAll("[data-fund-submitting]");
        Array.prototype.forEach.call(blocked, function (form) {
            form.removeAttribute("data-fund-submitting");
            var button = form.querySelector('button[type="submit"]');
            if (button) {
                button.disabled = false;
                button.removeAttribute("aria-busy");
            }
        });
    });
}());
