(function (root, factory) {
    "use strict";

    var api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root && root.document) {
        api.init(root, root.document);
    }
}(typeof window !== "undefined" ? window : null, function () {
    "use strict";

    function usesDirectMobileNavigation(win) {
        return Boolean(win.matchMedia && win.matchMedia("(max-width: 700px)").matches);
    }

    function setDialogDetails(dialog, card) {
        var name = String(card.dataset.appName || "");
        var targetUrl = String(card.dataset.appUrl || card.href || "");
        var qrUrl = String(card.dataset.appQr || "");
        var title = dialog.querySelector("[data-dialog-title]");
        var input = dialog.querySelector("[data-dialog-url]");
        var qrImage = dialog.querySelector("[data-dialog-qr]");
        var openLink = dialog.querySelector("[data-dialog-open]");
        var status = dialog.querySelector("[data-copy-status]");

        if (title) title.textContent = name;
        if (input) input.value = targetUrl;
        if (openLink) openLink.href = targetUrl;
        if (status) status.textContent = "";
        if (qrImage) {
            qrImage.alt = "QR-код для приложения «" + name + "»";
            qrImage.src = qrUrl;
        }
    }

    function openDialog(dialog) {
        if (typeof dialog.showModal === "function") {
            dialog.showModal();
        } else {
            dialog.setAttribute("open", "");
        }
    }

    function closeDialog(dialog) {
        if (typeof dialog.close === "function") {
            dialog.close();
        } else {
            dialog.removeAttribute("open");
        }
    }

    function copyValue(win, input) {
        var value = input ? String(input.value || "") : "";
        if (!value) return Promise.reject(new Error("empty"));
        if (win.navigator && win.navigator.clipboard && win.navigator.clipboard.writeText) {
            return win.navigator.clipboard.writeText(value);
        }
        input.focus();
        input.select();
        var copied = win.document.execCommand && win.document.execCommand("copy");
        return copied ? Promise.resolve() : Promise.reject(new Error("copy-failed"));
    }

    function init(win, doc) {
        var dialog = doc.querySelector("[data-app-dialog]");
        if (!dialog) return;

        doc.querySelectorAll("[data-app-card]").forEach(function (card) {
            card.addEventListener("click", function (event) {
                if (usesDirectMobileNavigation(win)) return;
                event.preventDefault();
                setDialogDetails(dialog, card);
                openDialog(dialog);
            });
        });

        var closeButton = dialog.querySelector("[data-dialog-close]");
        if (closeButton) {
            closeButton.addEventListener("click", function () {
                closeDialog(dialog);
            });
        }

        dialog.addEventListener("click", function (event) {
            if (event.target === dialog) closeDialog(dialog);
        });

        var copyButton = dialog.querySelector("[data-copy-link]");
        var input = dialog.querySelector("[data-dialog-url]");
        var status = dialog.querySelector("[data-copy-status]");
        if (copyButton) {
            copyButton.addEventListener("click", function () {
                copyValue(win, input).then(function () {
                    if (status) status.textContent = "Ссылка скопирована.";
                }).catch(function () {
                    if (status) status.textContent = "Не удалось скопировать. Выделите ссылку вручную.";
                });
            });
        }
    }

    return {
        init: init,
        usesDirectMobileNavigation: usesDirectMobileNavigation,
        setDialogDetails: setDialogDetails,
        copyValue: copyValue
    };
}));
