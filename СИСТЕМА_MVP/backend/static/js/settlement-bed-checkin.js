/* Оформление фактического проживания из выбранной койки. */
window.createSettlementBedCheckin = function (form, applied) {
    "use strict";
    if (!form) return null;
    const fields = form.elements;
    const name = fields.namedItem("full_name");
    const card = form.querySelector("[data-checkin-card]");
    const results = form.querySelector("[data-checkin-results]");
    const selection = form.querySelector("[data-checkin-selected]");
    const message = form.querySelector("[data-checkin-message]");
    const findButton = form.querySelector("[data-checkin-find]");
    const newButton = form.querySelector("[data-checkin-new]");
    const dates = form.querySelector("[data-checkin-dates]");
    const period = form.querySelector("[data-checkin-period]");
    const submit = form.querySelector('[type="submit"]');
    let target = null;
    let subject = "";
    let timer;
    let request = 0;
    let saving = false;

    function notify(text, isError = false) {
        message.textContent = text;
        message.hidden = !text;
        message.classList.toggle("is-error", isError);
        if (isError) message.scrollIntoView({block: "nearest"});
    }

    function clearResults() {
        clearTimeout(timer);
        request += 1;
        results.replaceChildren();
        results.hidden = true;
    }

    function toggleCard(open) {
        card.hidden = card.disabled = !open;
        findButton.setAttribute("aria-pressed", String(!open));
        newButton.setAttribute("aria-pressed", String(open));
        name.placeholder = open ? "Фамилия Имя Отчество" : "Фамилия или табельный номер";
        subject = open ? "new" : "";
        selection.hidden = true;
        name.setCustomValidity("");
        clearResults();
        notify("");
    }

    function updatePeriod() {
        const arrival = fields.arrival.value;
        const departure = fields.departure.value;
        const start = !arrival || arrival === fields.arrival.max ? "Сегодня" : arrival.split("-").reverse().join(".");
        period.textContent = [start, departure && "до " + departure.split("-").reverse().join(".")].filter(Boolean).join(" · ");
    }

    function search() {
        clearResults();
        selection.hidden = true;
        name.setCustomValidity("");
        notify("");
        if (!card.hidden) return;
        subject = "";
        const query = name.value.trim();
        if (!target || query.length < 2) return;
        const version = request;
        timer = setTimeout(async function () {
            try {
                const url = new URL(form.dataset.searchUrl, location.href);
                url.searchParams.set("q", query);
                const response = await fetch(url, {credentials: "same-origin"});
                if (!response.ok) throw new Error();
                const data = await response.json();
                if (version !== request) return;
                for (const person of data.results) {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "settlement-employee-result";
                    const label = document.createElement("strong");
                    label.className = "settlement-person-identity";
                    label.textContent = person.name;
                    label.append(window.settlementPersonnelNumber(person.personnel_number));
                    const meta = document.createElement("small");
                    meta.textContent = [person.kind, person.category_label, person.details].filter(Boolean).join(" · ");
                    button.append(label, meta);
                    button.addEventListener("click", function () {
                        subject = person.id;
                        name.value = person.name;
                        name.setCustomValidity("");
                        clearResults();
                        selection.replaceChildren(window.settlementPersonnelNumber(person.personnel_number));
                        if (person.category_label) selection.append(person.category_label);
                        selection.hidden = false;
                        notify("");
                    });
                    results.append(button);
                }
                results.hidden = !data.results.length;
                notify(data.results.length
                    ? "Нажмите на ФИО в списке, чтобы выбрать жильца."
                    : "Совпадений нет. Проверьте ФИО или выберите «Новый жилец».");
            } catch {
                if (version === request) notify("Не удалось проверить справочник. Повторите поиск.", true);
            }
        }, 180);
    }

    name.addEventListener("input", search);
    form.addEventListener("invalid", function (event) {
        const field = event.target;
        if (field !== form.querySelector("input:invalid, select:invalid")) return;
        if (dates.contains(field)) dates.open = true;
        if (field.type === "radio") {
            notify(field.name === "sex" ? "Выберите пол жильца." : "Выберите тип жильца.", true);
        } else {
            const label = field.labels?.[0]?.querySelector("span")?.textContent;
            const prefix = label && !field.validity.customError ? label + " — " : "";
            notify(prefix + field.validationMessage, true);
        }
    }, true);
    form.addEventListener("input", function () {
        if (message.classList.contains("is-error")) notify("");
    });
    dates.addEventListener("input", updatePeriod);
    findButton.addEventListener("click", function () {
        if (!card.hidden) {
            toggleCard(false);
            search();
        }
        name.focus();
    });
    newButton.addEventListener("click", function () {
        if (card.hidden) toggleCard(true);
        const type = card.querySelector('[name="resident_type"]:checked') || fields.resident_type[0];
        (name.value.trim() ? type : name).focus();
    });
    form.addEventListener("submit", async function (event) {
        event.preventDefault();
        if (!target || saving || form.hidden) return;
        if (!subject) {
            notify("Нажмите на ФИО в результатах поиска. Если человека нет в справочнике, выберите «Новый жилец».", true);
            name.focus();
            return;
        }
        const bed = target;
        const payload = Object.fromEntries(new FormData(form));
        delete payload.csrfmiddlewaretoken;
        payload.subject = subject;
        payload.bed_stable_id = bed.dataset.bedId;
        payload.confirm_itr = fields.confirm_itr.checked && !fields.confirm_itr.disabled;
        saving = submit.disabled = true;
        notify("Сохраняем заселение…");
        try {
            const response = await fetch(form.action, {
                method: "POST", credentials: "same-origin",
                headers: {"Content-Type": "application/json", "X-CSRFToken": fields.csrfmiddlewaretoken.value},
                body: JSON.stringify(payload)
            });
            const result = await response.json().catch(() => null);
            if (!result) throw new Error(response.redirected || response.status === 403
                ? "Сеанс мог завершиться. Откройте карту и проверьте вход."
                : "Сервер не подтвердил заселение. Проверьте койку на карте перед повторной попыткой.");
            if (!response.ok || !result.ok) throw new Error(result.error || "Сервер не подтвердил заселение. Проверьте койку на карте.");
            applied(result, bed);
        } catch (error) {
            if (target === bed) notify(error instanceof TypeError
                ? "Нет подтверждения от сервера. Проверьте связь и состояние койки на карте."
                : error.message, true);
        } finally {
            saving = submit.disabled = false;
        }
    });
    return {
        select(bed) {
            if (target === bed) return;
            target = bed;
            form.reset();
            dates.open = false;
            toggleCard(false);
            form.dataset.targetBed = bed ? bed.dataset.bedId : "";
            form.hidden = !bed;
            if (!bed) return;
            form.querySelector("[data-checkin-place]").textContent = bed.dataset.bedNumber;
            const room = bed.closest("[data-room-card]");
            const reserve = Boolean(room.querySelector('[data-room-anchor="reserve"]'));
            const protectedRoom = Boolean(room.querySelector('[data-room-anchor="protected"]'));
            const capacity = Number(room.dataset.roomCapacity) || room.querySelectorAll("[data-bed]").length;
            const itr = !reserve && (capacity <= 2 || protectedRoom);
            form.querySelector("[data-checkin-room]").hidden = !reserve && !itr;
            const rule = form.querySelector("[data-checkin-room-rule]");
            rule.hidden = !itr;
            rule.textContent = protectedRoom
                ? "В закреплённую комнату можно заселить её жильца. Если закрепления нет, укажите основание и подтвердите ИТР."
                : "Для двухместной комнаты укажите основание и подтвердите ИТР.";
            fields.basis.disabled = form.querySelector("[data-checkin-basis]").hidden = !reserve && !itr;
            fields.basis.required = reserve;
            fields.confirm_itr.disabled = form.querySelector("[data-checkin-itr]").hidden = !itr;
            const today = new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Vladivostok"}).format(new Date());
            fields.arrival.max = fields.arrival.value = fields.departure.min = today;
            updatePeriod();
        }
    };
};
