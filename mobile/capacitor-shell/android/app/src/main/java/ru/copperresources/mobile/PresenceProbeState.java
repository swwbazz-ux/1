package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

/** Durable short-lived handoff from FCM delivery to the authenticated native heartbeat. */
final class PresenceProbeState {
    private static final String PREFS_NAME = "application_connection_probe";
    private static final String PROBE_ID = "probe_id";
    private static final String RECEIVED_AT = "received_at";
    private static final long MAX_AGE_MS = 2 * 60_000L;

    private PresenceProbeState() {}

    static synchronized boolean remember(Context context, String probeId, long now) {
        String normalized = probeId == null ? "" : probeId.trim();
        if (!normalized.matches("[A-Za-z0-9_-]{16,96}")) return false;
        preferences(context).edit()
            .putString(PROBE_ID, normalized)
            .putLong(RECEIVED_AT, now)
            .commit();
        return true;
    }

    static synchronized String pending(Context context, long now) {
        SharedPreferences preferences = preferences(context);
        String probeId = preferences.getString(PROBE_ID, "");
        long receivedAt = preferences.getLong(RECEIVED_AT, 0L);
        if (probeId == null || probeId.isEmpty() || receivedAt <= 0L || now - receivedAt > MAX_AGE_MS) {
            clear(context);
            return "";
        }
        return probeId;
    }

    static synchronized void clear(Context context) {
        preferences(context).edit().clear().commit();
    }

    static synchronized void clearIfEquals(Context context, String probeId) {
        SharedPreferences preferences = preferences(context);
        String current = preferences.getString(PROBE_ID, "");
        if (!shouldClear(current, probeId)) return;
        preferences.edit().clear().commit();
    }

    static boolean shouldClear(String currentProbeId, String sentProbeId) {
        return sentProbeId != null
            && !sentProbeId.isEmpty()
            && sentProbeId.equals(currentProbeId);
    }

    private static SharedPreferences preferences(Context context) {
        return context.getApplicationContext().getSharedPreferences(
            PREFS_NAME,
            Context.MODE_PRIVATE
        );
    }
}
