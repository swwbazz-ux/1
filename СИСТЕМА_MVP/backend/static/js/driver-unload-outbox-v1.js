/* Подтверждение привязано к исходному рейсу даже после следующей отгрузки. */
(function (root) {
    "use strict";
    function createOutbox(options) {
        var storage = options.storage;
        var key = "driver-unload-outbox-v1:" + options.accessId;
        var running = null;
        function read() {
            var value = JSON.parse(storage.getItem(key) || "[]");
            if (!Array.isArray(value)) throw new Error("Не удалось прочитать сохранённые разгрузки.");
            return value;
        }
        function write(items) { storage.setItem(key, JSON.stringify(items)); }
        function queue(event) {
            var items = read();
            var existing = items.find(function (item) { return item.trip_id === event.trip_id; });
            if (existing) return existing;
            items.push(event);
            write(items); // Без сохранения не показываем водителю «сохранено».
            return event;
        }
        function flush() {
            if (running) return running;
            running = (async function () {
                var events = read();
                for (var event of events) {
                    if (event.needs_review) continue;
                    try {
                        var result = await options.send(event);
                        if (!result.ok) {
                            if (result.conflict) {
                                write(read().map(function (item) {
                                    return item.client_action_id === event.client_action_id
                                        ? Object.assign({}, item, {needs_review: true, error: result.error}) : item;
                                }));
                                if (options.onConflict) options.onConflict(event, result.error);
                            }
                            break;
                        }
                        if (String(result.trip_id) !== String(event.trip_id)
                            || result.client_action_id !== event.client_action_id) break;
                        write(read().filter(function (item) { return item.client_action_id !== event.client_action_id; }));
                        if (options.onConfirmed) await options.onConfirmed(event);
                    } catch (error) { break; } // Связь вернётся — повторим с тем же ключом.
                }
                return read();
            })().finally(function () { running = null; });
            return running;
        }
        return {queue: queue, flush: flush, pending: read};
    }
    root.createDriverUnloadOutbox = createOutbox;
    if (typeof module !== "undefined") module.exports = createOutbox;
})(typeof window !== "undefined" ? window : globalThis);
