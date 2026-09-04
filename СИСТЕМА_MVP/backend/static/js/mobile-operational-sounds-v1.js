(function () {
    "use strict";

    var script = document.currentScript;
    var profile = script && script.dataset.mobileSoundProfile
        ? String(script.dataset.mobileSoundProfile).trim()
        : "";
    var baseUrl = script && script.dataset.mobileSoundBase
        ? script.dataset.mobileSoundBase
        : "/static/audio/" + profile + "/";
    var soundNames = Object.freeze([
        "truck_assigned",
        "action_ok",
        "action_error",
        "connection_lost",
        "connection_restored",
        "shift_start",
        "shift_end"
    ]);
    var soundFiles = Object.freeze(soundNames.reduce(function (files, name) {
        files[name] = profile + "_" + name + ".wav";
        return files;
    }, {}));
    var audioContext = null;
    var decodedBuffers = Object.create(null);
    var loadingBuffers = Object.create(null);
    var activeSource = null;
    var lastConnectionState = "";

    function nativeSoundPlugin() {
        var capacitor = window.Capacitor;
        var plugins = capacitor && capacitor.Plugins;
        var plugin = plugins && plugins.NativeSound;
        return plugin && typeof plugin.play === "function" ? plugin : null;
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

    function decodeAudioData(context, arrayBuffer) {
        return new Promise(function (resolve, reject) {
            var settled = false;
            function finish(value) {
                if (settled) return;
                settled = true;
                resolve(value);
            }
            function fail(error) {
                if (settled) return;
                settled = true;
                reject(error);
            }
            try {
                var result = context.decodeAudioData(arrayBuffer, finish, fail);
                if (result && typeof result.then === "function") result.then(finish, fail);
            } catch (error) {
                fail(error);
            }
        });
    }

    function loadWebSound(name) {
        var context = getAudioContext();
        if (!context || !soundFiles[name]) return Promise.reject(new Error("Sound is unavailable"));
        if (decodedBuffers[name]) return Promise.resolve(decodedBuffers[name]);
        if (loadingBuffers[name]) return loadingBuffers[name];
        loadingBuffers[name] = fetch(baseUrl + soundFiles[name], {
            credentials: "same-origin",
            cache: "force-cache"
        }).then(function (response) {
            if (!response.ok) throw new Error("Sound request failed: " + response.status);
            return response.arrayBuffer();
        }).then(function (arrayBuffer) {
            return decodeAudioData(context, arrayBuffer);
        }).then(function (buffer) {
            decodedBuffers[name] = buffer;
            delete loadingBuffers[name];
            return buffer;
        }, function (error) {
            delete loadingBuffers[name];
            throw error;
        });
        return loadingBuffers[name];
    }

    function startWebSound(context, buffer) {
        if (activeSource) {
            try { activeSource.stop(); } catch (error) {}
            activeSource = null;
        }
        var source = context.createBufferSource();
        var gain = context.createGain();
        gain.gain.value = 1;
        source.buffer = buffer;
        source.connect(gain);
        gain.connect(context.destination);
        source.addEventListener("ended", function () {
            if (activeSource === source) activeSource = null;
        }, {once: true});
        activeSource = source;
        source.start(0);
        return true;
    }

    function playWebSound(name) {
        var context = getAudioContext();
        if (!context) return Promise.resolve(false);
        var resume = context.state === "suspended" ? context.resume() : Promise.resolve();
        return Promise.resolve(resume).then(function () {
            return loadWebSound(name);
        }).then(function (buffer) {
            if (context.state !== "running") return false;
            return startWebSound(context, buffer);
        }).catch(function () {
            return false;
        });
    }

    function play(name) {
        if (!soundFiles[name]) return Promise.resolve(false);
        var plugin = nativeSoundPlugin();
        if (plugin) {
            return Promise.resolve(plugin.play({name: name})).then(function () {
                return true;
            }).catch(function () {
                return playWebSound(name);
            });
        }
        return playWebSound(name);
    }

    function unlock() {
        var context = getAudioContext();
        if (!context) return;
        var resume = context.state === "suspended" ? context.resume() : Promise.resolve();
        Promise.resolve(resume).then(function () {
            soundNames.forEach(function (name) {
                loadWebSound(name).catch(function () {});
            });
        }).catch(function () {});
    }

    ["pointerdown", "touchstart", "keydown"].forEach(function (eventName) {
        document.addEventListener(eventName, unlock, {capture: true, passive: true});
    });

    window.addEventListener("operational-state-connection", function () {
        var nextState = document.body && document.body.dataset.connectionState
            ? document.body.dataset.connectionState
            : "";
        if (!nextState) return;
        if (!lastConnectionState) {
            lastConnectionState = nextState;
            return;
        }
        if (nextState === lastConnectionState) return;
        var previousState = lastConnectionState;
        lastConnectionState = nextState;
        if (nextState === "lost") {
            play("connection_lost");
        } else if (previousState === "lost" && nextState === "ok") {
            play("connection_restored");
        }
    });

    window.MobileOperationalSounds = Object.freeze({
        profile: profile,
        files: soundFiles,
        play: play,
        preload: unlock
    });
})();
