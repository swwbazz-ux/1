(() => {
    const form = document.querySelector('#housing-import-form');
    if (!form) return;
    const main = form.closest('main');
    const feedback = document.querySelector('#housing-import-feedback');
    let busy = false;
    let availableRooms = [];

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

    function readReviewPlaces() {
        const catalog = main.querySelector('#housing-review-places');
        availableRooms = catalog ? JSON.parse(catalog.textContent) : [];
        main.querySelectorAll('.housing-review__place[open]').forEach(initPlacePicker);
    }

    function initPlacePicker(picker) {
        const room = picker.querySelector('[data-review-room]');
        if (room.options.length > 1) return;
        const bed = picker.querySelector('[data-review-bed]');
        const save = picker.querySelector('button');
        for (const option of availableRooms) room.add(new Option(option.label, String(option.id)));
        const attemptedBed = picker.dataset.attemptedBed;
        const attemptedRoom = availableRooms.find(option => option.beds.some(place => String(place.id) === attemptedBed));
        room.value = attemptedRoom ? String(attemptedRoom.id) : picker.dataset.roomId;

        function updateBeds() {
            const selected = availableRooms.find(option => String(option.id) === room.value);
            bed.options.length = 1;
            bed.value = '';
            for (const place of selected?.beds || []) bed.add(new Option(place.label, String(place.id)));
            bed.disabled = !selected;
            save.disabled = true;
        }

        room.addEventListener('change', updateBeds);
        bed.addEventListener('change', () => { save.disabled = !bed.value; });
        updateBeds();
        if (attemptedRoom) {
            bed.value = attemptedBed;
            save.disabled = false;
        }
    }

    readReviewPlaces();
    initReviewQueue();
    main.addEventListener('toggle', event => {
        if (event.target.matches('.housing-review__place[open]')) initPlacePicker(event.target);
    }, true);

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
        if (reviewAction && event.submitter.value.startsWith('move:')) {
            data.set('review_bed_id', event.submitter.closest('.housing-review__place').querySelector('[data-review-bed]').value);
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
            readReviewPlaces();
            initReviewQueue(queueSnapshot, source);
            const rowError = result.querySelector('.housing-review__error');
            if (rowError) rowError.focus();
            else if (reviewAction && queueState && !result.querySelector('[role="alert"]')) queueState.focus();
            else result.focus({preventScroll: false});
            feedback.textContent = '';
        } catch {
            feedback.textContent = 'Ответ не получен. Обновите страницу, чтобы открыть сохранённый черновик.';
        } finally {
            controls.forEach(control => { control.disabled = false; });
            form.removeAttribute('aria-busy');
            busy = false;
        }
    });
})();
