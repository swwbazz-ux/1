(function () {
    "use strict";

    const form = document.querySelector("[data-crew-import-form]");
    if (!form || !window.fetch) return;
    const result = form.querySelector("[data-crew-import-result]");
    const feedback = form.querySelector("[data-crew-import-feedback]");
    const file = form.elements.workbook;

    function announce(message) {
        feedback.textContent = message;
        feedback.hidden = !message;
    }

    file.addEventListener("change", function () {
        result.querySelectorAll("[data-crew-import-message], [name=draft_confirmation]")
            .forEach((element) => element.remove());
        const submit = result.querySelector("button[type=submit]");
        submit.textContent = submit.dataset.uploadLabel;
        announce("");
    });

    form.addEventListener("submit", async function (event) {
        event.preventDefault();
        if (form.getAttribute("aria-busy") === "true") return;
        const payload = new FormData(form);
        const submit = result.querySelector("button[type=submit]");
        form.setAttribute("aria-busy", "true");
        submit.disabled = true;
        file.disabled = true;
        announce("Обрабатываем файл…");
        try {
            const response = await fetch(form.action, {
                method: "POST",
                body: payload,
                cache: "no-store",
            });
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            const document = new DOMParser().parseFromString(await response.text(), "text/html");
            const nextResult = document.querySelector("[data-crew-import-result]");
            if (!nextResult) throw new Error("Upload response is missing its result.");
            result.replaceChildren(...nextResult.childNodes);
            announce("");
            const employeeChoice = result.querySelector(".deputy-import-choice select");
            if (employeeChoice) {
                employeeChoice.focus();
            } else if (result.querySelector("[name=draft_confirmation]")) {
                result.querySelector("button[type=submit]").focus();
            }
        } catch {
            announce("Нет ответа от сервера. Проверь результат в расстановке перед повторной загрузкой.");
        } finally {
            form.removeAttribute("aria-busy");
            submit.disabled = false;
            file.disabled = false;
        }
    });
}());
