/* Dispatcher request transport and durable offline queue.
   The legacy queue key and payload format are intentionally preserved so
   actions queued by the production runtime remain recoverable after upgrade. */
(function (global) {
    "use strict";

    var DISPATCHER_SYNC_QUEUE_KEY = "mining-master-mobile-sync-queue-v3";
    var DISPATCHER_SYNC_REQUEST_TIMEOUT_MS = 12000;
    var DISPATCHER_REFRESH_REQUEST_TIMEOUT_MS = 12000;

    function createDispatcherTransport(options) {
        options = options || {};
        var getCsrfToken = typeof options.getCsrfToken === "function"
            ? options.getCsrfToken
            : function () { return ""; };
        var onServerError = typeof options.onServerError === "function"
            ? options.onServerError
            : function () {};
        var onStateChange = typeof options.onStateChange === "function"
            ? options.onStateChange
            : function () {};
        var fetchRequest = typeof options.fetch === "function"
            ? options.fetch
            : function (url, fetchOptions) { return global.fetch(url, fetchOptions); };
        var syncPendingCount = 0;
        var syncQueueFlushing = false;
        var syncFlushTimer = null;
        var realtimeConnected = true;
        var realtimeLastSuccessAt = 0;
        var realtimeLastReason = "";

        try {
            // Очередь v1 не содержала версии назначения. Повторять такие команды
            // после обновления опасно: они могли быть сформированы до более нового
            // решения диспетчера.
            global.localStorage.removeItem("mining-master-mobile-sync-queue-v1");
            global.localStorage.removeItem("mining-master-mobile-sync-queue-v2");
        } catch (error) {}

        function notifyStateChange() {
            onStateChange({
                realtimeConnected: realtimeConnected,
                realtimeLastSuccessAt: realtimeLastSuccessAt,
                realtimeLastReason: realtimeLastReason
            });
        }

        function readQueue() {
            try {
                return JSON.parse(global.localStorage.getItem(DISPATCHER_SYNC_QUEUE_KEY) || "[]");
            } catch (error) {
                return [];
            }
        }

        function writeQueue(queue) {
            try {
                global.localStorage.setItem(DISPATCHER_SYNC_QUEUE_KEY, JSON.stringify(queue || []));
            } catch (error) {}
            notifyStateChange();
        }

        function getQueueState() {
            var queue = readQueue();
            var now = Date.now();
            var oldestAgeMs = 0;
            queue.forEach(function (item) {
                var createdAt = Number(item && item.createdAt ? item.createdAt : 0);
                var age = createdAt ? Math.max(0, now - createdAt) : 0;
                if (!oldestAgeMs || age > oldestAgeMs) oldestAgeMs = age;
            });
            return {
                length: queue.length,
                oldestAgeMs: oldestAgeMs,
                isFlushing: syncQueueFlushing,
                pendingCount: syncPendingCount
            };
        }

        function setSyncPending(isPending) {
            syncPendingCount = Math.max(0, syncPendingCount + (isPending ? 1 : -1));
            notifyStateChange();
        }

        function roleIsReadonly() {
            return (
                typeof global.isAppRoleReadonly === "function"
                && global.isAppRoleReadonly()
            );
        }

        function inactiveRoleError() {
            var error = new Error("Роль неактивна — доступен только просмотр");
            error.isServerResponse = true;
            error.code = "inactive_role";
            return error;
        }

        function enqueue(request, delayMs) {
            if (roleIsReadonly()) {
                return false;
            }
            var queue = readQueue();
            var queuedRequest = Object.assign({
                id: "sync-" + Date.now() + "-" + Math.random().toString(16).slice(2),
                createdAt: Date.now(),
                attempts: 0
            }, request || {});
            var replaceIndex = queuedRequest.coalesceKey ? queue.findIndex(function (item, index) {
                return item.coalesceKey === queuedRequest.coalesceKey && !(syncQueueFlushing && index === 0);
            }) : -1;
            if (replaceIndex >= 0) {
                queuedRequest.createdAt = queue[replaceIndex].createdAt || queuedRequest.createdAt;
                queue[replaceIndex] = queuedRequest;
            } else {
                queue.push(queuedRequest);
            }
            writeQueue(queue);
            scheduleFlush(delayMs);
            return true;
        }

        function send(request) {
            if (roleIsReadonly()) {
                return Promise.reject(inactiveRoleError());
            }
            var headers = { "X-CSRFToken": getCsrfToken() };
            var body = null;
            var controller = global.AbortController ? new global.AbortController() : null;
            var timeoutId = null;
            if (request.kind === "form") {
                body = new global.FormData();
                Object.keys(request.fields || {}).forEach(function (key) {
                    body.append(key, request.fields[key]);
                });
            } else {
                headers["Content-Type"] = "application/json";
                body = JSON.stringify(request.data || {});
            }
            if (controller) {
                timeoutId = global.setTimeout(function () {
                    try {
                        controller.abort();
                    } catch (error) {}
                }, DISPATCHER_SYNC_REQUEST_TIMEOUT_MS);
            }
            return fetchRequest(request.url, {
                method: "POST",
                headers: headers,
                body: body,
                credentials: "same-origin",
                cache: "no-store",
                signal: controller ? controller.signal : undefined
            }).then(function (response) {
                if (!response.ok) {
                    return response.json().catch(function () { return {}; }).then(function (payload) {
                        var error = new Error(payload.error || "Действие не выполнено.");
                        error.isServerResponse = true;
                        error.code = payload.code || "";
                        error.conflict = Boolean(payload.conflict);
                        error.status = response.status;
                        throw error;
                    });
                }
                return response.json().catch(function () { return { ok: true }; });
            }).finally(function () {
                if (timeoutId) {
                    global.clearTimeout(timeoutId);
                }
            });
        }

        function fetchWithTimeout(url, options, timeoutMs) {
            var controller = global.AbortController ? new global.AbortController() : null;
            var timeoutId = null;
            var fetchOptions = Object.assign({}, options || {});
            if (controller) {
                fetchOptions.signal = controller.signal;
                timeoutId = global.setTimeout(function () {
                    try {
                        controller.abort();
                    } catch (error) {}
                }, timeoutMs || DISPATCHER_REFRESH_REQUEST_TIMEOUT_MS);
            }
            return fetchRequest(url, fetchOptions).finally(function () {
                if (timeoutId) {
                    global.clearTimeout(timeoutId);
                }
            });
        }

        function flush() {
            if (roleIsReadonly()) {
                notifyStateChange();
                return;
            }
            if (syncQueueFlushing) {
                notifyStateChange();
                return;
            }
            var queue = readQueue();
            if (!queue.length) {
                notifyStateChange();
                return;
            }
            syncQueueFlushing = true;
            setSyncPending(true);
            var request = queue[0];
            request.attempts = (request.attempts || 0) + 1;
            send(request).then(function () {
                var freshQueue = readQueue();
                if (freshQueue.length && freshQueue[0].id === request.id) {
                    freshQueue.shift();
                } else {
                    freshQueue = freshQueue.filter(function (item) {
                        return item.id !== request.id;
                    });
                }
                writeQueue(freshQueue);
            }).catch(function (error) {
                if (error && error.isServerResponse) {
                    var freshQueue = readQueue().filter(function (item) {
                        return item.id !== request.id;
                    });
                    writeQueue(freshQueue);
                    onServerError(error);
                } else {
                    var retryQueue = readQueue();
                    if (retryQueue.length && retryQueue[0].id === request.id) {
                        retryQueue[0].attempts = request.attempts;
                        writeQueue(retryQueue);
                    }
                }
            }).finally(function () {
                syncQueueFlushing = false;
                setSyncPending(false);
                if (readQueue().length) {
                    global.setTimeout(flush, 1200);
                }
            });
        }

        function scheduleFlush(delayMs) {
            notifyStateChange();
            if (syncFlushTimer) {
                global.clearTimeout(syncFlushTimer);
            }
            syncFlushTimer = global.setTimeout(function () {
                syncFlushTimer = null;
                flush();
            }, typeof delayMs === "number" ? delayMs : 80);
        }

        function post(url, data, postOptions) {
            if (roleIsReadonly()) {
                return Promise.reject(inactiveRoleError());
            }
            var payload = Object.assign({}, data || {});
            postOptions = postOptions || {};
            if (!payload.client_action_id) {
                payload.client_action_id = "mm-" + Date.now() + "-" + Math.random().toString(16).slice(2);
            }
            var request = {
                kind: "json",
                url: url,
                data: payload
            };
            setSyncPending(true);
            return send(request).catch(function (error) {
                if (error && error.isServerResponse) throw error;
                if (postOptions.queueOnNetworkFailure === false) throw error;
                enqueue(request);
                return { queued: true };
            }).finally(function () {
                setSyncPending(false);
            });
        }

        function updateRealtimeConnection(detail) {
            detail = detail || {};
            realtimeConnected = detail.connected !== false;
            realtimeLastReason = detail.reason || "";
            if (realtimeConnected) {
                realtimeLastSuccessAt = detail.lastSuccessAt || Date.now();
                scheduleFlush(0);
            }
            notifyStateChange();
        }

        function getDebugState() {
            return {
                realtimeConnected: realtimeConnected,
                realtimeLastSuccessAt: realtimeLastSuccessAt,
                realtimeLastReason: realtimeLastReason,
                syncQueue: getQueueState()
            };
        }

        return {
            queueKey: DISPATCHER_SYNC_QUEUE_KEY,
            readQueue: readQueue,
            getQueueState: getQueueState,
            refreshState: notifyStateChange,
            roleIsReadonly: roleIsReadonly,
            inactiveRoleError: inactiveRoleError,
            enqueue: enqueue,
            send: send,
            fetchWithTimeout: fetchWithTimeout,
            flush: flush,
            scheduleFlush: scheduleFlush,
            post: post,
            updateRealtimeConnection: updateRealtimeConnection,
            getDebugState: getDebugState
        };
    }

    global.createDispatcherTransport = createDispatcherTransport;
})(window);
