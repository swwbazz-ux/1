package ru.copperresources.mobile;

import android.media.AudioAttributes;
import android.media.MediaPlayer;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

@CapacitorPlugin(name = "NativeSound")
public class NativeSoundPlugin extends Plugin {
    private static final Set<String> ALLOWED_SOUNDS = new HashSet<>(Arrays.asList(
        "truck_assigned",
        "action_ok",
        "action_error",
        "connection_lost",
        "connection_restored",
        "shift_start",
        "shift_end"
    ));
    private static final Set<String> ALLOWED_VOICES = new HashSet<>(Arrays.asList(
        "voice_shift_opened",
        "voice_shift_closed",
        "voice_downtime_started",
        "voice_downtime_finished",
        "voice_action_failed",
        "voice_connection_lost",
        "voice_connection_restored",
        "voice_excavator_assigned",
        "voice_excavator_changed",
        "voice_assignment_removed",
        "voice_trip_finished",
        "voice_trip_finish_failed",
        "voice_truck_assigned",
        "voice_truck_removed",
        "voice_face_settings_saved",
        "voice_truck_sent",
        "voice_truck_send_failed"
    ));

    private MediaPlayer activePlayer;

    @PluginMethod
    public void play(PluginCall call) {
        String soundName = call.getString("name", "");
        if (!ALLOWED_SOUNDS.contains(soundName)) {
            call.reject("Unknown sound");
            return;
        }
        int resourceId = getContext().getResources().getIdentifier(
            BuildConfig.APP_PROFILE_ID + "_" + soundName,
            "raw",
            getContext().getPackageName()
        );
        if (resourceId == 0) {
            call.reject("Sound resource is unavailable");
            return;
        }
        if (getActivity() == null) {
            call.reject("Activity is unavailable");
            return;
        }
        getActivity().runOnUiThread(() -> playResource(resourceId, call));
    }

    @PluginMethod
    public void announceDumpPoint(PluginCall call) {
        if (!BuildConfig.DRIVER_VOICE_ALERTS_ENABLED || !"driver".equals(BuildConfig.APP_PROFILE_ID)) {
            call.resolve(new JSObject().put("announced", false));
            return;
        }
        long eventVersion = readNumericLong(call, "eventVersion");
        long tripId = readNumericLong(call, "tripId");
        long dumpPointId = readNumericLong(call, "dumpPointId");
        String dumpPointName = call.getString("dumpPointName", "");
        if (getActivity() == null) {
            call.reject("Activity is unavailable");
            return;
        }
        getActivity().runOnUiThread(() -> {
            DriverDumpPointAnnouncer.Result result = DriverDumpPointAnnouncer.announce(
                getContext(),
                eventVersion,
                tripId,
                dumpPointId,
                dumpPointName,
                false,
                true
            );
            if (!result.announced) {
                call.resolve(new JSObject()
                    .put("announced", false)
                    .put("reason", result.reason)
                    .put("eventVersion", eventVersion)
                    .put("tripId", tripId));
                return;
            }
            call.resolve(new JSObject()
                .put("announced", true)
                .put("cuePlayed", result.cuePlayed));
        });
    }

    @PluginMethod
    public void announceOperational(PluginCall call) {
        String cueName = call.getString("cue", "");
        String voiceName = call.getString("voice", "");
        if (!ALLOWED_SOUNDS.contains(cueName) || !ALLOWED_VOICES.contains(voiceName)) {
            call.reject("Unknown operational voice");
            return;
        }
        long eventVersion = readNumericLong(call, "eventVersion");
        String eventKey = call.getString("eventKey", "");
        OperationalVoiceAnnouncer.Result result = OperationalVoiceAnnouncer.announce(
            getContext(),
            cueName,
            voiceName,
            eventVersion,
            eventKey,
            false,
            "",
            ""
        );
        call.resolve(new JSObject()
            .put("announced", result.announced)
            .put("reason", result.reason)
            .put("eventVersion", eventVersion));
    }

    /**
     * Capacitor deserializes ordinary JavaScript integer literals as Integer,
     * while PluginCall.getLong() accepts only an actual Long instance. Read
     * every JSON Number through the common Number contract so event and trip
     * identifiers are not silently replaced with zero.
     */
    private long readNumericLong(PluginCall call, String name) {
        return numericLong(call.getData().opt(name));
    }

    static long numericLong(Object value) {
        if (value instanceof Number) {
            return ((Number) value).longValue();
        }
        if (value instanceof String) {
            try {
                return Long.parseLong(((String) value).trim());
            } catch (NumberFormatException ignored) {}
        }
        return 0L;
    }

    @PluginMethod
    public void getDiagnostics(PluginCall call) {
        SharedPreferences preferences = getContext().getSharedPreferences(
            DriverVoicePlayer.DIAGNOSTIC_PREFS,
            Context.MODE_PRIVATE
        );
        AudioManager audioManager = getContext().getSystemService(AudioManager.class);
        JSObject result = new JSObject()
            .put("stage", preferences.getString(DriverVoicePlayer.DIAGNOSTIC_STAGE, "none"))
            .put("detail", preferences.getString(DriverVoicePlayer.DIAGNOSTIC_DETAIL, ""))
            .put("stageAt", preferences.getLong(DriverVoicePlayer.DIAGNOSTIC_AT, 0L))
            .put("appForeground", AppVisibility.isForeground());
        if (audioManager != null) {
            result.put("musicVolume", audioManager.getStreamVolume(AudioManager.STREAM_MUSIC));
            result.put("musicVolumeMax", audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC));
            result.put("ringerMode", audioManager.getRingerMode());
            result.put("audioMode", audioManager.getMode());
        }
        call.resolve(result);
    }

    private synchronized void playResource(int resourceId, PluginCall call) {
        releaseActivePlayer();
        AudioAttributes attributes = new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ASSISTANCE_SONIFICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
            .build();
        MediaPlayer player = MediaPlayer.create(getContext(), resourceId, attributes, 0);
        if (player == null) {
            call.reject("Sound could not be prepared");
            return;
        }
        activePlayer = player;
        player.setVolume(1.0f, 1.0f);
        player.setOnCompletionListener(completed -> {
            synchronized (NativeSoundPlugin.this) {
                if (activePlayer == completed) activePlayer = null;
                completed.release();
            }
        });
        player.setOnErrorListener((failed, what, extra) -> {
            synchronized (NativeSoundPlugin.this) {
                if (activePlayer == failed) activePlayer = null;
                failed.release();
            }
            return true;
        });
        player.start();
        JSObject result = new JSObject();
        result.put("played", true);
        call.resolve(result);
    }

    private synchronized void releaseActivePlayer() {
        if (activePlayer == null) return;
        try {
            activePlayer.stop();
        } catch (IllegalStateException ignored) {}
        activePlayer.release();
        activePlayer = null;
    }

    @Override
    protected void handleOnDestroy() {
        releaseActivePlayer();
        super.handleOnDestroy();
    }
}
