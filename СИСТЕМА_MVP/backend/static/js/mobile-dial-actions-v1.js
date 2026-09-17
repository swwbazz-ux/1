(function (root) {
    "use strict";

    if (root.__mobileDialActionsBound) return;
    root.__mobileDialActionsBound = true;

    var pressed = null;

    function actionFromTarget(target) {
        return target && target.closest ? target.closest("[data-mobile-dial-action]") : null;
    }

    function release() {
        if (!pressed) return;
        pressed.classList.remove("is-pressed");
        pressed = null;
    }

    root.document.addEventListener("pointerdown", function (event) {
        var action = actionFromTarget(event.target);
        if (!action || action.disabled) return;
        release();
        pressed = action;
        action.classList.add("is-pressed");
    }, true);

    root.document.addEventListener("pointerup", release, true);
    root.document.addEventListener("pointercancel", release, true);
    root.addEventListener("blur", release);

    root.document.addEventListener("click", function (event) {
        var action = actionFromTarget(event.target);
        if (!action || action.disabled) return;
        if (root.navigator && typeof root.navigator.vibrate === "function") {
            try { root.navigator.vibrate(18); } catch (error) {}
        }
    });
})(window);
