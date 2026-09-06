package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * Единый владелец голосового оповещения о точке разгрузки.
 *
 * <p>WebView и фоновый heartbeat могут увидеть одно событие почти одновременно.
 * Раньше каждый контур владел отдельным проигрывателем: один из них успевал
 * записать dedupe-маркер, а затем уничтожался до старта звука. Общий процессный
 * проигрыватель делает claim и постановку звука в очередь одной операцией.</p>
 */
public final class DriverDumpPointAnnouncer {
    public static final String REASON_ALREADY_ANNOUNCED = "already_announced";
    public static final String REASON_INVALID_EVENT = "invalid_event";

    private static DriverVoicePlayer sharedPlayer;
    private static long lastScheduledVersion;

    private DriverDumpPointAnnouncer() {}

    public static synchronized Result announce(
            Context context,
            long eventVersion,
            long tripId,
            long dumpPointId,
            String dumpPointName,
            boolean showNotification,
            boolean playCue) {
        if (eventVersion <= 0L || tripId <= 0L) {
            return Result.rejected(REASON_INVALID_EVENT);
        }
        Context appContext = context.getApplicationContext();
        String displayName = DriverVoiceCatalog.displayNameFor(dumpPointId, dumpPointName);
        if (displayName.isEmpty()) {
            return Result.rejected("dump_point_unavailable");
        }

        SharedPreferences preferences = appContext.getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        );
        long persistedVersion = preferences.getLong(
            ConnectivityForegroundService.LAST_DRIVER_DUMP_POINT_ALERT_VERSION,
            0L
        );
        if (eventVersion <= Math.max(persistedVersion, lastScheduledVersion)) {
            return Result.rejected(REASON_ALREADY_ANNOUNCED);
        }

        if (sharedPlayer == null) {
            sharedPlayer = new DriverVoicePlayer(appContext);
        }
        boolean notificationShown = showNotification && AppNotifications.showOperationalAlert(
            appContext,
            "Новая точка разгрузки",
            displayName
        );
        long voiceDelayMs = BuildConfig.ALERT_CUE_DURATION_MS
            + BuildConfig.VOICE_AFTER_CUE_DELAY_MS;

        /* Сначала гарантированно ставим звук в общий проигрыватель и только
           после этого публикуем dedupe-маркер для второго канала. */
        sharedPlayer.announce(
            dumpPointId,
            displayName,
            voiceDelayMs,
            playCue && !notificationShown,
            eventVersion,
            tripId
        );
        lastScheduledVersion = eventVersion;
        preferences.edit()
            .putLong(ConnectivityForegroundService.LAST_DRIVER_DUMP_POINT_ALERT_VERSION, eventVersion)
            .putLong("last_driver_dump_point_alert_trip_id", tripId)
            .putLong("last_driver_dump_point_alert_dump_point_id", dumpPointId)
            .commit();
        return Result.announced(notificationShown, playCue && !notificationShown);
    }

    public static final class Result {
        public final boolean announced;
        public final boolean notificationShown;
        public final boolean cuePlayed;
        public final String reason;

        private Result(boolean announced, boolean notificationShown, boolean cuePlayed, String reason) {
            this.announced = announced;
            this.notificationShown = notificationShown;
            this.cuePlayed = cuePlayed;
            this.reason = reason;
        }

        static Result announced(boolean notificationShown, boolean cuePlayed) {
            return new Result(true, notificationShown, cuePlayed, "");
        }

        static Result rejected(String reason) {
            return new Result(false, false, false, reason);
        }
    }
}
