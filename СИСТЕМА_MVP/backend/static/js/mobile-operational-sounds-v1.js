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
        "shift_end",
        "assignment_notice",
        "action_success_notice",
        "assignment_removed_notice",
        "shift_notice",
        "action_failed_notice",
        "connection_lost_notice",
        "connection_restored_notice"
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
    var connectionLossStableMs = positiveDatasetNumber("connectionLossStableMs");
    var connectionRecoveryStableMs = positiveDatasetNumber("connectionRecoveryStableMs");
    var connectionAlertCooldownMs = positiveDatasetNumber("connectionAlertCooldownMs");
    var connectionStorageKey = "mobile-operational-sounds:" + profile + ":connection";
    var connectionIncident = readConnectionIncident();
    var connectionIncidentOpen = connectionIncident.active;
    var connectionLossAnnounced = connectionIncident.announced;
    var lastConnectionLossAnnouncedAt = connectionIncident.lossAt;
    var connectionLossTimer = null;
    var connectionRecoveryTimer = null;

    function positiveDatasetNumber(name) {
        var value = Number(script && script.dataset ? script.dataset[name] : 0);
        return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
    }

    function readConnectionIncident() {
        var empty = {active: false, announced: false, lossAt: 0};
        if (!connectionAlertCooldownMs) return empty;
        try {
            var stored = JSON.parse(window.localStorage.getItem(connectionStorageKey) || "null");
            var lossAt = Number(stored && stored.lossAt);
            if (!Number.isFinite(lossAt) || lossAt <= 0 || Date.now() - lossAt > 6 * 60 * 60 * 1000) {
                return empty;
            }
            return {
                active: stored.active === true,
                announced: stored.active === true && stored.announced === true,
                lossAt: lossAt
            };
        } catch (error) {
            return empty;
        }
    }

    function storeConnectionIncident() {
        if (!connectionAlertCooldownMs) return;
        try {
            window.localStorage.setItem(connectionStorageKey, JSON.stringify({
                active: connectionIncidentOpen,
                announced: connectionLossAnnounced,
                lossAt: lastConnectionLossAnnouncedAt
            }));
        } catch (error) {
            // Storage can be unavailable in a locked-down WebView. In-memory gating still works.
        }
    }

    function clearConnectionTimer(name) {
        var timer = name === "loss" ? connectionLossTimer : connectionRecoveryTimer;
        if (timer === null) return;
        window.clearTimeout(timer);
        if (name === "loss") connectionLossTimer = null;
        else connectionRecoveryTimer = null;
    }

    function announceConnectionLoss() {
        connectionLossTimer = null;
        if (lastConnectionState !== "lost" || connectionIncidentOpen) return;
        connectionIncidentOpen = true;
        var now = Date.now();
        var cooldownActive = connectionAlertCooldownMs > 0 && lastConnectionLossAnnouncedAt > 0
            && now - lastConnectionLossAnnouncedAt < connectionAlertCooldownMs;
        connectionLossAnnounced = !cooldownActive;
        if (connectionLossAnnounced) {
            lastConnectionLossAnnouncedAt = now;
            announceOperational({
                cue: "connection_lost",
                voice: "voice_connection_lost"
            });
        }
        storeConnectionIncident();
    }

    function confirmConnectionRecovery() {
        connectionRecoveryTimer = null;
        if (lastConnectionState !== "ok") return;
        var shouldAnnounce = connectionIncidentOpen && connectionLossAnnounced;
        var nativePlugin = capacitorNativeSoundPlugin();
        connectionIncidentOpen = false;
        connectionLossAnnounced = false;
        storeConnectionIncident();
        if (shouldAnnounce) {
            announceOperational({
                cue: "connection_restored",
                voice: "voice_connection_restored"
            });
        } else if (profile === "excavator" && nativePlugin && typeof nativePlugin.announceOperational === "function") {
            // A background native heartbeat may have announced the loss while WebView was paused.
            // Ask the shared native gate to close that incident; it stays silent when none is open.
            Promise.resolve(nativePlugin.announceOperational({
                cue: "connection_restored",
                voice: "voice_connection_restored",
                cueResolved: false,
                eventVersion: 0,
                eventKey: ""
            })).catch(function () {});
        }
    }

    var directCueAliases = Object.freeze({
        truck_assigned: "assignment_notice",
        action_ok: "action_success_notice",
        action_error: "action_failed_notice",
        connection_lost: "connection_lost_notice",
        connection_restored: "connection_restored_notice",
        shift_start: "shift_notice",
        shift_end: "shift_notice"
    });

    function directCueName(name) {
        name = String(name || "");
        return directCueAliases[name] || name;
    }

    function operationalCueName(cueName, voiceName) {
        voiceName = String(voiceName || "");
        if (voiceName === "voice_shift_opened" || voiceName === "voice_shift_closed") {
            return "shift_notice";
        }
        if (voiceName === "voice_connection_lost") return "connection_lost_notice";
        if (voiceName === "voice_connection_restored") return "connection_restored_notice";
        if (voiceName === "voice_assignment_removed" || voiceName === "voice_truck_removed") {
            return "assignment_removed_notice";
        }
        if ([
            "voice_action_failed",
            "voice_trip_finish_failed",
            "voice_truck_send_failed"
        ].indexOf(voiceName) >= 0) {
            return "action_failed_notice";
        }
        if ([
            "voice_trip_finished",
            "voice_downtime_started",
            "voice_downtime_finished",
            "voice_face_settings_saved",
            "voice_truck_sent"
        ].indexOf(voiceName) >= 0) {
            return "action_success_notice";
        }
        if ([
            "voice_excavator_assigned",
            "voice_excavator_changed",
            "voice_truck_assigned"
        ].indexOf(voiceName) >= 0) {
            return "assignment_notice";
        }
        return directCueName(cueName);
    }

    function equipmentCueName(cueName, action) {
        action = String(action || "");
        if (action === "excavator_truck_removed") return "assignment_removed_notice";
        if (action === "excavator_truck_sent") return "action_success_notice";
        if (action === "excavator_truck_assigned" || action === "driver_excavator_assigned") {
            return "assignment_notice";
        }
        return directCueName(cueName);
    }

    function equipmentBatchCueName(cueName, items) {
        var sawAssignment = false;
        var sawRemoval = false;
        var sawSuccess = false;
        (items || []).forEach(function (item) {
            var action = String(item && item.action || "");
            if (action === "excavator_truck_removed") sawRemoval = true;
            else if (action === "excavator_truck_sent") sawSuccess = true;
            else if (action === "excavator_truck_assigned" || action === "driver_excavator_assigned") {
                sawAssignment = true;
            }
        });
        if (sawRemoval && !sawAssignment && !sawSuccess) return "assignment_removed_notice";
        if (sawSuccess && !sawAssignment && !sawRemoval) return "action_success_notice";
        if (sawAssignment || sawRemoval || sawSuccess) return "assignment_notice";
        return directCueName(cueName);
    }

    function capacitorNativeSoundPlugin() {
        var capacitor = window.Capacitor;
        var plugins = capacitor && capacitor.Plugins;
        return plugins && plugins.NativeSound ? plugins.NativeSound : null;
    }

    function nativeSoundPlugin() {
        var plugin = capacitorNativeSoundPlugin();
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
        var webName = directCueName(name);
        var plugin = nativeSoundPlugin();
        if (plugin) {
            return Promise.resolve(plugin.play({name: name})).then(function () {
                return true;
            }).catch(function () {
                return playWebSound(webName);
            });
        }
        return playWebSound(webName);
    }

    function announceDumpPoint(details) {
        var plugin = capacitorNativeSoundPlugin();
        if (!plugin || typeof plugin.announceDumpPoint !== "function") {
            return Promise.resolve({supported: false, announced: false});
        }
        return Promise.resolve(plugin.announceDumpPoint(details || {})).then(function (result) {
            return {
                supported: true,
                announced: !!(result && result.announced === true),
                reason: result && result.reason ? String(result.reason) : ""
            };
        }).catch(function (error) {
            return {
                supported: true,
                announced: false,
                reason: "bridge_error",
                error: error && error.message ? String(error.message) : ""
            };
        });
    }

    function announceOperational(details) {
        details = details || {};
        var requestedCue = String(details.cue || "action_ok");
        var voiceName = String(details.voice || "");
        var fallbackCue = details._cueIsResolved === true
            ? requestedCue
            : operationalCueName(requestedCue, voiceName);
        var plugin = capacitorNativeSoundPlugin();
        function fallback() {
            return play(fallbackCue).then(function (played) {
                return {supported: false, announced: played};
            });
        }
        if (!plugin || typeof plugin.announceOperational !== "function") {
            return fallback();
        }
        var pending;
        try {
            pending = plugin.announceOperational({
                cue: requestedCue,
                voice: voiceName,
                cueResolved: details._cueIsResolved === true,
                eventVersion: Number(details.eventVersion || 0),
                eventKey: String(details.eventKey || "")
            });
        } catch (error) {
            return fallback();
        }
        return Promise.resolve(pending).then(function (result) {
            return {
                supported: true,
                announced: !!(result && result.announced === true),
                reason: result && result.reason ? String(result.reason) : ""
            };
        }).catch(function () {
            return play(fallbackCue).then(function (played) {
                return {supported: true, announced: played, reason: "bridge_error"};
            });
        });
    }

    function announceEquipment(details) {
        details = details || {};
        var requestedCue = String(details.cue || "truck_assigned");
        var fallbackCue = details._cueIsResolved === true
            ? requestedCue
            : equipmentCueName(requestedCue, details.action);
        var plugin = capacitorNativeSoundPlugin();
        function fallback() {
            return announceOperational({
                cue: fallbackCue,
                _cueIsResolved: true,
                voice: String(details.fallbackVoice || "voice_truck_assigned"),
                eventVersion: Number(details.eventVersion || 0),
                eventKey: String(details.eventKey || "")
            });
        }
        if (!plugin || typeof plugin.announceEquipment !== "function") {
            return fallback();
        }
        var pending;
        try {
            pending = plugin.announceEquipment({
                cue: requestedCue,
                cueResolved: details._cueIsResolved === true,
                action: String(details.action || ""),
                equipmentNumber: String(details.equipmentNumber || ""),
                dumpPointId: Number(details.dumpPointId || 0),
                dumpPointName: String(details.dumpPointName || ""),
                eventVersion: Number(details.eventVersion || 0),
                eventKey: String(details.eventKey || "")
            });
        } catch (error) {
            return fallback();
        }
        return Promise.resolve(pending).then(function (result) {
            if (result && result.announced === true) {
                return {supported: true, announced: true, reason: String(result.reason || "")};
            }
            return fallback();
        }).catch(fallback);
    }

    function announceEquipmentBatch(details) {
        details = details || {};
        var items = Array.isArray(details.items) ? details.items.filter(Boolean) : [];
        if (!items.length) {
            return Promise.resolve({supported: true, announced: false, reason: "resource_unavailable"});
        }
        var plugin = capacitorNativeSoundPlugin();
        var requestedCue = String(details.cue || "truck_assigned");
        var fallbackCue = equipmentBatchCueName(requestedCue, items);
        function fallback() {
            var first = items[0] || {};
            return announceEquipment({
                cue: fallbackCue,
                _cueIsResolved: true,
                action: String(first.action || ""),
                equipmentNumber: String(first.equipmentNumber || ""),
                fallbackVoice: String(first.fallbackVoice || "voice_truck_assigned"),
                eventVersion: Number(first.eventVersion || 0),
                eventKey: String(details.eventKey || "excavator_operator_assignment")
            });
        }
        if (!plugin || typeof plugin.announceEquipmentBatch !== "function") {
            return fallback();
        }
        var pending;
        try {
            pending = plugin.announceEquipmentBatch({
                cue: requestedCue,
                items: items.map(function (item) {
                    return {
                        action: String(item.action || ""),
                        equipmentNumber: String(item.equipmentNumber || ""),
                        fallbackVoice: String(item.fallbackVoice || "voice_truck_assigned"),
                        eventVersion: Number(item.eventVersion || 0),
                        operationKey: String(item.operationKey || "")
                    };
                })
            });
        } catch (error) {
            return fallback();
        }
        return Promise.resolve(pending).then(function (result) {
            if (result && result.announced === true) {
                return {supported: true, announced: true, reason: String(result.reason || "")};
            }
            if (result && String(result.reason || "") === "already_announced") {
                return {supported: true, announced: false, reason: "already_announced"};
            }
            return fallback();
        }).catch(fallback);
    }

    function diagnostics() {
        var plugin = capacitorNativeSoundPlugin();
        if (!plugin || typeof plugin.getDiagnostics !== "function") {
            return Promise.resolve({supported: false});
        }
        return Promise.resolve(plugin.getDiagnostics()).then(function (result) {
            result = result || {};
            result.supported = true;
            return result;
        }).catch(function (error) {
            return {
                supported: true,
                error: error && error.message ? String(error.message) : "bridge_error"
            };
        });
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
        if (nextState === lastConnectionState) return;
        lastConnectionState = nextState;
        if (nextState === "lost") {
            clearConnectionTimer("recovery");
            if (connectionIncidentOpen || connectionLossTimer !== null) return;
            if (connectionLossStableMs > 0) {
                connectionLossTimer = window.setTimeout(announceConnectionLoss, connectionLossStableMs);
            } else {
                announceConnectionLoss();
            }
            return;
        }
        clearConnectionTimer("loss");
        if (nextState !== "ok") {
            clearConnectionTimer("recovery");
            return;
        }
        if (!connectionIncidentOpen && !(profile === "excavator" && capacitorNativeSoundPlugin())) return;
        if (connectionRecoveryStableMs > 0) {
            clearConnectionTimer("recovery");
            connectionRecoveryTimer = window.setTimeout(confirmConnectionRecovery, connectionRecoveryStableMs);
        } else {
            confirmConnectionRecovery();
        }
    });

    window.MobileOperationalSounds = Object.freeze({
        profile: profile,
        files: soundFiles,
        play: play,
        announceDumpPoint: announceDumpPoint,
        announceOperational: announceOperational,
        announceEquipment: announceEquipment,
        announceEquipmentBatch: announceEquipmentBatch,
        diagnostics: diagnostics,
        preload: unlock
    });
})();
