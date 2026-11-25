(function () {
    "use strict";

    var toggleSelector = ".credential-field > .credential-visibility-toggle";

    function setVisibility(button, visible) {
        var input = button.parentElement.querySelector("input");
        if (!input || input.closest(".cr-pin-field")) return;

        var type = visible ? "text" : "password";
        if (input.type !== type) {
            var start = input.selectionStart;
            var end = input.selectionEnd;
            var direction = input.selectionDirection;
            input.type = type;
            if (start !== null) input.setSelectionRange(start, end, direction);
        }
        button.setAttribute("aria-pressed", String(visible));
        button.setAttribute("aria-label", (visible ? "Скрыть " : "Показать ") + button.dataset.credentialLabel);
        button.classList.toggle("is-revealing", visible);
    }

    function hideCredentialFields(root) {
        root.querySelectorAll(toggleSelector + '[aria-pressed="true"]').forEach(function (button) {
            setVisibility(button, false);
        });
    }

    function enhanceAdminFields() {
        var template = document.querySelector("template[data-admin-credential-button]");
        if (!template) return;

        document.querySelectorAll('form input[type="password"][id]').forEach(function (input) {
            var field = document.createElement("span");
            field.className = "credential-field";
            var button = template.content.firstElementChild.cloneNode(true);
            button.setAttribute("aria-controls", input.id);
            if (input.name === "access_code") {
                button.dataset.credentialLabel = "код доступа";
                button.setAttribute("aria-label", "Показать код доступа");
            }
            input.before(field);
            field.append(input, button);
        });
    }

    // Delegation also covers shift forms replaced by realtime updates.
    document.addEventListener("click", function (event) {
        var button = event.target.closest(toggleSelector);
        if (button) setVisibility(button, button.getAttribute("aria-pressed") !== "true");
    });
    document.addEventListener("pointerdown", function (event) {
        var button = event.target.closest(toggleSelector);
        if (button && button.parentElement.contains(document.activeElement)
            && document.activeElement.matches("input")) {
            event.preventDefault();
        }
    });
    ["submit", "reset"].forEach(function (name) {
        document.addEventListener(name, function (event) { hideCredentialFields(event.target); }, true);
    });
    ["pagehide", "pageshow"].forEach(function (name) {
        window.addEventListener(name, function () { hideCredentialFields(document); });
    });
    window.hideCredentialFields = hideCredentialFields;

    enhanceAdminFields();
}());
