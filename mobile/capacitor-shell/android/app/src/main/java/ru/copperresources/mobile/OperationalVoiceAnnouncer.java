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
    public static final String REASON_IN_PROGRESS = "in_progress";
    public static final String REASON_PERSISTENCE_UNAVAILABLE = "persistence_unavailable";
    public static final String REASON_RESOURCE_UNAVAILABLE = "resource_unavailable";
    public static final String REASON_SEMANTIC_KEY_REQUIRED = "semantic_key_required";
    private static final String OPERATION_ORDER_KEY = "announced_operational_voice_operations";
    private static final String CLAIM_PREFIX = "operational_voice_claim_";
    private static final long CLAIM_STALE_AFTER_MS = 30_000L;
    private static final int OPERATION_HISTORY_LIMIT = 256;

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
        String normalizedKey = OperationalDeliveryLedger.qualifyOperationKey(appContext, eventKey);
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

        String claimToken = "";
        if (!normalizedKey.isEmpty() && eventVersion > 0L) {
            String versionedOperationKey = normalizedKey + ":v:" + eventVersion;
            claimToken = claimOperation(appContext, versionedOperationKey, true);
            if (claimToken.isEmpty()) {
                return isOperationCommitted(appContext, versionedOperationKey, true)
                    ? Result.rejected(REASON_ALREADY_ANNOUNCED, true)
                    : Result.rejected(REASON_IN_PROGRESS, false);
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
            releaseOperation(appContext, claimToken, true);
            return Result.rejected(REASON_RESOURCE_UNAVAILABLE);
        }
        if (!commitOperation(appContext, claimToken, true)) {
            return Result.queuedButUncommitted(notificationShown);
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
        List<String> operationOrder = readOperationOrder(appContext, preferences);
        Set<String> announcedKeys = new LinkedHashSet<>(operationOrder);
        List<String> pendingKeys = new ArrayList<>();
        List<String> voiceNames = new ArrayList<>();
        Set<String> requestedKeys = new LinkedHashSet<>();
        boolean claimInProgress = false;
        boolean invalidOperation = operations == null || operations.isEmpty();

        if (operations != null) {
            for (Operation operation : operations) {
                String qualifiedKey = operation == null
                    ? ""
                    : OperationalDeliveryLedger.qualifyOperationKey(appContext, operation.operationKey);
                List<String> operationVoices = new ArrayList<>();
                if (operation != null) {
                    for (String voiceName : operation.voiceNames) {
                        if (voiceName != null && !voiceName.isBlank()) operationVoices.add(voiceName);
                    }
                }
                if (operation == null || qualifiedKey.isEmpty() || operationVoices.isEmpty()
                        || !requestedKeys.add(qualifiedKey)) {
                    invalidOperation = true;
                    continue;
                }
                if (announcedKeys.contains(qualifiedKey)) continue;
                String claimToken = claimOperation(appContext, qualifiedKey, true);
                if (!claimToken.isEmpty()) {
                    pendingKeys.add(claimToken);
                    voiceNames.addAll(operationVoices);
                } else {
                    if (!isOperationCommitted(appContext, qualifiedKey, true)) {
                        claimInProgress = true;
                    }
                }
            }
        }
        if (invalidOperation) {
            for (String operationKey : pendingKeys) releaseOperation(appContext, operationKey, true);
            return Result.rejected(REASON_RESOURCE_UNAVAILABLE, false);
        }
        if (claimInProgress) {
            for (String operationKey : pendingKeys) releaseOperation(appContext, operationKey, true);
            return Result.rejected(REASON_IN_PROGRESS, false);
        }
        if (pendingKeys.isEmpty()) {
            return Result.rejected(REASON_ALREADY_ANNOUNCED, true);
        }
        if (voiceNames.isEmpty()) {
            for (String operationKey : pendingKeys) releaseOperation(appContext, operationKey, true);
            return Result.rejected(REASON_RESOURCE_UNAVAILABLE, false);
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
            for (String operationKey : pendingKeys) releaseOperation(appContext, operationKey, true);
            return Result.rejected(REASON_RESOURCE_UNAVAILABLE, false);
        }

        for (String operationKey : pendingKeys) {
            operationOrder.remove(operationKey);
            operationOrder.add(operationKey);
        }
        while (operationOrder.size() > OPERATION_HISTORY_LIMIT) {
            operationOrder.remove(0);
        }
        boolean historyCommitted = preferences.edit()
            .putString(OPERATION_ORDER_KEY, String.join("\n", operationOrder))
            .commit();
        if (!historyCommitted) {
            // The queue may already have accepted the audio, but the shared
            // cursor must remain retryable until semantic ownership is durable.
            // Keep claims in place; they expire and safely retry after the
            // short lease if the process survives.
            return Result.queuedButUncommitted(notificationShown);
        }
        for (String operationKey : pendingKeys) {
            preferences.edit().remove(CLAIM_PREFIX + operationKey).commit();
        }
        return Result.announced(notificationShown);
    }

    /**
     * Serial persistent claim. A crash before queueing leaves a 30 second lease
     * and is retried afterwards. A crash after playback starts but before commit
     * may therefore repeat the phrase; committing before audible completion can
     * conversely suppress a phrase Android never played. Exactly-once audible
     * output is impossible without acknowledgement from the audio device.
     */
    static synchronized String claimOperation(Context context, String operationKey, boolean alreadyQualified) {
        Context appContext = context.getApplicationContext();
        String qualified = alreadyQualified
            ? (operationKey == null ? "" : operationKey.trim())
            : OperationalDeliveryLedger.qualifyOperationKey(appContext, operationKey);
        if (qualified.isEmpty()) return "";
        SharedPreferences preferences = appContext.getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        );
        if (readOperationOrder(appContext, preferences).contains(qualified)) return "";
        String claimKey = CLAIM_PREFIX + qualified;
        long now = System.currentTimeMillis();
        long claimedAt = preferences.getLong(claimKey, 0L);
        if (claimedAt > 0L && now - claimedAt < CLAIM_STALE_AFTER_MS) return "";
        if (!preferences.edit().putLong(claimKey, now).commit()) return "";
        return qualified;
    }

    static synchronized boolean commitOperation(Context context, String claimToken, boolean alreadyQualified) {
        Context appContext = context.getApplicationContext();
        String qualified = alreadyQualified
            ? (claimToken == null ? "" : claimToken.trim())
            : OperationalDeliveryLedger.qualifyOperationKey(appContext, claimToken);
        if (qualified.isEmpty()) return true;
        SharedPreferences preferences = appContext.getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        );
        List<String> order = readOperationOrder(appContext, preferences);
        order.remove(qualified);
        order.add(qualified);
        while (order.size() > OPERATION_HISTORY_LIMIT) order.remove(0);
        return preferences.edit()
            .putString(OPERATION_ORDER_KEY, String.join("\n", order))
            .remove(CLAIM_PREFIX + qualified)
            .commit();
    }

    static synchronized void releaseOperation(Context context, String claimToken, boolean alreadyQualified) {
        String qualified = alreadyQualified
            ? (claimToken == null ? "" : claimToken.trim())
            : OperationalDeliveryLedger.qualifyOperationKey(context, claimToken);
        if (qualified.isEmpty()) return;
        context.getApplicationContext().getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        ).edit().remove(CLAIM_PREFIX + qualified).commit();
    }

    static synchronized boolean isOperationCommitted(
            Context context,
            String operationKey,
            boolean alreadyQualified) {
        Context appContext = context.getApplicationContext();
        String qualified = alreadyQualified
            ? (operationKey == null ? "" : operationKey.trim())
            : OperationalDeliveryLedger.qualifyOperationKey(appContext, operationKey);
        if (qualified.isEmpty()) return false;
        return readOperationOrder(appContext, appContext.getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        )).contains(qualified);
    }

    private static List<String> readOperationOrder(Context context, SharedPreferences preferences) {
        List<String> order = new ArrayList<>();
        String stored = preferences.getString(OPERATION_ORDER_KEY, "");
        if (stored == null || stored.isBlank()) {
            return order;
        }
        for (String item : stored.split("\\n")) {
            String normalized = OperationalDeliveryLedger.normalizeStoredOperationKey(context, item);
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
        public final boolean processed;
        public final String reason;

        private Result(boolean announced, boolean notificationShown, boolean processed, String reason) {
            this.announced = announced;
            this.notificationShown = notificationShown;
            this.processed = processed;
            this.reason = reason;
        }

        static Result announced(boolean notificationShown) {
            return new Result(true, notificationShown, true, "");
        }

        static Result queuedButUncommitted(boolean notificationShown) {
            return new Result(true, notificationShown, false, REASON_PERSISTENCE_UNAVAILABLE);
        }

        static Result rejected(String reason) {
            return rejected(reason, REASON_ALREADY_ANNOUNCED.equals(reason));
        }

        static Result rejected(String reason, boolean processed) {
            return new Result(false, false, processed, reason);
        }
    }
}
