package ru.copperresources.mobile;

import android.content.Context;
import android.media.AudioAttributes;
import android.os.Build;
import android.os.VibrationAttributes;
import android.os.VibrationEffect;
import android.os.Vibrator;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "NativeHaptics")
public class NativeHapticsPlugin extends Plugin {
    private static final int MAX_PATTERN_ITEMS = 31;
    private static final long MAX_ITEM_MS = 5_000L;
    private static final long MAX_PATTERN_MS = 15_000L;

    @PluginMethod
    public void vibrate(PluginCall call) {
        if (!NativeFieldProfile.supportsPushAndHaptics()) {
            call.reject("Native haptics are unavailable for this application");
            return;
        }
        JSArray source = call.getArray("pattern");
        if (source == null || source.length() == 0 || source.length() > MAX_PATTERN_ITEMS) {
            call.reject("A bounded vibration pattern is required");
            return;
        }

        long[] timings = new long[source.length()];
        long totalMs = 0L;
        for (int index = 0; index < source.length(); index += 1) {
            long durationMs = source.optLong(index, -1L);
            if (durationMs < 0L || durationMs > MAX_ITEM_MS) {
                call.reject("Vibration pattern contains an invalid duration");
                return;
            }
            totalMs += durationMs;
            if (totalMs > MAX_PATTERN_MS) {
                call.reject("Vibration pattern is too long");
                return;
            }
            timings[index] = durationMs;
        }

        int amplitude = Math.max(1, Math.min(255, call.getInt("amplitude", 160)));
        Vibrator vibrator = (Vibrator) getContext().getSystemService(Context.VIBRATOR_SERVICE);
        if (vibrator == null || !vibrator.hasVibrator()) {
            call.resolve(new JSObject().put("performed", false));
            return;
        }
        if (totalMs == 0L) {
            vibrator.cancel();
            call.resolve(new JSObject().put("performed", true));
            return;
        }

        vibrateWaveform(vibrator, timings, amplitude);
        call.resolve(new JSObject().put("performed", true));
    }

    @SuppressWarnings("deprecation")
    private static void vibrateWaveform(Vibrator vibrator, long[] timings, int amplitude) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            vibrator.vibrate(timings, -1);
            return;
        }
        int[] amplitudes = new int[timings.length];
        for (int index = 0; index < timings.length; index += 1) {
            amplitudes[index] = index % 2 == 0 && timings[index] > 0L ? amplitude : 0;
        }
        VibrationEffect effect = VibrationEffect.createWaveform(timings, amplitudes, -1);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            VibrationAttributes attributes = new VibrationAttributes.Builder()
                .setUsage(VibrationAttributes.USAGE_MEDIA)
                .build();
            vibrator.vibrate(effect, attributes);
            return;
        }
        AudioAttributes attributes = new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .build();
        vibrator.vibrate(effect, attributes);
    }
}
