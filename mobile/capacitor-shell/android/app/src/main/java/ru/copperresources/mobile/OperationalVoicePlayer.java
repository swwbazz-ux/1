package ru.copperresources.mobile;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;

/** Единая нативная цепочка «действующий сигнал → записанная рабочая фраза». */
public final class OperationalVoicePlayer {
    private static final AudioAttributes CUE_ATTRIBUTES = new AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANCE_SONIFICATION)
        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
        .build();
    private static final AudioAttributes SPEECH_ATTRIBUTES = new AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE)
        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
        .build();

    private static OperationalVoicePlayer shared;

    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AudioManager audioManager;
    private final AudioManager.OnAudioFocusChangeListener focusChangeListener = focusChange -> {
        if (focusChange == AudioManager.AUDIOFOCUS_LOSS) {
            mainHandler.post(this::stopCurrentPlayback);
        }
    };

    private MediaPlayer cuePlayer;
    private MediaPlayer voicePlayer;
    private AudioFocusRequest audioFocusRequest;
    private int generation;

    private OperationalVoicePlayer(Context context) {
        this.context = context.getApplicationContext();
        this.audioManager = context.getSystemService(AudioManager.class);
    }

    public static synchronized boolean play(
            Context context,
            String cueName,
            String voiceName,
            boolean playCue,
            long voiceDelayMs) {
        if (shared == null) {
            shared = new OperationalVoicePlayer(context);
        }
        return shared.enqueue(cueName, voiceName, playCue, voiceDelayMs);
    }

    private boolean enqueue(String cueName, String voiceName, boolean playCue, long voiceDelayMs) {
        int voiceResourceId = resourceId(voiceName);
        if (voiceResourceId == 0) {
            return false;
        }
        int cueResourceId = playCue ? resourceId(cueName) : 0;
        int scheduledGeneration = ++generation;
        mainHandler.post(() -> {
            if (scheduledGeneration != generation) {
                return;
            }
            stopCurrentPlayback();
            requestTransientAudioFocus();
            if (cueResourceId != 0) {
                playCueThenVoice(cueResourceId, voiceResourceId, scheduledGeneration);
            } else {
                mainHandler.postDelayed(
                    () -> playVoice(voiceResourceId, scheduledGeneration),
                    Math.max(0L, voiceDelayMs)
                );
            }
        });
        return true;
    }

    private int resourceId(String suffix) {
        if (suffix == null || suffix.isBlank()) {
            return 0;
        }
        return context.getResources().getIdentifier(
            BuildConfig.APP_PROFILE_ID + "_" + suffix,
            "raw",
            context.getPackageName()
        );
    }

    private void playCueThenVoice(int cueResourceId, int voiceResourceId, int scheduledGeneration) {
        cuePlayer = MediaPlayer.create(
            context,
            cueResourceId,
            CUE_ATTRIBUTES,
            AudioManager.AUDIO_SESSION_ID_GENERATE
        );
        if (cuePlayer == null) {
            playVoice(voiceResourceId, scheduledGeneration);
            return;
        }
        cuePlayer.setVolume(1.0f, 1.0f);
        cuePlayer.setOnCompletionListener(player -> {
            if (cuePlayer == player) {
                cuePlayer = null;
            }
            player.release();
            mainHandler.postDelayed(
                () -> playVoice(voiceResourceId, scheduledGeneration),
                Math.max(0L, BuildConfig.VOICE_AFTER_CUE_DELAY_MS)
            );
        });
        cuePlayer.setOnErrorListener((player, what, extra) -> {
            if (cuePlayer == player) {
                cuePlayer = null;
            }
            player.release();
            playVoice(voiceResourceId, scheduledGeneration);
            return true;
        });
        cuePlayer.start();
    }

    private void playVoice(int resourceId, int scheduledGeneration) {
        if (scheduledGeneration != generation) {
            return;
        }
        voicePlayer = MediaPlayer.create(
            context,
            resourceId,
            SPEECH_ATTRIBUTES,
            AudioManager.AUDIO_SESSION_ID_GENERATE
        );
        if (voicePlayer == null) {
            abandonAudioFocus();
            return;
        }
        voicePlayer.setVolume(1.0f, 1.0f);
        voicePlayer.setOnCompletionListener(player -> stopCurrentPlayback());
        voicePlayer.setOnErrorListener((player, what, extra) -> {
            stopCurrentPlayback();
            return true;
        });
        voicePlayer.start();
    }

    private void stopCurrentPlayback() {
        mainHandler.removeCallbacksAndMessages(null);
        if (cuePlayer != null) {
            try {
                cuePlayer.stop();
            } catch (IllegalStateException ignored) {}
            cuePlayer.release();
            cuePlayer = null;
        }
        if (voicePlayer != null) {
            try {
                voicePlayer.stop();
            } catch (IllegalStateException ignored) {}
            voicePlayer.release();
            voicePlayer = null;
        }
        abandonAudioFocus();
    }

    @SuppressWarnings("deprecation")
    private void requestTransientAudioFocus() {
        if (audioManager == null) {
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            audioFocusRequest = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                .setAudioAttributes(SPEECH_ATTRIBUTES)
                .setOnAudioFocusChangeListener(focusChangeListener)
                .build();
            audioManager.requestAudioFocus(audioFocusRequest);
        } else {
            audioManager.requestAudioFocus(
                focusChangeListener,
                AudioManager.STREAM_MUSIC,
                AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK
            );
        }
    }

    @SuppressWarnings("deprecation")
    private void abandonAudioFocus() {
        if (audioManager == null) {
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && audioFocusRequest != null) {
            audioManager.abandonAudioFocusRequest(audioFocusRequest);
            audioFocusRequest = null;
        } else {
            audioManager.abandonAudioFocus(focusChangeListener);
        }
    }
}
