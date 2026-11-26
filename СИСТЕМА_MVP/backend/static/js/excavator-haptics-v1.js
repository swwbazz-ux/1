(function excavatorHapticsModule(window) {
    "use strict";

    function normalizedPattern(pattern) {
        var values = Array.isArray(pattern) ? pattern : [pattern];
        return values.map(function (value) {
            return Math.max(0, Math.min(5000, Math.round(Number(value) || 0)));
        }).slice(0, 31);
    }

    function webVibrate(pattern) {
        if (!window.navigator || typeof window.navigator.vibrate !== "function") return false;
        try { return window.navigator.vibrate(pattern.length === 1 ? pattern[0] : pattern) === true; } catch (error) { return false; }
    }

    function vibrate(pattern, amplitude) {
        var normalized = normalizedPattern(pattern);
        var strength = Math.max(1, Math.min(255, Number(amplitude) || 255));
        var plugin = window.Capacitor
            && window.Capacitor.Plugins
            && window.Capacitor.Plugins.NativeHaptics;
        if (plugin && typeof plugin.vibrate === "function") {
            try {
                var result = plugin.vibrate({pattern: normalized, amplitude: strength});
                if (result && typeof result.catch === "function") {
                    result.catch(function () { webVibrate(normalized); });
                }
                return true;
            } catch (error) {}
        }
        return webVibrate(normalized);
    }

    var clickContext;
    var clickBuffer;

    function playControlClick() {
        var AudioContext = window.AudioContext || window.webkitAudioContext;
        if (!AudioContext) return;
        if (!clickContext) clickContext = new AudioContext();
        if (!clickBuffer) {
            var rate = clickContext.sampleRate;
            clickBuffer = clickContext.createBuffer(1, Math.round(rate * 0.09), rate);
            var samples = clickBuffer.getChannelData(0);
            for (var index = 0; index < samples.length; index += 1) {
                var seconds = index / rate;
                // A sharp transient and a lower knock, both shaped to avoid a hard cut.
                var attack = Math.min(seconds / 0.0015, 1);
                var release = Math.min((samples.length - index - 1) / (rate * 0.008), 1);
                var snap = 0.58 * Math.exp(-seconds / 0.012) * Math.sin(2 * Math.PI * 2450 * seconds);
                var knock = 0.42 * Math.exp(-seconds / 0.025) * Math.sin(2 * Math.PI * 1330 * seconds);
                samples[index] = attack * release * (snap + knock);
            }
        }
        function start() {
            var source = clickContext.createBufferSource();
            source.buffer = clickBuffer;
            source.connect(clickContext.destination);
            source.start();
        }
        if (clickContext.state === "running") start();
        else clickContext.resume().then(start).catch(function () {});
    }

    function roundControlFeedback(options) {
        vibrate([145, 45, 110], 255);
        if (options && options.sound === false) return;
        try { playControlClick(); } catch (error) {}
    }

    window.excavatorRoundControlFeedback = roundControlFeedback;
    window.excavatorHaptic = vibrate;
})(window);
