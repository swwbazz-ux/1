// Small IndexedDB double: isolated read/write transactions, rollback, queued
// requests and version barriers. Runtime tests also exercise it with v380 code.
const clone = value => value === undefined ? undefined : structuredClone(value);
module.exports = function transactionalIndexedDB(options = {}) {
    let stores = new Map(), version = 0, active = null;
    const connections = new Set(), opens = [], transactions = [];
    const api = {options, dump: () => clone(stores), mutate(fn) { fn(stores); }};
    function pumpTransactions() {
        if (active || !transactions.length) return;
        const task = active = transactions.shift();
        task.start();
    }
    function makeTransaction(names, mode) {
        names = Array.isArray(names) ? names : [names];
        let data, pending = 0, ended = false, started = false, timer;
        const jobs = [], tx = {error: null};
        function finish(commit) {
            if (ended) return;
            ended = true; clearImmediate(timer);
            if (commit && mode === 'readwrite') for (const name of names) stores.set(name, data.get(name));
            setImmediate(() => {
                if (commit) tx.oncomplete?.(); else tx.onabort?.();
                active = null; pumpTransactions();
            });
        }
        function settle() {
            clearImmediate(timer);
            timer = setImmediate(() => { if (!pending && !ended && !options.hangTransactions) finish(true); });
        }
        tx.abort = () => {
            if (ended) throw Error('TransactionInactiveError');
            tx.error ||= Error('AbortError'); finish(false);
        };
        tx.objectStore = name => {
            if (!names.includes(name)) throw Error('NotFoundError');
            function request(operation) {
                if (ended) throw Error('TransactionInactiveError');
                pending++;
                const req = {onsuccess: null, onerror: null};
                const run = () => setImmediate(() => {
                    if (ended) return;
                    try {
                        req.result = clone(operation(data.get(name)));
                        req.onsuccess?.({target: req});
                    } catch (error) {
                        req.error = tx.error = error; req.onerror?.({target: req});
                        if (!ended) finish(false);
                    }
                    pending--; settle();
                });
                if (started) run(); else jobs.push(run);
                return req;
            }
            return {
                get: key => request(store => store.get(key)),
                getAll: () => request(store => [...store.values()]),
                put: (value, key) => request(store => {
                    options.failPut?.(name, value, key);
                    if (mode !== 'readwrite') throw Error('ReadOnlyError');
                    key = key === undefined ? value.event_id || value.archive_id : key;
                    store.set(key, clone(value)); return key;
                }),
                delete: key => request(store => { if (mode !== 'readwrite') throw Error('ReadOnlyError'); store.delete(key); }),
            };
        };
        transactions.push({start() {
            started = true;
            options.onTransaction?.(names, mode, tx);
            data = new Map(names.map(name => [name, clone(stores.get(name) || new Map())]));
            jobs.forEach(run => run()); settle();
        }});
        setImmediate(pumpTransactions);
        return tx;
    }
    function connection() {
        let closed = false;
        const db = {
            get version() { return version; },
            objectStoreNames: {contains: name => stores.has(name)},
            createObjectStore(name) { stores.set(name, new Map()); return {createIndex() {}}; },
            transaction(names, mode) { if (closed) throw Error('InvalidStateError'); return makeTransaction(names, mode); },
            close() { closed = true; connections.delete(db); setImmediate(pumpOpens); },
        };
        connections.add(db); return db;
    }
    function pumpOpens() {
        if (!opens.length) return;
        const {request, wanted} = opens[0];
        if (options.hangOpen) return;
        if (wanted < version) {
            opens.shift(); request.error = Object.assign(Error('VersionError'), {name: 'VersionError'});
            request.onerror?.(); setImmediate(pumpOpens); return;
        }
        if (wanted > version && connections.size) {
            for (const db of [...connections]) db.onversionchange?.({newVersion: wanted});
            if (connections.size) { request.onblocked?.(); return; }
        }
        opens.shift();
        request.result = connection();
        if (wanted > version) {
            const before = clone(stores), previous = version;
            let aborted = false;
            request.transaction = {abort() { aborted = true; }};
            request.onupgradeneeded?.();
            if (aborted) {
                stores = before; version = previous; request.result.close();
                request.error = Error('AbortError'); request.onerror?.(); setImmediate(pumpOpens); return;
            }
            version = wanted;
        }
        request.onsuccess?.(); setImmediate(pumpOpens);
    }
    api.open = (name, requested) => {
        const request = {};
        opens.push({request, wanted: requested || version || 1}); setImmediate(pumpOpens); return request;
    };
    return api;
};
