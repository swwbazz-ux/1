(function (window, document) {
    "use strict";

    if (window.DispatcherSounds) return;

    var STORAGE_KEY = "dispatcher-sound-enabled-v1";
    var MAX_EVENT_KEYS = 128;
    var enabled = readEnabled();
    var audioContext = null;
    var lastConnectionState = document.body && document.body.dataset
        ? String(document.body.dataset.connectionState || "")
        : "";
    var seenEventKeys = [];
    var seenEventKeySet = Object.create(null);
    var lastErrorSignature = "";
    var lastErrorAt = 0;

    var patterns = Object.freeze({
        enabled: [
            {at: 0, frequency: 698, duration: 0.13, gain: 0.100, type: "triangle"},
            {at: 0.16, frequency: 1047, duration: 0.18, gain: 0.120, type: "triangle"}
        ],
        attention: [
            {at: 0, frequency: 880, duration: 0.20, gain: 0.180, type: "triangle"},
            {at: 0.27, frequency: 1175, duration: 0.22, gain: 0.220, type: "triangle"},
            {at: 0.56, frequency: 880, duration: 0.26, gain: 0.200, type: "triangle"}
        ],
        error: [
            {at: 0, frequency: 440, duration: 0.24, gain: 0.150, type: "square"},
            {at: 0.32, frequency: 330, duration: 0.25, gain: 0.165, type: "square"},
            {at: 0.66, frequency: 220, duration: 0.30, gain: 0.180, type: "square"}
        ],
        connection_lost: [
            {at: 0, frequency: 523, duration: 0.25, gain: 0.160, type: "triangle"},
            {at: 0.34, frequency: 349, duration: 0.27, gain: 0.180, type: "triangle"},
            {at: 0.71, frequency: 220, duration: 0.32, gain: 0.200, type: "triangle"}
        ],
        connection_restored: [
            {at: 0, frequency: 523, duration: 0.15, gain: 0.110, type: "triangle"},
            {at: 0.19, frequency: 659, duration: 0.17, gain: 0.125, type: "triangle"},
            {at: 0.41, frequency: 784, duration: 0.22, gain: 0.145, type: "triangle"}
        ]
    });

    function readEnabled() {
        try {
            return window.localStorage.getItem(STORAGE_KEY) !== "false";
        } catch (error) {
            return true;
        }
    }

    function writeEnabled(value) {
        try {
            window.localStorage.setItem(STORAGE_KEY, value ? "true" : "false");
        } catch (error) {
            // A locked-down browser can deny storage; sound still works for this page.
        }
    }

    function getAudioContext() {
        if (audioContext) return audioContext;
        var AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) return null;
        try {
            audioContext = new AudioContextClass();
        } catch (error) {
            audioContext = null;
        }
        return audioContext;
    }

    function unlock() {
        if (!enabled) return Promise.resolve(false);
        var context = getAudioContext();
        if (!context) return Promise.resolve(false);
        if (context.state !== "suspended" || typeof context.resume !== "function") {
            return Promise.resolve(context.state !== "closed");
        }
        return Promise.resolve(context.resume()).then(function () {
            return context.state !== "suspended" && context.state !== "closed";
        }).catch(function () {
            return false;
        });
    }

    function renderTone(context, tone, origin) {
        var start = origin + Number(tone.at || 0);
        var duration = Math.max(0.04, Number(tone.duration || 0.1));
        var gainValue = Math.max(0.001, Number(tone.gain || 0.04));
        var oscillator = context.createOscillator();
        var gain = context.createGain();
        oscillator.type = ["sine", "triangle", "square"].indexOf(tone.type) >= 0
            ? tone.type
            : "triangle";
        oscillator.frequency.setValueAtTime(Number(tone.frequency || 660), start);
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(gainValue, start + 0.012);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
        oscillator.connect(gain);
        gain.connect(context.destination);
        oscillator.start(start);
        oscillator.stop(start + duration + 0.02);
    }

    function play(name) {
        if (!enabled || !patterns[name]) return Promise.resolve(false);
        return unlock().then(function (ready) {
            var context = getAudioContext();
            if (!ready || !context || context.state === "closed") return false;
            var origin = Number(context.currentTime || 0) + 0.01;
            patterns[name].forEach(function (tone) {
                renderTone(context, tone, origin);
            });
            return true;
        }).catch(function () {
            return false;
        });
    }

    function updateButtons() {
        document.querySelectorAll("[data-dispatcher-sound-toggle]").forEach(function (button) {
            button.setAttribute("aria-pressed", enabled ? "true" : "false");
            button.dataset.soundEnabled = enabled ? "true" : "false";
            button.title = enabled ? "Звуковые оповещения включены" : "Звуковые оповещения выключены";
            button.setAttribute(
                "aria-label",
                enabled ? "Выключить звуковые оповещения" : "Включить звуковые оповещения"
            );
            var mutedMark = button.querySelector("[data-dispatcher-sound-muted-mark]");
            if (mutedMark) mutedMark.style.display = enabled ? "none" : "";
        });
    }

    function setEnabled(value, announce) {
        enabled = value === true;
        writeEnabled(enabled);
        updateButtons();
        if (!enabled || announce === false) return Promise.resolve(enabled);
        return unlock().then(function () {
            return play("enabled");
        }).then(function () {
            return enabled;
        });
    }

    function rememberEventKey(key) {
        if (!key || seenEventKeySet[key]) return false;
        seenEventKeySet[key] = true;
        seenEventKeys.push(key);
        while (seenEventKeys.length > MAX_EVENT_KEYS) {
            delete seenEventKeySet[seenEventKeys.shift()];
        }
        return true;
    }

    function eventKey(event, index, contextVersion) {
        if (event && Number(event.version) > 0) return "v:" + Number(event.version);
        return [
            "e",
            contextVersion || 0,
            index,
            event && event.type || "",
            event && event.object_type || "",
            event && event.object_id || ""
        ].join(":");
    }

    function onRefreshApplied(event) {
        var detail = event && event.detail ? event.detail : {};
        if (detail.role && detail.role !== "dispatcher") return;
        var events = Array.isArray(detail.events) ? detail.events : [];
        var hasNewEvent = false;
        events.forEach(function (item, index) {
            if (rememberEventKey(eventKey(item, index, detail.version))) hasNewEvent = true;
        });
        if (!hasNewEvent && detail.eventsTruncated === true) {
            hasNewEvent = rememberEventKey("truncated:" + String(detail.version || 0));
        }
        if (hasNewEvent) play("attention");
    }

    function onConnectionChange(event) {
        var detail = event && event.detail ? event.detail : {};
        if (detail.role && detail.role !== "dispatcher") return;
        var nextState = String(detail.state || "");
        var previousState = lastConnectionState || String(detail.previousState || "");
        if (!nextState || nextState === lastConnectionState) return;
        lastConnectionState = nextState;
        if (nextState === "lost") {
            play("connection_lost");
        } else if (previousState === "lost" && (nextState === "ok" || nextState === "recovering")) {
            play("connection_restored");
        }
    }

    function onActionError(event) {
        var detail = event && event.detail ? event.detail : {};
        var signature = String(detail.code || "") + "\n" + String(detail.message || "");
        var now = Date.now();
        if (signature && signature === lastErrorSignature && now - lastErrorAt < 1200) return;
        lastErrorSignature = signature;
        lastErrorAt = now;
        play("error");
    }

    function onDocumentClick(event) {
        var target = event.target && event.target.closest
            ? event.target.closest("[data-dispatcher-sound-toggle]")
            : null;
        if (!target) return;
        event.preventDefault();
        setEnabled(!enabled, true);
    }

    ["pointerdown", "keydown"].forEach(function (eventName) {
        document.addEventListener(eventName, unlock, {capture: true, passive: true});
    });
    document.addEventListener("click", onDocumentClick);
    window.addEventListener("operational-state-refresh-applied", onRefreshApplied);
    window.addEventListener("operational-state-connection", onConnectionChange);
    window.addEventListener("dispatcher-action-error", onActionError);
    window.addEventListener("pageshow", updateButtons);
    window.addEventListener("operational-state-refresh-applied", updateButtons);

    updateButtons();

    window.DispatcherSounds = Object.freeze({
        play: play,
        preload: unlock,
        setEnabled: setEnabled,
        isEnabled: function () { return enabled; },
        storageKey: STORAGE_KEY
    });
})(window, document);
