package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

/** Тихая загрузка расстановки после явного удаления приложения из недавних. */
final class AssignmentVoiceResumeGate {
    private static final String PENDING = "assignment_resume_pending";
    private static final String THROUGH = "assignment_resume_silent_through";
    private static final String SCOPE = "assignment_resume_scope";

    private AssignmentVoiceResumeGate() {}

    static synchronized boolean dismissed(Context context) {
        return preferences(context).edit().putLong(PENDING, 1L).commit();
    }

    /** Only the authenticated native state response establishes a baseline. */
    static synchronized boolean observeServer(Context context, long version) {
        SharedPreferences prefs = preferences(context);
        if (prefs.getLong(PENDING, 0L) == 0L) return true;
        if (version < 0L) return false;
        return prefs.edit().putLong(THROUGH, version).putString(SCOPE, scope(context))
            .putLong(PENDING, 0L).commit();
    }

    static synchronized boolean pending(Context context) {
        return preferences(context).getLong(PENDING, 0L) != 0L;
    }

    static boolean isAssignment(String key) {
        return key != null && (key.startsWith("assign:") || key.startsWith("release:")
            || key.startsWith("excavator-assignment:"));
    }

    static synchronized boolean silent(Context context, String key, long version) {
        SharedPreferences prefs = preferences(context);
        return isAssignment(key) && prefs.getString(SCOPE, "").equals(scope(context))
            && prefs.getLong(THROUGH, 0L) > 0L
            && version <= prefs.getLong(THROUGH, 0L);
    }

    private static String scope(Context context) {
        OperationalDeliveryLedger.Snapshot state = OperationalDeliveryLedger.snapshot(context);
        return state.contextKey + "|" + state.journalEpoch;
    }

    private static SharedPreferences preferences(Context context) {
        return context.getApplicationContext().getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME, Context.MODE_PRIVATE);
    }
}
