package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.Locale;

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
        String operationKey = "driver-dump:" + tripId + ":" + (
                dumpPointId > 0L
                    ? "id-" + dumpPointId
                    : "name-" + displayName.toLowerCase(Locale.ROOT).replaceAll("\\s+", "_")
            );
        String claimToken = OperationalVoiceAnnouncer.claimOperation(
            appContext,
            operationKey,
            false
        );
        if (claimToken.isEmpty()) {
            return OperationalVoiceAnnouncer.isOperationCommitted(
                appContext,
                operationKey,
                false
            )
                ? Result.rejected(REASON_ALREADY_ANNOUNCED, true)
                : Result.rejected(OperationalVoiceAnnouncer.REASON_IN_PROGRESS, false);
        }

        if (sharedPlayer == null) {
            sharedPlayer = new DriverVoicePlayer(appContext);
        }
        boolean notificationShown = showNotification && AppNotifications.showOperationalAlert(
            appContext,
            "Новая точка разгрузки",
            displayName
        );
        /* Сначала гарантированно ставим звук в общий проигрыватель и только
           после этого публикуем dedupe-маркер для второго канала. */
        try {
            sharedPlayer.announce(
                dumpPointId,
                displayName,
                0L,
                playCue,
                eventVersion,
                tripId
            );
        } catch (RuntimeException error) {
            OperationalVoiceAnnouncer.releaseOperation(appContext, claimToken, true);
            return Result.rejected("resource_unavailable", false);
        }
        if (!OperationalVoiceAnnouncer.commitOperation(appContext, claimToken, true)) {
            return Result.queuedButUncommitted(notificationShown, playCue);
        }
        preferences.edit()
            .putLong(ConnectivityForegroundService.LAST_DRIVER_DUMP_POINT_ALERT_VERSION, eventVersion)
            .putLong("last_driver_dump_point_alert_trip_id", tripId)
            .putLong("last_driver_dump_point_alert_dump_point_id", dumpPointId)
            .commit();
        return Result.announced(notificationShown, playCue);
    }

    public static final class Result {
        public final boolean announced;
        public final boolean notificationShown;
        public final boolean cuePlayed;
        public final boolean processed;
        public final String reason;

        private Result(
                boolean announced,
                boolean notificationShown,
                boolean cuePlayed,
                boolean processed,
                String reason) {
            this.announced = announced;
            this.notificationShown = notificationShown;
            this.cuePlayed = cuePlayed;
            this.processed = processed;
            this.reason = reason;
        }

        static Result announced(boolean notificationShown, boolean cuePlayed) {
            return new Result(true, notificationShown, cuePlayed, true, "");
        }

        static Result queuedButUncommitted(boolean notificationShown, boolean cuePlayed) {
            return new Result(
                true,
                notificationShown,
                cuePlayed,
                false,
                OperationalVoiceAnnouncer.REASON_PERSISTENCE_UNAVAILABLE
            );
        }

        static Result rejected(String reason) {
            return rejected(reason, true);
        }

        static Result rejected(String reason, boolean processed) {
            return new Result(false, false, false, processed, reason);
        }
    }
}
