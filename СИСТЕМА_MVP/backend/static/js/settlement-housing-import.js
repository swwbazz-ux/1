(() => {
    const form = document.querySelector('#housing-import-form');
    if (!form) return;
    const main = form.closest('main');
    const feedback = document.querySelector('#housing-import-feedback');
    let busy = false;

    let queueState = null;

    function initReviewQueue(previous, preferredSource) {
        const queue = main.querySelector('.housing-review__queue');
        queueState = null;
        if (!queue) return;
        const savedChoice = main.querySelector('.housing-import__saved-choice');
        if (savedChoice) queue.parentElement.before(savedChoice);
        const search = queue.querySelector('input');
        const empty = queue.querySelector('.housing-review__empty');
        const normalize = text => text.toLocaleLowerCase('ru').replaceAll('ё', 'е').trim();
        const entries = Array.from(queue.querySelectorAll('[data-case-target]'), button => {
            const title = document.getElementById(button.dataset.caseTarget);
            const panel = title.closest('.housing-review__case');
            const sources = Array.from(panel.querySelectorAll('[data-housing-review-source]'), row => row.dataset.housingReviewSource);
            return {button, panel, title, sources, searchText: normalize(button.textContent + ' ' + button.dataset.search)};
        });
        let active;
        function select(entry) {
            active = entry;
            for (const item of entries) {
                item.panel.hidden = item !== entry;
                if (item === entry) item.button.setAttribute('aria-current', 'true');
                else item.button.removeAttribute('aria-current');
            }
        }
        function filter(preferred = active) {
            const words = normalize(search.value).split(/\s+/);
            for (const item of entries) item.button.hidden = !words.every(word => item.searchText.includes(word));
            const first = entries.find(item => !item.button.hidden);
            empty.hidden = Boolean(first);
            select(preferred && !preferred.button.hidden ? preferred : first);
        }
        search.value = previous?.query || '';
        // A validation error or an undo must always reveal the affected placement.
        const error = main.querySelector('.housing-review__error');
        const priority = error?.closest('[data-housing-review-source]')?.dataset.housingReviewSource || preferredSource;
        const target = entries.find(item => item.sources.includes(priority));
        if (target && !normalize(search.value).split(/\s+/).every(word => target.searchText.includes(word))) search.value = '';
        const remaining = previous?.order || [];
        const nearby = remaining.map(key => entries.find(item => item.sources.includes(key))).find(Boolean);
        filter(target || nearby);
        if (previous && search.value && !active) {
            search.value = '';
            filter(nearby);
        }
        const narrow = () => queue.closest('.housing-review').clientWidth < 720;
        queue.hidden = false;
        queue.open = !narrow() || !active;
        queue.addEventListener('click', event => {
            const button = event.target.closest('[data-case-target]');
            if (!button) return;
            const item = entries.find(entry => entry.button === button);
            select(item);
            if (narrow()) queue.open = false;
            item.title.focus({preventScroll: true});
        });
        search.addEventListener('input', () => filter());
        queueState = {
            snapshot() {
                const index = entries.indexOf(active);
                const sequence = [...entries.slice(Math.max(index, 0)), ...entries.slice(0, Math.max(index, 0)).reverse()];
                return {query: search.value, order: sequence.flatMap(item => item.sources)};
            },
            focus() {
                const element = active?.title || search;
                element.focus({preventScroll: true});
                element.scrollIntoView({block: 'nearest'});
            }
        };
    }

    const dialog = main.querySelector('[data-housing-map]');
    const map = {
        close: dialog.querySelector('[data-map-close]'),
        filters: dialog.querySelector('[data-map-filters]'),
        building: dialog.querySelector('[data-map-building]'),
        floor: dialog.querySelector('[data-map-floor]'),
        room: dialog.querySelector('[data-map-room]'),
        plan: dialog.querySelector('[data-map-plan]'),
        status: dialog.querySelector('[data-map-status]'),
        selection: dialog.querySelector('[data-map-selection]'),
        resident: dialog.querySelector('[data-map-resident]'),
        save: dialog.querySelector('[data-map-save]'),
    };
    let mapRequest;
    let mapSource;
    let mapTrigger;
    let mapRooms = [];
    let originRoomId;
    let selectedBed;

    function element(tag, text, className) {
        const node = document.createElement(tag);
        node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function fillOptions(select, values, preferred) {
        select.replaceChildren(...values.map(([value, label]) => new Option(label, value)));
        if (Array.from(select.options).some(option => option.value === String(preferred))) {
            select.value = preferred;
        }
    }

    function selectBed(room, bed, button) {
        selectedBed?.button.setAttribute('aria-pressed', 'false');
        selectedBed = {bed, button};
        button.setAttribute('aria-pressed', 'true');
        const details = [element('strong', room.label + ' · койка ' + bed.label), element('span', bed.status_label)];
        for (const occupant of bed.residents) {
            details.push(element('span', [occupant.name, occupant.shift,
                occupant.starts + (occupant.ends ? ' — ' + occupant.ends : '')].filter(Boolean).join(' · ')));
        }
        for (const name of bed.draft) details.push(element('span', 'В Excel · ' + name));
        map.selection.replaceChildren(...details);
        map.save.disabled = bed.status !== 'free';
        map.status.textContent = '';
    }

    function renderRoom(room) {
        const card = element('section', '', 'housing-map-picker__room');
        const heading = element('h3', 'Комната ' + room.number);
        if (room.id === originRoomId) {
            card.dataset.originRoom = '';
            heading.append(element('small', 'Место из сверки'));
        }
        card.append(heading);
        const blocks = new Map();
        const beds = element('div', '', 'housing-map-picker__beds');
        for (const bed of room.beds) {
            const button = element('button', '', 'housing-map-picker__bed');
            button.type = 'button';
            button.dataset.bedStatus = bed.status;
            button.dataset.bedId = bed.id;
            button.setAttribute('aria-pressed', 'false');
            const people = [...bed.residents.map(item => item.name), ...bed.draft];
            const labels = {draft: 'В Excel', occupied: 'Занята', unavailable: 'Недоступна'};
            const label = labels[bed.status] || bed.status_label;
            button.append(element('strong', bed.label), element('small', label));
            if (people.length) button.append(element('span', people.join(', '), 'housing-map-picker__people'));
            button.setAttribute('aria-label', [room.label, bed.label, bed.status_label, ...people].join(', '));
            button.addEventListener('click', () => selectBed(room, bed, button));
            if (!blocks.has(bed.block)) {
                const block = element('div', '', 'housing-map-picker__block');
                blocks.set(bed.block, block);
                beds.append(block);
            }
            blocks.get(bed.block).append(button);
        }
        if (blocks.size < 2) beds.classList.add('housing-map-picker__beds--single');
        if (!blocks.size) beds.append(element('p', 'Коек нет'));
        card.append(beds);
        return card;
    }

    function renderMap() {
        selectedBed = null;
        map.save.disabled = true;
        map.selection.textContent = 'Выберите койку на карте';
        map.status.textContent = '';
        const rooms = mapRooms.filter(room => room.dormitory === map.building.value
            && String(room.floor) === map.floor.value && (!map.room.value || String(room.id) === map.room.value));
        const sides = ['left', 'right'].map(side => {
            const row = element('div', '', 'housing-map-picker__side');
            row.append(...rooms.filter(room => room.side === side).map(renderRoom));
            return row;
        });
        map.plan.replaceChildren(sides[0], element('div', 'Коридор', 'housing-map-picker__corridor'), sides[1]);
        if (!rooms.length) map.status.textContent = 'На этом этаже нет комнат.';
        map.plan.scrollLeft = 0;
    }

    function updateMapFilters(preferred = null) {
        const inBuilding = mapRooms.filter(room => room.dormitory === map.building.value);
        const floors = [...new Set(inBuilding.map(room => room.floor))];
        fillOptions(map.floor, floors.map(floor => [floor, floor + ' этаж']), preferred?.floor ?? map.floor.value);
        const onFloor = inBuilding.filter(room => String(room.floor) === map.floor.value);
        fillOptions(map.room, [['', 'Весь этаж'], ...onFloor.map(room => [room.id, 'Комната ' + room.number])], '');
        renderMap();
    }

    function mapMoveSubmitter() {
        return main.querySelector('[data-housing-review-source="' + CSS.escape(mapSource) + '"] [data-submit-housing-map]');
    }

    async function openHousingMap(trigger) {
        mapTrigger = trigger;
        mapSource = trigger.closest('[data-housing-review-source]').dataset.housingReviewSource;
        selectedBed = null;
        map.save.disabled = true;
        map.filters.hidden = true;
        map.filters.inert = false;
        map.plan.inert = false;
        map.plan.replaceChildren();
        map.resident.textContent = '';
        map.selection.textContent = '';
        map.status.textContent = 'Загружаем карту…';
        dialog.showModal();

        const controller = new AbortController();
        mapRequest = controller;
        const url = new URL(form.getAttribute('action') || window.location.href, window.location.href);
        url.search = new URLSearchParams({
            place_map: mapSource,
            revision: main.querySelector('[name="draft_revision"]').value,
        });
        try {
            const response = await fetch(url, {signal: controller.signal, cache: 'no-store'});
            if (response.redirected) throw new Error('Войдите в приложение снова, чтобы открыть карту.');
            const data = await response.json();
            if (!response.ok || !Array.isArray(data.rooms)) throw new Error(data.message);
            if (controller.signal.aborted) return;
            mapRooms = data.rooms;
            map.resident.textContent = data.resident + ' · ' + data.date + ' · Место из сверки: ' + data.origin;
            originRoomId = data.room_id;
            const origin = mapRooms.find(room => room.id === originRoomId);
            const buildings = [...new Set(mapRooms.map(room => room.dormitory))];
            fillOptions(map.building, buildings.map(number => [number, 'КИС-' + number]), origin?.dormitory);
            updateMapFilters(origin);
            map.filters.hidden = !buildings.length;
            const originCard = map.plan.querySelector('[data-origin-room]');
            originCard?.scrollIntoView({block: 'nearest', inline: 'nearest'});
        } catch (error) {
            if (!controller.signal.aborted) {
                map.status.textContent = error instanceof SyntaxError || error instanceof TypeError
                    ? 'Не удалось загрузить карту. Закройте её и попробуйте ещё раз.'
                    : error.message;
            }
        }
    }

    main.addEventListener('click', event => {
        const trigger = event.target.closest('[data-open-housing-map]');
        if (trigger && !busy) openHousingMap(trigger);
    });
    map.close.addEventListener('click', () => { if (!busy) dialog.close(); });
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => {
        if (dialog.open) return;
        mapRequest?.abort();
        if (!busy && mapTrigger) {
            const trigger = mapMoveSubmitter()?.closest('[data-housing-review-source]').querySelector('[data-open-housing-map]');
            (trigger || mapTrigger)?.focus({preventScroll: true});
        }
    });
    map.building.addEventListener('change', () => updateMapFilters());
    map.floor.addEventListener('change', () => updateMapFilters());
    map.room.addEventListener('change', renderMap);
    map.save.addEventListener('click', () => {
        const submitter = mapMoveSubmitter();
        if (submitter && selectedBed?.bed.status === 'free') form.requestSubmit(submitter);
    });

    initReviewQueue();

    form.addEventListener('change', event => {
        if (!busy && event.target.matches('#id_as_of, #id_workbook')) {
            main.querySelector('#housing-import-report-content')?.replaceChildren();
        }
    });

    main.addEventListener('keydown', event => {
        if (event.key !== 'Enter' || !event.target.matches('.housing-import__review-fields input')) return;
        event.preventDefault();
        form.requestSubmit(event.target.closest('.housing-import__review-fields').querySelector('button'));
    });

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (busy) return;

        const reviewAction = event.submitter?.name === 'review_action';
        const action = event.submitter?.value === 'apply' ? 'apply' : 'preview';
        const queueSnapshot = reviewAction ? queueState?.snapshot() : null;
        const source = reviewAction ? event.submitter.value.slice(event.submitter.value.indexOf(':') + 1) : null;
        const data = new FormData(form);
        data.set('action', action);
        if (reviewAction) data.set('review_action', event.submitter.value);
        const moving = reviewAction && event.submitter.value.startsWith('move:');
        let mapStale = false;
        if (moving) {
            if (!dialog.open || selectedBed?.bed.status !== 'free') return;
            data.set('review_bed_id', selectedBed.bed.id);
            map.close.disabled = true;
            map.plan.inert = true;
            map.filters.inert = true;
            map.save.disabled = true;
            map.status.textContent = 'Проверяем койку…';
        }
        if (!form.querySelector('#id_workbook').files.length) data.delete('workbook');
        const controls = [...form.elements].filter(control => !control.disabled);
        busy = true;
        controls.forEach(control => { control.disabled = true; });
        form.setAttribute('aria-busy', 'true');
        feedback.textContent = action === 'apply' ? 'Переносим строки на карту…'
            : reviewAction ? 'Проверяем решение…' : 'Проверяем Excel…';

        try {
            const response = await fetch(form.getAttribute('action') || window.location.href, {
                method: 'POST', body: data, credentials: 'same-origin',
            });
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            const page = new DOMParser().parseFromString(await response.text(), 'text/html');
            const result = page.querySelector('#housing-import-response');
            if (!result) throw new Error('Missing import response');
            if (response.ok) {
                document.querySelector('#housing-import-source-state')
                    .replaceWith(page.querySelector('#housing-import-source-state'));
            }
            document.querySelector('#housing-import-response').replaceWith(result);
            initReviewQueue(queueSnapshot, source);
            const rowError = result.querySelector('.housing-review__error');
            if (moving && (rowError || !response.ok || result.querySelector('[role="alert"]'))) {
                mapStale = !response.ok || !rowError;
                map.status.textContent = rowError?.textContent || result.querySelector('[role="alert"]')?.textContent
                    || 'Не удалось сохранить койку. Попробуйте ещё раз.';
                map.status.focus();
            } else if (moving) {
                mapTrigger = null;
                dialog.close();
                if (queueState) queueState.focus();
                else result.focus();
            } else if (rowError) rowError.focus();
            else if (reviewAction && queueState && !result.querySelector('[role="alert"]')) queueState.focus();
            else result.focus({preventScroll: false});
            feedback.textContent = '';
        } catch {
            const message = 'Ответ не получен. Обновите страницу, чтобы открыть сохранённый черновик.';
            if (moving) map.status.textContent = message;
            else feedback.textContent = message;
        } finally {
            controls.forEach(control => { control.disabled = false; });
            form.removeAttribute('aria-busy');
            busy = false;
            if (moving) {
                map.close.disabled = false;
                map.plan.inert = mapStale;
                map.filters.inert = mapStale;
                map.save.disabled = mapStale || selectedBed?.bed.status !== 'free';
            }
        }
    });
})();
