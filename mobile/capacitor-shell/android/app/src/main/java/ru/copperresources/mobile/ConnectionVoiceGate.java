package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

/** Shared persistent arbiter for foreground WebView and background native connection speech. */
final class ConnectionVoiceGate {
    static final String LOSS_ANNOUNCED_KEY = "connection_loss_announced";
    static final String LAST_LOSS_ANNOUNCED_AT_KEY = "connection_last_loss_announced_at";
    static final long LOSS_COOLDOWN_MS = 5 * 60_000L;

    private ConnectionVoiceGate() {}

    static synchronized boolean isLossAnnounced(Context context) {
        return preferences(context).getBoolean(LOSS_ANNOUNCED_KEY, false);
    }

    static synchronized boolean confirmLoss(Context context, long now) {
        SharedPreferences preferences = preferences(context);
        if (preferences.getBoolean(LOSS_ANNOUNCED_KEY, false)) {
            return false;
        }
        long lastLossAt = preferences.getLong(LAST_LOSS_ANNOUNCED_AT_KEY, 0L);
        if (lastLossAt > 0L && now - lastLossAt < LOSS_COOLDOWN_MS) {
            return false;
        }
        preferences.edit()
            .putBoolean(LOSS_ANNOUNCED_KEY, true)
            .putLong(LAST_LOSS_ANNOUNCED_AT_KEY, now)
            .apply();
        return true;
    }

    static synchronized boolean confirmRecovery(Context context) {
        SharedPreferences preferences = preferences(context);
        if (!preferences.getBoolean(LOSS_ANNOUNCED_KEY, false)) {
            return false;
        }
        preferences.edit().putBoolean(LOSS_ANNOUNCED_KEY, false).apply();
        return true;
    }

    static synchronized void rollbackLoss(Context context, long attemptedAt) {
        SharedPreferences preferences = preferences(context);
        if (preferences.getLong(LAST_LOSS_ANNOUNCED_AT_KEY, 0L) != attemptedAt) return;
        preferences.edit()
            .putBoolean(LOSS_ANNOUNCED_KEY, false)
            .remove(LAST_LOSS_ANNOUNCED_AT_KEY)
            .apply();
    }

    static synchronized void rollbackRecovery(Context context) {
        preferences(context).edit().putBoolean(LOSS_ANNOUNCED_KEY, true).apply();
    }

    private static SharedPreferences preferences(Context context) {
        return context.getApplicationContext().getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        );
    }
}
