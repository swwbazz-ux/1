(function (global) {
    if (!global.tabletPersonalPinRestoreBound) {
        global.tabletPersonalPinRestoreBound = true;
        global.addEventListener('pageshow', function () {
            document.querySelectorAll('[data-tablet-personal-login] [data-shared-shift-code]').forEach(function (input) {
                input.value = '';
            });
        });
    }
    function formatPhone(value, separator) {
        var digits = String(value || "").replace(/\D/g, "");
        if (digits.length > 10 && (digits.charAt(0) === "7" || digits.charAt(0) === "8")) {
            digits = digits.slice(1);
        }
        digits = digits.slice(0, 10);
        var result = digits.slice(0, 3);
        if (digits.length > 3) {
            result += separator + digits.slice(3, 6);
        }
        if (digits.length > 6) {
            result += separator + digits.slice(6, 8);
        }
        if (digits.length > 8) {
            result += separator + digits.slice(8, 10);
        }
        return result;
    }
    function normalizePhoneInput(input) {
        if (input) input.value = formatPhone(input.value, input.hasAttribute('data-tablet-phone') ? " " : "-");
    }
    function formatAccessCode(value) {
        var digits = String(value || "").replace(/\D/g, "").slice(0, 6);
        var result = digits.slice(0, 2);
        if (digits.length > 2) {
            result += "-" + digits.slice(2, 4);
        }
        if (digits.length > 4) {
            result += "-" + digits.slice(4, 6);
        }
        return result;
    }
    function initSharedShiftLogin(root) {
        var scope = root && root.querySelectorAll ? root : document;
        scope.querySelectorAll("[data-shared-shift-phone]").forEach(function (input) {
            if (input.dataset.sharedShiftLoginBound === "true") return;
            input.dataset.sharedShiftLoginBound = "true";
            function normalizePhone() {
                normalizePhoneInput(input);
            }
            input.addEventListener("input", normalizePhone);
            input.addEventListener("focus", normalizePhone);
            normalizePhone();
        });
        var modals = scope.querySelectorAll("[data-shared-shift-login-modal]");
        if (scope.matches && scope.matches("[data-shared-shift-login-modal]")) {
            modals = [scope];
        }
        modals.forEach(function (modal) {
            if (modal.hasAttribute('data-tablet-personal-login') && modal.tabletClose) return;
            var container = modal.parentElement || document;
            var openButtons = container.querySelectorAll("[data-shared-shift-login-open]");
            var cancelButtons = modal.querySelectorAll("[data-shared-shift-login-cancel]");
            var firstInput = modal.querySelector("[data-shared-shift-phone]");
            var codeInput = modal.querySelector("[data-shared-shift-code]");
            var tabletPortal = modal.hasAttribute('data-tablet-personal-login');
            var home = null;
            if (tabletPortal) {
                home = document.createComment('tablet shift login');
                modal.parentNode.insertBefore(home, modal);
            }
            function normalizeCode() {
                if (codeInput) codeInput.value = formatAccessCode(codeInput.value);
            }
            function closeModal() {
                modal.hidden = true;
                if (modal.hasAttribute('data-tablet-personal-login') && codeInput) codeInput.value = '';
                if (tabletPortal) {
                    if (home.parentNode) home.parentNode.insertBefore(modal, home.nextSibling);
                    else modal.remove();
                }
            }
            if (tabletPortal) modal.tabletClose = closeModal;
            openButtons.forEach(function (openButton) {
                if (openButton.dataset.sharedShiftLoginBound === "true") return;
                openButton.dataset.sharedShiftLoginBound = "true";
                openButton.addEventListener("click", function () {
                    if (modal.hasAttribute('data-tablet-personal-login') && codeInput) codeInput.value = '';
                    if (tabletPortal) {
                        modal.classList.add('tablet-personal-login-portal');
                        document.body.appendChild(modal);
                    }
                    modal.hidden = false;
                    normalizePhoneInput(firstInput);
                    if (firstInput) {
                        firstInput.focus();
                        firstInput.setSelectionRange(firstInput.value.length, firstInput.value.length);
                    }
                });
            });
            if (codeInput && codeInput.dataset.sharedShiftLoginBound !== "true") {
                codeInput.dataset.sharedShiftLoginBound = "true";
                codeInput.addEventListener("input", normalizeCode);
                codeInput.addEventListener("paste", function (event) {
                    var clipboard = event.clipboardData || global.clipboardData;
                    if (!clipboard) return;
                    event.preventDefault();
                    codeInput.value = formatAccessCode(clipboard.getData("text"));
                    normalizeCode();
                });
            }
            cancelButtons.forEach(function (cancelButton) {
                if (cancelButton.dataset.sharedShiftLoginBound === "true") return;
                cancelButton.dataset.sharedShiftLoginBound = "true";
                cancelButton.addEventListener("click", closeModal);
            });
            if (modal.dataset.sharedShiftLoginBound !== "true") {
                modal.dataset.sharedShiftLoginBound = "true";
                modal.addEventListener("click", function (event) {
                    if (event.target === modal) closeModal();
                });
            }
            normalizePhoneInput(firstInput);
            normalizeCode();
        });
        if (document.documentElement.dataset.sharedShiftEscapeBound !== "true") {
            document.documentElement.dataset.sharedShiftEscapeBound = "true";
            document.addEventListener("keydown", function (event) {
                if (event.key !== "Escape") return;
                document.querySelectorAll("[data-shared-shift-login-modal]").forEach(function (modal) {
                    if (typeof modal.tabletClose === 'function') { modal.tabletClose(); return; }
                    if (!modal.hidden) modal.hidden = true;
                    if (modal.hasAttribute('data-tablet-personal-login')) {
                        var pin = modal.querySelector('[data-shared-shift-code]');
                        if (pin) pin.value = '';
                    }
                });
            });
        }
    }
    global.initSharedShiftLogin = initSharedShiftLogin;
    initSharedShiftLogin(document);
})(window);
