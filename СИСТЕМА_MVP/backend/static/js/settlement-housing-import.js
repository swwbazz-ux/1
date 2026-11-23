(() => {
    const form = document.querySelector('#housing-import-form');
    if (!form) return;
    const feedback = document.querySelector('#housing-import-feedback');
    let busy = false;

    form.addEventListener('change', () => {
        if (!busy) document.querySelector('#housing-import-response').replaceChildren();
    });

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (busy) return;

        const action = event.submitter?.value === 'apply' ? 'apply' : 'preview';
        const data = new FormData(form);
        data.set('action', action);
        const controls = [...form.elements].filter(control => !control.disabled);
        busy = true;
        controls.forEach(control => { control.disabled = true; });
        form.setAttribute('aria-busy', 'true');
        feedback.textContent = action === 'apply' ? 'Переносим строки на карту…' : 'Проверяем Excel…';

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
            document.querySelector('#housing-import-response').replaceWith(result);
            result.focus({ preventScroll: true });
            feedback.textContent = '';
        } catch {
            feedback.textContent = 'Ответ не получен. Снова нажмите «Проверить Excel», чтобы сверить карту.';
        } finally {
            controls.forEach(control => { control.disabled = false; });
            form.removeAttribute('aria-busy');
            busy = false;
        }
    });
})();
