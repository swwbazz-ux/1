(function (root) {
    "use strict";
    // Background only: readiness, new facts and reports never await the network.
    function create(options) {
        var inFlight = null;
        var stopped = false;
        var timer = null;
        var retryMs = 5000;
        var lastShiftId = "";
        function current() { return !stopped && (!options.isCurrent || options.isCurrent()); }
        function refresh() {
            if (inFlight) return inFlight;
            if (!current() || (root.navigator && root.navigator.onLine === false)) return Promise.resolve(false);
            var expired = false;
            var abort = root.AbortController ? new root.AbortController() : null;
            var timeout;
            var deadline = new Promise(function (resolve, reject) {
                timeout = setTimeout(function () {
                    expired = true;
                    if (abort) abort.abort();
                    reject(new Error("archive_deadline"));
                }, options.timeoutMs || 12000);
            });
            function allowed() { if (expired || !current()) throw new Error("archive_stale_request"); }
            var work = options.ledger.snapshot().then(function (state) {
                allowed();
                var candidates = options.ledger.archiveCandidates(state);
                var previous = candidates.findIndex(function (item) { return item.local_shift_id === lastShiftId; });
                var shift = candidates.length ? candidates[(previous + 1) % candidates.length] : null;
                if (!shift) return null;
                lastShiftId = shift.local_shift_id;
                var proof = null;
                var entries = [];
                function page(offset) {
                    allowed();
                    var url = new URL(options.url, root.location && root.location.href || "https://localhost/");
                    url.searchParams.set("device_id", state.identity.device_id);
                    url.searchParams.set("local_shift_id", shift.local_shift_id);
                    url.searchParams.set("close_event_id", shift.close_event_id);
                    url.searchParams.set("offset", String(offset));
                    if (proof) url.searchParams.set("snapshot_id", proof.snapshot_id);
                    return options.fetch(url.toString(), {credentials: "same-origin", cache: "no-store",
                        headers: {"Accept": "application/json"}, signal: abort && abort.signal}).then(function (response) {
                        allowed();
                        if (!response.ok) throw new Error("archive_not_ready");
                        return response.json();
                    }).then(function (body) {
                        allowed();
                        if (!body || body.ok !== true || body.schema_version !== 1 || body.offset !== offset
                            || body.event_count !== shift.events.length || !Array.isArray(body.entries)
                            || !body.entries.length || body.entries.length > 100
                            || (proof && (body.snapshot_id !== proof.snapshot_id
                                || JSON.stringify(body.shift) !== JSON.stringify(proof.shift)
                                || JSON.stringify(body.identity) !== JSON.stringify(proof.identity)
                                || JSON.stringify(body.projection) !== JSON.stringify(proof.projection)))) {
                            throw new Error("archive_page_invalid");
                        }
                        if (!proof) proof = body;
                        entries = entries.concat(body.entries);
                        if (entries.length > shift.events.length) throw new Error("archive_page_overflow");
                        if (body.next_offset !== null) {
                            if (body.next_offset !== entries.length || entries.length >= shift.events.length) {
                                throw new Error("archive_cursor_invalid");
                            }
                            return page(body.next_offset);
                        }
                        if (entries.length !== shift.events.length) throw new Error("archive_incomplete");
                        return Object.assign({}, proof, {entries: entries});
                    });
                }
                return page(0);
            });
            inFlight = Promise.race([work, deadline]).then(function (proof) {
                allowed();
                if (!proof) return false;
                return options.ledger.confirmArchive(proof).then(function (coverage) {
                    allowed();
                    return options.ledger.compactArchive(proof.shift.local_shift_id).then(function () { return coverage; });
                });
            }).catch(function () { return false; }).finally(function () {
                expired = true;
                clearTimeout(timeout);
                inFlight = null;
            });
            return inFlight;
        }
        function schedule(delay) {
            if (!current()) return;
            clearTimeout(timer);
            timer = setTimeout(function () {
                timer = null;
                refresh().then(function (covered) {
                    if (!current() || (root.navigator && root.navigator.onLine === false)) return;
                    if (covered) { retryMs = 5000; schedule(); return; }
                    return options.ledger.snapshot().then(function (state) {
                        if (current() && options.ledger.archiveCandidates(state).length) {
                            schedule(retryMs);
                            retryMs = Math.min(60000, retryMs * 2);
                        }
                    });
                }).catch(function () {});
            }, typeof delay === "number" ? delay : 1000);
        }
        return {refresh: refresh, schedule: schedule, stop: function () {
            stopped = true;
            clearTimeout(timer);
        }};
    }
    root.createExcavatorShiftArchive = create;
    if (typeof module !== "undefined") module.exports = create;
})(typeof window !== "undefined" ? window : globalThis);
