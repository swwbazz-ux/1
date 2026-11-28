/* Keep expired reservations visible until an authoritative fragment arrives. */
(function (host) {
    "use strict";

    host.startDispatcherFreeBucketRefresh = function (boardSelector, isUnsafe, refresh) {
        var snapshot = "";
        var clockOffset = 0;
        var nextRetry = 0;
        var loading = false;

        function refreshAtDeadline() {
            var board = document.querySelector(boardSelector);
            if (!board) return;
            var renderedAt = board.dataset.serverNow;
            var now = Date.now();
            if (renderedAt !== snapshot) {
                snapshot = renderedAt;
                var serverNow = Date.parse(renderedAt);
                clockOffset = Number.isFinite(serverNow) ? serverNow - now : 0;
            }
            var deadline = Date.parse(board.dataset.dispatcherFreeBucketRefreshAt);
            if (!Number.isFinite(deadline) || now + clockOffset < deadline
                || loading || now < nextRetry || document.hidden || navigator.onLine === false || isUnsafe()) return;
            loading = true;
            nextRetry = now + 5000;
            Promise.resolve().then(refresh).catch(function () {}).finally(function () {
                loading = false;
            });
        }

        refreshAtDeadline();
        host.setInterval(refreshAtDeadline, 1000);
    };
})(window);
