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

    window.excavatorHaptic = vibrate;
})(window);
