(function () {
    "use strict";
    const script = document.currentScript;
    if (!script?.dataset.owner || window.DispatcherDutyJournal) return;
    window.DispatcherDutyJournal = true;

    const {owner, ticket, serverMs, endpoint} = script.dataset;
    const prefix = "copper.dispatcher.duty.v1:" + owner + ":";
    const grantPrefix = "copper.dispatcher.duty.grant.v1:" + owner + ":";
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
    const windowId = [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
    const started = performance.now();
    const browserStart = Date.now();
    const responseStart = performance.getEntriesByType("navigation")[0]?.responseStart;
    const baseTime = Number(serverMs) + (responseStart > 0 ? Math.max(0, started - responseStart) : 0);
    const memory = new Map();
    const limit = 64;
    let sequence = 0;
    let retired = false;
    let leaving = false;
    let sending = false;
    let changed = false;
    let timer = 0;
    let retry = 2000;
    let sampleTimer = 0;

    function remember(state) {
        if (!ticket || retired) return;
        // Anchor to the grant's server time. A sleeping device adds no intermediate samples.
        const at = Math.floor(baseTime + performance.now() - started);
        const wallAt = Math.floor(baseTime + Date.now() - browserStart);
        const key = prefix + windowId + ":" + (++sequence);
        // Some platforms pause their monotonic clock during sleep. The receiver
        // can use the wall estimate only if it is also within actual server time.
        const value = JSON.stringify(wallAt - at > 5000 ? [at, state, wallAt] : [at, state]);
        try {
            if (!localStorage.getItem(grantPrefix + windowId)) localStorage.setItem(grantPrefix + windowId, ticket);
            localStorage.setItem(key, value);
        } catch (error) {
            memory.set(key, value);
        }
        changed = true;
    }

    function groups() {
        // Independent immutable keys allow one tab to drain a closed tab's queue
        // without overwriting observations another tab is currently recording.
        const keys = new Set(memory.keys());
        try {
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                if (key && key.startsWith(prefix)) keys.add(key);
            }
        } catch (error) { /* Memory still supports delivery within this document. */ }
        const result = new Map();
        for (const key of keys) {
            const [id, number] = key.slice(prefix.length).split(":");
            if (!result.has(id)) result.set(id, []);
            result.get(id).push({key, seq: Number(number)});
        }
        for (const entries of result.values()) entries.sort((a, b) => a.seq - b.seq);
        return result;
    }

    function packet(id, entries) {
        const events = [];
        let grant = id === windowId ? ticket : "";
        if (!grant) {
            try { grant = localStorage.getItem(grantPrefix + id); } catch (error) { /* This window has no stored grant. */ }
        }
        if (!grant) return null;
        for (const entry of entries.slice(0, limit)) {
            let value = memory.get(entry.key);
            if (!value) {
                try { value = localStorage.getItem(entry.key); } catch (error) { /* May be unavailable. */ }
            }
            if (!value) continue; // A concurrent acknowledgement removed this item.
            events.push([entry.seq, ...JSON.parse(value)]);
        }
        const token = document.querySelector('meta[name="csrf-token"]')?.content;
        if (!token || !events.length) return null;
        return new URLSearchParams({window: id, ticket: grant, events: JSON.stringify(events), csrfmiddlewaretoken: token});
    }

    function later(delay) {
        if (!leaving && !timer && navigator.onLine !== false) {
            timer = setTimeout(() => { timer = 0; flush(); }, delay);
        }
    }

    async function deliver(id, entries) {
        const batch = entries.slice(0, limit);
        const body = packet(id, batch);
        if (!body) return false;
        const controller = new AbortController();
        const deadline = setTimeout(() => controller.abort(), 10000);
        try {
            const response = await fetch(endpoint, {method: "POST", credentials: "same-origin", cache: "no-store",
                body, signal: controller.signal});
            if (!response.ok || response.redirected) return false;
            const receipt = await response.json();
            if (receipt.window !== id || !Number.isSafeInteger(receipt.ack) || receipt.ack < batch[0].seq) return false;
            for (const entry of batch) {
                if (entry.seq > receipt.ack) break;
                memory.delete(entry.key);
                try { localStorage.removeItem(entry.key); } catch (error) { /* Replays are idempotent. */ }
            }
            const last = JSON.parse(body.get("events")).at(-1);
            if (last[2] === "closed" && receipt.ack >= last[0] && !groups().has(id)) {
                try { localStorage.removeItem(grantPrefix + id); } catch (error) { /* The event is already acknowledged. */ }
            }
            if (id === windowId && receipt.retired) {
                retired = true;
                clearInterval(sampleTimer);
            }
            return true;
        } finally {
            clearTimeout(deadline);
        }
    }

    async function flush() {
        if (sending || leaving || navigator.onLine === false) return;
        sending = true;
        changed = false;
        let more = false;
        let failed = false;
        try {
            for (const [id, entries] of groups()) {
                try {
                    if (await deliver(id, entries)) more ||= entries.length > limit;
                    else failed = true;
                } catch (error) { failed = true; }
            }
        } finally {
            sending = false;
            if (failed) {
                later(retry);
                retry = Math.min(60000, retry * 2);
            } else {
                retry = 2000;
                if (changed || more) later(0);
            }
        }
    }

    function observe() {
        clearInterval(sampleTimer);
        if (leaving) return;
        if (ticket && !retired) {
            const state = document.hidden ? "hidden" : "visible";
            remember(state);
            sampleTimer = setInterval(() => {
                remember(document.hidden ? "hidden" : "visible");
                later(0);
            }, state === "hidden" ? 60000 : 30000);
        }
        later(0);
    }

    document.addEventListener("visibilitychange", observe);
    window.addEventListener("offline", observe);
    window.addEventListener("online", () => {
        clearTimeout(timer);
        timer = 0;
        retry = 2000;
        observe();
    });
    window.addEventListener("pagehide", event => {
        leaving = true;
        clearInterval(sampleTimer);
        clearTimeout(timer);
        timer = 0;
        remember(event.persisted ? "hidden" : "closed");
        if (ticket && !retired && navigator.onLine !== false && navigator.sendBeacon) {
            const body = packet(windowId, groups().get(windowId) || []);
            if (body) navigator.sendBeacon(endpoint, body); // Only a subsequent server receipt removes it.
        }
    });
    window.addEventListener("pageshow", event => {
        if (event.persisted) { leaving = false; observe(); }
    });
    observe();
})();
