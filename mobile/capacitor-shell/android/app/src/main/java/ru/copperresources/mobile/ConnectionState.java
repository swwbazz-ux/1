package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

final class ConnectionState {
    static final String PREFS_NAME = "native_connectivity";
    private static final String CONNECTION_DESIRED = "connection_desired";
    private static final String SHIFT_ACTIVE = "shift_active";
    private static final String ACTIVE_SHIFT_ID = "active_shift_id";
    private static final String LAST_ALIVE_AT = "last_alive_at";
    private static final String LAST_STOP_REASON = "last_stop_reason";
    private static final String LAST_SERVER_REQUIRED = "last_server_required";

    private ConnectionState() {}

    static boolean isDesired(Context context) {
        return preferences(context).getBoolean(CONNECTION_DESIRED, false);
    }

    static boolean isShiftActive(Context context) {
        return preferences(context).getBoolean(SHIFT_ACTIVE, false);
    }

    static String activeShiftId(Context context) {
        return preferences(context).getString(ACTIVE_SHIFT_ID, "");
    }

    static long lastAliveAt(Context context) {
        return preferences(context).getLong(LAST_ALIVE_AT, 0L);
    }

    static String lastStopReason(Context context) {
        return preferences(context).getString(LAST_STOP_REASON, "");
    }

    static boolean enableFromUi(Context context, String shiftId) {
        return preferences(context).edit()
            .putBoolean(CONNECTION_DESIRED, true)
            .putBoolean(SHIFT_ACTIVE, true)
            .putString(ACTIVE_SHIFT_ID, safeShiftId(shiftId))
            .putBoolean(LAST_SERVER_REQUIRED, true)
            .remove(LAST_STOP_REASON)
            .commit();
    }

    static boolean applyServerRequirement(Context context, boolean required, String shiftId) {
        SharedPreferences.Editor editor = preferences(context).edit()
            .putBoolean(CONNECTION_DESIRED, required)
            .putBoolean(SHIFT_ACTIVE, required)
            .putBoolean(LAST_SERVER_REQUIRED, required);
        if (required) {
            editor.putString(ACTIVE_SHIFT_ID, safeShiftId(shiftId));
            editor.remove(LAST_STOP_REASON);
        } else {
            editor.remove(ACTIVE_SHIFT_ID);
        }
        return editor.commit();
    }

    static boolean disable(Context context, String reason) {
        return preferences(context).edit()
            .putBoolean(CONNECTION_DESIRED, false)
            .putBoolean(SHIFT_ACTIVE, false)
            .putBoolean(LAST_SERVER_REQUIRED, false)
            .remove(ACTIVE_SHIFT_ID)
            .putString(LAST_STOP_REASON, reason == null ? "" : reason)
            .commit();
    }

    static void recordAlive(Context context, long aliveAt) {
        preferences(context).edit().putLong(LAST_ALIVE_AT, aliveAt).apply();
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
    }

    private static String safeShiftId(String shiftId) {
        return shiftId == null ? "" : shiftId.trim();
    }
}
