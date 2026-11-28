(function () {
    "use strict";
    const script = document.currentScript;
    if (!script || !script.dataset.dutyShift || window.DispatcherDutyRecorder) return;
    const shift = script.dataset.dutyShift;
    const endpoint = script.dataset.presenceUrl;
    // Each document is a different observation source, including duplicated tabs.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
    const id = [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
    let sequence = 0;
    let timer;
    function attach(params) {
        params.set("duty_shift", shift);
        params.set("duty_window", id);
        params.set("duty_sequence", String(++sequence));
    }
    window.DispatcherDutyRecorder = {attach};
    function send(state) {
        if (navigator.onLine === false) return;
        const token = document.querySelector('meta[name="csrf-token"]')?.content;
        if (!token) return;
        const params = new URLSearchParams();
        attach(params);
        params.set("csrfmiddlewaretoken", token);
        params.set("duty_state", state);
        if (navigator.sendBeacon && navigator.sendBeacon(endpoint, params)) return;
        fetch(endpoint, {method: "POST", body: params, credentials: "same-origin", keepalive: true}).catch(() => {});
    }
    function visibility() {
        clearInterval(timer);
        if (document.hidden) {
            send("hidden");
            timer = setInterval(() => send("hidden"), 60000);
        }
    }
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", () => { clearInterval(timer); send("closed"); });
    window.addEventListener("pageshow", visibility);
    window.addEventListener("online", visibility);
    if (document.hidden) visibility();
})();
