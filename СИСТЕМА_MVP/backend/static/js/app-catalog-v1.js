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

    function setDialogDetails(dialog, card) {
        var name = String(card.dataset.appName || "");
        var targetUrl = String(card.dataset.appUrl || card.href || "");
        var qrUrl = String(card.dataset.appQr || "");
        var qrTargetUrl = String(card.dataset.appQrTarget || targetUrl);
        var title = dialog.querySelector("[data-dialog-title]");
        var input = dialog.querySelector("[data-dialog-url]");
        var qrImage = dialog.querySelector("[data-dialog-qr]");
        var qrTarget = dialog.querySelector("[data-dialog-qr-target]");
        var openLink = dialog.querySelector("[data-dialog-open]");
        var status = dialog.querySelector("[data-copy-status]");

        dialog.dataset.appName = name;
        dialog.dataset.appUrl = targetUrl;
        if (title) title.textContent = name;
        if (input) input.value = targetUrl;
        if (openLink) openLink.href = targetUrl;
        if (qrTarget) qrTarget.textContent = "QR откроет: " + qrTargetUrl;
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

    function shareValue(win, name, url, input) {
        if (win.navigator && typeof win.navigator.share === "function") {
            return Promise.resolve(win.navigator.share({
                title: "Рабочее приложение «" + name + "»",
                text: "Откройте ссылку, установите приложение и войдите в свою учётную запись.",
                url: url
            })).then(function () {
                return "shared";
            });
        }
        return copyValue(win, input).then(function () {
            return "copied";
        });
    }

    function init(win, doc) {
        var dialog = doc.querySelector("[data-app-dialog]");
        if (!dialog) return;

        doc.querySelectorAll("[data-app-card]").forEach(function (card) {
            card.addEventListener("click", function (event) {
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
        var shareButton = dialog.querySelector("[data-share-link]");
        var input = dialog.querySelector("[data-dialog-url]");
        var status = dialog.querySelector("[data-copy-status]");
        if (shareButton) {
            shareButton.addEventListener("click", function () {
                shareValue(
                    win,
                    String(dialog.dataset.appName || ""),
                    String(dialog.dataset.appUrl || (input && input.value) || ""),
                    input
                ).then(function (result) {
                    if (!status) return;
                    status.textContent = result === "shared"
                        ? "Ссылка передана в выбранное приложение."
                        : "Меню отправки недоступно — ссылка скопирована.";
                }).catch(function (error) {
                    if (!status) return;
                    status.textContent = error && error.name === "AbortError"
                        ? "Отправка отменена."
                        : "Не удалось отправить ссылку. Скопируйте её кнопкой ниже.";
                });
            });
        }
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
        setDialogDetails: setDialogDetails,
        copyValue: copyValue,
        shareValue: shareValue
    };
}));
