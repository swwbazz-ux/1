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
