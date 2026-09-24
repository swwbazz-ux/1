package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.UUID;

/** Stable non-secret identifier used to correlate foreground and background heartbeats. */
final class AppInstallationIdentity {
    private static final String PREFS_NAME = "application_connection_identity";
    private static final String INSTALLATION_ID = "installation_id";

    private AppInstallationIdentity() {}

    static synchronized String get(Context context) {
        SharedPreferences preferences = context.getApplicationContext().getSharedPreferences(
            PREFS_NAME,
            Context.MODE_PRIVATE
        );
        String stored = preferences.getString(INSTALLATION_ID, "");
        if (stored != null && !stored.trim().isEmpty()) {
            return stored.trim();
        }
        String created = "android-" + UUID.randomUUID();
        preferences.edit().putString(INSTALLATION_ID, created).commit();
        return created;
    }
}
