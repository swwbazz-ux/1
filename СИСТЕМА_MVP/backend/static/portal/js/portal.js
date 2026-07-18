(function () {
    "use strict";

    document.documentElement.classList.add("js");

    const publicHeader = document.querySelector("[data-public-header]");
    if (publicHeader) {
        const syncPublicHeader = () => {
            publicHeader.classList.toggle("is-scrolled", window.scrollY > 18);
        };
        window.addEventListener("scroll", syncPublicHeader, { passive: true });
        syncPublicHeader();
    }

    const routeScene = document.querySelector("[data-route-scene]");
    if (routeScene) {
        if ("IntersectionObserver" in window) {
            const routeObserver = new IntersectionObserver((entries, observer) => {
                if (!entries.some((entry) => entry.isIntersecting)) return;
                routeScene.classList.add("is-visible");
                observer.disconnect();
            }, { threshold: 0.18 });
            routeObserver.observe(routeScene);
        } else {
            routeScene.classList.add("is-visible");
        }
    }

    const setMenuButtonText = (button, isOpen) => {
        const label = button.querySelector(".sr-only");
        if (label) label.textContent = isOpen ? "Закрыть меню" : "Открыть меню";
    };

    const closeMenu = (button, restoreFocus = false) => {
        const menuId = button.getAttribute("aria-controls");
        const menu = menuId ? document.getElementById(menuId) : null;
        button.setAttribute("aria-expanded", "false");
        setMenuButtonText(button, false);
        menu?.classList.remove("is-open");
        if (restoreFocus) button.focus();
    };

    document.querySelectorAll(".js-menu-toggle").forEach((button) => {
        const menuId = button.getAttribute("aria-controls");
        const menu = menuId ? document.getElementById(menuId) : null;
        if (!menu) return;

        button.addEventListener("click", () => {
            const willOpen = button.getAttribute("aria-expanded") !== "true";
            button.setAttribute("aria-expanded", String(willOpen));
            setMenuButtonText(button, willOpen);
            menu.classList.toggle("is-open", willOpen);
        });

        menu.querySelectorAll("a").forEach((link) => {
            link.addEventListener("click", () => closeMenu(button));
        });
    });

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        document.querySelectorAll(".js-menu-toggle[aria-expanded='true']").forEach((button) => closeMenu(button, true));
    });

    document.addEventListener("click", (event) => {
        document.querySelectorAll(".js-menu-toggle[aria-expanded='true']").forEach((button) => {
            const menu = document.getElementById(button.getAttribute("aria-controls"));
            if (button.contains(event.target) || menu?.contains(event.target)) return;
            closeMenu(button);
        });
    });

    document.querySelectorAll(".js-flash-close").forEach((button) => {
        button.addEventListener("click", () => button.closest(".flash")?.remove());
    });

    document.querySelectorAll("a[aria-disabled='true']").forEach((link) => {
        link.addEventListener("click", (event) => event.preventDefault());
    });

    document.querySelectorAll("input[type='file']").forEach((input) => {
        const label = document.createElement("small");
        label.className = "file-selection";
        label.setAttribute("aria-live", "polite");
        input.insertAdjacentElement("afterend", label);
        input.addEventListener("change", () => {
            const names = Array.from(input.files || []).map((file) => file.name);
            label.textContent = names.length ? `Выбрано: ${names.join(", ")}` : "";
        });
    });

    document.querySelectorAll("textarea[maxlength]").forEach((textarea) => {
        const counter = document.createElement("small");
        counter.className = "character-counter";
        counter.setAttribute("aria-live", "polite");
        textarea.insertAdjacentElement("afterend", counter);
        const update = () => {
            counter.textContent = `${textarea.value.length} / ${textarea.maxLength}`;
        };
        textarea.addEventListener("input", update);
        update();
    });

    const makeConfirmDialog = () => {
        if (typeof HTMLDialogElement === "undefined") return null;
        const dialog = document.createElement("dialog");
        dialog.className = "confirm-dialog";
        dialog.innerHTML = `
            <form method="dialog" class="confirm-dialog__panel">
                <strong class="confirm-dialog__title">Подтвердите действие</strong>
                <p class="confirm-dialog__text"></p>
                <div class="confirm-dialog__actions">
                    <button class="button button--ghost" value="cancel">Отмена</button>
                    <button class="button button--primary" value="confirm">Продолжить</button>
                </div>
            </form>`;
        document.body.appendChild(dialog);
        return dialog;
    };

    const confirmDialog = makeConfirmDialog();
    document.querySelectorAll("form[data-confirm]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            if (form.dataset.confirmed === "true") {
                delete form.dataset.confirmed;
                return;
            }

            const question = form.dataset.confirm || "Продолжить?";
            if (!confirmDialog) {
                if (!window.confirm(question)) event.preventDefault();
                return;
            }

            event.preventDefault();
            const submitter = event.submitter;
            confirmDialog.querySelector(".confirm-dialog__text").textContent = question;
            confirmDialog.showModal();
            confirmDialog.addEventListener("close", () => {
                if (confirmDialog.returnValue !== "confirm") return;
                form.dataset.confirmed = "true";
                form.requestSubmit(submitter || undefined);
            }, { once: true });
        });
    });
})();
