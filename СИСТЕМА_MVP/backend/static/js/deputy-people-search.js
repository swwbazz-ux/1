/* One compact directory per search session. Only the selected equipment tab is loaded. */
window.deputyPeopleSearch = function (ui) {
    const input = ui.input;
    const panel = document.getElementById("deputy-search-results");
    const status = panel.querySelector("[data-search-status]");
    const announcement = input.parentElement.querySelector("[data-search-announcement]");
    const choices = panel.querySelector("[data-search-choices]");
    let request = null;
    let fetchedAt = 0;
    let generation = 0;
    let timer;
    let target = null;
    let choosing = false;
    let revealed = null;
    let alternatives = [];
    let quickSearchOrigin = null;

    // The directory highlights a match at the start of a name or a hyphenated part.
    function nameMatch(name, query) {
        if (!query) return null;
        const words = Array.from(name.matchAll(/[^\s-]+/gu));
        const parts = query.split(/[\s-]+/u).filter(Boolean).sort((a, b) => b.length - a.length);
        const ranges = [];
        for (const part of parts) {
            const word = words.find(item => !ranges.some(range => range.start === item.index)
                && ui.normalize(item[0]).startsWith(part));
            if (!word) return null;
            ranges.push({start: word.index, length: Math.min(part.length, word[0].length)});
        }
        return ranges.length ? ranges.sort((a, b) => a.start - b.start) : null;
    }

    function highlight(node, query) {
        const name = node.textContent;
        const ranges = nameMatch(name, query);
        if (!ranges) return;
        const content = document.createDocumentFragment();
        let offset = 0;
        for (const range of ranges) {
            content.append(name.slice(offset, range.start));
            const mark = document.createElement("mark");
            mark.className = "employee-search-highlight";
            offset = range.start + range.length;
            mark.textContent = name.slice(range.start, offset);
            content.append(mark);
        }
        content.append(name.slice(offset));
        node.replaceChildren(content);
    }

    function hidePanel() {
        choices.replaceChildren();
        panel.hidden = true;
    }

    function clear(clearInput) {
        generation++;
        clearTimeout(timer);
        if (choosing) ui.cancelNavigation();
        choosing = false;
        target = revealed = request = null;
        alternatives = [];
        quickSearchOrigin = null;
        hidePanel();
        if (announcement) announcement.textContent = "";
        if (clearInput) input.value = "";
    }

    function description(hit) {
        const place = hit.place;
        const parts = [ui.categoryLabel(place.role)];
        if (place.equipment) {
            parts.push("№ " + place.number, place.shift === "day" ? "1-я смена" : "2-я смена");
            if (place.position === "secondary") parts.push("Второе место");
        } else parts.push("Свободен");
        if (hit.person.personnel_number) parts.push("Таб. " + hit.person.personnel_number);
        return parts.join(" · ");
    }

    function message(text) {
        status.textContent = text;
        choices.replaceChildren();
        panel.hidden = false;
    }

    async function select(hit, ticket) {
        if (ticket !== generation) return;
        if (ui.busy()) {
            message("Дождитесь сохранения расстановки и повторите поиск.");
            return;
        }
        target = hit;
        choosing = true;
        hidePanel();
        if (hit.place.role !== ui.role()) {
            const ok = await ui.navigate(hit.place.role);
            if (ticket !== generation) return;
            if (!ok) {
                choosing = false;
                target = null;
                message("Не удалось открыть вкладку. Повторите поиск.");
                return;
            }
        }
        ui.render();
        let node = ui.findBadge(hit);
        if (!node && !ui.isSnapshot()) {
            await ui.navigate(hit.place.role);
            if (ticket !== generation) return;
            node = ui.findBadge(hit);
        }
        choosing = false;
        if (!node) {
            target = request = null;
            ui.render();
            message("Состав изменился. Повторите поиск по фамилии.");
            return;
        }
        if (node !== revealed) node.scrollIntoView({block: "nearest", inline: "nearest"});
        revealed = node;
        if (announcement) announcement.textContent = hit.person.full_name + ". " + description(hit);
    }

    function show(hits, ticket) {
        alternatives = hits;
        if (hits.length === 1) return select(hits[0], ticket);
        ui.render();
        if (!hits.length && ui.hasLocalMatches()) return hidePanel();
        message(hits.length ? new Set(hits.map(hit => hit.person.id)).size + " найдено" : "Совпадений нет");
        const query = ui.query();
        for (const hit of hits) {
            const button = document.createElement("a");
            button.href = ui.categoryUrl(hit.place.role);
            button.setAttribute("data-role-readonly-safe", "");
            const name = document.createElement("strong");
            name.textContent = hit.person.full_name;
            highlight(name, query);
            const location = document.createElement("small");
            location.textContent = description(hit);
            button.append(name, location);
            button.addEventListener("click", event => {
                if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                select(hit, ticket);
                input.focus({preventScroll: true});
            });
            choices.appendChild(button);
        }
    }

    async function search(ticket, query) {
        try {
            if (!request || Date.now() - fetchedAt > 30000) {
                fetchedAt = Date.now();
                request = ui.load();
            }
            const people = await request;
            if (ticket !== generation) return;
            const hits = [];
            for (const person of people) {
                if (!nameMatch(person.full_name, query)) continue;
                const places = person.places.length ? person.places :
                    [{role: person.roles.includes(ui.role()) ? ui.role() : person.roles[0]}];
                for (const place of places) hits.push({person, place});
            }
            hits.sort((a, b) => a.person.full_name.localeCompare(b.person.full_name, "ru") ||
                a.person.id - b.person.id || description(a).localeCompare(description(b), "ru", {numeric: true}));
            await show(hits, ticket);
        } catch (error) {
            if (ticket !== generation) return;
            request = target = null;
            ui.render();
            message("Поиск по всем вкладкам недоступен. Повторите поиск.");
        }
    }

    function update() {
        const query = ui.query();
        const ticket = ++generation;
        const hadTarget = Boolean(target);
        clearTimeout(timer);
        if (choosing) ui.cancelNavigation();
        choosing = false;
        target = revealed = null;
        alternatives = [];
        if (announcement) announcement.textContent = "";
        if (!query || !/\p{L}/u.test(query)) {
            hidePanel();
            request = null;
            ui.render();
            return;
        }
        if (hadTarget) ui.render();
        if (request) search(ticket, query);
        else {
            message("Ищу сотрудников…");
            timer = setTimeout(() => search(ticket, query), 150);
        }
    }

    input.addEventListener("input", update);
    function reopen() {
        if (panel.hidden && alternatives.length > 1 && !choosing) {
            target = revealed = null;
            show(alternatives, generation);
        }
    }
    input.addEventListener("click", () => {
        quickSearchOrigin = null;
        reopen();
    });
    document.addEventListener("focusin", event => {
        if (event.target !== input && !choices.contains(event.target)) quickSearchOrigin = null;
    });

    document.addEventListener("keydown", event => {
        const source = event.target;
        if (event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey || event.isComposing
            || !/^[0-9\p{L}]$/u.test(event.key) || ui.quickSearchBlocked()
            || !(source instanceof HTMLElement) || source.isContentEditable
            || source.closest('input, textarea, select, [role="textbox"], [role="combobox"], [role="listbox"], [role="menu"]')
            || (!ui.root.contains(source) && source !== document.body && source !== document.documentElement)
            || document.querySelector("dialog[open]")) return;

        event.preventDefault();
        quickSearchOrigin = source;
        input.focus();
        input.value = event.key;
        input.setSelectionRange(input.value.length, input.value.length);
        input.dispatchEvent(new Event("input", {bubbles: true}));
    });

    function handleKeys(event) {
        if (event.isComposing) return;
        if (event.key === "Escape" && (input.value || quickSearchOrigin)) {
            const origin = quickSearchOrigin;
            event.preventDefault();
            clear(true);
            ui.render();
            if (origin) {
                input.blur();
                if (origin.isConnected) origin.focus({preventScroll: true});
            } else input.focus({preventScroll: true});
            return;
        }
        if (event.key === "ArrowDown" && event.target === input) reopen();
        if (!choices.childElementCount) return;
        const buttons = Array.from(choices.children);
        const index = buttons.indexOf(document.activeElement);
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            const next = event.key === "ArrowDown" ? index + 1 : index <= 0 ? buttons.length - 1 : index - 1;
            buttons[next % buttons.length].focus();
        } else if (event.key === "Enter" && index === -1) {
            event.preventDefault();
            buttons[0].focus();
        }
    }
    input.addEventListener("keydown", handleKeys);
    choices.addEventListener("keydown", handleKeys);
    return {target: () => target, matches: nameMatch, highlight, clear};
};
