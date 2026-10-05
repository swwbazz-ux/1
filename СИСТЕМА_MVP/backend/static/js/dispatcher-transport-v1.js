/* Общий транспорт диспетчера и горного мастера. Исходная команда живёт в
   отдельной записи журнала; v3 остаётся совместимой проекцией очереди. */
(function (global) {
    "use strict";

    var DISPATCHER_SYNC_QUEUE_KEY = "mining-master-mobile-sync-queue-v3";
    var DISPATCHER_COMMAND_PREFIX = DISPATCHER_SYNC_QUEUE_KEY + ":command:";
    var DISPATCHER_SYNC_REQUEST_TIMEOUT_MS = 12000;
    var DISPATCHER_REFRESH_REQUEST_TIMEOUT_MS = 12000;

    function createDispatcherTransport(options) {
        options = options || {};
        var getCsrfToken = options.getCsrfToken || function () { return ""; };
        var onServerError = options.onServerError || function () {};
        var onStateChange = options.onStateChange || function () {};
        var onAcknowledged = options.onAcknowledged || function () {};
        var fetchRequest = options.fetch || function (url, init) { return global.fetch(url, init); };
        var getCommandContext = options.getCommandContext || function () {
            var shell = global.document && (global.document.querySelector(".mm-mobile-shell[data-dispatcher-command-access-id]")
                || global.document.querySelector(".dispatcher-board[data-dispatcher-command-access-id]")
                || global.document.querySelector("[data-dispatcher-theme]"));
            var data = shell && shell.dataset || {};
            return {
                actor_id: String(data.dispatcherCommandActorId || ""),
                access_id: String(data.dispatcherCommandAccessId || ""),
                role: String(data.dispatcherCommandRole || ""),
                shift_id: String(data.dispatcherCommandShiftId || "")
            };
        };
        var syncPendingCount = 0;
        var syncQueueFlushing = false;
        var syncFlushTimer = null;
        var syncFlushDueAt = 0;
        var realtimeConnected = true;
        var realtimeLastSuccessAt = 0;
        var realtimeLastReason = "";
        var inFlight = Object.create(null);
        var waitingForDependencies = Object.create(null);
        var lastStorageError = null;
        var receiptInFlight = null;
        var receiptNextCheck = Object.create(null);

        // Старые v1/v2 не имеют достаточного контекста для автоповтора.
        // Сохраняем их без изменений: обновление не вправе уничтожать исходник.
        function copy(value) { return JSON.parse(JSON.stringify(value)); }
        function storageError() {
            var error = new Error("Не удалось сохранить действие на устройстве. Освободите место и повторите.");
            error.code = "storage_unavailable";
            return error;
        }
        function roleIsReadonly() {
            return typeof global.isAppRoleReadonly === "function" && global.isAppRoleReadonly();
        }
        function inactiveRoleError() {
            var error = new Error("Роль неактивна — доступен только просмотр");
            error.isServerResponse = true;
            error.code = "inactive_role";
            return error;
        }
        function notifyStateChange() {
            onStateChange({
                realtimeConnected: realtimeConnected,
                realtimeLastSuccessAt: realtimeLastSuccessAt,
                realtimeLastReason: realtimeLastReason,
                syncQueue: getQueueState()
            });
        }
        function setSyncPending(pending) {
            syncPendingCount = Math.max(0, syncPendingCount + (pending ? 1 : -1));
            notifyStateChange();
        }
        function recordKey(id) { return DISPATCHER_COMMAND_PREFIX + encodeURIComponent(id); }
        function readRecord(id) {
            var raw = global.localStorage.getItem(recordKey(id));
            if (!raw) return null;
            var record = JSON.parse(raw);
            if (!record || !record.request || !record.delivery) throw storageError();
            return record;
        }
        function saveRecord(record) {
            var key = recordKey(record.request.id);
            var serialized = JSON.stringify(record);
            global.localStorage.setItem(key, serialized);
            if (global.localStorage.getItem(key) !== serialized) throw storageError();
        }
        function readQueue() {
            var legacy = JSON.parse(global.localStorage.getItem(DISPATCHER_SYNC_QUEUE_KEY) || "[]");
            if (!Array.isArray(legacy)) throw storageError();
            var records = Object.create(null);
            for (var index = 0; index < global.localStorage.length; index += 1) {
                var key = global.localStorage.key(index);
                if (!key || key.indexOf(DISPATCHER_COMMAND_PREFIX) !== 0) continue;
                var record = JSON.parse(global.localStorage.getItem(key));
                if (!record || !record.request || !record.delivery) throw storageError();
                records[record.request.id] = record;
            }
            var queue = legacy.filter(function (request) { return request && !records[request.id]; });
            Object.keys(records).forEach(function (id) {
                var record = records[id];
                // Запрет автоповтора записан ДО первого HTTP, поэтому действует
                // и если quota/обрыв не дали записать последующий статус held.
                if (record.delivery.state !== "pending" || record.request.autoRetry === false) return;
                queue.push(Object.assign({}, record.request, {
                    attempts: record.delivery.attempts || 0,
                    nextAttemptAt: record.delivery.nextAttemptAt || 0,
                    lastError: record.delivery.lastError || null
                }));
            });
            return queue.sort(function (left, right) { return (left.createdAt || 0) - (right.createdAt || 0); });
        }
        function mirrorQueue() {
            // Журнал — источник восстановления даже при quota/обрыве до mirror.
            try { global.localStorage.setItem(DISPATCHER_SYNC_QUEUE_KEY, JSON.stringify(readQueue())); } catch (error) {}
        }
        function owns(request) {
            var current = getCommandContext() || {};
            var author = request.author || {};
            return String(author.access_id || "") === String(current.access_id || "")
                && String(author.role || "") === String(current.role || "");
        }
        function receiptSupported(request) {
            return request && request.kind === "json" && typeof request.id === "string" && request.id
                && request.data && typeof request.data.client_action_id === "string"
                && /^(\/mining-master\/assignments|\/dispatcher\/control)\/(excavator\/move|truck\/assign)\/$/.test(request.url);
        }
        function incompleteContext(request) {
            var author = request.author || {};
            return !author.actor_id || !author.access_id || !author.role || !author.shift_id || !request.occurredAt;
        }
        function receiptOwnerPossible(request) {
            var current = getCommandContext() || {}, author = request.author || {};
            if (!current.actor_id || !current.access_id || !current.role) return false;
            return ["actor_id", "access_id", "role"].every(function (field) {
                return !author[field] || String(author[field]) === String(current[field]);
            });
        }
        function receiptCandidates() {
            var candidates = Object.create(null), collisions = Object.create(null);
            function add(request, sourceKey, delivery) {
                if (!receiptSupported(request)) return;
                var id = request.id, prior = candidates[id];
                if (prior && JSON.stringify(prior.request) !== JSON.stringify(request)) collisions[id] = true;
                if (!prior) candidates[id] = {request: request, sourceKey: sourceKey, delivery: delivery};
            }
            // Не переписываем и не удаляем ни один legacy-массив.
            ["mining-master-mobile-sync-queue-v1", "mining-master-mobile-sync-queue-v2", DISPATCHER_SYNC_QUEUE_KEY].forEach(function (key) {
                var raw = global.localStorage.getItem(key);
                if (!raw) return;
                try {
                    var items = JSON.parse(raw);
                    if (!Array.isArray(items)) throw storageError();
                    items.forEach(function (request) {
                        var saved = request && request.id && readRecord(request.id);
                        // v3 — изменяемая проекция; исходник уже находится в journal.
                        if (saved && key === DISPATCHER_SYNC_QUEUE_KEY) return;
                        add(request, key, null);
                    });
                } catch (error) { lastStorageError = storageError(); }
            });
            for (var index = 0; index < global.localStorage.length; index += 1) {
                var key = global.localStorage.key(index);
                if (!key || key.indexOf(DISPATCHER_COMMAND_PREFIX) !== 0) continue;
                try {
                    var record = JSON.parse(global.localStorage.getItem(key));
                    if (!record || !record.request || !record.delivery) throw storageError();
                    add(record.request, key, record.delivery);
                    if (candidates[record.request.id]) candidates[record.request.id].delivery = record.delivery;
                } catch (error) { lastStorageError = storageError(); }
            }
            return Object.keys(candidates).filter(function (id) {
                var candidate = candidates[id], delivery = candidate.delivery;
                return !collisions[id] && (!delivery || delivery.state === "held" || delivery.state === "pending")
                    && (!delivery || delivery.state === "held" || candidate.request.autoRetry === false || incompleteContext(candidate.request))
                    && receiptOwnerPossible(candidate.request);
            }).map(function (id) { return candidates[id]; });
        }
        function reconcileReceipt() {
            if (roleIsReadonly()) return Promise.resolve();
            if (receiptInFlight) return receiptInFlight;
            var candidates;
            try { candidates = receiptCandidates(); } catch (error) {
                lastStorageError = storageError(); return Promise.resolve();
            }
            var owner = getCommandContext() || {};
            var scope = String(owner.access_id) + ":" + String(owner.role) + ":";
            var candidate = candidates.find(function (item) {
                return !inFlight[item.request.id] && (!receiptNextCheck[scope + item.request.id] || receiptNextCheck[scope + item.request.id] <= Date.now());
            });
            if (!candidate) {
                if (candidates.length) scheduleFlush(Math.max(80, Math.min.apply(null, candidates.map(function (item) {
                    return (receiptNextCheck[scope + item.request.id] || Date.now() + 1200) - Date.now();
                }))));
                return Promise.resolve();
            }
            var request = copy(candidate.request), checkKey = scope + request.id;
            receiptNextCheck[checkKey] = Date.now() + 30000;
            receiptInFlight = withDeadline(function (signal) {
                return fetchRequest("/assignments/commands/receipt/", {
                    method: "POST", credentials: "same-origin", cache: "no-store", signal: signal,
                    headers: {"Content-Type": "application/json", "X-CSRFToken": getCsrfToken()},
                    body: JSON.stringify(request)
                }).then(function (response) {
                    if (!response.ok) return null;
                    return response.json();
                });
            }, DISPATCHER_SYNC_REQUEST_TIMEOUT_MS).then(function (result) {
                if (!result || result.ok !== true || result.status !== "acknowledged"
                    || !result.receipt || result.receipt.ok !== true || !result.evidence
                    || result.evidence.client_action_id !== request.data.client_action_id.trim()
                    || String(result.evidence.actor_id) !== String(owner.actor_id)) return;
                var existing = readRecord(request.id);
                if (existing && JSON.stringify(existing.request) !== JSON.stringify(request)) return;
                if (existing && existing.delivery.state === "acknowledged") return;
                // Повторная проверка защищает исходник от замены во время HTTP.
                if (!receiptCandidates().some(function (item) {
                    return JSON.stringify(item.request) === JSON.stringify(request);
                })) return;
                var record = existing || {request: request, delivery: {}};
                record.delivery = Object.assign({}, record.delivery, {
                    state: "acknowledged", acknowledgedAt: Date.now(), receipt: result.receipt,
                    reconciliation: {source: "server_receipt", evidence: result.evidence}
                });
                // ACK пишется отдельно; legacy-массивы остаются побайтно прежними.
                try { saveRecord(record); } catch (error) { throw storageError(); }
                lastStorageError = null;
                try { onAcknowledged(record.request, result.receipt); } catch (callbackError) {}
            }).catch(function (error) {
                if (error.code === "storage_unavailable" || error.name === "QuotaExceededError") lastStorageError = storageError();
            }).finally(function () {
                receiptInFlight = null;
                notifyStateChange();
                scheduleFlush(80);
            });
            return receiptInFlight;
        }
        function getQueueState() {
            var queue = [];
            try { queue = readQueue(); } catch (error) { lastStorageError = storageError(); }
            var now = Date.now();
            return {
                length: queue.length,
                oldestAgeMs: queue.reduce(function (age, request) {
                    return Math.max(age, request.createdAt ? now - request.createdAt : 0);
                }, 0),
                isFlushing: syncQueueFlushing,
                pendingCount: syncPendingCount,
                waitingDependencyCount: Object.keys(waitingForDependencies).length,
                heldForAuthorCount: queue.filter(function (request) { return !owns(request); }).length,
                storageError: lastStorageError ? lastStorageError.code : ""
            };
        }
        function assignmentCommand(request) {
            return receiptSupported(request) && /\/truck\/assign\/$/.test(request.url)
                && ["assign", "release"].indexOf(request.data.action) !== -1 && request.data.truck_id;
        }
        function predecessorId(request) {
            var value = request.data && request.data.expected_assignment_state_id;
            return typeof value === "string" && value.indexOf("command:") === 0 ? value.slice(8) : "";
        }
        function sameAssignmentScope(left, right) {
            return assignmentCommand(left) && assignmentCommand(right) && left.url === right.url
                && String(left.data.truck_id) === String(right.data.truck_id)
                && ["actor_id", "access_id", "role", "shift_id"].every(function (key) {
                    return String((left.author || {})[key] || "") === String((right.author || {})[key] || "");
                });
        }
        function journalRecords() {
            var records = [];
            for (var index = 0; index < global.localStorage.length; index += 1) {
                var key = global.localStorage.key(index);
                if (!key || key.indexOf(DISPATCHER_COMMAND_PREFIX) !== 0) continue;
                var record = JSON.parse(global.localStorage.getItem(key));
                if (!record || !record.request || !record.delivery) throw storageError();
                records.push(record);
            }
            return records;
        }
        function assignmentRecords(request) {
            return journalRecords().filter(function (record) {
                return record.request.id !== request.id && sameAssignmentScope(request, record.request);
            });
        }
        function massCommand(request) {
            return receiptSupported(request) && (
                /\/excavator\/move\/$/.test(request.url) && request.data.zone === "inactive"
                || /\/truck\/assign\/$/.test(request.url) && request.data.action === "release_complex");
        }
        function assignmentProbe(request, truckId) {
            return Object.assign({}, request, {
                url: request.url.replace(/excavator\/move\/$/, "truck/assign/"),
                data: {action: "assign", truck_id: truckId, client_action_id: request.data.client_action_id}
            });
        }
        function linkMassAssignments(request) {
            if (!massCommand(request) || incompleteContext(request)) return;
            var states = request.data.expected_assignment_states;
            if (!states || typeof states !== "object" || Array.isArray(states)) return;
            var truckIds = [];
            journalRecords().forEach(function (record) {
                if (!assignmentCommand(record.request) || record.delivery.state !== "pending"
                        || record.request.autoRetry === false) return;
                var truckId = String(record.request.data.truck_id);
                if (sameAssignmentScope(assignmentProbe(request, truckId), record.request)
                        && truckIds.indexOf(truckId) === -1) truckIds.push(truckId);
            });
            var dependencies = (request.data.assignment_dependencies || []).slice();
            truckIds.forEach(function (truckId) {
                var probe = assignmentProbe(request, truckId);
                probe.data.expected_assignment_state_id = "0";
                linkAssignment(probe);
                var ident = predecessorId(probe);
                if (!ident) return;
                if (!dependencies.some(function (item) {
                    return String(item.truck_id) === truckId && item.client_action_id === ident;
                })) dependencies.push({truck_id: truckId, client_action_id: ident});
                if (Object.prototype.hasOwnProperty.call(states, truckId) && /^\d+$/.test(String(states[truckId]))) {
                    states[truckId] = "command:" + ident;
                }
            });
            if (dependencies.length > 256) throw storageError();
            if (dependencies.length) request.data.assignment_dependencies = dependencies;
        }
        function dependencyRefs(request) {
            if (assignmentCommand(request) && predecessorId(request)) {
                return [{truck_id: request.data.truck_id, client_action_id: predecessorId(request)}];
            }
            if (!massCommand(request)) return [];
            var refs = (request.data.assignment_dependencies || []).slice();
            var states = request.data.expected_assignment_states || {};
            Object.keys(states).forEach(function (truckId) {
                if (typeof states[truckId] === "string" && states[truckId].indexOf("command:") === 0) {
                    refs.push({truck_id: truckId, client_action_id: states[truckId].slice(8)});
                }
            });
            return refs;
        }
        function linkAssignment(request) {
            if (!assignmentCommand(request) || incompleteContext(request)
                    || !/^\d+$/.test(String(request.data.expected_assignment_state_id))) return;
            var pending = assignmentRecords(request).filter(function (record) {
                return record.delivery.state === "pending" && record.request.autoRetry !== false;
            });
            var tails = pending.filter(function (record) {
                return !pending.some(function (child) {
                    return predecessorId(child.request) === record.request.data.client_action_id;
                });
            });
            if (tails.length > 1 || pending.length && !tails.length) {
                var error = new Error("Порядок сохранённых распоряжений не определён. Дождитесь подтверждения.");
                error.code = "command_dependency_conflict";
                throw error;
            }
            if (tails.length) request.data.expected_assignment_state_id = "command:" + tails[0].request.data.client_action_id;
        }
        function dependencyReady(request) {
            var refs = dependencyRefs(request);
            if (!refs.length) return true;
            var records = journalRecords();
            return refs.every(function (ref) {
                var probe = assignmentProbe(request, ref.truck_id);
                var parents = records.filter(function (record) {
                    return record.request.id !== request.id && sameAssignmentScope(probe, record.request)
                        && record.request.data.client_action_id === ref.client_action_id;
                });
                return parents.length === 1 && parents[0].delivery.state === "acknowledged"
                    && parents[0].delivery.receipt && parents[0].delivery.receipt.ok === true;
            });
        }
        function waitForDependencies(request) {
            if (waitingForDependencies[request.id]) return waitingForDependencies[request.id];
            var timer = null, stopped = false;
            setSyncPending(true);
            var waiting = withDeadline(function () {
                return new Promise(function (resolve, reject) {
                    function check() {
                        if (stopped) return;
                        try {
                            if (roleIsReadonly()) throw inactiveRoleError();
                            if (!owns(request)) {
                                var error = new Error("Действие сохранено за исходным сотрудником.");
                                error.code = "command_author_mismatch";
                                throw error;
                            }
                            var saved = readRecord(request.id);
                            if (saved && saved.delivery.state !== "pending" || dependencyReady(request)) {
                                stopped = true; resolve(); return;
                            }
                        } catch (error) { stopped = true; reject(error); return; }
                        timer = global.setTimeout(check, 500);
                    }
                    check();
                });
            }, DISPATCHER_SYNC_REQUEST_TIMEOUT_MS).then(function () {
                // send заново проверяет роль, исходник и статус перед HTTP.
                return send(request);
            }).catch(function (error) {
                try {
                    var record = readRecord(request.id);
                    // Квитанция могла прийти из другой вкладки или read-only
                    // сверки на границе тайм-аута. Не откатываем доказанный успех.
                    if (record && record.delivery.state === "acknowledged" && owns(request) && !roleIsReadonly()) {
                        return record.delivery.receipt;
                    }
                    if (record && record.delivery.state === "pending") {
                        record.delivery.state = "held";
                        record.delivery.lastError = {code: error.code || "dependency_error", message: error.message || ""};
                        saveRecord(record);
                    }
                } catch (storageFailure) { lastStorageError = storageError(); }
                throw error;
            }).finally(function () {
                stopped = true;
                if (timer) global.clearTimeout(timer);
                delete waitingForDependencies[request.id];
                setSyncPending(false);
            });
            waitingForDependencies[request.id] = waiting;
            scheduleFlush(0);
            return waiting;
        }
        function prepare(request) {
            var prepared = copy(request || {});
            prepared.id = prepared.id || "sync-" + Date.now() + "-" + Math.random().toString(16).slice(2);
            var saved = readRecord(prepared.id);
            prepared.createdAt = prepared.createdAt || saved && saved.request.createdAt || Date.now();
            prepared.occurredAt = prepared.occurredAt || saved && saved.request.occurredAt || new Date(prepared.createdAt).toISOString();
            prepared.author = copy(prepared.author || getCommandContext() || {});
            if (saved && !request.author && owns(saved.request)) prepared.author = copy(saved.request.author);
            delete prepared.attempts;
            delete prepared.nextAttemptAt;
            delete prepared.lastError;
            // Повтор сохранения не пересчитывает ссылку по изменившемуся журналу.
            if (!saved) { linkAssignment(prepared); linkMassAssignments(prepared); }
            return prepared;
        }
        function persist(request) {
            // Проверяем читаемость старой очереди, не заменяя повреждённый исходник [].
            readQueue();
            var existing = readRecord(request.id);
            if (existing) {
                if (JSON.stringify(existing.request) !== JSON.stringify(request)) {
                    var error = new Error("Идентификатор действия уже используется другой командой.");
                    error.code = "command_id_reused";
                    throw error;
                }
                return existing;
            }
            var record = {request: request, delivery: {state: "pending", attempts: 0}};
            saveRecord(record);
            lastStorageError = null;
            mirrorQueue();
            return record;
        }
        function enqueue(request, delayMs) {
            if (roleIsReadonly()) return false;
            try { persist(prepare(request)); } catch (error) {
                lastStorageError = error.code ? error : storageError();
                notifyStateChange();
                return false;
            }
            scheduleFlush(delayMs);
            return true;
        }
        function withDeadline(operation, timeoutMs) {
            var controller = global.AbortController ? new global.AbortController() : null;
            var startedAt = Date.now();
            var timer;
            function deadlineError() {
                var error = new Error("Время ожидания ответа истекло. Действие сохранено на устройстве.");
                error.code = "request_timeout";
                return error;
            }
            if (timeoutMs <= 0) return Promise.reject(deadlineError());
            var timeout = new Promise(function (resolve, reject) {
                timer = global.setTimeout(function () {
                    // Сначала отвергаем собственный promise: некоторые bridge игнорируют abort.
                    reject(deadlineError());
                    if (controller) { try { controller.abort(); } catch (abortError) {} }
                }, timeoutMs);
            });
            return Promise.race([
                Promise.resolve().then(function () { return operation(controller && controller.signal); }),
                timeout
            ]).then(function (result) {
                // Синхронный JSON.parse способен задержать callback таймера.
                if (Date.now() - startedAt >= timeoutMs) throw deadlineError();
                return result;
            }).finally(function () { global.clearTimeout(timer); });
        }
        function requestPayload(request) {
            var headers = {"X-CSRFToken": getCsrfToken(), "X-Command-Context": JSON.stringify({
                version: 1, id: request.id, author: request.author || {}, occurred_at: request.occurredAt || ""
            })};
            var body;
            if (request.kind === "form") {
                body = new global.FormData();
                Object.keys(request.fields || {}).forEach(function (key) { body.append(key, request.fields[key]); });
            } else {
                headers["Content-Type"] = "application/json";
                body = JSON.stringify(request.data || {});
            }
            return withDeadline(function (signal) {
                return fetchRequest(request.url, {
                    method: "POST", headers: headers, body: body,
                    credentials: "same-origin", cache: "no-store", signal: signal
                }).then(function (response) {
                    return response.json().catch(function (error) {
                        if (response.ok) throw error;
                        return {};
                    }).then(function (payload) {
                        if (!response.ok || !payload || payload.ok !== true) {
                            var error = new Error(payload && payload.error || "Действие не подтверждено сервером.");
                            error.isServerResponse = !response.ok;
                            error.code = payload && payload.code || "unconfirmed_response";
                            error.conflict = Boolean(payload && payload.conflict);
                            error.status = response.status;
                            // Структурированный 400 означает неверную исходную команду.
                            // Конфликт 409, авторизация и временный 5xx не терминальны.
                            error.isTerminalResponse = response.status === 400 && payload && payload.ok === false;
                            error.responsePayload = payload;
                            throw error;
                        }
                        return payload;
                    });
                });
            }, DISPATCHER_SYNC_REQUEST_TIMEOUT_MS);
        }
        function fetchWithTimeout(url, init, timeoutMs) {
            // Читаем тело до освобождения запроса: таймер и abort охватывают весь HTTP.
            var startedAt = Date.now();
            var timeout = timeoutMs || DISPATCHER_REFRESH_REQUEST_TIMEOUT_MS;
            return withDeadline(function (signal) {
                return fetchRequest(url, Object.assign({}, init || {}, {signal: signal})).then(function (response) {
                    return response.text().then(function (body) { return {response: response, body: body}; });
                });
            }, timeout).then(function (result) {
                var response = result.response;
                var wrapper = {};
                ["ok", "status", "statusText", "url", "headers", "redirected", "type"].forEach(function (key) {
                    wrapper[key] = response[key];
                });
                wrapper.text = function () { return Promise.resolve(result.body); };
                wrapper.json = function () {
                    return withDeadline(function () { return JSON.parse(result.body); }, timeout - (Date.now() - startedAt));
                };
                return wrapper;
            });
        }
        function send(request) {
            if (roleIsReadonly()) return Promise.reject(inactiveRoleError());
            if (receiptSupported(request) && incompleteContext(request)) {
                var contextError = new Error("Старая команда сохранена для проверки серверной квитанции.");
                contextError.code = "command_context_incomplete";
                return Promise.reject(contextError);
            }
            if (!owns(request)) {
                var error = new Error("Действие сохранено за исходным сотрудником.");
                error.code = "command_author_mismatch";
                return Promise.reject(error);
            }
            if (inFlight[request.id]) return inFlight[request.id];
            var record;
            try {
                record = readRecord(request.id);
                if (!record) {
                    // Legacy без автора не получает нынешнего автора при переносе.
                    var legacy = copy(request);
                    delete legacy.attempts;
                    delete legacy.nextAttemptAt;
                    delete legacy.lastError;
                    record = persist(legacy);
                }
                if (record.delivery.state === "acknowledged") return Promise.resolve(record.delivery.receipt);
                if (record.delivery.state !== "pending") {
                    var heldError = new Error(record.delivery.lastError && record.delivery.lastError.message || "Результат сохранённого действия пока не подтверждён.");
                    heldError.isServerResponse = true;
                    heldError.code = "command_" + record.delivery.state;
                    return Promise.reject(heldError);
                }
                if (!dependencyReady(record.request)) {
                    if (massCommand(record.request) && record.request.autoRetry === false) return waitForDependencies(record.request);
                    var dependencyError = new Error("Действие сохранено и ожидает подтверждения предыдущего распоряжения.");
                    dependencyError.code = "command_dependency_pending";
                    return Promise.reject(dependencyError);
                }
                record.delivery.attempts = (record.delivery.attempts || 0) + 1;
                // Исходник уже записан. Сбой служебного счётчика не превращает
                // сохранённое действие в якобы несохранённое и не блокирует сеть.
                try { saveRecord(record); } catch (attemptError) { lastStorageError = storageError(); }
            } catch (error) { return Promise.reject(storageError()); }
            setSyncPending(true);
            var sending = requestPayload(record.request).then(function (payload) {
                record.delivery = {
                    state: "acknowledged", attempts: record.delivery.attempts,
                    acknowledgedAt: Date.now(), receipt: payload
                };
                // ACK сначала в журнал. Не удалось записать — исходник остаётся на повтор.
                saveRecord(record);
                mirrorQueue();
                try { onAcknowledged(record.request, payload); } catch (callbackError) {}
                return payload;
            }).catch(function (error) {
                var original = readRecord(request.id);
                if (original && original.delivery.state !== "acknowledged") {
                    original.delivery.nextAttemptAt = Date.now() + Math.min(30000, 1200 * Math.pow(2, Math.min(original.delivery.attempts - 1, 5)));
                    original.delivery.lastError = {code: error.code || "network_error", status: error.status || 0, message: error.message || ""};
                    if (error.isTerminalResponse) {
                        original.delivery.state = "rejected";
                        original.delivery.receipt = error.responsePayload;
                    } else if (original.request.autoRetry === false) {
                        // Старые structural-вызовы откатывают UI после ошибки. Не
                        // исполняем потом в фоне то, что интерфейс показал отменённым.
                        // Исходник сохранён для серверного согласования, не удалён.
                        original.delivery.state = "held";
                    }
                    try { saveRecord(original); } catch (storageFailure) { lastStorageError = storageError(); }
                }
                mirrorQueue();
                throw error;
            }).finally(function () {
                delete inFlight[request.id];
                setSyncPending(false);
                scheduleFlush(1200);
            });
            inFlight[request.id] = sending;
            return sending;
        }
        function flushCommands() {
            if (roleIsReadonly() || syncQueueFlushing) { notifyStateChange(); return Promise.resolve(); }
            var queue;
            try { queue = readQueue(); } catch (error) {
                lastStorageError = storageError(); notifyStateChange(); return Promise.resolve();
            }
            function replayable(item) {
                return owns(item) && item.autoRetry !== false && !(receiptSupported(item) && incompleteContext(item));
            }
            var request = queue.find(function (item) {
                return replayable(item)
                    && dependencyReady(item) && !inFlight[item.id] && (!item.nextAttemptAt || item.nextAttemptAt <= Date.now());
            });
            if (!request) {
                if (queue.some(replayable)) scheduleFlush(1200);
                notifyStateChange();
                return Promise.resolve();
            }
            syncQueueFlushing = true;
            return send(request).catch(function (error) {
                if (error.isServerResponse && (!request.lastError || request.lastError.code !== error.code || request.lastError.status !== error.status)) onServerError(error);
            }).finally(function () {
                syncQueueFlushing = false;
                notifyStateChange();
            });
        }
        function flush() {
            return Promise.all([flushCommands(), reconcileReceipt()]);
        }
        function scheduleFlush(delayMs) {
            notifyStateChange();
            var delay = typeof delayMs === "number" ? delayMs : 80;
            var dueAt = Date.now() + delay;
            // Долгая сверка квитанции не отодвигает уже назначенную отправку.
            if (syncFlushTimer && syncFlushDueAt <= dueAt) return;
            if (syncFlushTimer) global.clearTimeout(syncFlushTimer);
            syncFlushDueAt = dueAt;
            syncFlushTimer = global.setTimeout(function () { syncFlushTimer = null; syncFlushDueAt = 0; flush(); }, delay);
        }
        // Синхронная граница сохранения для локального UI: вернуть команду
        // можно только после проверенной записи исходника; HTTP здесь нет.
        function storePost(url, data, postOptions) {
            if (roleIsReadonly()) throw inactiveRoleError();
            var payload = copy(data || {});
            if (!payload.client_action_id) payload.client_action_id = "mm-" + Date.now() + "-" + Math.random().toString(16).slice(2);
            var request;
            try {
                request = prepare({id: "sync-" + payload.client_action_id, kind: "json", url: url, data: payload,
                    autoRetry: !(postOptions && postOptions.queueOnNetworkFailure === false)});
                persist(request);
            } catch (error) { throw error.code ? error : storageError(); }
            return request;
        }
        function post(url, data, postOptions) {
            var request;
            try { request = storePost(url, data, postOptions); }
            catch (error) { return Promise.reject(error); }
            return send(request).catch(function (error) {
                if (error.isServerResponse || error.code === "storage_unavailable" || error.code === "command_author_mismatch" || postOptions && postOptions.queueOnNetworkFailure === false) throw error;
                return {queued: true};
            });
        }
        function updateRealtimeConnection(detail) {
            detail = detail || {};
            realtimeConnected = detail.connected !== false;
            realtimeLastReason = detail.reason || "";
            if (realtimeConnected) { realtimeLastSuccessAt = detail.lastSuccessAt || Date.now(); scheduleFlush(0); }
            notifyStateChange();
        }
        function getDebugState() {
            return {realtimeConnected: realtimeConnected, realtimeLastSuccessAt: realtimeLastSuccessAt,
                realtimeLastReason: realtimeLastReason, syncQueue: getQueueState()};
        }
        return {
            queueKey: DISPATCHER_SYNC_QUEUE_KEY, journalPrefix: DISPATCHER_COMMAND_PREFIX,
            readQueue: readQueue, readOwnQueue: function () { return readQueue().filter(owns); },
            getQueueState: getQueueState, refreshState: notifyStateChange,
            roleIsReadonly: roleIsReadonly, inactiveRoleError: inactiveRoleError, storageError: storageError,
            setSyncPending: setSyncPending, enqueue: enqueue, send: send, fetchWithTimeout: fetchWithTimeout,
            flush: flush, reconcileReceipt: reconcileReceipt, scheduleFlush: scheduleFlush, storePost: storePost, post: post,
            updateRealtimeConnection: updateRealtimeConnection, getDebugState: getDebugState
        };
    }
    global.createDispatcherTransport = createDispatcherTransport;
})(window);
