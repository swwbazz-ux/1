(() => {
    const form = document.querySelector('#housing-import-form');
    if (!form) return;
    const main = form.closest('main');
    const feedback = document.querySelector('#housing-import-feedback');
    let busy = false;
    let availableRooms = [];

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
            const rowError = result.querySelector('.housing-review__error');
            (rowError || result).focus({ preventScroll: !rowError });
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
