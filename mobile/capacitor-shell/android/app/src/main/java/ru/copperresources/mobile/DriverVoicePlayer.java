package ru.copperresources.mobile;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;

import java.util.Locale;

/** Проигрывает проверенную запись после штатного сигнала уведомления. */
public final class DriverVoicePlayer {
    private static final String TTS_UTTERANCE_ID = "driver-dump-point";
    private static final AudioAttributes SPEECH_ATTRIBUTES = new AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE)
        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
        .build();
    private static final AudioAttributes CUE_ATTRIBUTES = new AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANCE_SONIFICATION)
        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
        .build();

    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AudioManager audioManager;
    private final AudioManager.OnAudioFocusChangeListener focusChangeListener = focusChange -> {
        if (focusChange == AudioManager.AUDIOFOCUS_LOSS) {
            mainHandler.post(this::stopCurrentPlayback);
        }
    };

    private MediaPlayer mediaPlayer;
    private MediaPlayer cuePlayer;
    private TextToSpeech textToSpeech;
    private AudioFocusRequest audioFocusRequest;
    private String pendingFallbackText = "";
    private int generation;
    private boolean textToSpeechInitializing;
    private boolean destroyed;

    public DriverVoicePlayer(Context context) {
        this.context = context.getApplicationContext();
        this.audioManager = context.getSystemService(AudioManager.class);
    }

    public void announce(long dumpPointId, String dumpPointName, long delayMs) {
        announce(dumpPointId, dumpPointName, delayMs, false);
    }

    public void announce(
            long dumpPointId,
            String dumpPointName,
            long delayMs,
            boolean playCue) {
        int scheduledGeneration = ++generation;
        String resourceName = DriverVoiceCatalog.resourceNameFor(dumpPointId, dumpPointName);
        String fallbackText = DriverVoiceCatalog.fallbackPhrase(dumpPointName);
        if (playCue) {
            mainHandler.post(() -> {
                if (destroyed || scheduledGeneration != generation) {
                    return;
                }
                stopCurrentPlayback();
                playAlertCue();
            });
        }
        mainHandler.postDelayed(() -> {
            if (destroyed || scheduledGeneration != generation) {
                return;
            }
            stopCurrentPlayback();
            playRecordedOrFallback(resourceName, fallbackText, scheduledGeneration);
        }, Math.max(0L, delayMs));
    }

    private void playAlertCue() {
        int resourceId = context.getResources().getIdentifier(
            BuildConfig.ALERT_SOUND_RESOURCE,
            "raw",
            context.getPackageName()
        );
        if (resourceId == 0) {
            return;
        }
        cuePlayer = MediaPlayer.create(
            context,
            resourceId,
            CUE_ATTRIBUTES,
            AudioManager.AUDIO_SESSION_ID_GENERATE
        );
        if (cuePlayer == null) {
            return;
        }
        cuePlayer.setVolume(1.0f, 1.0f);
        cuePlayer.setOnCompletionListener(player -> releaseCuePlayer(player));
        cuePlayer.setOnErrorListener((player, what, extra) -> {
            releaseCuePlayer(player);
            return true;
        });
        cuePlayer.start();
    }

    private void releaseCuePlayer(MediaPlayer player) {
        if (cuePlayer == player) {
            cuePlayer = null;
        }
        player.release();
    }

    public void shutdown() {
        destroyed = true;
        generation += 1;
        mainHandler.removeCallbacksAndMessages(null);
        stopCurrentPlayback();
        if (textToSpeech != null) {
            textToSpeech.stop();
            textToSpeech.shutdown();
            textToSpeech = null;
        }
    }

    private void playRecordedOrFallback(String resourceName, String fallbackText, int scheduledGeneration) {
        int resourceId = resourceName.isEmpty()
            ? 0
            : context.getResources().getIdentifier(resourceName, "raw", context.getPackageName());
        if (resourceId == 0) {
            speakFallback(fallbackText, scheduledGeneration);
            return;
        }

        requestTransientAudioFocus();
        mediaPlayer = MediaPlayer.create(
            context,
            resourceId,
            SPEECH_ATTRIBUTES,
            AudioManager.AUDIO_SESSION_ID_GENERATE
        );
        if (mediaPlayer == null) {
            abandonAudioFocus();
            speakFallback(fallbackText, scheduledGeneration);
            return;
        }
        mediaPlayer.setOnCompletionListener(player -> stopCurrentPlayback());
        mediaPlayer.setOnErrorListener((player, what, extra) -> {
            stopCurrentPlayback();
            speakFallback(fallbackText, scheduledGeneration);
            return true;
        });
        mediaPlayer.start();
    }

    private void speakFallback(String fallbackText, int scheduledGeneration) {
        if (destroyed || scheduledGeneration != generation || fallbackText.isBlank()) {
            return;
        }
        pendingFallbackText = fallbackText;
        if (textToSpeech != null) {
            startTextToSpeech(scheduledGeneration);
            return;
        }
        if (textToSpeechInitializing) {
            return;
        }
        textToSpeechInitializing = true;
        textToSpeech = new TextToSpeech(context, status -> {
            textToSpeechInitializing = false;
            if (destroyed || scheduledGeneration != generation || status != TextToSpeech.SUCCESS) {
                abandonAudioFocus();
                return;
            }
            textToSpeech.setLanguage(Locale.forLanguageTag("ru-RU"));
            textToSpeech.setAudioAttributes(SPEECH_ATTRIBUTES);
            textToSpeech.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                @Override
                public void onStart(String utteranceId) {}

                @Override
                public void onDone(String utteranceId) {
                    mainHandler.post(DriverVoicePlayer.this::abandonAudioFocus);
                }

                @Override
                public void onError(String utteranceId) {
                    mainHandler.post(DriverVoicePlayer.this::abandonAudioFocus);
                }
            });
            startTextToSpeech(scheduledGeneration);
        });
    }

    private void startTextToSpeech(int scheduledGeneration) {
        if (destroyed || scheduledGeneration != generation || textToSpeech == null) {
            return;
        }
        requestTransientAudioFocus();
        textToSpeech.speak(
            pendingFallbackText,
            TextToSpeech.QUEUE_FLUSH,
            null,
            TTS_UTTERANCE_ID
        );
    }

    private void stopCurrentPlayback() {
        if (cuePlayer != null) {
            try {
                cuePlayer.stop();
            } catch (IllegalStateException ignored) {}
            cuePlayer.release();
            cuePlayer = null;
        }
        if (mediaPlayer != null) {
            try {
                mediaPlayer.stop();
            } catch (IllegalStateException ignored) {}
            mediaPlayer.release();
            mediaPlayer = null;
        }
        if (textToSpeech != null) {
            textToSpeech.stop();
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
