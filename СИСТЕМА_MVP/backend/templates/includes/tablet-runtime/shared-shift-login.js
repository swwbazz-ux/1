(function (global) {
    if (!global.tabletPersonalPinRestoreBound) {
        global.tabletPersonalPinRestoreBound = true;
        global.addEventListener('pageshow', function () {
            document.querySelectorAll('[data-tablet-personal-login] [data-shared-shift-code]').forEach(function (input) {
                input.value = '';
            });
        });
    }
    function formatPhone(value) {
        var digits = String(value || "").replace(/\D/g, "");
        if (digits.charAt(0) === "8") {
            digits = "7" + digits.slice(1);
        }
        if (digits.charAt(0) === "7") {
            digits = digits.slice(1);
        }
        digits = digits.slice(0, 10);
        var result = digits.slice(0, 3);
        if (digits.length > 3) {
            result += "-" + digits.slice(3, 6);
        }
        if (digits.length > 6) {
            result += "-" + digits.slice(6, 8);
        }
        if (digits.length > 8) {
            result += "-" + digits.slice(8, 10);
        }
        return result;
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
            function normalizePhone() {
                if (firstInput) firstInput.value = formatPhone(firstInput.value);
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
                    normalizePhone();
                    if (firstInput) {
                        firstInput.focus();
                        firstInput.setSelectionRange(firstInput.value.length, firstInput.value.length);
                    }
                });
            });
            if (firstInput && firstInput.dataset.sharedShiftLoginBound !== "true") {
                firstInput.dataset.sharedShiftLoginBound = "true";
                firstInput.addEventListener("input", normalizePhone);
                firstInput.addEventListener("focus", normalizePhone);
            }
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
            normalizePhone();
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
