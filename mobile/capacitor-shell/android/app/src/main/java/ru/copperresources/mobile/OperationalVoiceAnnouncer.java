package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

/** Защищает внешнее рабочее событие от дубля между WebView и heartbeat. */
public final class OperationalVoiceAnnouncer {
    public static final String REASON_ALREADY_ANNOUNCED = "already_announced";
    public static final String REASON_RESOURCE_UNAVAILABLE = "resource_unavailable";

    private OperationalVoiceAnnouncer() {}

    public static synchronized Result announce(
            Context context,
            String cueName,
            String voiceName,
            long eventVersion,
            String eventKey,
            boolean showNotification,
            String notificationTitle,
            String notificationBody) {
        return announceSequence(
            context,
            cueName,
            new String[] {voiceName},
            eventVersion,
            eventKey,
            showNotification,
            notificationTitle,
            notificationBody
        );
    }

    public static synchronized Result announceSequence(
            Context context,
            String cueName,
            String[] voiceNames,
            long eventVersion,
            String eventKey,
            boolean showNotification,
            String notificationTitle,
            String notificationBody) {
        Context appContext = context.getApplicationContext();
        String normalizedKey = eventKey == null ? "" : eventKey.trim();
        SharedPreferences preferences = appContext.getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        );
        String preferenceKey = normalizedKey.isEmpty()
            ? ""
            : "last_operational_voice_" + normalizedKey;
        if (!preferenceKey.isEmpty() && eventVersion > 0L) {
            long previousVersion = preferences.getLong(preferenceKey, 0L);
            if (eventVersion <= previousVersion) {
                return Result.rejected(REASON_ALREADY_ANNOUNCED);
            }
        }

        boolean notificationShown = showNotification && AppNotifications.showOperationalAlert(
            appContext,
            notificationTitle,
            notificationBody
        );
        boolean queued = OperationalVoicePlayer.playSequence(
            appContext,
            cueName,
            voiceNames,
            !notificationShown,
            notificationShown
                ? BuildConfig.ALERT_CUE_DURATION_MS + BuildConfig.VOICE_AFTER_CUE_DELAY_MS
                : 0L
        );
        if (!queued) {
            return Result.rejected(REASON_RESOURCE_UNAVAILABLE);
        }
        if (!preferenceKey.isEmpty() && eventVersion > 0L) {
            preferences.edit().putLong(preferenceKey, eventVersion).commit();
        }
        return Result.announced(notificationShown);
    }

    public static final class Result {
        public final boolean announced;
        public final boolean notificationShown;
        public final String reason;

        private Result(boolean announced, boolean notificationShown, String reason) {
            this.announced = announced;
            this.notificationShown = notificationShown;
            this.reason = reason;
        }

        static Result announced(boolean notificationShown) {
            return new Result(true, notificationShown, "");
        }

        static Result rejected(String reason) {
            return new Result(false, false, reason);
        }
    }
}
