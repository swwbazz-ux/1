package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Защищает внешнее рабочее событие от дубля между WebView и heartbeat. */
public final class OperationalVoiceAnnouncer {
    public static final String REASON_ALREADY_ANNOUNCED = "already_announced";
    public static final String REASON_RESOURCE_UNAVAILABLE = "resource_unavailable";
    private static final String OPERATION_ORDER_KEY = "announced_operational_voice_operations";
    private static final int OPERATION_HISTORY_LIMIT = 96;

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
            true,
            0L
        );
        if (!queued) {
            return Result.rejected(REASON_RESOURCE_UNAVAILABLE);
        }
        if (!preferenceKey.isEmpty() && eventVersion > 0L) {
            preferences.edit().putLong(preferenceKey, eventVersion).commit();
        }
        return Result.announced(notificationShown);
    }

    /**
     * Озвучивает несколько независимых рабочих операций одной непрерывной
     * цепочкой и дедуплицирует их по стабильным идентификаторам, а не по
     * глобальной версии состояния. Это общий арбитр между WebView и heartbeat.
     */
    public static synchronized Result announceOperations(
            Context context,
            String cueName,
            List<Operation> operations,
            boolean showNotification,
            String notificationTitle,
            String notificationBody) {
        Context appContext = context.getApplicationContext();
        SharedPreferences preferences = appContext.getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        );
        List<String> operationOrder = readOperationOrder(preferences);
        Set<String> announcedKeys = new LinkedHashSet<>(operationOrder);
        List<String> pendingKeys = new ArrayList<>();
        List<String> voiceNames = new ArrayList<>();

        if (operations != null) {
            for (Operation operation : operations) {
                if (operation == null || operation.operationKey.isEmpty()
                        || announcedKeys.contains(operation.operationKey)
                        || pendingKeys.contains(operation.operationKey)) {
                    continue;
                }
                int voiceCountBefore = voiceNames.size();
                for (String voiceName : operation.voiceNames) {
                    if (voiceName != null && !voiceName.isBlank()) {
                        voiceNames.add(voiceName);
                    }
                }
                if (voiceNames.size() > voiceCountBefore) {
                    pendingKeys.add(operation.operationKey);
                }
            }
        }
        if (pendingKeys.isEmpty()) {
            return Result.rejected(REASON_ALREADY_ANNOUNCED);
        }
        if (voiceNames.isEmpty()) {
            return Result.rejected(REASON_RESOURCE_UNAVAILABLE);
        }

        boolean notificationShown = showNotification && AppNotifications.showOperationalAlert(
            appContext,
            notificationTitle,
            notificationBody
        );
        boolean queued = OperationalVoicePlayer.playSequence(
            appContext,
            cueName,
            voiceNames.toArray(new String[0]),
            true,
            0L
        );
        if (!queued) {
            return Result.rejected(REASON_RESOURCE_UNAVAILABLE);
        }

        for (String operationKey : pendingKeys) {
            operationOrder.remove(operationKey);
            operationOrder.add(operationKey);
        }
        while (operationOrder.size() > OPERATION_HISTORY_LIMIT) {
            operationOrder.remove(0);
        }
        preferences.edit()
            .putString(OPERATION_ORDER_KEY, String.join("\n", operationOrder))
            .commit();
        return Result.announced(notificationShown);
    }

    private static List<String> readOperationOrder(SharedPreferences preferences) {
        List<String> order = new ArrayList<>();
        String stored = preferences.getString(OPERATION_ORDER_KEY, "");
        if (stored == null || stored.isBlank()) {
            return order;
        }
        for (String item : stored.split("\\n")) {
            String normalized = item == null ? "" : item.trim();
            if (!normalized.isEmpty() && !order.contains(normalized)) {
                order.add(normalized);
            }
        }
        while (order.size() > OPERATION_HISTORY_LIMIT) {
            order.remove(0);
        }
        return order;
    }

    public static final class Operation {
        public final String operationKey;
        public final String[] voiceNames;

        public Operation(String operationKey, String[] voiceNames) {
            this.operationKey = operationKey == null ? "" : operationKey.trim();
            this.voiceNames = voiceNames == null ? new String[0] : voiceNames.clone();
        }
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
